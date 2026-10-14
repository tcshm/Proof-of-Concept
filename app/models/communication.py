# app/models/communication.py
"""Group 5: store communication (alerts, actions, replies, notifications)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, ForeignKeyConstraint, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAt, UpdatedAt, UUIDPk


class StoreAlert(Base):
    __tablename__ = "store_alerts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["incident_id", "store_id"],
            ["incidents.id", "incidents.store_id"],
        ),
    )

    id: Mapped[UUIDPk]
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"))
    incident_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    title: Mapped[str]
    detail: Mapped[str]
    severity: Mapped[str] = mapped_column(server_default=text("'warning'"))
    source: Mapped[str]
    ai_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_workflow_runs.id"))
    created_by: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=text("'active'"))
    close_reason: Mapped[str | None]
    closed_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class StoreAction(Base):
    __tablename__ = "store_actions"
    __table_args__ = (
        UniqueConstraint("id", "store_id"),
        ForeignKeyConstraint(
            ["incident_id", "store_id"],
            ["incidents.id", "incidents.store_id"],
        ),
    )

    id: Mapped[UUIDPk]
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"))
    incident_id: Mapped[uuid.UUID]
    source_journal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("incident_journal.id"))
    title: Mapped[str]
    detail: Mapped[str]
    severity: Mapped[str] = mapped_column(server_default=text("'warning'"))
    expected_response: Mapped[str]
    due_by: Mapped[datetime | None]
    source: Mapped[str]
    ai_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_workflow_runs.id"))
    created_by: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=text("'open'"))
    close_reason: Mapped[str | None]
    closed_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class StoreActionResponse(Base):
    __tablename__ = "store_action_responses"
    __table_args__ = (
        ForeignKeyConstraint(
            ["action_id", "store_id"],
            ["store_actions.id", "store_actions.store_id"],
        ),
    )

    id: Mapped[UUIDPk]
    action_id: Mapped[uuid.UUID]
    store_id: Mapped[uuid.UUID]
    response_type: Mapped[str]
    answer_yes_no: Mapped[bool | None]
    response_text: Mapped[str | None]
    responded_by: Mapped[str]
    source: Mapped[str]
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(unique=True)
    journal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident_journal.id"), unique=True)
    created_at: Mapped[CreatedAt]


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[UUIDPk]
    category: Mapped[str]
    title: Mapped[str]
    description: Mapped[str]
    link_url: Mapped[str | None]
    source: Mapped[str]
    system_event: Mapped[str | None]
    created_by: Mapped[str]
    incident_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("incidents.id"))
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("appointments.id"))
    action_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("store_actions.id"))
    source_journal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("incident_journal.id"), unique=True
    )
    status: Mapped[str] = mapped_column(server_default=text("'published'"))
    publish_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime | None]
    withdrawn_at: Mapped[datetime | None]
    withdrawn_by: Mapped[str | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class NotificationTarget(Base):
    __tablename__ = "notification_targets"

    notification_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notifications.id"), primary_key=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"), primary_key=True)
    delivered_at: Mapped[datetime | None]
    read_at: Mapped[datetime | None]
    read_by: Mapped[str | None]
    created_at: Mapped[CreatedAt]
