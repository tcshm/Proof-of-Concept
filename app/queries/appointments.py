# app/queries/appointments.py
"""Data for GET /api/v1/appointments: a store's upcoming visits."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Appointment, Incident
from app.models.values import AppointmentStatus

# A visit without an end time is treated as lasting this long.
DEFAULT_VISIT_LENGTH = timedelta(hours=2)


@dataclass(frozen=True)
class AppointmentRow:
    id: uuid.UUID
    appointment_type: str
    title: str
    purpose: str
    details: str | None
    scheduled_start: datetime
    scheduled_end: datetime | None
    status: str
    incident_number: str


async def upcoming_appointments(
    session: AsyncSession, store_id: uuid.UUID, now: datetime
) -> list[AppointmentRow]:
    """Scheduled or in-progress visits that are not over yet, soonest first."""
    ends_at = func.coalesce(
        Appointment.scheduled_end, Appointment.scheduled_start + DEFAULT_VISIT_LENGTH
    )
    rows = (
        await session.execute(
            select(
                Appointment.id,
                Appointment.appointment_type,
                Appointment.title,
                Appointment.purpose,
                Appointment.details,
                Appointment.scheduled_start,
                Appointment.scheduled_end,
                Appointment.status,
                Incident.number.label("incident_number"),
            )
            .join(Incident, Incident.id == Appointment.incident_id)
            .where(
                Appointment.store_id == store_id,
                Appointment.status.in_(
                    (AppointmentStatus.SCHEDULED, AppointmentStatus.IN_PROGRESS)
                ),
                ends_at > now,
            )
            .order_by(Appointment.scheduled_start, Appointment.title)
        )
    ).all()
    return [AppointmentRow(**row._asdict()) for row in rows]
