# app/api/schemas/notifications.py
"""Request and response models for the notification routes.

Mobile field names follow the mock data. The mock's "icon" is replaced by
"category" (and systemEvent): the frontend maps them to icons.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import AwareDatetime, Field

from app.api.schemas.base import ApiModel, UtcDateTime
from app.models.values import NotificationCategory

SiteCode = Annotated[str, Field(pattern=r"^[0-9]{5}$")]


# --- mobile ----------------------------------------------------------------------------
class NotificationItem(ApiModel):
    id: uuid.UUID
    category: str  # display label: Maintenance, Updates, Incidents, Appointment, Training, Others
    title: str
    description: str
    link_url: str | None
    created_at: UtcDateTime  # publish_at: when the store could first see it
    system_event: str | None
    incident_id: str | None  # incident number, e.g. INC00001234
    appointment_id: uuid.UUID | None
    action_id: uuid.UUID | None
    is_read: bool


class UnreadCount(ApiModel):
    unread_count: int


class ReadResult(ApiModel):
    id: uuid.UUID
    is_read: bool
    read_at: UtcDateTime


class ReadAllResult(ApiModel):
    marked: int


# --- portal ----------------------------------------------------------------------------
class PublishRequest(ApiModel):
    """A portal announcement. publishAt / expiresAt are the portal's start and end
    times; the portal turns "all stores" or a region into explicit site codes."""

    category: NotificationCategory
    title: Annotated[str, Field(min_length=1, max_length=150)]
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    link_url: Annotated[str, Field(pattern=r"^https://", max_length=500)] | None = None
    target_site_codes: Annotated[list[SiteCode], Field(min_length=1, max_length=500)]
    publish_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None


class PortalNotification(ApiModel):
    id: uuid.UUID
    category: str
    title: str
    description: str
    link_url: str | None
    source: str
    status: str
    publish_at: UtcDateTime
    expires_at: UtcDateTime | None
    withdrawn_at: UtcDateTime | None
    created_by: str
    target_count: int


class DeliveryStore(ApiModel):
    site_code: str
    store_name: str
    delivered_at: UtcDateTime | None
    read_at: UtcDateTime | None
    read_by: str | None


class DeliveryReport(ApiModel):
    notification_id: uuid.UUID
    title: str
    status: str
    targets: int
    delivered: int
    read: int
    stores: list[DeliveryStore]
