"""Initial schema.

Revision ID: 0001
Revises:
Create Date: 2026-11-03
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email varchar(320) NOT NULL UNIQUE,
    full_name varchar(200) NOT NULL DEFAULT '',
    password_hash varchar(255) NOT NULL,
    role varchar(20) NOT NULL CHECK (role IN ('admin', 'approver', 'engineer', 'viewer')),
    is_active boolean NOT NULL DEFAULT true,
    failed_login_count integer NOT NULL DEFAULT 0,
    locked_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE refresh_tokens (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash varchar(64) NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_refresh_tokens_user_id ON refresh_tokens (user_id);

CREATE TABLE jobs (
    id bigserial PRIMARY KEY,
    job_type varchar(50) NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status varchar(20) NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 3,
    run_after timestamptz NOT NULL DEFAULT now(),
    locked_at timestamptz,
    finished_at timestamptz,
    last_error text,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_jobs_claim ON jobs (run_after, id) WHERE status = 'queued';

-- Tamper-evident audit trail (hash chain; UPDATE/DELETE rejected by trigger).
CREATE TABLE audit_events (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    actor varchar(100) NOT NULL,
    action varchar(100) NOT NULL,
    entity varchar(50) NOT NULL,
    entity_id varchar(100),
    run_id uuid,
    data jsonb NOT NULL DEFAULT '{}'::jsonb,
    prev_hash varchar(64) NOT NULL,
    hash varchar(64) NOT NULL
);
CREATE FUNCTION audit_events_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END $$;
CREATE TRIGGER trg_audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_immutable();

CREATE TABLE datasets (
    month varchar(7) PRIMARY KEY CHECK (month ~ '^[0-9]{4}-[0-9]{2}$'),
    status varchar(12) NOT NULL CHECK (status IN ('validated', 'rejected')),
    scenario varchar(80) NOT NULL DEFAULT 'none',
    rows integer NOT NULL DEFAULT 0,
    sha256 varchar(64),
    contract jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE pipeline_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind varchar(12) NOT NULL CHECK (kind IN ('ingest', 'train', 'monitor')),
    trigger varchar(12) NOT NULL DEFAULT 'manual' CHECK (trigger IN ('manual', 'schedule', 'drift')),
    status varchar(12) NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'succeeded', 'blocked', 'failed')),
    params jsonb NOT NULL DEFAULT '{}'::jsonb,
    steps jsonb NOT NULL DEFAULT '[]'::jsonb,
    result jsonb,
    model_version varchar(20),
    created_by varchar(100),
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);
CREATE INDEX ix_pipeline_runs_created_at ON pipeline_runs (created_at DESC);

CREATE TABLE promotions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version varchar(20) NOT NULL,
    from_version varchar(20),
    action varchar(10) NOT NULL CHECK (action IN ('promote', 'rollback')),
    status varchar(10) NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    gate_report jsonb NOT NULL DEFAULT '{}'::jsonb,
    requested_by varchar(100) NOT NULL,
    decided_by varchar(100),
    decided_at timestamptz,
    note text NOT NULL DEFAULT '',
    pipeline_run_id uuid REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- At most one pending promotion per version.
CREATE UNIQUE INDEX uq_promotions_pending ON promotions (version) WHERE status = 'pending';

CREATE TABLE monitoring_reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    month varchar(7) NOT NULL,
    model_version varchar(20) NOT NULL,
    status varchar(8) NOT NULL CHECK (status IN ('ok', 'warn', 'alert')),
    drift jsonb NOT NULL DEFAULT '{}'::jsonb,
    performance jsonb NOT NULL DEFAULT '{}'::jsonb,
    pipeline_run_id uuid REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    retrain_run_id uuid REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_monitoring_reports_month ON monitoring_reports (month);

CREATE TABLE predictions (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    request_id varchar(64) NOT NULL,
    model_version varchar(20) NOT NULL,
    unit_id varchar(40),
    features jsonb NOT NULL DEFAULT '{}'::jsonb,
    score double precision NOT NULL,
    alert boolean NOT NULL,
    latency_ms double precision NOT NULL
);
CREATE INDEX ix_predictions_ts ON predictions (ts DESC);
"""

DOWNGRADE = """
DROP TABLE IF EXISTS predictions, monitoring_reports, promotions, pipeline_runs, datasets, audit_events, jobs,
    refresh_tokens, users CASCADE;
DROP FUNCTION IF EXISTS audit_events_immutable();
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
