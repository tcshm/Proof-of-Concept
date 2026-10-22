# app/api/routes/portal_notifications.py
"""Admin portal notification routes (no X-Site-Id; the actor is IT staff).

    POST /api/v1/portal/notifications                     publish an announcement
    POST /api/v1/portal/notifications/{id}/withdraw       withdraw it
    GET  /api/v1/portal/notifications/{id}/delivery       who received and read it

The rest of the portal comes in Phase 13.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.context import PortalActorDep
from app.api.deps import ClockDep, SessionDep
from app.api.schemas.notifications import (
    DeliveryReport,
    DeliveryStore,
    PortalNotification,
    PublishRequest,
)
from app.core.errors import NotFoundError
from app.models import Notification
from app.queries.notifications import delivery_report
from app.services import notifications as service

router = APIRouter(prefix="/portal/notifications", tags=["portal: notifications"])


def _portal_view(notification: Notification, target_count: int) -> PortalNotification:
    return PortalNotification(
        id=notification.id,
        category=notification.category,
        title=notification.title,
        description=notification.description,
        link_url=notification.link_url,
        source=notification.source,
        status=notification.status,
        publish_at=notification.publish_at,
        expires_at=notification.expires_at,
        withdrawn_at=notification.withdrawn_at,
        created_by=notification.created_by,
        target_count=target_count,
    )


@router.post("", response_model=PortalNotification, status_code=status.HTTP_201_CREATED)
async def publish(
    body: PublishRequest, actor: PortalActorDep, session: SessionDep, clock: ClockDep
) -> PortalNotification:
    published = await service.publish_notification(
        session,
        actor,
        service.NewAnnouncement(
            category=body.category,
            title=body.title,
            description=body.description,
            target_site_codes=body.target_site_codes,
            link_url=body.link_url,
            publish_at=body.publish_at,
            expires_at=body.expires_at,
        ),
        clock.now(),
    )
    return _portal_view(published.notification, published.target_count)


@router.post("/{notification_id}/withdraw", response_model=PortalNotification)
async def withdraw(
    notification_id: uuid.UUID, actor: PortalActorDep, session: SessionDep, clock: ClockDep
) -> PortalNotification:
    notification = await service.withdraw_notification(session, actor, notification_id, clock.now())
    report = await delivery_report(session, notification_id)
    return _portal_view(notification, len(report[1]) if report else 0)


@router.get("/{notification_id}/delivery", response_model=DeliveryReport)
async def get_delivery(
    notification_id: uuid.UUID, actor: PortalActorDep, session: SessionDep
) -> DeliveryReport:
    report = await delivery_report(session, notification_id)
    if report is None:
        raise NotFoundError(f"Notification {notification_id} was not found.")
    notification, rows = report
    return DeliveryReport(
        notification_id=notification.id,
        title=notification.title,
        status=notification.status,
        targets=len(rows),
        delivered=sum(1 for row in rows if row.delivered_at is not None),
        read=sum(1 for row in rows if row.read_at is not None),
        stores=[
            DeliveryStore(
                site_code=row.site_code,
                store_name=row.store_name,
                delivered_at=row.delivered_at,
                read_at=row.read_at,
                read_by=row.read_by,
            )
            for row in rows
        ],
    )
