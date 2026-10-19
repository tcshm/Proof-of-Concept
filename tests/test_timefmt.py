# tests/test_timefmt.py
"""Store-local time formatting and the clock. Pure functions: no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.clock import FixedClock, SystemClock
from app.core.timefmt import (
    day_label,
    format_eta,
    format_when,
    is_open,
    local_day,
    to_iso_utc,
    tz_abbreviation,
)

CHENNAI = ZoneInfo("Asia/Kolkata")
LONDON = ZoneInfo("Europe/London")
BERLIN = ZoneInfo("Europe/Berlin")


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --- ISO output --------------------------------------------------------------
def test_iso_utc_has_z_suffix_and_no_fraction() -> None:
    assert to_iso_utc(utc(2026, 10, 13, 7, 12, 0)) == "2026-10-13T07:12:00Z"
    local = datetime(2026, 10, 13, 12, 42, 30, 123456, tzinfo=CHENNAI)
    assert to_iso_utc(local) == "2026-10-13T07:12:30Z"


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError):
        to_iso_utc(datetime(2026, 10, 13, 7, 12))


# --- appointment "when" ------------------------------------------------------
NOW = utc(2026, 10, 13, 4, 0)  # 09:30 IST on Tue 13 Oct


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (utc(2026, 10, 13, 7, 30), "Today at 1:00 PM"),
        (utc(2026, 10, 14, 5, 30), "Tomorrow at 11:00 AM"),
        (utc(2026, 10, 15, 4, 30), "Thu 15 Oct at 10:00 AM"),
        (utc(2026, 10, 12, 12, 0), "Yesterday at 5:30 PM"),
        (utc(2026, 10, 13, 18, 29), "Today at 11:59 PM"),
        (utc(2026, 10, 13, 18, 30), "Tomorrow at 12:00 AM"),
        (utc(2026, 10, 13, 6, 30), "Today at 12:00 PM"),
    ],
)
def test_format_when(start: datetime, expected: str) -> None:
    assert format_when(start, CHENNAI, NOW) == expected


def test_today_follows_the_store_not_utc() -> None:
    # 20:00 UTC on 12 Oct is already 01:30 IST on 13 Oct in Chennai.
    now = utc(2026, 10, 12, 20, 0)
    assert local_day(now, CHENNAI).isoformat() == "2026-10-13"
    assert format_when(utc(2026, 10, 13, 2, 30), CHENNAI, now) == "Today at 8:00 AM"
    # In London the same moment is 21:00 BST on 12 Oct, so 13 Oct is tomorrow.
    assert format_when(utc(2026, 10, 13, 7, 0), LONDON, now) == "Tomorrow at 8:00 AM"


def test_other_year_is_shown() -> None:
    now = utc(2026, 12, 30, 9, 0)
    assert format_when(utc(2027, 1, 2, 9, 0), LONDON, now) == "Sat 2 Jan 2027 at 9:00 AM"


# --- resolution ETA ----------------------------------------------------------
@pytest.mark.parametrize(
    ("eta", "tz", "expected"),
    [
        (utc(2026, 10, 13, 8, 30), CHENNAI, "Today 2PM IST"),
        (utc(2026, 10, 14, 5, 0), CHENNAI, "Tomorrow 10:30AM IST"),
        (utc(2026, 10, 15, 6, 30), CHENNAI, "Thu 15 Oct 12PM IST"),
        (utc(2026, 10, 13, 10, 0), BERLIN, "Today 12PM CEST"),
        (utc(2026, 12, 20, 13, 0), BERLIN, "Sun 20 Dec 2PM CET"),
    ],
)
def test_format_eta(eta: datetime, tz: ZoneInfo, expected: str) -> None:
    assert format_eta(eta, tz, NOW) == expected


def test_uk_daylight_saving_end() -> None:
    # Clocks go back on Sun 25 Oct 2026: 01:30 local happens twice.
    now = utc(2026, 10, 24, 12, 0)
    first = utc(2026, 10, 25, 0, 30)  # 01:30 BST
    second = utc(2026, 10, 25, 1, 30)  # 01:30 GMT
    assert format_eta(first, LONDON, now) == "Tomorrow 1:30AM BST"
    assert format_eta(second, LONDON, now) == "Tomorrow 1:30AM GMT"
    assert tz_abbreviation(utc(2026, 7, 1), LONDON) == "BST"
    assert tz_abbreviation(utc(2026, 12, 1), LONDON) == "GMT"


def test_day_label_names_are_english() -> None:
    assert day_label(utc(2026, 10, 18, 6, 0), CHENNAI, NOW) == "Sun 18 Oct"


# --- business hours ----------------------------------------------------------
HOURS = {
    "mon": [["10:00", "22:00"]],
    "tue": [["10:00", "22:00"]],
    "sat": [["09:00", "13:00"], ["14:00", "20:00"]],
    "sun": [],
}


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (utc(2026, 10, 13, 4, 30), True),  # Tue 10:00 IST: opening minute
        (utc(2026, 10, 13, 4, 29), False),  # Tue 09:59 IST
        (utc(2026, 10, 13, 16, 29), True),  # Tue 21:59 IST
        (utc(2026, 10, 13, 16, 30), False),  # Tue 22:00 IST: closed
        (utc(2026, 10, 17, 8, 0), False),  # Sat 13:30 IST: lunch break
        (utc(2026, 10, 17, 9, 0), True),  # Sat 14:30 IST
        (utc(2026, 10, 18, 6, 0), False),  # Sun: closed all day
        (utc(2026, 10, 14, 6, 0), False),  # Wed: day missing means closed
    ],
)
def test_is_open(now: datetime, expected: bool) -> None:
    assert is_open(HOURS, CHENNAI, now) is expected


# --- clock -------------------------------------------------------------------
def test_fixed_clock() -> None:
    clock = FixedClock(datetime(2026, 10, 13, 9, 30, tzinfo=CHENNAI))
    assert clock.now() == utc(2026, 10, 13, 4, 0)
    assert clock.now().tzinfo == UTC
    clock.advance(timedelta(hours=1))
    assert clock.now() == utc(2026, 10, 13, 5, 0)
    clock.set(utc(2027, 1, 1))
    assert clock.now() == utc(2027, 1, 1)
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 10, 13))


def test_system_clock_is_utc_and_current() -> None:
    now = SystemClock().now()
    assert now.tzinfo == UTC
    assert abs((datetime.now(UTC) - now).total_seconds()) < 5
