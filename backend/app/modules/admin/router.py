from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import DB, Admin, CurrentUser
from app.core.config import get_settings
from app.db.models import AuditEvent, Job, PipelineRun, User
from app.modules import audit

router = APIRouter(tags=["admin"])


@router.get("/system/info")
def system_info(_: CurrentUser) -> dict[str, Any]:
    s = get_settings()
    return {
        "env": s.env,
        "model_name": s.model_name,
        "mlflow_tracking_uri": s.mlflow_tracking_uri if s.env != "production" else "configured",
        "deploy_target": s.deploy_target,
        "approval_policy": s.approval_policy,
        "gates": {"min_pr_auc": s.gate_min_pr_auc, "max_regression": s.gate_max_regression, "max_ece": s.gate_max_ece,
                  "min_slice_recall": s.gate_min_slice_recall, "max_p95_latency_ms": s.gate_max_p95_latency_ms},
        "drift": {"psi_warn": s.drift_psi_warn, "psi_alert": s.drift_psi_alert, "retrain_on_drift": s.retrain_on_drift},
    }  # fmt: skip


@router.get("/admin/stats")
def stats(db: DB, _: Admin) -> dict[str, Any]:
    return {
        "users": db.scalar(select(func.count()).select_from(User)) or 0,
        "runs_by_status": {
            k: v for k, v in db.execute(select(PipelineRun.status, func.count()).group_by(PipelineRun.status)).all()
        },
        "runs_by_trigger": {
            k: v for k, v in db.execute(select(PipelineRun.trigger, func.count()).group_by(PipelineRun.trigger)).all()
        },
        "audit_events": db.scalar(select(func.count()).select_from(AuditEvent)) or 0,
    }


@router.get("/admin/jobs")
def jobs(db: DB, _: Admin, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.scalars(select(Job).order_by(Job.created_at.desc()).limit(min(limit, 500)))
    return [{"id": j.id, "type": j.job_type, "status": j.status, "attempts": j.attempts, "payload": j.payload,
             "last_error": (j.last_error or "")[:500] or None, "created_at": j.created_at.isoformat()} for j in rows]  # fmt: skip


@router.get("/admin/audit")
def audit_events(db: DB, _: Admin, limit: int = 100) -> dict[str, Any]:
    rows = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(min(limit, 500)))
    emails = {f"user:{u.id}": u.email for u in db.scalars(select(User))}
    return {
        "chain": audit.verify_chain(db),
        "events": [{"id": e.id, "ts": e.ts.isoformat(), "actor": emails.get(e.actor, e.actor), "action": e.action,
                    "entity": e.entity, "entity_id": e.entity_id, "data": e.data, "hash": e.hash[:12]} for e in rows],
    }  # fmt: skip
