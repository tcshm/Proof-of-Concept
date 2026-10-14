# app/models/audit.py
"""Group 6: audit and logs. All three tables are append-only."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Identity, Integer, SmallInteger, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonObject


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    actor_type: Mapped[str]
    actor: Mapped[str]
    source: Mapped[str]
    action: Mapped[str]
    entity_type: Mapped[str]
    entity_id: Mapped[str]
    store_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stores.id"))
    changes: Mapped[JsonObject]
    request_id: Mapped[uuid.UUID | None]


class ApiRequestLog(Base):
    __tablename__ = "api_request_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    client_app: Mapped[str]
    method: Mapped[str]
    path: Mapped[str]
    route: Mapped[str | None]
    site_code: Mapped[str | None]
    actor: Mapped[str | None]
    status_code: Mapped[int] = mapped_column(SmallInteger)
    duration_ms: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None]


class EventProcessingRun(Base):
    __tablename__ = "event_processing_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    mode: Mapped[str]
    as_of: Mapped[datetime]
    status: Mapped[str] = mapped_column(server_default=text("'running'"))
    events_read: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    events_processed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    duplicates: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    unmatched: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    errors: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    status_changes: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error_message: Mapped[str | None]
    triggered_by: Mapped[str]
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
