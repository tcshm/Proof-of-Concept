# app/db/base.py
"""Declarative base and reusable column types shared by all models.

The database is the source of truth for CHECK constraints, partial indexes and
triggers (see migrations/sql). Models describe columns, keys and defaults so the
ORM can read and write rows correctly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from sqlalchemy import DateTime, FetchedValue, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, mapped_column

# Python type -> PostgreSQL type used when a column has no explicit type.
TYPE_MAP: dict[Any, Any] = {
    uuid.UUID: UUID(as_uuid=True),
    datetime: DateTime(timezone=True),
    dict[str, Any]: JSONB,
    str: Text,
    Decimal: Numeric,
}


class Base(DeclarativeBase):
    type_annotation_map = TYPE_MAP
    # Read server-generated values (defaults, generated columns, trigger-set
    # updated_at) back with RETURNING, so no lazy load is needed afterwards.
    __mapper_args__ = {"eager_defaults": True}


# Reusable annotated column types
UUIDPk = Annotated[
    uuid.UUID,
    mapped_column(primary_key=True, server_default=text("gen_random_uuid()")),
]
CreatedAt = Annotated[datetime, mapped_column(server_default=func.now())]
UpdatedAt = Annotated[
    datetime,
    mapped_column(server_default=func.now(), server_onupdate=FetchedValue()),
]
JsonObject = Annotated[dict[str, Any], mapped_column(server_default=text("'{}'::jsonb"))]
ServiceNowSysId = Annotated[str | None, mapped_column(unique=True)]
