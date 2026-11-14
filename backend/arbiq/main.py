import asyncio
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

import aiohttp
import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.exceptions import HTTPException

from arbiq import __version__
from arbiq.ai.api import router as ai_router
from arbiq.ai.providers import close_ai, initialize_ai
from arbiq.analytics.api import router as analytics_router
from arbiq.analytics.export_jobs import router as export_jobs_router
from arbiq.api.routes import router as api_router
from arbiq.api.terminal import initialize_terminal
from arbiq.api.terminal import router as terminal_router
from arbiq.auth.bootstrap import provision_operator
from arbiq.auth.routes import router as auth_router
from arbiq.core.clock import elapsed_seconds
from arbiq.core.config import Settings, get_settings
from arbiq.core.logging import configure_logging
from arbiq.core.memory import maintain_process_memory
from arbiq.db.models import SystemEvent
from arbiq.db.schema import EXPECTED_SCHEMA_REVISION
from arbiq.market_data.api import router as market_router
from arbiq.notifications.api import router as notification_router
from arbiq.opportunities.api import router as opportunity_router
from arbiq.paper_trading.api import router as paper_router
from arbiq.paper_trading.funding import router as funding_router
from arbiq.paper_trading.replay_api import router as replay_router
from arbiq.settings.api import router as settings_router

log = structlog.get_logger()


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or get_settings()
        configure_logging(config.log_level)
        engine = create_async_engine(
            config.database_url.get_secret_value(),
            pool_pre_ping=True,
            # Retain one idle backend; preserve the same three-query burst limit.
            # Overflow connections are closed after the burst instead of keeping
            # their PostgreSQL per-process allocation resident indefinitely.
            pool_size=1,
            max_overflow=2,
            pool_use_lifo=True,
            hide_parameters=True,
        )
        app.state.settings = config
        initialize_ai(app)
        initialize_terminal(app)
        app.state.engine = engine
        app.state.sessions = async_sessionmaker(engine, expire_on_commit=False)
        app.state.memory_maintenance = None
        memory_task = None
        token = config.collector_internal_token.get_secret_value()
        app.state.collector_http = (
            aiohttp.ClientSession(
                base_url=config.collector_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=3),
            )
            if token
            else None
        )
        try:
            async with app.state.sessions() as db:
                # Migrations are a separate required job, never implicit create_all.
                revision = await db.scalar(text("SELECT version_num FROM alembic_version"))
                if revision != EXPECTED_SCHEMA_REVISION:
                    raise RuntimeError("Run Alembic migrations before starting QUANTIX")
                await provision_operator(db, config)
                db.add(
                    SystemEvent(
                        event_type="system.started",
                        severity="info",
                        message="QUANTIX market-data application started in paper mode",
                        details={"version": __version__, "stage": 7},
                    )
                )
                await db.commit()
            log.info("application_started", mode="paper", version=__version__)
            memory_task = asyncio.create_task(maintain_process_memory(app.state))
            yield
        finally:
            if hasattr(app.state, "export_jobs"):
                await app.state.export_jobs.close()
            if memory_task is not None:
                memory_task.cancel()
                with suppress(asyncio.CancelledError):
                    await memory_task
            await close_ai(app)
            if app.state.collector_http is not None:
                await app.state.collector_http.close()
            await engine.dispose()

    app = FastAPI(
        title="QUANTIX API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = str(uuid4())
        start = elapsed_seconds()
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin not in request.app.state.settings.allowed_origins:
                return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        response = await call_next(request)
        if hasattr(request.app.state, "http_latencies"):
            request.app.state.http_latencies.append((elapsed_seconds() - start) * 1000)
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # No body, headers, query strings, credentials or exception details in logs.
        route = request.scope.get("route")
        log.info(
            "http_request",
            request_id=request_id,
            method=request.method,
            route=getattr(route, "path", "unmatched"),
            status=response.status_code,
            duration_ms=round((elapsed_seconds() - start) * 1000),
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            {
                "detail": "Request validation failed",
                "fields": [
                    {"location": list(error["loc"]), "type": error["type"]}
                    for error in exc.errors()
                ],
            },
            status_code=422,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        log.error("request_failed", error_type=type(exc).__name__)
        return JSONResponse({"detail": "Internal service error"}, status_code=500)

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "alive", "mode": "paper", "version": __version__}

    @app.get("/health/ready")
    async def ready(request: Request):
        try:
            async with request.app.state.sessions() as db:
                revision = await db.scalar(text("SELECT version_num FROM alembic_version"))
                if revision != EXPECTED_SCHEMA_REVISION:
                    raise RuntimeError("schema mismatch")
            return {"status": "ready", "database": "connected", "mode": "paper"}
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(api_router, prefix="/api/v1")
    app.include_router(market_router, prefix="/api/v1")
    app.include_router(opportunity_router, prefix="/api/v1")
    app.include_router(paper_router, prefix="/api/v1")
    app.include_router(terminal_router, prefix="/api/v1")
    app.include_router(funding_router, prefix="/api/v1")
    app.include_router(replay_router, prefix="/api/v1")
    app.include_router(settings_router, prefix="/api/v1")
    app.include_router(analytics_router, prefix="/api/v1")
    app.include_router(export_jobs_router, prefix="/api/v1")
    app.include_router(notification_router, prefix="/api/v1")
    app.include_router(ai_router, prefix="/api/v1")
    return app


app = create_app()
