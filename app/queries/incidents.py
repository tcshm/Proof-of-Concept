# app/queries/incidents.py
"""Data for GET /api/v1/incidents and GET /api/v1/incidents/{number}.

One query returns everything the list needs: the incident, its assignment
group, the type of its main CI and its summary (the latest comment written by
IT staff, so a store manager's reply never becomes the summary).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import AssignmentGroup, Device, Incident, IncidentJournal, NetworkElement
from app.models.values import ACTIVE_INCIDENT_STATES, ActorType, JournalElement

_STORE_VISIBLE_AUTHORS = (ActorType.IT_STAFF, ActorType.STORE_USER)


@dataclass(frozen=True)
class IncidentRow:
    id: uuid.UUID
    number: str
    short_description: str
    u_symptom: str | None
    business_service: str | None
    description: str | None
    latest_it_comment: str | None
    ci_type: str | None
    ci_kind: str | None
    priority: int
    state: int
    hold_reason: str | None
    contact_type: str
    opened_at: datetime
    u_resolution_eta: datetime | None
    u_eta_note: str | None
    group_name: str | None
    assigned_to: str | None


@dataclass(frozen=True)
class CommentRow:
    id: uuid.UUID
    value: str
    author_type: str
    created_by: str
    created_at: datetime


def _incident_select() -> Select:
    element = aliased(NetworkElement)
    device = aliased(Device)
    latest_it_comment = (
        select(IncidentJournal.value)
        .where(
            IncidentJournal.incident_id == Incident.id,
            IncidentJournal.element == JournalElement.COMMENTS,
            IncidentJournal.author_type == ActorType.IT_STAFF,
        )
        .order_by(IncidentJournal.created_at.desc())
        .limit(1)
        .correlate(Incident)
        .scalar_subquery()
    )
    ci_kind = case(
        (Incident.cmdb_ci_network_element_id.is_not(None), "network_element"),
        (Incident.cmdb_ci_device_id.is_not(None), "device"),
        else_=None,
    )
    return (
        select(
            Incident.id,
            Incident.number,
            Incident.short_description,
            Incident.u_symptom,
            Incident.business_service,
            Incident.description,
            latest_it_comment.label("latest_it_comment"),
            func.coalesce(element.element_type_code, device.device_type_code).label("ci_type"),
            ci_kind.label("ci_kind"),
            Incident.priority,
            Incident.state,
            Incident.hold_reason,
            Incident.contact_type,
            Incident.opened_at,
            Incident.u_resolution_eta,
            Incident.u_eta_note,
            AssignmentGroup.name.label("group_name"),
            Incident.assigned_to,
        )
        .outerjoin(AssignmentGroup, AssignmentGroup.id == Incident.assignment_group_id)
        .outerjoin(element, element.id == Incident.cmdb_ci_network_element_id)
        .outerjoin(device, device.id == Incident.cmdb_ci_device_id)
    )


def _to_row(row) -> IncidentRow:
    return IncidentRow(**row._asdict())


async def active_incidents(session: AsyncSession, store_id: uuid.UUID) -> list[IncidentRow]:
    """Open incidents (states 1-3) of one store: highest priority first, then newest."""
    query = (
        _incident_select()
        .where(Incident.store_id == store_id, Incident.state.in_(ACTIVE_INCIDENT_STATES))
        .order_by(Incident.priority, Incident.opened_at.desc())
    )
    return [_to_row(row) for row in (await session.execute(query)).all()]


async def incident_detail(
    session: AsyncSession, store_id: uuid.UUID, number: str
) -> tuple[IncidentRow, list[CommentRow]] | None:
    """One incident of the store (any state) and its store-visible comments, newest
    first. Work notes and system entries are never returned."""
    row = (
        await session.execute(
            _incident_select().where(Incident.store_id == store_id, Incident.number == number)
        )
    ).one_or_none()
    if row is None:
        return None
    incident = _to_row(row)
    comments = (
        await session.execute(
            select(
                IncidentJournal.id,
                IncidentJournal.value,
                IncidentJournal.author_type,
                IncidentJournal.created_by,
                IncidentJournal.created_at,
            )
            .where(
                IncidentJournal.incident_id == incident.id,
                IncidentJournal.element == JournalElement.COMMENTS,
                IncidentJournal.author_type.in_(_STORE_VISIBLE_AUTHORS),
            )
            .order_by(IncidentJournal.created_at.desc())
        )
    ).all()
    return incident, [CommentRow(**c._asdict()) for c in comments]
