# app/api/context.py
"""Who and where: the store a request is for, and the actor making a change.

Mobile requests name their store with the X-Site-Id header. get_store_context
validates it, loads the store once, and returns a small read-only StoreContext.
Every mobile route filters by store_context.id, so store A never sees store B.

ActorContext (app/core/actor.py) records who is making a change; the mobile
and portal dependencies below build it from the request.

When Entra ID authentication arrives, only these dependencies change (the store
and the user come from the token); no route changes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, Header, Request
from sqlalchemy import select

from app.api.deps import SessionDep
from app.core.actor import ActorContext
from app.core.errors import NotFoundError
from app.core.request_context import get_request_id
from app.models import Store
from app.models.values import ActorType, AuditSource

SITE_ID_HEADER = "X-Site-Id"
USER_HEADER = "X-User"
SITE_CODE_PATTERN = r"^[0-9]{5}$"

DEFAULT_PORTAL_USER = "portal-user"


@dataclass(frozen=True)
class StoreContext:
    id: uuid.UUID
    site_code: str
    name: str
    city: str | None
    region: str
    timezone: ZoneInfo
    business_hours: dict[str, Any] = field(default_factory=dict)
    status: str = "open"


def _current_request_uuid() -> uuid.UUID | None:
    value = get_request_id()
    return uuid.UUID(value) if value else None


async def get_store_context(
    request: Request,
    session: SessionDep,
    site_code: Annotated[str, Header(alias=SITE_ID_HEADER, pattern=SITE_CODE_PATTERN)],
) -> StoreContext:
    """Resolve X-Site-Id to a store. Missing or malformed header: 422. Unknown: 404."""
    request.state.site_code = site_code
    row = (
        await session.execute(
            select(
                Store.id,
                Store.site_code,
                Store.name,
                Store.city,
                Store.region,
                Store.timezone,
                Store.business_hours,
                Store.status,
            ).where(Store.site_code == site_code)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError(f"Store {site_code} was not found.")
    return StoreContext(
        id=row.id,
        site_code=row.site_code,
        name=row.name,
        city=row.city,
        region=row.region,
        timezone=ZoneInfo(row.timezone),
        business_hours=row.business_hours or {},
        status=row.status,
    )


StoreDep = Annotated[StoreContext, Depends(get_store_context)]


def _user_from_header(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()[:100]
    return cleaned or None


async def get_mobile_actor(
    request: Request,
    store: StoreDep,
    user: Annotated[str | None, Header(alias=USER_HEADER)] = None,
) -> ActorContext:
    """A store user on the mobile app. Defaults to 'store-<site code>' without X-User."""
    actor = ActorContext(
        actor_type=ActorType.STORE_USER,
        actor=_user_from_header(user) or f"store-{store.site_code}",
        source=AuditSource.MOBILE_APP,
        request_id=_current_request_uuid(),
    )
    request.state.actor = actor.actor
    return actor


async def get_portal_actor(
    request: Request,
    user: Annotated[str | None, Header(alias=USER_HEADER)] = None,
) -> ActorContext:
    """An IT / command centre user on the admin portal."""
    actor = ActorContext(
        actor_type=ActorType.IT_STAFF,
        actor=_user_from_header(user) or DEFAULT_PORTAL_USER,
        source=AuditSource.ADMIN_PORTAL,
        request_id=_current_request_uuid(),
    )
    request.state.actor = actor.actor
    return actor


MobileActorDep = Annotated[ActorContext, Depends(get_mobile_actor)]
PortalActorDep = Annotated[ActorContext, Depends(get_portal_actor)]
