"""Model server: the same container runs locally and as a SageMaker real-time endpoint.

SageMaker's container contract: ``GET /ping`` returns 200 once the model is loaded, and
``POST /invocations`` scores a payload, both on port 8080. Here:

* ``/invocations`` accepts JSON (``{"instances": [...]}`` or a list of records) or CSV with a header.
* ``/v1/predict`` is the typed JSON API used by applications and the dashboard.
* ``/admin/reload`` re-resolves the ``champion`` alias (local deployments; protected by a token).
* ``/metrics`` exposes request, latency and alert counters in Prometheus format.

Which model: ``MLP_MODEL_VERSION`` if set (SageMaker bakes the version into each deployment, so a
canary really serves the new version), otherwise the registry's ``champion`` alias.

A sample of predictions is captured to PostgreSQL for live drift monitoring (locally); on AWS,
SageMaker's own data capture writes to S3 instead.
"""

from __future__ import annotations

import io
import logging
import os
import random
import threading
import time
import uuid
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.ml import registry
from app.ml.generator import EQUIPMENT_TYPES
from app.ml.train import FEATURES

logger = logging.getLogger("app.serving")
CAPTURE_RATE = float(os.environ.get("MLP_CAPTURE_RATE", "0.2"))
REFRESH_SECONDS = float(os.environ.get("MLP_SERVING_REFRESH_SECONDS", "30"))
LATENCY_BUCKETS = (5, 10, 25, 50, 100, 250, 500, 1000)


class Record(BaseModel):
    unit_id: str | None = Field(default=None, max_length=40)
    equipment_type: str = Field(pattern="|".join(EQUIPMENT_TYPES))
    age_days: float = Field(ge=0)
    days_since_service: float = Field(ge=0)
    load_pct: float = Field(ge=0, le=120)
    ambient_temp: float = Field(ge=-40, le=60)
    vibration_mean: float | None = Field(default=None, ge=0)
    vibration_std: float | None = Field(default=None, ge=0)
    vibration_trend: float | None = None
    bearing_temp_rise: float | None = None
    current_mean: float | None = Field(default=None, ge=0)
    pressure_mean: float | None = Field(default=None, ge=0)


class PredictRequest(BaseModel):
    records: list[Record] = Field(min_length=1, max_length=1000)


class Loaded:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.version: str | None = None
        self.run_id: str | None = None
        self.threshold = 0.5
        self.model: Any = None
        self.loaded_at: float | None = None

    def load(self) -> str:
        pinned = os.environ.get("MLP_MODEL_VERSION")
        mv = registry.get_version(pinned) if pinned else registry.get_alias(registry.CHAMPION)
        if mv is None:
            raise RuntimeError("no champion model in the registry yet")
        if mv.version == self.version and mv.run_id != self.run_id:
            registry.load_model.cache_clear()  # same number, different model (the registry was reset)
        model = registry.load_model(mv.version)
        with self.lock:
            self.version, self.run_id, self.threshold, self.model = mv.version, mv.run_id, mv.threshold, model
            self.loaded_at = time.time()
        logger.info("model loaded", extra={"version": mv.version})
        return mv.version


state = Loaded()
counters: dict[str, float] = {"requests": 0, "rows": 0, "alerts": 0, "errors": 0}
latency_hist = dict.fromkeys(LATENCY_BUCKETS, 0) | {"inf": 0}


def _load_and_follow() -> None:
    """Load the model, retrying until the registry has one, then keep following the champion.

    ``/admin/reload`` only reaches the worker that receives the request, so each worker also checks
    the registry alias periodically and loads a new version when it changes. Pinned deployments
    (SageMaker sets MLP_MODEL_VERSION) load once and stop.
    """
    pinned = bool(os.environ.get("MLP_MODEL_VERSION"))
    while state.model is None:
        try:
            state.load()
        except Exception as exc:
            logger.warning("model not loaded yet: %s", exc)
            time.sleep(10)
    while not pinned and REFRESH_SECONDS > 0:
        time.sleep(REFRESH_SECONDS)
        try:
            mv = registry.get_alias(registry.CHAMPION)
            if mv is not None and (mv.version, mv.run_id) != (state.version, state.run_id):
                state.load()
        except Exception:
            logger.warning("champion check failed", exc_info=True)


def _observe(ms: float) -> None:
    for b in LATENCY_BUCKETS:
        if ms <= b:
            latency_hist[b] += 1
    latency_hist["inf"] += 1


def score(frame: pd.DataFrame) -> tuple[np.ndarray, str, float]:
    if state.model is None:
        raise HTTPException(503, "model not loaded")
    with state.lock:
        model, version, threshold = state.model, state.version or "", state.threshold
    p = model.predict_proba(frame[FEATURES])[:, 1]
    return p, version, threshold


def _capture(frame: pd.DataFrame, p: np.ndarray, version: str, threshold: float, ms: float) -> None:
    if os.environ.get("MLP_CAPTURE_DB", "true").lower() != "true" or random.random() > CAPTURE_RATE:
        return
    try:
        from app.db.models import Prediction
        from app.db.session import new_session

        rid = uuid.uuid4().hex
        with new_session() as db:
            for (_, row), prob in zip(frame.iterrows(), p, strict=True):
                feats = {k: (None if pd.isna(v) else v) for k, v in row[FEATURES].items()}
                db.add(Prediction(request_id=rid, model_version=version, unit_id=row.get("unit_id"), features=feats,
                                  score=float(prob), alert=bool(prob >= threshold), latency_ms=ms))  # fmt: skip
            db.commit()
    except Exception:  # capture must never break serving
        logger.warning("prediction capture failed", exc_info=True)


def _respond(frame: pd.DataFrame) -> dict[str, Any]:
    t0 = time.perf_counter()
    p, version, threshold = score(frame)
    ms = (time.perf_counter() - t0) * 1000
    counters["requests"] += 1
    counters["rows"] += len(frame)
    counters["alerts"] += int((p >= threshold).sum())
    _observe(ms)
    _capture(frame, p, version, threshold, ms)
    preds = [{"unit_id": uid, "failure_probability": round(float(prob), 5), "alert": bool(prob >= threshold)}
             for uid, prob in zip(frame.get("unit_id", [None] * len(frame)), p, strict=True)]  # fmt: skip
    return {
        "model_version": version,
        "threshold": round(threshold, 4),
        "latency_ms": round(ms, 2),
        "predictions": preds,
    }


def create_app() -> FastAPI:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    app = FastAPI(title="Machine Failure Model Server", version="1.0.0", docs_url="/docs")

    @app.on_event("startup")
    def _startup() -> None:
        # Load in the background: startup must not block (process supervisors health-check workers),
        # and /ping answers 503 until the model is ready, which is what SageMaker waits for.
        threading.Thread(target=_load_and_follow, name="model-loader", daemon=True).start()

    @app.get("/ping")
    def ping() -> Response:
        return Response(status_code=200 if state.model is not None else 503)

    @app.post("/invocations")
    async def invocations(request: Request) -> dict[str, Any]:
        body = await request.body()
        ctype = request.headers.get("content-type", "application/json")
        try:
            if "csv" in ctype:
                frame = pd.read_csv(io.BytesIO(body))
            else:
                import json

                data = json.loads(body)
                rows = data.get("instances", data.get("records")) if isinstance(data, dict) else data
                if not isinstance(rows, list) or not rows:
                    raise ValueError('expected a non-empty list of records (or {"instances": [...]})')
                frame = pd.DataFrame([Record.model_validate(r).model_dump() for r in rows])
            missing = [c for c in FEATURES if c not in frame.columns]
            if missing:
                raise ValueError(f"missing columns: {', '.join(missing)}")
        except Exception as exc:
            counters["errors"] += 1
            raise HTTPException(422, f"invalid payload: {exc}") from exc
        return _respond(frame)

    @app.post("/v1/predict")
    def predict(req: PredictRequest) -> dict[str, Any]:
        return _respond(pd.DataFrame([r.model_dump() for r in req.records]))

    @app.get("/v1/model")
    def model_info() -> dict[str, Any]:
        return {"version": state.version, "threshold": state.threshold, "loaded_at": state.loaded_at,
                "features": FEATURES}  # fmt: skip

    @app.post("/admin/reload")
    def reload(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
        expected = os.environ.get("MLP_SERVING_ADMIN_TOKEN")
        if expected and x_admin_token != expected:
            raise HTTPException(403, "invalid admin token")
        registry.load_model.cache_clear()
        return {"version": state.load()}

    @app.get("/metrics")
    def prom() -> Response:
        lines = [
            "# TYPE mlp_requests_total counter", f"mlp_requests_total {counters['requests']:.0f}",
            "# TYPE mlp_predictions_total counter", f"mlp_predictions_total {counters['rows']:.0f}",
            "# TYPE mlp_alerts_total counter", f"mlp_alerts_total {counters['alerts']:.0f}",
            "# TYPE mlp_errors_total counter", f"mlp_errors_total {counters['errors']:.0f}",
            "# TYPE mlp_latency_ms histogram",
            *[f'mlp_latency_ms_bucket{{le="{b}"}} {latency_hist[b]}' for b in LATENCY_BUCKETS],
            f'mlp_latency_ms_bucket{{le="+Inf"}} {latency_hist["inf"]}',
            f'mlp_model_info{{version="{state.version}"}} 1',
        ]  # fmt: skip
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    return app


app = create_app()
