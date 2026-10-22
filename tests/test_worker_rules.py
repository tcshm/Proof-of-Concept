# tests/test_worker_rules.py
"""Pure worker rules: routing, tasks, template texts, due times, retries. No database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.llm.types import LlmRejected, LlmUnavailable
from app.worker.rules import (
    Route,
    Task,
    clip,
    is_rate_limited,
    parse_due_by_local,
    plan_entry,
    retry_delay,
    severity_from_priority,
    task_for,
    template_alert,
    template_fix_question,
)

CHENNAI = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 13, 4, 30, tzinfo=UTC)  # 10:00 IST
RESOLVED = NOW - timedelta(hours=1)


def plan(element="work_notes", author="it_staff", state=2, resolved_at=None, created=NOW):
    return plan_entry(
        element=element,
        author_type=author,
        incident_state=state,
        resolved_at=resolved_at,
        entry_created_at=created,
    )


@pytest.mark.parametrize("state", [1, 2, 3])
def test_created_entry_of_active_incident_is_assessed(state: int) -> None:
    result = plan("system", "system", state)
    assert result.route == Route.IMPACT
    assert task_for(result, "system") == Task.ASSESS_IMPACT


@pytest.mark.parametrize("state", [6, 7, 8])
def test_created_entry_of_finished_incident_is_skipped(state: int) -> None:
    result = plan("system", "system", state, RESOLVED)
    assert (result.route, result.reason) == (Route.SKIP, "the incident is no longer active")


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"author": "store_user", "element": "comments"}, "not written by IT staff"),
        ({"author": "agent"}, "not written by IT staff"),
        ({"state": 7}, "the incident is closed or cancelled"),
        ({"state": 8, "element": "comments"}, "the incident is closed or cancelled"),
        ({"state": 6, "resolved_at": RESOLVED, "created": RESOLVED - timedelta(minutes=5)},
         "written before the incident was resolved"),
        ({"element": "attachment"}, "unknown journal element 'attachment'"),
    ],
)  # fmt: skip
def test_skips(kwargs: dict, reason: str) -> None:
    result = plan(**kwargs)
    assert (result.route, result.reason) == (Route.SKIP, reason)


def test_tasks() -> None:
    assert task_for(plan("work_notes"), "work_notes") == Task.CLASSIFY_WORK_NOTE
    assert task_for(plan("comments"), "comments") == Task.CLASSIFY_COMMENT
    resolution = plan("work_notes", state=6, resolved_at=RESOLVED, created=RESOLVED)
    assert resolution.is_resolution and task_for(resolution, "work_notes") == Task.CONFIRM_FIX
    later_comment = plan("comments", state=6, resolved_at=RESOLVED, created=RESOLVED)
    assert task_for(later_comment, "comments") == Task.CLASSIFY_COMMENT


@pytest.mark.parametrize(
    ("priority", "severity"),
    [(1, "critical"), (2, "critical"), (3, "warning"), (4, "info"), (None, "info")],
)
def test_severity_from_priority(priority, severity) -> None:
    assert severity_from_priority(priority) == severity


def test_template_texts() -> None:
    alert = template_alert(
        short_description="ISP - Primary Link", symptom="Retail SDWAN ATTLink Status.", priority=2
    )
    assert (alert.title, alert.severity) == ("ISP - Primary Link", "critical")
    assert alert.detail.startswith("Retail SDWAN ATTLink Status. The IT team is working on it.")
    title, detail = template_fix_question("Printer 02 - Not Printing")
    assert title == 'Is "Printer 02 - Not Printing" fixed now?'
    assert detail.startswith("IT has resolved this incident.")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-13T13:00", datetime(2026, 10, 13, 7, 30, tzinfo=UTC)),
        ("2026-10-13 13:00", datetime(2026, 10, 13, 7, 30, tzinfo=UTC)),
        ("", None),
        (None, None),
        ("tomorrow", None),
        ("2026-10-13T09:00", None),  # already past
        ("2026-10-20T09:00", None),  # more than 3 days ahead
    ],
)
def test_parse_due_by_local(value, expected) -> None:
    assert parse_due_by_local(value, CHENNAI, NOW) == expected


def test_retry_delay_and_rate_limit() -> None:
    delays = (timedelta(minutes=2), timedelta(minutes=10))
    assert [retry_delay(n, delays) for n in (1, 2, 3)] == [
        timedelta(minutes=2),
        timedelta(minutes=10),
        timedelta(minutes=10),
    ]
    assert retry_delay(1, ()) == timedelta(minutes=1)
    assert is_rate_limited(LlmUnavailable("HTTP 429: too many requests"))
    assert not is_rate_limited(LlmUnavailable("no answer within 120 s"))
    assert not is_rate_limited(LlmRejected("HTTP 429"))


def test_clip() -> None:
    assert clip("a\n b", 10) == "a b"
    assert clip("x" * 100, 80).endswith("\u2026")
