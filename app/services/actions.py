# app/services/actions.py
"""Store actions: something the store team must do for an incident.

Created by the worker (update classification). Closed when the incident is
closed or cancelled (incident service), when they expire (expire_overdue_actions)
and, from Phase 10, when IT accepts the store's reply.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import ActorContext
from app.models import StoreAction
from app.models.values import (
    ActionCloseReason,
    ActionSource,
    ActionStatus,
    AuditAction,
    ExpectedResponse,
    Severity,
)
from app.services.audit import record_audit


async def create_action(
    session: AsyncSession,
    actor: ActorContext,
    *,
    store_id: uuid.UUID,
    incident_id: uuid.UUID,
    source_journal_id: uuid.UUID | None,
    title: str,
    detail: str,
    severity: str,
    expected_response: str,
    due_by: datetime | None,
    source: ActionSource | str,
    ai_workflow_run_id: uuid.UUID | None,
    created_at: datetime,
) -> tuple[StoreAction, bool]:
    """One action per journal entry: an existing one is returned, not duplicated.
    Returns (action, created). Never commits."""
    if source_journal_id is not None:
        existing = await session.scalar(
            select(StoreAction).where(StoreAction.source_journal_id == source_journal_id)
        )
        if existing is not None:
            return existing, False
    action = StoreAction(
        id=uuid.uuid4(),
        store_id=store_id,
        incident_id=incident_id,
        source_journal_id=source_journal_id,
        title=title,
        detail=detail,
        severity=str(Severity(severity)),
        expected_response=str(ExpectedResponse(expected_response)),
        due_by=due_by,
        source=str(ActionSource(source)),
        ai_workflow_run_id=ai_workflow_run_id,
        created_by=actor.actor,
        status=ActionStatus.OPEN,
        created_at=created_at,
    )
    session.add(action)
    await session.flush()
    record_audit(
        session,
        actor,
        action=AuditAction.CREATE,
        entity_type="store_actions",
        entity_id=action.id,
        store_id=store_id,
        changes={
            "title": title,
            "expected_response": action.expected_response,
            "due_by": due_by,
        },
    )
    return action, True


async def expire_overdue_actions(
    session: AsyncSession, actor: ActorContext, now: datetime, *, commit: bool = True
) -> int:
    """Open actions whose due time has passed become closed / expired."""
    rows = (
        await session.execute(
            update(StoreAction)
            .where(
                StoreAction.status == ActionStatus.OPEN,
                StoreAction.due_by.is_not(None),
                StoreAction.due_by <= now,
            )
            .values(
                status=ActionStatus.CLOSED,
                close_reason=ActionCloseReason.EXPIRED,
                closed_at=now,
            )
            .returning(StoreAction.id, StoreAction.store_id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    for action_id, store_id in rows:
        record_audit(
            session,
            actor,
            action=AuditAction.STATUS_CHANGE,
            entity_type="store_actions",
            entity_id=action_id,
            store_id=store_id,
            changes={"status": {"old": "open", "new": "closed"}, "close_reason": "expired"},
        )
    if commit:
        await session.commit()
    else:
        await session.flush()
    return len(rows)
