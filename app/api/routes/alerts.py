# app/api/routes/alerts.py
"""GET /api/v1/alerts: the store's active alerts (one per open incident)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.context import StoreDep
from app.api.deps import SessionDep
from app.api.schemas.alerts import AlertItem
from app.queries.alerts import active_alerts

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertItem])
async def list_alerts(store: StoreDep, session: SessionDep) -> list[AlertItem]:
    return [
        AlertItem(
            id=row.id,
            title=row.title,
            detail=row.detail,
            severity=row.severity,
            incident_id=row.incident_number,
            created_at=row.created_at,
        )
        for row in await active_alerts(session, store.id)
    ]
