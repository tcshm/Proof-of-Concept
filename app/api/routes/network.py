# app/api/routes/network.py
"""GET /api/v1/network: the store's network elements and their status."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.context import StoreDep
from app.api.deps import SessionDep
from app.api.schemas.common import IncidentRef
from app.api.schemas.network import (
    AccessPoint,
    NetworkResponse,
    WanLink,
    WiredConnection,
    WiredTile,
)
from app.core.status import connectivity_or_unknown, status_colour
from app.queries.network import ElementRow, load_store_network

router = APIRouter(prefix="/network", tags=["network"])

_TILE_TYPES = ("router", "switch")
_WIRED_ELEMENT_TYPES = ("gateway", "switch")


def _uplink_order(element: ElementRow) -> str:
    """WAN1 (primary) before WAN2 (backup)."""
    return element.source_uplink_name or element.name


@router.get("", response_model=NetworkResponse)
async def get_network(store: StoreDep, session: SessionDep) -> NetworkResponse:
    data = await load_store_network(session, store.id)

    def colour(element: ElementRow) -> tuple[str, str]:
        connectivity = connectivity_or_unknown(element.connectivity)
        return status_colour(connectivity), connectivity

    wired_tiles = []
    wan_links = []
    wired_connections = []
    access_points = []
    for element in data.elements:
        status, connectivity = colour(element)
        if element.type_code in _TILE_TYPES:
            wired_tiles.append(
                WiredTile(
                    id=element.id,
                    label=element.name,
                    sub=element.role,
                    element_type=element.type_code,
                    status=status,
                    connectivity=connectivity,
                    status_since=element.status_since,
                )
            )
        if element.type_code in _WIRED_ELEMENT_TYPES:
            wired_connections.append(
                WiredConnection(
                    id=element.id,
                    name=element.name,
                    kind="network_element",
                    status=status,
                    connectivity=connectivity,
                )
            )
        if element.type_code == "access_point":
            access_points.append(
                AccessPoint(
                    id=element.id,
                    name=element.name,
                    location=element.location,
                    port=element.uplink_port,
                    status=status,
                    connectivity=connectivity,
                    status_since=element.status_since,
                    incidents=[
                        IncidentRef(id=i.number, title=i.short_description)
                        for i in data.ap_incidents.get(element.id, [])
                    ],
                )
            )

    for element in sorted(
        (e for e in data.elements if e.type_code == "wan_link"), key=_uplink_order
    ):
        status, connectivity = colour(element)
        provider = element.attributes.get("provider")
        wan_links.append(
            WanLink(
                id=element.id,
                name=element.name,
                role=element.role,
                provider=provider if isinstance(provider, str) else None,
                status=status,
                connectivity=connectivity,
                status_since=element.status_since,
            )
        )

    for device in data.wired_devices:
        connectivity = connectivity_or_unknown(device.connectivity)
        wired_connections.append(
            WiredConnection(
                id=device.id,
                name=device.name,
                kind="device",
                status=status_colour(connectivity),
                connectivity=connectivity,
            )
        )

    return NetworkResponse(
        wired_tiles=wired_tiles,
        wan_links=wan_links,
        wired_connections=wired_connections,
        access_points=access_points,
    )
