# tests/test_incident_rules.py
"""Incident state rules, journal queue rules and display mapping. No database."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.api.mappers import incidents as m
from app.core.errors import ConflictError
from app.services.incident_rules import can_move, check_transition, journal_process_status

ALL_STATES = (1, 2, 3, 6, 7, 8)
ALLOWED = {
    (1, 2), (1, 3), (1, 6), (1, 8),
    (2, 3), (2, 6), (2, 8),
    (3, 2), (3, 6), (3, 8),
    (6, 7),
}  # fmt: skip


@pytest.mark.parametrize("current", ALL_STATES)
@pytest.mark.parametrize("target", ALL_STATES)
def test_state_transitions(current: int, target: int) -> None:
    assert can_move(current, target) is ((current, target) in ALLOWED)


def test_forbidden_transition_message() -> None:
    with pytest.raises(
        ConflictError, match="INC00001234 is Resolved and cannot move to In Progress"
    ):
        check_transition("INC00001234", 6, 2)


@pytest.mark.parametrize(
    ("element", "author", "expected"),
    [
        ("system", "system", "pending"),  # "Incident created" -> impact assessment
        ("work_notes", "it_staff", "pending"),
        ("comments", "it_staff", "pending"),
        ("comments", "store_user", "skipped"),  # manager's reply: never processed
        ("work_notes", "agent", "skipped"),  # loop protection
        ("comments", "system", "skipped"),
    ],
)
def test_journal_process_status(element: str, author: str, expected: str) -> None:
    assert journal_process_status(element, author) == expected


# --- display mapping ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("priority", "expected"), [(1, "high"), (2, "high"), (3, "medium"), (4, "low"), (5, "low")]
)
def test_severity(priority: int, expected: str) -> None:
    assert m.severity(priority) == expected


@pytest.mark.parametrize(
    ("state", "hold", "status", "badge", "status_new"),
    [
        (1, None, "open", "Open", "Ongoing"),
        (2, None, "open", "Open", "Ongoing"),
        (3, "awaiting_site_visit", "pending", "Pending Site Visit", "Ongoing"),
        (3, "awaiting_vendor", "pending", "Pending", "Ongoing"),
        (3, "awaiting_caller", "pending", "Pending Store Response", "Ongoing"),
        (3, "awaiting_change", "pending", "Pending Change", "Ongoing"),
        (6, None, "resolved", "Resolved", "Resolved"),
        (7, None, "closed", "Closed", "Closed"),
        (8, None, "cancelled", "Cancelled", "Cancelled"),
    ],
)
def test_status_badge_and_status_new(
    state: int, hold: str | None, status: str, badge: str, status_new: str
) -> None:
    assert m.list_status(state) == status
    assert m.badge(state, hold) == badge
    assert m.status_new(state) == status_new


@pytest.mark.parametrize(
    ("contact", "ci_kind", "expected"),
    [
        ("monitoring", "network_element", "Network Alert Based"),
        ("monitoring", "device", "Device Alert Based"),
        ("monitoring", None, "User Reported"),
        ("phone", "device", "User Reported"),
        ("self_service", "network_element", "User Reported"),
    ],
)
def test_incident_type(contact: str, ci_kind: str | None, expected: str) -> None:
    assert m.incident_type(contact, ci_kind) == expected


def test_resolution_eta_and_summary() -> None:
    chennai = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 10, 13, 3, 30, tzinfo=UTC)  # 09:00 IST
    eta = datetime(2026, 10, 13, 8, 30, tzinfo=UTC)  # 14:00 IST
    assert m.resolution_eta(eta, "ignored when a time is set", chennai, now) == "Today 2PM IST"
    assert m.resolution_eta(None, "Awaiting update from ISP", chennai, now) == (
        "Awaiting update from ISP"
    )
    assert m.resolution_eta(None, None, chennai, now) is None
    assert m.summary("latest IT comment", "description") == "latest IT comment"
    assert m.summary(None, "description") == "description"
