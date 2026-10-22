# app/services/appointments.py
"""The appointment service: the ONLY code that writes appointments.

Appointments are created from the admin portal (Phase 13) and shown on the
mobile app. Rules:
    - every appointment is tagged to an ACTIVE incident (state 1-3) of the same
      store; the store comes from the incident,
    - the visit must start in the future and end after it starts,
    - only scheduled visits can be rescheduled; cancelling needs a note.

Scheduling, rescheduling and cancelling write the store notification in the
SAME transaction (Phase 7), so a visit is never booked without the store being
told. Status updates (in progress, completed, no-show) send no notification.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import ActorContext
from app.core.errors import BusinessRuleError, ConflictError, NotFoundError
from app.models import Appointment
from app.models.values import (
    ACTIVE_INCIDENT_STATES,
    AppointmentSource,
    AppointmentStatus,
    AppointmentType,
    AuditAction,
    SystemEvent,
)
from app.services.audit import diff, record_audit
from app.services.incident_rules import state_label
from app.services.incidents import lock_incident
from app.services.notifications import notify_appointment

# from-status -> statuses update_appointment_status may move to.
_STATUS_MOVES: dict[str, frozenset[str]] = {
    AppointmentStatus.SCHEDULED: frozenset(
        {AppointmentStatus.IN_PROGRESS, AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW}
    ),
    AppointmentStatus.IN_PROGRESS: frozenset({AppointmentStatus.COMPLETED}),
}
_OPEN_STATUSES = (AppointmentStatus.SCHEDULED, AppointmentStatus.IN_PROGRESS)


@dataclass(frozen=True)
class NewAppointment:
    appointment_type: str
    title: str
    purpose: str
    scheduled_start: datetime
    scheduled_end: datetime | None = None
    details: str | None = None
    vendor_name: str | None = None
    technician_name: str | None = None
    vendor_reference: str | None = None


def _source(actor: ActorContext) -> str:
    try:
        return str(AppointmentSource(actor.source))
    except ValueError as exc:
        raise BusinessRuleError("Appointments are created from the admin portal.") from exc


def _check_times(start: datetime, end: datetime | None, now: datetime) -> None:
    if start <= now:
        raise BusinessRuleError("The appointment must start in the future.")
    if end is not None and end <= start:
        raise BusinessRuleError("The appointment must end after it starts.")


def _snapshot(appointment: Appointment) -> dict:
    return {
        "status": appointment.status,
        "scheduled_start": appointment.scheduled_start,
        "scheduled_end": appointment.scheduled_end,
        "status_note": appointment.status_note,
    }


async def _finish(session: AsyncSession, commit: bool) -> None:
    if commit:
        await session.commit()
    else:
        await session.flush()


async def _lock_appointment(session: AsyncSession, appointment_id: uuid.UUID) -> Appointment:
    appointment = await session.scalar(
        select(Appointment)
        .where(Appointment.id == appointment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if appointment is None:
        raise NotFoundError(f"Appointment {appointment_id} was not found.")
    return appointment


async def _require_active_incident(session: AsyncSession, incident_id: uuid.UUID):
    incident = await lock_incident(session, incident_id)
    if incident.state not in ACTIVE_INCIDENT_STATES:
        raise ConflictError(
            f"Incident {incident.number} is {state_label(incident.state)}; "
            "appointments need an active incident."
        )
    return incident


async def create_appointment(
    session: AsyncSession,
    actor: ActorContext,
    incident_id: uuid.UUID,
    data: NewAppointment,
    now: datetime,
    *,
    commit: bool = True,
) -> Appointment:
    try:
        appointment_type = str(AppointmentType(data.appointment_type))
    except ValueError as exc:
        raise BusinessRuleError(f"Unknown appointment type {data.appointment_type!r}.") from exc
    if not data.title.strip() or not data.purpose.strip():
        raise BusinessRuleError("Title and purpose are required.")
    _check_times(data.scheduled_start, data.scheduled_end, now)
    source = _source(actor)
    incident = await _require_active_incident(session, incident_id)

    appointment = Appointment(
        id=uuid.uuid4(),
        store_id=incident.store_id,
        incident_id=incident.id,
        appointment_type=appointment_type,
        title=data.title.strip(),
        purpose=data.purpose.strip(),
        details=data.details,
        scheduled_start=data.scheduled_start,
        scheduled_end=data.scheduled_end,
        status=AppointmentStatus.SCHEDULED,
        vendor_name=data.vendor_name,
        technician_name=data.technician_name,
        vendor_reference=data.vendor_reference,
        created_by=actor.actor,
        source=source,
    )
    session.add(appointment)
    await session.flush()
    record_audit(
        session,
        actor,
        action=AuditAction.CREATE,
        entity_type="appointments",
        entity_id=appointment.id,
        store_id=appointment.store_id,
        changes={
            "incident": incident.number,
            **diff({}, _snapshot(appointment), ["status", "scheduled_start", "scheduled_end"]),
        },
    )
    await notify_appointment(session, actor, appointment, SystemEvent.APPOINTMENT_SCHEDULED, now)
    await _finish(session, commit)
    return appointment


async def reschedule_appointment(
    session: AsyncSession,
    actor: ActorContext,
    appointment_id: uuid.UUID,
    scheduled_start: datetime,
    scheduled_end: datetime | None,
    now: datetime,
    *,
    commit: bool = True,
) -> Appointment:
    appointment = await _lock_appointment(session, appointment_id)
    if appointment.status != AppointmentStatus.SCHEDULED:
        raise ConflictError(
            f"Only scheduled appointments can be rescheduled ({appointment.status})."
        )
    _check_times(scheduled_start, scheduled_end, now)
    await _require_active_incident(session, appointment.incident_id)
    before = _snapshot(appointment)
    old_start = appointment.scheduled_start
    appointment.scheduled_start = scheduled_start
    appointment.scheduled_end = scheduled_end
    record_audit(
        session,
        actor,
        action=AuditAction.UPDATE,
        entity_type="appointments",
        entity_id=appointment.id,
        store_id=appointment.store_id,
        changes=diff(before, _snapshot(appointment)),
    )
    await notify_appointment(
        session,
        actor,
        appointment,
        SystemEvent.APPOINTMENT_RESCHEDULED,
        now,
        old_start=old_start,
    )
    await _finish(session, commit)
    return appointment


async def cancel_appointment(
    session: AsyncSession,
    actor: ActorContext,
    appointment_id: uuid.UUID,
    note: str,
    now: datetime,
    *,
    commit: bool = True,
) -> Appointment:
    if not note or not note.strip():
        raise BusinessRuleError("A note is required to cancel an appointment.")
    appointment = await _lock_appointment(session, appointment_id)
    if appointment.status not in _OPEN_STATUSES:
        raise ConflictError(f"The appointment is already {appointment.status}.")
    before = _snapshot(appointment)
    appointment.status = AppointmentStatus.CANCELLED
    appointment.status_note = note.strip()
    record_audit(
        session,
        actor,
        action=AuditAction.STATUS_CHANGE,
        entity_type="appointments",
        entity_id=appointment.id,
        store_id=appointment.store_id,
        changes=diff(before, _snapshot(appointment)),
    )
    await notify_appointment(session, actor, appointment, SystemEvent.APPOINTMENT_CANCELLED, now)
    await _finish(session, commit)
    return appointment


async def update_appointment_status(
    session: AsyncSession,
    actor: ActorContext,
    appointment_id: uuid.UUID,
    status: str,
    now: datetime,
    *,
    note: str | None = None,
    commit: bool = True,
) -> Appointment:
    """scheduled -> in_progress / completed / no_show; in_progress -> completed."""
    appointment = await _lock_appointment(session, appointment_id)
    if status not in _STATUS_MOVES.get(appointment.status, frozenset()):
        raise ConflictError(f"An appointment cannot move from {appointment.status} to {status}.")
    if status == AppointmentStatus.NO_SHOW and not (note and note.strip()):
        raise BusinessRuleError("A note is required for a no-show.")
    before = _snapshot(appointment)
    appointment.status = status
    if note and note.strip():
        appointment.status_note = note.strip()
    record_audit(
        session,
        actor,
        action=AuditAction.STATUS_CHANGE,
        entity_type="appointments",
        entity_id=appointment.id,
        store_id=appointment.store_id,
        changes=diff(before, _snapshot(appointment)),
    )
    await _finish(session, commit)
    return appointment
