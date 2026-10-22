# app/api/schemas/actions.py
"""Request and response models for the store action routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import Field, StrictBool

from app.api.schemas.base import ApiModel, UtcDateTime


class ActionReplyInfo(ApiModel):
    """The store's latest reply to an action."""

    answer_yes_no: bool | None
    response_text: str | None
    responded_by: str
    responded_at: UtcDateTime


class ActionItem(ApiModel):
    id: uuid.UUID
    title: str
    detail: str
    severity: str  # critical, warning or info
    expected_response: str  # acknowledge, yes_no or free_text
    due_by: UtcDateTime | None
    due_by_text: str | None  # store time, e.g. "Today at 1:00 PM"
    status: str  # open, or responded (answered, waiting for IT)
    response: ActionReplyInfo | None
    incident_id: str  # incident number, e.g. INC00001234
    created_at: UtcDateTime


class ActionReplyRequest(ApiModel):
    """The app creates clientRequestId once per tap and reuses it on every retry."""

    client_request_id: uuid.UUID
    answer_yes_no: StrictBool | None = None
    response_text: Annotated[str, Field(max_length=2000)] | None = None


class ActionReplyResult(ApiModel):
    id: uuid.UUID
    action_id: uuid.UUID
    response_type: str
    answer_yes_no: bool | None
    response_text: str | None
    responded_by: str
    responded_at: UtcDateTime
    action_status: str
