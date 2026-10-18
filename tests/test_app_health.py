# tests/test_app_health.py
"""Health routes, lifespan and the application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.core.config import Settings
from app.main import create_app


async def test_health_reports_version_and_environment(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0", "environment": "test"}


async def test_ready_when_database_answers(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


class _BrokenSession:
    """Stands in for an AsyncSession whose database is unreachable."""

    async def execute(self, *args: object, **kwargs: object) -> None:
        raise ConnectionRefusedError("database is down")


async def test_ready_returns_503_when_database_unavailable(app: FastAPI) -> None:
    async def broken_session() -> AsyncIterator[AsyncSession]:
        yield _BrokenSession()  # type: ignore[misc]

    app.dependency_overrides[get_session] = broken_session
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ready = await client.get("/health/ready")
            alive = await client.get("/health")

    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready", "database": "unavailable"}
    assert alive.status_code == 200  # liveness never depends on the database


async def test_app_starts_when_database_is_down(test_settings: Settings) -> None:
    unreachable = test_settings.model_copy(
        update={"database_url": "postgresql+asyncpg://ric_app:x@127.0.0.1:1/ric_poc_test"}
    )
    app = create_app(unreachable)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/health/ready")).status_code == 503


async def test_lifespan_creates_and_disposes_engine(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.main")
    async with app.router.lifespan_context(app):
        assert app.state.engine is not None
        assert app.state.session_factory is not None
        assert app.state.engine.pool.checkedout() == 0
    messages = [record.getMessage() for record in caplog.records]
    assert "application started" in messages
    assert "application stopped" in messages


def test_one_app_instance_per_factory_call(test_settings: Settings) -> None:
    first = create_app(test_settings)
    second = create_app(test_settings)
    assert first is not second
    assert first.state.settings is test_settings


async def test_business_prefix_and_docs(client: AsyncClient, app: FastAPI) -> None:
    assert (await client.get("/docs")).status_code == 200
    schema = (await client.get("/openapi.json")).json()
    assert schema["info"]["version"] == "0.1.0"
    assert "/health" in schema["paths"]
    # Nothing is registered under /api/v1 yet; the prefix is reserved for Phase 5+.
    assert (await client.get("/api/v1/incidents")).status_code == 404


async def test_docs_can_be_disabled(test_settings: Settings) -> None:
    app = create_app(test_settings.model_copy(update={"docs_enabled": False}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404
