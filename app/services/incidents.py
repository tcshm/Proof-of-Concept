# app/services/incidents.py
"""The incident service: the ONLY code that writes incidents and journal entries.

Every method:
    1. locks the incident row (SELECT ... FOR UPDATE), so two people changing the
       same incident at the same time are handled one after the other,
    2. checks the rules and raises ConflictError / BusinessRuleError / NotFoundError,
    3. changes the rows, adds journal entries and audit rows,
    4. commits (commit=True, used by routes) or only flushes (commit=False, used by
       the seed and tests, which own the transaction).

Callers: the admin portal (Phase 13), the mobile action replies (Phase 10), the
agentic system later, and the tests. Times come from the caller ("now"), never
from datetime.now(), so seeds can backdate and tests can freeze time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import ActorContext
from app.core.errors import BusinessRuleError, ConflictError, NotFoundError
from app.models import (
    Appointment,
    AssignmentGroup,
    Device,
    Incident,
    IncidentJournal,
    NetworkElement,
    StoreAction,
    StoreAlert,
)
from app.models.values import (
    ACTIVE_INCIDENT_STATES,
    ActionCloseReason,
    ActionStatus,
    AlertCloseReason,
    AlertStatus,
    AppointmentStatus,
    AuditAction,
    ContactType,
    HoldReason,
    IncidentCategory,
    IncidentState,
    JournalElement,
    JournalProcessStatus,
    JournalSource,
)
from app.services.audit import diff, record_audit
from app.services.incident_rules import (
    FINAL_STATES,
    check_transition,
    journal_process_status,
    state_label,
)

INCIDENT_NUMBER_SEQUENCE = "incident_number_seq"
CREATED_ENTRY_TEXT = "Incident created."

# Fields an audit "create" row records, and fields update_incident may change.
_SNAPSHOT_FIELDS = (
    "number",
    "short_description",
    "category",
    "contact_type",
    "impact",
    "urgency",
    "state",
    "hold_reason",
    "assignment_group_id",
    "assigned_to",
    "u_resolution_eta",
    "u_eta_note",
    "cmdb_ci_network_element_id",
    "cmdb_ci_device_id",
)


class _Unset:
    """Marks an IncidentUpdate field that the caller did not set."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()


@dataclass(frozen=True)
class NewIncident:
    short_description: str
    category: str
    impact: int
    urgency: int
    contact_type: str
    description: str | None = None
    u_symptom: str | None = None
    business_service: str | None = None
    subcategory: str | None = None
    caller: str | None = None
    network_element_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    assignment_group_id: uuid.UUID | None = None
    assigned_to: str | None = None
    u_resolution_eta: datetime | None = None
    u_eta_note: str | None = None


@dataclass(frozen=True)
class IncidentUpdate:
    """Only the fields given (not UNSET) are changed. None clears a field."""

    state: Any = UNSET
    hold_reason: Any = UNSET
    impact: Any = UNSET
    urgency: Any = UNSET
    assignment_group_id: Any = UNSET
    assigned_to: Any = UNSET
    u_resolution_eta: Any = UNSET
    u_eta_note: Any = UNSET

    def given(self) -> dict[str, Any]:
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) is not UNSET
        }


# --- helpers -----------------------------------------------------------------------
def _snapshot(incident: Incident) -> dict[str, Any]:
    return {name: getattr(incident, name) for name in _SNAPSHOT_FIELDS}


def _journal_source(actor: ActorContext) -> str:
    try:
        return str(JournalSource(actor.source))
    except ValueError as exc:
        raise BusinessRuleError(
            f"Journal entries cannot be written from source {actor.source!r}."
        ) from exc


def _require_text(value: str | None, label: str) -> str:
    if value is None or not value.strip():
        raise BusinessRuleError(f"{label} is required.")
    return value.strip()


def _require_value(allowed: type[Enum], value: Any, label: str) -> str:
    try:
        return str(allowed(value))
    except ValueError as exc:
        choices = ", ".join(str(member.value) for member in allowed)
        raise BusinessRuleError(f"{label} must be one of: {choices}.") from exc


def _require_level(value: int, label: str) -> int:
    if value not in (1, 2, 3):
        raise BusinessRuleError(f"{label} must be 1, 2 or 3.")
    return value


async def _finish(session: AsyncSession, commit: bool) -> None:
    if commit:
        await session.commit()
    else:
        await session.flush()


async def next_incident_number(session: AsyncSession) -> str:
    value = await session.scalar(text(f"SELECT nextval('{INCIDENT_NUMBER_SEQUENCE}')"))
    return f"INC{value:08d}"


async def lock_incident(session: AsyncSession, incident_id: uuid.UUID) -> Incident:
    incident = await session.scalar(
        select(Incident)
        .where(Incident.id == incident_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if incident is None:
        raise NotFoundError(f"Incident {incident_id} was not found.")
    return incident


async def find_incident_id(
    session: AsyncSession, number: str, store_id: uuid.UUID | None = None
) -> uuid.UUID:
    """The id of an incident by number; with store_id, only within that store."""
    query = select(Incident.id).where(Incident.number == number)
    if store_id is not None:
        query = query.where(Incident.store_id == store_id)
    incident_id = await session.scalar(query)
    if incident_id is None:
        raise NotFoundError(f"Incident {number} was not found.")
    return incident_id


async def _check_ci(session: AsyncSession, store_id: uuid.UUID, data: NewIncident) -> None:
    if data.network_element_id is not None and data.device_id is not None:
        raise BusinessRuleError(
            "An incident has at most one main CI: a network element or a device."
        )
    if data.network_element_id is not None:
        found = await session.scalar(
            select(NetworkElement.id).where(
                NetworkElement.id == data.network_element_id, NetworkElement.store_id == store_id
            )
        )
        if found is None:
            raise NotFoundError("The network element was not found in this store.")
    if data.device_id is not None:
        found = await session.scalar(
            select(Device.id).where(Device.id == data.device_id, Device.store_id == store_id)
        )
        if found is None:
            raise NotFoundError("The device was not found in this store.")


async def _check_group(session: AsyncSession, group_id: uuid.UUID | None) -> None:
    if group_id is not None and await session.get(AssignmentGroup, group_id) is None:
        raise NotFoundError(f"Assignment group {group_id} was not found.")


def _add_entry(
    session: AsyncSession,
    actor: ActorContext,
    incident: Incident,
    element: str,
    value: str,
    now: datetime,
    author_type: str | None = None,
) -> IncidentJournal:
    author = author_type or str(actor.actor_type)
    status = journal_process_status(element, author)
    entry = IncidentJournal(
        id=uuid.uuid4(),
        incident_id=incident.id,
        element=element,
        value=value,
        author_type=author,
        created_by=actor.actor,
        source=_journal_source(actor),
        process_status=str(status),
        available_at=now,
        processed_at=now if status == JournalProcessStatus.SKIPPED else None,
        created_at=now,
    )
    session.add(entry)
    return entry


async def _close_alert(
    session: AsyncSession,
    actor: ActorContext,
    incident: Incident,
    reason: AlertCloseReason,
    now: datetime,
) -> None:
    alert_ids = (
        await session.scalars(
            update(StoreAlert)
            .where(StoreAlert.incident_id == incident.id, StoreAlert.status == AlertStatus.ACTIVE)
            .values(status=AlertStatus.CLOSED, close_reason=reason, closed_at=now)
            .returning(StoreAlert.id)
        )
    ).all()
    for alert_id in alert_ids:
        record_audit(
            session,
            actor,
            action=AuditAction.STATUS_CHANGE,
            entity_type="store_alerts",
            entity_id=alert_id,
            store_id=incident.store_id,
            changes={"status": {"old": "active", "new": "closed"}, "close_reason": str(reason)},
        )


async def _close_actions(
    session: AsyncSession, actor: ActorContext, incident: Incident, now: datetime
) -> None:
    rows = (
        await session.execute(
            update(StoreAction)
            .where(
                StoreAction.incident_id == incident.id,
                StoreAction.status.in_((ActionStatus.OPEN, ActionStatus.RESPONDED)),
            )
            .values(
                status=ActionStatus.CLOSED,
                close_reason=ActionCloseReason.INCIDENT_CLOSED,
                closed_at=now,
            )
            .returning(StoreAction.id)
        )
    ).all()
    for (action_id,) in rows:
        record_audit(
            session,
            actor,
            action=AuditAction.STATUS_CHANGE,
            entity_type="store_actions",
            entity_id=action_id,
            store_id=incident.store_id,
            changes={"status": {"new": "closed"}, "close_reason": "incident_closed"},
        )


async def _cancel_appointments(
    session: AsyncSession, actor: ActorContext, incident: Incident, note: str
) -> None:
    rows = (
        await session.execute(
            update(Appointment)
            .where(
                Appointment.incident_id == incident.id,
                Appointment.status.in_(
                    (AppointmentStatus.SCHEDULED, AppointmentStatus.IN_PROGRESS)
                ),
            )
            .values(status=AppointmentStatus.CANCELLED, status_note=note)
            .returning(Appointment.id)
        )
    ).all()
    for (appointment_id,) in rows:
        record_audit(
            session,
            actor,
            action=AuditAction.STATUS_CHANGE,
            entity_type="appointments",
            entity_id=appointment_id,
            store_id=incident.store_id,
            changes={"status": {"new": "cancelled"}, "status_note": note},
        )
        # Phase 7: write the "appointment cancelled" store notification here.


# --- operations ----------------------------------------------------------------------
async def create_incident(
    session: AsyncSession,
    actor: ActorContext,
    store_id: uuid.UUID,
    data: NewIncident,
    now: datetime,
    *,
    commit: bool = True,
) -> Incident:
    """New incident in state 1, plus its pending "created" journal entry."""
    _require_text(data.short_description, "Short description")
    _require_value(IncidentCategory, data.category, "Category")
    _require_value(ContactType, data.contact_type, "Contact type")
    _require_level(data.impact, "Impact")
    _require_level(data.urgency, "Urgency")
    await _check_ci(session, store_id, data)
    await _check_group(session, data.assignment_group_id)

    incident = Incident(
        id=uuid.uuid4(),
        number=await next_incident_number(session),
        store_id=store_id,
        short_description=data.short_description.strip(),
        description=data.description,
        u_symptom=data.u_symptom,
        business_service=data.business_service,
        category=data.category,
        subcategory=data.subcategory,
        cmdb_ci_network_element_id=data.network_element_id,
        cmdb_ci_device_id=data.device_id,
        contact_type=data.contact_type,
        caller=data.caller,
        opened_by=actor.actor,
        impact=data.impact,
        urgency=data.urgency,
        state=IncidentState.NEW,
        assignment_group_id=data.assignment_group_id,
        assigned_to=data.assigned_to,
        u_resolution_eta=data.u_resolution_eta,
        u_eta_note=data.u_eta_note,
        opened_at=now,
    )
    session.add(incident)
    await session.flush()
    _add_entry(
        session,
        actor,
        incident,
        JournalElement.SYSTEM,
        CREATED_ENTRY_TEXT,
        now,
        author_type="system",
    )
    record_audit(
        session,
        actor,
        action=AuditAction.CREATE,
        entity_type="incidents",
        entity_id=incident.id,
        store_id=store_id,
        changes=diff({}, _snapshot(incident)),
    )
    await _finish(session, commit)
    return incident


async def add_journal_entry(
    session: AsyncSession,
    actor: ActorContext,
    incident_id: uuid.UUID,
    element: str,
    value: str,
    now: datetime,
    *,
    commit: bool = True,
) -> IncidentJournal:
    """A work note or a comment. IT staff entries are queued for the worker."""
    if element not in (JournalElement.WORK_NOTES, JournalElement.COMMENTS):
        raise BusinessRuleError("Only work notes and comments can be added.")
    text_value = _require_text(value, "The note")
    incident = await lock_incident(session, incident_id)
    if incident.state in FINAL_STATES:
        raise ConflictError(
            f"Incident {incident.number} is {state_label(incident.state)}; "
            "no further updates can be added."
        )
    entry = _add_entry(session, actor, incident, element, text_value, now)
    record_audit(
        session,
        actor,
        action=AuditAction.CREATE,
        entity_type="incident_journal",
        entity_id=entry.id,
        store_id=incident.store_id,
        changes={"incident": incident.number, "element": element},
    )
    await _finish(session, commit)
    return entry


async def update_incident(
    session: AsyncSession,
    actor: ActorContext,
    incident_id: uuid.UUID,
    changes: IncidentUpdate,
    now: datetime,
    *,
    commit: bool = True,
) -> Incident:
    """State (1-3), hold reason, impact, urgency, assignment and ETA.

    No journal entry: the mobile app shows these fields live. A hold reason is
    required when moving to On Hold and cleared automatically when leaving it.
    """
    given = changes.given()
    incident = await lock_incident(session, incident_id)
    if incident.state not in ACTIVE_INCIDENT_STATES:
        raise ConflictError(
            f"Incident {incident.number} is {state_label(incident.state)} and cannot be changed."
        )
    before = _snapshot(incident)

    target = given.get("state", incident.state)
    if target not in ACTIVE_INCIDENT_STATES:
        raise BusinessRuleError("Use resolve, close or cancel to finish an incident.")
    if target != incident.state:
        check_transition(incident.number, incident.state, target)

    hold_reason = given.get("hold_reason", incident.hold_reason)
    if target == IncidentState.ON_HOLD:
        if hold_reason is None:
            raise BusinessRuleError("A hold reason is required when putting an incident on hold.")
        _require_value(HoldReason, hold_reason, "Hold reason")
    else:
        if given.get("hold_reason") is not None:
            raise BusinessRuleError("A hold reason only applies to incidents on hold.")
        hold_reason = None

    for level in ("impact", "urgency"):
        if level in given:
            _require_level(given[level], level.capitalize())
    if "assignment_group_id" in given:
        await _check_group(session, given["assignment_group_id"])

    for name, value in given.items():
        setattr(incident, name, value)
    incident.state = target
    incident.hold_reason = hold_reason

    after = _snapshot(incident)
    changed = diff(before, after)
    if changed:
        action = AuditAction.STATUS_CHANGE if "state" in changed else AuditAction.UPDATE
        record_audit(
            session,
            actor,
            action=action,
            entity_type="incidents",
            entity_id=incident.id,
            store_id=incident.store_id,
            changes=changed,
        )
    await _finish(session, commit)
    return incident


async def resolve_incident(
    session: AsyncSession,
    actor: ActorContext,
    incident_id: uuid.UUID,
    close_code: str,
    close_notes: str,
    now: datetime,
    *,
    commit: bool = True,
) -> Incident:
    """State 6. Closes the store alert and writes the close notes as a work note
    (pending, so the worker can ask the store to confirm the fix). Open actions
    stay open."""
    code = _require_text(close_code, "Close code")
    notes = _require_text(close_notes, "Close notes")
    incident = await lock_incident(session, incident_id)
    check_transition(incident.number, incident.state, IncidentState.RESOLVED)
    before = _snapshot(incident)

    incident.state = IncidentState.RESOLVED
    incident.hold_reason = None
    incident.resolved_at = now
    incident.close_code = code
    incident.close_notes = notes

    await _close_alert(session, actor, incident, AlertCloseReason.INCIDENT_RESOLVED, now)
    _add_entry(session, actor, incident, JournalElement.WORK_NOTES, notes, now)
    record_audit(
        session,
        actor,
        action=AuditAction.STATUS_CHANGE,
        entity_type="incidents",
        entity_id=incident.id,
        store_id=incident.store_id,
        changes={**diff(before, _snapshot(incident)), "close_code": {"old": None, "new": code}},
    )
    await _finish(session, commit)
    return incident


async def close_incident(
    session: AsyncSession,
    actor: ActorContext,
    incident_id: uuid.UUID,
    now: datetime,
    *,
    commit: bool = True,
) -> Incident:
    """Resolved -> Closed. Closes any remaining alert, actions and appointments."""
    incident = await lock_incident(session, incident_id)
    check_transition(incident.number, incident.state, IncidentState.CLOSED)
    incident.state = IncidentState.CLOSED
    incident.closed_at = now
    await _close_alert(session, actor, incident, AlertCloseReason.INCIDENT_RESOLVED, now)
    await _close_actions(session, actor, incident, now)
    await _cancel_appointments(session, actor, incident, "Incident closed")
    record_audit(
        session,
        actor,
        action=AuditAction.STATUS_CHANGE,
        entity_type="incidents",
        entity_id=incident.id,
        store_id=incident.store_id,
        changes={"state": {"old": IncidentState.RESOLVED, "new": IncidentState.CLOSED}},
    )
    await _finish(session, commit)
    return incident


async def cancel_incident(
    session: AsyncSession,
    actor: ActorContext,
    incident_id: uuid.UUID,
    reason: str,
    now: datetime,
    *,
    commit: bool = True,
) -> Incident:
    """New / In Progress / On Hold -> Canceled. Closes the alert, actions and
    appointments."""
    note = _require_text(reason, "A cancellation reason")
    incident = await lock_incident(session, incident_id)
    old_state = incident.state
    check_transition(incident.number, incident.state, IncidentState.CANCELED)
    incident.state = IncidentState.CANCELED
    incident.hold_reason = None
    incident.closed_at = now
    incident.close_code = "Canceled"
    incident.close_notes = note
    await _close_alert(session, actor, incident, AlertCloseReason.INCIDENT_CANCELED, now)
    await _close_actions(session, actor, incident, now)
    await _cancel_appointments(session, actor, incident, "Incident cancelled")
    record_audit(
        session,
        actor,
        action=AuditAction.STATUS_CHANGE,
        entity_type="incidents",
        entity_id=incident.id,
        store_id=incident.store_id,
        changes={"state": {"old": old_state, "new": IncidentState.CANCELED}, "reason": note},
    )
    await _finish(session, commit)
    return incident
