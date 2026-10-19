# app/api/schemas/base.py
"""Shared response conventions.

- Python code uses snake_case; JSON uses camelCase (the names the mobile app
  already uses). A field whose mock name is not plain camelCase sets its own
  alias, e.g. Field(alias="resolutionETA").
- Every date is ISO 8601 in UTC with a Z suffix (UtcDateTime).
- UUIDs are serialised as strings by Pydantic.

FastAPI serialises response models by alias, so routes just return the model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PlainSerializer
from pydantic.alias_generators import to_camel

from app.core.timefmt import to_iso_utc

UtcDateTime = Annotated[datetime, PlainSerializer(to_iso_utc, return_type=str)]


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        from_attributes=True,
    )
