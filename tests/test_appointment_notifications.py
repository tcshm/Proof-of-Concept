# tests/test_appointment_notifications.py
"""Appointment changes notify the store in the same transaction (rolled-back test database)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleError
from app.models import Appointment, Notification, NotificationTarget
from app.services import appointments as svc
from app.services import incidents as incident_svc
from app.services.appointments import NewAppointment
from tests.itsm_helpers import IT, NOW, Itsm, build_itsm_fixture, new_incident

START = NOW + timedelta(hours=3)  # NOW is 09:00 IST, so 12:00 PM IST


@pytest.fixture
async def itsm(db_session: AsyncSession) -> Itsm:
    return await build_itsm_fixture(db_session)


@pytest.fixture
async def incident_id(db_session: AsyncSession, itsm: Itsm) -> uuid.UUID:
    incident = await incident_svc.create_incident(
        db_session, IT, itsm.store_a, new_incident(device_id=itsm.pos), NOW, commit=False
    )
    return incident.id


def visit() -> NewAppointment:
    return NewAppointment(
        appointment_type="field_tech",
        title="Site Visit - Field Tech Engineer",
        purpose="POS Terminal Hardware Replacement",
        details="Tech is scheduled for replacement at 12 PM.",
        scheduled_start=START,
        scheduled_end=START + timedelta(hours=1),
    )


async def notices(session: AsyncSession, appointment_id: uuid.UUID) -> list[Notification]:
    rows = await session.scalars(
        select(Notification)
        .where(Notification.appointment_id == appointment_id)
        .order_by(Notification.publish_at, Notification.created_at)
    )
    return list(rows.all())


async def test_create_notifies_the_store(
    db_session: AsyncSession, itsm: Itsm, incident_id: uuid.UUID
) -> None:
    appointment = await svc.create_appointment(
        db_session, IT, incident_id, visit(), NOW, commit=False
    )
    [notice] = await notices(db_session, appointment.id)
    assert (notice.source, notice.system_event, notice.category) == (
        "system",
        "appointment_scheduled",
        "appointment",
    )
    assert notice.title == "New Appointment: Site Visit - Field Tech Engineer at 12:00 PM"
    assert notice.description.startswith(
        "POS Terminal Hardware Replacement on Tue 13 Oct at 12:00 PM IST for incident INC"
    )
    assert (notice.incident_id, notice.publish_at) == (incident_id, NOW)
    assert notice.expires_at == START + timedelta(hours=1) + timedelta(hours=24)
    targets = (
        await db_session.scalars(
            select(NotificationTarget.store_id).where(
                NotificationTarget.notification_id == notice.id
            )
        )
    ).all()
    assert targets == [itsm.store_a]


async def test_reschedule_and_cancel_notify(
    db_session: AsyncSession, incident_id: uuid.UUID
) -> None:
    appointment = await svc.create_appointment(
        db_session, IT, incident_id, visit(), NOW, commit=False
    )
    later = NOW + timedelta(minutes=10)
    new_start = START + timedelta(days=1)
    await svc.reschedule_appointment(
        db_session, IT, appointment.id, new_start, new_start + timedelta(hours=1), later,
        commit=False,
    )  # fmt: skip
    await svc.cancel_appointment(
        db_session, IT, appointment.id, "Part not delivered", later + timedelta(minutes=5),
        commit=False,
    )  # fmt: skip

    events = [(n.system_event, n.title) for n in await notices(db_session, appointment.id)]
    assert events == [
        ("appointment_scheduled", "New Appointment: Site Visit - Field Tech Engineer at 12:00 PM"),
        (
            "appointment_rescheduled",
            "Appointment Rescheduled: Site Visit - Field Tech Engineer at 12:00 PM",
        ),
        ("appointment_cancelled", "Appointment Cancelled: Site Visit - Field Tech Engineer"),
    ]
    rescheduled, cancelled = (await notices(db_session, appointment.id))[1:]
    assert "moved from Tue 13 Oct at 12:00 PM IST to Wed 14 Oct at 12:00 PM IST" in (
        rescheduled.description
    )
    assert "is cancelled: Part not delivered" in cancelled.description


async def test_status_updates_send_nothing(
    db_session: AsyncSession, incident_id: uuid.UUID
) -> None:
    appointment = await svc.create_appointment(
        db_session, IT, incident_id, visit(), NOW, commit=False
    )
    await svc.update_appointment_status(
        db_session, IT, appointment.id, "in_progress", NOW, commit=False
    )
    await svc.update_appointment_status(
        db_session, IT, appointment.id, "completed", NOW, commit=False
    )
    assert len(await notices(db_session, appointment.id)) == 1


async def test_failed_change_writes_no_notification(
    db_session: AsyncSession, incident_id: uuid.UUID
) -> None:
    appointment = await svc.create_appointment(
        db_session, IT, incident_id, visit(), NOW, commit=False
    )
    with pytest.raises(BusinessRuleError):
        await svc.reschedule_appointment(
            db_session, IT, appointment.id, NOW - timedelta(hours=1), None, NOW, commit=False
        )
    assert len(await notices(db_session, appointment.id)) == 1


async def test_cancelling_the_incident_notifies_each_visit(
    db_session: AsyncSession, incident_id: uuid.UUID
) -> None:
    first = await svc.create_appointment(db_session, IT, incident_id, visit(), NOW, commit=False)
    second = await svc.create_appointment(db_session, IT, incident_id, visit(), NOW, commit=False)
    await incident_svc.cancel_incident(
        db_session, IT, incident_id, "Raised in error", NOW + timedelta(minutes=1), commit=False
    )
    for appointment in (first, second):
        refreshed = await db_session.get(Appointment, appointment.id, populate_existing=True)
        assert (refreshed.status, refreshed.status_note) == ("cancelled", "Incident cancelled")
        last = (await notices(db_session, appointment.id))[-1]
        assert last.system_event == "appointment_cancelled"
        assert "is cancelled: Incident cancelled" in last.description
