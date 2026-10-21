# app/models/itsm.py
"""Group 3: ITSM. incident_journal is also the worker queue.

Every appointment belongs to an incident (incident_id is NOT NULL since revision 0002).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    SmallInteger,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAt, ServiceNowSysId, UpdatedAt, UUIDPk


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("id", "store_id"),
        ForeignKeyConstraint(
            ["cmdb_ci_network_element_id", "store_id"],
            ["network_elements.id", "network_elements.store_id"],
        ),
        ForeignKeyConstraint(
            ["cmdb_ci_device_id", "store_id"],
            ["devices.id", "devices.store_id"],
        ),
    )

    id: Mapped[UUIDPk]
    number: Mapped[str] = mapped_column(unique=True)
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"))
    short_description: Mapped[str]
    description: Mapped[str | None]
    u_symptom: Mapped[str | None]
    business_service: Mapped[str | None]
    category: Mapped[str]
    subcategory: Mapped[str | None]
    cmdb_ci_network_element_id: Mapped[uuid.UUID | None]
    cmdb_ci_device_id: Mapped[uuid.UUID | None]
    contact_type: Mapped[str]
    caller: Mapped[str | None]
    opened_by: Mapped[str | None]
    impact: Mapped[int] = mapped_column(SmallInteger)
    urgency: Mapped[int] = mapped_column(SmallInteger)
    priority: Mapped[int | None] = mapped_column(
        SmallInteger, Computed("impact + urgency - 1", persisted=True)
    )
    state: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    hold_reason: Mapped[str | None]
    assignment_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assignment_groups.id")
    )
    assigned_to: Mapped[str | None]
    u_resolution_eta: Mapped[datetime | None]
    u_eta_note: Mapped[str | None]
    opened_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    close_code: Mapped[str | None]
    close_notes: Mapped[str | None]
    servicenow_sys_id: Mapped[ServiceNowSysId]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class IncidentJournal(Base):
    __tablename__ = "incident_journal"

    id: Mapped[UUIDPk]
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"))
    element: Mapped[str]
    value: Mapped[str]
    author_type: Mapped[str]
    created_by: Mapped[str]
    source: Mapped[str]
    process_status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_error: Mapped[str | None]
    processed_at: Mapped[datetime | None]
    servicenow_sys_id: Mapped[ServiceNowSysId]
    created_at: Mapped[CreatedAt]


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["incident_id", "store_id"],
            ["incidents.id", "incidents.store_id"],
        ),
    )

    id: Mapped[UUIDPk]
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"))
    incident_id: Mapped[uuid.UUID]
    appointment_type: Mapped[str]
    title: Mapped[str]
    purpose: Mapped[str]
    details: Mapped[str | None]
    scheduled_start: Mapped[datetime]
    scheduled_end: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(server_default=text("'scheduled'"))
    status_note: Mapped[str | None]
    vendor_name: Mapped[str | None]
    technician_name: Mapped[str | None]
    vendor_reference: Mapped[str | None]
    created_by: Mapped[str]
    source: Mapped[str]
    servicenow_sys_id: Mapped[ServiceNowSysId]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
