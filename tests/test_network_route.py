# tests/test_network_route.py
"""GET /api/v1/network on the seeded and processed database."""

from __future__ import annotations

from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import Incident, NetworkElement
from app.seed import ids
from tests.conftest import SEED_ANCHOR

CHENNAI = {"X-Site-Id": "01234"}


async def _network(client: AsyncClient, headers: dict[str, str] = CHENNAI) -> dict:
    response = await client.get("/api/v1/network", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_chennai_network(client: AsyncClient, seeded_engine: AsyncEngine) -> None:
    body = await _network(client)
    assert set(body) == {"wiredTiles", "wanLinks", "wiredConnections", "accessPoints"}

    tiles = {t["label"]: t for t in body["wiredTiles"]}
    assert list(tiles) == [
        "RN01234N001-1",
        "RN01234N001-2",
        "SW01234N301",
        "SW01234N302",
        "SW01234N303",
    ]
    assert tiles["RN01234N001-1"]["status"] == "green"
    assert tiles["RN01234N001-1"]["elementType"] == "router"
    assert tiles["SW01234N302"]["sub"] == "Access switch - front of store"

    wan = body["wanLinks"]
    assert [w["name"] for w in wan] == ["WAN01234-PRI", "WAN01234-BKP"]
    assert wan[0]["status"] == "red" and wan[0]["connectivity"] == "offline"
    assert wan[0]["provider"] == "AT&T"
    assert wan[0]["statusSince"] == "2026-10-13T07:30:00Z"
    assert wan[1]["status"] == "green" and wan[1]["provider"] == "Airtel"

    aps = {a["name"]: a for a in body["accessPoints"]}
    assert len(aps) == 5
    ap4 = aps["AP01234N004"]
    assert ap4 == {
        "id": str(ids.network_element_id("01234", "AP01234N004")),
        "name": "AP01234N004",
        "location": "Sales Floor",
        "port": "11",
        "status": "red",
        "connectivity": "offline",
        "statusSince": "2026-10-13T06:40:00Z",
        "incidents": [],
    }
    assert all(a["status"] == "green" for name, a in aps.items() if name != "AP01234N004")


async def test_wired_connections(client: AsyncClient, seeded_engine: AsyncEngine) -> None:
    connections = (await _network(client))["wiredConnections"]
    elements = [c for c in connections if c["kind"] == "network_element"]
    devices = [c for c in connections if c["kind"] == "device"]
    assert [c["name"] for c in elements] == [
        "GW01234N001",
        "SW01234N301",
        "SW01234N302",
        "SW01234N303",
    ]
    # 13 wired devices (4 POS, 4 registers, 3 printers, 2 workstations), by name.
    assert len(devices) == 13
    assert [d["name"] for d in devices] == sorted(d["name"] for d in devices)
    pos3 = next(d for d in devices if d["name"] == "POS Terminal 03")
    assert (pos3["status"], pos3["connectivity"]) == ("red", "offline")
    assert connections.index(elements[-1]) < connections.index(devices[0])


async def test_open_incidents_on_access_points(
    client: AsyncClient, seeded_engine: AsyncEngine
) -> None:
    ap4 = ids.network_element_id("01234", "AP01234N004")
    common = {
        "store_id": ids.store_id("01234"),
        "category": "network",
        "contact_type": "monitoring",
        "cmdb_ci_network_element_id": ap4,
    }
    async with seeded_engine.begin() as connection:
        await connection.execute(
            insert(Incident).values(
                **common,
                number="INC00001237",
                short_description="Wireless - Access Point A004",
                impact=2,
                urgency=2,
                state=1,
                opened_at=SEED_ANCHOR - timedelta(hours=2.2),
            )
        )
        await connection.execute(
            insert(Incident).values(
                **common,
                number="INC00009999",
                short_description="Old AP incident",
                impact=3,
                urgency=3,
                state=7,
                opened_at=SEED_ANCHOR - timedelta(days=5),
                resolved_at=SEED_ANCHOR - timedelta(days=4),
                closed_at=SEED_ANCHOR - timedelta(days=3),
            )
        )
    aps = {a["name"]: a for a in (await _network(client))["accessPoints"]}
    assert aps["AP01234N004"]["incidents"] == [
        {"id": "INC00001237", "title": "Wireless - Access Point A004"}
    ]  # the closed incident is not listed
    assert aps["AP01234N001"]["incidents"] == []


async def test_stores_never_see_each_other(client: AsyncClient, seeded_engine: AsyncEngine) -> None:
    paris = await _network(client, {"X-Site-Id": "02556"})
    names = [t["label"] for t in paris["wiredTiles"]] + [a["name"] for a in paris["accessPoints"]]
    assert names and all("02556" in name for name in names)
    offline = {a["name"] for a in paris["accessPoints"] if a["status"] == "red"}
    assert offline == {"AP02556N003", "AP02556N005"}


async def test_unknown_status_is_yellow(client: AsyncClient, seeded_engine: AsyncEngine) -> None:
    # An element added after the last processing run has no status row yet.
    async with seeded_engine.begin() as connection:
        await connection.execute(
            insert(NetworkElement).values(
                store_id=ids.store_id("01234"),
                element_type_code="access_point",
                name="AP01234N006",
                serial_number="CN01234AP006",
                uplink_element_id=(
                    await connection.scalar(
                        select(NetworkElement.id).where(NetworkElement.name == "SW01234N303")
                    )
                ),
                uplink_port="25",
            )
        )
    aps = {a["name"]: a for a in (await _network(client))["accessPoints"]}
    assert aps["AP01234N006"]["status"] == "yellow"
    assert aps["AP01234N006"]["connectivity"] == "unknown"
    assert aps["AP01234N006"]["statusSince"] is None


async def test_store_header_rules_apply(client: AsyncClient, seeded_engine: AsyncEngine) -> None:
    assert (await client.get("/api/v1/network")).status_code == 422
    assert (await client.get("/api/v1/network", headers={"X-Site-Id": "99999"})).status_code == 404
