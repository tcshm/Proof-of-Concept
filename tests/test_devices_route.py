# tests/test_devices_route.py
"""GET /api/v1/devices and GET /api/v1/devices/{deviceType} on the seeded database."""

from __future__ import annotations

from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import Device, Incident
from app.seed import ids
from tests.conftest import SEED_ANCHOR

CHENNAI = {"X-Site-Id": "01234"}


async def test_device_tiles(client: AsyncClient, seeded_engine: AsyncEngine) -> None:
    response = await client.get("/api/v1/devices", headers=CHENNAI)
    assert response.status_code == 200
    assert response.json() == [
        {"deviceType": "pos", "title": "POS", "status": "yellow", "total": 4, "online": 3},
        {
            "deviceType": "register",
            "title": "Register",
            "status": "yellow",
            "total": 4,
            "online": 3,
        },
        {"deviceType": "rf_gun", "title": "RF Gun", "status": "green", "total": 4, "online": 4},
        {"deviceType": "printer", "title": "Printer", "status": "green", "total": 3, "online": 3},
        {
            "deviceType": "workstation",
            "title": "Workstation",
            "status": "green",
            "total": 2,
            "online": 2,
        },
    ]


async def test_degraded_counts_as_not_online(
    client: AsyncClient, seeded_engine: AsyncEngine
) -> None:
    # Bengaluru: Printer 02 is degraded in the seed.
    tiles = (await client.get("/api/v1/devices", headers={"X-Site-Id": "01456"})).json()
    printer = next(t for t in tiles if t["deviceType"] == "printer")
    assert (printer["status"], printer["total"], printer["online"]) == ("yellow", 3, 2)


async def test_all_offline_is_red(client: AsyncClient, seeded_engine: AsyncEngine) -> None:
    async with seeded_engine.begin() as connection:
        await connection.execute(
            update(Device)
            .where(Device.store_id == ids.store_id("02556"), Device.device_type_code == "printer")
            .values(status="offline")
        )
    tiles = (await client.get("/api/v1/devices", headers={"X-Site-Id": "02556"})).json()
    printer = next(t for t in tiles if t["deviceType"] == "printer")
    assert (printer["status"], printer["online"]) == ("red", 0)


async def test_device_list_puts_problems_first(
    client: AsyncClient, seeded_engine: AsyncEngine
) -> None:
    response = await client.get("/api/v1/devices/pos", headers=CHENNAI)
    assert response.status_code == 200
    devices = response.json()
    assert [d["name"] for d in devices] == [
        "POS Terminal 03",
        "POS Terminal 01",
        "POS Terminal 02",
        "POS Terminal 04",
    ]
    pos3 = devices[0]
    assert pos3["id"] == str(ids.device_id("01234", "POS Terminal 03"))
    assert pos3["location"] == "Checkout 3"
    assert (pos3["status"], pos3["connectivity"]) == ("red", "offline")
    assert pos3["statusReason"] == "POS application error - terminal hardware not responding"
    assert pos3["statusSince"] == "2026-10-13T02:55:00Z"
    assert pos3["connectionType"] == "wired"
    assert pos3["connectedTo"] == "SW01234N302"
    assert pos3["port"] == "3"
    assert pos3["ipAddress"] == "10.10.120.13"
    assert pos3["incidents"] == []


async def test_wireless_devices_show_their_access_point(
    client: AsyncClient, seeded_engine: AsyncEngine
) -> None:
    guns = (await client.get("/api/v1/devices/rf_gun", headers=CHENNAI)).json()
    assert {g["connectedTo"] for g in guns} == {
        "AP01234N001",
        "AP01234N002",
        "AP01234N004",
        "AP01234N005",
    }
    assert all(g["port"] is None and g["connectionType"] == "wireless" for g in guns)


async def test_device_incidents(client: AsyncClient, seeded_engine: AsyncEngine) -> None:
    async with seeded_engine.begin() as connection:
        await connection.execute(
            insert(Incident).values(
                number="INC00001235",
                store_id=ids.store_id("01234"),
                short_description="POS Terminal 03 - System Offline",
                category="hardware",
                contact_type="monitoring",
                impact=2,
                urgency=2,
                state=3,
                hold_reason="awaiting_site_visit",
                cmdb_ci_device_id=ids.device_id("01234", "POS Terminal 03"),
                opened_at=SEED_ANCHOR - timedelta(hours=6),
            )
        )
    devices = (await client.get("/api/v1/devices/pos", headers=CHENNAI)).json()
    assert devices[0]["incidents"] == [
        {"id": "INC00001235", "title": "POS Terminal 03 - System Offline"}
    ]


async def test_unknown_and_invalid_device_types(
    client: AsyncClient, seeded_engine: AsyncEngine
) -> None:
    missing = await client.get("/api/v1/devices/pin_pad", headers=CHENNAI)
    assert missing.status_code == 404
    assert missing.json()["error"]["message"] == "Device type pin_pad was not found."

    invalid = await client.get("/api/v1/devices/PIN-PAD", headers=CHENNAI)
    assert invalid.status_code == 422
    assert invalid.json()["error"]["details"][0]["field"] == "path.device_type"


async def test_known_type_with_no_devices_is_empty(
    client: AsyncClient, seeded_engine: AsyncEngine
) -> None:
    async with seeded_engine.begin() as connection:
        await connection.execute(
            update(Device)
            .where(Device.store_id == ids.store_id("01234"), Device.device_type_code == "printer")
            .values(is_active=False)
        )
    response = await client.get("/api/v1/devices/printer", headers=CHENNAI)
    assert response.status_code == 200 and response.json() == []
    tiles = (await client.get("/api/v1/devices", headers=CHENNAI)).json()
    assert "printer" not in {t["deviceType"] for t in tiles}


async def test_store_header_rules_apply(client: AsyncClient, seeded_engine: AsyncEngine) -> None:
    assert (await client.get("/api/v1/devices")).status_code == 422
    assert (await client.get("/api/v1/devices/pos")).status_code == 422
