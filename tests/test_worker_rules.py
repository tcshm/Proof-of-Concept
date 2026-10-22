# tests/test_worker_rules.py
"""Pure worker rules: routing, answer checks, template alert, due times. No database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.llm.types import LlmInvalidOutput, LlmRejected, LlmUnavailable
from app.worker.rules import (
    Decision,
    Route,
    check_alert_output,
    check_classification,
    clip,
    is_rate_limited,
    parse_due_by_local,
    plan_entry,
    retry_delay,
    severity_from_priority,
    template_alert,
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


# --- routing ---------------------------------------------------------------------------
@pytest.mark.parametrize("state", [1, 2, 3])
def test_created_entry_of_active_incident_is_assessed(state: int) -> None:
    assert plan("system", "system", state).route == Route.IMPACT


@pytest.mark.parametrize("state", [6, 7, 8])
def test_created_entry_of_finished_incident_is_skipped(state: int) -> None:
    result = plan("system", "system", state, RESOLVED)
    assert (result.route, result.reason) == (Route.SKIP, "the incident is no longer active")


def test_notes_and_comments_of_open_incidents_are_classified() -> None:
    assert plan("work_notes").route == Route.CLASSIFY
    assert plan("comments", state=3).route == Route.CLASSIFY
    assert plan("work_notes").is_resolution is False


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


def test_resolution_note_is_classified_as_resolution() -> None:
    note = plan("work_notes", state=6, resolved_at=RESOLVED, created=RESOLVED)
    assert (note.route, note.is_resolution) == (Route.CLASSIFY, True)
    comment = plan("comments", state=6, resolved_at=RESOLVED, created=RESOLVED)
    assert (comment.route, comment.is_resolution) == (Route.CLASSIFY, False)


# --- alerts ----------------------------------------------------------------------------
def test_alert_output_is_clipped() -> None:
    alert = check_alert_output(
        {"severity": "warning", "title": "x" * 120, "detail": " Wi-Fi  down. ", "rationale": "r"}
    )
    assert (len(alert.title), alert.title[-1], alert.detail, alert.severity) == (
        80,
        "\u2026",
        "Wi-Fi down.",
        "warning",
    )


@pytest.mark.parametrize(
    "output",
    [
        {"severity": "warning", "title": " ", "detail": "d"},
        {"severity": "warning", "title": "t", "detail": ""},
        {"severity": "urgent", "title": "t", "detail": "d"},
    ],
)
def test_bad_alert_output(output: dict) -> None:
    with pytest.raises(LlmInvalidOutput):
        check_alert_output(output)


@pytest.mark.parametrize(
    ("priority", "severity"),
    [(1, "critical"), (2, "critical"), (3, "warning"), (4, "info"), (None, "info")],
)
def test_severity_from_priority(priority, severity) -> None:
    assert severity_from_priority(priority) == severity


def test_template_alert() -> None:
    alert = template_alert(
        short_description="ISP - Primary Link",
        symptom="Retail SDWAN ATTLink Status.",
        priority=2,
    )
    assert alert.title == "ISP - Primary Link"
    assert alert.detail == (
        "Retail SDWAN ATTLink Status. The IT team is working on it. "
        "Check the incident in the app for updates."
    )
    assert alert.severity == "critical"
    assert template_alert(short_description="x", symptom=None, priority=5).detail.startswith(
        "The IT team"
    )


# --- classification --------------------------------------------------------------------
def answer(**values) -> dict:
    base = {
        "decision": "action",
        "title": "Provide comms room access at 1 PM",
        "detail": "The AT&T engineer needs access to the comms room.",
        "severity": "warning",
        "expected_response": "acknowledge",
        "due_by_local": "2026-10-13T13:00",
        "rationale": "The store must act.",
    }
    base.update(values)
    return base


def classify(output: dict, element="work_notes", resolution=False, text="IT note"):
    return check_classification(
        output, element=element, entry_text=text, is_resolution=resolution, tz=CHENNAI, now=NOW
    )


def test_action() -> None:
    result = classify(answer())
    assert result.decision == Decision.ACTION
    assert (result.title, result.expected_response, result.severity) == (
        "Provide comms room access at 1 PM",
        "acknowledge",
        "warning",
    )
    assert result.due_by == datetime(2026, 10, 13, 7, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "changes",
    [{"title": ""}, {"detail": " "}, {"expected_response": "not_applicable"}],
)
def test_incomplete_action_is_invalid(changes: dict) -> None:
    with pytest.raises(LlmInvalidOutput, match="an action needs"):
        classify(answer(**changes))


def test_resolution_action_is_always_yes_no() -> None:
    result = classify(answer(expected_response="acknowledge"), resolution=True)
    assert result.expected_response == "yes_no"
    fixed = classify(answer(expected_response="not_applicable"), resolution=True)
    assert fixed.expected_response == "yes_no"


def test_notify_from_work_note_uses_the_model_text() -> None:
    result = classify(answer(decision="notify", detail="An engineer visits today."))
    assert (result.decision, result.message) == (Decision.NOTIFY, "An engineer visits today.")
    with pytest.raises(LlmInvalidOutput, match="a notification needs a detail"):
        classify(answer(decision="notify", detail=""))


def test_comment_is_sent_as_written_and_never_none() -> None:
    comment = "Primary link is down,\n store is on backup."
    for decision in ("notify", "none"):
        result = classify(answer(decision=decision, detail=""), element="comments", text=comment)
        assert (result.decision, result.message) == (
            Decision.NOTIFY,
            "Primary link is down, store is on backup.",
        )


def test_none_and_unknown_decision() -> None:
    assert classify(answer(decision="none", severity="bad")).decision == Decision.NONE
    assert classify(answer(decision="none", severity="bad")).severity == "warning"
    with pytest.raises(LlmInvalidOutput, match="unknown decision"):
        classify(answer(decision="escalate"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-13T13:00", datetime(2026, 10, 13, 7, 30, tzinfo=UTC)),
        ("2026-10-13 13:00", datetime(2026, 10, 13, 7, 30, tzinfo=UTC)),
        ("2026-10-13T08:00:00Z", datetime(2026, 10, 13, 8, 0, tzinfo=UTC)),
        ("", None),
        (None, None),
        ("tomorrow", None),
        ("2026-10-13T09:00", None),  # already past (09:00 IST < 10:00 IST)
        ("2026-10-20T09:00", None),  # more than 3 days ahead
    ],
)
def test_parse_due_by_local(value, expected) -> None:
    assert parse_due_by_local(value, CHENNAI, NOW) == expected


# --- retries ----------------------------------------------------------------------------
def test_retry_delay_and_rate_limit() -> None:
    delays = (timedelta(minutes=2), timedelta(minutes=10))
    assert [retry_delay(n, delays) for n in (1, 2, 3, 9)] == [
        timedelta(minutes=2),
        timedelta(minutes=10),
        timedelta(minutes=10),
        timedelta(minutes=10),
    ]
    assert retry_delay(1, ()) == timedelta(minutes=1)
    assert is_rate_limited(LlmUnavailable("HTTP 429: too many requests"))
    assert not is_rate_limited(LlmUnavailable("no answer within 120 s"))
    assert not is_rate_limited(LlmRejected("HTTP 429"))


def test_clip() -> None:
    assert clip("a\n b", 10) == "a b"
