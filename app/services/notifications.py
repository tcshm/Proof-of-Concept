# app/services/notifications.py
"""The notification service: the ONLY code that writes notifications and targets.

    publish_notification        portal announcement to an explicit list of stores
    withdraw_notification       hide a published portal announcement
    create_system_notification  fixed-template notice to exactly one store
    notify_appointment          system notice for a scheduled / rescheduled /
                                cancelled visit (called by the appointment and
                                incident services, in THEIR transaction)
    mark_delivered / mark_read / mark_all_read   per-store delivery and read status

Notifications are pulled by the app: writing the rows is "sending". Whether a
store can see a notification is decided at read time (see app/queries/notifications.py).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import ActorContext
from app.core.errors import BusinessRuleError, ConflictError, NotFoundError
from app.models import Appointment, Incident, Notification, NotificationTarget, Store
from app.models.values import (
    AuditAction,
    NotificationCategory,
    NotificationSource,
    NotificationStatus,
    SystemEvent,
)
from app.queries.notifications import live_filter
from app.services import notification_templates as templates
from app.services.audit import record_audit
from app.services.notification_templates import MAX_DESCRIPTION, MAX_TITLE, NotificationContent

# Who may publish portal announcements (mobile users may not).
_PUBLISHING_SOURCES = (NotificationSource.ADMIN_PORTAL, NotificationSource.SEED)


@dataclass(frozen=True)
class NewAnnouncement:
    category: str
    title: str
    description: str
    target_site_codes: Sequence[str]
    link_url: str | None = None
    publish_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class PublishedNotification:
    notification: Notification
    target_count: int


async def _finish(session: AsyncSession, commit: bool) -> None:
    if commit:
        await session.commit()
    else:
        await session.flush()


def _publishing_source(actor: ActorContext) -> str:
    if actor.source not in _PUBLISHING_SOURCES:
        raise BusinessRuleError("Announcements are published from the admin portal.")
    return str(NotificationSource(actor.source))


def _text(value: str | None, label: str, limit: int) -> str:
    text = (value or "").strip()
    if not text:
        raise BusinessRuleError(f"{label} is required.")
    if len(text) > limit:
        raise BusinessRuleError(f"{label} must be at most {limit} characters.")
    return text


async def _resolve_stores(session: AsyncSession, site_codes: Sequence[str]) -> list[uuid.UUID]:
    codes = list(dict.fromkeys(code.strip() for code in site_codes if code and code.strip()))
    if not codes:
        raise BusinessRuleError("At least one target store is required.")
    found = dict(
        (await session.execute(select(Store.site_code, Store.id).where(Store.site_code.in_(codes))))
        .tuples()
        .all()
    )
    unknown = [code for code in codes if code not in found]
    if unknown:
        raise NotFoundError(
            f"Unknown store(s): {', '.join(unknown)}.",
            details=[{"siteCode": code} for code in unknown],
        )
    return [found[code] for code in codes]


async def _lock_notification(session: AsyncSession, notification_id: uuid.UUID) -> Notification:
    notification = await session.scalar(
        select(Notification)
        .where(Notification.id == notification_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if notification is None:
        raise NotFoundError(f"Notification {notification_id} was not found.")
    return notification


# --- portal announcements --------------------------------------------------------------
async def publish_notification(
    session: AsyncSession,
    actor: ActorContext,
    data: NewAnnouncement,
    now: datetime,
    *,
    commit: bool = True,
) -> PublishedNotification:
    """One notification row plus one target row per store, in one transaction.

    An unknown site code rejects the whole request. publish_at in the past (or
    missing) means now; in the future it schedules the notification.
    """
    source = _publishing_source(actor)
    try:
        category = str(NotificationCategory(data.category))
    except ValueError as exc:
        choices = ", ".join(c.value for c in NotificationCategory)
        raise BusinessRuleError(f"Category must be one of: {choices}.") from exc
    title = _text(data.title, "Title", MAX_TITLE)
    description = _text(data.description, "Description", MAX_DESCRIPTION)
    link_url = (data.link_url or "").strip() or None
    if link_url is not None and not link_url.startswith("https://"):
        raise BusinessRuleError("The link must start with https://.")
    publish_at = data.publish_at if data.publish_at and data.publish_at > now else now
    if data.expires_at is not None and data.expires_at <= publish_at:
        raise BusinessRuleError("The end time must be after the start time.")
    store_ids = await _resolve_stores(session, data.target_site_codes)

    notification = Notification(
        id=uuid.uuid4(),
        category=category,
        title=title,
        description=description,
        link_url=link_url,
        source=source,
        system_event=None,
        created_by=actor.actor,
        status=NotificationStatus.PUBLISHED,
        publish_at=publish_at,
        expires_at=data.expires_at,
        created_at=now,
    )
    session.add(notification)
    await session.flush()
    session.add_all(
        NotificationTarget(notification_id=notification.id, store_id=store_id, created_at=now)
        for store_id in store_ids
    )
    record_audit(
        session,
        actor,
        action=AuditAction.PUBLISH,
        entity_type="notifications",
        entity_id=notification.id,
        changes={
            "category": category,
            "title": title,
            "targets": len(store_ids),
            "publish_at": publish_at,
            "expires_at": data.expires_at,
        },
    )
    await _finish(session, commit)
    return PublishedNotification(notification=notification, target_count=len(store_ids))


async def withdraw_notification(
    session: AsyncSession,
    actor: ActorContext,
    notification_id: uuid.UUID,
    now: datetime,
    *,
    commit: bool = True,
) -> Notification:
    """Hide a published announcement from every store. Target rows are kept, so
    the delivery report still shows who saw it."""
    notification = await _lock_notification(session, notification_id)
    if notification.source == NotificationSource.SYSTEM:
        raise ConflictError("System notifications cannot be withdrawn.")
    if notification.status != NotificationStatus.PUBLISHED:
        raise ConflictError(f"The notification is {notification.status}, not published.")
    notification.status = NotificationStatus.WITHDRAWN
    notification.withdrawn_at = now
    notification.withdrawn_by = actor.actor
    record_audit(
        session,
        actor,
        action=AuditAction.WITHDRAW,
        entity_type="notifications",
        entity_id=notification.id,
        changes={"status": {"old": "published", "new": "withdrawn"}},
    )
    await _finish(session, commit)
    return notification


# --- system notifications --------------------------------------------------------------
async def create_system_notification(
    session: AsyncSession,
    actor: ActorContext,
    *,
    event: SystemEvent | str,
    store_id: uuid.UUID,
    content: NotificationContent,
    publish_at: datetime,
    incident_id: uuid.UUID | None = None,
    appointment_id: uuid.UUID | None = None,
    action_id: uuid.UUID | None = None,
    source_journal_id: uuid.UUID | None = None,
) -> Notification:
    """A fixed-template notice to exactly one store. Never commits: it is always
    part of the caller's transaction (appointment change, worker step).

    With source_journal_id, calling it again for the same journal entry returns
    the existing notification (a retried worker step cannot send it twice).
    """
    if source_journal_id is not None:
        existing = await session.scalar(
            select(Notification).where(Notification.source_journal_id == source_journal_id)
        )
        if existing is not None:
            return existing
    notification = Notification(
        id=uuid.uuid4(),
        category=content.category,
        title=content.title,
        description=content.description,
        link_url=None,
        source=NotificationSource.SYSTEM,
        system_event=str(SystemEvent(event)),
        created_by=actor.actor,
        incident_id=incident_id,
        appointment_id=appointment_id,
        action_id=action_id,
        source_journal_id=source_journal_id,
        status=NotificationStatus.PUBLISHED,
        publish_at=publish_at,
        expires_at=content.expires_at,
        created_at=publish_at,
    )
    session.add(notification)
    await session.flush()
    session.add(
        NotificationTarget(
            notification_id=notification.id, store_id=store_id, created_at=publish_at
        )
    )
    record_audit(
        session,
        actor,
        action=AuditAction.CREATE,
        entity_type="notifications",
        entity_id=notification.id,
        store_id=store_id,
        changes={"system_event": str(SystemEvent(event)), "title": content.title},
    )
    await session.flush()
    return notification


async def notify_appointment(
    session: AsyncSession,
    actor: ActorContext,
    appointment: Appointment,
    event: SystemEvent | str,
    now: datetime,
    *,
    old_start: datetime | None = None,
) -> Notification:
    """The store notice for a scheduled, rescheduled or cancelled visit."""
    number, timezone = (
        await session.execute(
            select(Incident.number, Store.timezone)
            .join(Store, Store.id == Incident.store_id)
            .where(Incident.id == appointment.incident_id)
        )
    ).one()
    tz = ZoneInfo(timezone)
    common = {
        "title": appointment.title,
        "purpose": appointment.purpose,
        "start": appointment.scheduled_start,
        "end": appointment.scheduled_end,
        "incident_number": number,
        "tz": tz,
        "publish_at": now,
    }
    event = SystemEvent(event)
    if event == SystemEvent.APPOINTMENT_SCHEDULED:
        content = templates.appointment_scheduled(details=appointment.details, **common)
    elif event == SystemEvent.APPOINTMENT_RESCHEDULED:
        content = templates.appointment_rescheduled(
            old_start=old_start or appointment.scheduled_start, **common
        )
    elif event == SystemEvent.APPOINTMENT_CANCELLED:
        content = templates.appointment_cancelled(
            note=appointment.status_note or "No reason given", **common
        )
    else:
        raise ValueError(f"{event} is not an appointment event")
    return await create_system_notification(
        session,
        actor,
        event=event,
        store_id=appointment.store_id,
        content=content,
        publish_at=now,
        incident_id=appointment.incident_id,
        appointment_id=appointment.id,
    )


# --- delivery and read status ------------------------------------------------------------
async def mark_delivered(
    session: AsyncSession,
    store_id: uuid.UUID,
    notification_ids: Sequence[uuid.UUID],
    now: datetime,
    *,
    commit: bool = True,
) -> int:
    """Set delivered_at on the store's targets that do not have it yet."""
    if not notification_ids:
        return 0
    marked = (
        await session.scalars(
            update(NotificationTarget)
            .where(
                NotificationTarget.store_id == store_id,
                NotificationTarget.notification_id.in_(list(notification_ids)),
                NotificationTarget.delivered_at.is_(None),
            )
            .values(delivered_at=now)
            .returning(NotificationTarget.notification_id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    await _finish(session, commit)
    return len(marked)


async def mark_read(
    session: AsyncSession,
    actor: ActorContext,
    store_id: uuid.UUID,
    notification_id: uuid.UUID,
    now: datetime,
    *,
    commit: bool = True,
) -> NotificationTarget:
    """Mark one live notification read for the store. Reading it again keeps the
    first read time. A notification the store cannot see gives 404."""
    target = await session.scalar(
        select(NotificationTarget)
        .join(Notification, Notification.id == NotificationTarget.notification_id)
        .where(
            NotificationTarget.store_id == store_id,
            NotificationTarget.notification_id == notification_id,
            live_filter(now),
        )
        .with_for_update(of=NotificationTarget)
        .execution_options(populate_existing=True)
    )
    if target is None:
        raise NotFoundError(f"Notification {notification_id} was not found.")
    if target.read_at is None:
        target.delivered_at = target.delivered_at or now
        target.read_at = now
        target.read_by = actor.actor
    await _finish(session, commit)
    return target


async def mark_all_read(
    session: AsyncSession,
    actor: ActorContext,
    store_id: uuid.UUID,
    now: datetime,
    *,
    commit: bool = True,
) -> int:
    """Mark every live, unread notification of the store read. Returns how many."""
    live_ids = select(Notification.id).where(live_filter(now))
    marked = (
        await session.scalars(
            update(NotificationTarget)
            .where(
                NotificationTarget.store_id == store_id,
                NotificationTarget.read_at.is_(None),
                NotificationTarget.notification_id.in_(live_ids),
            )
            .values(
                delivered_at=func.coalesce(NotificationTarget.delivered_at, now),
                read_at=now,
                read_by=actor.actor,
            )
            .returning(NotificationTarget.notification_id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    await _finish(session, commit)
    return len(marked)
