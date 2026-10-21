# tests/test_appointments_route.py
"""GET /api/v1/appointments on the seeded database."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.deps import get_clock
from app.core.clock import FixedClock
from app.models import Appointment
from app.seed.itsm import appointment_id
from tests.conftest import ITSM_ANCHOR

CHENNAI = {"X-Site-Id": "01234"}


@pytest.fixture
def clock(app: FastAPI) -> FixedClock:
    fixed = FixedClock(ITSM_ANCHOR)  # 09:00 IST on Tue 13 Oct 2026
    app.dependency_overrides[get_clock] = lambda: fixed
    return fixed


async def _appointments(client: AsyncClient, headers: dict[str, str] = CHENNAI) -> list[dict]:
    response = await client.get("/api/v1/appointments", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_chennai_appointments_match_the_mock(
    client: AsyncClient, itsm_engine: AsyncEngine, clock: FixedClock
) -> None:
    appointments = await _appointments(client)
    assert [(a["subTitle"], a["when"]) for a in appointments] == [
        ("POS Terminal Hardware Replacement", "Today at 12:00 PM"),
        ("AT&T Link Restoration", "Today at 1:00 PM"),
        ("Network Access Point-004 Replacement", "Today at 2:00 PM"),
    ]
    assert [a["title"] for a in appointments] == [
        "Site Visit - Field Tech Engineer",
        "ISP Technician",
        "Third Party Technician",
    ]
    first = appointments[0]
    assert first == {
        "id": str(appointment_id("apt-001")),
        "appointmentType": "field_tech",
        "title": "Site Visit - Field Tech Engineer",
        "subTitle": "POS Terminal Hardware Replacement",
        "when": "Today at 12:00 PM",
        "scheduledStart": "2026-10-13T06:30:00Z",
        "scheduledEnd": "2026-10-13T07:30:00Z",
        "status": "scheduled",
        "details": "POS terminal hardware failure. Tech is scheduled for replacement at 12 PM.",
        "incidentId": "INC00001235",
    }
    assert [a["incidentId"] for a in appointments] == [
        "INC00001235",
        "INC00001234",
        "INC00001237",
    ]
    assert [a["appointmentType"] for a in appointments] == [
        "field_tech",
        "isp_technician",
        "third_party_vendor",
    ]


async def test_finished_and_cancelled_visits_are_hidden(
    client: AsyncClient, itsm_engine: AsyncEngine, clock: FixedClock
) -> None:
    # 13:45 IST: the 12:00 visit has ended (60 min); the 13:00 ISP visit runs to 15:00.
    clock.set(ITSM_ANCHOR + timedelta(hours=4, minutes=45))
    assert [a["subTitle"] for a in await _appointments(client)] == [
        "AT&T Link Restoration",
        "Network Access Point-004 Replacement",
    ]
    async with itsm_engine.begin() as connection:
        await connection.execute(
            update(Appointment)
            .where(Appointment.id == appointment_id("apt-003"))
            .values(status="cancelled", status_note="Vendor rescheduling")
        )
    assert [a["subTitle"] for a in await _appointments(client)] == ["AT&T Link Restoration"]


async def test_in_progress_visit_stays_listed(
    client: AsyncClient, itsm_engine: AsyncEngine, clock: FixedClock
) -> None:
    async with itsm_engine.begin() as connection:
        await connection.execute(
            update(Appointment)
            .where(Appointment.id == appointment_id("apt-001"))
            .values(status="in_progress")
        )
    clock.set(ITSM_ANCHOR + timedelta(hours=3, minutes=15))  # 12:15 IST
    first = (await _appointments(client))[0]
    assert (first["status"], first["when"]) == ("in_progress", "Today at 12:00 PM")


async def test_other_stores(client: AsyncClient, itsm_engine: AsyncEngine, clock: FixedClock):
    paris = await _appointments(client, {"X-Site-Id": "02556"})
    assert [(a["subTitle"], a["when"], a["incidentId"]) for a in paris] == [
        ("Back Office Switch Replacement", "Today at 4:00 PM", "INC00001303")
    ]
    manchester = await _appointments(client, {"X-Site-Id": "03340"})
    assert [(a["subTitle"], a["when"]) for a in manchester] == [
        ("Checkout Printer Repair", "Tomorrow at 9:30 AM")
    ]
    assert await _appointments(client, {"X-Site-Id": "02112"}) == []
    assert (await client.get("/api/v1/appointments")).status_code == 422
