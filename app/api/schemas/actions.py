# app/api/schemas/actions.py
"""Response model for GET /api/v1/actions."""

from __future__ import annotations

import uuid

from app.api.schemas.base import ApiModel, UtcDateTime


class ActionItem(ApiModel):
    id: uuid.UUID
    title: str
    detail: str
    severity: str  # critical, warning or info
    expected_response: str  # acknowledge, yes_no or free_text
    due_by: UtcDateTime | None
    due_by_text: str | None  # store time, e.g. "Today at 1:00 PM"
    incident_id: str  # incident number, e.g. INC00001234
    created_at: UtcDateTime
