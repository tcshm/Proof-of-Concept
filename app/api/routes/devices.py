# app/api/routes/devices.py
"""GET /api/v1/devices (one tile per device type) and
GET /api/v1/devices/{deviceType} (the devices behind a tile)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from app.api.context import StoreDep
from app.api.deps import SessionDep
from app.api.schemas.common import IncidentRef
from app.api.schemas.devices import DeviceItem, DeviceTile
from app.core.errors import NotFoundError
from app.core.status import connectivity_or_unknown, status_colour, tile_colour
from app.queries.devices import device_tiles, device_type_exists, devices_of_type

router = APIRouter(prefix="/devices", tags=["devices"])

DEVICE_TYPE_PATTERN = r"^[a-z][a-z0-9_]*$"


@router.get("", response_model=list[DeviceTile])
async def list_device_tiles(store: StoreDep, session: SessionDep) -> list[DeviceTile]:
    return [
        DeviceTile(
            device_type=row.code,
            title=row.title,
            status=tile_colour(row.total, row.online, row.offline),
            total=row.total,
            online=row.online,
        )
        for row in await device_tiles(session, store.id)
    ]


@router.get("/{device_type}", response_model=list[DeviceItem])
async def list_devices_of_type(
    store: StoreDep,
    session: SessionDep,
    device_type: Annotated[str, Path(pattern=DEVICE_TYPE_PATTERN, max_length=50)],
) -> list[DeviceItem]:
    if not await device_type_exists(session, device_type):
        raise NotFoundError(f"Device type {device_type} was not found.")
    devices, incidents = await devices_of_type(session, store.id, device_type)
    items = []
    for device in devices:
        connectivity = connectivity_or_unknown(device.connectivity)
        items.append(
            DeviceItem(
                id=device.id,
                name=device.name,
                location=device.location,
                status=status_colour(connectivity),
                connectivity=connectivity,
                status_since=device.status_since,
                status_reason=device.status_reason,
                last_heartbeat_at=device.last_heartbeat_at,
                connection_type=device.connection_type,
                connected_to=device.connected_to,
                port=device.port,
                ip_address=device.ip_address,
                incidents=[
                    IncidentRef(id=i.number, title=i.short_description)
                    for i in incidents.get(device.id, [])
                ],
            )
        )
    return items
