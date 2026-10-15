# app/seed/schemas.py
"""Pydantic models for the seed files. A file that does not match fails loudly
with the file name and field, before anything is written to the database."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.values import AiWorkflow, ConnectionType, Criticality, DeviceStatus
from app.seed.anchor import WINDOW, parse_offset

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _check_offset(value: str) -> str:
    if -parse_offset(value) > WINDOW * 30:
        raise ValueError(f"offset {value!r} is unreasonably far in the past")
    return value


def _check_window_offset(value: str) -> str:
    if -parse_offset(value) > WINDOW:
        raise ValueError(f"offset {value!r} is outside the 24-hour event window")
    return value


Offset = Annotated[str, AfterValidator(_check_offset)]
WindowOffset = Annotated[str, AfterValidator(_check_window_offset)]
SiteCode = Annotated[str, Field(pattern=r"^[0-9]{5}$")]
Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class SeedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- master.yaml ---------------------------------------------------------------
class AssignmentGroupSeed(SeedModel):
    name: str
    description: str
    email: str | None = None


class NetworkElementTypeSeed(SeedModel):
    code: Key
    display_name: str
    business_criticality: Criticality
    aruba_device_type: Literal["ACCESS POINT", "SWITCH", "GATEWAY"] | None = None
    servicenow_ci_class: str | None = None
    sort_order: int = 0


class DeviceTypeSeed(SeedModel):
    code: Key
    display_name: str
    business_criticality: Criticality
    servicenow_ci_class: str | None = None
    sort_order: int = 0


class MasterSeed(SeedModel):
    assignment_groups: list[AssignmentGroupSeed]
    network_element_types: list[NetworkElementTypeSeed]
    device_types: list[DeviceTypeSeed]

    @model_validator(mode="after")
    def _unique(self) -> MasterSeed:
        for label, values in (
            ("assignment group name", [g.name for g in self.assignment_groups]),
            ("network element type code", [t.code for t in self.network_element_types]),
            ("device type code", [t.code for t in self.device_types]),
        ):
            duplicates = {v for v in values if values.count(v) > 1}
            if duplicates:
                raise ValueError(f"duplicate {label}: {sorted(duplicates)}")
        return self


# --- stores.yaml ---------------------------------------------------------------
class StoreSeed(SeedModel):
    site_code: SiteCode
    name: str
    region: str
    country_code: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    city: str | None = None
    address_line: str | None = None
    postal_code: str | None = None
    latitude: Annotated[Decimal, Field(ge=-90, le=90)]
    longitude: Annotated[Decimal, Field(ge=-180, le=180)]
    timezone: str
    business_hours: dict[str, list[tuple[str, str]]]
    contact_phone: str | None = None
    contact_email: str | None = None
    isp_primary: str
    isp_backup: str

    @field_validator("timezone")
    @classmethod
    def _real_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {value!r}") from exc
        return value

    @field_validator("business_hours")
    @classmethod
    def _valid_hours(cls, value: dict[str, list[tuple[str, str]]]) -> dict:
        for day, intervals in value.items():
            if day not in _DAYS:
                raise ValueError(f"unknown day {day!r}: use {', '.join(_DAYS)}")
            for start, end in intervals:
                if not (_HHMM.match(start) and _HHMM.match(end)):
                    raise ValueError(f"{day}: times must be quoted HH:MM, got {start!r}-{end!r}")
                if start >= end:
                    raise ValueError(f"{day}: opening {start} must be before closing {end}")
        return value


class StoresSeed(SeedModel):
    stores: list[StoreSeed]

    @model_validator(mode="after")
    def _unique(self) -> StoresSeed:
        codes = [s.site_code for s in self.stores]
        duplicates = {c for c in codes if codes.count(c) > 1}
        if duplicates:
            raise ValueError(f"duplicate site_code: {sorted(duplicates)}")
        return self


# --- inventory_template.yaml -------------------------------------------------
class ElementTemplate(SeedModel):
    key: Key
    type: Key
    name: str
    role: str | None = None
    location: str | None = None
    serial: str | None = None
    ip: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    uplink: Key | None = None
    uplink_port: str | None = None
    source_gateway: Key | None = None
    source_uplink: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> ElementTemplate:
        if (self.source_gateway is None) != (self.source_uplink is None):
            raise ValueError(f"{self.key}: source_gateway and source_uplink go together")
        if self.uplink_port is not None and self.uplink is None:
            raise ValueError(f"{self.key}: uplink_port needs uplink")
        return self


class DeviceTemplate(SeedModel):
    type: Key
    name: str
    count: Annotated[int, Field(ge=1, le=99)]
    location: str | None = None
    serial: str
    ip: str | None = None
    connection: ConnectionType
    connected_to: Key | list[Key]
    port_start: Annotated[int, Field(ge=1)] | None = None
    user_role: str | None = None
    vlan: str | None = None
    manufacturer: str | None = None
    model: str | None = None

    @model_validator(mode="after")
    def _ports(self) -> DeviceTemplate:
        if self.connection == ConnectionType.WIRED and self.port_start is None:
            raise ValueError(f"{self.type}: wired devices need port_start")
        if self.connection == ConnectionType.WIRELESS and self.port_start is not None:
            raise ValueError(f"{self.type}: wireless devices have no port_start")
        return self

    @property
    def connection_keys(self) -> list[str]:
        return [self.connected_to] if isinstance(self.connected_to, str) else self.connected_to


class InventoryTemplate(SeedModel):
    network_elements: list[ElementTemplate]
    devices: list[DeviceTemplate]

    @model_validator(mode="after")
    def _references(self) -> InventoryTemplate:
        seen: set[str] = set()
        for element in self.network_elements:
            if element.key in seen:
                raise ValueError(f"duplicate element key {element.key!r}")
            for ref in (element.uplink, element.source_gateway):
                if ref is not None and ref not in seen:
                    raise ValueError(
                        f"{element.key}: references {ref!r}, which must be defined above it"
                    )
            seen.add(element.key)
        for device in self.devices:
            for ref in device.connection_keys:
                if ref not in seen:
                    raise ValueError(f"{device.type}: unknown element key {ref!r}")
        return self


# --- store_overrides.yaml ----------------------------------------------------
class DeviceOverride(SeedModel):
    status: DeviceStatus
    since: Offset
    reason: str | None = None


class StoreOverride(SeedModel):
    devices: dict[str, DeviceOverride] = Field(default_factory=dict)


class OverridesSeed(SeedModel):
    stores: dict[SiteCode, StoreOverride] = Field(default_factory=dict)


# --- event_scenarios.yaml ----------------------------------------------------
class Outage(SeedModel):
    element: Key
    down_at: WindowOffset
    up_at: WindowOffset | None = None

    @model_validator(mode="after")
    def _order(self) -> Outage:
        if self.up_at is not None and parse_offset(self.up_at) <= parse_offset(self.down_at):
            raise ValueError(f"{self.element}: up_at must be after down_at")
        return self


class DuplicateEdge(SeedModel):
    element: Key


class LateEdge(SeedModel):
    element: Key
    status: Literal["up", "down"]
    event_at: WindowOffset
    ingested_at: WindowOffset

    @model_validator(mode="after")
    def _late(self) -> LateEdge:
        if parse_offset(self.ingested_at) <= parse_offset(self.event_at):
            raise ValueError(f"{self.element}: ingested_at must be after event_at")
        return self


class UnmatchedEdge(SeedModel):
    serial: str
    hostname: str
    at: WindowOffset


class StoreScenario(SeedModel):
    outages: list[Outage] = Field(default_factory=list)
    duplicates: list[DuplicateEdge] = Field(default_factory=list)
    late_events: list[LateEdge] = Field(default_factory=list)
    unmatched: list[UnmatchedEdge] = Field(default_factory=list)


class ScenariosSeed(SeedModel):
    baseline_at: WindowOffset
    client_events_per_ap: tuple[int, int]
    stores: dict[SiteCode, StoreScenario] = Field(default_factory=dict)

    @field_validator("client_events_per_ap")
    @classmethod
    def _range(cls, value: tuple[int, int]) -> tuple[int, int]:
        low, high = value
        if not 0 <= low <= high <= 20:
            raise ValueError("client_events_per_ap must be [low, high] with 0 <= low <= high <= 20")
        return value


# --- prompts.yaml ------------------------------------------------------------
class PromptSeed(SeedModel):
    workflow: AiWorkflow
    version: Annotated[int, Field(ge=1)]
    is_active: bool
    model_name: str
    temperature: Annotated[Decimal, Field(ge=0, le=2)]
    created_by: str
    change_notes: str | None = None
    system_prompt: str
    user_prompt_template: str
    output_schema: dict[str, Any]


class PromptsSeed(SeedModel):
    prompts: list[PromptSeed]

    @model_validator(mode="after")
    def _one_active(self) -> PromptsSeed:
        active = [p.workflow for p in self.prompts if p.is_active]
        duplicates = {w for w in active if active.count(w) > 1}
        if duplicates:
            raise ValueError(f"more than one active prompt for: {sorted(duplicates)}")
        return self
