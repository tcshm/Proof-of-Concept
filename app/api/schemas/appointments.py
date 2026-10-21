# app/api/schemas/appointments.py
"""Response model for GET /api/v1/appointments.

Field names follow the mobile mock data. The mock's "icon" is replaced by
appointmentType (user_support -> person icon, the other types -> tool icon).
"""

from __future__ import annotations

import uuid

from app.api.schemas.base import ApiModel, UtcDateTime


class AppointmentItem(ApiModel):
    id: uuid.UUID
    appointment_type: str
    title: str
    sub_title: str
    when: str
    scheduled_start: UtcDateTime
    scheduled_end: UtcDateTime | None
    status: str
    details: str | None
    incident_id: str  # the incident number, e.g. INC00001235
