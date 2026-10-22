# app/api/schemas/alerts.py
"""Response model for GET /api/v1/alerts (mock AlertItem: id, title, detail, incidentId;
plus severity and createdAt)."""

from __future__ import annotations

import uuid

from app.api.schemas.base import ApiModel, UtcDateTime


class AlertItem(ApiModel):
    id: uuid.UUID
    title: str
    detail: str
    severity: str  # critical, warning or info
    incident_id: str  # incident number, e.g. INC00001234
    created_at: UtcDateTime
