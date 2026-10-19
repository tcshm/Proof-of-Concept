# tests/test_processor_seed.py
"""The processor on the full Phase 2 seed (rolled-back test database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    NetworkElement,
    NetworkElementCurrentStatus,
    NetworkEvent,
    RawNetworkEvent,
    Store,
)
from app.processing.runner import process_pending, run_processor, status_summary
from app.seed.loader import load_seed_data
from app.seed.runner import STEPS

ANCHOR = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)

EXPECTED_OFFLINE = {
    ("01234", "AP01234N004"): ANCHOR - timedelta(hours=2, minutes=20),
    ("01234", "WAN01234-PRI"): ANCHOR - timedelta(hours=1, minutes=30),
    ("01456", "WAN01456-BKP"): ANCHOR - timedelta(hours=6),
    ("02556", "SW02556N303"): ANCHOR - timedelta(hours=3),
    ("02556", "AP02556N003"): ANCHOR - timedelta(hours=2, minutes=59),
    ("02556", "AP02556N005"): ANCHOR - timedelta(hours=2, minutes=59),
}


async def _seed_raw(session: AsyncSession) -> None:
    data = load_seed_data()
    for name in ("master", "inventory", "events"):
        await STEPS[name](session, data, ANCHOR)


async def _statuses(session: AsyncSession) -> dict[tuple[str, str], tuple[str, datetime | None]]:
    rows = await session.execute(
        select(
            Store.site_code,
            NetworkElement.name,
            NetworkElementCurrentStatus.status,
            NetworkElementCurrentStatus.status_since,
        )
        .join(NetworkElement, NetworkElement.id == NetworkElementCurrentStatus.network_element_id)
        .join(Store, Store.id == NetworkElement.store_id)
    )
    return {(site, name): (status, since) for site, name, status, since in rows}


async def test_seed_window_gives_expected_counts(db_session: AsyncSession) -> None:
    await _seed_raw(db_session)
    run, counts = await run_processor(db_session, mode="rebuild", as_of=ANCHOR, triggered_by="test")

    assert counts.events_read == 237
    assert counts.events_processed == 235
    assert counts.duplicates == 1
    assert counts.unmatched == 1
    assert counts.errors == 0
    assert run.status == "succeeded"
    assert await db_session.scalar(select(func.count()).select_from(NetworkEvent)) == 236
    pending = await db_session.scalar(
        select(func.count())
        .select_from(RawNetworkEvent)
        .where(RawNetworkEvent.processing_status == "pending")
    )
    assert pending == 0


async def test_seed_window_gives_expected_statuses(db_session: AsyncSession) -> None:
    await _seed_raw(db_session)
    await process_pending(db_session, as_of=ANCHOR, rebuild=True)

    statuses = await _statuses(db_session)
    assert len(statuses) == 130
    offline = {key: since for key, (status, since) in statuses.items() if status == "offline"}
    assert offline == EXPECTED_OFFLINE
    assert all(status != "unknown" for status, _ in statuses.values())

    # Chennai: the store runs on its backup link, both routers stay green.
    assert statuses[("01234", "RN01234N001-1")][0] == "online"
    assert statuses[("01234", "RN01234N001-2")][0] == "online"
    assert statuses[("01234", "WAN01234-BKP")][0] == "online"
    # Flapped overnight and recovered.
    assert statuses[("01234", "SW01234N302")][0] == "online"
    assert statuses[("03118", "AP03118N002")][0] == "online"

    summary = {
        site: (on, off, unknown) for site, on, off, unknown in await status_summary(db_session)
    }
    assert summary["01234"] == (11, 2, 0)
    assert summary["02556"] == (10, 3, 0)


async def test_edge_cases_in_the_seed(db_session: AsyncSession) -> None:
    await _seed_raw(db_session)
    await process_pending(db_session, as_of=ANCHOR, rebuild=True)

    statuses = dict(
        (
            await db_session.execute(
                select(RawNetworkEvent.processing_status, func.count()).group_by(
                    RawNetworkEvent.processing_status
                )
            )
        ).all()
    )
    assert statuses == {"processed": 235, "duplicate": 1, "unmatched": 1}

    # The late AP004 "UP" is stored but not applied.
    late = (
        await db_session.execute(
            select(NetworkEvent.reported_status, NetworkEvent.status_applied).where(
                NetworkEvent.hostname == "AP01234N004",
                NetworkEvent.ingested_at - NetworkEvent.occurred_at > timedelta(hours=1),
            )
        )
    ).one()
    assert tuple(late) == ("online", False)

    ghost = await db_session.scalar(
        select(NetworkEvent.matched_by).where(NetworkEvent.device_serial == "CN03562AP099")
    )
    assert ghost == "none"


async def test_rebuild_is_repeatable_and_incremental_is_a_no_op(db_session: AsyncSession) -> None:
    await _seed_raw(db_session)
    first = await process_pending(db_session, as_of=ANCHOR, rebuild=True)
    first_statuses = await _statuses(db_session)

    second = await process_pending(db_session, as_of=ANCHOR, rebuild=True)
    assert second == first
    assert await _statuses(db_session) == first_statuses

    nothing = await process_pending(db_session, as_of=ANCHOR, rebuild=False)
    assert nothing.events_read == 0
    assert await _statuses(db_session) == first_statuses


async def test_seed_network_step(db_session: AsyncSession) -> None:
    await _seed_raw(db_session)
    message = await STEPS["network"](db_session, load_seed_data(), ANCHOR)
    assert message.startswith("235 processed, 1 duplicate, 1 unmatched, 0 errors")
