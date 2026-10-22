# app/worker/rules.py
"""Pure worker rules (no database, no network): easy to test.

    plan_entry          impact assessment, update classification, or skip
    task_for            the task the model gets: assess_impact, classify_work_note,
                        classify_comment or confirm_fix
    template_alert      the alert written when the LLM keeps failing
    template_fix_question  the yes/no question written when confirm_fix keeps failing
    parse_due_by_local  the model's local due time -> UTC (or None)
    retry_delay, is_rate_limited, clip

The checks applied to every model answer are in app/worker/guards.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from app.llm.types import LlmError, LlmUnavailable
from app.models.values import (
    ACTIVE_INCIDENT_STATES,
    ActorType,
    IncidentState,
    JournalElement,
    Severity,
)

MAX_TITLE = 80
MAX_DETAIL = 300
MAX_DUE_AHEAD = timedelta(days=3)


class Route(StrEnum):
    IMPACT = "impact_assessment"
    CLASSIFY = "update_classification"
    SKIP = "skip"


class Task(StrEnum):
    ASSESS_IMPACT = "assess_impact"
    CLASSIFY_WORK_NOTE = "classify_work_note"
    CLASSIFY_COMMENT = "classify_comment"
    CONFIRM_FIX = "confirm_fix"


@dataclass(frozen=True)
class Plan:
    route: Route
    reason: str = ""
    is_resolution: bool = False


@dataclass(frozen=True)
class AlertText:
    title: str
    detail: str
    severity: str


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def plan_entry(
    *,
    element: str,
    author_type: str,
    incident_state: int,
    resolved_at: datetime | None,
    entry_created_at: datetime,
) -> Plan:
    """Which workflow handles a pending journal entry, or why it is skipped (no LLM call)."""
    if element == JournalElement.SYSTEM:
        if incident_state in ACTIVE_INCIDENT_STATES:
            return Plan(Route.IMPACT)
        return Plan(Route.SKIP, "the incident is no longer active")
    if element not in (JournalElement.WORK_NOTES, JournalElement.COMMENTS):
        return Plan(Route.SKIP, f"unknown journal element {element!r}")
    if author_type != ActorType.IT_STAFF:
        return Plan(Route.SKIP, "not written by IT staff")
    if incident_state in (IncidentState.CLOSED, IncidentState.CANCELED):
        return Plan(Route.SKIP, "the incident is closed or cancelled")
    if incident_state == IncidentState.RESOLVED:
        if resolved_at is not None and entry_created_at < resolved_at:
            return Plan(Route.SKIP, "written before the incident was resolved")
        return Plan(Route.CLASSIFY, is_resolution=element == JournalElement.WORK_NOTES)
    return Plan(Route.CLASSIFY)


def task_for(plan: Plan, element: str) -> Task:
    if plan.route == Route.IMPACT:
        return Task.ASSESS_IMPACT
    if plan.is_resolution:
        return Task.CONFIRM_FIX
    if element == JournalElement.COMMENTS:
        return Task.CLASSIFY_COMMENT
    return Task.CLASSIFY_WORK_NOTE


def severity_from_priority(priority: int | None) -> str:
    if priority is not None and priority <= 2:
        return Severity.CRITICAL
    if priority == 3:
        return Severity.WARNING
    return Severity.INFO


def template_alert(
    *, short_description: str, symptom: str | None, priority: int | None
) -> AlertText:
    """Used when the LLM keeps failing, so a store never misses an outage."""
    parts = []
    if symptom and symptom.strip():
        parts.append(symptom.strip().rstrip(".") + ".")
    parts.append("The IT team is working on it. Check the incident in the app for updates.")
    return AlertText(
        title=clip(short_description, MAX_TITLE),
        detail=clip(" ".join(parts), MAX_DETAIL),
        severity=severity_from_priority(priority),
    )


def template_fix_question(short_description: str) -> tuple[str, str]:
    """(title, detail) of the yes/no question used when confirm_fix keeps failing."""
    title = clip(f'Is "{short_description}" fixed now?', MAX_TITLE)
    detail = "IT has resolved this incident. Please confirm that everything works again."
    return title, detail


def parse_due_by_local(value: Any, tz: ZoneInfo, now: datetime) -> datetime | None:
    """'2026-10-13T13:00' in store time -> UTC. Empty, unreadable, past or more than
    3 days ahead -> None (the action is kept without a due time)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    parsed = parsed.astimezone(UTC)
    if parsed <= now or parsed > now + MAX_DUE_AHEAD:
        return None
    return parsed


def retry_delay(attempts: int, delays: Sequence[timedelta]) -> timedelta:
    """Delay before the next attempt after `attempts` attempts (1-based)."""
    if not delays:
        return timedelta(minutes=1)
    return delays[min(max(attempts, 1), len(delays)) - 1]


def is_rate_limited(error: LlmError) -> bool:
    return isinstance(error, LlmUnavailable) and "HTTP 429" in error.message
