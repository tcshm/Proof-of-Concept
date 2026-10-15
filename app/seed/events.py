# app/seed/events.py
"""Generate the frozen 24-hour window of Aruba events, then replace it in the database.

generate_events() is pure and deterministic: the same seed files and anchor
always give the same events. The Aruba payload shape lives in the _payload_*
functions only, so a change to the real Aruba format is a change here.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NetworkElementCurrentStatus, NetworkEvent, RawNetworkEvent
from app.seed import ids
from app.seed.anchor import parse_offset, window_start
from app.seed.db import insert_many
from app.seed.inventory import ElementInfo, StoreInventory
from app.seed.loader import SeedData

# Envelope constants
SCHEMA_VERSION = "1.0"
SOURCE = "aruba-central-webhook"
VENDOR = "aruba"

# Event types (the envelope's event_type)
DEVICE_STATUS = "aruba.device.status"
GATEWAY_UPLINK = "aruba.gateway.uplink"
WAN_HEALTH = "aruba.gateway.wan_health"
CLIENT_EVENT = "aruba.client.event"

# Aruba's numeric event ids per kind (payload.nid)
_NID = {DEVICE_STATUS: 1301, GATEWAY_UPLINK: 1401, WAN_HEALTH: 1402, CLIENT_EVENT: 1501}

_ARUBA_TYPE = {"access_point": "ACCESS POINT", "switch": "SWITCH", "gateway": "GATEWAY"}
_LABEL = {"access_point": "AP", "switch": "Switch", "gateway": "Gateway"}

CLIENT_SSID = "RIC-STORE"


@dataclass(frozen=True)
class _Draft:
    """One event before event ids are assigned."""

    key: str  # unique per store; source of the Aruba payload id
    event_type: str
    event_time: datetime
    ingestion_time: datetime
    device_id: str | None
    payload: dict[str, Any]


def _status_word(up: bool) -> str:
    return "UP" if up else "DOWN"


def _base_payload(
    event_type: str, aruba_id: str, at: datetime, alert_type: str, up: bool | None
) -> dict[str, Any]:
    if up is None:
        severity, state = "Info", "Close"
    else:
        severity, state = ("Info", "Close") if up else ("Major", "Open")
    return {
        "id": aruba_id,
        "nid": _NID[event_type],
        "alert_type": alert_type,
        "severity": severity,
        "state": state,
        "timestamp": int(at.timestamp()),
    }


def _payload_device_status(
    aruba_id: str, at: datetime, element: ElementInfo, up: bool, site: str
) -> dict[str, Any]:
    label = _LABEL[element.type]
    alert = f"{label} {'connected' if up else 'disconnected'}"
    payload = _base_payload(DEVICE_STATUS, aruba_id, at, alert, up)
    payload["description"] = f"{label} {element.name} is {_status_word(up)}"
    payload["details"] = {
        "serial": element.serial,
        "hostname": element.name,
        "macaddr": element.mac,
        "device_type": _ARUBA_TYPE[element.type],
        "site": site,
        "status": _status_word(up),
    }
    return payload


def _payload_uplink(
    event_type: str,
    aruba_id: str,
    at: datetime,
    gateway: ElementInfo,
    uplink: str,
    up: bool,
    site: str,
) -> dict[str, Any]:
    if event_type == GATEWAY_UPLINK:
        alert = f"Uplink {'up' if up else 'down'}"
        status_field = "link_status"
        extra: dict[str, Any] = {}
    else:
        alert = f"WAN {'healthy' if up else 'down'}"
        status_field = "wan_status"
        extra = {"latency_ms": 28 if up else None, "packet_loss_pct": 0 if up else 100}
    payload = _base_payload(event_type, aruba_id, at, alert, up)
    payload["description"] = f"{gateway.name} {uplink} {status_field} {_status_word(up)}"
    payload["details"] = {
        "serial": gateway.serial,
        "hostname": gateway.name,
        "macaddr": gateway.mac,
        "device_type": "GATEWAY",
        "site": site,
        "uplink": uplink,
        status_field: _status_word(up),
        **extra,
    }
    return payload


def _payload_client(
    aruba_id: str, at: datetime, ap: ElementInfo, client: dict[str, Any], site: str
) -> dict[str, Any]:
    payload = _base_payload(CLIENT_EVENT, aruba_id, at, client["alert_type"], None)
    if client["alert_type"] != "Client connected":
        payload["severity"] = "Minor"
    payload["description"] = f"{client['alert_type']}: {client['hostname']} on {ap.name}"
    payload["details"] = {
        "serial": ap.serial,
        "hostname": ap.name,
        "macaddr": ap.mac,
        "device_type": "ACCESS POINT",
        "site": site,
        "client_mac": client["mac"],
        "client_ip": client["ip"],
        "client_hostname": client["hostname"],
        "username": client.get("username"),
        "user_role": client["user_role"],
        "vlan": client["vlan"],
        "bssid": ap.mac,
        "ssid": CLIENT_SSID,
        "band": "5GHz",
        "channel": client["channel"],
        "rssi": client["rssi"],
        "snr": client["snr"],
        "retry_rate": client["retry_rate"],
        "reason": client.get("reason"),
        "dhcp_latency_ms": client.get("dhcp_latency_ms"),
        "roaming_latency_ms": client.get("roaming_latency_ms"),
    }
    return payload


class _StoreEventBuilder:
    def __init__(self, inventory: StoreInventory, anchor: datetime) -> None:
        self.inventory = inventory
        self.site = inventory.store.site_code
        self.anchor = anchor
        self.rng = random.Random(f"ric-events:{self.site}")
        self.drafts: list[_Draft] = []

    def at(self, offset: str) -> datetime:
        return self.anchor + parse_offset(offset)

    def _ingest(self, event_time: datetime) -> datetime:
        return min(event_time + timedelta(seconds=self.rng.randint(1, 8)), self.anchor)

    def status_event(
        self, element: ElementInfo, up: bool, at: datetime, key: str, ingested: datetime | None
    ) -> _Draft:
        """A status event for any element. Routers and WAN links report through their gateway."""
        aruba_id = str(ids.aruba_event_uuid(self.site, key))
        if element.type in _ARUBA_TYPE:
            event_type = DEVICE_STATUS
            device_id = element.serial
            payload = _payload_device_status(aruba_id, at, element, up, self.site)
        else:
            gateway = self.inventory.element_info[element.gateway_key or ""]
            event_type = GATEWAY_UPLINK if element.type == "router" else WAN_HEALTH
            device_id = gateway.serial
            payload = _payload_uplink(
                event_type, aruba_id, at, gateway, element.uplink_name or "", up, self.site
            )
        draft = _Draft(
            key=key,
            event_type=event_type,
            event_time=at,
            ingestion_time=ingested or self._ingest(at),
            device_id=device_id,
            payload=payload,
        )
        self.drafts.append(draft)
        return draft

    def down_intervals(self, key: str, outages: list[Any]) -> list[tuple[datetime, datetime]]:
        return [
            (self.at(o.down_at), self.at(o.up_at) if o.up_at else self.anchor)
            for o in outages
            if o.element == key
        ]

    def client_events(self, data: SeedData, outages: list[Any], baseline: datetime) -> None:
        low, high = data.scenarios.client_events_per_ap
        devices_by_ap: dict[str, list[dict[str, Any]]] = {}
        for device in self.inventory.devices:
            if device["connection_type"] == "wireless":
                key = device["attributes"]["connected_element_key"]
                devices_by_ap.setdefault(key, []).append(device)

        start = baseline + timedelta(minutes=10)
        span = int((self.anchor - timedelta(minutes=10) - start).total_seconds())
        for key, element in self.inventory.element_info.items():
            if element.type != "access_point":
                continue
            down = self.down_intervals(key, outages)
            for index in range(self.rng.randint(low, high)):
                at = start + timedelta(seconds=self.rng.randint(0, span))
                at = at.replace(microsecond=0)
                if any(begin <= at <= end for begin, end in down):
                    continue
                client = self._client(key, index, devices_by_ap.get(key, []))
                aruba_id = str(ids.aruba_event_uuid(self.site, f"client:{key}:{index}"))
                self.drafts.append(
                    _Draft(
                        key=f"client:{key}:{index}",
                        event_type=CLIENT_EVENT,
                        event_time=at,
                        ingestion_time=self._ingest(at),
                        device_id=element.serial,
                        payload=_payload_client(aruba_id, at, element, client, self.site),
                    )
                )

    def _client(self, ap_key: str, index: int, devices: list[dict[str, Any]]) -> dict[str, Any]:
        rng = self.rng
        if devices:
            device = devices[index % len(devices)]
            identity = {
                "mac": device["mac_address"],
                "ip": device["ip_address"],
                "hostname": device["name"],
                "user_role": device["user_role"],
                "vlan": device["vlan"],
            }
        else:
            identity = {
                "mac": ids.mac_address("client", self.site, ap_key, str(index)),
                "ip": f"10.{10 + index}.150.{100 + index}",
                "hostname": f"Staff-Phone-{index + 1:02d}",
                "user_role": "staff-byod",
                "vlan": "150",
            }
        kind = rng.choices(["connected", "disconnected", "auth_failed"], weights=[5, 3, 2])[0]
        client: dict[str, Any] = {**identity, "channel": str(rng.choice([36, 40, 44, 48]))}
        if kind == "connected":
            client.update(
                alert_type="Client connected",
                rssi=rng.randint(-67, -45),
                snr=rng.randint(28, 45),
                retry_rate=rng.randint(1, 8),
                dhcp_latency_ms=rng.randint(20, 180),
                roaming_latency_ms=rng.randint(15, 60),
            )
        elif kind == "disconnected":
            client.update(
                alert_type="Client disconnected",
                rssi=rng.randint(-84, -76),
                snr=rng.randint(6, 14),
                retry_rate=rng.randint(18, 40),
                reason="Low signal strength",
            )
        else:
            client.update(
                alert_type="Client authentication failed",
                rssi=rng.randint(-70, -55),
                snr=rng.randint(20, 35),
                retry_rate=rng.randint(2, 10),
                reason="RADIUS reject",
            )
        return client


def generate_store_events(
    data: SeedData, inventory: StoreInventory, anchor: datetime
) -> list[dict[str, Any]]:
    builder = _StoreEventBuilder(inventory, anchor)
    site = builder.site
    scenario = data.scenarios.stores.get(site)
    outages = list(scenario.outages) if scenario else []

    # 1. Baseline: every element reports UP once.
    baseline = builder.at(data.scenarios.baseline_at)
    for key, element in inventory.element_info.items():
        at = baseline + timedelta(seconds=builder.rng.randint(0, 300))
        builder.status_event(element, True, at, f"baseline:{key}", None)

    # 2. Outages and recoveries.
    down_drafts: dict[str, _Draft] = {}
    for number, outage in enumerate(outages):
        element = inventory.element_info[outage.element]
        draft = builder.status_event(
            element, False, builder.at(outage.down_at), f"down:{outage.element}:{number}", None
        )
        down_drafts.setdefault(outage.element, draft)
        if outage.up_at:
            builder.status_event(
                element, True, builder.at(outage.up_at), f"up:{outage.element}:{number}", None
            )

    # 3. Client noise on access points (never while the AP is down).
    builder.client_events(data, outages, baseline)

    # 4. Edge cases for the processor.
    if scenario:
        for duplicate in scenario.duplicates:
            original = down_drafts[duplicate.element]
            builder.drafts.append(
                replace(
                    original,
                    key=f"{original.key}:redelivered",
                    ingestion_time=min(original.ingestion_time + timedelta(minutes=2), anchor),
                )
            )
        for number, late in enumerate(scenario.late_events):
            builder.status_event(
                inventory.element_info[late.element],
                late.status == "up",
                builder.at(late.event_at),
                f"late:{late.element}:{number}",
                builder.at(late.ingested_at),
            )
        for number, edge in enumerate(scenario.unmatched):
            at = builder.at(edge.at)
            ghost = ElementInfo(
                key="unmatched",
                type="access_point",
                name=edge.hostname,
                serial=edge.serial,
                mac=ids.mac_address("unmatched", site, edge.serial),
                gateway_key=None,
                uplink_name=None,
            )
            builder.status_event(ghost, True, at, f"unmatched:{number}", None)

    drafts = sorted(builder.drafts, key=lambda d: (d.event_time, d.ingestion_time, d.key))
    country = inventory.store.country_code
    return [
        {
            "event_id": f"evt-{site}-{sequence:04d}",
            "schema_version": SCHEMA_VERSION,
            "event_type": draft.event_type,
            "event_time": draft.event_time,
            "ingestion_time": draft.ingestion_time,
            "source": SOURCE,
            "vendor": VENDOR,
            "device_id": draft.device_id,
            "site_id": site,
            "country": country,
            "incident_id": None,
            "correlation_id": None,
            "payload": draft.payload,
            "processing_status": "pending",
            "processed_at": None,
            "process_error": None,
        }
        for sequence, draft in enumerate(drafts, start=1)
    ]


def generate_events(
    data: SeedData, inventories: list[StoreInventory], anchor: datetime
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for inventory in inventories:
        events.extend(generate_store_events(data, inventory, anchor))
    for event in events:
        assert window_start(anchor) <= event["event_time"] <= anchor, event["event_id"]
    return events


async def write_events(session: AsyncSession, events: list[dict[str, Any]]) -> None:
    """Replace the whole window: derived tables first (foreign keys), then raw events."""
    await session.execute(delete(NetworkElementCurrentStatus))
    await session.execute(delete(NetworkEvent))
    await session.execute(delete(RawNetworkEvent))
    await insert_many(session, RawNetworkEvent, events)
