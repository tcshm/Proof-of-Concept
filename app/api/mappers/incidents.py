# app/api/mappers/incidents.py
"""Incident display rules (the agreed mapping from ServiceNow fields to the app)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from app.core.timefmt import format_eta
from app.models.values import ContactType, HoldReason, IncidentState

Severity = Literal["high", "medium", "low"]

HOLD_REASON_BADGES: dict[str, str] = {
    HoldReason.AWAITING_SITE_VISIT: "Pending Site Visit",
    HoldReason.AWAITING_CALLER: "Pending Store Response",
    HoldReason.AWAITING_VENDOR: "Pending",
    HoldReason.AWAITING_CHANGE: "Pending Change",
    HoldReason.AWAITING_PROBLEM: "Pending",
}

_STATUS: dict[int, str] = {
    IncidentState.NEW: "open",
    IncidentState.IN_PROGRESS: "open",
    IncidentState.ON_HOLD: "pending",
    IncidentState.RESOLVED: "resolved",
    IncidentState.CLOSED: "closed",
    IncidentState.CANCELED: "cancelled",
}

_STATUS_NEW: dict[int, str] = {
    IncidentState.NEW: "Ongoing",
    IncidentState.IN_PROGRESS: "Ongoing",
    IncidentState.ON_HOLD: "Ongoing",
    IncidentState.RESOLVED: "Resolved",
    IncidentState.CLOSED: "Closed",
    IncidentState.CANCELED: "Cancelled",
}

_FINISHED_BADGES: dict[int, str] = {
    IncidentState.RESOLVED: "Resolved",
    IncidentState.CLOSED: "Closed",
    IncidentState.CANCELED: "Cancelled",
}


def severity(priority: int) -> Severity:
    """Priority 1-2 high, 3 medium, 4-5 low."""
    if priority <= 2:
        return "high"
    if priority == 3:
        return "medium"
    return "low"


def list_status(state: int) -> str:
    return _STATUS[state]


def badge(state: int, hold_reason: str | None) -> str:
    if state == IncidentState.ON_HOLD and hold_reason:
        return HOLD_REASON_BADGES.get(hold_reason, "Pending")
    return _FINISHED_BADGES.get(state, "Open")


def incident_type(contact_type: str, ci_kind: str | None) -> str:
    if contact_type == ContactType.MONITORING and ci_kind == "network_element":
        return "Network Alert Based"
    if contact_type == ContactType.MONITORING and ci_kind == "device":
        return "Device Alert Based"
    return "User Reported"


def status_new(state: int) -> str:
    return _STATUS_NEW[state]


def resolution_eta(
    eta: datetime | None, note: str | None, tz: ZoneInfo, now: datetime
) -> str | None:
    """A firm ETA in store time ('Today 2PM IST'), otherwise the note, otherwise None."""
    if eta is not None:
        return format_eta(eta, tz, now)
    return note or None


def summary(latest_it_comment: str | None, description: str | None) -> str | None:
    return latest_it_comment or description
