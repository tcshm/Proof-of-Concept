# app/seed/itsm.py
"""Seed part 2: incidents, their journal entries, appointments and the
"New Appointment" store notification of each appointment.

build_itsm() is pure: seed files + anchor in, rows out. write_itsm() replaces
the ITSM data inside the caller's transaction.

The seed writes rows directly (not through the services) so it can backdate
everything and be re-run safely, but it writes exactly what the services would:
    - a system "Incident created." entry at opened_at for every incident,
    - the close notes as a work note at resolved_at for a resolved incident,
    - every journal entry "pending", so the worker (Phase 9) processes them,
    - one appointment_scheduled notification per appointment, from the same
      template the appointment service uses, published at booked_at.

Re-running replaces all ITSM data and everything derived from it (alerts,
actions, AI runs, incident and appointment notifications), giving a clean demo.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AiWorkflowRun,
    Appointment,
    Incident,
    IncidentJournal,
    Notification,
    NotificationTarget,
    StoreAction,
    StoreActionResponse,
    StoreAlert,
)
from app.models.values import (
    ActorType,
    AppointmentStatus,
    JournalElement,
    JournalProcessStatus,
    JournalSource,
    NotificationSource,
    NotificationStatus,
    SystemEvent,
)
from app.seed import ids
from app.seed.anchor import parse_offset
from app.seed.db import insert_many
from app.seed.itsm_schemas import AppointmentSeed, IncidentSeed
from app.seed.loader import SeedData, SeedDataError
from app.services.notification_templates import appointment_scheduled

CREATED_ENTRY_TEXT = "Incident created."
SEED_CREATED_BY = "seed"


def incident_id(number: str) -> uuid.UUID:
    return uuid.uuid5(ids.RIC_NAMESPACE, f"incident:{number}")


def journal_id(number: str, position: int) -> uuid.UUID:
    return uuid.uuid5(ids.RIC_NAMESPACE, f"incident_journal:{number}:{position}")


def appointment_id(key: str) -> uuid.UUID:
    return uuid.uuid5(ids.RIC_NAMESPACE, f"appointment:{key}")


def appointment_notification_id(key: str) -> uuid.UUID:
    return uuid.uuid5(ids.RIC_NAMESPACE, f"notification:appointment:{key}")


def next_local_time(anchor: datetime, tz: ZoneInfo, hhmm: str, day: str = "today") -> datetime:
    """The store-local time hhmm on the anchor's local day ("today") or the next
    day ("tomorrow"); moved one more day if it is not after the anchor. UTC."""
    hours, minutes = (int(part) for part in hhmm.split(":"))
    local_day = anchor.astimezone(tz).date() + timedelta(days=1 if day == "tomorrow" else 0)
    moment = datetime.combine(local_day, time(hours, minutes), tzinfo=tz)
    if moment <= anchor:
        moment = datetime.combine(local_day + timedelta(days=1), time(hours, minutes), tzinfo=tz)
    return moment.astimezone(UTC)


@dataclass
class ItsmRows:
    incidents: list[dict[str, Any]] = field(default_factory=list)
    journal: list[dict[str, Any]] = field(default_factory=list)
    appointments: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    notification_targets: list[dict[str, Any]] = field(default_factory=list)


def _journal_row(
    number: str,
    position: int,
    element: str,
    value: str,
    author_type: str,
    created_by: str,
    at: datetime,
) -> dict[str, Any]:
    return {
        "id": journal_id(number, position),
        "incident_id": incident_id(number),
        "element": element,
        "value": value,
        "author_type": author_type,
        "created_by": created_by,
        "source": JournalSource.SEED,
        "process_status": JournalProcessStatus.PENDING,
        "attempts": 0,
        "available_at": at,
        "processed_at": None,
        "created_at": at,
    }


def _incident_rows(
    incident: IncidentSeed, tz: ZoneInfo, anchor: datetime
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    site = incident.site
    opened_at = anchor + parse_offset(incident.opened_at)
    resolved_at = anchor + parse_offset(incident.resolved_at) if incident.resolved_at else None
    eta = next_local_time(anchor, tz, incident.eta_local) if incident.eta_local else None
    row = {
        "id": incident_id(incident.number),
        "number": incident.number,
        "store_id": ids.store_id(site),
        "short_description": incident.short_description,
        "description": incident.description,
        "u_symptom": incident.u_symptom,
        "business_service": incident.business_service,
        "category": str(incident.category),
        "subcategory": incident.subcategory,
        "cmdb_ci_network_element_id": (
            ids.network_element_id(site, incident.ci_element) if incident.ci_element else None
        ),
        "cmdb_ci_device_id": (
            ids.device_id(site, incident.ci_device) if incident.ci_device else None
        ),
        "contact_type": str(incident.contact_type),
        "caller": incident.caller,
        "opened_by": incident.opened_by,
        "impact": incident.impact,
        "urgency": incident.urgency,
        "state": incident.state,
        "hold_reason": str(incident.hold_reason) if incident.hold_reason else None,
        "assignment_group_id": ids.assignment_group_id(incident.assignment_group),
        "assigned_to": incident.assigned_to,
        "u_resolution_eta": eta,
        "u_eta_note": incident.eta_note,
        "opened_at": opened_at,
        "resolved_at": resolved_at,
        "closed_at": None,
        "close_code": incident.close_code,
        "close_notes": incident.close_notes,
        "created_at": opened_at,
    }

    journal = [
        _journal_row(
            incident.number,
            0,
            JournalElement.SYSTEM,
            CREATED_ENTRY_TEXT,
            ActorType.SYSTEM,
            incident.opened_by,
            opened_at,
        )
    ]
    for position, entry in enumerate(incident.journal, start=1):
        journal.append(
            _journal_row(
                incident.number,
                position,
                entry.element,
                entry.value,
                entry.author_type,
                entry.created_by,
                anchor + parse_offset(entry.at),
            )
        )
    if resolved_at is not None and incident.close_notes:
        journal.append(
            _journal_row(
                incident.number,
                len(journal),
                JournalElement.WORK_NOTES,
                incident.close_notes,
                ActorType.IT_STAFF,
                incident.assigned_to or "it-staff",
                resolved_at,
            )
        )
    return row, journal


def _appointment_row(
    appointment: AppointmentSeed, incident: IncidentSeed, tz: ZoneInfo, anchor: datetime
) -> dict[str, Any]:
    start = next_local_time(anchor, tz, appointment.at_local, appointment.day)
    return {
        "id": appointment_id(appointment.key),
        "store_id": ids.store_id(incident.site),
        "incident_id": incident_id(incident.number),
        "appointment_type": str(appointment.appointment_type),
        "title": appointment.title,
        "purpose": appointment.purpose,
        "details": appointment.details,
        "scheduled_start": start,
        "scheduled_end": start + timedelta(minutes=appointment.duration_minutes),
        "status": AppointmentStatus.SCHEDULED,
        "status_note": None,
        "vendor_name": appointment.vendor_name,
        "technician_name": appointment.technician_name,
        "vendor_reference": None,
        "created_by": SEED_CREATED_BY,
        "source": "seed",
    }


def _appointment_notice(
    appointment: AppointmentSeed,
    row: dict[str, Any],
    incident: IncidentSeed,
    tz: ZoneInfo,
    anchor: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    booked = anchor + parse_offset(appointment.booked_at)
    if booked < anchor + parse_offset(incident.opened_at):
        raise SeedDataError(
            f"appointments.yaml: {appointment.key}: booked_at is before its incident opened"
        )
    content = appointment_scheduled(
        title=row["title"],
        purpose=row["purpose"],
        details=row["details"],
        start=row["scheduled_start"],
        end=row["scheduled_end"],
        incident_number=incident.number,
        tz=tz,
        publish_at=booked,
    )
    notification_id = appointment_notification_id(appointment.key)
    notification = {
        "id": notification_id,
        "category": content.category,
        "title": content.title,
        "description": content.description,
        "link_url": None,
        "source": NotificationSource.SYSTEM,
        "system_event": SystemEvent.APPOINTMENT_SCHEDULED,
        "created_by": SEED_CREATED_BY,
        "incident_id": row["incident_id"],
        "appointment_id": row["id"],
        "action_id": None,
        "source_journal_id": None,
        "status": NotificationStatus.PUBLISHED,
        "publish_at": booked,
        "expires_at": content.expires_at,
        "withdrawn_at": None,
        "withdrawn_by": None,
        "created_at": booked,
    }
    target = {
        "notification_id": notification_id,
        "store_id": row["store_id"],
        "delivered_at": None,
        "read_at": None,
        "read_by": None,
        "created_at": booked,
    }
    return notification, target


def build_itsm(data: SeedData, anchor: datetime) -> ItsmRows:
    zones = {s.site_code: ZoneInfo(s.timezone) for s in data.stores.stores}
    by_number = {i.number: i for i in data.incidents.incidents}
    rows = ItsmRows()
    for incident in data.incidents.incidents:
        row, journal = _incident_rows(incident, zones[incident.site], anchor)
        rows.incidents.append(row)
        rows.journal.extend(journal)
    for appointment in data.appointments.appointments:
        incident = by_number[appointment.incident]
        tz = zones[incident.site]
        row = _appointment_row(appointment, incident, tz, anchor)
        rows.appointments.append(row)
        notification, target = _appointment_notice(appointment, row, incident, tz, anchor)
        rows.notifications.append(notification)
        rows.notification_targets.append(target)
    return rows


async def clear_itsm(session: AsyncSession) -> None:
    """Delete ITSM data and everything derived from it, in foreign-key order.

    Portal notifications that are not linked to an incident, an appointment, an
    action or a journal entry are kept.
    """
    linked = select(Notification.id).where(
        or_(
            Notification.incident_id.is_not(None),
            Notification.appointment_id.is_not(None),
            Notification.action_id.is_not(None),
            Notification.source_journal_id.is_not(None),
        )
    )
    await session.execute(
        delete(NotificationTarget).where(NotificationTarget.notification_id.in_(linked))
    )
    await session.execute(delete(Notification).where(Notification.id.in_(linked)))
    for model in (
        StoreActionResponse,
        StoreAction,
        StoreAlert,
        AiWorkflowRun,
        Appointment,
        IncidentJournal,
        Incident,
    ):
        await session.execute(delete(model))


async def write_itsm(session: AsyncSession, rows: ItsmRows) -> None:
    await clear_itsm(session)
    await insert_many(session, Incident, rows.incidents)
    await insert_many(session, IncidentJournal, rows.journal)
    await insert_many(session, Appointment, rows.appointments)
    await insert_many(session, Notification, rows.notifications)
    await insert_many(session, NotificationTarget, rows.notification_targets)
