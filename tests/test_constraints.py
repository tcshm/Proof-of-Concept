# tests/test_constraints.py
"""The database rejects invalid data, and the models read back server-set values."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AssignmentGroup,
    Device,
    DeviceType,
    Incident,
    IncidentJournal,
    NetworkElement,
    NetworkElementType,
    Store,
    StoreAlert,
)
from app.models.values import (
    ActorType,
    AlertSource,
    ContactType,
    Criticality,
    HoldReason,
    IncidentCategory,
    IncidentState,
    JournalElement,
    JournalSource,
)

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


# --- helpers -----------------------------------------------------------------
async def add(session: AsyncSession, obj: object) -> None:
    session.add(obj)
    await session.flush()


async def expect_rejected(session: AsyncSession, obj: object) -> None:
    """The insert must fail; the savepoint keeps the test transaction usable."""
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(obj)
            await session.flush()


async def make_store(session: AsyncSession, site_code: str = "01234") -> Store:
    store = Store(
        site_code=site_code,
        name=f"Store {site_code}",
        region="UK",
        country_code="GB",
        timezone="Europe/London",
    )
    await add(session, store)
    return store


async def make_switch(session: AsyncSession, store: Store, name: str) -> NetworkElement:
    if await session.get(NetworkElementType, "switch") is None:
        await add(
            session,
            NetworkElementType(
                code="switch",
                display_name="Switch",
                business_criticality=Criticality.HIGH,
                aruba_device_type="SWITCH",
            ),
        )
    switch = NetworkElement(
        store_id=store.id,
        element_type_code="switch",
        name=name,
        serial_number=f"SN-{uuid.uuid4().hex[:10]}",
    )
    await add(session, switch)
    return switch


async def make_device(session: AsyncSession, store: Store) -> Device:
    if await session.get(DeviceType, "pos") is None:
        await add(
            session,
            DeviceType(code="pos", display_name="POS", business_criticality=Criticality.HIGH),
        )
    device = Device(store_id=store.id, device_type_code="pos", name="POS Terminal-01")
    await add(session, device)
    return device


def new_incident(store: Store, **overrides: object) -> Incident:
    fields: dict[str, object] = {
        "number": "INC0001234",
        "store_id": store.id,
        "short_description": "ISP - Primary Link",
        "category": IncidentCategory.NETWORK,
        "contact_type": ContactType.MONITORING,
        "impact": 1,
        "urgency": 2,
        "opened_at": NOW,
    }
    fields.update(overrides)
    return Incident(**fields)


# --- server-set values -------------------------------------------------------
async def test_defaults_are_read_back_without_lazy_load(db_session: AsyncSession) -> None:
    store = await make_store(db_session)
    assert isinstance(store.id, uuid.UUID)
    assert store.status == "open"
    assert store.business_hours == {}
    assert store.created_at is not None


async def test_priority_is_generated(db_session: AsyncSession) -> None:
    store = await make_store(db_session)
    incident = new_incident(store, impact=1, urgency=2)
    await add(db_session, incident)
    assert incident.priority == 2
    assert incident.state == IncidentState.NEW


async def test_updated_at_trigger(db_session: AsyncSession) -> None:
    group = AssignmentGroup(name="Network Ops")
    await add(db_session, group)
    # Force an old value; the trigger must replace it with now() on update.
    old = NOW - timedelta(days=365)
    await db_session.execute(
        text("UPDATE assignment_groups SET description = 'WAN', updated_at = :old WHERE id = :id"),
        {"old": old, "id": group.id},
    )
    updated_at = await db_session.scalar(
        text("SELECT updated_at FROM assignment_groups WHERE id = :id"), {"id": group.id}
    )
    assert updated_at > old


# --- rejected data -----------------------------------------------------------
async def test_rejects_bad_site_code(db_session: AsyncSession) -> None:
    await expect_rejected(
        db_session,
        Store(
            site_code="12AB",
            name="Bad",
            region="UK",
            country_code="GB",
            timezone="Europe/London",
        ),
    )


async def test_rejects_incident_with_two_main_cis(db_session: AsyncSession) -> None:
    store = await make_store(db_session)
    switch = await make_switch(db_session, store, "SW01234N301")
    device = await make_device(db_session, store)
    await expect_rejected(
        db_session,
        new_incident(store, cmdb_ci_network_element_id=switch.id, cmdb_ci_device_id=device.id),
    )


async def test_rejects_on_hold_without_hold_reason(db_session: AsyncSession) -> None:
    store = await make_store(db_session)
    await expect_rejected(db_session, new_incident(store, state=IncidentState.ON_HOLD))
    on_hold = new_incident(
        store, state=IncidentState.ON_HOLD, hold_reason=HoldReason.AWAITING_SITE_VISIT
    )
    await add(db_session, on_hold)


async def test_rejects_cross_store_reference(db_session: AsyncSession) -> None:
    store_a = await make_store(db_session, "01234")
    store_b = await make_store(db_session, "05678")
    switch_b = await make_switch(db_session, store_b, "SW05678N301")
    await make_device(db_session, store_a)
    # A device in store A cannot connect through store B's switch.
    await expect_rejected(
        db_session,
        Device(
            store_id=store_a.id,
            device_type_code="pos",
            name="POS Terminal-02",
            connected_network_element_id=switch_b.id,
        ),
    )


async def test_rejects_system_entry_from_it_staff(db_session: AsyncSession) -> None:
    store = await make_store(db_session)
    incident = new_incident(store)
    await add(db_session, incident)
    await expect_rejected(
        db_session,
        IncidentJournal(
            incident_id=incident.id,
            element=JournalElement.SYSTEM,
            value="Incident created",
            author_type=ActorType.IT_STAFF,
            created_by="j.smith",
            source=JournalSource.SEED,
        ),
    )


async def test_rejects_second_alert_for_same_incident(db_session: AsyncSession) -> None:
    store = await make_store(db_session)
    incident = new_incident(store)
    await add(db_session, incident)

    def alert() -> StoreAlert:
        return StoreAlert(
            store_id=store.id,
            incident_id=incident.id,
            title="Primary link down",
            detail="Store is running on the backup link.",
            source=AlertSource.SEED,
            created_by="seed",
        )

    await add(db_session, alert())
    await expect_rejected(db_session, alert())
