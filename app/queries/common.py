# app/queries/common.py
"""Queries shared by several screens."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.models import Incident
from app.models.values import ACTIVE_INCIDENT_STATES


@dataclass(frozen=True)
class IncidentRefRow:
    number: str
    short_description: str


async def open_incidents_by_ci(
    session: AsyncSession,
    store_id: uuid.UUID,
    ci_column: InstrumentedAttribute,
    ci_ids: Collection[uuid.UUID],
) -> dict[uuid.UUID, list[IncidentRefRow]]:
    """Open incidents (states 1-3) whose main CI is one of ci_ids, newest first.

    ci_column is Incident.cmdb_ci_network_element_id or Incident.cmdb_ci_device_id.
    """
    if not ci_ids:
        return {}
    rows = (
        await session.execute(
            select(ci_column, Incident.number, Incident.short_description)
            .where(
                Incident.store_id == store_id,
                Incident.state.in_(ACTIVE_INCIDENT_STATES),
                ci_column.in_(list(ci_ids)),
            )
            .order_by(Incident.opened_at.desc())
        )
    ).all()
    grouped: dict[uuid.UUID, list[IncidentRefRow]] = defaultdict(list)
    for ci_id, number, short_description in rows:
        grouped[ci_id].append(IncidentRefRow(number=number, short_description=short_description))
    return dict(grouped)
