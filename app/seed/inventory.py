# app/seed/inventory.py
"""Expand the inventory template for every store, then upsert it.

build_inventory() is pure: seed files + anchor in, rows out. write_inventory()
only writes the rows it is given, inside the caller's transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, NetworkElement
from app.models.values import DeviceStatus
from app.seed import ids
from app.seed.anchor import parse_offset
from app.seed.db import upsert
from app.seed.loader import SeedData
from app.seed.schemas import ElementTemplate, StoreSeed

ONLINE_SINCE = timedelta(days=3)
HEARTBEAT_AGE = timedelta(minutes=1)


@dataclass(frozen=True)
class ElementInfo:
    """What the event generator needs to know about one network element."""

    key: str
    type: str
    name: str
    serial: str | None
    mac: str | None
    gateway_key: str | None
    uplink_name: str | None


@dataclass
class StoreInventory:
    store: StoreSeed
    store_id: uuid.UUID
    elements: list[dict[str, Any]] = field(default_factory=list)
    devices: list[dict[str, Any]] = field(default_factory=list)
    element_info: dict[str, ElementInfo] = field(default_factory=dict)


def store_vars(store: StoreSeed, position: int) -> dict[str, Any]:
    return {
        "site": store.site_code,
        "net": 10 + position,
        "isp_primary": store.isp_primary,
        "isp_backup": store.isp_backup,
    }


def _fmt(value: str | None, variables: dict[str, Any]) -> str | None:
    return None if value is None else value.format(**variables)


def _fmt_attributes(attributes: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
    return {k: v.format(**variables) if isinstance(v, str) else v for k, v in attributes.items()}


def _element_row(
    element: ElementTemplate,
    store: StoreSeed,
    sid: uuid.UUID,
    variables: dict[str, Any],
    names: dict[str, str],
) -> dict[str, Any]:
    site = store.site_code
    name = _fmt(element.name, variables)
    serial = _fmt(element.serial, variables)
    is_aruba = serial is not None

    def ref(key: str | None) -> uuid.UUID | None:
        return None if key is None else ids.network_element_id(site, names[key])

    return {
        "id": ids.network_element_id(site, name),
        "store_id": sid,
        "element_type_code": element.type,
        "name": name,
        "role": element.role,
        "location_in_store": element.location,
        "serial_number": serial,
        "mac_address": ids.mac_address("network_element", site, name) if is_aruba else None,
        "ip_address": _fmt(element.ip, variables),
        "manufacturer": element.manufacturer,
        "model": element.model,
        "uplink_element_id": ref(element.uplink),
        "uplink_port": element.uplink_port,
        "source_gateway_id": ref(element.source_gateway),
        "source_uplink_name": element.source_uplink,
        "is_active": True,
        "attributes": _fmt_attributes(element.attributes, variables),
    }


def build_store_inventory(
    data: SeedData, store: StoreSeed, position: int, anchor: datetime
) -> StoreInventory:
    site = store.site_code
    inventory = StoreInventory(store=store, store_id=ids.store_id(site))
    variables = store_vars(store, position)

    names = {e.key: _fmt(e.name, variables) for e in data.template.network_elements}
    for element in data.template.network_elements:
        row = _element_row(element, store, inventory.store_id, variables, names)
        inventory.elements.append(row)
        inventory.element_info[element.key] = ElementInfo(
            key=element.key,
            type=element.type,
            name=row["name"],
            serial=row["serial_number"],
            mac=row["mac_address"],
            gateway_key=element.source_gateway,
            uplink_name=element.source_uplink,
        )

    overrides = data.overrides.stores.get(site)
    for template in data.template.devices:
        targets = template.connection_keys
        for n in range(1, template.count + 1):
            device_vars = {**variables, "n": n, "h": 10 + n}
            name = template.name.format(**device_vars)
            target_key = targets[(n - 1) % len(targets)]
            port = None
            if template.port_start is not None:
                port = str(template.port_start + n - 1)

            status = DeviceStatus.ONLINE
            since = anchor - ONLINE_SINCE
            reason = None
            override = overrides.devices.get(name) if overrides else None
            if override is not None:
                status = override.status
                since = anchor + parse_offset(override.since)
                reason = override.reason
            heartbeat = (
                anchor - HEARTBEAT_AGE
                if status in (DeviceStatus.ONLINE, DeviceStatus.DEGRADED)
                else since
            )

            inventory.devices.append(
                {
                    "id": ids.device_id(site, name),
                    "store_id": inventory.store_id,
                    "device_type_code": template.type,
                    "name": name,
                    "location_in_store": _fmt(template.location, device_vars),
                    "serial_number": template.serial.format(**device_vars),
                    "mac_address": ids.mac_address("device", site, name),
                    "ip_address": _fmt(template.ip, device_vars),
                    "manufacturer": template.manufacturer,
                    "model": template.model,
                    "connection_type": template.connection.value,
                    "connected_network_element_id": ids.network_element_id(site, names[target_key]),
                    "connected_port": port,
                    "user_role": template.user_role,
                    "vlan": template.vlan,
                    "status": str(status),
                    "status_since": since,
                    "status_reason": reason,
                    "last_heartbeat_at": heartbeat,
                    "is_active": True,
                    "attributes": {"connected_element_key": target_key},
                }
            )
    return inventory


def build_inventory(data: SeedData, anchor: datetime) -> list[StoreInventory]:
    return [
        build_store_inventory(data, store, position, anchor)
        for position, store in enumerate(data.stores.stores)
    ]


async def write_inventory(session: AsyncSession, inventories: list[StoreInventory]) -> None:
    """Upsert elements, then devices, one store at a time.

    All elements of a store go in one INSERT, so an element can reference
    another element of the same store (uplink, source gateway).
    """
    for inventory in inventories:
        await upsert(session, NetworkElement, inventory.elements)
        await upsert(session, Device, inventory.devices)
