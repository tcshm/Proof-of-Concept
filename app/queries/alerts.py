# app/queries/alerts.py
"""Data for GET /api/v1/alerts: the store's active alerts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Incident, StoreAlert
from app.models.values import AlertStatus, Severity

_SEVERITY_ORDER = case(
    (StoreAlert.severity == Severity.CRITICAL, 0),
    (StoreAlert.severity == Severity.WARNING, 1),
    else_=2,
)


@dataclass(frozen=True)
class AlertRow:
    id: uuid.UUID
    title: str
    detail: str
    severity: str
    incident_number: str
    created_at: datetime


async def active_alerts(session: AsyncSession, store_id: uuid.UUID) -> list[AlertRow]:
    """Critical first, then warning, then info; newest first within each."""
    rows = (
        await session.execute(
            select(
                StoreAlert.id,
                StoreAlert.title,
                StoreAlert.detail,
                StoreAlert.severity,
                Incident.number.label("incident_number"),
                StoreAlert.created_at,
            )
            .join(Incident, Incident.id == StoreAlert.incident_id)
            .where(StoreAlert.store_id == store_id, StoreAlert.status == AlertStatus.ACTIVE)
            .order_by(_SEVERITY_ORDER, StoreAlert.created_at.desc(), StoreAlert.id)
        )
    ).all()
    return [AlertRow(**row._asdict()) for row in rows]
