# tests/conftest.py
"""Shared fixtures. Tests run against <database>_test, never the main database."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from alembic import command
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.db.migrate import alembic_config
from app.main import create_app
from app.models import ApiRequestLog, AuditLog, Store

TEST_CORS_ORIGIN = "http://localhost:8081"


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """DATABASE_URL with the database name changed to <name>_test."""
    url = make_url(get_settings().database_url)
    test_url = url.set(database=f"{url.database}_test")
    assert test_url.database.endswith("_test"), "tests must never use the main database"
    return test_url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def migrated_db(test_database_url: str) -> str:
    """Bring the test database to the latest revision once per test run.

    Sync on purpose: Alembic's async env.py starts its own event loop.
    """
    command.upgrade(alembic_config(test_database_url), "head")
    return test_database_url


@pytest.fixture
async def db_session(migrated_db: str) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back.

    Code under test may call commit() or begin_nested(); with
    join_transaction_mode="create_savepoint" those only touch savepoints, so
    nothing a test writes survives the test.
    """
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


# --- API fixtures (Phase 3) ------------------------------------------------------
@pytest.fixture
def test_settings(test_database_url: str) -> Settings:
    """Settings for an app instance that talks to the test database.

    The request log is off by default so API tests do not leave rows behind;
    tests about the request log switch it on (see committed_data).
    """
    return Settings(
        _env_file=None,
        database_url=test_database_url,
        environment="test",
        log_level="INFO",
        log_format="console",
        cors_origins=TEST_CORS_ORIGIN,
        docs_enabled=True,
        request_log_enabled=False,
    )


@pytest.fixture
def app(test_settings: Settings) -> FastAPI:
    """A fresh app instance. Tests may add test-only routes before starting it."""
    return create_app(test_settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client that calls the app in memory, with the lifespan running.

    httpx's ASGITransport does not run the lifespan itself, so the fixture
    enters it: the engine and session factory exist for the whole test and the
    engine is disposed afterwards.
    """
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client


# --- Committed data for API tests (Phase 4) --------------------------------------
CHENNAI_HOURS = {day: [["10:00", "22:00"]] for day in ("mon", "tue", "wed", "thu", "fri", "sat")}
CHENNAI_HOURS["sun"] = []


@dataclass
class CommittedData:
    """Rows the app can see through its own connections.

    The rollback session (db_session) is invisible to the app, which opens its
    own connections, so API tests commit real rows. Everything inserted here,
    plus every api_request_log and audit_log row, is deleted after the test.
    """

    engine: AsyncEngine
    store_ids: list[uuid.UUID] = field(default_factory=list)

    async def add_store(self, site_code: str, **values: Any) -> uuid.UUID:
        row = {
            "id": uuid.uuid4(),
            "site_code": site_code,
            "name": f"Test Store {site_code}",
            "region": "Test Region",
            "country_code": "IN",
            "city": "Test City",
            "timezone": "Asia/Kolkata",
            "business_hours": CHENNAI_HOURS,
            **values,
        }
        async with self.engine.begin() as connection:
            await connection.execute(insert(Store).values(**row))
        self.store_ids.append(row["id"])
        return row["id"]

    async def cleanup(self) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(delete(ApiRequestLog))
            await connection.execute(delete(AuditLog))
            if self.store_ids:
                await connection.execute(delete(Store).where(Store.id.in_(self.store_ids)))


@pytest.fixture
async def committed_data(migrated_db: str) -> AsyncIterator[CommittedData]:
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    data = CommittedData(engine=engine)
    try:
        yield data
    finally:
        await data.cleanup()
        await engine.dispose()


@pytest.fixture
async def chennai(committed_data: CommittedData) -> uuid.UUID:
    """Store 01234, Chennai, open Mon-Sat 10:00-22:00 IST, closed on Sunday."""
    return await committed_data.add_store(
        "01234", name="Chennai Anna Nagar", region="India", city="Chennai"
    )
