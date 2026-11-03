"""FastAPI application factory."""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import get_settings
from app.core.errors import ServiceUnavailable, register_error_handlers
from app.core.logging import configure_logging, log_extra, request_id_var
from app.db.session import new_session
from app.modules.admin.router import router as admin_router
from app.modules.auth.router import router as auth_router
from app.modules.platform.router import router as platform_router
from app.modules.users.router import router as users_router

logger = logging.getLogger("app.http")

API_DESCRIPTION = """
MLOps control plane for a production machine-failure model: versioned datasets with a data contract,
tracked training in MLflow, promotion gates, human approval, deployment (local or SageMaker canary),
and drift monitoring that triggers retraining.

**Auth:** `POST /api/v1/auth/login` returns a bearer access token (15 min) and sets an httpOnly
refresh cookie. Roles: `viewer` < `engineer` (ingest, train, monitor) < `approver` (promote,
reject, roll back) < `admin` (users).

Pipeline runs are asynchronous: poll `GET /api/v1/runs/{id}` for status and step details.
**Errors** use RFC 9457 problem details (`application/problem+json`).
"""


def create_app() -> FastAPI:
    s = get_settings()
    s.signing_key()  # fail fast: the API cannot issue or verify tokens without MLP_JWT_SECRET
    configure_logging(s.log_level, s.log_json)
    app = FastAPI(
        title="MLOps Platform API",
        version="1.0.0",
        description=API_DESCRIPTION,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("X-Request-ID", "")[:64] or uuid.uuid4().hex
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if s.env == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if not request.url.path.startswith("/api/health"):
            logger.info(
                "request",
                extra=log_extra(
                    method=request.method,
                    path=request.url.path,
                    status=response.status_code,
                    ms=round((time.perf_counter() - started) * 1000, 1),
                    request_id=rid,
                ),
            )
        return response

    register_error_handlers(app)

    api = APIRouter(prefix="/api/v1")
    for r in (
        auth_router,
        users_router,
        platform_router,
        admin_router,
    ):
        api.include_router(r)
    app.include_router(api)

    @app.get("/api/health/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/health/ready", tags=["health"])
    def ready() -> dict[str, object]:
        db = new_session()
        try:
            db.execute(text("SELECT 1"))
            model_ready = True
        except Exception as exc:
            raise ServiceUnavailable("Database unavailable") from exc
        finally:
            db.close()
        return {"status": "ok", "database": "ok", "ready": model_ready}

    return app


app = create_app()
