# app/services/alerts.py
"""Store alerts: one per incident, created by the worker (impact assessment).
The incident service closes the alert when the incident is resolved or cancelled."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import ActorContext
from app.models import StoreAlert
from app.models.values import AlertSource, AlertStatus, AuditAction, Severity
from app.services.audit import record_audit


async def create_alert(
    session: AsyncSession,
    actor: ActorContext,
    *,
    store_id: uuid.UUID,
    incident_id: uuid.UUID,
    title: str,
    detail: str,
    severity: str,
    source: AlertSource | str,
    ai_workflow_run_id: uuid.UUID | None,
    created_at: datetime,
) -> tuple[StoreAlert, bool]:
    """The incident's alert, created if it does not exist yet. Returns (alert, created).
    Never commits: part of the caller's transaction."""
    existing = await session.scalar(select(StoreAlert).where(StoreAlert.incident_id == incident_id))
    if existing is not None:
        return existing, False
    alert = StoreAlert(
        id=uuid.uuid4(),
        store_id=store_id,
        incident_id=incident_id,
        title=title,
        detail=detail,
        severity=str(Severity(severity)),
        source=str(AlertSource(source)),
        ai_workflow_run_id=ai_workflow_run_id,
        created_by=actor.actor,
        status=AlertStatus.ACTIVE,
        created_at=created_at,
    )
    session.add(alert)
    await session.flush()
    record_audit(
        session,
        actor,
        action=AuditAction.CREATE,
        entity_type="store_alerts",
        entity_id=alert.id,
        store_id=store_id,
        changes={"title": title, "severity": alert.severity, "source": alert.source},
    )
    return alert, True
