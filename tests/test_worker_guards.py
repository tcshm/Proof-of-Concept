# tests/test_worker_guards.py
"""The guards applied to every model answer. No database, no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.llm.types import LlmInvalidOutput
from app.worker.guards import (
    OpenActionInfo,
    allowed_severities,
    answer_of,
    check_alert,
    check_classification,
    find_internal_detail,
    has_clock_time,
    is_store_request,
    quote_in_text,
    request_problem,
)
from app.worker.rules import Task

CHENNAI = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 13, 4, 30, tzinfo=UTC)  # 10:00 IST
ACCESS = "AT&T engineer onsite at 1 PM today. Store must provide access to the comms room."


# --- request barrier ------------------------------------------------------------------------
@pytest.mark.parametrize(
    "quote",
    [
        "Store must provide access to the comms room",
        "Store manager to check Register 04 power and network cable",
        "Please restart the printer and let us know",
        "could you confirm the terminal shows the login screen",
        "staff should keep the stockroom door unlocked",
    ],
)
def test_requests(quote: str) -> None:
    assert is_store_request(quote)


@pytest.mark.parametrize(
    "quote",
    [
        "Trying to reach store to check the device status",
        "Please use Printer 01 or 03 meanwhile",
        "Store traffic failed over to WAN2",
        "Checked switch logs and port counters",
        "third-party vendor to replace the AP at 2 PM",
        "Our network team will confirm the fix",
        "",
    ],
)
def test_not_requests(quote: str) -> None:
    assert not is_store_request(quote)


def test_quote_matching_ignores_case_and_punctuation() -> None:
    assert quote_in_text("store MUST provide access to the comms-room", ACCESS)
    assert not quote_in_text("store must give access to the comms room", ACCESS)  # reworded
    assert not quote_in_text("", ACCESS)


@pytest.mark.parametrize(
    ("quote", "problem"),
    [
        (None, "no store_request_quote"),
        ("Store must", "store_request_quote is too short"),
        ("Store must give access to the comms room", "store_request_quote is not in the update"),
        ("AT&T engineer onsite at 1 PM", "store_request_quote does not ask the store"),
        ("Store must provide access to the comms room", None),
    ],
)
def test_request_problem(quote, problem) -> None:
    result = request_problem(quote, ACCESS)
    assert result == problem if problem is None else result.startswith(problem)


# --- internal details -----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Gateway 10.40.0.1 is down", "an IP address"),
        ("MAC 02:1a:2b:3c:4d:5e", "a MAC address"),
        ("Mail network-ops@ric.example", "an email address"),
        ("Ticket ATT-778812 raised", "a ticket or circuit id"),
        ("Assigned to r.kumar", "a staff user name"),
        ("AP01234N004 is down", "a device code or serial number"),
        ("Serial CN01234AP004", "a device code or serial number"),
        ("WAN01234-PRI down", "a device code or serial number"),
    ],
)
def test_internal_details_found(text: str, kind: str) -> None:
    assert find_internal_detail(text) == kind


@pytest.mark.parametrize(
    "text",
    [
        "POS Terminal 03 at Checkout 3 is offline.",
        "Incident INC00001234 updated by IT.",
        "Use the Wi-Fi in the checkout area; AP-004 is being replaced.",
        "Register 04 cable was loose.",
    ],
)
def test_normal_text_is_allowed(text: str) -> None:
    assert find_internal_detail(text) is None


def test_clock_times() -> None:
    assert has_clock_time("Engineer at 1 PM")
    assert has_clock_time("visit 13:00")
    assert has_clock_time("on 2026-10-13")
    assert not has_clock_time("IT is working on it today")


# --- severity range -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("priority", "criticality", "allowed"),
    [
        (1, "high", ("warning", "critical")),
        (2, "medium", ("warning", "critical")),
        (2, "low", ("warning",)),
        (3, "high", ("info", "warning", "critical")),
        (3, "medium", ("info", "warning")),
        (5, "low", ("info", "warning")),
        (None, None, ("info", "warning")),
    ],
)
def test_allowed_severities(priority, criticality, allowed) -> None:
    assert allowed_severities(priority, criticality) == allowed


def alert(**values) -> dict:
    base = {"affected": ["store_network"], "severity": "warning",
            "title": "Primary internet link down", "detail": "Payments still work.",
            "rationale": "r"}  # fmt: skip
    base.update(values)
    return base


def test_alert_is_clamped_and_cleaned() -> None:
    decision = check_alert(alert(severity="info", title="x" * 120), ("warning", "critical"))
    assert decision.alert.severity == "warning" and len(decision.alert.title) == 80
    assert decision.adjustments == ["severity info -> warning: allowed warning, critical"]
    assert check_alert(alert(), ("warning", "critical")).adjustments == []


def test_no_visible_impact_is_info() -> None:
    decision = check_alert(alert(affected=["no_visible_impact"]), ("info", "warning"))
    assert decision.alert.severity == "info"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"title": " "}, "needs a title and a detail"),
        ({"severity": "urgent"}, "unknown severity"),
        ({"detail": "Engineer visit at 1 PM."}, "mentions a time"),
        ({"detail": "AP01234N004 is down."}, "device code"),
    ],
)
def test_bad_alerts(changes: dict, message: str) -> None:
    with pytest.raises(LlmInvalidOutput, match=message):
        check_alert(alert(**changes), ("warning", "critical"))


# --- classification ---------------------------------------------------------------------------
def answer(**values) -> dict:
    base = {
        "decision": "action",
        "store_request_quote": "Store must provide access to the comms room",
        "title": "Provide comms room access at 1 PM",
        "detail": "The AT&T engineer needs access to the comms room at 1 PM.",
        "severity": "warning",
        "expected_response": "acknowledge",
        "due_time_quote": "onsite at 1 PM today",
        "due_by_local": "2026-10-13T13:00",
        "rationale": "Gate 1.",
    }
    base.update(values)
    return base


def classify(output, task=Task.CLASSIFY_WORK_NOTE, text=ACCESS, open_action=None, resolved=None):
    return check_classification(
        output, task=task, entry_text=text, tz=CHENNAI, now=NOW, open_action=open_action,
        resolved_at=resolved,
    )  # fmt: skip


def test_grounded_action() -> None:
    result = classify(answer())
    assert (result.decision, result.expected_response, result.adjustments) == (
        "action",
        "acknowledge",
        [],
    )
    assert result.due_by == datetime(2026, 10, 13, 7, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "quote",
    ["", "Store must give access to the comms room", "AT&T engineer onsite at 1 PM"],
)
def test_ungrounded_action_becomes_notify(quote: str) -> None:
    result = classify(answer(store_request_quote=quote))
    assert result.decision == "notify"
    assert result.message == "The AT&T engineer needs access to the comms room at 1 PM."
    assert result.adjustments[0].startswith("request barrier:")


def test_open_action_holds_a_new_request_back() -> None:
    waiting = OpenActionInfo("Check Register 04 cable", NOW - timedelta(hours=2))
    result = classify(answer(), open_action=waiting)
    assert (result.decision, result.held_back) == ("notify", True)
    assert "is still waiting" in result.adjustments[0]


def test_due_time_needs_a_quoted_clock_time() -> None:
    no_quote = classify(answer(due_time_quote=""))
    assert no_quote.due_by is None and no_quote.adjustments[0].startswith("due time dropped")
    no_time = classify(answer(due_time_quote="onsite"))
    assert no_time.due_by is None
    past = classify(answer(due_by_local="2026-10-13T09:00"))
    assert past.due_by is None and "not between now" in past.adjustments[0]
    none_given = classify(answer(due_by_local="", due_time_quote=""))
    assert none_given.due_by is None and none_given.adjustments == []


def test_comment_is_never_none_and_sent_as_written() -> None:
    comment = "Register 04 is offline.\n Trying to reach store to check the device status."
    for decision in ("none", "notify", "action"):
        result = classify(
            answer(decision=decision, store_request_quote="Trying to reach store to check"),
            task=Task.CLASSIFY_COMMENT,
            text=comment,
        )
        assert result.decision == "notify"
        assert result.message == (
            "Register 04 is offline. Trying to reach store to check the device status."
        )


def test_confirm_fix_is_always_a_yes_no_action() -> None:
    result = classify(
        answer(decision="notify", expected_response="not_applicable", store_request_quote="",
               title="Is the Wi-Fi working again?", detail="Please check the sales floor Wi-Fi."),
        task=Task.CONFIRM_FIX, text="Replaced the AP.",
    )  # fmt: skip
    assert (result.decision, result.expected_response, result.due_by) == ("action", "yes_no", None)
    assert result.adjustments[0].startswith("confirm_fix: notify -> action")


def test_confirm_fix_replaces_an_older_open_action() -> None:
    resolved = NOW - timedelta(minutes=10)
    older = OpenActionInfo("Provide comms room access", NOW - timedelta(hours=2))
    result = classify(answer(), task=Task.CONFIRM_FIX, open_action=older, resolved=resolved)
    assert (result.decision, result.replace_open_action) == ("action", True)
    already = OpenActionInfo("Is the Wi-Fi working again?", NOW - timedelta(minutes=5))
    again = classify(answer(), task=Task.CONFIRM_FIX, open_action=already, resolved=resolved)
    assert (again.decision, again.held_back) == ("notify", True)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"decision": "escalate"}, "unknown decision"),
        ({"title": ""}, "needs a title and a detail"),
        ({"expected_response": "not_applicable"}, "needs expected_response"),
        ({"detail": "Call r.kumar about it."}, "staff user name"),
        ({"decision": "notify", "detail": ""}, "notification needs a detail"),
        ({"decision": "notify", "detail": "Ticket ATT-778812 raised."}, "ticket"),
    ],
)
def test_invalid_answers(changes: dict, message: str) -> None:
    with pytest.raises(LlmInvalidOutput, match=message):
        classify(answer(**changes))


def test_none_and_applied() -> None:
    result = classify(answer(decision="none", severity="bad"))
    assert (result.decision, result.severity) == ("none", "warning")
    applied = classify(answer()).applied()
    assert applied["decision"] == "action" and applied["due_by"] == "2026-10-13T07:30:00+00:00"


def test_answer_of() -> None:
    assert answer_of({"answer": {"decision": "none"}, "applied": {}}) == {"decision": "none"}
    assert answer_of({"decision": "notify"}) == {"decision": "notify"}
    assert answer_of(None) == {}
