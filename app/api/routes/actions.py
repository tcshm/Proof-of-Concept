# app/api/routes/actions.py
"""GET /api/v1/actions: open actions for the store team, soonest due first.
(Replies to actions come in Phase 10.)"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.context import StoreDep
from app.api.deps import ClockDep, SessionDep
from app.api.schemas.actions import ActionItem
from app.core.timefmt import format_when
from app.queries.actions import open_actions

router = APIRouter(prefix="/actions", tags=["actions"])


@router.get("", response_model=list[ActionItem])
async def list_actions(store: StoreDep, session: SessionDep, clock: ClockDep) -> list[ActionItem]:
    now = clock.now()
    return [
        ActionItem(
            id=row.id,
            title=row.title,
            detail=row.detail,
            severity=row.severity,
            expected_response=row.expected_response,
            due_by=row.due_by,
            due_by_text=format_when(row.due_by, store.timezone, now) if row.due_by else None,
            incident_id=row.incident_number,
            created_at=row.created_at,
        )
        for row in await open_actions(session, store.id, now)
    ]
