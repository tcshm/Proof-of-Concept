# tests/test_notification_service.py
"""The notification service and the live rule (rolled-back test database)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleError, ConflictError, NotFoundError
from app.models import AuditLog, IncidentJournal, Notification, NotificationTarget, Store
from app.queries.notifications import live_notifications, unread_count
from app.services import incidents as incident_svc
from app.services import notifications as svc
from app.services.notification_templates import incident_update
from app.services.notifications import NewAnnouncement
from tests.itsm_helpers import IT, NOW, STORE_USER, build_itsm_fixture, new_incident


@dataclass
class Stores:
    chennai: uuid.UUID
    bengaluru: uuid.UUID
    paris: uuid.UUID


@pytest.fixture
async def stores(db_session: AsyncSession) -> Stores:
    rows = [
        Store(site_code="01234", name="Chennai", region="India", country_code="IN",
              timezone="Asia/Kolkata"),
        Store(site_code="01456", name="Bengaluru", region="India", country_code="IN",
              timezone="Asia/Kolkata"),
        Store(site_code="02556", name="Paris", region="EU Central", country_code="FR",
              timezone="Europe/Paris"),
    ]  # fmt: skip
    db_session.add_all(rows)
    await db_session.flush()
    return Stores(rows[0].id, rows[1].id, rows[2].id)


def announcement(**overrides) -> NewAnnouncement:
    values = {
        "category": "maintenance",
        "title": "Network maintenance tonight at 1 AM",
        "description": "No action is needed from stores.",
        "target_site_codes": ["01234", "01456"],
    }
    values.update(overrides)
    return NewAnnouncement(**values)


async def publish(session: AsyncSession, **overrides) -> svc.PublishedNotification:
    return await svc.publish_notification(session, IT, announcement(**overrides), NOW, commit=False)


async def live_ids(session: AsyncSession, store_id: uuid.UUID, at=NOW) -> list[uuid.UUID]:
    return [row.id for row in await live_notifications(session, store_id, at)]


async def _count(session: AsyncSession, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


# --- publish ------------------------------------------------------------------------------
async def test_publish_writes_one_notification_and_one_target_per_store(
    db_session: AsyncSession, stores: Stores
) -> None:
    result = await publish(db_session, link_url="https://intranet.example/maintenance")
    n = result.notification
    assert result.target_count == 2
    assert (n.category, n.source, n.status, n.system_event) == (
        "maintenance",
        "admin_portal",
        "published",
        None,
    )
    assert (n.publish_at, n.expires_at, n.created_by) == (NOW, None, "r.kumar")
    assert n.link_url == "https://intranet.example/maintenance"
    targets = (
        await db_session.scalars(
            select(NotificationTarget.store_id).where(NotificationTarget.notification_id == n.id)
        )
    ).all()
    assert set(targets) == {stores.chennai, stores.bengaluru}
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.entity_id == str(n.id)))
    assert audit.action == "publish" and audit.changes["targets"] == 2


async def test_duplicate_codes_are_removed(db_session: AsyncSession, stores: Stores) -> None:
    result = await publish(db_session, target_site_codes=["01234", " 01234 ", "01456", ""])
    assert result.target_count == 2


async def test_unknown_store_rejects_the_whole_request(
    db_session: AsyncSession, stores: Stores
) -> None:
    with pytest.raises(NotFoundError, match=r"Unknown store\(s\): 99999, 88888") as error:
        await publish(db_session, target_site_codes=["01234", "99999", "88888"])
    assert error.value.details == [{"siteCode": "99999"}, {"siteCode": "88888"}]
    assert await _count(db_session, Notification) == 0


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"target_site_codes": []}, "At least one target store"),
        ({"title": "  "}, "Title is required"),
        ({"description": "x" * 2001}, "at most 2000 characters"),
        ({"category": "news"}, "Category must be one of"),
        ({"link_url": "http://intranet.example"}, "must start with https://"),
        ({"expires_at": NOW}, "end time must be after the start time"),
        ({"publish_at": NOW + timedelta(hours=2), "expires_at": NOW + timedelta(hours=1)},
         "end time must be after the start time"),
    ],
)  # fmt: skip
async def test_publish_validation(
    db_session: AsyncSession, stores: Stores, overrides: dict, message: str
) -> None:
    with pytest.raises(BusinessRuleError, match=message):
        await publish(db_session, **overrides)


async def test_store_users_cannot_publish(db_session: AsyncSession, stores: Stores) -> None:
    with pytest.raises(BusinessRuleError, match="published from the admin portal"):
        await svc.publish_notification(db_session, STORE_USER, announcement(), NOW, commit=False)


async def test_past_start_time_means_now(db_session: AsyncSession, stores: Stores) -> None:
    past = await publish(db_session, publish_at=NOW - timedelta(hours=1))
    future = await publish(db_session, publish_at=NOW + timedelta(hours=1))
    assert past.notification.publish_at == NOW
    assert future.notification.publish_at == NOW + timedelta(hours=1)


# --- live rule ------------------------------------------------------------------------------
async def test_scheduled_and_expired_edges(db_session: AsyncSession, stores: Stores) -> None:
    starts = NOW + timedelta(hours=1)
    ends = NOW + timedelta(hours=3)
    scheduled = (await publish(db_session, publish_at=starts, expires_at=ends)).notification

    assert scheduled.id not in await live_ids(db_session, stores.chennai, NOW)
    assert scheduled.id not in await live_ids(
        db_session, stores.chennai, starts - timedelta(seconds=1)
    )
    assert scheduled.id in await live_ids(db_session, stores.chennai, starts)
    assert scheduled.id in await live_ids(db_session, stores.chennai, ends - timedelta(seconds=1))
    assert scheduled.id not in await live_ids(db_session, stores.chennai, ends)


async def test_other_stores_never_see_it(db_session: AsyncSession, stores: Stores) -> None:
    n = (await publish(db_session)).notification
    assert n.id in await live_ids(db_session, stores.bengaluru)
    assert n.id not in await live_ids(db_session, stores.paris)


async def test_newest_first_and_limit(db_session: AsyncSession, stores: Stores) -> None:
    old = (
        await svc.publish_notification(
            db_session, IT, announcement(title="older"), NOW - timedelta(hours=2), commit=False
        )
    ).notification
    new = (await publish(db_session, title="newer")).notification
    assert await live_ids(db_session, stores.chennai) == [new.id, old.id]
    rows = await live_notifications(db_session, stores.chennai, NOW, limit=1)
    assert [row.id for row in rows] == [new.id]


# --- withdraw -------------------------------------------------------------------------------
async def test_withdraw(db_session: AsyncSession, stores: Stores) -> None:
    n = (await publish(db_session)).notification
    later = NOW + timedelta(minutes=10)
    await svc.withdraw_notification(db_session, IT, n.id, later, commit=False)
    assert (n.status, n.withdrawn_at, n.withdrawn_by) == ("withdrawn", later, "r.kumar")
    assert n.id not in await live_ids(db_session, stores.chennai, later)
    with pytest.raises(ConflictError, match="withdrawn, not published"):
        await svc.withdraw_notification(db_session, IT, n.id, later, commit=False)
    with pytest.raises(NotFoundError):
        await svc.withdraw_notification(db_session, IT, uuid.uuid4(), later, commit=False)
    # Targets are kept for the delivery report.
    targets = await db_session.scalar(
        select(func.count())
        .select_from(NotificationTarget)
        .where(NotificationTarget.notification_id == n.id)
    )
    assert targets == 2


# --- delivered and read ------------------------------------------------------------------------
async def test_delivered_is_set_only_once(db_session: AsyncSession, stores: Stores) -> None:
    n = (await publish(db_session)).notification
    assert await svc.mark_delivered(db_session, stores.chennai, [n.id], NOW, commit=False) == 1
    later = NOW + timedelta(minutes=5)
    assert await svc.mark_delivered(db_session, stores.chennai, [n.id], later, commit=False) == 0
    target = await db_session.get(
        NotificationTarget, (n.id, stores.chennai), populate_existing=True
    )
    assert target.delivered_at == NOW
    other = await db_session.get(
        NotificationTarget, (n.id, stores.bengaluru), populate_existing=True
    )
    assert other.delivered_at is None


async def test_read_is_idempotent_and_updates_the_count(
    db_session: AsyncSession, stores: Stores
) -> None:
    first = (await publish(db_session, title="first")).notification
    await publish(db_session, title="second")
    assert await unread_count(db_session, stores.chennai, NOW) == 2

    read_at = NOW + timedelta(minutes=1)
    target = await svc.mark_read(
        db_session, STORE_USER, stores.chennai, first.id, read_at, commit=False
    )
    assert (target.read_at, target.read_by, target.delivered_at) == (
        read_at,
        "store-01234",
        read_at,
    )
    again = await svc.mark_read(
        db_session, IT, stores.chennai, first.id, read_at + timedelta(hours=1), commit=False
    )
    assert (again.read_at, again.read_by) == (read_at, "store-01234")
    assert await unread_count(db_session, stores.chennai, read_at) == 1
    assert await unread_count(db_session, stores.bengaluru, read_at) == 2


async def test_read_needs_a_live_notification_of_the_store(
    db_session: AsyncSession, stores: Stores
) -> None:
    n = (await publish(db_session)).notification
    with pytest.raises(NotFoundError):  # Paris is not a target
        await svc.mark_read(db_session, STORE_USER, stores.paris, n.id, NOW, commit=False)
    scheduled = (await publish(db_session, publish_at=NOW + timedelta(hours=1))).notification
    with pytest.raises(NotFoundError):  # not published yet
        await svc.mark_read(db_session, STORE_USER, stores.chennai, scheduled.id, NOW, commit=False)
    await svc.withdraw_notification(db_session, IT, n.id, NOW, commit=False)
    with pytest.raises(NotFoundError):  # withdrawn
        await svc.mark_read(db_session, STORE_USER, stores.chennai, n.id, NOW, commit=False)


async def test_read_all(db_session: AsyncSession, stores: Stores) -> None:
    await publish(db_session, title="one")
    await publish(db_session, title="two")
    await publish(db_session, title="later", publish_at=NOW + timedelta(hours=1))
    assert await svc.mark_all_read(db_session, STORE_USER, stores.chennai, NOW, commit=False) == 2
    assert await unread_count(db_session, stores.chennai, NOW) == 0
    assert await svc.mark_all_read(db_session, STORE_USER, stores.chennai, NOW, commit=False) == 0
    # The scheduled one becomes unread when it goes live.
    assert await unread_count(db_session, stores.chennai, NOW + timedelta(hours=1)) == 1
    assert await unread_count(db_session, stores.bengaluru, NOW) == 2


# --- system notifications ----------------------------------------------------------------------
async def test_system_notification_once_per_journal_entry(db_session: AsyncSession) -> None:
    itsm = await build_itsm_fixture(db_session)
    incident = await incident_svc.create_incident(
        db_session, IT, itsm.store_a, new_incident(network_element_id=itsm.ap), NOW, commit=False
    )
    entry_id = await db_session.scalar(
        select(IncidentJournal.id).where(IncidentJournal.incident_id == incident.id)
    )
    content = incident_update(
        incident_number=incident.number, message="Engineer on the way.", publish_at=NOW
    )

    async def create():
        return await svc.create_system_notification(
            db_session,
            IT,
            event="incident_update",
            store_id=itsm.store_a,
            content=content,
            publish_at=NOW,
            incident_id=incident.id,
            source_journal_id=entry_id,
        )

    first = await create()
    second = await create()
    assert first.id == second.id
    assert (first.source, first.system_event, first.category) == (
        "system",
        "incident_update",
        "incidents",
    )
    assert first.title == f"Incident {incident.number} Updated"
    assert first.expires_at == NOW + timedelta(days=7)
    assert first.id in await live_ids(db_session, itsm.store_a)
    with pytest.raises(ConflictError, match="System notifications cannot be withdrawn"):
        await svc.withdraw_notification(db_session, IT, first.id, NOW, commit=False)
