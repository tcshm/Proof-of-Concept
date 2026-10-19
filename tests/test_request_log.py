# tests/test_request_log.py
"""One api_request_log row per API request, written without affecting the response."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.context import MobileActorDep
from app.core.config import Settings
from app.core.request_log import client_app_from_header, should_log
from app.main import create_app
from app.models import ApiRequestLog
from tests.conftest import CommittedData

probe = APIRouter(prefix="/_probe")


@probe.get("/items/{item_id}")
async def probe_item(item_id: int, actor: MobileActorDep) -> dict[str, int]:
    return {"itemId": item_id}


@probe.get("/crash")
async def probe_crash() -> None:
    raise RuntimeError("boom")


@pytest.fixture
def logged_app(test_settings: Settings) -> FastAPI:
    app = create_app(test_settings.model_copy(update={"request_log_enabled": True}))
    app.include_router(probe)
    return app


@pytest.fixture
async def logged_client(
    logged_app: FastAPI, committed_data: CommittedData
) -> AsyncIterator[tuple[AsyncClient, CommittedData]]:
    async with logged_app.router.lifespan_context(logged_app):
        transport = ASGITransport(app=logged_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, committed_data


async def _rows(data: CommittedData) -> list[ApiRequestLog]:
    async with data.engine.connect() as connection:
        result = await connection.execute(select(ApiRequestLog).order_by(ApiRequestLog.id))
        return list(result.all())


async def test_successful_request_is_logged(logged_client, chennai: uuid.UUID) -> None:
    client, data = logged_client
    response = await client.get(
        "/api/v1/store",
        headers={"X-Site-Id": "01234", "X-Client-App": "mobile_app"},
    )
    assert response.status_code == 200
    [row] = await _rows(data)
    assert str(row.request_id) == response.headers["X-Request-ID"]
    assert row.client_app == "mobile_app"
    assert row.method == "GET"
    assert row.path == "/api/v1/store"
    assert row.route == "/api/v1/store"
    assert row.site_code == "01234"
    assert row.status_code == 200
    assert row.duration_ms >= 0
    assert row.error_code is None


async def test_route_template_actor_and_error_code(logged_client, chennai: uuid.UUID) -> None:
    client, data = logged_client
    await client.get(
        "/_probe/items/42",
        headers={"X-Site-Id": "01234", "X-User": "priya", "X-Client-App": "admin_portal"},
    )
    await client.get("/_probe/items/42", headers={"X-Site-Id": "99999"})
    await client.get("/_probe/items/42")
    await client.get("/no/such/path", headers={"X-Client-App": "something-else"})
    ok, unknown_store, missing_header, no_route = await _rows(data)

    assert ok.route == "/_probe/items/{item_id}"
    assert ok.path == "/_probe/items/42"
    assert ok.actor == "priya"
    assert ok.client_app == "admin_portal"

    assert unknown_store.status_code == 404
    assert unknown_store.error_code == "not_found"
    assert unknown_store.site_code == "99999"

    assert missing_header.status_code == 422
    assert missing_header.error_code == "validation_error"
    assert missing_header.site_code is None

    assert no_route.route is None
    assert no_route.error_code == "not_found"
    assert no_route.client_app == "other"


async def test_unhandled_error_is_logged_as_500(logged_client) -> None:
    client, data = logged_client
    response = await client.get("/_probe/crash")
    assert response.status_code == 500
    [row] = await _rows(data)
    assert row.status_code == 500
    assert row.error_code == "internal_error"


async def test_health_docs_and_preflight_are_not_logged(logged_client) -> None:
    client, data = logged_client
    await client.get("/health")
    await client.get("/health/ready")
    await client.get("/docs")
    await client.get("/openapi.json")
    await client.options(
        "/api/v1/store",
        headers={"Origin": "http://localhost:8081", "Access-Control-Request-Method": "GET"},
    )
    assert await _rows(data) == []


async def test_log_failure_does_not_change_response(
    logged_app: FastAPI, logged_client, caplog: pytest.LogCaptureFixture
) -> None:
    client, data = logged_client
    caplog.set_level(logging.WARNING, logger="app.request_log")

    def broken_factory() -> None:
        raise ConnectionRefusedError("database is down")

    # /no/such/path needs no session, so only the log write uses the broken factory.
    logged_app.state.session_factory = broken_factory
    response = await client.get("/no/such/path")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert any(r.getMessage() == "could not write api_request_log row" for r in caplog.records)
    assert await _rows(data) == []


async def test_disabled_by_setting(client: AsyncClient, committed_data, chennai) -> None:
    # The shared client fixture uses request_log_enabled=False.
    await client.get("/api/v1/store", headers={"X-Site-Id": "01234"})
    assert await _rows(committed_data) == []


def test_helpers() -> None:
    assert client_app_from_header("Mobile_App ") == "mobile_app"
    assert client_app_from_header("admin_portal") == "admin_portal"
    assert client_app_from_header(None) == "other"
    assert client_app_from_header("curl") == "other"
    assert should_log("GET", "/api/v1/store")
    assert not should_log("OPTIONS", "/api/v1/store")
    assert not should_log("GET", "/health")
