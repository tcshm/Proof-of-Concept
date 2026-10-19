# app/core/request_context.py
"""Per-request context: the request id, and the middleware that sets it.

One process serves many requests concurrently, so per-request values must never
live in globals or on app.state. A ContextVar holds a separate value for each
request, even while requests overlap on the event loop.

Values that route dependencies discover during the request (site code, actor,
error code) are put on request.state; the middleware reads them at the end to
write the log line and the api_request_log row.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"

# Health probes run every few seconds in hosting platforms: log them at DEBUG,
# or at WARNING when they report a failure (503).
QUIET_PATHS = frozenset({"/health", "/health/ready"})

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

logger = logging.getLogger("app.request")


def get_request_id() -> str | None:
    """The id of the request being handled, or None outside a request."""
    return _request_id.get()


def _accept_request_id(value: str | None) -> str | None:
    """Accept a client-supplied id only if it is a UUID; normalise it to lowercase."""
    if not value:
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        return None


class RequestContextMiddleware:
    """Pure ASGI middleware, outermost apart from CORS.

    For every HTTP request it:
    1. takes a valid incoming X-Request-ID or generates a new one,
    2. stores it in the context variable and on request.state,
    3. adds it to the response headers,
    4. turns any unhandled exception into the standard 500 error response,
    5. logs one line when the request finishes (method, path, status, duration).
    6. writes one api_request_log row after the response has been sent.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = _accept_request_id(headers.get(REQUEST_ID_HEADER)) or str(uuid.uuid4())
        token = _request_id.set(request_id)
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception(
                "unhandled error",
                extra={"method": scope["method"], "path": scope["path"]},
            )
            if response_started:
                raise
            # Imported here to avoid a circular import (errors.py reads the request id).
            from app.core.errors import unhandled_error_response

            state["error_code"] = "internal_error"
            await unhandled_error_response()(scope, receive, send_with_request_id)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            path = scope["path"]
            if path in QUIET_PATHS:
                level = logging.DEBUG if status_code < 500 else logging.WARNING
            elif status_code >= 500:
                level = logging.ERROR
            else:
                level = logging.INFO
            logger.log(
                level,
                "request completed",
                extra={
                    "method": scope["method"],
                    "path": path,
                    "status": status_code,
                    "durationMs": duration_ms,
                },
            )
            await _write_request_log(scope, headers, request_id, status_code, duration_ms)
            _request_id.reset(token)


async def _write_request_log(
    scope: Scope, headers: Headers, request_id: str, status_code: int, duration_ms: float
) -> None:
    # Imported here: request_log imports the models, which this module must not
    # load at import time (logging.py imports this module very early).
    from app.core.request_log import (
        CLIENT_APP_HEADER,
        RequestLogEntry,
        client_app_from_header,
        route_template,
        should_log,
        write_request_log,
    )

    app = scope.get("app")
    app_state = getattr(app, "state", None)
    settings = getattr(app_state, "settings", None)
    factory = getattr(app_state, "session_factory", None)
    if settings is None or factory is None or not settings.request_log_enabled:
        return
    if not should_log(scope["method"], scope["path"]):
        return

    state = scope.get("state", {})
    entry = RequestLogEntry(
        request_id=request_id,
        client_app=client_app_from_header(headers.get(CLIENT_APP_HEADER)),
        method=scope["method"],
        path=scope["path"],
        route=route_template(scope),
        site_code=state.get("site_code"),
        actor=state.get("actor"),
        status_code=status_code,
        duration_ms=round(duration_ms),
        error_code=state.get("error_code"),
    )
    await write_request_log(factory, entry)
