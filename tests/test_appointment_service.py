# tests/test_appointment_service.py
"""The appointment service (rolled-back test database)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleError, ConflictError, NotFoundError
from app.models import Appointment, AuditLog
from app.services import appointments as svc
from app.services import incidents as incident_svc
from app.services.appointments import NewAppointment
from tests.itsm_helpers import IT, NOW, STORE_USER, Itsm, build_itsm_fixture, new_incident


@pytest.fixture
async def itsm(db_session: AsyncSession) -> Itsm:
    return await build_itsm_fixture(db_session)


@pytest.fixture
async def incident_id(db_session: AsyncSession, itsm: Itsm) -> uuid.UUID:
    incident = await incident_svc.create_incident(
        db_session, IT, itsm.store_a, new_incident(device_id=itsm.pos), NOW, commit=False
    )
    return incident.id


def visit(**overrides) -> NewAppointment:
    values = {
        "appointment_type": "field_tech",
        "title": "Site Visit - Field Tech Engineer",
        "purpose": "POS Terminal Hardware Replacement",
        "details": "Tech is scheduled for replacement at 12 PM.",
        "scheduled_start": NOW + timedelta(hours=3),
        "scheduled_end": NOW + timedelta(hours=4),
    }
    values.update(overrides)
    return NewAppointment(**values)


async def test_create_takes_store_from_incident(
    db_session: AsyncSession, itsm: Itsm, incident_id: uuid.UUID
) -> None:
    appointment = await svc.create_appointment(
        db_session, IT, incident_id, visit(), NOW, commit=False
    )
    assert appointment.store_id == itsm.store_a
    assert appointment.incident_id == incident_id
    assert (appointment.status, appointment.source, appointment.created_by) == (
        "scheduled",
        "admin_portal",
        "r.kumar",
    )
    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.entity_id == str(appointment.id))
    )
    assert audit.action == "create" and audit.changes["incident"].startswith("INC")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"scheduled_start": NOW - timedelta(minutes=1)}, "must start in the future"),
        ({"scheduled_end": NOW + timedelta(hours=2)}, "must end after it starts"),
        ({"appointment_type": "courier"}, "Unknown appointment type"),
        ({"title": " "}, "Title and purpose are required"),
    ],
)
async def test_create_validates_input(
    db_session: AsyncSession, incident_id: uuid.UUID, overrides: dict, message: str
) -> None:
    with pytest.raises(BusinessRuleError, match=message):
        await svc.create_appointment(
            db_session, IT, incident_id, visit(**overrides), NOW, commit=False
        )


async def test_create_needs_an_active_incident(
    db_session: AsyncSession, incident_id: uuid.UUID
) -> None:
    await incident_svc.resolve_incident(
        db_session, IT, incident_id, "Solved", "Fixed.", NOW, commit=False
    )
    with pytest.raises(ConflictError, match="appointments need an active incident"):
        await svc.create_appointment(db_session, IT, incident_id, visit(), NOW, commit=False)
    with pytest.raises(NotFoundError):
        await svc.create_appointment(db_session, IT, uuid.uuid4(), visit(), NOW, commit=False)


async def test_mobile_users_cannot_create(db_session: AsyncSession, incident_id: uuid.UUID) -> None:
    with pytest.raises(BusinessRuleError, match="created from the admin portal"):
        await svc.create_appointment(
            db_session, STORE_USER, incident_id, visit(), NOW, commit=False
        )


async def test_database_requires_an_incident(db_session: AsyncSession, itsm: Itsm) -> None:
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                Appointment(
                    store_id=itsm.store_a, incident_id=None, appointment_type="user_support",
                    title="John Martinez", purpose="Laptop Setup",
                    scheduled_start=NOW + timedelta(hours=1), created_by="seed", source="seed",
                )
            )  # fmt: skip
            await db_session.flush()


async def test_reschedule(db_session: AsyncSession, incident_id: uuid.UUID) -> None:
    appointment = await svc.create_appointment(
        db_session, IT, incident_id, visit(), NOW, commit=False
    )
    new_start = NOW + timedelta(days=1)
    await svc.reschedule_appointment(
        db_session, IT, appointment.id, new_start, new_start + timedelta(hours=1), NOW,
        commit=False,
    )  # fmt: skip
    assert appointment.scheduled_start == new_start
    audits = (
        await db_session.scalars(
            select(AuditLog).where(AuditLog.entity_id == str(appointment.id)).order_by(AuditLog.id)
        )
    ).all()
    assert [a.action for a in audits] == ["create", "update"]
    assert "scheduled_start" in audits[1].changes

    await svc.update_appointment_status(
        db_session, IT, appointment.id, "in_progress", NOW, commit=False
    )
    with pytest.raises(ConflictError, match="Only scheduled appointments"):
        await svc.reschedule_appointment(
            db_session, IT, appointment.id, new_start, None, NOW, commit=False
        )


async def test_cancel_needs_a_note(db_session: AsyncSession, incident_id: uuid.UUID) -> None:
    appointment = await svc.create_appointment(
        db_session, IT, incident_id, visit(), NOW, commit=False
    )
    with pytest.raises(BusinessRuleError, match="note is required"):
        await svc.cancel_appointment(db_session, IT, appointment.id, " ", NOW, commit=False)
    await svc.cancel_appointment(
        db_session, IT, appointment.id, "Store closed for stock count", NOW, commit=False
    )
    assert (appointment.status, appointment.status_note) == (
        "cancelled",
        "Store closed for stock count",
    )
    with pytest.raises(ConflictError, match="already cancelled"):
        await svc.cancel_appointment(db_session, IT, appointment.id, "again", NOW, commit=False)


@pytest.mark.parametrize(
    ("moves", "allowed"),
    [
        (["in_progress", "completed"], True),
        (["completed"], True),
        (["no_show"], True),
        (["in_progress", "no_show"], False),
        (["completed", "in_progress"], False),
    ],
)
async def test_status_moves(
    db_session: AsyncSession, incident_id: uuid.UUID, moves: list[str], allowed: bool
) -> None:
    appointment = await svc.create_appointment(
        db_session, IT, incident_id, visit(), NOW, commit=False
    )

    async def apply_all() -> None:
        for status in moves:
            await svc.update_appointment_status(
                db_session, IT, appointment.id, status, NOW, note="Technician did not arrive",
                commit=False,
            )  # fmt: skip

    if allowed:
        await apply_all()
        assert appointment.status == moves[-1]
    else:
        with pytest.raises(ConflictError, match="cannot move"):
            await apply_all()


async def test_no_show_needs_a_note(db_session: AsyncSession, incident_id: uuid.UUID) -> None:
    appointment = await svc.create_appointment(
        db_session, IT, incident_id, visit(), NOW, commit=False
    )
    with pytest.raises(BusinessRuleError, match="note is required for a no-show"):
        await svc.update_appointment_status(
            db_session, IT, appointment.id, "no_show", NOW, commit=False
        )
