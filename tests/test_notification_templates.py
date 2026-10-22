# tests/test_notification_templates.py
"""System notification templates and absolute time text. Pure functions: no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.api.mappers.notifications import category_label
from app.core.timefmt import format_absolute
from app.services import notification_templates as t

CHENNAI = ZoneInfo("Asia/Kolkata")
LONDON = ZoneInfo("Europe/London")
START = datetime(2026, 10, 13, 7, 30, tzinfo=UTC)  # Tue 13 Oct, 1:00 PM IST
BOOKED = START - timedelta(hours=4)


def test_format_absolute() -> None:
    assert format_absolute(START, CHENNAI) == "Tue 13 Oct at 1:00 PM IST"
    assert format_absolute(START, LONDON) == "Tue 13 Oct at 8:30 AM BST"
    assert format_absolute(datetime(2026, 12, 1, 9, 0, tzinfo=UTC), LONDON) == (
        "Tue 1 Dec at 9:00 AM GMT"
    )


def test_title_time_is_zero_padded() -> None:
    assert t.title_time(START, CHENNAI) == "01:00 PM"
    assert t.title_time(datetime(2026, 10, 13, 6, 30, tzinfo=UTC), CHENNAI) == "12:00 PM"
    assert t.title_time(datetime(2026, 10, 13, 18, 30, tzinfo=UTC), CHENNAI) == "12:00 AM"
    assert t.title_time(datetime(2026, 10, 13, 4, 45, tzinfo=UTC), CHENNAI) == "10:15 AM"


def _common(**overrides) -> dict:
    values = {
        "title": "ISP Technician",
        "purpose": "AT&T Link Restoration",
        "start": START,
        "end": START + timedelta(hours=2),
        "incident_number": "INC00001234",
        "tz": CHENNAI,
        "publish_at": BOOKED,
    }
    values.update(overrides)
    return values


def test_appointment_scheduled_matches_the_mock_title() -> None:
    content = t.appointment_scheduled(
        details="AT&T technician visit to store to check ISP device.", **_common()
    )
    assert content.category == "appointment"
    assert content.title == "New Appointment: ISP Technician at 01:00 PM"  # mock n-004
    assert content.description == (
        "AT&T Link Restoration on Tue 13 Oct at 1:00 PM IST for incident INC00001234. "
        "AT&T technician visit to store to check ISP device."
    )
    assert content.expires_at == START + timedelta(hours=2) + timedelta(hours=24)
    assert "Today" not in content.title + content.description


def test_appointment_rescheduled_and_cancelled() -> None:
    new_start = START + timedelta(days=1)
    moved = t.appointment_rescheduled(
        old_start=START, **_common(start=new_start, end=None, publish_at=START)
    )
    assert moved.title == "Appointment Rescheduled: ISP Technician at 01:00 PM"
    assert moved.description == (
        "AT&T Link Restoration moved from Tue 13 Oct at 1:00 PM IST to "
        "Wed 14 Oct at 1:00 PM IST for incident INC00001234."
    )
    assert moved.expires_at == new_start + timedelta(hours=2) + timedelta(hours=24)

    cancelled = t.appointment_cancelled(note="Incident closed", **_common())
    assert cancelled.title == "Appointment Cancelled: ISP Technician"
    assert cancelled.description == (
        "AT&T Link Restoration on Tue 13 Oct at 1:00 PM IST is cancelled: "
        "Incident closed (incident INC00001234)."
    )


def test_notice_never_expires_before_it_is_published() -> None:
    late = START + timedelta(days=3)
    content = t.appointment_cancelled(note="x", **_common(publish_at=late))
    assert content.expires_at == late + timedelta(hours=1)


def test_incident_and_action_notices() -> None:
    update = t.incident_update(
        incident_number="INC00001234",
        message="Incident details were updated by the technical team.",
        publish_at=START,
    )
    assert (update.category, update.title) == ("incidents", "Incident INC00001234 Updated")
    assert update.expires_at == START + timedelta(days=7)

    action = t.action_required(
        incident_number="INC00001234",
        action_title="Provide comms room access at 1 PM",
        action_detail="The AT&T engineer needs access to the comms room.",
        publish_at=START,
    )
    assert action.title == "Action Required: Provide comms room access at 1 PM"
    assert action.description.endswith("(incident INC00001234)")


def test_long_text_is_clipped_to_one_line() -> None:
    content = t.incident_update(
        incident_number="INC00001234", message="line one\n\nline   two", publish_at=START
    )
    assert content.description == "line one line two"
    long = t.clip("x" * 400, t.MAX_TITLE)
    assert len(long) == t.MAX_TITLE and long.endswith("\u2026")


def test_category_labels() -> None:
    assert [category_label(c) for c in ("maintenance", "updates", "incidents")] == [
        "Maintenance",
        "Updates",
        "Incidents",
    ]
    assert [category_label(c) for c in ("appointment", "training", "others", "x")] == [
        "Appointment",
        "Training",
        "Others",
        "Others",
    ]
