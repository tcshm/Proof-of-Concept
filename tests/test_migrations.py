# tests/test_migrations.py
"""The migration can build the schema, remove it completely and build it again."""

from __future__ import annotations

import asyncio

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.db.migrate import alembic_config

EXPECTED_TABLES = 22


async def _table_count(url: str) -> int:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return await conn.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                    "AND table_name <> 'alembic_version'"
                )
            )
    finally:
        await engine.dispose()


async def _function_exists(url: str) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return bool(await conn.scalar(text("SELECT to_regproc('set_updated_at')")))
    finally:
        await engine.dispose()


def test_upgrade_downgrade_upgrade(migrated_db: str) -> None:
    config = alembic_config(migrated_db)

    assert asyncio.run(_table_count(migrated_db)) == EXPECTED_TABLES

    command.downgrade(config, "base")
    assert asyncio.run(_table_count(migrated_db)) == 0
    assert not asyncio.run(_function_exists(migrated_db))

    command.upgrade(config, "head")
    assert asyncio.run(_table_count(migrated_db)) == EXPECTED_TABLES
    assert asyncio.run(_function_exists(migrated_db))
