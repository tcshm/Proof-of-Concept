# app/core/logging.py
"""Logging setup: one line per log record, as JSON or as readable console text.

Every record carries the current request id (from the context variable), so all
lines written while handling one request can be found together.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.request_context import get_request_id

# Attributes every LogRecord has; anything else was passed with extra={...}.
_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "request_id",
    "taskName",
    "color_message",  # uvicorn's coloured duplicate of the message
}

# Marks the handler installed here, so a second configure call replaces only ours
# and leaves handlers added by others (for example pytest's caplog) in place.
_RIC_HANDLER_ATTR = "_ric_handler"


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_ATTRS and not key.startswith("_")
    }


class RequestIdFilter(logging.Filter):
    """Copy the current request id onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for Azure Monitor / Log Analytics."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "requestId": getattr(record, "request_id", None),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Readable single line for local development."""

    def format(self, record: logging.LogRecord) -> str:
        time_text = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        request_id = getattr(record, "request_id", None)
        prefix = f"[{request_id[:8]}] " if request_id else ""
        extras = " ".join(f"{key}={value}" for key, value in _extras(record).items())
        line = f"{time_text} {record.levelname:<7} {record.name}: {prefix}{record.getMessage()}"
        if extras:
            line = f"{line}  {extras}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(level: str = "INFO", log_format: str = "console") -> None:
    """Configure the root logger once at startup. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JsonFormatter() if log_format == "json" else ConsoleFormatter())
    setattr(handler, _RIC_HANDLER_ATTR, True)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _RIC_HANDLER_ATTR, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn's own messages go through our handler; its access log is replaced
    # by the "request completed" line written by RequestContextMiddleware.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True

    # SQL statements are logged only when DB_ECHO=true (SQLAlchemy handles that itself).
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
