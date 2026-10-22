# app/queries/actions.py
"""Data for GET /api/v1/actions: what the store team must do now."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Incident, StoreAction
from app.models.values import ActionStatus


@dataclass(frozen=True)
class ActionRow:
    id: uuid.UUID
    title: str
    detail: str
    severity: str
    expected_response: str
    due_by: datetime | None
    incident_number: str
    created_at: datetime


async def open_actions(
    session: AsyncSession, store_id: uuid.UUID, now: datetime
) -> list[ActionRow]:
    """Open actions not yet overdue (even before the expiry sweep runs), soonest
    due first, actions without a due time last."""
    rows = (
        await session.execute(
            select(
                StoreAction.id,
                StoreAction.title,
                StoreAction.detail,
                StoreAction.severity,
                StoreAction.expected_response,
                StoreAction.due_by,
                Incident.number.label("incident_number"),
                StoreAction.created_at,
            )
            .join(Incident, Incident.id == StoreAction.incident_id)
            .where(
                StoreAction.store_id == store_id,
                StoreAction.status == ActionStatus.OPEN,
                or_(StoreAction.due_by.is_(None), StoreAction.due_by > now),
            )
            .order_by(StoreAction.due_by.asc().nulls_last(), StoreAction.created_at, StoreAction.id)
        )
    ).all()
    return [ActionRow(**row._asdict()) for row in rows]
