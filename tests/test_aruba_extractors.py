# tests/test_aruba_extractors.py
"""Aruba payload extractors and status colours. Pure functions: no database."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.status import connectivity_or_unknown, status_colour, tile_colour
from app.processing.aruba import (
    CLIENT_EVENT,
    DEVICE_STATUS,
    GATEWAY_UPLINK,
    WAN_HEALTH,
    ArubaPayloadError,
    UnsupportedEventType,
    extract,
    parse_timestamp,
)

AT = 1791871920  # 2026-10-13T06:12:00Z


def _payload(details: dict, **top: object) -> dict:
    return {
        "id": "b3f1c9e0-6a2d-5c1e-9d77-0d5a4c2e8f10",
        "nid": 1301,
        "alert_type": "AP disconnected",
        "severity": "Major",
        "state": "Open",
        "timestamp": AT,
        "description": "AP AP01234N004 is DOWN",
        "details": details,
        **top,
    }


AP_DETAILS = {
    "serial": "CN01234AP004",
    "hostname": "AP01234N004",
    "macaddr": "02:1A:2B:3C:4D:5E",
    "device_type": "ACCESS POINT",
    "site": "01234",
    "status": "DOWN",
}


def test_device_status() -> None:
    event = extract(DEVICE_STATUS, _payload(AP_DETAILS))
    assert event.target == "element"
    assert event.vendor_event_uuid == "b3f1c9e0-6a2d-5c1e-9d77-0d5a4c2e8f10"
    assert event.vendor_event_number == "1301"
    assert event.vendor_event_type == "AP disconnected"
    assert event.level == "Major"
    assert event.device_serial == "CN01234AP004"
    assert event.hostname == "AP01234N004"
    assert event.device_mac == "02:1a:2b:3c:4d:5e"
    assert event.aruba_device_type == "ACCESS POINT"
    assert event.reported_status == "offline"
    assert event.uplink_name is None
    # Fields without a column are kept in details.
    assert event.details == {
        "state": "Open",
        "aruba_timestamp": "2026-10-13T06:12:00+00:00",
        "site": "01234",
    }


def test_up_is_online() -> None:
    event = extract(DEVICE_STATUS, _payload({**AP_DETAILS, "status": "up"}))
    assert event.reported_status == "online"


def test_gateway_uplink_targets_router() -> None:
    details = {"serial": "CN01234GW001", "uplink": "WAN1", "link_status": "UP"}
    event = extract(GATEWAY_UPLINK, _payload(details, alert_type="Uplink up"))
    assert event.target == "router"
    assert event.uplink_name == "WAN1"
    assert event.reported_status == "online"


def test_wan_health_targets_wan_link_and_keeps_metrics() -> None:
    details = {
        "serial": "CN01234GW001",
        "uplink": "WAN1",
        "wan_status": "DOWN",
        "latency_ms": None,
        "packet_loss_pct": 100,
    }
    event = extract(WAN_HEALTH, _payload(details, alert_type="WAN down"))
    assert event.target == "wan_link"
    assert event.reported_status == "offline"
    assert event.details["packet_loss_pct"] == 100
    assert "latency_ms" not in event.details  # None values are dropped


def test_client_event_has_no_status() -> None:
    details = {
        "serial": "CN01234AP002",
        "hostname": "AP01234N002",
        "client_mac": "02:AA:BB:CC:DD:EE",
        "client_ip": "10.10.130.11",
        "client_hostname": "RF Gun 01",
        "user_role": "scanner-device",
        "vlan": "130",
        "ssid": "RIC-STORE",
        "channel": "36",
        "rssi": -62,
        "snr": "31",
        "retry_rate": 4,
        "reason": None,
    }
    event = extract(CLIENT_EVENT, _payload(details, alert_type="Client connected"))
    assert event.reported_status is None
    assert event.client["client_mac"] == "02:aa:bb:cc:dd:ee"
    assert event.client["client_hostname"] == "RF Gun 01"
    assert event.client["rssi"] == -62
    assert event.client["snr"] == 31
    assert event.client["reason"] is None
    assert event.client["band"] is None
    assert "rssi" not in event.details


def test_timestamps_in_seconds_or_milliseconds() -> None:
    expected = datetime(2026, 10, 13, 6, 12, tzinfo=UTC)
    assert parse_timestamp(AT) == expected
    assert parse_timestamp(AT * 1000) == expected
    assert parse_timestamp(str(AT)) == expected
    assert parse_timestamp(None) is None


@pytest.mark.parametrize(
    ("event_type", "payload", "message"),
    [
        ("aruba.unknown.kind", _payload(AP_DETAILS), "unsupported event type"),
        (DEVICE_STATUS, "not a dict", "JSON object"),
        (DEVICE_STATUS, {"id": "x"}, "details must be a JSON object"),
        (DEVICE_STATUS, _payload({**AP_DETAILS, "status": "FLAPPING"}), "UP or DOWN"),
        (DEVICE_STATUS, _payload({"status": "UP"}), "both missing"),
        (GATEWAY_UPLINK, _payload({"serial": "GW", "link_status": "UP"}), "uplink is missing"),
        (CLIENT_EVENT, _payload({"serial": "AP", "rssi": "strong"}), "expected a number"),
    ],
)
def test_bad_payloads_are_rejected(event_type: str, payload: object, message: str) -> None:
    with pytest.raises(ArubaPayloadError, match=message):
        extract(event_type, payload)


def test_unsupported_type_is_a_payload_error() -> None:
    assert issubclass(UnsupportedEventType, ArubaPayloadError)


# --- status colours ------------------------------------------------------------
def test_status_colours() -> None:
    assert status_colour("online") == "green"
    assert status_colour("degraded") == "yellow"
    assert status_colour("unknown") == "yellow"
    assert status_colour("offline") == "red"
    assert status_colour(None) == "yellow"
    assert connectivity_or_unknown(None) == "unknown"


@pytest.mark.parametrize(
    ("total", "online", "offline", "expected"),
    [
        (4, 4, 0, "green"),
        (4, 0, 4, "red"),
        (4, 3, 1, "yellow"),
        (3, 2, 0, "yellow"),  # one degraded or unknown device
        (0, 0, 0, "yellow"),
    ],
)
def test_tile_colour(total: int, online: int, offline: int, expected: str) -> None:
    assert tile_colour(total, online, offline) == expected
