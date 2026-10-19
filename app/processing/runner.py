# app/processing/runner.py
"""One processing run over raw_network_events.

    rebuild      clear the derived tables, reset every raw row to pending, replay all
    incremental  process only pending rows

Only events that had ARRIVED by as_of (ingestion_time <= as_of) are processed,
in arrival order (ingestion_time, event_id), the way a live feed sees them.
The whole run is one transaction, owned by the caller: a half-finished rebuild
is never visible. A PostgreSQL advisory lock stops two runs at the same time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    EventProcessingRun,
    NetworkElement,
    NetworkElementCurrentStatus,
    NetworkEvent,
    RawNetworkEvent,
    Store,
)
from app.models.values import ProcessingRunMode, ProcessingRunStatus, RawProcessingStatus
from app.processing.matching import ElementIndex
from app.processing.processor import Outcome, ProcessingContext, process_event
from app.processing.status import StatusBook

BATCH_SIZE = 500

# Any fixed number; the same key must be used by every processor run.
ADVISORY_LOCK_KEY = 7_420_501


class ProcessorBusyError(RuntimeError):
    """Another processing run holds the lock."""


@dataclass
class RunCounts:
    events_read: int = 0
    events_processed: int = 0
    duplicates: int = 0
    unmatched: int = 0
    errors: int = 0
    status_changes: int = 0

    def add(self, outcome: Outcome) -> None:
        self.events_read += 1
        if outcome.status == RawProcessingStatus.PROCESSED:
            self.events_processed += 1
        elif outcome.status == RawProcessingStatus.DUPLICATE:
            self.duplicates += 1
        elif outcome.status == RawProcessingStatus.UNMATCHED:
            self.unmatched += 1
        elif outcome.status == RawProcessingStatus.ERROR:
            self.errors += 1
        if outcome.status_changed:
            self.status_changes += 1


async def _acquire_lock(session: AsyncSession) -> None:
    locked = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
    )
    if not locked:
        raise ProcessorBusyError("another event processing run is in progress")


async def _reset_for_rebuild(session: AsyncSession) -> None:
    """Derived tables first (foreign keys), then every raw row back to pending."""
    await session.execute(delete(NetworkElementCurrentStatus))
    await session.execute(delete(NetworkEvent))
    await session.execute(
        update(RawNetworkEvent).values(
            processing_status=RawProcessingStatus.PENDING, processed_at=None, process_error=None
        )
    )


async def _ensure_status_rows(session: AsyncSession) -> None:
    """One 'unknown' current-status row for every active element that has none."""
    statement = pg_insert(NetworkElementCurrentStatus).from_select(
        ["network_element_id", "store_id"],
        select(NetworkElement.id, NetworkElement.store_id).where(NetworkElement.is_active),
    )
    await session.execute(statement.on_conflict_do_nothing(index_elements=["network_element_id"]))


async def _seen_vendor_ids(session: AsyncSession) -> set[str]:
    rows = await session.scalars(
        select(NetworkEvent.vendor_event_uuid).where(NetworkEvent.vendor_event_uuid.is_not(None))
    )
    return set(rows.all())


async def _pending_batch(
    session: AsyncSession, as_of: datetime, after: tuple[datetime, str] | None
) -> list[Any]:
    query = (
        select(
            RawNetworkEvent.event_id,
            RawNetworkEvent.event_type,
            RawNetworkEvent.event_time,
            RawNetworkEvent.ingestion_time,
            RawNetworkEvent.source,
            RawNetworkEvent.site_id,
            RawNetworkEvent.correlation_id,
            RawNetworkEvent.payload,
        )
        .where(
            RawNetworkEvent.processing_status == RawProcessingStatus.PENDING,
            RawNetworkEvent.ingestion_time <= as_of,
        )
        .order_by(RawNetworkEvent.ingestion_time, RawNetworkEvent.event_id)
        .limit(BATCH_SIZE)
    )
    if after is not None:
        query = query.where(
            tuple_(RawNetworkEvent.ingestion_time, RawNetworkEvent.event_id) > tuple_(*after)
        )
    return list((await session.execute(query)).all())


async def _mark_raw(
    session: AsyncSession, outcomes: list[tuple[str, Outcome]], processed_at: datetime
) -> None:
    if not outcomes:
        return
    await session.execute(
        update(RawNetworkEvent),
        [
            {
                "event_id": event_id,
                "processing_status": str(outcome.status),
                "processed_at": processed_at,
                "process_error": outcome.error,
            }
            for event_id, outcome in outcomes
        ],
    )


async def process_pending(
    session: AsyncSession,
    *,
    as_of: datetime,
    rebuild: bool,
    now: datetime | None = None,
) -> RunCounts:
    """Process pending raw events inside the caller's transaction (never commits)."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    processed_at = now or datetime.now(UTC)

    await _acquire_lock(session)
    if rebuild:
        await _reset_for_rebuild(session)
    await _ensure_status_rows(session)

    ctx = ProcessingContext(
        index=await ElementIndex.load(session),
        book=await StatusBook.load(session),
        seen_vendor_ids=await _seen_vendor_ids(session),
    )
    counts = RunCounts()
    after: tuple[datetime, str] | None = None
    while batch := await _pending_batch(session, as_of, after):
        outcomes: list[tuple[str, Outcome]] = []
        for raw in batch:
            try:
                outcome = await process_event(session, raw, ctx)
            except Exception as exc:  # one bad event never stops the run
                outcome = Outcome(
                    RawProcessingStatus.ERROR, f"{exc.__class__.__name__}: {exc}"[:500]
                )
            counts.add(outcome)
            outcomes.append((raw.event_id, outcome))
        await _mark_raw(session, outcomes, processed_at)
        last = batch[-1]
        after = (last.ingestion_time, last.event_id)

    await ctx.book.flush(session)
    return counts


async def run_processor(
    session: AsyncSession,
    *,
    mode: ProcessingRunMode | str,
    as_of: datetime,
    triggered_by: str,
    now: datetime | None = None,
) -> tuple[EventProcessingRun, RunCounts]:
    """A recorded run: run row, processing, run row completed. Never commits."""
    mode = ProcessingRunMode(mode)
    run = EventProcessingRun(mode=str(mode), as_of=as_of, triggered_by=triggered_by)
    session.add(run)
    await session.flush()

    counts = await process_pending(
        session, as_of=as_of, rebuild=mode == ProcessingRunMode.REBUILD, now=now
    )

    run.status = str(ProcessingRunStatus.SUCCEEDED)
    run.events_read = counts.events_read
    run.events_processed = counts.events_processed
    run.duplicates = counts.duplicates
    run.unmatched = counts.unmatched
    run.errors = counts.errors
    run.status_changes = counts.status_changes
    run.finished_at = now or datetime.now(UTC)
    await session.flush()
    return run, counts


async def record_failed_run(
    session: AsyncSession,
    *,
    mode: ProcessingRunMode | str,
    as_of: datetime,
    triggered_by: str,
    error: str,
) -> None:
    """Called in a NEW transaction after a run failed and was rolled back."""
    session.add(
        EventProcessingRun(
            mode=str(ProcessingRunMode(mode)),
            as_of=as_of,
            status=str(ProcessingRunStatus.FAILED),
            error_message=error[:1000],
            triggered_by=triggered_by,
            finished_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def status_summary(session: AsyncSession) -> list[tuple[str, int, int, int]]:
    """(site_code, online, offline, unknown) for every store, by site code."""
    status = NetworkElementCurrentStatus.status
    rows = (
        await session.execute(
            select(
                Store.site_code,
                func.count().filter(status == "online"),
                func.count().filter(status == "offline"),
                func.count().filter(status == "unknown"),
            )
            .join(NetworkElementCurrentStatus, NetworkElementCurrentStatus.store_id == Store.id)
            .group_by(Store.site_code)
            .order_by(Store.site_code)
        )
    ).all()
    return [tuple(row) for row in rows]
