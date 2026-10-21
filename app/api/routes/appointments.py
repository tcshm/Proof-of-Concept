# app/api/routes/appointments.py
"""GET /api/v1/appointments: the store's upcoming visits."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.context import StoreDep
from app.api.deps import ClockDep, SessionDep
from app.api.schemas.appointments import AppointmentItem
from app.core.timefmt import format_when
from app.queries.appointments import upcoming_appointments

router = APIRouter(prefix="/appointments", tags=["appointments"])


@router.get("", response_model=list[AppointmentItem])
async def list_appointments(
    store: StoreDep, session: SessionDep, clock: ClockDep
) -> list[AppointmentItem]:
    now = clock.now()
    return [
        AppointmentItem(
            id=row.id,
            appointment_type=row.appointment_type,
            title=row.title,
            sub_title=row.purpose,
            when=format_when(row.scheduled_start, store.timezone, now),
            scheduled_start=row.scheduled_start,
            scheduled_end=row.scheduled_end,
            status=row.status,
            details=row.details,
            incident_id=row.incident_number,
        )
        for row in await upcoming_appointments(session, store.id, now)
    ]
