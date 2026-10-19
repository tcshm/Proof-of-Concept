# app/api/schemas/network.py
"""Response model for GET /api/v1/network."""

from __future__ import annotations

import uuid
from typing import Literal

from app.api.schemas.base import ApiModel, UtcDateTime
from app.api.schemas.common import Colour, IncidentRef


class WiredTile(ApiModel):
    """Routers and switches."""

    id: uuid.UUID
    label: str
    sub: str | None
    element_type: str
    status: Colour
    connectivity: str
    status_since: UtcDateTime | None


class WanLink(ApiModel):
    id: uuid.UUID
    name: str
    role: str | None
    provider: str | None
    status: Colour
    connectivity: str
    status_since: UtcDateTime | None


class WiredConnection(ApiModel):
    """A wired network element (gateway, switch) or a wired store device."""

    id: uuid.UUID
    name: str
    kind: Literal["network_element", "device"]
    status: Colour
    connectivity: str


class AccessPoint(ApiModel):
    id: uuid.UUID
    name: str
    location: str | None
    port: str | None
    status: Colour
    connectivity: str
    status_since: UtcDateTime | None
    incidents: list[IncidentRef]


class NetworkResponse(ApiModel):
    wired_tiles: list[WiredTile]
    wan_links: list[WanLink]
    wired_connections: list[WiredConnection]
    access_points: list[AccessPoint]
