"""Run the lifecycle evaluation without MinIO or an MLflow server (CI, laptops).

S3 is an in-process moto server and MLflow uses a throwaway SQLite store; PostgreSQL is whatever
``MLP_DATABASE_URL`` points at. Everything else (pipeline, gates, registry, worker) is the real code.

    python scripts/lifecycle_offline.py --out ../eval/reports [--gate]
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mlp-lifecycle-"))
    port = _free_port()
    os.environ.setdefault("MLP_JWT_SECRET", "offline-evaluation-secret-0123456789abcdef")
    os.environ["MLP_S3_ENDPOINT_URL"] = f"http://127.0.0.1:{port}"
    os.environ["MLP_MLFLOW_TRACKING_URI"] = f"sqlite:///{(tmp / 'mlflow.db').as_posix()}"
    os.environ["MLP_SERVING_URL"] = "http://127.0.0.1:9"
    os.environ["MLP_LOG_LEVEL"] = "WARNING"
    os.environ.setdefault("MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR", "false")
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        os.environ.setdefault(k, v)

    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    deadline = time.monotonic() + 15
    while socket.socket().connect_ex(("127.0.0.1", port)) != 0:
        if time.monotonic() > deadline:
            sys.exit("moto server did not start")
        time.sleep(0.1)
    try:
        import mlflow

        from app import cli
        from app.core.config import get_settings

        mlflow.set_tracking_uri(os.environ["MLP_MLFLOW_TRACKING_URI"])
        mlflow.create_experiment(get_settings().experiment_name, artifact_location=(tmp / "artifacts").as_posix())
        cli.main(["migrate"])
        cli.main(["lifecycle", "--reset", *sys.argv[1:]])
    finally:
        server.stop()


if __name__ == "__main__":
    main()
