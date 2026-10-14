# scripts/db_status.py
"""Show the migration revision and table count of the configured database.

uv run python -m scripts.db_status
"""

from __future__ import annotations

import asyncio
import sys

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.migrate import alembic_config

EXPECTED_TABLES = 22

TABLE_COUNT_SQL = text(
    """
    SELECT count(*) FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_type = 'BASE TABLE'
      AND table_name <> 'alembic_version'
    """
)


async def main() -> int:
    settings = get_settings()
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            has_version = await conn.scalar(text("SELECT to_regclass('alembic_version')"))
            current = (
                await conn.scalar(text("SELECT version_num FROM alembic_version"))
                if has_version
                else None
            )
            tables = await conn.scalar(TABLE_COUNT_SQL)
    finally:
        await engine.dispose()

    print(f"database : {make_url(settings.database_url).database}")
    print(f"revision : {current or 'none'} (head {head})")
    print(f"tables   : {tables} (expected {EXPECTED_TABLES})")
    ok = current == head and tables == EXPECTED_TABLES
    print("status   : OK" if ok else "status   : NOT UP TO DATE - run: uv run alembic upgrade head")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
