# app/seed/itsm_schemas.py
"""Pydantic models for seed/data/incidents.yaml and seed/data/appointments.yaml."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator

from app.models.values import (
    AppointmentType,
    ContactType,
    HoldReason,
    IncidentCategory,
)
from app.seed.anchor import parse_offset
from app.seed.schemas import Offset, SeedModel, SiteCode

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Seeded numbers stay below the incident number sequence (starts at 2000).
MAX_SEED_INCIDENT_NUMBER = 1999


def _check_hhmm(value: str) -> str:
    if not _HHMM.match(value):
        raise ValueError(f'time {value!r} must be quoted HH:MM, e.g. "14:00"')
    return value


LocalTime = Annotated[str, AfterValidator(_check_hhmm)]
IncidentNumber = Annotated[str, Field(pattern=r"^INC[0-9]{8}$")]
Level = Annotated[int, Field(ge=1, le=3)]
AppointmentKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$")]


class JournalSeed(SeedModel):
    element: Literal["work_notes", "comments"]
    author_type: Literal["it_staff", "store_user"] = "it_staff"
    created_by: str
    at: Offset
    value: Annotated[str, Field(min_length=1)]


class IncidentSeed(SeedModel):
    number: IncidentNumber
    site: SiteCode
    short_description: str
    u_symptom: str | None = None
    business_service: str | None = None
    description: str | None = None
    category: IncidentCategory
    subcategory: str | None = None
    ci_element: str | None = None
    ci_device: str | None = None
    contact_type: ContactType
    caller: str | None = None
    opened_by: str
    impact: Level
    urgency: Level
    state: Literal[1, 2, 3, 6, 7]
    hold_reason: HoldReason | None = None
    assignment_group: str
    assigned_to: str | None = None
    eta_local: LocalTime | None = None
    eta_note: str | None = None
    opened_at: Offset
    resolved_at: Offset | None = None
    close_code: str | None = None
    close_notes: str | None = None
    journal: list[JournalSeed] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> IncidentSeed:
        n = self.number
        if int(n[3:]) > MAX_SEED_INCIDENT_NUMBER:
            raise ValueError(f"{n}: seeded numbers must be below INC00002000")
        if self.ci_element and self.ci_device:
            raise ValueError(f"{n}: set ci_element or ci_device, not both")
        if (self.state == 3) != (self.hold_reason is not None):
            raise ValueError(f"{n}: hold_reason is required exactly when state is 3")
        opened = parse_offset(self.opened_at)
        if self.state in (6, 7):
            if not (self.resolved_at and self.close_code and self.close_notes):
                raise ValueError(
                    f"{n}: resolved incidents need resolved_at, close_code, close_notes"
                )
            if parse_offset(self.resolved_at) <= opened:
                raise ValueError(f"{n}: resolved_at must be after opened_at")
        elif self.resolved_at or self.close_code or self.close_notes:
            raise ValueError(f"{n}: only resolved incidents have resolved_at / close fields")
        for entry in self.journal:
            if parse_offset(entry.at) < opened:
                raise ValueError(f"{n}: journal entry at {entry.at} is before opened_at")
        return self


class IncidentsSeed(SeedModel):
    incidents: list[IncidentSeed]


class AppointmentSeed(SeedModel):
    key: AppointmentKey
    incident: IncidentNumber
    appointment_type: AppointmentType
    title: str
    purpose: str
    details: str | None = None
    day: Literal["today", "tomorrow"]
    at_local: LocalTime
    duration_minutes: Annotated[int, Field(ge=15, le=480)]
    booked_at: Offset  # when IT booked the visit: the "appointment scheduled" notice time
    vendor_name: str | None = None
    technician_name: str | None = None


class AppointmentsSeed(SeedModel):
    appointments: list[AppointmentSeed]
