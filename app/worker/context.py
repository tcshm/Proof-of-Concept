# app/worker/context.py
"""What the model sees for one journal entry.

The context is a plain dict, stored as ai_workflow_runs.input_context and sent
as {context_json}. It contains only what the model needs: incident text, the
main CI, the store's local time and hours, store-visible comments, upcoming
visits and open actions. Work notes are NEVER included except the entry being
classified (the trigger), and no staff names, phone numbers or emails.
Texts are shortened to keep requests small (free-tier quotas).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.timefmt import is_open, to_local, tz_abbreviation
from app.models import (
    Appointment,
    AssignmentGroup,
    Device,
    DeviceType,
    Incident,
    IncidentJournal,
    NetworkElement,
    NetworkElementType,
    Store,
    StoreAction,
)
from app.models.values import (
    ActionStatus,
    ActorType,
    AppointmentStatus,
    JournalElement,
)
from app.services.incident_rules import state_label
from app.worker.queue import ClaimedEntry
from app.worker.rules import Plan, Route, clip

MAX_TEXT = 600
MAX_COMMENTS = 5
MAX_VISITS = 5
_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True)
class IncidentInfo:
    id: uuid.UUID
    number: str
    store_id: uuid.UUID
    store_name: str
    timezone: ZoneInfo
    business_hours: dict[str, Any]
    state: int
    priority: int | None
    short_description: str
    description: str | None
    u_symptom: str | None
    category: str
    hold_reason: str | None
    u_resolution_eta: datetime | None
    u_eta_note: str | None
    resolved_at: datetime | None
    close_code: str | None
    close_notes: str | None
    group_name: str | None
    group_description: str | None
    network_element_id: uuid.UUID | None
    device_id: uuid.UUID | None


def local_text(value: datetime, tz: ZoneInfo) -> str:
    """'2026-10-13 13:00 IST'"""
    return f"{to_local(value, tz).strftime('%Y-%m-%d %H:%M')} {tz_abbreviation(value, tz)}"


def _short(value: str | None) -> str | None:
    return clip(value, MAX_TEXT) if value else None


async def load_incident(session: AsyncSession, incident_id: uuid.UUID) -> IncidentInfo:
    row = (
        await session.execute(
            select(
                Incident.id,
                Incident.number,
                Incident.store_id,
                Store.name.label("store_name"),
                Store.timezone,
                Store.business_hours,
                Incident.state,
                Incident.priority,
                Incident.short_description,
                Incident.description,
                Incident.u_symptom,
                Incident.category,
                Incident.hold_reason,
                Incident.u_resolution_eta,
                Incident.u_eta_note,
                Incident.resolved_at,
                Incident.close_code,
                Incident.close_notes,
                AssignmentGroup.name.label("group_name"),
                AssignmentGroup.description.label("group_description"),
                Incident.cmdb_ci_network_element_id,
                Incident.cmdb_ci_device_id,
            )
            .join(Store, Store.id == Incident.store_id)
            .outerjoin(AssignmentGroup, AssignmentGroup.id == Incident.assignment_group_id)
            .where(Incident.id == incident_id)
        )
    ).one()
    return IncidentInfo(
        id=row.id,
        number=row.number,
        store_id=row.store_id,
        store_name=row.store_name,
        timezone=ZoneInfo(row.timezone),
        business_hours=row.business_hours or {},
        state=row.state,
        priority=row.priority,
        short_description=row.short_description,
        description=row.description,
        u_symptom=row.u_symptom,
        category=row.category,
        hold_reason=row.hold_reason,
        u_resolution_eta=row.u_resolution_eta,
        u_eta_note=row.u_eta_note,
        resolved_at=row.resolved_at,
        close_code=row.close_code,
        close_notes=row.close_notes,
        group_name=row.group_name,
        group_description=row.group_description,
        network_element_id=row.cmdb_ci_network_element_id,
        device_id=row.cmdb_ci_device_id,
    )


async def _main_ci(session: AsyncSession, info: IncidentInfo) -> dict[str, Any] | None:
    if info.network_element_id is not None:
        kind = aliased(NetworkElementType)
        row = (
            await session.execute(
                select(
                    NetworkElement.name,
                    NetworkElement.location_in_store,
                    kind.display_name,
                    kind.business_criticality,
                )
                .join(kind, kind.code == NetworkElement.element_type_code)
                .where(NetworkElement.id == info.network_element_id)
            )
        ).one_or_none()
        if row is not None:
            return {
                "kind": "network element",
                "type": row.display_name,
                "name": row.name,
                "location": row.location_in_store,
                "business_criticality": row.business_criticality,
            }
    if info.device_id is not None:
        kind = aliased(DeviceType)
        row = (
            await session.execute(
                select(
                    Device.name,
                    Device.location_in_store,
                    kind.display_name,
                    kind.business_criticality,
                )
                .join(kind, kind.code == Device.device_type_code)
                .where(Device.id == info.device_id)
            )
        ).one_or_none()
        if row is not None:
            return {
                "kind": "store device",
                "type": row.display_name,
                "name": row.name,
                "location": row.location_in_store,
                "business_criticality": row.business_criticality,
            }
    return None


def _store(info: IncidentInfo, now: datetime) -> dict[str, Any]:
    local = to_local(now, info.timezone)
    hours = info.business_hours.get(_DAY_KEYS[local.weekday()]) or []
    today = ", ".join(f"{start}-{end}" for start, end in hours) or "closed"
    return {
        "name": info.store_name,
        "timezone": info.timezone.key,
        "local_time_now": local_text(now, info.timezone),
        "today": f"{_DAY_NAMES[local.weekday()]} {local.date().isoformat()}",
        "opening_hours_today": today,
        "open_now": is_open(info.business_hours, info.timezone, now),
    }


async def _comments(
    session: AsyncSession, info: IncidentInfo, entry: ClaimedEntry
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(IncidentJournal.value, IncidentJournal.author_type, IncidentJournal.created_at)
            .where(
                IncidentJournal.incident_id == info.id,
                IncidentJournal.element == JournalElement.COMMENTS,
                IncidentJournal.author_type.in_((ActorType.IT_STAFF, ActorType.STORE_USER)),
                IncidentJournal.id != entry.id,
                IncidentJournal.created_at <= entry.created_at,
            )
            .order_by(IncidentJournal.created_at.desc())
            .limit(MAX_COMMENTS)
        )
    ).all()
    return [
        {
            "from": "IT team" if row.author_type == ActorType.IT_STAFF else "store",
            "text": clip(row.value, MAX_TEXT),
            "written_at_local": local_text(row.created_at, info.timezone),
        }
        for row in reversed(rows)
    ]


async def _visits(session: AsyncSession, info: IncidentInfo, now: datetime) -> list[dict]:
    rows = (
        await session.execute(
            select(Appointment.title, Appointment.purpose, Appointment.scheduled_start)
            .where(
                Appointment.incident_id == info.id,
                Appointment.status.in_(
                    (AppointmentStatus.SCHEDULED, AppointmentStatus.IN_PROGRESS)
                ),
                Appointment.scheduled_start >= now - timedelta(hours=2),
            )
            .order_by(Appointment.scheduled_start)
            .limit(MAX_VISITS)
        )
    ).all()
    return [
        {
            "title": row.title,
            "purpose": row.purpose,
            "starts_at_local": local_text(row.scheduled_start, info.timezone),
        }
        for row in rows
    ]


async def _open_actions(session: AsyncSession, info: IncidentInfo) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(StoreAction.title, StoreAction.expected_response, StoreAction.status)
            .where(
                StoreAction.incident_id == info.id,
                StoreAction.status.in_((ActionStatus.OPEN, ActionStatus.RESPONDED)),
            )
            .order_by(StoreAction.created_at)
        )
    ).all()
    return [
        {"title": row.title, "expected_response": row.expected_response, "status": row.status}
        for row in rows
    ]


async def build_context(
    session: AsyncSession,
    info: IncidentInfo,
    entry: ClaimedEntry,
    plan: Plan,
    now: datetime,
) -> dict[str, Any]:
    incident = {
        "number": info.number,
        "title": info.short_description,
        "description": _short(info.description),
        "symptom": info.u_symptom,
        "category": info.category,
        "priority": info.priority,
        "state": state_label(info.state),
        "on_hold_reason": info.hold_reason,
        "expected_fix_local": (
            local_text(info.u_resolution_eta, info.timezone) if info.u_resolution_eta else None
        ),
        "status_note": info.u_eta_note,
    }
    context: dict[str, Any] = {
        "incident": incident,
        "main_ci": await _main_ci(session, info),
        "store": _store(info, now),
        "assignment_group": {"name": info.group_name, "description": info.group_description},
        "recent_store_comments": await _comments(session, info, entry),
        "upcoming_visits": await _visits(session, info, now),
    }
    if plan.route == Route.CLASSIFY:
        if plan.is_resolution:
            kind = "resolution"
        elif entry.element == JournalElement.COMMENTS:
            kind = "comment"
        else:
            kind = "work_note"
        trigger: dict[str, Any] = {
            "type": kind,
            "text": clip(entry.value, 1500),
            "written_at_local": local_text(entry.created_at, info.timezone),
        }
        if plan.is_resolution:
            trigger["close_code"] = info.close_code
        context["trigger"] = trigger
        context["open_actions"] = await _open_actions(session, info)
    return context
