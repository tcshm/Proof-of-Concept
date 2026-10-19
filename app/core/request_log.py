# app/core/request_log.py
"""Write one api_request_log row per API request.

Called by RequestContextMiddleware after the response has been sent, so the
client never waits for it. Uses its own short session (never the request's),
and a failure only logs a warning: logging must never break a request.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ApiRequestLog
from app.models.values import ClientApp

CLIENT_APP_HEADER = "X-Client-App"
WRITE_TIMEOUT_SECONDS = 2.0

# Paths that are browser or platform plumbing, not API usage.
SKIPPED_PATHS = frozenset(
    {"/health", "/health/ready", "/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}
)

_MAX_TEXT = {"path": 500, "route": 200, "site_code": 16, "actor": 200, "error_code": 64}

logger = logging.getLogger("app.request_log")


@dataclass(frozen=True)
class RequestLogEntry:
    request_id: str
    client_app: str
    method: str
    path: str
    route: str | None
    site_code: str | None
    actor: str | None
    status_code: int
    duration_ms: int
    error_code: str | None


def should_log(method: str, path: str) -> bool:
    return method != "OPTIONS" and path not in SKIPPED_PATHS


def client_app_from_header(value: str | None) -> str:
    """mobile_app or admin_portal when the client says so, otherwise other."""
    if value:
        candidate = value.strip().lower()
        if candidate in (ClientApp.MOBILE_APP, ClientApp.ADMIN_PORTAL):
            return candidate
    return ClientApp.OTHER


def route_template(scope: dict[str, Any]) -> str | None:
    """The path template FastAPI matched, e.g. /api/v1/actions/{action_id}/responses.

    Depending on the FastAPI version, the matched route's .path is either the
    full template or only the part below its router's prefix (/store instead of
    /api/v1/store). The prefix never contains parameters, so the template is the
    leading segments of the real path followed by the route's own segments.
    """
    route = scope.get("route")
    template = getattr(route, "path", None)
    if not isinstance(template, str):
        return None
    path_segments = scope["path"].strip("/").split("/")
    route_segments = [s for s in template.strip("/").split("/") if s]
    prefix = path_segments[: max(len(path_segments) - len(route_segments), 0)]
    return "/" + "/".join(prefix + route_segments)


def _clip(field: str, value: str | None) -> str | None:
    return None if value is None else value[: _MAX_TEXT[field]]


async def write_request_log(
    factory: async_sessionmaker[AsyncSession], entry: RequestLogEntry
) -> None:
    try:
        async with asyncio.timeout(WRITE_TIMEOUT_SECONDS):
            async with factory() as session:
                session.add(
                    ApiRequestLog(
                        request_id=uuid.UUID(entry.request_id),
                        client_app=entry.client_app,
                        method=entry.method,
                        path=_clip("path", entry.path),
                        route=_clip("route", entry.route),
                        site_code=_clip("site_code", entry.site_code),
                        actor=_clip("actor", entry.actor),
                        status_code=entry.status_code,
                        duration_ms=entry.duration_ms,
                        error_code=_clip("error_code", entry.error_code),
                    )
                )
                await session.commit()
    except Exception as exc:
        logger.warning(
            "could not write api_request_log row",
            extra={"error": exc.__class__.__name__, "path": entry.path},
        )
