# app/queries/devices.py
"""Data for GET /api/v1/devices and GET /api/v1/devices/{deviceType}."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import Device, DeviceType, Incident, NetworkElement
from app.models.values import DeviceStatus
from app.queries.common import IncidentRefRow, open_incidents_by_ci


@dataclass(frozen=True)
class DeviceTileRow:
    code: str
    title: str
    total: int
    online: int
    offline: int


@dataclass(frozen=True)
class DeviceRow:
    id: uuid.UUID
    name: str
    location: str | None
    connectivity: str
    status_since: datetime
    status_reason: str | None
    last_heartbeat_at: datetime | None
    connection_type: str | None
    connected_to: str | None
    port: str | None
    ip_address: str | None


# Problems first: offline, degraded, unknown, then online.
_STATUS_ORDER = case(
    (Device.status == DeviceStatus.OFFLINE, 0),
    (Device.status == DeviceStatus.DEGRADED, 1),
    (Device.status == DeviceStatus.UNKNOWN, 2),
    else_=3,
)


async def device_tiles(session: AsyncSession, store_id: uuid.UUID) -> list[DeviceTileRow]:
    rows = (
        await session.execute(
            select(
                DeviceType.code,
                DeviceType.display_name,
                func.count(Device.id),
                func.count(Device.id).filter(Device.status == DeviceStatus.ONLINE),
                func.count(Device.id).filter(Device.status == DeviceStatus.OFFLINE),
            )
            .join(Device, Device.device_type_code == DeviceType.code)
            .where(Device.store_id == store_id, Device.is_active)
            .group_by(DeviceType.code, DeviceType.display_name, DeviceType.sort_order)
            .order_by(DeviceType.sort_order, DeviceType.code)
        )
    ).all()
    return [
        DeviceTileRow(code=code, title=title, total=total, online=online, offline=offline)
        for code, title, total, online, offline in rows
    ]


async def device_type_exists(session: AsyncSession, code: str) -> bool:
    return await session.scalar(select(DeviceType.code).where(DeviceType.code == code)) is not None


async def devices_of_type(
    session: AsyncSession, store_id: uuid.UUID, device_type: str
) -> tuple[list[DeviceRow], dict[uuid.UUID, list[IncidentRefRow]]]:
    connected = aliased(NetworkElement)
    rows = (
        await session.execute(
            select(
                Device.id,
                Device.name,
                Device.location_in_store,
                Device.status,
                Device.status_since,
                Device.status_reason,
                Device.last_heartbeat_at,
                Device.connection_type,
                connected.name.label("connected_to"),
                Device.connected_port,
                Device.ip_address,
            )
            .outerjoin(connected, connected.id == Device.connected_network_element_id)
            .where(
                Device.store_id == store_id,
                Device.device_type_code == device_type,
                Device.is_active,
            )
            .order_by(_STATUS_ORDER, Device.name)
        )
    ).all()
    devices = [
        DeviceRow(
            id=row.id,
            name=row.name,
            location=row.location_in_store,
            connectivity=row.status,
            status_since=row.status_since,
            status_reason=row.status_reason,
            last_heartbeat_at=row.last_heartbeat_at,
            connection_type=row.connection_type,
            connected_to=row.connected_to,
            port=row.connected_port,
            ip_address=None if row.ip_address is None else str(row.ip_address),
        )
        for row in rows
    ]
    incidents = await open_incidents_by_ci(
        session, store_id, Incident.cmdb_ci_device_id, [d.id for d in devices]
    )
    return devices, incidents
