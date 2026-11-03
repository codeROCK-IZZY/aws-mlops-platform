"""Job handlers run by the worker."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.ml import pipeline


def pipeline_run(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    run = pipeline.execute(db, uuid.UUID(payload["run_id"]))
    return {"run_id": str(run.id), "status": run.status}


HANDLERS: dict[str, Callable[[Session, dict[str, Any]], dict[str, Any] | None]] = {
    "pipeline_run": pipeline_run,
}
