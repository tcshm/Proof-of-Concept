# app/db/session.py
"""Async engine and session factory.

One engine (connection pool) per process: the API, the worker and each script
create it once at startup and dispose of it at shutdown. A session is one short
unit of work: open it, do one transaction, close it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings


def create_engine_from_settings(settings: Settings | None = None) -> AsyncEngine:
    """Build the async engine (connection pool) from DATABASE_URL."""
    settings = settings or get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions never expire loaded objects on commit.

    With async SQLAlchemy an expired attribute would trigger a hidden database
    call on next access, which raises an error outside an awaited context.
    """
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One transaction for scripts and the worker: commit on success, roll back on error."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
