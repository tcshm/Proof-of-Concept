# tests/conftest.py
"""Shared fixtures. Tests run against <database>_test, never the main database."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from alembic import command
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.migrate import alembic_config


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
