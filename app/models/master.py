# app/models/master.py
"""Group 1: master data."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import CHAR, Numeric, SmallInteger, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAt, JsonObject, ServiceNowSysId, UpdatedAt, UUIDPk


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[UUIDPk]
    site_code: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    region: Mapped[str]
    country_code: Mapped[str] = mapped_column(CHAR(2))
    city: Mapped[str | None]
    address_line: Mapped[str | None]
    postal_code: Mapped[str | None]
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    timezone: Mapped[str]
    business_hours: Mapped[JsonObject]
    contact_phone: Mapped[str | None]
    contact_email: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=text("'open'"))
    servicenow_sys_id: Mapped[ServiceNowSysId]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class AssignmentGroup(Base):
    __tablename__ = "assignment_groups"

    id: Mapped[UUIDPk]
    name: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str | None]
    email: Mapped[str | None]
    servicenow_sys_id: Mapped[ServiceNowSysId]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class NetworkElementType(Base):
    __tablename__ = "network_element_types"

    code: Mapped[str] = mapped_column(primary_key=True)
    display_name: Mapped[str]
    business_criticality: Mapped[str]
    aruba_device_type: Mapped[str | None]
    servicenow_ci_class: Mapped[str | None]
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class DeviceType(Base):
    __tablename__ = "device_types"

    code: Mapped[str] = mapped_column(primary_key=True)
    display_name: Mapped[str]
    business_criticality: Mapped[str]
    servicenow_ci_class: Mapped[str | None]
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


__all__ = ["AssignmentGroup", "DeviceType", "NetworkElementType", "Store"]
