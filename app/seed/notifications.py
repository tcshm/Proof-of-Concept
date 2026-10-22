# app/seed/notifications.py
"""Seed part 3: portal announcements (seed/data/notifications.yaml).

The step goes through the notification service, exactly like the admin portal:
publish -> (optionally) mark read for some stores -> (optionally) withdraw.
Times are backdated by passing "now" = the publish time.

Re-running deletes the previously seeded announcements (source = 'seed') and
publishes them again. Appointment notices belong to the itsm step.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import Field, ValidationError, model_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import ActorContext
from app.models import Notification, NotificationTarget, Store
from app.models.values import ActorType, AuditSource, NotificationCategory, NotificationSource
from app.seed.loader import DEFAULT_DATA_DIR, SeedDataError
from app.seed.schemas import SeedModel, SiteCode
from app.services.notifications import (
    NewAnnouncement,
    mark_read,
    publish_notification,
    withdraw_notification,
)

NOTIFICATIONS_FILE = "notifications.yaml"
SEED_ACTOR = ActorContext.system("seed", AuditSource.SEED)
# Reads and withdrawals happen this long after publishing.
FOLLOW_UP = timedelta(minutes=30)


class AnnouncementSeed(SeedModel):
    key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$")]
    category: NotificationCategory
    title: Annotated[str, Field(min_length=1, max_length=150)]
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    link_url: Annotated[str, Field(pattern=r"^https://")] | None = None
    # Exactly one way to choose the stores (the portal turns these into site codes).
    all_stores: bool = False
    region: str | None = None
    sites: list[SiteCode] = Field(default_factory=list)
    # Minutes from the anchor: negative = already published, positive = scheduled.
    publish_offset_minutes: Annotated[int, Field(ge=-30 * 24 * 60, le=7 * 24 * 60)]
    expires_after_hours: Annotated[int, Field(ge=1, le=90 * 24)] | None = None
    read_by_sites: list[SiteCode] = Field(default_factory=list)
    withdrawn: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> AnnouncementSeed:
        modes = [self.all_stores, self.region is not None, bool(self.sites)]
        if sum(modes) != 1:
            raise ValueError(f"{self.key}: set exactly one of all_stores, region or sites")
        if (self.read_by_sites or self.withdrawn) and self.publish_offset_minutes > 0:
            raise ValueError(f"{self.key}: a scheduled notification cannot be read or withdrawn")
        return self


class AnnouncementsSeed(SeedModel):
    notifications: list[AnnouncementSeed]

    @model_validator(mode="after")
    def _unique_keys(self) -> AnnouncementsSeed:
        keys = [n.key for n in self.notifications]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        if duplicates:
            raise ValueError(f"duplicate keys: {duplicates}")
        return self


def load_announcements(data_dir: Path = DEFAULT_DATA_DIR) -> AnnouncementsSeed:
    path = data_dir / NOTIFICATIONS_FILE
    if not path.is_file():
        raise SeedDataError(f"{NOTIFICATIONS_FILE}: file not found in {data_dir}")
    try:
        return AnnouncementsSeed.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (yaml.YAMLError, ValidationError) as exc:
        raise SeedDataError(f"{NOTIFICATIONS_FILE}: {exc}") from exc


async def clear_seeded_announcements(session: AsyncSession) -> None:
    seeded = select(Notification.id).where(Notification.source == NotificationSource.SEED)
    await session.execute(
        delete(NotificationTarget).where(NotificationTarget.notification_id.in_(seeded))
    )
    await session.execute(
        delete(Notification).where(Notification.source == NotificationSource.SEED)
    )


async def write_announcements(
    session: AsyncSession, announcements: AnnouncementsSeed, anchor: datetime
) -> tuple[int, int]:
    """Publish every announcement. Returns (notifications, store targets)."""
    stores = (await session.execute(select(Store.site_code, Store.region, Store.id))).all()
    store_ids = {site: store_id for site, _, store_id in stores}
    by_region: dict[str, list[str]] = {}
    for site, region, _ in stores:
        by_region.setdefault(region, []).append(site)

    await clear_seeded_announcements(session)
    targets_total = 0
    for item in announcements.notifications:
        if item.all_stores:
            sites = sorted(store_ids)
        elif item.region is not None:
            sites = sorted(by_region.get(item.region, []))
            if not sites:
                raise SeedDataError(f"{NOTIFICATIONS_FILE}: {item.key}: no stores in {item.region}")
        else:
            sites = item.sites

        offset = timedelta(minutes=item.publish_offset_minutes)
        if offset <= timedelta(0):
            now, publish_at = anchor + offset, None
            published = now
        else:
            now, publish_at = anchor, anchor + offset
            published = publish_at
        expires_at = (
            published + timedelta(hours=item.expires_after_hours)
            if item.expires_after_hours
            else None
        )
        result = await publish_notification(
            session,
            SEED_ACTOR,
            NewAnnouncement(
                category=item.category,
                title=item.title,
                description=item.description,
                target_site_codes=sites,
                link_url=item.link_url,
                publish_at=publish_at,
                expires_at=expires_at,
            ),
            now,
            commit=False,
        )
        targets_total += result.target_count

        for site in item.read_by_sites:
            if site not in sites:
                raise SeedDataError(f"{NOTIFICATIONS_FILE}: {item.key}: {site} is not a target")
            reader = ActorContext(
                actor_type=ActorType.STORE_USER, actor=f"store-{site}", source=AuditSource.SEED
            )
            await mark_read(
                session,
                reader,
                store_ids[site],
                result.notification.id,
                published + FOLLOW_UP,
                commit=False,
            )
        if item.withdrawn:
            await withdraw_notification(
                session, SEED_ACTOR, result.notification.id, published + FOLLOW_UP, commit=False
            )
    return len(announcements.notifications), targets_total
