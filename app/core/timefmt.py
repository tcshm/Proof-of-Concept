# app/core/timefmt.py
"""Turn UTC timestamps into the text a store sees.

The database stores every time in UTC. Formatting happens only here, at the
edge, using the store's IANA timezone, so daylight saving and "today" follow
the store, not the server or the phone.

All functions are pure: they take "now" as an argument (from the Clock) and
never read the current time themselves.

    to_iso_utc(dt)            "2026-10-13T07:12:00Z"     date fields in responses
    format_when(dt, tz, now)  "Today at 1:00 PM"         appointment "when"
    format_eta(dt, tz, now)   "Today 2PM IST"            incident "resolutionETA"
    is_open(hours, tz, now)   True / False               store business hours
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# English names, independent of the server's locale settings.
_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime: every time in the backend must be timezone-aware")
    return value


def to_iso_utc(value: datetime) -> str:
    """ISO 8601 in UTC with a Z suffix, to the second: 2026-10-13T07:12:00Z."""
    return _aware(value).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_local(value: datetime, tz: ZoneInfo) -> datetime:
    return _aware(value).astimezone(tz)


def local_day(value: datetime, tz: ZoneInfo) -> date:
    """The calendar date in the store's timezone."""
    return to_local(value, tz).date()


def tz_abbreviation(value: datetime, tz: ZoneInfo) -> str:
    """IST, CET/CEST, GMT/BST ... for the given moment (daylight saving aware)."""
    return to_local(value, tz).tzname() or tz.key


def day_label(value: datetime, tz: ZoneInfo, now: datetime) -> str:
    """Today, Tomorrow, Yesterday, otherwise 'Thu 8 Oct' (plus the year if different)."""
    day = local_day(value, tz)
    today = local_day(now, tz)
    difference = (day - today).days
    if difference == 0:
        return "Today"
    if difference == 1:
        return "Tomorrow"
    if difference == -1:
        return "Yesterday"
    label = f"{_DAY_NAMES[day.weekday()]} {day.day} {_MONTH_NAMES[day.month - 1]}"
    if day.year != today.year:
        label = f"{label} {day.year}"
    return label


def _hour_12(local: datetime) -> tuple[int, str]:
    hour = local.hour % 12 or 12
    return hour, "AM" if local.hour < 12 else "PM"


def clock_time(value: datetime, tz: ZoneInfo) -> str:
    """'1:00 PM', '11:30 AM' (appointment style)."""
    local = to_local(value, tz)
    hour, suffix = _hour_12(local)
    return f"{hour}:{local.minute:02d} {suffix}"


def short_time(value: datetime, tz: ZoneInfo) -> str:
    """'2PM', '10:30AM' (ETA style, as in the mobile mock 'Today 12PM CET')."""
    local = to_local(value, tz)
    hour, suffix = _hour_12(local)
    if local.minute == 0:
        return f"{hour}{suffix}"
    return f"{hour}:{local.minute:02d}{suffix}"


def format_when(value: datetime, tz: ZoneInfo, now: datetime) -> str:
    """Appointment time: 'Today at 1:00 PM', 'Tomorrow at 11:00 AM', 'Thu 8 Oct at 10:00 AM'."""
    return f"{day_label(value, tz, now)} at {clock_time(value, tz)}"


def format_eta(value: datetime, tz: ZoneInfo, now: datetime) -> str:
    """Resolution ETA: 'Today 2PM IST', 'Tomorrow 10:30AM IST', 'Thu 8 Oct 2PM IST'."""
    return f"{day_label(value, tz, now)} {short_time(value, tz)} {tz_abbreviation(value, tz)}"


def is_open(business_hours: dict[str, Any], tz: ZoneInfo, now: datetime) -> bool:
    """Whether the store is open at 'now', from business_hours like
    {"mon": [["08:00", "21:00"]], "sun": []}. A missing day means closed."""
    local = to_local(now, tz)
    intervals = business_hours.get(_DAY_KEYS[local.weekday()]) or []
    current = local.strftime("%H:%M")
    return any(start <= current < end for start, end in intervals)


def format_absolute(value: datetime, tz: ZoneInfo) -> str:
    """Absolute store-local time for stored text: 'Tue 13 Oct at 1:00 PM IST'.

    Used in notification text, which is stored and must not say "Today".
    """
    local = to_local(value, tz)
    day = f"{_DAY_NAMES[local.weekday()]} {local.day} {_MONTH_NAMES[local.month - 1]}"
    return f"{day} at {clock_time(value, tz)} {tz_abbreviation(value, tz)}"
