# tests/test_one_open_action.py
"""At most one open action per incident: database index and action service."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import ActorContext
from app.models import AuditLog, StoreAction
from app.models.values import AuditSource
from app.services import incidents as incident_svc
from app.services.actions import OpenActionExists, close_open_action, create_action
from tests.itsm_helpers import IT, NOW, Itsm, build_itsm_fixture, new_incident

WORKER = ActorContext.system("change-worker", AuditSource.WORKER)


@pytest.fixture
async def itsm(db_session: AsyncSession) -> Itsm:
    return await build_itsm_fixture(db_session)


@pytest.fixture
async def incident(db_session: AsyncSession, itsm: Itsm):
    return await incident_svc.create_incident(
        db_session, IT, itsm.store_a, new_incident(device_id=itsm.pos), NOW, commit=False
    )


def row(itsm: Itsm, incident, title: str, status: str = "open", **values) -> StoreAction:
    return StoreAction(
        store_id=itsm.store_a, incident_id=incident.id, title=title, detail="d",
        expected_response="acknowledge", source="seed", created_by="seed", status=status,
        **values,
    )  # fmt: skip


async def make(session: AsyncSession, itsm: Itsm, incident, title: str):
    return await create_action(
        session, WORKER, store_id=itsm.store_a, incident_id=incident.id, source_journal_id=None,
        title=title, detail="Please check.", severity="warning", expected_response="yes_no",
        due_by=None, source="seed", ai_workflow_run_id=None, created_at=NOW,
    )  # fmt: skip


async def test_index_exists(db_session: AsyncSession) -> None:
    found = await db_session.scalar(
        text("SELECT to_regclass('public.ux_action_one_open_per_incident') IS NOT NULL")
    )
    assert found is True


async def test_database_refuses_a_second_open_action(db_session, itsm, incident) -> None:
    db_session.add(row(itsm, incident, "first"))
    await db_session.flush()
    with pytest.raises(IntegrityError, match="ux_action_one_open_per_incident"):
        async with db_session.begin_nested():
            db_session.add(row(itsm, incident, "second"))
            await db_session.flush()


async def test_answered_and_closed_actions_do_not_count(db_session, itsm, incident) -> None:
    db_session.add_all(
        [
            row(itsm, incident, "answered", status="responded"),
            row(itsm, incident, "closed", status="closed", close_reason="manual", closed_at=NOW),
            row(itsm, incident, "open"),
        ]
    )
    await db_session.flush()  # no error


async def test_service_refuses_a_second_open_action(db_session, itsm, incident) -> None:
    action, created = await make(db_session, itsm, incident, "Is the terminal working?")
    assert created and action.status == "open"
    with pytest.raises(OpenActionExists, match="already has an action waiting"):
        await make(db_session, itsm, incident, "Restart the terminal")


async def test_close_open_action_then_create(db_session, itsm, incident) -> None:
    first, _ = await make(db_session, itsm, incident, "first question")
    closed = await close_open_action(
        db_session, WORKER, incident.id, "Replaced by the fix confirmation.",
        NOW + timedelta(minutes=5),
    )  # fmt: skip
    assert closed == 1
    await db_session.refresh(first)
    assert (first.status, first.close_reason) == ("closed", "manual")
    second, created = await make(db_session, itsm, incident, "second question")
    assert created and second.status == "open"
    audit = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == str(first.id), AuditLog.action == "status_change"
        )
    )
    assert audit.changes["note"] == "Replaced by the fix confirmation."
