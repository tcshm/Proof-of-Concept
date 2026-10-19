# app/processing/status.py
"""The current status of every element, kept in memory during a run.

Rule: an event changes an element's status only if it is NEWER (by event time)
than the newest status report already applied to that element. A late, older
event is stored with status_applied = false and changes nothing.

A report with the same status as the current one is "applied" (it is the newest
report) but does not move status_since: that stays the time the element
entered its current status.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NetworkElementCurrentStatus, NetworkEvent


@dataclass
class ElementState:
    status: str
    status_since: datetime | None
    last_status_event_id: str | None
    last_event_at: datetime | None
    last_report_at: datetime | None
    dirty: bool = False


class StatusBook:
    def __init__(self, states: dict[uuid.UUID, ElementState]) -> None:
        self.states = states

    @classmethod
    async def load(cls, session: AsyncSession) -> StatusBook:
        latest_reports = dict(
            (
                await session.execute(
                    select(NetworkEvent.network_element_id, func.max(NetworkEvent.occurred_at))
                    .where(NetworkEvent.status_applied)
                    .group_by(NetworkEvent.network_element_id)
                )
            ).all()
        )
        rows = (await session.execute(select(NetworkElementCurrentStatus))).scalars().all()
        states = {
            row.network_element_id: ElementState(
                status=row.status,
                status_since=row.status_since,
                last_status_event_id=row.last_status_event_id,
                last_event_at=row.last_event_at,
                last_report_at=latest_reports.get(row.network_element_id),
            )
            for row in rows
        }
        return cls(states)

    def is_newer(self, element_id: uuid.UUID, occurred_at: datetime) -> bool:
        state = self.states.get(element_id)
        if state is None:
            return False
        return state.last_report_at is None or occurred_at > state.last_report_at

    def record(
        self,
        element_id: uuid.UUID,
        event_id: str,
        occurred_at: datetime,
        reported_status: str | None,
        applied: bool,
    ) -> bool:
        """Update the in-memory state after the event row is stored.

        Returns True when the element's status value changed.
        """
        state = self.states.get(element_id)
        if state is None:
            return False
        if state.last_event_at is None or occurred_at > state.last_event_at:
            state.last_event_at = occurred_at
            state.dirty = True
        if not applied or reported_status is None:
            return False
        state.last_report_at = occurred_at
        state.dirty = True
        if state.status == reported_status:
            return False
        state.status = reported_status
        state.status_since = occurred_at
        state.last_status_event_id = event_id
        return True

    async def flush(self, session: AsyncSession) -> int:
        """Write every changed state back to network_element_current_status."""
        rows = [
            {
                "network_element_id": element_id,
                "status": state.status,
                "status_since": state.status_since,
                "last_status_event_id": state.last_status_event_id,
                "last_event_at": state.last_event_at,
            }
            for element_id, state in self.states.items()
            if state.dirty
        ]
        if rows:
            await session.execute(update(NetworkElementCurrentStatus), rows)
        for state in self.states.values():
            state.dirty = False
        return len(rows)
