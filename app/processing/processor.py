# app/processing/processor.py
"""Process ONE raw event. The batch loop (runner.py) and a future live feed both
call process_event(); the rules live only here.

    1. dedupe       same Aruba event id already processed      -> duplicate
    2. store        site_id not a known store                  -> unmatched
    3. extract      unsupported type or malformed payload      -> error
    4. match        element by serial / MAC / gateway uplink
    5. insert       one network_events row (inside a savepoint)
    6. apply        newer status report -> update current status
    7. outcome      processed, or unmatched when no element matched

The raw row's processing columns are not written here; the caller collects the
outcomes and writes them in bulk.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NetworkEvent
from app.models.values import RawProcessingStatus
from app.processing.aruba import ArubaPayloadError, extract
from app.processing.matching import ElementIndex
from app.processing.status import StatusBook

_MAX_ERROR = 500


@dataclass(frozen=True)
class Outcome:
    status: RawProcessingStatus
    error: str | None = None
    status_changed: bool = False


@dataclass
class ProcessingContext:
    """Everything a run keeps in memory between events."""

    index: ElementIndex
    book: StatusBook
    seen_vendor_ids: set[str] = field(default_factory=set)


def _vendor_id(payload: Any) -> str | None:
    if isinstance(payload, dict) and payload.get("id") is not None:
        return str(payload["id"]).strip() or None
    return None


async def process_event(session: AsyncSession, raw: Row, ctx: ProcessingContext) -> Outcome:
    # 1. Dedupe: the same Aruba event delivered in a second envelope.
    vendor_id = _vendor_id(raw.payload)
    if vendor_id is not None and vendor_id in ctx.seen_vendor_ids:
        return Outcome(RawProcessingStatus.DUPLICATE)

    # 2. Store.
    store_id = ctx.index.store_id(raw.site_id)
    if store_id is None:
        return Outcome(RawProcessingStatus.UNMATCHED, f"unknown site {raw.site_id}")

    # 3. Extract.
    try:
        event = extract(raw.event_type, raw.payload)
    except ArubaPayloadError as exc:
        return Outcome(RawProcessingStatus.ERROR, str(exc)[:_MAX_ERROR])

    # 4. Match.
    match = ctx.index.match(store_id, event)
    element_id: uuid.UUID | None = match.element.id if match.element else None
    applied = (
        element_id is not None
        and event.reported_status is not None
        and ctx.book.is_newer(element_id, raw.event_time)
    )

    # 5. Insert, in a savepoint so a database error affects only this event.
    async with session.begin_nested():
        session.add(
            NetworkEvent(
                event_id=raw.event_id,
                store_id=store_id,
                network_element_id=element_id,
                matched_by=str(match.matched_by),
                occurred_at=raw.event_time,
                ingested_at=raw.ingestion_time,
                source=raw.source,
                vendor_event_uuid=event.vendor_event_uuid,
                vendor_event_number=event.vendor_event_number,
                vendor_event_type=event.vendor_event_type,
                correlation_id=raw.correlation_id,
                aruba_device_type=event.aruba_device_type,
                hostname=event.hostname,
                device_serial=event.device_serial,
                device_mac=event.device_mac,
                level=event.level,
                description=event.description,
                reported_status=event.reported_status,
                status_applied=applied,
                uplink_name=event.uplink_name,
                details=event.details,
                **event.client,
            )
        )
        await session.flush()

    if vendor_id is not None:
        ctx.seen_vendor_ids.add(vendor_id)

    # 6. Apply.
    changed = False
    if element_id is not None:
        changed = ctx.book.record(
            element_id, raw.event_id, raw.event_time, event.reported_status, applied
        )

    # 7. Outcome.
    if element_id is None:
        return Outcome(RawProcessingStatus.UNMATCHED, "no matching network element")
    return Outcome(RawProcessingStatus.PROCESSED, status_changed=changed)
