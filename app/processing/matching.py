# app/processing/matching.py
"""In-memory lookups that match an event to a store and a network element.

Loaded once per run (10 stores, 130 elements), so matching an event is a few
dictionary lookups instead of database queries. Matching order, as agreed:

    1. element by serial number                     matched_by = serial
    2. element by MAC address                       matched_by = device_mac
    3. router / WAN link via gateway + uplink name  matched_by = gateway_uplink
    4. nothing found                                matched_by = none

A matched element must belong to the event's store; otherwise it is treated as
unmatched and never attached to another store's element.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NetworkElement, Store
from app.models.values import MatchedBy
from app.processing.aruba import ExtractedEvent


@dataclass(frozen=True)
class ElementRef:
    id: uuid.UUID
    store_id: uuid.UUID
    type_code: str
    name: str


@dataclass(frozen=True)
class Match:
    element: ElementRef | None
    matched_by: MatchedBy


NO_MATCH = Match(element=None, matched_by=MatchedBy.NONE)


@dataclass
class ElementIndex:
    stores: dict[str, uuid.UUID] = field(default_factory=dict)
    by_serial: dict[str, ElementRef] = field(default_factory=dict)
    by_mac: dict[str, ElementRef] = field(default_factory=dict)
    by_uplink: dict[tuple[uuid.UUID, str, str], ElementRef] = field(default_factory=dict)

    @classmethod
    async def load(cls, session: AsyncSession) -> ElementIndex:
        index = cls()
        for store_id, site_code in (await session.execute(select(Store.id, Store.site_code))).all():
            index.stores[site_code] = store_id

        rows = (
            await session.execute(
                select(
                    NetworkElement.id,
                    NetworkElement.store_id,
                    NetworkElement.element_type_code,
                    NetworkElement.name,
                    NetworkElement.serial_number,
                    NetworkElement.mac_address,
                    NetworkElement.source_gateway_id,
                    NetworkElement.source_uplink_name,
                ).where(NetworkElement.is_active)
            )
        ).all()
        for row in rows:
            ref = ElementRef(
                id=row.id, store_id=row.store_id, type_code=row.element_type_code, name=row.name
            )
            if row.serial_number:
                index.by_serial[row.serial_number.upper()] = ref
            if row.mac_address:
                index.by_mac[str(row.mac_address).lower()] = ref
            if row.source_gateway_id and row.source_uplink_name:
                key = (row.source_gateway_id, row.source_uplink_name.upper(), row.element_type_code)
                index.by_uplink[key] = ref
        return index

    def store_id(self, site_code: str) -> uuid.UUID | None:
        return self.stores.get(site_code)

    def _reporting_element(self, event: ExtractedEvent) -> tuple[ElementRef | None, MatchedBy]:
        if event.device_serial:
            ref = self.by_serial.get(event.device_serial.upper())
            if ref is not None:
                return ref, MatchedBy.SERIAL
        if event.device_mac:
            ref = self.by_mac.get(event.device_mac.lower())
            if ref is not None:
                return ref, MatchedBy.DEVICE_MAC
        return None, MatchedBy.NONE

    def match(self, store_id: uuid.UUID, event: ExtractedEvent) -> Match:
        reporter, matched_by = self._reporting_element(event)
        if reporter is None or reporter.store_id != store_id:
            return NO_MATCH
        if event.target == "element":
            return Match(element=reporter, matched_by=matched_by)

        # Router / WAN link: the reporter must be the gateway that owns the uplink.
        if reporter.type_code != "gateway" or not event.uplink_name:
            return NO_MATCH
        behind = self.by_uplink.get((reporter.id, event.uplink_name.upper(), event.target))
        if behind is None or behind.store_id != store_id:
            return NO_MATCH
        return Match(element=behind, matched_by=MatchedBy.GATEWAY_UPLINK)
