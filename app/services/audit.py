# app/services/audit.py
"""The audit writer.

record_audit() ADDS an audit_log row to the caller's session and never commits.
The service commits once, so a change and its audit row are saved together or
not at all. diff() builds the {"field": {"old": ..., "new": ...}} changes object.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import ActorContext
from app.models import AuditLog
from app.models.values import AuditAction


def to_jsonable(value: Any) -> Any:
    """Convert values to types that fit a jsonb column."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, uuid.UUID | Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, Iterable):
        return [to_jsonable(item) for item in value]
    return str(value)


def diff(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    fields: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Only the fields whose value changed: {"status": {"old": "open", "new": "responded"}}.

    A field missing on one side counts as None. With fields=..., only those keys
    are compared.
    """
    keys = list(fields) if fields is not None else sorted(set(before) | set(after))
    changes: dict[str, dict[str, Any]] = {}
    for key in keys:
        old = to_jsonable(before.get(key))
        new = to_jsonable(after.get(key))
        if old != new:
            changes[key] = {"old": old, "new": new}
    return changes


def record_audit(
    session: AsyncSession,
    actor: ActorContext,
    *,
    action: AuditAction | str,
    entity_type: str,
    entity_id: uuid.UUID | str,
    store_id: uuid.UUID | None = None,
    changes: Mapping[str, Any] | None = None,
) -> AuditLog:
    """Add one audit row to the session (no flush, no commit)."""
    entry = AuditLog(
        actor_type=str(actor.actor_type),
        actor=actor.actor,
        source=str(actor.source),
        action=str(AuditAction(action)),
        entity_type=entity_type,
        entity_id=str(entity_id),
        store_id=store_id,
        changes=to_jsonable(dict(changes or {})),
        request_id=actor.request_id,
    )
    session.add(entry)
    return entry
