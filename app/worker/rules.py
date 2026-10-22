# app/worker/rules.py
"""Pure worker rules (no database, no network): easy to test.

plan_entry            impact assessment, update classification, or skip
check_alert_output    the impact answer -> alert text (clipped, non-empty)
check_classification  the classification answer -> action / notify / none
template_alert        the alert written when the LLM keeps failing
parse_due_by_local    the model's local due time -> UTC (or None)
retry_delay, is_rate_limited
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from app.llm.types import LlmError, LlmInvalidOutput, LlmUnavailable
from app.models.values import (
    ACTIVE_INCIDENT_STATES,
    ActorType,
    ExpectedResponse,
    IncidentState,
    JournalElement,
    Severity,
)

MAX_TITLE = 80
MAX_DETAIL = 300
MAX_DUE_AHEAD = timedelta(days=3)
_EXPECTED = {e.value for e in ExpectedResponse}
_SEVERITIES = {s.value for s in Severity}


class Route(StrEnum):
    IMPACT = "impact_assessment"
    CLASSIFY = "update_classification"
    SKIP = "skip"


class Decision(StrEnum):
    ACTION = "action"
    NOTIFY = "notify"
    NONE = "none"


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


@dataclass(frozen=True)
class ClassificationPlan:
    decision: Decision
    title: str | None = None
    detail: str | None = None
    severity: str = Severity.WARNING
    expected_response: str | None = None
    due_by: datetime | None = None
    message: str | None = None  # notification text for "notify"


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


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


def severity_from_priority(priority: int | None) -> str:
    if priority is not None and priority <= 2:
        return Severity.CRITICAL
    if priority == 3:
        return Severity.WARNING
    return Severity.INFO


def _invalid(message: str, output: dict[str, Any]) -> LlmInvalidOutput:
    return LlmInvalidOutput(message, raw_text=json.dumps(output, ensure_ascii=False))


def check_alert_output(output: dict[str, Any]) -> AlertText:
    title = _text(output.get("title"))
    detail = _text(output.get("detail"))
    severity = output.get("severity")
    if not title or not detail:
        raise _invalid("the alert needs a title and a detail", output)
    if severity not in _SEVERITIES:
        raise _invalid(f"unknown severity {severity!r}", output)
    return AlertText(
        title=clip(title, MAX_TITLE), detail=clip(detail, MAX_DETAIL), severity=severity
    )


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


def check_classification(
    output: dict[str, Any],
    *,
    element: str,
    entry_text: str,
    is_resolution: bool,
    tz: ZoneInfo,
    now: datetime,
) -> ClassificationPlan:
    """Normalise the classification answer. Small problems are fixed in code;
    an action without title / detail / expected response is an invalid answer."""
    raw_decision = output.get("decision")
    if raw_decision not in {d.value for d in Decision}:
        raise _invalid(f"unknown decision {raw_decision!r}", output)
    decision = Decision(raw_decision)
    if decision == Decision.NONE and element == JournalElement.COMMENTS:
        decision = Decision.NOTIFY  # a comment is IT writing to the store
    severity = output.get("severity")
    if severity not in _SEVERITIES:
        severity = Severity.WARNING
    title = _text(output.get("title"))
    detail = _text(output.get("detail"))

    if decision == Decision.ACTION:
        expected = output.get("expected_response")
        if is_resolution:
            expected = ExpectedResponse.YES_NO
        if not title or not detail or expected not in _EXPECTED:
            raise _invalid("an action needs a title, a detail and an expected response", output)
        return ClassificationPlan(
            decision=decision,
            title=clip(title, MAX_TITLE),
            detail=clip(detail, MAX_DETAIL),
            severity=severity,
            expected_response=str(expected),
            due_by=parse_due_by_local(output.get("due_by_local"), tz, now),
        )
    if decision == Decision.NOTIFY:
        if element == JournalElement.COMMENTS:
            message = " ".join(entry_text.split())
        else:
            if not detail:
                raise _invalid("a notification needs a detail", output)
            message = clip(detail, MAX_DETAIL)
        return ClassificationPlan(decision=decision, severity=severity, message=message)
    return ClassificationPlan(decision=Decision.NONE, severity=severity)


def retry_delay(attempts: int, delays: Sequence[timedelta]) -> timedelta:
    """Delay before the next attempt after `attempts` attempts (1-based)."""
    if not delays:
        return timedelta(minutes=1)
    return delays[min(max(attempts, 1), len(delays)) - 1]


def is_rate_limited(error: LlmError) -> bool:
    return isinstance(error, LlmUnavailable) and "HTTP 429" in error.message
