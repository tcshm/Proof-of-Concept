# app/seed/db.py
"""Small database helpers shared by the seed writers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

# Never overwritten by an upsert: the row keeps its original creation time,
# and updated_at is maintained by the database trigger.
_KEEP_ON_UPDATE = {"created_at", "updated_at"}


async def upsert(
    session: AsyncSession,
    model: type[Base],
    rows: Sequence[dict[str, Any]],
    conflict: Sequence[str] = ("id",),
) -> None:
    """INSERT ... ON CONFLICT (conflict) DO UPDATE SET every other given column."""
    if not rows:
        return
    statement = pg_insert(model.__table__).values(list(rows))
    skip = set(conflict) | _KEEP_ON_UPDATE
    statement = statement.on_conflict_do_update(
        index_elements=list(conflict),
        set_={column: statement.excluded[column] for column in rows[0] if column not in skip},
    )
    await session.execute(statement)


async def insert_many(
    session: AsyncSession, model: type[Base], rows: Sequence[dict[str, Any]], chunk: int = 500
) -> None:
    """Plain INSERT in chunks (asyncpg allows at most 32767 parameters per statement)."""
    for start in range(0, len(rows), chunk):
        await session.execute(pg_insert(model.__table__).values(list(rows[start : start + chunk])))
