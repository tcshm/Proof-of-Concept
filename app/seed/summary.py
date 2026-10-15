# app/seed/summary.py
"""Row counts printed at the end of a seed run."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AiPromptVersion,
    AssignmentGroup,
    Device,
    DeviceType,
    NetworkElement,
    NetworkElementType,
    RawNetworkEvent,
    Store,
)

_TABLES = (
    Store,
    AssignmentGroup,
    NetworkElementType,
    DeviceType,
    AiPromptVersion,
    NetworkElement,
    Device,
    RawNetworkEvent,
)


async def collect_summary(session: AsyncSession) -> list[str]:
    lines = ["Table counts:"]
    for model in _TABLES:
        count = await session.scalar(select(func.count()).select_from(model))
        lines.append(f"  {model.__tablename__:<24} {count:>5}")

    lines.append("")
    lines.append("Per store:  site   elements  devices (online/degraded/offline)  raw events")
    stores = (
        await session.execute(select(Store.id, Store.site_code).order_by(Store.site_code))
    ).all()
    for store_id, site in stores:
        elements = await session.scalar(
            select(func.count())
            .select_from(NetworkElement)
            .where(NetworkElement.store_id == store_id)
        )
        statuses = dict(
            (
                await session.execute(
                    select(Device.status, func.count())
                    .where(Device.store_id == store_id)
                    .group_by(Device.status)
                )
            ).all()
        )
        events = await session.scalar(
            select(func.count()).select_from(RawNetworkEvent).where(RawNetworkEvent.site_id == site)
        )
        devices = sum(statuses.values())
        split = "/".join(str(statuses.get(s, 0)) for s in ("online", "degraded", "offline"))
        lines.append(f"            {site}  {elements:>8}  {devices:>7} ({split:>8}){events:>26}")

    lines.append("")
    lines.append("Raw events by type and processing status:")
    rows = (
        await session.execute(
            select(RawNetworkEvent.event_type, RawNetworkEvent.processing_status, func.count())
            .group_by(RawNetworkEvent.event_type, RawNetworkEvent.processing_status)
            .order_by(RawNetworkEvent.event_type)
        )
    ).all()
    for event_type, status, count in rows:
        lines.append(f"  {event_type:<26} {status:<10} {count:>5}")
    return lines
