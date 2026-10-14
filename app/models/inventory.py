# app/models/inventory.py
"""Group 2: network and device inventory."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    SmallInteger,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, MACADDR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAt, JsonObject, ServiceNowSysId, UpdatedAt, UUIDPk


class NetworkElement(Base):
    __tablename__ = "network_elements"
    __table_args__ = (
        UniqueConstraint("store_id", "name"),
        UniqueConstraint("id", "store_id"),
        ForeignKeyConstraint(
            ["uplink_element_id", "store_id"],
            ["network_elements.id", "network_elements.store_id"],
        ),
        ForeignKeyConstraint(
            ["source_gateway_id", "store_id"],
            ["network_elements.id", "network_elements.store_id"],
        ),
    )

    id: Mapped[UUIDPk]
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"))
    element_type_code: Mapped[str] = mapped_column(ForeignKey("network_element_types.code"))
    name: Mapped[str]
    role: Mapped[str | None]
    location_in_store: Mapped[str | None]
    serial_number: Mapped[str | None] = mapped_column(unique=True)
    mac_address: Mapped[str | None] = mapped_column(MACADDR)
    ip_address: Mapped[str | None] = mapped_column(INET)
    manufacturer: Mapped[str | None]
    model: Mapped[str | None]
    uplink_element_id: Mapped[uuid.UUID | None]
    uplink_port: Mapped[str | None]
    source_gateway_id: Mapped[uuid.UUID | None]
    source_uplink_name: Mapped[str | None]
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    attributes: Mapped[JsonObject]
    servicenow_sys_id: Mapped[ServiceNowSysId]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("store_id", "name"),
        UniqueConstraint("id", "store_id"),
        ForeignKeyConstraint(
            ["connected_network_element_id", "store_id"],
            ["network_elements.id", "network_elements.store_id"],
        ),
    )

    id: Mapped[UUIDPk]
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"))
    device_type_code: Mapped[str] = mapped_column(ForeignKey("device_types.code"))
    name: Mapped[str]
    location_in_store: Mapped[str | None]
    serial_number: Mapped[str | None] = mapped_column(unique=True)
    mac_address: Mapped[str | None] = mapped_column(MACADDR)
    ip_address: Mapped[str | None] = mapped_column(INET)
    manufacturer: Mapped[str | None]
    model: Mapped[str | None]
    connection_type: Mapped[str | None]
    connected_network_element_id: Mapped[uuid.UUID | None]
    connected_port: Mapped[str | None]
    user_role: Mapped[str | None]
    vlan: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=text("'online'"))
    status_since: Mapped[datetime] = mapped_column(server_default=func.now())
    status_reason: Mapped[str | None]
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(server_default=func.now())
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    attributes: Mapped[JsonObject]
    servicenow_sys_id: Mapped[ServiceNowSysId]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class RawNetworkEvent(Base):
    __tablename__ = "raw_network_events"

    event_id: Mapped[str] = mapped_column(primary_key=True)
    schema_version: Mapped[str]
    event_type: Mapped[str]
    event_time: Mapped[datetime]
    ingestion_time: Mapped[datetime]
    source: Mapped[str]
    vendor: Mapped[str]
    device_id: Mapped[str | None]
    site_id: Mapped[str]
    country: Mapped[str | None]
    incident_id: Mapped[str | None]
    correlation_id: Mapped[str | None]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    processing_status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    processed_at: Mapped[datetime | None]
    process_error: Mapped[str | None]


class NetworkEvent(Base):
    __tablename__ = "network_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["network_element_id", "store_id"],
            ["network_elements.id", "network_elements.store_id"],
        ),
    )

    event_id: Mapped[str] = mapped_column(
        ForeignKey("raw_network_events.event_id"), primary_key=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"))
    network_element_id: Mapped[uuid.UUID | None]
    matched_by: Mapped[str]
    occurred_at: Mapped[datetime]
    ingested_at: Mapped[datetime]
    source: Mapped[str]
    vendor_event_uuid: Mapped[str | None] = mapped_column(unique=True)
    vendor_event_number: Mapped[str | None]
    vendor_event_type: Mapped[str]
    correlation_id: Mapped[str | None]
    aruba_device_type: Mapped[str | None]
    hostname: Mapped[str | None]
    device_serial: Mapped[str | None]
    device_mac: Mapped[str | None] = mapped_column(MACADDR)
    level: Mapped[str | None]
    description: Mapped[str | None]
    reported_status: Mapped[str | None]
    status_applied: Mapped[bool] = mapped_column(server_default=text("false"))
    uplink_name: Mapped[str | None]
    client_mac: Mapped[str | None] = mapped_column(MACADDR)
    client_ip: Mapped[str | None] = mapped_column(INET)
    client_hostname: Mapped[str | None]
    username: Mapped[str | None]
    user_role: Mapped[str | None]
    vlan: Mapped[str | None]
    bssid: Mapped[str | None]
    ssid: Mapped[str | None]
    band: Mapped[str | None]
    channel: Mapped[str | None]
    rssi: Mapped[int | None] = mapped_column(SmallInteger)
    snr: Mapped[int | None] = mapped_column(SmallInteger)
    retry_rate: Mapped[int | None] = mapped_column(SmallInteger)
    reason: Mapped[str | None]
    dhcp_latency_ms: Mapped[int | None] = mapped_column(Integer)
    roaming_latency_ms: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[JsonObject]


class NetworkElementCurrentStatus(Base):
    __tablename__ = "network_element_current_status"
    __table_args__ = (
        ForeignKeyConstraint(
            ["network_element_id", "store_id"],
            ["network_elements.id", "network_elements.store_id"],
        ),
    )

    network_element_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"))
    status: Mapped[str] = mapped_column(server_default=text("'unknown'"))
    status_since: Mapped[datetime | None]
    last_status_event_id: Mapped[str | None] = mapped_column(ForeignKey("network_events.event_id"))
    last_event_at: Mapped[datetime | None]
    updated_at: Mapped[UpdatedAt]
