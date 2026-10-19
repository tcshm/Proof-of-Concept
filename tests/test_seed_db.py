# tests/test_seed_db.py
"""The seed steps write the expected rows and can safely run more than once."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AiPromptVersion,
    Device,
    NetworkElement,
    NetworkElementCurrentStatus,
    RawNetworkEvent,
    Store,
)
from app.seed.loader import load_seed_data
from app.seed.runner import STEP_ORDER, STEPS, SeedDependencyError

ANCHOR = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


async def _count(session: AsyncSession, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def _seed_all(session: AsyncSession, anchor: datetime = ANCHOR) -> None:
    data = load_seed_data()
    for name in STEP_ORDER:
        await STEPS[name](session, data, anchor)


async def test_seed_all_writes_expected_rows(db_session: AsyncSession) -> None:
    await _seed_all(db_session)
    assert await _count(db_session, Store) == 10
    assert await _count(db_session, NetworkElement) == 130
    assert await _count(db_session, Device) == 170
    assert await _count(db_session, AiPromptVersion) == 2
    raw = await _count(db_session, RawNetworkEvent)
    assert 150 <= raw <= 400
    # The network step processed the window: nothing is left pending.
    pending = await db_session.scalar(
        select(func.count())
        .select_from(RawNetworkEvent)
        .where(RawNetworkEvent.processing_status == "pending")
    )
    assert pending == 0
    assert await _count(db_session, NetworkElementCurrentStatus) == 130


async def test_seed_is_idempotent(db_session: AsyncSession) -> None:
    await _seed_all(db_session)
    first = [await _count(db_session, m) for m in (Store, NetworkElement, Device, RawNetworkEvent)]
    await _seed_all(db_session, ANCHOR + timedelta(hours=3))
    second = [await _count(db_session, m) for m in (Store, NetworkElement, Device, RawNetworkEvent)]
    assert first == second
    newest = await db_session.scalar(select(func.max(RawNetworkEvent.event_time)))
    assert newest <= ANCHOR + timedelta(hours=3)
    assert newest > ANCHOR  # the old window was replaced, not kept


async def test_chennai_rows_in_database(db_session: AsyncSession) -> None:
    await _seed_all(db_session)
    store = await db_session.scalar(select(Store).where(Store.site_code == "01234"))
    assert store is not None and store.timezone == "Asia/Kolkata"
    offline = (
        await db_session.scalars(
            select(Device.name)
            .where(Device.store_id == store.id, Device.status == "offline")
            .order_by(Device.name)
        )
    ).all()
    assert offline == ["POS Terminal 03", "Register 04"]


async def test_steps_check_their_dependencies(db_session: AsyncSession) -> None:
    data = load_seed_data()
    with pytest.raises(SeedDependencyError, match="scripts.seed master"):
        await STEPS["inventory"](db_session, data, ANCHOR)
    await STEPS["master"](db_session, data, ANCHOR)
    with pytest.raises(SeedDependencyError, match="scripts.seed inventory"):
        await STEPS["events"](db_session, data, ANCHOR)
    with pytest.raises(SeedDependencyError, match="scripts.seed events"):
        await STEPS["network"](db_session, data, ANCHOR)
