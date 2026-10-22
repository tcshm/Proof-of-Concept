# tests/test_action_response_service.py
"""The action reply service (rolled-back test database, plus one committed concurrency test)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.errors import BusinessRuleError, ConflictError, NotFoundError
from app.models import (
    AssignmentGroup,
    AuditLog,
    Device,
    Incident,
    IncidentJournal,
    NetworkElement,
    Store,
    StoreAction,
    StoreActionResponse,
)
from app.services import incidents as incident_svc
from app.services.action_responses import NewReply, respond_to_action
from app.worker.queue import claim_next
from tests.itsm_helpers import IT, NOW, STORE_USER, Itsm, build_itsm_fixture, new_incident


@pytest.fixture
async def itsm(db_session: AsyncSession) -> Itsm:
    return await build_itsm_fixture(db_session)


async def make_action(
    session: AsyncSession, itsm: Itsm, expected: str = "yes_no", due=None, **values
) -> StoreAction:
    incident = await incident_svc.create_incident(
        session, IT, itsm.store_a, new_incident(device_id=itsm.pos), NOW, commit=False
    )
    action = StoreAction(
        store_id=itsm.store_a,
        incident_id=incident.id,
        title="Is POS Terminal 03 working now?",
        detail="Please check the terminal.",
        expected_response=expected,
        due_by=due,
        source="seed",
        created_by="seed",
        **values,
    )
    session.add(action)
    await session.flush()
    return action


def reply(answer=None, text=None, request_id=None) -> NewReply:
    return NewReply(client_request_id=request_id or uuid.uuid4(), answer_yes_no=answer,
                    response_text=text)  # fmt: skip


async def respond(session, itsm, action, new_reply, at=NOW, actor=STORE_USER, store=None):
    return await respond_to_action(
        session, actor, store or itsm.store_a, action.id, new_reply, at, commit=False
    )


async def test_reply_writes_comment_response_status_and_audit(db_session, itsm) -> None:
    action = await make_action(db_session, itsm)
    later = NOW + timedelta(minutes=10)
    result = await respond(db_session, itsm, action, reply(True, " Prints fine. "), later)

    assert result.created is True
    saved = result.response
    assert (saved.response_type, saved.answer_yes_no, saved.response_text) == (
        "yes_no",
        True,
        "Prints fine.",
    )
    assert (saved.responded_by, saved.source, saved.store_id, saved.created_at) == (
        "store-01234",
        "mobile_app",
        itsm.store_a,
        later,
    )
    assert result.action.status == "responded"

    comment = await db_session.get(IncidentJournal, saved.journal_id)
    assert (comment.element, comment.author_type, comment.source) == (
        "comments",
        "store_user",
        "mobile_app",
    )
    assert comment.value == 'Store reply to "Is POS Terminal 03 working now?": Yes. Prints fine.'
    # Recorded only: the worker never processes the store's reply.
    assert (comment.process_status, comment.processed_at) == ("skipped", later)

    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.entity_id == str(action.id), AuditLog.action == "respond")
    )
    assert audit.changes["status"] == {"old": "open", "new": "responded"}
    assert audit.changes["answer_yes_no"] is True


async def test_same_request_id_returns_the_first_reply(db_session, itsm) -> None:
    action = await make_action(db_session, itsm)
    request_id = uuid.uuid4()
    first = await respond(db_session, itsm, action, reply(True, request_id=request_id))
    again = await respond(db_session, itsm, action, reply(False, request_id=request_id))
    assert again.created is False
    assert again.response.id == first.response.id and again.response.answer_yes_no is True
    count = await db_session.scalar(
        select(func.count())
        .select_from(StoreActionResponse)
        .where(StoreActionResponse.action_id == action.id)
    )
    assert count == 1


async def test_request_id_of_another_action_is_refused(db_session, itsm) -> None:
    first = await make_action(db_session, itsm)
    second = await make_action(db_session, itsm)
    request_id = uuid.uuid4()
    await respond(db_session, itsm, first, reply(True, request_id=request_id))
    with pytest.raises(ConflictError, match="already used for another action"):
        await respond(db_session, itsm, second, reply(True, request_id=request_id))


async def test_second_reply_is_refused(db_session, itsm) -> None:
    action = await make_action(db_session, itsm)
    await respond(db_session, itsm, action, reply(True))
    with pytest.raises(ConflictError, match="already been answered"):
        await respond(db_session, itsm, action, reply(False))


async def test_closed_and_overdue_actions_are_refused(db_session, itsm) -> None:
    closed = await make_action(
        db_session, itsm, status="closed", close_reason="manual", closed_at=NOW
    )
    with pytest.raises(ConflictError, match="This action is closed"):
        await respond(db_session, itsm, closed, reply(True))
    overdue = await make_action(db_session, itsm, due=NOW + timedelta(minutes=5))
    with pytest.raises(ConflictError, match="expired"):
        await respond(db_session, itsm, overdue, reply(True), NOW + timedelta(minutes=5))


async def test_other_store_and_unknown_action(db_session, itsm) -> None:
    action = await make_action(db_session, itsm)
    with pytest.raises(NotFoundError):
        await respond(db_session, itsm, action, reply(True), store=itsm.store_b)
    missing = StoreAction(id=uuid.uuid4())
    with pytest.raises(NotFoundError):
        await respond(db_session, itsm, missing, reply(True))


async def test_invalid_reply_writes_nothing(db_session, itsm) -> None:
    action = await make_action(db_session, itsm, expected="free_text")
    before = await db_session.scalar(select(func.count()).select_from(IncidentJournal))
    with pytest.raises(BusinessRuleError, match="needs a written answer"):
        await respond(db_session, itsm, action, reply(text="  "))
    with pytest.raises(BusinessRuleError, match="does not take a yes or no"):
        await respond(db_session, itsm, action, reply(True, "x"))
    assert await db_session.scalar(select(func.count()).select_from(IncidentJournal)) == before
    await db_session.refresh(action)
    assert action.status == "open"


async def test_only_store_users_reply(db_session, itsm) -> None:
    action = await make_action(db_session, itsm)
    with pytest.raises(BusinessRuleError, match="Only store users"):
        await respond(db_session, itsm, action, reply(True), actor=IT)


async def test_worker_never_claims_the_reply(db_session, itsm) -> None:
    action = await make_action(db_session, itsm, expected="acknowledge")
    result = await respond(db_session, itsm, action, reply())
    # Only the "Incident created" entry is waiting for the worker.
    claimed = await claim_next(db_session, NOW + timedelta(hours=1))
    assert claimed.element == "system"
    assert claimed.id != result.response.journal_id


# --- concurrency (committed data, two real connections) ---------------------------------
@pytest.fixture
async def committed_action(migrated_db: str):
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        itsm = await build_itsm_fixture(session)
        action = await make_action(session, itsm)
        await session.commit()
    try:
        yield factory, itsm, action.id
    finally:
        stores = [itsm.store_a, itsm.store_b]
        async with engine.begin() as connection:
            await connection.execute(
                delete(StoreActionResponse).where(StoreActionResponse.store_id.in_(stores))
            )
            await connection.execute(delete(StoreAction).where(StoreAction.store_id.in_(stores)))
            await connection.execute(delete(AuditLog).where(AuditLog.store_id.in_(stores)))
            incidents = select(Incident.id).where(Incident.store_id.in_(stores))
            await connection.execute(
                delete(IncidentJournal).where(IncidentJournal.incident_id.in_(incidents))
            )
            await connection.execute(delete(Incident).where(Incident.store_id.in_(stores)))
            await connection.execute(delete(Device).where(Device.store_id.in_(stores)))
            await connection.execute(
                delete(NetworkElement).where(NetworkElement.store_id.in_(stores))
            )
            await connection.execute(
                delete(AssignmentGroup).where(AssignmentGroup.id == itsm.group)
            )
            await connection.execute(delete(Store).where(Store.id.in_(stores)))
        await engine.dispose()


async def test_two_replies_at_the_same_time(committed_action) -> None:
    factory, itsm, action_id = committed_action

    async def answer(value: bool):
        async with factory() as session:
            return await respond_to_action(
                session, STORE_USER, itsm.store_a, action_id, reply(value), NOW
            )

    results = await asyncio.gather(answer(True), answer(False), return_exceptions=True)
    assert sum(1 for r in results if isinstance(r, ConflictError)) == 1
    assert sum(1 for r in results if not isinstance(r, Exception)) == 1
    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(StoreActionResponse)
            .where(StoreActionResponse.action_id == action_id)
        )
    assert count == 1
