# app/worker/queue.py
"""The journal queue: claim, release, finish.

claim_next picks the OLDEST pending entry that is due (available_at <= now) and
whose incident has no older entry still pending or processing, so an incident's
entries are handled in order ("Incident created" before its work notes). It uses
FOR UPDATE SKIP LOCKED, so two workers would never take the same entry.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import IncidentJournal
from app.models.values import JournalProcessStatus

_WAITING = (JournalProcessStatus.PENDING, JournalProcessStatus.PROCESSING)
MAX_ERROR = 2000


@dataclass(frozen=True)
class ClaimedEntry:
    id: uuid.UUID
    incident_id: uuid.UUID
    element: str
    value: str
    author_type: str
    created_at: datetime
    attempts: int  # including this attempt


async def claim_next(session: AsyncSession, now: datetime) -> ClaimedEntry | None:
    """Mark the next entry 'processing' (attempts + 1). The caller commits."""
    earlier = aliased(IncidentJournal)
    blocked = (
        select(earlier.id)
        .where(
            earlier.incident_id == IncidentJournal.incident_id,
            earlier.process_status.in_(_WAITING),
            tuple_(earlier.created_at, earlier.id)
            < tuple_(IncidentJournal.created_at, IncidentJournal.id),
        )
        .exists()
    )
    entry = await session.scalar(
        select(IncidentJournal)
        .where(
            IncidentJournal.process_status == JournalProcessStatus.PENDING,
            IncidentJournal.available_at <= now,
            ~blocked,
        )
        .order_by(IncidentJournal.created_at, IncidentJournal.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=IncidentJournal)
        .execution_options(populate_existing=True)
    )
    if entry is None:
        return None
    entry.process_status = JournalProcessStatus.PROCESSING
    entry.attempts = entry.attempts + 1
    await session.flush()
    return ClaimedEntry(
        id=entry.id,
        incident_id=entry.incident_id,
        element=entry.element,
        value=entry.value,
        author_type=entry.author_type,
        created_at=entry.created_at,
        attempts=entry.attempts,
    )


async def _set(session: AsyncSession, entry_id: uuid.UUID, **values) -> None:
    await session.execute(
        update(IncidentJournal)
        .where(IncidentJournal.id == entry_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )


async def finish_entry(
    session: AsyncSession, entry_id: uuid.UUID, status: JournalProcessStatus, now: datetime
) -> None:
    """'done' (handled) or 'skipped' (nothing to do)."""
    await _set(session, entry_id, process_status=status, processed_at=now, last_error=None)


async def release_for_retry(
    session: AsyncSession, entry_id: uuid.UUID, error: str, available_at: datetime
) -> None:
    await _set(
        session,
        entry_id,
        process_status=JournalProcessStatus.PENDING,
        available_at=available_at,
        last_error=error[:MAX_ERROR],
    )


async def fail_entry(session: AsyncSession, entry_id: uuid.UUID, error: str, now: datetime) -> None:
    await _set(
        session,
        entry_id,
        process_status=JournalProcessStatus.FAILED,
        processed_at=now,
        last_error=error[:MAX_ERROR],
    )


async def reset_processing(session: AsyncSession) -> int:
    """Entries left 'processing' by a stopped worker go back to 'pending'."""
    rows = await session.scalars(
        update(IncidentJournal)
        .where(IncidentJournal.process_status == JournalProcessStatus.PROCESSING)
        .values(process_status=JournalProcessStatus.PENDING)
        .returning(IncidentJournal.id)
        .execution_options(synchronize_session=False)
    )
    return len(rows.all())


async def pending_count(session: AsyncSession) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(IncidentJournal)
        .where(IncidentJournal.process_status == JournalProcessStatus.PENDING)
    )
    return count or 0
