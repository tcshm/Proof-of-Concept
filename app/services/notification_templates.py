# app/services/notification_templates.py
"""Fixed texts for system notifications (never written by the LLM).

Pure functions: event details in, NotificationContent out. Stored text must not
go stale, so times are written as absolute store-local text
("Tue 13 Oct at 1:00 PM IST"), never "Today".

Expiry:
    appointment notices  24 hours after the visit ends
    incident / action    7 days after publishing
A notice never expires less than one hour after it is published.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.timefmt import format_absolute, to_local
from app.models.values import NotificationCategory

APPOINTMENT_NOTICE_KEEP = timedelta(hours=24)
INCIDENT_NOTICE_KEEP = timedelta(days=7)
DEFAULT_VISIT_LENGTH = timedelta(hours=2)
MIN_LIFETIME = timedelta(hours=1)
MAX_TITLE = 150
MAX_DESCRIPTION = 2000


@dataclass(frozen=True)
class NotificationContent:
    category: str
    title: str
    description: str
    expires_at: datetime | None


def clip(text: str, limit: int) -> str:
    """One line of text, shortened with an ellipsis when it is longer than limit."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def title_time(value: datetime, tz: ZoneInfo) -> str:
    """'01:00 PM' (zero-padded, as in the mobile mock 'New Appointment: ... at 01:00 PM')."""
    local = to_local(value, tz)
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{hour:02d}:{local.minute:02d} {suffix}"


def _visit_expiry(start: datetime, end: datetime | None, publish_at: datetime) -> datetime:
    finish = end or start + DEFAULT_VISIT_LENGTH
    return max(finish + APPOINTMENT_NOTICE_KEEP, publish_at + MIN_LIFETIME)


def _content(
    category: str, title: str, description: str, expires_at: datetime | None
) -> NotificationContent:
    return NotificationContent(
        category=str(category),
        title=clip(title, MAX_TITLE),
        description=clip(description, MAX_DESCRIPTION),
        expires_at=expires_at,
    )


def appointment_scheduled(
    *,
    title: str,
    purpose: str,
    details: str | None,
    start: datetime,
    end: datetime | None,
    incident_number: str,
    tz: ZoneInfo,
    publish_at: datetime,
) -> NotificationContent:
    description = f"{purpose} on {format_absolute(start, tz)} for incident {incident_number}."
    if details:
        description = f"{description} {details}"
    return _content(
        NotificationCategory.APPOINTMENT,
        f"New Appointment: {title} at {title_time(start, tz)}",
        description,
        _visit_expiry(start, end, publish_at),
    )


def appointment_rescheduled(
    *,
    title: str,
    purpose: str,
    old_start: datetime,
    start: datetime,
    end: datetime | None,
    incident_number: str,
    tz: ZoneInfo,
    publish_at: datetime,
) -> NotificationContent:
    return _content(
        NotificationCategory.APPOINTMENT,
        f"Appointment Rescheduled: {title} at {title_time(start, tz)}",
        f"{purpose} moved from {format_absolute(old_start, tz)} to "
        f"{format_absolute(start, tz)} for incident {incident_number}.",
        _visit_expiry(start, end, publish_at),
    )


def appointment_cancelled(
    *,
    title: str,
    purpose: str,
    note: str,
    start: datetime,
    end: datetime | None,
    incident_number: str,
    tz: ZoneInfo,
    publish_at: datetime,
) -> NotificationContent:
    return _content(
        NotificationCategory.APPOINTMENT,
        f"Appointment Cancelled: {title}",
        f"{purpose} on {format_absolute(start, tz)} is cancelled: {note} "
        f"(incident {incident_number}).",
        _visit_expiry(start, end, publish_at),
    )


def incident_update(
    *, incident_number: str, message: str, publish_at: datetime
) -> NotificationContent:
    """Used by the worker (Phase 9). message is the IT comment or the LLM's
    store-friendly rewrite of a work note."""
    return _content(
        NotificationCategory.INCIDENTS,
        f"Incident {incident_number} Updated",
        message,
        publish_at + INCIDENT_NOTICE_KEEP,
    )


def action_required(
    *, incident_number: str, action_title: str, action_detail: str, publish_at: datetime
) -> NotificationContent:
    """Used by the worker (Phase 9) when it creates a store action."""
    return _content(
        NotificationCategory.INCIDENTS,
        f"Action Required: {action_title}",
        f"{action_detail} (incident {incident_number})",
        publish_at + INCIDENT_NOTICE_KEEP,
    )
