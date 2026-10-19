# app/api/schemas/devices.py
"""Response models for GET /api/v1/devices and GET /api/v1/devices/{deviceType}."""

from __future__ import annotations

import uuid

from app.api.schemas.base import ApiModel, UtcDateTime
from app.api.schemas.common import Colour, IncidentRef


class DeviceTile(ApiModel):
    device_type: str
    title: str
    status: Colour
    total: int
    online: int


class DeviceItem(ApiModel):
    id: uuid.UUID
    name: str
    location: str | None
    status: Colour
    connectivity: str
    status_since: UtcDateTime
    status_reason: str | None
    last_heartbeat_at: UtcDateTime | None
    connection_type: str | None
    connected_to: str | None
    port: str | None
    ip_address: str | None
    incidents: list[IncidentRef]
