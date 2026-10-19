# tests/test_audit.py
"""The audit writer adds rows to the caller's transaction; diff() reports changes."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import ActorContext
from app.models import AuditLog, Store
from app.models.values import ActorType, AuditAction, AuditSource
from app.services.audit import diff, record_audit, to_jsonable

ACTOR = ActorContext(
    actor_type=ActorType.STORE_USER,
    actor="priya.store-manager",
    source=AuditSource.MOBILE_APP,
    request_id=uuid.UUID("6f1c2a0e-1111-4222-8333-444455556666"),
)


async def _store(session: AsyncSession) -> Store:
    store = Store(
        site_code="01234",
        name="Chennai Anna Nagar",
        region="India",
        country_code="IN",
        timezone="Asia/Kolkata",
    )
    session.add(store)
    await session.flush()
    return store


def test_diff_reports_only_changed_fields() -> None:
    before = {"status": "open", "title": "Confirm the fix", "due_by": None}
    after = {"status": "responded", "title": "Confirm the fix", "due_by": None}
    assert diff(before, after) == {"status": {"old": "open", "new": "responded"}}
    assert diff(before, before) == {}


def test_diff_handles_missing_keys_and_field_filter() -> None:
    assert diff({}, {"state": 6}) == {"state": {"old": None, "new": 6}}
    assert diff({"a": 1, "b": 1}, {"a": 2, "b": 2}, fields=["b"]) == {"b": {"old": 1, "new": 2}}


def test_values_become_json_safe() -> None:
    value = {
        "id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "at": datetime(2026, 10, 13, 7, 12, tzinfo=UTC),
        "day": date(2026, 10, 13),
        "amount": Decimal("13.085000"),
        "status": AuditAction.RESPOND,
        "list": (1, "two"),
    }
    assert to_jsonable(value) == {
        "id": "12345678-1234-5678-1234-567812345678",
        "at": "2026-10-13T07:12:00+00:00",
        "day": "2026-10-13",
        "amount": "13.085000",
        "status": "respond",
        "list": [1, "two"],
    }
    eta = datetime(2026, 10, 13, 8, 30, tzinfo=UTC)
    assert diff({"eta": None}, {"eta": eta}) == {
        "eta": {"old": None, "new": "2026-10-13T08:30:00+00:00"}
    }


async def test_record_audit_writes_in_callers_transaction(db_session: AsyncSession) -> None:
    store = await _store(db_session)
    entry = record_audit(
        db_session,
        ACTOR,
        action=AuditAction.STATUS_CHANGE,
        entity_type="store_actions",
        entity_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        store_id=store.id,
        changes=diff({"status": "open"}, {"status": "responded"}),
    )
    await db_session.flush()

    saved = await db_session.get(AuditLog, entry.id)
    assert saved is not None
    assert saved.actor_type == "store_user"
    assert saved.actor == "priya.store-manager"
    assert saved.source == "mobile_app"
    assert saved.action == "status_change"
    assert saved.entity_id == "12345678-1234-5678-1234-567812345678"
    assert saved.store_id == store.id
    assert saved.changes == {"status": {"old": "open", "new": "responded"}}
    assert saved.request_id == ACTOR.request_id
    assert saved.occurred_at is not None


async def test_audit_row_disappears_with_rollback(db_session: AsyncSession) -> None:
    async def count() -> int:
        return await db_session.scalar(select(func.count()).select_from(AuditLog)) or 0

    before = await count()
    savepoint = await db_session.begin_nested()
    record_audit(db_session, ACTOR, action="create", entity_type="incidents", entity_id="x")
    await db_session.flush()
    assert await count() == before + 1
    await savepoint.rollback()
    assert await count() == before


def test_unknown_action_is_rejected_before_the_database() -> None:
    with pytest.raises(ValueError):
        record_audit(None, ACTOR, action="delete", entity_type="incidents", entity_id="x")  # type: ignore[arg-type]


async def test_system_actor_for_worker_and_seed(db_session: AsyncSession) -> None:
    worker = ActorContext.system("change-worker", AuditSource.WORKER)
    assert worker.actor_type == "system" and worker.request_id is None
    entry = record_audit(
        db_session, worker, action="create", entity_type="store_alerts", entity_id="a1"
    )
    await db_session.flush()
    assert (await db_session.get(AuditLog, entry.id)).source == "worker"

    bad = ActorContext(actor_type="robot", actor="x", source="worker")
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            record_audit(db_session, bad, action="create", entity_type="t", entity_id="1")
            await db_session.flush()
