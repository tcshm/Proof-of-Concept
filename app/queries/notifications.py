# app/queries/notifications.py
"""Read side for notifications.

A notification is LIVE for a store when the store has a target row and
    status = 'published', publish_at <= now, and expires_at is empty or later.
Scheduling, expiry and withdrawal are therefore just this filter at read time;
no background job changes the rows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Incident, Notification, NotificationTarget, Store
from app.models.values import NotificationStatus


def live_filter(now: datetime) -> ColumnElement[bool]:
    return and_(
        Notification.status == NotificationStatus.PUBLISHED,
        Notification.publish_at <= now,
        or_(Notification.expires_at.is_(None), Notification.expires_at > now),
    )


@dataclass(frozen=True)
class NotificationRow:
    id: uuid.UUID
    category: str
    title: str
    description: str
    link_url: str | None
    publish_at: datetime
    system_event: str | None
    incident_number: str | None
    appointment_id: uuid.UUID | None
    action_id: uuid.UUID | None
    read_at: datetime | None


@dataclass(frozen=True)
class DeliveryRow:
    site_code: str
    store_name: str
    delivered_at: datetime | None
    read_at: datetime | None
    read_by: str | None


async def live_notifications(
    session: AsyncSession, store_id: uuid.UUID, now: datetime, limit: int = 50
) -> list[NotificationRow]:
    """The store's live notifications, newest first."""
    rows = (
        await session.execute(
            select(
                Notification.id,
                Notification.category,
                Notification.title,
                Notification.description,
                Notification.link_url,
                Notification.publish_at,
                Notification.system_event,
                Incident.number.label("incident_number"),
                Notification.appointment_id,
                Notification.action_id,
                NotificationTarget.read_at,
            )
            .select_from(NotificationTarget)
            .join(Notification, Notification.id == NotificationTarget.notification_id)
            .outerjoin(Incident, Incident.id == Notification.incident_id)
            .where(NotificationTarget.store_id == store_id, live_filter(now))
            .order_by(Notification.publish_at.desc(), Notification.id)
            .limit(limit)
        )
    ).all()
    return [NotificationRow(**row._asdict()) for row in rows]


async def unread_count(session: AsyncSession, store_id: uuid.UUID, now: datetime) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(NotificationTarget)
        .join(Notification, Notification.id == NotificationTarget.notification_id)
        .where(
            NotificationTarget.store_id == store_id,
            NotificationTarget.read_at.is_(None),
            live_filter(now),
        )
    )
    return count or 0


async def delivery_report(
    session: AsyncSession, notification_id: uuid.UUID
) -> tuple[Notification, list[DeliveryRow]] | None:
    """One notification and, per target store, when it was delivered and read."""
    notification = await session.get(Notification, notification_id)
    if notification is None:
        return None
    rows = (
        await session.execute(
            select(
                Store.site_code,
                Store.name.label("store_name"),
                NotificationTarget.delivered_at,
                NotificationTarget.read_at,
                NotificationTarget.read_by,
            )
            .join(Store, Store.id == NotificationTarget.store_id)
            .where(NotificationTarget.notification_id == notification_id)
            .order_by(Store.site_code)
        )
    ).all()
    return notification, [DeliveryRow(**row._asdict()) for row in rows]
