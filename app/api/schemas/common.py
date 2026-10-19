# app/api/schemas/common.py
"""Small response models shared by several screens."""

from __future__ import annotations

from typing import Literal

from app.api.schemas.base import ApiModel

Colour = Literal["green", "yellow", "red"]


class IncidentRef(ApiModel):
    """An open incident shown under an element or device: id is the incident number."""

    id: str
    title: str
