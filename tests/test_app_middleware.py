# tests/test_app_middleware.py
"""Request id, request logging, CORS, logging formatters and settings."""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.core import request_context
from app.core.config import Settings
from app.core.logging import ConsoleFormatter, JsonFormatter, RequestIdFilter
from tests.conftest import TEST_CORS_ORIGIN


# --- request id --------------------------------------------------------------
async def test_request_id_is_generated(client: AsyncClient) -> None:
    response = await client.get("/health")
    request_id = response.headers["X-Request-ID"]
    assert str(uuid.UUID(request_id)) == request_id


async def test_valid_request_id_is_echoed(client: AsyncClient) -> None:
    sent = str(uuid.uuid4())
    response = await client.get("/health", headers={"X-Request-ID": sent.upper()})
    assert response.headers["X-Request-ID"] == sent


async def test_invalid_request_id_is_replaced(client: AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "not-a-uuid"})
    request_id = response.headers["X-Request-ID"]
    assert request_id != "not-a-uuid"
    uuid.UUID(request_id)


async def test_each_request_gets_its_own_id(client: AsyncClient) -> None:
    ids = {(await client.get("/health")).headers["X-Request-ID"] for _ in range(5)}
    assert len(ids) == 5
    assert request_context.get_request_id() is None  # nothing leaks outside a request


async def test_request_completed_is_logged(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="app.request")
    response = await client.get("/does-not-exist")
    record = next(r for r in caplog.records if r.getMessage() == "request completed")
    assert record.method == "GET"
    assert record.path == "/does-not-exist"
    assert record.status == 404
    assert record.durationMs >= 0
    assert record.levelno == logging.INFO
    assert record.request_id == response.headers["X-Request-ID"]


async def test_health_probes_log_at_debug(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="app.request")
    await client.get("/health")
    record = next(r for r in caplog.records if r.getMessage() == "request completed")
    assert record.levelno == logging.DEBUG


# --- CORS --------------------------------------------------------------------
async def test_cors_preflight_allows_configured_origin(client: AsyncClient) -> None:
    response = await client.options(
        "/health",
        headers={
            "Origin": TEST_CORS_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-site-id, x-request-id",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == TEST_CORS_ORIGIN
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "x-site-id" in allowed and "x-request-id" in allowed


async def test_cors_rejects_other_origin(client: AsyncClient) -> None:
    response = await client.options(
        "/health",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


async def test_cors_exposes_request_id(client: AsyncClient) -> None:
    response = await client.get("/health", headers={"Origin": TEST_CORS_ORIGIN})
    assert response.headers["access-control-allow-origin"] == TEST_CORS_ORIGIN
    assert "x-request-id" in response.headers["access-control-expose-headers"].lower()


# --- logging formatters ------------------------------------------------------
def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    for key, value in extra.items():
        setattr(record, key, value)
    RequestIdFilter().filter(record)
    return record


def test_json_formatter_includes_request_id_and_extras() -> None:
    token = request_context._request_id.set("11111111-2222-3333-4444-555555555555")
    try:
        line = JsonFormatter().format(_record(status=200))
    finally:
        request_context._request_id.reset(token)
    payload = json.loads(line)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["requestId"] == "11111111-2222-3333-4444-555555555555"
    assert payload["status"] == 200


def test_console_formatter_is_one_readable_line() -> None:
    line = ConsoleFormatter().format(_record(status=200))
    assert "INFO" in line and "hello world" in line and "status=200" in line
    assert "\n" not in line


# --- settings ----------------------------------------------------------------
def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@localhost/db", **values)


def test_cors_origins_parsed_from_comma_list() -> None:
    settings = _settings(cors_origins="http://localhost:8081, http://localhost:3000 ,")
    assert settings.cors_origin_list == ["http://localhost:8081", "http://localhost:3000"]
    assert _settings().cors_origin_list == []


def test_cors_wildcard_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(cors_origins="*")


def test_log_format_must_be_known() -> None:
    with pytest.raises(ValidationError):
        _settings(log_format="xml")
