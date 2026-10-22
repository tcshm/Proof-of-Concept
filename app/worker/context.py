# app/worker/context.py
"""What the model sees for one journal entry.

The context is a plain dict, stored as ai_workflow_runs.input_context and sent
as {context_json}. assemble_context() builds it from plain parts, so the worker
and the evaluation scorecard (app/worker/evaluate.py) send exactly the same shape.

Keys: task, incident, main_ci, store, assignment_group, recent_store_comments,
and per task:
    assess_impact  allowed_severities (no visits, no ETA: alerts never mention times)
    classify_*     trigger, open_action, recently_answered_actions, upcoming_visits
Work notes are NEVER included except the entry being classified (the trigger).
No staff names, phone numbers or emails. Texts are shortened (free-tier quotas).
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
    StoreActionResponse,
)
from app.models.values import ActionStatus, ActorType, AppointmentStatus, JournalElement
from app.services.incident_rules import state_label
from app.worker.guards import OpenActionInfo, allowed_severities
from app.worker.queue import ClaimedEntry
from app.worker.rules import Task, clip

MAX_TEXT = 600
MAX_TRIGGER = 1500
MAX_COMMENTS = 5
MAX_VISITS = 5
RECENT_ANSWERS = timedelta(hours=24)
_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
TRIGGER_TYPES = {
    Task.CLASSIFY_WORK_NOTE: "work_note",
    Task.CLASSIFY_COMMENT: "comment",
    Task.CONFIRM_FIX: "resolution",
}


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


@dataclass(frozen=True)
class OpenAction:
    id: uuid.UUID
    title: str
    expected_response: str
    created_at: datetime

    def info(self) -> OpenActionInfo:
        return OpenActionInfo(title=self.title, created_at=self.created_at)


def local_text(value: datetime, tz: ZoneInfo) -> str:
    """'2026-10-13 13:00 IST'"""
    return f"{to_local(value, tz).strftime('%Y-%m-%d %H:%M')} {tz_abbreviation(value, tz)}"


def _short(value: str | None) -> str | None:
    return clip(value, MAX_TEXT) if value else None


# --- pure assembly -------------------------------------------------------------------------
def store_facts(
    name: str, tz: ZoneInfo, business_hours: dict[str, Any], now: datetime
) -> dict[str, Any]:
    local = to_local(now, tz)
    hours = business_hours.get(_DAY_KEYS[local.weekday()]) or []
    today = ", ".join(f"{start}-{end}" for start, end in hours) or "closed"
    return {
        "name": name,
        "timezone": tz.key,
        "local_time_now": local_text(now, tz),
        "today": f"{_DAY_NAMES[local.weekday()]} {local.date().isoformat()}",
        "opening_hours_today": today,
        "open_now": is_open(business_hours, tz, now),
    }


def assemble_context(
    *,
    task: Task | str,
    incident: dict[str, Any],
    main_ci: dict[str, Any] | None,
    store: dict[str, Any],
    assignment_group: dict[str, Any],
    comments: list[dict[str, Any]],
    visits: list[dict[str, Any]] | None = None,
    trigger: dict[str, Any] | None = None,
    open_action: dict[str, Any] | None = None,
    recent_answers: list[dict[str, Any]] | None = None,
    severities: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    task = Task(task)
    context: dict[str, Any] = {
        "task": task.value,
        "incident": incident,
        "main_ci": main_ci,
        "store": store,
        "assignment_group": assignment_group,
        "recent_store_comments": comments,
    }
    if task == Task.ASSESS_IMPACT:
        context["allowed_severities"] = list(severities or ())
        return context
    context["trigger"] = trigger
    context["open_action"] = open_action
    context["recently_answered_actions"] = recent_answers or []
    context["upcoming_visits"] = visits or []
    return context


def incident_facts(info: IncidentInfo, *, with_eta: bool) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "number": info.number,
        "title": info.short_description,
        "description": _short(info.description),
        "symptom": info.u_symptom,
        "category": info.category,
        "priority": info.priority,
        "state": state_label(info.state),
        "on_hold_reason": info.hold_reason,
    }
    if with_eta:
        facts["expected_fix_local"] = (
            local_text(info.u_resolution_eta, info.timezone) if info.u_resolution_eta else None
        )
        facts["status_note"] = info.u_eta_note
    return facts


# --- database loading ----------------------------------------------------------------------
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


async def load_open_action(session: AsyncSession, incident_id: uuid.UUID) -> OpenAction | None:
    """The incident's action still waiting for the store (at most one exists)."""
    row = (
        await session.execute(
            select(
                StoreAction.id,
                StoreAction.title,
                StoreAction.expected_response,
                StoreAction.created_at,
            )
            .where(StoreAction.incident_id == incident_id, StoreAction.status == ActionStatus.OPEN)
            .order_by(StoreAction.created_at)
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return None
    return OpenAction(row.id, row.title, row.expected_response, row.created_at)


async def _main_ci(session: AsyncSession, info: IncidentInfo) -> dict[str, Any] | None:
    if info.network_element_id is not None:
        kind = aliased(NetworkElementType)
        row = (
            await session.execute(
                select(
                    NetworkElement.location_in_store,
                    kind.display_name,
                    kind.business_criticality,
                )
                .join(kind, kind.code == NetworkElement.element_type_code)
                .where(NetworkElement.id == info.network_element_id)
            )
        ).one_or_none()
        if row is not None:
            where = f" ({row.location_in_store})" if row.location_in_store else ""
            return {
                "kind": "network element",
                "type": row.display_name,
                "store_friendly_name": f"{row.display_name}{where}",
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
                "store_friendly_name": row.name,
                "location": row.location_in_store,
                "business_criticality": row.business_criticality,
            }
    return None


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


async def _recent_answers(
    session: AsyncSession, info: IncidentInfo, now: datetime
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(
                StoreAction.title,
                StoreActionResponse.answer_yes_no,
                StoreActionResponse.response_text,
                StoreActionResponse.created_at,
            )
            .join(StoreActionResponse, StoreActionResponse.action_id == StoreAction.id)
            .where(
                StoreAction.incident_id == info.id,
                StoreActionResponse.created_at >= now - RECENT_ANSWERS,
            )
            .order_by(StoreActionResponse.created_at)
        )
    ).all()
    return [
        {
            "action": row.title,
            "answer_yes_no": row.answer_yes_no,
            "answer_text": clip(row.response_text, MAX_TEXT) if row.response_text else None,
            "answered_at_local": local_text(row.created_at, info.timezone),
        }
        for row in rows
    ]


async def build_context(
    session: AsyncSession,
    info: IncidentInfo,
    entry: ClaimedEntry,
    task: Task,
    now: datetime,
    open_action: OpenAction | None,
) -> dict[str, Any]:
    main_ci = await _main_ci(session, info)
    common = {
        "main_ci": main_ci,
        "store": store_facts(info.store_name, info.timezone, info.business_hours, now),
        "assignment_group": {"name": info.group_name, "description": info.group_description},
        "comments": await _comments(session, info, entry),
    }
    if task == Task.ASSESS_IMPACT:
        criticality = main_ci.get("business_criticality") if main_ci else None
        return assemble_context(
            task=task,
            incident=incident_facts(info, with_eta=False),
            severities=allowed_severities(info.priority, criticality),
            **common,
        )
    trigger: dict[str, Any] = {
        "type": TRIGGER_TYPES[task],
        "text": clip(entry.value, MAX_TRIGGER),
        "written_at_local": local_text(entry.created_at, info.timezone),
    }
    if task == Task.CONFIRM_FIX:
        trigger["close_code"] = info.close_code
    waiting = None
    if open_action is not None:
        waiting = {
            "title": open_action.title,
            "expected_response": open_action.expected_response,
            "waiting_since_local": local_text(open_action.created_at, info.timezone),
        }
    return assemble_context(
        task=task,
        incident=incident_facts(info, with_eta=True),
        trigger=trigger,
        open_action=waiting,
        recent_answers=await _recent_answers(session, info, now),
        visits=await _visits(session, info, now),
        **common,
    )
