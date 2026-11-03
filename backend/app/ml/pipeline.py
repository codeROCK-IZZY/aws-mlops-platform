"""The ML pipeline: ingest → validate → train → evaluate → gate → register → approve → deploy → monitor.

Each operation runs as a tracked ``PipelineRun`` with an ordered list of steps (name, status,
timing, detail), so the dashboard can show exactly what happened and why a run stopped. Runs are
executed by the worker (PostgreSQL job queue); on AWS the same entry points run as a scheduled ECS
task.

Retraining is triggered three ways: manually, on a schedule, or automatically when monitoring
raises a drift alert. However it is triggered, a new model only reaches production through the
promotion gates, and, with ``approval_policy = manual``, a human approval.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import Dataset, MonitoringReport, PipelineRun, Promotion
from app.jobs import queue
from app.ml import contract, deploy, drift, gates, metrics, registry, storage
from app.ml.generator import LABEL, BatchSpec, generate_batch
from app.ml.train import FEATURES, train
from app.modules import audit

TRAIN_WINDOW = 4  # months of data per training run (the last one picks the threshold)


class PipelineBlocked(Exception):
    """A step decided the run must stop (contract failure, gates failed). Not an error."""


class StepRecorder:
    def __init__(self, db: Session, run: PipelineRun):
        self.db, self.run = db, run

    @contextmanager
    def step(self, name: str) -> Iterator[dict[str, Any]]:
        detail: dict[str, Any] = {}
        record: dict[str, Any] = {"name": name, "status": "running", "started_at": datetime.now(UTC).isoformat()}
        self.run.steps = [*self.run.steps, record]
        self.db.commit()
        t0 = time.perf_counter()
        status = "succeeded"
        try:
            yield detail
        except PipelineBlocked:
            status = "blocked"
            raise
        except Exception as exc:
            status = "failed"
            detail.setdefault("error", f"{type(exc).__name__}: {exc}"[:500])
            raise
        finally:
            done = {**record, "status": status, "seconds": round(time.perf_counter() - t0, 2), "detail": detail}
            self.run.steps = [*self.run.steps[:-1], done]
            self.db.commit()


def create_run(db: Session, kind: str, params: dict[str, Any], *, trigger: str = "manual",
               created_by: str | None = None) -> PipelineRun:  # fmt: skip
    run = PipelineRun(kind=kind, params=params, trigger=trigger, created_by=created_by, status="queued", steps=[])
    db.add(run)
    db.flush()
    queue.enqueue(db, "pipeline_run", {"run_id": str(run.id)}, max_attempts=1)
    return run


def execute(db: Session, run_id: uuid.UUID) -> PipelineRun:
    run = db.get(PipelineRun, run_id)
    if run is None:
        raise ValueError(f"run {run_id} not found")
    run.status, run.started_at = "running", datetime.now(UTC)
    db.commit()
    rec = StepRecorder(db, run)
    handler: Callable[[Session, PipelineRun, StepRecorder], dict[str, Any]] = {
        "ingest": _ingest, "train": _train, "monitor": _monitor,
    }[run.kind]  # fmt: skip
    try:
        run.result = handler(db, run, rec)
        run.status = "succeeded"
    except PipelineBlocked as exc:
        run.status, run.result = "blocked", {**(run.result or {}), "reason": str(exc)}
    except Exception as exc:
        db.rollback()
        run = db.get(PipelineRun, run_id) or run
        run.status, run.result = "failed", {"error": f"{type(exc).__name__}: {exc}"[:1000]}
    run.finished_at = datetime.now(UTC)
    db.commit()
    return run


# ---------------------------------------------------------------------- ingest
def _ingest(db: Session, run: PipelineRun, rec: StepRecorder) -> dict[str, Any]:
    month, scenario = run.params["month"], run.params.get("scenario", "none")
    with rec.step("receive") as d:
        df = generate_batch(BatchSpec(month=month, scenario=scenario))
        raw = storage.put_batch(month, df, stage="raw")
        d.update(rows=raw.rows, key=raw.key, sha256=raw.sha256[:16])
    with rec.step("validate") as d:
        report = contract.validate(df)
        d.update(passed=report.passed, errors=report.errors, warnings=report.warnings)
        ds = db.get(Dataset, month) or Dataset(month=month)
        ds.scenario, ds.contract, ds.rows, ds.status = scenario, report.as_dict(), len(df), "rejected"
        db.add(ds)
        db.commit()
        if not report.passed:
            raise PipelineBlocked(f"{month} failed the data contract: {'; '.join(report.errors[:3])}")
    with rec.step("publish") as d:
        ref = storage.put_batch(
            month, df, stage="validated", manifest={"scenario": scenario, "contract": report.as_dict()}
        )
        ds.status, ds.sha256 = "validated", ref.sha256
        d.update(key=ref.key, sha256=ref.sha256[:16])
    db.commit()
    return {"month": month, "rows": len(df), "sha256": ds.sha256, "label_rate": report.stats.get("label_rate")}


# ---------------------------------------------------------------------- train, evaluate, gate, promote
def validated_months(db: Session) -> list[str]:
    return list(db.scalars(select(Dataset.month).where(Dataset.status == "validated").order_by(Dataset.month)))


def _train(db: Session, run: PipelineRun, rec: StepRecorder) -> dict[str, Any]:
    s = get_settings()
    months = validated_months(db)
    holdout = run.params.get("holdout_month") or (months[-1] if months else None)
    if holdout is None or holdout not in months:
        raise PipelineBlocked("no validated holdout month")
    earlier = [m for m in months if m < holdout]
    window = run.params.get("train_months") or earlier[-TRAIN_WINDOW:]
    if len(window) < 2:
        raise PipelineBlocked("need at least two validated months before the holdout month")

    with rec.step("train") as d:
        result = train(window, holdout, algorithm=run.params.get("algorithm", "xgboost"),
                       tags={"pipeline_run": str(run.id), "trigger": run.trigger})  # fmt: skip
        d.update(version=result.version, run_id=result.run_id, train_months=window, holdout_month=holdout,
                 pr_auc=result.metrics["pr_auc"], recall=result.metrics["recall"], threshold=round(result.threshold, 4))  # fmt: skip
    run.model_version = result.version
    db.commit()

    with rec.step("evaluate_champion") as d:
        champion = registry.get_alias(registry.CHAMPION)
        champ_metrics = None
        if champion is not None:
            hold = storage.get_batch(holdout)
            p = registry.load_model(champion.version).predict_proba(hold[FEATURES])[:, 1]
            champ_metrics = metrics.evaluate(
                hold[LABEL].to_numpy(), p, champion.threshold, hold["equipment_type"].to_numpy()
            )
            d.update(version=champion.version, pr_auc=champ_metrics["pr_auc"], recall=champ_metrics["recall"])
        else:
            d.update(version=None, note="no champion yet")

    with rec.step("gates") as d:
        ds = db.get(Dataset, holdout)
        report = gates.run_gates(result.metrics, champ_metrics, s, contract_passed=bool(ds and ds.status == "validated"),
                                 min_improvement=s.gate_min_improvement_on_drift if run.trigger == "drift" else 0.0)  # fmt: skip
        d.update(passed=report.passed, checks=[c.__dict__ for c in report.checks])
        registry.client().set_model_version_tag(
            s.model_name, result.version, "gates", "passed" if report.passed else "failed"
        )
        storage.put_json(f"reports/gates/v{result.version}.json", report.as_dict())
        if not report.passed:
            failed = ", ".join(c.name for c in report.checks if not c.passed)
            raise PipelineBlocked(f"version {result.version} failed the promotion gates: {failed}")

    with rec.step("register") as d:
        registry.set_challenger(result.version)
        promo = Promotion(version=result.version, from_version=champion.version if champion else None,
                          action="promote", status="pending", gate_report=report.as_dict(),
                          requested_by=f"pipeline:{run.trigger}", pipeline_run_id=run.id)  # fmt: skip
        db.add(promo)
        db.flush()
        d.update(alias=registry.CHALLENGER, promotion_id=str(promo.id), approval_policy=s.approval_policy)
        audit.record(db, "pipeline", "model.registered", "model_version", result.version, gates="passed")
    db.commit()

    if s.approval_policy == "auto" or (champion is None and run.params.get("auto_first", True)):
        with rec.step("approve") as d:
            approve(db, promo, by="policy:auto" if s.approval_policy == "auto" else "policy:first-model")
            d.update(by=promo.decided_by)
        with rec.step("deploy") as d:
            d.update(**deploy.deploy(result.version))
    return {"version": result.version, "gates": "passed", "promotion": str(promo.id), "status": promo.status}


def approve(db: Session, promo: Promotion, *, by: str) -> None:
    """Promote an approved candidate: move the champion alias and record who decided."""
    if promo.status != "pending":
        raise ValueError(f"promotion is already {promo.status}")
    previous = registry.promote(promo.version, by=by)
    promo.status, promo.decided_by, promo.decided_at = "approved", by, datetime.now(UTC)
    promo.from_version = previous.version if previous else promo.from_version
    audit.record(db, by, "model.promoted", "model_version", promo.version,
                 previous=promo.from_version, promotion=str(promo.id))  # fmt: skip


def reject(db: Session, promo: Promotion, *, by: str, note: str = "") -> None:
    if promo.status != "pending":
        raise ValueError(f"promotion is already {promo.status}")
    promo.status, promo.decided_by, promo.decided_at, promo.note = "rejected", by, datetime.now(UTC), note
    audit.record(db, by, "model.rejected", "model_version", promo.version, note=note)


def rollback(db: Session, *, by: str, note: str = "") -> Promotion:
    old, new = registry.rollback(by=by)
    promo = Promotion(version=new, from_version=old, action="rollback", status="approved", requested_by=by,
                      decided_by=by, decided_at=datetime.now(UTC), note=note, gate_report={})  # fmt: skip
    db.add(promo)
    audit.record(db, by, "model.rolled_back", "model_version", new, previous=old, note=note)
    deploy.deploy(new)
    return promo


# ---------------------------------------------------------------------- monitor
@dataclass
class Assessment:
    status: str  # ok | warn | alert
    drift: dict[str, Any]
    performance: dict[str, Any]
    baseline: float
    lost_ranking: bool
    alert_fatigue: bool


def assess(df: pd.DataFrame, scores: np.ndarray, champion: registry.ModelVersion, reference: dict[str, Any],
           s: Settings) -> Assessment:  # fmt: skip
    """The monitoring decision for one month: input/prediction drift plus measured performance.

    *alert* if any feature or the prediction distribution moved past the PSI alert level, if PR-AUC
    fell more than ``monitor_max_pr_auc_drop`` below the champion's own holdout PR-AUC, or if
    precision at the operating threshold collapsed (maintenance crews drowning in false alarms).
    """
    report = drift.compare(reference, df, scores, warn=s.drift_psi_warn, alert=s.drift_psi_alert)
    perf = metrics.evaluate(df[LABEL].to_numpy(), scores, champion.threshold, df["equipment_type"].to_numpy())
    baseline = float(champion.tags.get("pr_auc", "0") or 0)
    lost_ranking = perf["pr_auc"] < baseline - s.monitor_max_pr_auc_drop
    alert_fatigue = perf["precision"] < s.monitor_min_precision
    status = "alert" if report["status"] == "alert" or lost_ranking or alert_fatigue else report["status"]
    return Assessment(status, report, perf, baseline, lost_ranking, alert_fatigue)


def publish_metrics(month: str, status: str, perf: dict[str, Any], s: Settings, *, client: Any | None = None) -> None:
    """On AWS, publish the monitoring outcome to CloudWatch (namespace MLOps/Monitoring), where the
    ``model-drift`` alarm notifies the on-call engineer. Locally the dashboard is the alerting surface."""
    if s.deploy_target != "sagemaker" and client is None:
        return
    import boto3

    cw = client or boto3.client("cloudwatch", region_name=s.s3_region)
    dims = [{"Name": "Model", "Value": s.model_name}]
    cw.put_metric_data(Namespace="MLOps/Monitoring", MetricData=[
        {"MetricName": "DriftAlert", "Dimensions": dims, "Value": 1.0 if status == "alert" else 0.0, "Unit": "Count"},
        {"MetricName": "PrAuc", "Dimensions": dims, "Value": float(perf["pr_auc"]), "Unit": "None"},
        {"MetricName": "AlertPrecision", "Dimensions": dims, "Value": float(perf["precision"]), "Unit": "None"},
    ])  # fmt: skip


def _monitor(db: Session, run: PipelineRun, rec: StepRecorder) -> dict[str, Any]:
    """Score a month of production data with the champion, then compare it with the training reference.

    Labels for a month are complete once it is over (failures are known within 7 days), so the same
    batch also gives measured performance. In live operation the drift part runs daily and the
    performance part once labels mature.
    """
    s = get_settings()
    month = run.params["month"]
    champion = registry.get_alias(registry.CHAMPION)
    if champion is None or champion.run_id is None:
        raise PipelineBlocked("no champion model to monitor")
    with rec.step("score") as d:
        df = storage.get_batch(month)
        scores = registry.load_model(champion.version).predict_proba(df[FEATURES])[:, 1]
        d.update(rows=len(df), version=champion.version, alerts=int((scores >= champion.threshold).sum()))
    with rec.step("drift") as d:
        a = assess(df, scores, champion, registry.load_reference(champion.run_id), s)
        report, perf = a.drift, a.performance
        d.update(status=report["status"], drifted=report["drifted_features"], max_feature=report["max_feature_psi"],
                 prediction_psi=report.get("prediction", {}).get("psi"))  # fmt: skip
    with rec.step("performance") as d:
        d.update(pr_auc=perf["pr_auc"], recall=perf["recall"], precision=perf["precision"], baseline_pr_auc=a.baseline,
                 degraded=a.lost_ranking or a.alert_fatigue, lost_ranking=a.lost_ranking, alert_fatigue=a.alert_fatigue)  # fmt: skip
    status = a.status
    mr = MonitoringReport(month=month, model_version=champion.version, drift=report, performance=perf,
                          status=status, pipeline_run_id=run.id)  # fmt: skip
    db.add(mr)
    db.flush()
    publish_metrics(month, status, perf, s)
    triggered = None
    if status == "alert" and s.retrain_on_drift and not run.params.get("no_retrain"):
        with rec.step("trigger_retrain") as d:
            if a.lost_ranking:
                reason = f"PR-AUC fell to {perf['pr_auc']:.2f} (baseline {a.baseline:.2f})"
            elif a.alert_fatigue:
                reason = f"alert precision fell to {perf['precision']:.0%}"
            else:
                reason = f"drift in {', '.join(report['drifted_features'][:3]) or 'predictions'}"
            nxt = create_run(db, "train", {"holdout_month": month, "reason": reason}, trigger="drift")
            mr.retrain_run_id = triggered = nxt.id
            d.update(run_id=str(nxt.id), reason=reason)
    db.commit()
    return {"month": month, "status": status, "version": champion.version, "pr_auc": perf["pr_auc"],
            "retrain_run": str(triggered) if triggered else None}  # fmt: skip


def monthly_frame(months: list[str]) -> pd.DataFrame:
    return pd.concat([storage.get_batch(m) for m in months], ignore_index=True)
