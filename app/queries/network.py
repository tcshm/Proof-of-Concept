# app/queries/network.py
"""Data for GET /api/v1/network: one store's network elements with their status."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Device,
    Incident,
    NetworkElement,
    NetworkElementCurrentStatus,
    NetworkElementType,
)
from app.models.values import ConnectionType
from app.queries.common import IncidentRefRow, open_incidents_by_ci


@dataclass(frozen=True)
class ElementRow:
    id: uuid.UUID
    name: str
    role: str | None
    location: str | None
    uplink_port: str | None
    type_code: str
    source_uplink_name: str | None
    attributes: dict[str, Any]
    connectivity: str | None
    status_since: datetime | None


@dataclass(frozen=True)
class WiredDeviceRow:
    id: uuid.UUID
    name: str
    connectivity: str


@dataclass(frozen=True)
class StoreNetwork:
    elements: list[ElementRow]
    wired_devices: list[WiredDeviceRow]
    ap_incidents: dict[uuid.UUID, list[IncidentRefRow]]


async def load_store_network(session: AsyncSession, store_id: uuid.UUID) -> StoreNetwork:
    element_rows = (
        await session.execute(
            select(
                NetworkElement.id,
                NetworkElement.name,
                NetworkElement.role,
                NetworkElement.location_in_store,
                NetworkElement.uplink_port,
                NetworkElement.element_type_code,
                NetworkElement.source_uplink_name,
                NetworkElement.attributes,
                NetworkElementCurrentStatus.status,
                NetworkElementCurrentStatus.status_since,
            )
            .join(NetworkElementType, NetworkElementType.code == NetworkElement.element_type_code)
            .outerjoin(
                NetworkElementCurrentStatus,
                NetworkElementCurrentStatus.network_element_id == NetworkElement.id,
            )
            .where(NetworkElement.store_id == store_id, NetworkElement.is_active)
            .order_by(NetworkElementType.sort_order, NetworkElement.name)
        )
    ).all()
    elements = [
        ElementRow(
            id=row.id,
            name=row.name,
            role=row.role,
            location=row.location_in_store,
            uplink_port=row.uplink_port,
            type_code=row.element_type_code,
            source_uplink_name=row.source_uplink_name,
            attributes=row.attributes or {},
            connectivity=row.status,
            status_since=row.status_since,
        )
        for row in element_rows
    ]

    device_rows = (
        await session.execute(
            select(Device.id, Device.name, Device.status)
            .where(
                Device.store_id == store_id,
                Device.is_active,
                Device.connection_type == ConnectionType.WIRED,
            )
            .order_by(Device.name)
        )
    ).all()
    wired_devices = [
        WiredDeviceRow(id=row.id, name=row.name, connectivity=row.status) for row in device_rows
    ]

    ap_ids = [e.id for e in elements if e.type_code == "access_point"]
    ap_incidents = await open_incidents_by_ci(
        session, store_id, Incident.cmdb_ci_network_element_id, ap_ids
    )
    return StoreNetwork(elements=elements, wired_devices=wired_devices, ap_incidents=ap_incidents)
