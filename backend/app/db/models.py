"""SQLAlchemy ORM models mirroring the explicit-SQL Alembic migration.

MLflow owns models, runs and metrics. This database owns what MLflow doesn't: users, the dataset
catalogue, pipeline runs and their steps, promotion decisions, monitoring reports, captured
predictions, the job queue and the audit trail.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, MetaData, String, Text, func
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


class Base(DeclarativeBase):
    metadata = metadata


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=sql_text("gen_random_uuid()")
    )


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _jsonb(default: str = "'{}'::jsonb") -> Mapped[Any]:
    return mapped_column(JSONB, nullable=False, server_default=sql_text(default))


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # admin | approver | engineer | viewer
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("true"))
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = _jsonb()
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()


class AuditEvent(Base):
    """Append-only, hash-chained. Write only through ``app.modules.audit.record``."""

    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = _created()
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(100))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    data: Mapped[dict[str, Any]] = _jsonb()
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)


class Dataset(Base):
    __tablename__ = "datasets"
    month: Mapped[str] = mapped_column(String(7), primary_key=True)  # YYYY-MM
    status: Mapped[str] = mapped_column(String(12), nullable=False)  # validated | rejected
    scenario: Mapped[str] = mapped_column(String(80), nullable=False, server_default="none")
    rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    sha256: Mapped[str | None] = mapped_column(String(64))
    contract: Mapped[dict[str, Any]] = _jsonb()
    created_at: Mapped[datetime] = _created()


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # ingest | train | monitor
    trigger: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default="manual"
    )  # manual | schedule | drift
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default="queued")
    params: Mapped[dict[str, Any]] = _jsonb()
    steps: Mapped[list[dict[str, Any]]] = _jsonb("'[]'::jsonb")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    model_version: Mapped[str | None] = mapped_column(String(20))
    created_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Promotion(Base):
    __tablename__ = "promotions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    from_version: Mapped[str | None] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(10), nullable=False)  # promote | rollback
    status: Mapped[str] = mapped_column(String(10), nullable=False)  # pending | approved | rejected
    gate_report: Mapped[dict[str, Any]] = _jsonb()
    requested_by: Mapped[str] = mapped_column(String(100), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pipeline_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created()


class MonitoringReport(Base):
    __tablename__ = "monitoring_reports"
    id: Mapped[uuid.UUID] = _uuid_pk()
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    model_version: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False)  # ok | warn | alert
    drift: Mapped[dict[str, Any]] = _jsonb()
    performance: Mapped[dict[str, Any]] = _jsonb()
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pipeline_runs.id", ondelete="SET NULL")
    )
    retrain_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pipeline_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created()


class Prediction(Base):
    """Captured online predictions (sampled), for drift on live traffic and label joins later."""

    __tablename__ = "predictions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = _created()
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(20), nullable=False)
    unit_id: Mapped[str | None] = mapped_column(String(40))
    features: Mapped[dict[str, Any]] = _jsonb()
    score: Mapped[float] = mapped_column(Float, nullable=False)
    alert: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
