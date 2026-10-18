# app/api/routes/health.py
"""Liveness and readiness probes.

/health        is the process alive? Never touches the database, so a database
               outage does not make the platform restart healthy API processes.
/health/ready  can the process serve traffic? Runs SELECT 1; returns 503 while
               the database is unreachable and 200 again as soon as it is back.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import SessionDep, SettingsDep

READY_TIMEOUT_SECONDS = 3.0

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    environment: str


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    database: Literal["ok", "unavailable"]


@router.get("/health", response_model=HealthResponse)
async def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status="ok", version=settings.app_version, environment=settings.environment
    )


@router.get(
    "/health/ready",
    response_model=ReadyResponse,
    responses={503: {"model": ReadyResponse, "description": "Database unavailable"}},
)
async def ready(session: SessionDep) -> ReadyResponse | JSONResponse:
    try:
        await asyncio.wait_for(session.execute(text("SELECT 1")), READY_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("readiness check failed: %s", exc.__class__.__name__)
        body = ReadyResponse(status="not_ready", database="unavailable")
        return JSONResponse(status_code=503, content=body.model_dump())
    return ReadyResponse(status="ready", database="ok")
