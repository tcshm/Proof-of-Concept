# app/api/schemas/incidents.py
"""Response models for GET /api/v1/incidents and GET /api/v1/incidents/{number}.

Field names follow the mobile mock data. The mock's "icon" is replaced by
ciType (the frontend maps the type code to its icon).
"""

from __future__ import annotations

import uuid

from pydantic import Field

from app.api.schemas.base import ApiModel, UtcDateTime


class IncidentItem(ApiModel):
    id: str  # the incident number, e.g. INC00001234
    title: str
    subtitle: str | None
    service: str | None
    summary: str | None
    ci_type: str | None
    severity: str
    status: str
    badge: str
    reported_at: UtcDateTime
    incident_type: str
    incident_status_new: str
    resolution_eta: str | None = Field(alias="resolutionETA")
    resolution_group: str | None


class IncidentComment(ApiModel):
    id: uuid.UUID
    text: str
    author_type: str
    author: str
    created_at: UtcDateTime


class IncidentDetail(IncidentItem):
    description: str | None
    assigned_to: str | None
    comments: list[IncidentComment]
