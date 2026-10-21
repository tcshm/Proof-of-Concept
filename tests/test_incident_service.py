# tests/test_incident_service.py
"""The incident service (rolled-back test database, plus one committed concurrency test)."""

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
    Appointment,
    AssignmentGroup,
    AuditLog,
    Device,
    Incident,
    IncidentJournal,
    NetworkElement,
    Store,
    StoreAction,
    StoreAlert,
)
from app.services import incidents as svc
from app.services.incidents import UNSET, IncidentUpdate
from tests.itsm_helpers import AGENT, IT, NOW, STORE_USER, Itsm, build_itsm_fixture, new_incident


@pytest.fixture
async def itsm(db_session: AsyncSession) -> Itsm:
    return await build_itsm_fixture(db_session)


async def _create(session: AsyncSession, itsm: Itsm, **overrides) -> Incident:
    data = new_incident(network_element_id=itsm.ap, assignment_group_id=itsm.group, **overrides)
    return await svc.create_incident(session, IT, itsm.store_a, data, NOW, commit=False)


async def _journal(session: AsyncSession, incident: Incident) -> list[IncidentJournal]:
    rows = await session.scalars(
        select(IncidentJournal)
        .where(IncidentJournal.incident_id == incident.id)
        .order_by(IncidentJournal.created_at, IncidentJournal.element)
    )
    return list(rows.all())


async def _audit(session: AsyncSession, entity_id: uuid.UUID) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.entity_id == str(entity_id)).order_by(AuditLog.id)
    )
    return list(rows.all())


def _alert(store_id: uuid.UUID, incident_id: uuid.UUID) -> StoreAlert:
    return StoreAlert(
        store_id=store_id, incident_id=incident_id, title="Wi-Fi down",
        detail="Sales floor Wi-Fi is down.", source="seed", created_by="seed",
    )  # fmt: skip


def _action(store_id: uuid.UUID, incident_id: uuid.UUID) -> StoreAction:
    return StoreAction(
        store_id=store_id, incident_id=incident_id, title="Provide access",
        detail="Open the comms room.", expected_response="acknowledge", source="seed",
        created_by="seed",
    )  # fmt: skip


# --- create ----------------------------------------------------------------------------
async def test_create_writes_incident_created_entry_and_audit(
    db_session: AsyncSession, itsm: Itsm
) -> None:
    incident = await _create(db_session, itsm, u_symptom="Network accesspoint unavailable")

    assert incident.number.startswith("INC") and len(incident.number) == 11
    assert int(incident.number[3:]) >= 2000  # from incident_number_seq
    assert incident.state == 1 and incident.priority == 3
    assert incident.opened_at == NOW and incident.opened_by == "r.kumar"

    [entry] = await _journal(db_session, incident)
    assert (entry.element, entry.author_type, entry.process_status) == (
        "system",
        "system",
        "pending",
    )
    assert entry.available_at == NOW and entry.processed_at is None

    [audit] = await _audit(db_session, incident.id)
    assert audit.action == "create" and audit.entity_type == "incidents"
    assert audit.changes["number"] == {"old": None, "new": incident.number}


async def test_numbers_are_unique_and_increasing(db_session: AsyncSession, itsm: Itsm) -> None:
    first = await _create(db_session, itsm)
    second = await _create(db_session, itsm)
    assert int(second.number[3:]) > int(first.number[3:])


async def test_ci_must_belong_to_the_store(db_session: AsyncSession, itsm: Itsm) -> None:
    with pytest.raises(NotFoundError, match="network element was not found in this store"):
        await svc.create_incident(
            db_session, IT, itsm.store_a, new_incident(network_element_id=itsm.ap_b), NOW,
            commit=False,
        )  # fmt: skip
    with pytest.raises(BusinessRuleError, match="at most one main CI"):
        await svc.create_incident(
            db_session, IT, itsm.store_a,
            new_incident(network_element_id=itsm.ap, device_id=itsm.pos), NOW, commit=False,
        )  # fmt: skip


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"short_description": "  "}, "Short description is required"),
        ({"category": "facilities"}, "Category must be one of"),
        ({"contact_type": "fax"}, "Contact type must be one of"),
        ({"impact": 4}, "Impact must be 1, 2 or 3"),
    ],
)
async def test_create_validates_input(
    db_session: AsyncSession, itsm: Itsm, overrides: dict, message: str
) -> None:
    with pytest.raises(BusinessRuleError, match=message):
        await svc.create_incident(
            db_session, IT, itsm.store_a, new_incident(**overrides), NOW, commit=False
        )


# --- journal ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("actor", "element", "expected"),
    [
        (IT, "work_notes", "pending"),
        (IT, "comments", "pending"),
        (STORE_USER, "comments", "skipped"),
        (AGENT, "work_notes", "skipped"),
    ],
)
async def test_journal_entries_are_queued_by_author(
    db_session: AsyncSession, itsm: Itsm, actor, element: str, expected: str
) -> None:
    incident = await _create(db_session, itsm)
    later = NOW + timedelta(minutes=5)
    entry = await svc.add_journal_entry(
        db_session, actor, incident.id, element, "  Some update.  ", later, commit=False
    )
    assert entry.value == "Some update."
    assert entry.author_type == actor.actor_type
    assert entry.process_status == expected
    assert entry.processed_at == (later if expected == "skipped" else None)
    assert entry.available_at == later


async def test_journal_rejects_system_entries_and_finished_incidents(
    db_session: AsyncSession, itsm: Itsm
) -> None:
    incident = await _create(db_session, itsm)
    with pytest.raises(BusinessRuleError, match="Only work notes and comments"):
        await svc.add_journal_entry(db_session, IT, incident.id, "system", "x", NOW, commit=False)
    with pytest.raises(BusinessRuleError, match="note is required"):
        await svc.add_journal_entry(db_session, IT, incident.id, "comments", " ", NOW, commit=False)
    await svc.cancel_incident(db_session, IT, incident.id, "Duplicate", NOW, commit=False)
    with pytest.raises(ConflictError, match="no further updates"):
        await svc.add_journal_entry(db_session, IT, incident.id, "comments", "x", NOW, commit=False)


# --- update ----------------------------------------------------------------------------
async def test_update_on_hold_requires_and_clears_hold_reason(
    db_session: AsyncSession, itsm: Itsm
) -> None:
    incident = await _create(db_session, itsm)
    with pytest.raises(BusinessRuleError, match="hold reason is required"):
        await svc.update_incident(
            db_session, IT, incident.id, IncidentUpdate(state=3), NOW, commit=False
        )
    await svc.update_incident(
        db_session, IT, incident.id,
        IncidentUpdate(state=3, hold_reason="awaiting_site_visit"), NOW, commit=False,
    )  # fmt: skip
    assert (incident.state, incident.hold_reason) == (3, "awaiting_site_visit")

    await svc.update_incident(
        db_session, IT, incident.id, IncidentUpdate(state=2), NOW, commit=False
    )
    assert (incident.state, incident.hold_reason) == (2, None)

    with pytest.raises(BusinessRuleError, match="only applies to incidents on hold"):
        await svc.update_incident(
            db_session, IT, incident.id, IncidentUpdate(hold_reason="awaiting_vendor"), NOW,
            commit=False,
        )  # fmt: skip


async def test_update_writes_one_audit_row_with_changes_only(
    db_session: AsyncSession, itsm: Itsm
) -> None:
    incident = await _create(db_session, itsm)
    eta = NOW + timedelta(hours=5)
    await svc.update_incident(
        db_session, IT, incident.id,
        IncidentUpdate(state=2, assigned_to="r.kumar", u_resolution_eta=eta, impact=1),
        NOW, commit=False,
    )  # fmt: skip
    audits = await _audit(db_session, incident.id)
    assert [a.action for a in audits] == ["create", "status_change"]
    assert set(audits[1].changes) == {"state", "assigned_to", "u_resolution_eta", "impact"}
    assert incident.priority == 2  # generated column read back

    # No journal entry: field changes are shown live on the app.
    assert len(await _journal(db_session, incident)) == 1

    # Same values again: nothing changes, no audit row.
    await svc.update_incident(
        db_session, IT, incident.id, IncidentUpdate(assigned_to="r.kumar"), NOW, commit=False
    )
    assert len(await _audit(db_session, incident.id)) == 2


async def test_update_rules(db_session: AsyncSession, itsm: Itsm) -> None:
    incident = await _create(db_session, itsm)
    with pytest.raises(BusinessRuleError, match="Use resolve, close or cancel"):
        await svc.update_incident(
            db_session, IT, incident.id, IncidentUpdate(state=6), NOW, commit=False
        )
    with pytest.raises(NotFoundError, match="Assignment group"):
        await svc.update_incident(
            db_session, IT, incident.id, IncidentUpdate(assignment_group_id=uuid.uuid4()), NOW,
            commit=False,
        )  # fmt: skip
    assert IncidentUpdate(u_eta_note=None).given() == {"u_eta_note": None}
    assert IncidentUpdate().given() == {} and IncidentUpdate().state is UNSET


async def test_new_cannot_go_back_to_new(db_session: AsyncSession, itsm: Itsm) -> None:
    incident = await _create(db_session, itsm)
    await svc.update_incident(
        db_session, IT, incident.id, IncidentUpdate(state=2), NOW, commit=False
    )
    with pytest.raises(ConflictError, match="In Progress and cannot move to New"):
        await svc.update_incident(
            db_session, IT, incident.id, IncidentUpdate(state=1), NOW, commit=False
        )


# --- resolve / close / cancel ----------------------------------------------------------
async def test_resolve_closes_alert_writes_pending_note_keeps_actions(
    db_session: AsyncSession, itsm: Itsm
) -> None:
    incident = await _create(db_session, itsm)
    db_session.add_all([_alert(itsm.store_a, incident.id), _action(itsm.store_a, incident.id)])
    await db_session.flush()
    resolved_at = NOW + timedelta(hours=2)

    await svc.resolve_incident(
        db_session, IT, incident.id, "Solved (Permanently)",
        "AP replaced. Store to confirm Wi-Fi works on the sales floor.", resolved_at,
        commit=False,
    )  # fmt: skip

    assert (incident.state, incident.resolved_at, incident.hold_reason) == (6, resolved_at, None)
    alert = await db_session.scalar(select(StoreAlert).where(StoreAlert.incident_id == incident.id))
    await db_session.refresh(alert)
    assert (alert.status, alert.close_reason) == ("closed", "incident_resolved")
    action = await db_session.scalar(
        select(StoreAction).where(StoreAction.incident_id == incident.id)
    )
    await db_session.refresh(action)
    assert action.status == "open"  # actions stay open until the incident is closed

    note = (await _journal(db_session, incident))[-1]
    assert (note.element, note.process_status, note.author_type) == (
        "work_notes",
        "pending",
        "it_staff",
    )
    assert note.value.startswith("AP replaced.")


async def test_close_closes_actions_and_cancels_appointments(
    db_session: AsyncSession, itsm: Itsm
) -> None:
    incident = await _create(db_session, itsm)
    db_session.add(_action(itsm.store_a, incident.id))
    db_session.add(
        Appointment(
            store_id=itsm.store_a, incident_id=incident.id, appointment_type="field_tech",
            title="Site Visit", purpose="AP replacement", scheduled_start=NOW + timedelta(hours=4),
            created_by="seed", source="seed",
        )
    )  # fmt: skip
    await db_session.flush()
    with pytest.raises(ConflictError, match="New and cannot move to Closed"):
        await svc.close_incident(db_session, IT, incident.id, NOW, commit=False)

    await svc.resolve_incident(db_session, IT, incident.id, "Solved", "Fixed.", NOW, commit=False)
    await svc.close_incident(db_session, IT, incident.id, NOW + timedelta(days=1), commit=False)

    assert incident.state == 7 and incident.closed_at == NOW + timedelta(days=1)
    action = await db_session.scalar(
        select(StoreAction).where(StoreAction.incident_id == incident.id)
    )
    await db_session.refresh(action)
    assert (action.status, action.close_reason) == ("closed", "incident_closed")
    appointment = await db_session.scalar(
        select(Appointment).where(Appointment.incident_id == incident.id)
    )
    await db_session.refresh(appointment)
    assert (appointment.status, appointment.status_note) == ("cancelled", "Incident closed")


async def test_cancel(db_session: AsyncSession, itsm: Itsm) -> None:
    incident = await _create(db_session, itsm)
    db_session.add(_alert(itsm.store_a, incident.id))
    await db_session.flush()
    with pytest.raises(BusinessRuleError, match="cancellation reason is required"):
        await svc.cancel_incident(db_session, IT, incident.id, "", NOW, commit=False)
    await svc.cancel_incident(db_session, IT, incident.id, "Raised in error", NOW, commit=False)
    assert (incident.state, incident.close_notes) == (8, "Raised in error")
    alert = await db_session.scalar(select(StoreAlert).where(StoreAlert.incident_id == incident.id))
    await db_session.refresh(alert)
    assert alert.close_reason == "incident_canceled"
    with pytest.raises(ConflictError, match="Canceled and cannot move to Resolved"):
        await svc.resolve_incident(db_session, IT, incident.id, "x", "y", NOW, commit=False)


async def test_unknown_incident(db_session: AsyncSession, itsm: Itsm) -> None:
    with pytest.raises(NotFoundError):
        await svc.resolve_incident(db_session, IT, uuid.uuid4(), "x", "y", NOW, commit=False)
    with pytest.raises(NotFoundError, match="INC09999999"):
        await svc.find_incident_id(db_session, "INC09999999")


async def test_failed_operation_writes_nothing(db_session: AsyncSession, itsm: Itsm) -> None:
    incident = await _create(db_session, itsm)
    before = await db_session.scalar(select(func.count()).select_from(AuditLog))
    savepoint = await db_session.begin_nested()
    with pytest.raises(ConflictError):
        await svc.close_incident(db_session, IT, incident.id, NOW, commit=False)
    await savepoint.rollback()
    assert await db_session.scalar(select(func.count()).select_from(AuditLog)) == before
    await db_session.refresh(incident)
    assert incident.state == 1


# --- concurrency (committed data, two real connections) ---------------------------------
@pytest.fixture
async def committed_incident(migrated_db: str):
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        itsm = await build_itsm_fixture(session)
        incident = await svc.create_incident(
            session, IT, itsm.store_a, new_incident(network_element_id=itsm.ap), NOW
        )
    try:
        yield factory, incident.id
    finally:
        stores = [itsm.store_a, itsm.store_b]
        async with engine.begin() as connection:
            await connection.execute(delete(AuditLog).where(AuditLog.store_id.in_(stores)))
            await connection.execute(
                delete(IncidentJournal).where(IncidentJournal.incident_id == incident.id)
            )
            await connection.execute(delete(Incident).where(Incident.id == incident.id))
            await connection.execute(delete(Device).where(Device.store_id.in_(stores)))
            await connection.execute(
                delete(NetworkElement).where(NetworkElement.store_id.in_(stores))
            )
            await connection.execute(
                delete(AssignmentGroup).where(AssignmentGroup.id == itsm.group)
            )
            await connection.execute(delete(Store).where(Store.id.in_(stores)))
        await engine.dispose()


async def test_two_resolves_at_the_same_time(committed_incident) -> None:
    factory, incident_id = committed_incident

    async def resolve(note: str):
        async with factory() as session:
            return await svc.resolve_incident(session, IT, incident_id, "Solved", note, NOW)

    results = await asyncio.gather(resolve("first"), resolve("second"), return_exceptions=True)
    successes = [r for r in results if isinstance(r, Incident)]
    conflicts = [r for r in results if isinstance(r, ConflictError)]
    assert len(successes) == 1 and len(conflicts) == 1
    assert "Resolved and cannot move to Resolved" in conflicts[0].message

    async with factory() as session:
        notes = await session.scalar(
            select(func.count())
            .select_from(IncidentJournal)
            .where(
                IncidentJournal.incident_id == incident_id, IncidentJournal.element == "work_notes"
            )
        )
    assert notes == 1  # the losing resolve wrote nothing
