# tests/test_seed_itsm.py
"""Seed part 2: incidents, journal entries and appointments."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Appointment, Incident, IncidentJournal
from app.seed import ids
from app.seed.itsm import build_itsm, incident_id, next_local_time
from app.seed.loader import DEFAULT_DATA_DIR, SeedDataError, load_seed_data
from app.seed.runner import STEP_ORDER, STEPS, SeedDependencyError

ANCHOR = datetime(2026, 10, 13, 3, 30, tzinfo=UTC)  # 09:00 IST
CHENNAI = ZoneInfo("Asia/Kolkata")


@pytest.fixture(scope="module")
def rows():
    return build_itsm(load_seed_data(), ANCHOR)


def _incident(rows, number: str) -> dict:
    return next(i for i in rows.incidents if i["number"] == number)


# --- the mobile mock (store 01234) --------------------------------------------------
MOCK = {
    "INC00001234": ("ISP - Primary Link", "Retail SDWAN ATTLink Status", "AT&T SDWAN Linkdown"),
    "INC00001235": (
        "POS Terminal 03 - System Offline",
        "POS Application Error",
        "POS Terminal 03 - Checkout 3 Hardware Issue",
    ),
    "INC00001236": (
        "Register 04 - System Offline",
        "Register Application Error",
        "Register 04 - Checkout 4",
    ),
    "INC00001237": (
        "Wireless - Access Point A004",
        "Network accesspoint unavailable",
        "Access Point - Sales Floor is down",
    ),
}


def test_chennai_incidents_match_the_mock(rows) -> None:
    for number, (title, subtitle, service) in MOCK.items():
        row = _incident(rows, number)
        assert row["store_id"] == ids.store_id("01234")
        assert (row["short_description"], row["u_symptom"], row["business_service"]) == (
            title,
            subtitle,
            service,
        )
    assert _incident(rows, "INC00001234")["cmdb_ci_network_element_id"] == (
        ids.network_element_id("01234", "WAN01234-PRI")
    )
    assert _incident(rows, "INC00001235")["cmdb_ci_device_id"] == (
        ids.device_id("01234", "POS Terminal 03")
    )
    assert _incident(rows, "INC00001237")["cmdb_ci_network_element_id"] == (
        ids.network_element_id("01234", "AP01234N004")
    )


def test_opened_times_follow_the_mock(rows) -> None:
    ages = {n: ANCHOR - _incident(rows, n)["opened_at"] for n in MOCK}
    assert ages == {
        "INC00001234": timedelta(hours=1, minutes=24),  # 1.4 h
        "INC00001235": timedelta(hours=6),
        "INC00001236": timedelta(hours=12),
        "INC00001237": timedelta(hours=2, minutes=12),  # 2.2 h
    }


def test_every_incident_opens_after_its_fault() -> None:
    data = load_seed_data()
    faults = {
        "INC00001234": "-1h30m",  # WAN01234-PRI down
        "INC00001237": "-2h20m",  # AP01234N004 down
        "INC00001235": "-6h05m",  # POS Terminal 03 offline
        "INC00001236": "-12h05m",  # Register 04 offline
        "INC00001301": "-6h",  # WAN01456-BKP down
        "INC00001303": "-3h",  # SW02556N303 down
        "INC00001304": "-20h",  # Printer 01 offline (03340)
    }
    from app.seed.anchor import parse_offset

    opened = {i.number: parse_offset(i.opened_at) for i in data.incidents.incidents}
    for number, fault in faults.items():
        assert opened[number] > parse_offset(fault), number


def test_journal_entries(rows) -> None:
    by_incident: dict = {}
    for entry in rows.journal:
        by_incident.setdefault(entry["incident_id"], []).append(entry)
    for incident in rows.incidents:
        entries = by_incident[incident["id"]]
        created = entries[0]
        assert (created["element"], created["author_type"], created["value"]) == (
            "system",
            "system",
            "Incident created.",
        )
        assert created["created_at"] == incident["opened_at"]
        assert all(e["process_status"] == "pending" for e in entries)
        assert all(e["available_at"] == e["created_at"] for e in entries)
        assert all(e["created_at"] >= incident["opened_at"] for e in entries)
        assert any(e["element"] == "comments" for e in entries)

    resolved = _incident(rows, "INC00001238")
    last = by_incident[resolved["id"]][-1]
    assert (last["element"], last["created_at"], last["value"]) == (
        "work_notes",
        resolved["resolved_at"],
        resolved["close_notes"],
    )


def test_chennai_appointments_match_the_mock(rows) -> None:
    chennai = [a for a in rows.appointments if a["store_id"] == ids.store_id("01234")]
    assert [
        (a["title"], a["purpose"], a["scheduled_start"].astimezone(CHENNAI).strftime("%H:%M"))
        for a in chennai
    ] == [
        ("Site Visit - Field Tech Engineer", "POS Terminal Hardware Replacement", "12:00"),
        ("ISP Technician", "AT&T Link Restoration", "13:00"),
        ("Third Party Technician", "Network Access Point-004 Replacement", "14:00"),
    ]
    linked = {a["purpose"]: a["incident_id"] for a in chennai}
    assert linked["POS Terminal Hardware Replacement"] == incident_id("INC00001235")
    assert linked["AT&T Link Restoration"] == incident_id("INC00001234")
    assert linked["Network Access Point-004 Replacement"] == incident_id("INC00001237")
    # The ETAs match the visits.
    assert _incident(rows, "INC00001235")["u_resolution_eta"] == chennai[0]["scheduled_start"]
    assert _incident(rows, "INC00001237")["u_resolution_eta"] == chennai[2]["scheduled_start"]


def test_every_appointment_has_an_active_incident_of_its_store(rows) -> None:
    incidents = {i["id"]: i for i in rows.incidents}
    assert len(rows.appointments) == 6
    for appointment in rows.appointments:
        incident = incidents[appointment["incident_id"]]
        assert incident["state"] in (1, 2, 3)
        assert incident["store_id"] == appointment["store_id"]
        assert appointment["scheduled_end"] > appointment["scheduled_start"] > ANCHOR


def test_next_local_time() -> None:
    nine_am = ANCHOR  # 09:00 IST
    assert next_local_time(nine_am, CHENNAI, "12:00") == datetime(2026, 10, 13, 6, 30, tzinfo=UTC)
    # 15:00 IST: noon has passed, so it moves to tomorrow.
    three_pm = datetime(2026, 10, 13, 9, 30, tzinfo=UTC)
    assert next_local_time(three_pm, CHENNAI, "12:00") == datetime(2026, 10, 14, 6, 30, tzinfo=UTC)
    assert next_local_time(nine_am, CHENNAI, "10:00", "tomorrow") == datetime(
        2026, 10, 14, 4, 30, tzinfo=UTC
    )


def test_rows_are_deterministic(rows) -> None:
    again = build_itsm(load_seed_data(), ANCHOR)
    assert again.incidents == rows.incidents
    assert again.journal == rows.journal
    assert again.appointments == rows.appointments


# --- validation -----------------------------------------------------------------------
def _broken_copy(tmp_path: Path, file: str, old: str, new: str) -> Path:
    target = tmp_path / "data"
    shutil.copytree(DEFAULT_DATA_DIR, target)
    path = target / file
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not found in {file}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return target


INC = "incidents.yaml"
APT = "appointments.yaml"
BROKEN = [
    (INC, 'ci_element: "AP01234N004"', 'ci_element: "AP01234N009"', "unknown network element"),
    (INC, 'ci_device: "Register 04"', 'ci_device: "PIN Pad 01"', "unknown device"),
    (INC, 'assignment_group: "Network Ops"', 'assignment_group: "Net"', "unknown assignment"),
    (INC, 'number: "INC00001236"', 'number: "INC00002236"', "below INC00002000"),
    (INC, 'hold_reason: "awaiting_vendor"', "hold_reason: null", "hold_reason is required"),
    (APT, 'incident: "INC00001237"', 'incident: "INC00001238"', "is not active"),
    (APT, 'incident: "INC00001237"', 'incident: "INC00009999"', "unknown incident"),
    (APT, 'at_local: "14:00"', 'at_local: "2 PM"', "must be quoted HH:MM"),
]


@pytest.mark.parametrize(("file", "old", "new", "message"), BROKEN)
def test_broken_itsm_files_fail_clearly(
    tmp_path: Path, file: str, old: str, new: str, message: str
) -> None:
    with pytest.raises(SeedDataError, match=message):
        load_seed_data(_broken_copy(tmp_path, file, old, new))


# --- database -------------------------------------------------------------------------
async def _seed(session: AsyncSession, anchor: datetime = ANCHOR) -> None:
    data = load_seed_data()
    for name in STEP_ORDER:
        await STEPS[name](session, data, anchor)


async def _counts(session: AsyncSession) -> tuple[int, int, int]:
    return tuple(
        [
            await session.scalar(select(func.count()).select_from(model)) or 0
            for model in (Incident, IncidentJournal, Appointment)
        ]
    )


async def test_itsm_step_writes_and_replaces(db_session: AsyncSession) -> None:
    await _seed(db_session)
    assert await _counts(db_session) == (9, 28, 6)
    pending = await db_session.scalar(
        select(func.count())
        .select_from(IncidentJournal)
        .where(IncidentJournal.process_status == "pending")
    )
    assert pending == 28

    # Something the worker would change later is reset by the next run.
    entry = await db_session.scalar(select(IncidentJournal).limit(1))
    entry.process_status = "done"
    entry.processed_at = ANCHOR
    await db_session.flush()

    await _seed(db_session, ANCHOR + timedelta(hours=1))
    assert await _counts(db_session) == (9, 28, 6)
    statuses = (await db_session.scalars(select(IncidentJournal.process_status).distinct())).all()
    assert statuses == ["pending"]


async def test_itsm_step_needs_inventory(db_session: AsyncSession) -> None:
    data = load_seed_data()
    await STEPS["master"](db_session, data, ANCHOR)
    with pytest.raises(SeedDependencyError, match="scripts.seed inventory"):
        await STEPS["itsm"](db_session, data, ANCHOR)
