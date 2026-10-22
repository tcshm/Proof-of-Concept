# tests/test_verify.py
"""scripts.verify checks on the full seed after a worker drain (rolled-back test
database; the fake LLM answers every call, no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FixedClock
from app.llm.fake import FakeProvider
from app.models import Incident, IncidentJournal, StoreAlert
from app.seed.loader import load_seed_data
from app.seed.runner import STEP_ORDER, STEPS
from app.verify import run_checks
from app.worker.runner import Worker, WorkerConfig

ANCHOR = datetime(2026, 10, 13, 3, 30, tzinfo=UTC)  # 09:00 IST
NOW = ANCHOR + timedelta(minutes=1)


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
async def drained(db_session: AsyncSession):
    data = load_seed_data()
    for name in STEP_ORDER:
        await STEPS[name](db_session, data, ANCHOR)
    factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    config = WorkerConfig(poll_seconds=0, min_call_interval=0, rate_limit_pause=0)
    worker = Worker(factory, FakeProvider(), FixedClock(NOW), config, sleep=_no_sleep)
    stats = await worker.drain(wait_for_retries=False)
    return stats


def failures(results) -> dict[str, str]:
    return {r.name: r.detail for r in results if not r.ok}


async def test_everything_passes_after_a_drain(db_session: AsyncSession, drained) -> None:
    assert drained.still_pending == 0
    assert drained.outcomes["alert_created"] == 8
    results = await run_checks(db_session, now=NOW)
    assert failures(results) == {}
    assert len(results) == 19


async def test_pending_entry_and_missing_alert_are_reported(db_session, drained) -> None:
    await db_session.execute(
        update(IncidentJournal)
        .where(IncidentJournal.element == "system")
        .values(process_status="pending", processed_at=None)
        .execution_options(synchronize_session=False)
    )
    await db_session.execute(
        update(StoreAlert)
        .where(StoreAlert.incident_id == Incident.id, Incident.number == "INC00001234")
        .values(status="closed", close_reason="manual", closed_at=NOW)
        .execution_options(synchronize_session=False)
    )
    failed = failures(await run_checks(db_session, now=NOW))
    assert set(failed) == {"journal queue handled", "alert per active incident"}
    assert "run scripts.worker --drain" in failed["journal queue handled"]
    assert failed["alert per active incident"] == "no active alert: INC00001234"


async def test_changed_mock_incident_is_reported(db_session, drained) -> None:
    await db_session.execute(
        update(Incident)
        .where(Incident.number == "INC00001236")
        .values(short_description="Register 04 - Changed")
        .execution_options(synchronize_session=False)
    )
    failed = failures(await run_checks(db_session, now=NOW))
    assert failed == {"mock incidents active": "missing or changed: INC00001236"}
