# app/api/routes/notifications.py
"""Mobile notification routes (all need X-Site-Id).

    GET  /api/v1/notifications                 the store's live notifications, newest first
    GET  /api/v1/notifications/unread-count    number for the app's badge
    POST /api/v1/notifications/{id}/read       mark one read
    POST /api/v1/notifications/read-all        mark every live one read

GET /notifications also records delivery: it sets delivered_at on the returned
notifications for this store (first time only).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.context import MobileActorDep, StoreDep
from app.api.deps import ClockDep, SessionDep
from app.api.mappers.notifications import category_label
from app.api.schemas.notifications import (
    NotificationItem,
    ReadAllResult,
    ReadResult,
    UnreadCount,
)
from app.queries.notifications import live_notifications, unread_count
from app.services import notifications as service

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationItem])
async def list_notifications(
    store: StoreDep,
    session: SessionDep,
    clock: ClockDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[NotificationItem]:
    now = clock.now()
    rows = await live_notifications(session, store.id, now, limit)
    await service.mark_delivered(session, store.id, [row.id for row in rows], now)
    return [
        NotificationItem(
            id=row.id,
            category=category_label(row.category),
            title=row.title,
            description=row.description,
            link_url=row.link_url,
            created_at=row.publish_at,
            system_event=row.system_event,
            incident_id=row.incident_number,
            appointment_id=row.appointment_id,
            action_id=row.action_id,
            is_read=row.read_at is not None,
        )
        for row in rows
    ]


@router.get("/unread-count", response_model=UnreadCount)
async def get_unread_count(store: StoreDep, session: SessionDep, clock: ClockDep) -> UnreadCount:
    return UnreadCount(unread_count=await unread_count(session, store.id, clock.now()))


@router.post("/read-all", response_model=ReadAllResult)
async def read_all(
    store: StoreDep, actor: MobileActorDep, session: SessionDep, clock: ClockDep
) -> ReadAllResult:
    marked = await service.mark_all_read(session, actor, store.id, clock.now())
    return ReadAllResult(marked=marked)


@router.post("/{notification_id}/read", response_model=ReadResult)
async def read_one(
    notification_id: uuid.UUID,
    store: StoreDep,
    actor: MobileActorDep,
    session: SessionDep,
    clock: ClockDep,
) -> ReadResult:
    target = await service.mark_read(session, actor, store.id, notification_id, clock.now())
    return ReadResult(id=notification_id, is_read=True, read_at=target.read_at)
