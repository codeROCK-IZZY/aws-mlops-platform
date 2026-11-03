"""End-to-end through the API and the worker: ingest → train → gates → approval → rollback → monitor,
plus the model server's SageMaker container contract. Real PostgreSQL, real MLflow (SQLite store),
S3 via moto.
"""

import contextlib
import time
from collections.abc import Callable, Iterator

import mlflow
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.jobs.worker import drain
from app.ml import registry, storage
from app.ml.generator import FEATURES_CATEGORICAL, FEATURES_NUMERIC, BatchSpec, generate_batch

Auth = Callable[..., dict[str, str]]
API = "/api/v1"


@pytest.fixture
def fresh_registry() -> None:
    with contextlib.suppress(mlflow.exceptions.MlflowException):
        registry.client().delete_registered_model(get_settings().model_name)
    registry.load_model.cache_clear()


def _run(client: TestClient, headers: dict[str, str], path: str, body: dict) -> dict:
    r = client.post(f"{API}{path}", json=body, headers=headers)
    assert r.status_code == 202, r.text
    drain()
    run = client.get(f"{API}/runs/{r.json()['id']}", headers=headers).json()
    return run


def test_model_lifecycle_through_the_api(client: TestClient, auth: Auth, fresh_registry: None) -> None:
    eng, appr, viewer = auth("erin", "engineer"), auth("amir", "approver"), auth("vera", "viewer")

    # -- ingest: engineers only, scenarios validated
    assert client.post(f"{API}/datasets/ingest", json={"month": "2026-01"}, headers=viewer).status_code == 403
    bad = client.post(f"{API}/datasets/ingest", json={"month": "2026-01", "scenario": "alien_invasion"}, headers=eng)
    assert bad.status_code == 422
    for month, scenario in [("2026-01", "none"), ("2026-02", "none"), ("2026-03", "none"), ("2026-04", "none"),
                            ("2026-05", "sensor_drift+fleet_mix")]:  # fmt: skip
        run = _run(client, eng, "/datasets/ingest", {"month": month, "scenario": scenario})
        assert run["status"] == "succeeded", run["result"]
        assert [s["name"] for s in run["steps"]] == ["receive", "validate", "publish"]
    datasets = client.get(f"{API}/datasets", headers=viewer).json()
    assert [d["status"] for d in datasets] == ["validated"] * 5 and all(len(d["sha256"]) == 64 for d in datasets)

    # -- first model: gates pass, promoted by the first-model policy
    first = _run(client, eng, "/runs/train", {"holdout_month": "2026-03"})
    assert first["status"] == "succeeded", (first["result"], first["steps"][-1])
    assert {"train", "evaluate_champion", "gates", "register", "approve", "deploy"} <= {
        s["name"] for s in first["steps"]
    }
    overview = client.get(f"{API}/overview", headers=viewer).json()
    assert overview["champion"]["version"] == first["model_version"]

    # -- second model: waits for a human (manual approval policy)
    second = _run(client, eng, "/runs/train", {"holdout_month": "2026-04"})
    assert second["status"] == "succeeded", second
    pending = client.get(f"{API}/promotions?status=pending", headers=viewer).json()
    assert len(pending) == 1 and pending[0]["version"] == second["model_version"]
    assert registry.get_alias(registry.CHALLENGER).version == second["model_version"]
    pid = pending[0]["id"]
    assert client.post(f"{API}/promotions/{pid}/approve", json={}, headers=eng).status_code == 403
    ok = client.post(f"{API}/promotions/{pid}/approve", json={"note": "reviewed"}, headers=appr)
    assert ok.status_code == 200, ok.text
    assert ok.json()["decided_by"] == "amir@example.com" and "warning" in ok.json()["deployment"]  # no server in tests
    assert client.post(f"{API}/promotions/{pid}/approve", json={}, headers=appr).status_code == 409
    assert registry.get_alias(registry.CHAMPION).version == second["model_version"]

    # -- rollback restores the previous champion, and is audited
    rb = client.post(f"{API}/models/rollback", json={"note": "drill"}, headers=appr)
    assert rb.status_code == 200, rb.text
    assert registry.get_alias(registry.CHAMPION).version == first["model_version"]

    # -- monitoring the drifted month raises an alert and names the drifted features
    mon = _run(client, eng, "/runs/monitor", {"month": "2026-05", "no_retrain": True})
    assert mon["status"] == "succeeded", mon
    report = client.get(f"{API}/monitoring", headers=viewer).json()[-1]
    assert report["status"] == "alert"
    assert {"vibration_mean", "equipment_type"} & set(report["drift"]["drifted_features"])

    admin = auth("ada", "admin")
    audit = client.get(f"{API}/admin/audit", headers=admin).json()
    assert {"model.promoted", "model.rolled_back"} <= {e["action"] for e in audit["events"]}
    assert audit["chain"]["valid"]


def test_training_without_data_is_blocked(client: TestClient, auth: Auth) -> None:
    run = _run(client, auth("erin", "engineer"), "/runs/train", {"holdout_month": "2030-01"})
    assert run["status"] == "blocked" and "holdout" in run["result"]["reason"]


# ---------------------------------------------------------------- model server (SageMaker container contract)
@pytest.fixture
def serving_client(fresh_registry: None) -> Iterator[TestClient]:
    from app.ml.train import train

    for month in ("2026-01", "2026-02", "2026-03"):
        storage.put_batch(month, generate_batch(BatchSpec(month)), stage="validated")
    result = train(["2026-01", "2026-02"], "2026-03")
    registry.promote(result.version, by="test")
    from app.serving import create_app

    with TestClient(create_app()) as c:
        deadline = time.monotonic() + 60
        while c.get("/ping").status_code != 200:  # the model loads in the background
            assert time.monotonic() < deadline, "model server never became ready"
            time.sleep(0.2)
        yield c


def test_model_server_contract(serving_client: TestClient) -> None:
    c = serving_client
    assert c.get("/ping").status_code == 200
    rows = generate_batch(BatchSpec("2026-04")).head(5)
    payload = rows[["unit_id", *FEATURES_NUMERIC, *FEATURES_CATEGORICAL]]

    as_json = c.post(
        "/invocations", json={"instances": payload.astype(object).where(payload.notna(), None).to_dict("records")}
    )
    assert as_json.status_code == 200, as_json.text
    body = as_json.json()
    assert len(body["predictions"]) == 5 and all(0 <= p["failure_probability"] <= 1 for p in body["predictions"])

    as_csv = c.post("/invocations", content=payload.to_csv(index=False), headers={"content-type": "text/csv"})
    assert as_csv.status_code == 200
    assert [p["failure_probability"] for p in as_csv.json()["predictions"]] == [
        p["failure_probability"] for p in body["predictions"]
    ]

    invalid = c.post("/v1/predict", json={"records": [{"equipment_type": "turbine", "age_days": 1, "days_since_service": 1,
                                                       "load_pct": 50, "ambient_temp": 20}]})  # fmt: skip
    assert invalid.status_code == 422
    assert c.post("/invocations", content="a,b\n1,2\n", headers={"content-type": "text/csv"}).status_code == 422
    assert "mlp_requests_total 2" in c.get("/metrics").text
    assert c.get("/v1/model").json()["version"] == registry.get_alias(registry.CHAMPION).version


def test_scheduled_monitoring_picks_the_newest_unmonitored_month(
    client: TestClient, auth: Auth, fresh_registry: None
) -> None:
    from app import cli

    eng = auth("erin", "engineer")
    cli.monitor_latest()  # no champion yet: nothing to do, no error
    for month in ("2026-01", "2026-02", "2026-03", "2026-04"):
        assert _run(client, eng, "/datasets/ingest", {"month": month})["status"] == "succeeded"
    assert _run(client, eng, "/runs/train", {"holdout_month": "2026-03"})["status"] == "succeeded"
    cli.monitor_latest()
    runs = client.get(f"{API}/runs?kind=monitor", headers=eng).json()
    assert [(r["params"]["month"], r["trigger"], r["status"]) for r in runs] == [("2026-04", "schedule", "succeeded")]
