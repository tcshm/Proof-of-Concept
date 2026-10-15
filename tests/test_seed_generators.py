# tests/test_seed_generators.py
"""Pure generator tests: no database needed."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.seed.anchor import window_start
from app.seed.events import CLIENT_EVENT, generate_events
from app.seed.inventory import StoreInventory, build_inventory
from app.seed.loader import SeedData, load_seed_data

ANCHOR = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def data() -> SeedData:
    return load_seed_data()


@pytest.fixture(scope="module")
def inventories(data: SeedData) -> list[StoreInventory]:
    return build_inventory(data, ANCHOR)


@pytest.fixture(scope="module")
def events(data: SeedData, inventories: list[StoreInventory]) -> list[dict[str, Any]]:
    return generate_events(data, inventories, ANCHOR)


def _chennai(inventories: list[StoreInventory]) -> StoreInventory:
    return next(i for i in inventories if i.store.site_code == "01234")


def _final_status(events: list[dict[str, Any]], site: str) -> dict[str, str]:
    """Latest reported status per element by event time, as the processor will compute it.

    Device status events identify the element by serial; uplink and WAN health
    events by gateway serial + uplink name.
    """
    latest: dict[str, tuple[datetime, str]] = {}
    for event in events:
        if event["site_id"] != site or event["event_type"] == CLIENT_EVENT:
            continue
        details = event["payload"]["details"]
        if "uplink" in details:
            kind = "link" if "link_status" in details else "wan"
            key = f"{details['hostname']}/{details['uplink']}/{kind}"
            status = details.get("link_status") or details.get("wan_status")
        else:
            key = details["hostname"]
            status = details["status"]
        if key not in latest or event["event_time"] >= latest[key][0]:
            latest[key] = (event["event_time"], status)
    return {key: status for key, (_, status) in latest.items()}


# --- inventory ---------------------------------------------------------------
def test_every_store_has_full_inventory(inventories: list[StoreInventory]) -> None:
    assert len(inventories) == 10
    for inventory in inventories:
        assert len(inventory.elements) == 13
        assert len(inventory.devices) == 17
        types = Counter(d["device_type_code"] for d in inventory.devices)
        assert types == {"pos": 4, "register": 4, "rf_gun": 4, "printer": 3, "workstation": 2}


def test_references_stay_inside_the_store(inventories: list[StoreInventory]) -> None:
    for inventory in inventories:
        element_ids = {row["id"] for row in inventory.elements}
        for row in inventory.elements:
            for ref in (row["uplink_element_id"], row["source_gateway_id"]):
                assert ref is None or ref in element_ids
        for device in inventory.devices:
            assert device["connected_network_element_id"] in element_ids


def test_serials_and_macs_unique_across_stores(inventories: list[StoreInventory]) -> None:
    rows = [r for i in inventories for r in i.elements + i.devices]
    serials = [r["serial_number"] for r in rows if r["serial_number"]]
    macs = [r["mac_address"] for r in rows if r["mac_address"]]
    assert len(serials) == len(set(serials))
    assert len(macs) == len(set(macs))


def test_chennai_matches_mobile_mock(inventories: list[StoreInventory]) -> None:
    chennai = _chennai(inventories)
    devices = {d["name"]: d for d in chennai.devices}

    pos = devices["POS Terminal 03"]  # INC00001235, reported 6 h ago
    assert pos["status"] == "offline"
    assert pos["location_in_store"] == "Checkout 3"
    assert ANCHOR - pos["status_since"] > timedelta(hours=6)

    register = devices["Register 04"]  # INC00001236, reported 12 h ago
    assert register["status"] == "offline"
    assert register["location_in_store"] == "Checkout 4"
    assert ANCHOR - register["status_since"] > timedelta(hours=12)

    others = [d for name, d in devices.items() if name not in ("POS Terminal 03", "Register 04")]
    assert all(d["status"] == "online" for d in others)

    elements = {row["name"]: row for row in chennai.elements}
    assert elements["AP01234N004"]["location_in_store"] == "Sales Floor"
    assert elements["WAN01234-PRI"]["attributes"]["provider"] == "AT&T"
    assert {"RN01234N001-1", "SW01234N301", "GW01234N001"} <= set(elements)


# --- events ------------------------------------------------------------------
def test_events_are_deterministic(data: SeedData, inventories, events) -> None:
    assert generate_events(data, inventories, ANCHOR) == events


def test_new_anchor_only_shifts_times(data: SeedData, events) -> None:
    later = ANCHOR + timedelta(days=2)
    shifted = generate_events(data, build_inventory(data, later), later)
    assert [e["event_id"] for e in shifted] == [e["event_id"] for e in events]
    assert [e["payload"]["id"] for e in shifted] == [e["payload"]["id"] for e in events]
    for old, new in zip(events, shifted, strict=True):
        assert new["event_time"] - old["event_time"] == timedelta(days=2)


def test_events_inside_window(events) -> None:
    for event in events:
        assert window_start(ANCHOR) <= event["event_time"] <= ANCHOR
        assert event["event_time"] <= event["ingestion_time"] <= ANCHOR
        assert event["processing_status"] == "pending"
    assert len({e["event_id"] for e in events}) == len(events)


def test_status_events_match_inventory(inventories, events) -> None:
    serials = {row["serial_number"] for i in inventories for row in i.elements}
    unmatched = [e for e in events if e["device_id"] not in serials]
    assert [e["device_id"] for e in unmatched] == ["CN03562AP099"]


def test_edge_cases_exist_exactly_once(events) -> None:
    payload_ids = Counter(e["payload"]["id"] for e in events)
    duplicates = [pid for pid, count in payload_ids.items() if count > 1]
    assert len(duplicates) == 1
    late = [e for e in events if e["ingestion_time"] - e["event_time"] > timedelta(hours=1)]
    assert len(late) == 1
    assert late[0]["device_id"] == "CN01234AP004"


def test_chennai_final_network_status(events) -> None:
    status = _final_status(events, "01234")
    assert status["GW01234N001/WAN1/wan"] == "DOWN"  # INC00001234 primary link
    assert status["GW01234N001/WAN2/wan"] == "UP"  # store running on backup
    assert status["GW01234N001/WAN1/link"] == "UP"  # router RN01234N001-1 stays green
    assert status["GW01234N001/WAN2/link"] == "UP"
    assert status["AP01234N004"] == "DOWN"  # INC00001237
    assert status["SW01234N302"] == "UP"  # flapped overnight, recovered
    assert status["GW01234N001"] == "UP"


def test_status_change_precedes_mock_incidents(events) -> None:
    """The network fault must happen before the incident was reported in the mock."""
    chennai = [e for e in events if e["site_id"] == "01234"]
    wan_down = next(e for e in chennai if e["payload"]["alert_type"] == "WAN down")
    ap_down = next(e for e in chennai if e["payload"]["alert_type"] == "AP disconnected")
    assert ANCHOR - wan_down["event_time"] > timedelta(hours=1.4)
    assert ANCHOR - ap_down["event_time"] > timedelta(hours=2.2)
