# tests/test_session.py
"""session_scope commits on success and rolls back on error."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.db.session import create_session_factory, session_scope
from app.models import AssignmentGroup


async def test_session_scope_commit_and_rollback(migrated_db: str) -> None:
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    factory = create_session_factory(engine)
    committed = f"test-group-{uuid.uuid4().hex[:8]}"
    rolled_back = f"test-group-{uuid.uuid4().hex[:8]}"

    async def count(name: str) -> int:
        async with factory() as session:
            stmt = select(func.count()).where(AssignmentGroup.name == name)
            return await session.scalar(stmt)

    try:
        async with session_scope(factory) as session:
            session.add(AssignmentGroup(name=committed))
        assert await count(committed) == 1

        with pytest.raises(RuntimeError):
            async with session_scope(factory) as session:
                session.add(AssignmentGroup(name=rolled_back))
                await session.flush()
                raise RuntimeError("boom")
        assert await count(rolled_back) == 0
    finally:
        async with session_scope(factory) as session:
            await session.execute(delete(AssignmentGroup).where(AssignmentGroup.name == committed))
        await engine.dispose()
