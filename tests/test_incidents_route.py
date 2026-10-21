# tests/test_incidents_route.py
"""GET /api/v1/incidents and GET /api/v1/incidents/{number} on the seeded database."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.deps import get_clock
from app.core.clock import FixedClock
from app.models import IncidentJournal
from app.seed.itsm import incident_id
from tests.conftest import ITSM_ANCHOR

CHENNAI = {"X-Site-Id": "01234"}


@pytest.fixture
def clock(app: FastAPI) -> FixedClock:
    fixed = FixedClock(ITSM_ANCHOR)  # 09:00 IST on Tue 13 Oct 2026
    app.dependency_overrides[get_clock] = lambda: fixed
    return fixed


async def _incidents(client: AsyncClient, headers: dict[str, str] = CHENNAI) -> list[dict]:
    response = await client.get("/api/v1/incidents", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_chennai_incidents_match_the_mock(
    client: AsyncClient, itsm_engine: AsyncEngine, clock: FixedClock
) -> None:
    incidents = await _incidents(client)
    # Active only (INC00001238 is resolved), highest severity first, then newest.
    assert [i["id"] for i in incidents] == [
        "INC00001234",
        "INC00001237",
        "INC00001235",
        "INC00001236",
    ]
    by_id = {i["id"]: i for i in incidents}
    assert by_id["INC00001234"] == {
        "id": "INC00001234",
        "title": "ISP - Primary Link",
        "subtitle": "Retail SDWAN ATTLink Status",
        "service": "AT&T SDWAN Linkdown",
        "summary": (
            "Primary link for the store is down, store network is up backup link. "
            "Working with the Telco Provider for the link restoration."
        ),
        "ciType": "wan_link",
        "severity": "high",
        "status": "pending",
        "badge": "Pending Site Visit",
        "reportedAt": "2026-10-13T02:06:00Z",
        "incidentType": "Network Alert Based",
        "incidentStatusNew": "Ongoing",
        "resolutionETA": "Awaiting update from ISP",
        "resolutionGroup": "Network Ops",
    }
    pos = by_id["INC00001235"]
    assert (pos["severity"], pos["status"], pos["badge"]) == ("medium", "pending", "Pending")
    assert (pos["ciType"], pos["incidentType"]) == ("pos", "Device Alert Based")
    assert pos["resolutionETA"] == "Today 12PM IST"
    assert pos["summary"] == (
        "POS terminal hardware failure. Hardware needs to be replaced by Field Tech."
    )
    register = by_id["INC00001236"]
    assert (register["status"], register["badge"]) == ("open", "Open")
    assert register["resolutionETA"] == "Awaiting update from store"
    assert register["resolutionGroup"] == "Retail EUC Ops"
    ap = by_id["INC00001237"]
    assert (ap["ciType"], ap["incidentType"]) == ("access_point", "Network Alert Based")
    assert ap["resolutionETA"] == "Today 2PM IST"


async def test_eta_text_follows_the_clock(
    client: AsyncClient, itsm_engine: AsyncEngine, clock: FixedClock
) -> None:
    clock.set(ITSM_ANCHOR.replace(day=12))  # one day earlier
    by_id = {i["id"]: i for i in await _incidents(client)}
    assert by_id["INC00001237"]["resolutionETA"] == "Tomorrow 2PM IST"


async def test_summary_is_the_latest_it_comment(
    client: AsyncClient, itsm_engine: AsyncEngine, clock: FixedClock
) -> None:
    # A store manager's reply is newer, but never becomes the summary.
    async with itsm_engine.begin() as connection:
        await connection.execute(
            insert(IncidentJournal).values(
                incident_id=incident_id("INC00001236"),
                element="comments",
                value="Register 04 cable was loose; plugged it back in.",
                author_type="store_user",
                created_by="store-01234",
                source="mobile_app",
                process_status="skipped",
                processed_at=ITSM_ANCHOR,
                created_at=ITSM_ANCHOR,
                available_at=ITSM_ANCHOR,
            )
        )
    by_id = {i["id"]: i for i in await _incidents(client)}
    assert by_id["INC00001236"]["summary"] == (
        "Register 04 is offline. Trying to reach store to check the device status."
    )


async def test_other_stores(client: AsyncClient, itsm_engine: AsyncEngine, clock: FixedClock):
    paris = await _incidents(client, {"X-Site-Id": "02556"})
    assert [(i["id"], i["severity"], i["badge"], i["resolutionETA"]) for i in paris] == [
        ("INC00001303", "high", "Pending Site Visit", "Today 4PM CEST")
    ]
    bengaluru = await _incidents(client, {"X-Site-Id": "01456"})
    assert [i["id"] for i in bengaluru] == ["INC00001301", "INC00001302"]
    assert bengaluru[1]["incidentType"] == "User Reported"
    assert await _incidents(client, {"X-Site-Id": "02112"}) == []


async def test_incident_detail(client: AsyncClient, itsm_engine: AsyncEngine, clock: FixedClock):
    response = await client.get("/api/v1/incidents/INC00001234", headers=CHENNAI)
    assert response.status_code == 200
    detail = response.json()
    assert detail["id"] == "INC00001234"
    assert detail["assignedTo"] == "r.kumar"
    assert detail["description"].startswith("Aruba reports WAN health DOWN")
    # Comments only: no work notes, no system entry.
    assert [c["text"] for c in detail["comments"]] == [
        "Primary link for the store is down, store network is up backup link. "
        "Working with the Telco Provider for the link restoration."
    ]
    assert detail["comments"][0]["authorType"] == "it_staff"


async def test_resolved_incident_detail(
    client: AsyncClient, itsm_engine: AsyncEngine, clock: FixedClock
) -> None:
    detail = (await client.get("/api/v1/incidents/INC00001238", headers=CHENNAI)).json()
    assert (detail["status"], detail["badge"], detail["incidentStatusNew"]) == (
        "resolved",
        "Resolved",
        "Resolved",
    )
    assert detail["incidentType"] == "User Reported"


async def test_detail_errors(client: AsyncClient, itsm_engine: AsyncEngine, clock: FixedClock):
    other_store = await client.get("/api/v1/incidents/INC00001303", headers=CHENNAI)
    assert other_store.status_code == 404  # Paris incident: not revealed to Chennai
    assert other_store.json()["error"]["message"] == "Incident INC00001303 was not found."
    invalid = await client.get("/api/v1/incidents/1234", headers=CHENNAI)
    assert invalid.status_code == 422
    assert (await client.get("/api/v1/incidents")).status_code == 422  # no X-Site-Id


async def test_network_and_devices_show_the_incidents(
    client: AsyncClient, itsm_engine: AsyncEngine, clock: FixedClock
) -> None:
    network = (await client.get("/api/v1/network", headers=CHENNAI)).json()
    aps = {a["name"]: a for a in network["accessPoints"]}
    assert aps["AP01234N004"]["incidents"] == [
        {"id": "INC00001237", "title": "Wireless - Access Point A004"}
    ]
    pos = (await client.get("/api/v1/devices/pos", headers=CHENNAI)).json()
    by_name = {d["name"]: d for d in pos}
    assert by_name["POS Terminal 03"]["incidents"] == [
        {"id": "INC00001235", "title": "POS Terminal 03 - System Offline"}
    ]
    printers = (await client.get("/api/v1/devices/printer", headers=CHENNAI)).json()
    assert all(p["incidents"] == [] for p in printers)  # INC00001238 is resolved
