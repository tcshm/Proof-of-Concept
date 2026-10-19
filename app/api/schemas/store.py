# app/api/schemas/store.py
"""Response model for GET /api/v1/store."""

from __future__ import annotations

from app.api.schemas.base import ApiModel


class StoreResponse(ApiModel):
    site_code: str
    name: str
    city: str | None
    region: str
    timezone: str
    timezone_abbreviation: str
    local_time: str
    is_open_now: bool
