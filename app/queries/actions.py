# app/queries/actions.py
"""Data for GET /api/v1/actions: what the store team must do now, and what it
answered recently.

    open       not yet overdue (even before the expiry sweep runs); soonest due
               first, no due time last
    responded  answered in the last 24 hours, newest answer first (so the store
               can see its reply arrived while IT looks at it)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Incident, StoreAction, StoreActionResponse
from app.models.values import ActionStatus

RECENT_REPLIES = timedelta(hours=24)


@dataclass(frozen=True)
class ReplyRow:
    answer_yes_no: bool | None
    response_text: str | None
    responded_by: str
    created_at: datetime


@dataclass(frozen=True)
class ActionRow:
    id: uuid.UUID
    title: str
    detail: str
    severity: str
    expected_response: str
    due_by: datetime | None
    status: str
    incident_number: str
    created_at: datetime
    reply: ReplyRow | None = None


async def _latest_replies(
    session: AsyncSession, action_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ReplyRow]:
    if not action_ids:
        return {}
    rows = (
        await session.execute(
            select(
                StoreActionResponse.action_id,
                StoreActionResponse.answer_yes_no,
                StoreActionResponse.response_text,
                StoreActionResponse.responded_by,
                StoreActionResponse.created_at,
            )
            .where(StoreActionResponse.action_id.in_(action_ids))
            .order_by(StoreActionResponse.action_id, StoreActionResponse.created_at.desc())
        )
    ).all()
    latest: dict[uuid.UUID, ReplyRow] = {}
    for row in rows:
        latest.setdefault(
            row.action_id,
            ReplyRow(row.answer_yes_no, row.response_text, row.responded_by, row.created_at),
        )
    return latest


async def visible_actions(
    session: AsyncSession, store_id: uuid.UUID, now: datetime
) -> list[ActionRow]:
    recent_reply = (
        select(StoreActionResponse.id)
        .where(
            StoreActionResponse.action_id == StoreAction.id,
            StoreActionResponse.created_at >= now - RECENT_REPLIES,
        )
        .exists()
    )
    rows = (
        await session.execute(
            select(
                StoreAction.id,
                StoreAction.title,
                StoreAction.detail,
                StoreAction.severity,
                StoreAction.expected_response,
                StoreAction.due_by,
                StoreAction.status,
                Incident.number.label("incident_number"),
                StoreAction.created_at,
            )
            .join(Incident, Incident.id == StoreAction.incident_id)
            .where(
                StoreAction.store_id == store_id,
                or_(
                    and_(
                        StoreAction.status == ActionStatus.OPEN,
                        or_(StoreAction.due_by.is_(None), StoreAction.due_by > now),
                    ),
                    and_(StoreAction.status == ActionStatus.RESPONDED, recent_reply),
                ),
            )
        )
    ).all()
    replies = await _latest_replies(
        session, [row.id for row in rows if row.status == ActionStatus.RESPONDED]
    )
    items = [ActionRow(**row._asdict(), reply=replies.get(row.id)) for row in rows]
    open_items = sorted(
        (a for a in items if a.status == ActionStatus.OPEN),
        key=lambda a: (a.due_by is None, a.due_by or a.created_at, a.created_at, str(a.id)),
    )
    answered = sorted(
        (a for a in items if a.status != ActionStatus.OPEN),
        key=lambda a: a.reply.created_at if a.reply else a.created_at,
        reverse=True,
    )
    return open_items + answered
