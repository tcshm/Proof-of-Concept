# app/main.py
"""Application factory and lifespan.

    uv run uvicorn app.main:app --reload

create_app() builds ONE FastAPI instance per process; every request to that
process is handled by it. Tests call create_app(test_settings) to build their
own instance pointing at the test database.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.context import SITE_ID_HEADER, USER_HEADER
from app.api.router import API_PREFIX, api_router
from app.api.routes import health
from app.core.clock import SystemClock
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.request_context import REQUEST_ID_HEADER, RequestContextMiddleware
from app.core.request_log import CLIENT_APP_HEADER
from app.db.session import create_engine_from_settings, create_session_factory

STARTUP_DB_CHECK_SECONDS = 3.0

logger = logging.getLogger("app.main")


async def _log_database_status(engine: AsyncEngine) -> None:
    """Report whether the database answers. Never stops startup: /health/ready
    returns 503 until the database is reachable."""
    database = make_url(str(engine.url)).database
    try:
        async with asyncio.timeout(STARTUP_DB_CHECK_SECONDS):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning(
            "database not reachable at startup; /health/ready will report 503",
            extra={"database": database, "error": exc.__class__.__name__},
        )
    else:
        logger.info("database reachable", extra={"database": database})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level, settings.log_format)

    engine = create_engine_from_settings(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    await _log_database_status(engine)
    logger.info(
        "application started",
        extra={"version": settings.app_version, "environment": settings.environment},
    )
    try:
        yield
    finally:
        await engine.dispose()
        logger.info("application stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.state.clock = SystemClock()

    register_exception_handlers(app)

    # The last middleware added is the outermost. CORS is outermost so that
    # every response, including the 500 built by RequestContextMiddleware,
    # carries the CORS headers a browser needs to read it.
    app.add_middleware(RequestContextMiddleware)
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Content-Type",
                "Authorization",
                REQUEST_ID_HEADER,
                SITE_ID_HEADER,
                USER_HEADER,
                CLIENT_APP_HEADER,
            ],
            expose_headers=[REQUEST_ID_HEADER],
            max_age=600,
        )

    app.include_router(health.router)
    app.include_router(api_router, prefix=API_PREFIX)
    return app


app = create_app()
