"""Test fixtures.

* PostgreSQL: a real database (``mlp_test``), schema rebuilt from the migration per session.
* S3: an in-process moto server, so boto3 talks HTTP exactly as it does to MinIO or AWS.
* MLflow: a throwaway SQLite tracking store and artifact directory (registry aliases included).

Nothing touches AWS or needs credentials.
"""

import os
import socket
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

_TMP = Path(os.environ.setdefault("MLP_TEST_TMP", tempfile.mkdtemp(prefix="mlp-tests-")))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# setdefault: this module is imported twice (as the pytest conftest and as ``tests.conftest``); both must agree.
S3_PORT = int(os.environ.setdefault("MLP_TEST_S3_PORT", str(_free_port())))
os.environ.setdefault("MLP_DATABASE_URL", "postgresql+psycopg://mlp:mlp@127.0.0.1:5437/mlp_test")
os.environ["MLP_JWT_SECRET"] = "test-secret-not-for-production-0123456789abcdef"
os.environ["MLP_ENV"] = "test"
os.environ["MLP_COOKIE_SECURE"] = "false"
os.environ["MLP_LOG_JSON"] = "false"
os.environ["MLP_LOG_LEVEL"] = "WARNING"
os.environ["MLP_S3_ENDPOINT_URL"] = f"http://127.0.0.1:{S3_PORT}"
os.environ["MLP_MLFLOW_TRACKING_URI"] = f"sqlite:///{(_TMP / 'mlflow.db').as_posix()}"
os.environ["MLFLOW_ARTIFACT_ROOT"] = (_TMP / "artifacts").as_posix()
os.environ["MLP_SERVING_URL"] = "http://127.0.0.1:9"  # nothing listens: deploy() records a warning
os.environ["MLP_APPROVAL_POLICY"] = "manual"
os.environ["MLP_CAPTURE_DB"] = "false"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR"] = "false"

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from moto.server import ThreadedMotoServer
from sqlalchemy import text

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import User
from app.db.session import get_engine, new_session

BACKEND = Path(__file__).resolve().parent.parent
PASSWORD = "Correct-Horse-Battery-9"
TABLES = ("predictions, monitoring_reports, promotions, pipeline_runs, datasets, jobs, audit_events, refresh_tokens, "
          "users")  # fmt: skip


def _wait_for_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"moto server did not start on port {port}")


def _alembic(direction: str) -> None:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.attributes["database_url"] = get_settings().database_url
    cfg.attributes["skip_logging_config"] = True
    (command.downgrade if direction == "down" else command.upgrade)(cfg, "base" if direction == "down" else "head")


@pytest.fixture(scope="session", autouse=True)
def infrastructure() -> Iterator[None]:
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=S3_PORT, verbose=False)
    server.start()
    _wait_for_port(S3_PORT)
    import mlflow

    mlflow.set_tracking_uri(os.environ["MLP_MLFLOW_TRACKING_URI"])
    if mlflow.get_experiment_by_name(get_settings().experiment_name) is None:
        mlflow.create_experiment(get_settings().experiment_name, artifact_location=os.environ["MLFLOW_ARTIFACT_ROOT"])
    from app.ml import storage

    storage.ensure_buckets()
    _alembic("down")
    _alembic("up")
    yield
    server.stop()


def reset_database() -> None:
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    from app.modules.auth.router import login_limiter

    login_limiter.reset()


@pytest.fixture
def clean_db() -> None:
    reset_database()


@pytest.fixture
def client(clean_db: None) -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as c:
        yield c


def create_user(email: str, role: str = "engineer", password: str = PASSWORD, active: bool = True) -> User:
    with new_session() as s:
        u = User(email=email, full_name=email.split("@")[0], password_hash=hash_password(password), role=role,
                 is_active=active)  # fmt: skip
        s.add(u)
        s.commit()
        s.refresh(u)
        return u


def login(client: TestClient, email: str, password: str = PASSWORD) -> dict[str, str]:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def auth(client: TestClient) -> Callable[..., dict[str, str]]:
    """``auth("erin", "engineer")`` → headers for erin@example.com (created on first use)."""
    made: dict[str, dict[str, str]] = {}

    def _auth(name: str, role: str = "engineer") -> dict[str, str]:
        if name not in made:
            create_user(f"{name}@example.com", role)
            made[name] = login(client, f"{name}@example.com")
        return made[name]

    return _auth
