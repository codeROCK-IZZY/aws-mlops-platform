"""Platform API: datasets, pipeline runs, model versions, promotions, monitoring and predictions.

Roles: ``viewer`` reads everything; ``engineer`` ingests data and starts training and monitoring
runs; ``approver`` approves, rejects or rolls back production models; ``admin`` manages users.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import DB, Approver, Engineer, Viewer
from app.core.config import get_settings
from app.core.errors import Conflict, NotFound, ServiceUnavailable, UnprocessableData
from app.db.models import Dataset, MonitoringReport, PipelineRun, Prediction, Promotion
from app.ml import pipeline, registry
from app.ml.generator import BatchSpec

router = APIRouter(tags=["platform"])


def _run(r: PipelineRun) -> dict[str, Any]:
    return {"id": str(r.id), "kind": r.kind, "trigger": r.trigger, "status": r.status, "params": r.params,
            "steps": r.steps, "result": r.result, "model_version": r.model_version, "created_by": r.created_by,
            "created_at": r.created_at.isoformat(), "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None}  # fmt: skip


def _promotion(p: Promotion) -> dict[str, Any]:
    return {"id": str(p.id), "version": p.version, "from_version": p.from_version, "action": p.action,
            "status": p.status, "gate_report": p.gate_report, "requested_by": p.requested_by,
            "decided_by": p.decided_by, "decided_at": p.decided_at.isoformat() if p.decided_at else None,
            "note": p.note, "created_at": p.created_at.isoformat(),
            "pipeline_run_id": str(p.pipeline_run_id) if p.pipeline_run_id else None}  # fmt: skip


def _version(v: registry.ModelVersion | None) -> dict[str, Any] | None:
    if v is None:
        return None
    return {"version": v.version, "aliases": v.aliases, "threshold": v.threshold, "tags": v.tags,
            "run_id": v.run_id, "created_at": v.created_at}  # fmt: skip


def _report(m: MonitoringReport) -> dict[str, Any]:
    return {"id": str(m.id), "month": m.month, "model_version": m.model_version, "status": m.status,
            "drift": m.drift, "performance": m.performance, "created_at": m.created_at.isoformat(),
            "retrain_run_id": str(m.retrain_run_id) if m.retrain_run_id else None}  # fmt: skip


# ---------------------------------------------------------------------- overview
@router.get("/overview")
def overview(db: DB, _: Viewer) -> dict[str, Any]:
    latest = db.scalar(select(MonitoringReport).order_by(MonitoringReport.created_at.desc()).limit(1))
    runs = db.scalars(select(PipelineRun).order_by(PipelineRun.created_at.desc()).limit(8))
    return {
        "champion": _version(registry.get_alias(registry.CHAMPION)),
        "challenger": _version(registry.get_alias(registry.CHALLENGER)),
        "pending_promotions": db.scalar(select(func.count()).select_from(Promotion).where(Promotion.status == "pending")) or 0,
        "datasets": dict(db.execute(select(Dataset.status, func.count()).group_by(Dataset.status)).all()),
        "latest_monitoring": _report(latest) if latest else None,
        "recent_runs": [_run(r) for r in runs],
        "settings": {"approval_policy": get_settings().approval_policy, "deploy_target": get_settings().deploy_target,
                     "psi_alert": get_settings().drift_psi_alert, "retrain_on_drift": get_settings().retrain_on_drift},
    }  # fmt: skip


# ---------------------------------------------------------------------- datasets
class IngestIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    scenario: str = Field(default="none", max_length=80, description='e.g. "none", "sensor_drift+fleet_mix"')


@router.get("/datasets")
def datasets(db: DB, _: Viewer) -> list[dict[str, Any]]:
    rows = db.scalars(select(Dataset).order_by(Dataset.month))
    return [{"month": d.month, "status": d.status, "scenario": d.scenario, "rows": d.rows, "sha256": d.sha256,
             "contract": d.contract, "created_at": d.created_at.isoformat()} for d in rows]  # fmt: skip


@router.post("/datasets/ingest", status_code=202)
def ingest(body: IngestIn, db: DB, user: Engineer) -> dict[str, Any]:
    try:
        BatchSpec(body.month, body.scenario)
    except ValueError as exc:
        raise UnprocessableData(str(exc)) from exc
    run = pipeline.create_run(db, "ingest", body.model_dump(), created_by=user.email)
    db.commit()
    return _run(run)


# ---------------------------------------------------------------------- runs
class TrainIn(BaseModel):
    holdout_month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    algorithm: Literal["xgboost", "logistic"] = "xgboost"


class MonitorIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    no_retrain: bool = False


@router.get("/runs")
def runs(db: DB, _: Viewer, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    stmt = select(PipelineRun).order_by(PipelineRun.created_at.desc()).limit(min(limit, 200))
    if kind:
        stmt = stmt.where(PipelineRun.kind == kind)
    return [_run(r) for r in db.scalars(stmt)]


@router.get("/runs/{run_id}")
def run_detail(run_id: uuid.UUID, db: DB, _: Viewer) -> dict[str, Any]:
    r = db.get(PipelineRun, run_id)
    if r is None:
        raise NotFound("Run not found")
    return _run(r)


@router.post("/runs/train", status_code=202)
def start_training(body: TrainIn, db: DB, user: Engineer) -> dict[str, Any]:
    run = pipeline.create_run(db, "train", body.model_dump(exclude_none=True), created_by=user.email)
    db.commit()
    return _run(run)


@router.post("/runs/monitor", status_code=202)
def start_monitoring(body: MonitorIn, db: DB, user: Engineer) -> dict[str, Any]:
    if db.get(Dataset, body.month) is None:
        raise NotFound(f"No dataset for {body.month}")
    run = pipeline.create_run(db, "monitor", body.model_dump(), created_by=user.email)
    db.commit()
    return _run(run)


# ---------------------------------------------------------------------- models and promotions
@router.get("/models")
def models(_: Viewer) -> list[dict[str, Any]]:
    out = []
    for v in registry.list_versions():
        d = _version(v) or {}
        try:
            run = registry.client().get_run(v.run_id) if v.run_id else None
            d["metrics"] = dict(run.data.metrics) if run else {}
        except Exception:
            d["metrics"] = {}
        out.append(d)
    return out


@router.get("/promotions")
def promotions(db: DB, _: Viewer, status: str = "all") -> list[dict[str, Any]]:
    stmt = select(Promotion).order_by(Promotion.created_at.desc()).limit(100)
    if status != "all":
        stmt = stmt.where(Promotion.status == status)
    return [_promotion(p) for p in db.scalars(stmt)]


class DecisionIn(BaseModel):
    note: str = Field(default="", max_length=1000)


def _pending(db: DB, promotion_id: uuid.UUID) -> Promotion:
    p = db.get(Promotion, promotion_id, with_for_update=True)
    if p is None:
        raise NotFound("Promotion not found")
    if p.status != "pending":
        raise Conflict(f"This promotion is already {p.status}")
    return p


@router.post("/promotions/{promotion_id}/approve")
def approve(promotion_id: uuid.UUID, body: DecisionIn, db: DB, user: Approver) -> dict[str, Any]:
    p = _pending(db, promotion_id)
    p.note = body.note
    pipeline.approve(db, p, by=user.email)
    db.commit()
    from app.ml import deploy

    return {**_promotion(p), "deployment": deploy.deploy(p.version)}


@router.post("/promotions/{promotion_id}/reject")
def reject(promotion_id: uuid.UUID, body: DecisionIn, db: DB, user: Approver) -> dict[str, Any]:
    p = _pending(db, promotion_id)
    pipeline.reject(db, p, by=user.email, note=body.note)
    db.commit()
    return _promotion(p)


@router.post("/models/rollback")
def rollback(body: DecisionIn, db: DB, user: Approver) -> dict[str, Any]:
    try:
        p = pipeline.rollback(db, by=user.email, note=body.note)
    except ValueError as exc:
        raise Conflict(str(exc)) from exc
    db.commit()
    return _promotion(p)


# ---------------------------------------------------------------------- monitoring and predictions
@router.get("/monitoring")
def monitoring(db: DB, _: Viewer) -> list[dict[str, Any]]:
    return [
        _report(m)
        for m in db.scalars(select(MonitoringReport).order_by(MonitoringReport.month, MonitoringReport.created_at))
    ]


@router.get("/predictions/recent")
def recent_predictions(db: DB, _: Viewer, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.scalars(select(Prediction).order_by(Prediction.id.desc()).limit(min(limit, 500)))
    return [{"ts": p.ts.isoformat(), "unit_id": p.unit_id, "model_version": p.model_version, "score": p.score,
             "alert": p.alert, "latency_ms": p.latency_ms} for p in rows]  # fmt: skip


@router.post("/predict")
def predict(body: dict[str, Any], _: Viewer) -> dict[str, Any]:
    """Proxy to the model server, so the dashboard can try the live model."""
    try:
        r = httpx.post(f"{get_settings().serving_url}/v1/predict", json=body, timeout=15)
    except httpx.HTTPError as exc:
        raise ServiceUnavailable("The model server is not reachable") from exc
    if r.status_code >= 400:
        raise UnprocessableData(
            r.json().get("detail", "prediction failed")
            if r.headers.get("content-type", "").startswith("application/json")
            else "prediction failed"
        )
    return dict(r.json())
