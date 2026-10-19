# tests/test_processor.py
"""The event processor on small, handmade events (rolled-back test database)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import (
    EventProcessingRun,
    NetworkElement,
    NetworkElementCurrentStatus,
    NetworkElementType,
    NetworkEvent,
    RawNetworkEvent,
    Store,
)
from app.processing.aruba import CLIENT_EVENT, DEVICE_STATUS, GATEWAY_UPLINK, WAN_HEALTH
from app.processing.runner import (
    ADVISORY_LOCK_KEY,
    ProcessorBusyError,
    process_pending,
    run_processor,
)

T0 = datetime(2026, 10, 13, 6, 0, tzinfo=UTC)
AS_OF = T0 + timedelta(hours=3)


@dataclass
class Net:
    store_a: uuid.UUID
    store_b: uuid.UUID
    gateway: uuid.UUID
    ap: uuid.UUID
    router: uuid.UUID
    wan: uuid.UUID
    ap_b: uuid.UUID


def _el(store: uuid.UUID, type_code: str, name: str, **values: Any) -> NetworkElement:
    return NetworkElement(
        id=uuid.uuid4(), store_id=store, element_type_code=type_code, name=name, **values
    )


@pytest.fixture
async def net(db_session: AsyncSession) -> Net:
    for code, aruba in (
        ("gateway", "GATEWAY"),
        ("access_point", "ACCESS POINT"),
        ("router", None),
        ("wan_link", None),
    ):
        if await db_session.get(NetworkElementType, code) is None:
            db_session.add(
                NetworkElementType(
                    code=code,
                    display_name=code,
                    business_criticality="high",
                    aruba_device_type=aruba,
                )
            )
    a = Store(site_code="01234", name="A", region="R", country_code="IN", timezone="Asia/Kolkata")
    b = Store(site_code="05678", name="B", region="R", country_code="IN", timezone="Asia/Kolkata")
    db_session.add_all([a, b])
    await db_session.flush()

    gateway = _el(a.id, "gateway", "GW-A", serial_number="CN-GW-A", mac_address="02:00:00:00:00:01")
    ap = _el(a.id, "access_point", "AP-A", serial_number="CN-AP-A", mac_address="02:00:00:00:00:a1")
    ap_b = _el(b.id, "access_point", "AP-B", serial_number="CN-AP-B")
    db_session.add_all([gateway, ap, ap_b])
    await db_session.flush()
    router = _el(a.id, "router", "RTR-A", source_gateway_id=gateway.id, source_uplink_name="WAN1")
    wan = _el(a.id, "wan_link", "WAN-A", source_gateway_id=gateway.id, source_uplink_name="WAN1")
    db_session.add_all([router, wan])
    await db_session.flush()
    return Net(a.id, b.id, gateway.id, ap.id, router.id, wan.id, ap_b.id)


_sequence = 0


def raw(
    event_type: str,
    details: dict[str, Any],
    *,
    at: datetime,
    arrived: datetime | None = None,
    site: str = "01234",
    vendor_id: str | None = None,
    payload: Any = None,
) -> RawNetworkEvent:
    global _sequence
    _sequence += 1
    return RawNetworkEvent(
        event_id=f"evt-test-{_sequence:05d}",
        schema_version="1.0",
        event_type=event_type,
        event_time=at,
        ingestion_time=arrived or at + timedelta(seconds=5),
        source="test",
        vendor="aruba",
        site_id=site,
        payload=payload
        if payload is not None
        else {
            "id": vendor_id or str(uuid.uuid4()),
            "alert_type": "test",
            "details": details,
        },
    )


def ap_status(word: str, serial: str = "CN-AP-A", **extra: Any) -> dict[str, Any]:
    return {"serial": serial, "hostname": "AP", "status": word, **extra}


async def add(session: AsyncSession, *events: RawNetworkEvent) -> None:
    session.add_all(events)
    await session.flush()


async def status_of(session: AsyncSession, element_id: uuid.UUID) -> NetworkElementCurrentStatus:
    row = await session.get(NetworkElementCurrentStatus, element_id, populate_existing=True)
    assert row is not None
    return row


async def raw_status(session: AsyncSession, event: RawNetworkEvent) -> tuple[str, str | None]:
    row = (
        await session.execute(
            select(RawNetworkEvent.processing_status, RawNetworkEvent.process_error).where(
                RawNetworkEvent.event_id == event.event_id
            )
        )
    ).one()
    return row.processing_status, row.process_error


async def stored(session: AsyncSession, event: RawNetworkEvent) -> NetworkEvent | None:
    return await session.get(NetworkEvent, event.event_id, populate_existing=True)


async def run(session: AsyncSession, rebuild: bool = True, as_of: datetime = AS_OF):
    return await process_pending(session, as_of=as_of, rebuild=rebuild)


# --- status --------------------------------------------------------------------
async def test_up_then_down(db_session: AsyncSession, net: Net) -> None:
    up = raw(DEVICE_STATUS, ap_status("UP"), at=T0)
    down = raw(DEVICE_STATUS, ap_status("DOWN"), at=T0 + timedelta(hours=1))
    await add(db_session, up, down)

    counts = await run(db_session)

    assert (counts.events_read, counts.events_processed, counts.status_changes) == (2, 2, 2)
    state = await status_of(db_session, net.ap)
    assert state.status == "offline"
    assert state.status_since == T0 + timedelta(hours=1)
    assert state.last_status_event_id == down.event_id
    assert state.last_event_at == T0 + timedelta(hours=1)
    assert await raw_status(db_session, up) == ("processed", None)
    assert (await stored(db_session, down)).matched_by == "serial"


async def test_elements_without_events_are_unknown(db_session: AsyncSession, net: Net) -> None:
    await run(db_session)
    for element in (net.gateway, net.ap, net.router, net.wan, net.ap_b):
        assert (await status_of(db_session, element)).status == "unknown"


async def test_late_older_event_is_stored_but_not_applied(db_session: AsyncSession, net: Net):
    down = raw(DEVICE_STATUS, ap_status("DOWN"), at=T0 + timedelta(hours=2))
    late_up = raw(
        DEVICE_STATUS, ap_status("UP"), at=T0, arrived=T0 + timedelta(hours=2, minutes=30)
    )
    await add(db_session, down, late_up)

    await run(db_session)

    state = await status_of(db_session, net.ap)
    assert state.status == "offline"
    assert state.status_since == T0 + timedelta(hours=2)
    assert (await stored(db_session, late_up)).status_applied is False
    assert (await stored(db_session, down)).status_applied is True


async def test_same_status_keeps_status_since(db_session: AsyncSession, net: Net) -> None:
    first = raw(DEVICE_STATUS, ap_status("UP"), at=T0)
    again = raw(DEVICE_STATUS, ap_status("UP"), at=T0 + timedelta(hours=1))
    await add(db_session, first, again)

    counts = await run(db_session)

    state = await status_of(db_session, net.ap)
    assert state.status_since == T0
    assert state.last_status_event_id == first.event_id
    assert (await stored(db_session, again)).status_applied is True
    assert counts.status_changes == 1


async def test_client_event_moves_last_event_only(db_session: AsyncSession, net: Net) -> None:
    up = raw(DEVICE_STATUS, ap_status("UP"), at=T0)
    client = raw(
        CLIENT_EVENT,
        {"serial": "CN-AP-A", "client_mac": "02:aa:bb:cc:dd:ee", "rssi": -60},
        at=T0 + timedelta(hours=1),
    )
    await add(db_session, up, client)

    await run(db_session)

    state = await status_of(db_session, net.ap)
    assert state.status == "online" and state.status_since == T0
    assert state.last_event_at == T0 + timedelta(hours=1)
    row = await stored(db_session, client)
    assert row.reported_status is None and row.status_applied is False
    assert row.client_mac == "02:aa:bb:cc:dd:ee" and row.rssi == -60


# --- matching ------------------------------------------------------------------
async def test_router_and_wan_link_via_gateway_uplink(db_session: AsyncSession, net: Net):
    link = raw(GATEWAY_UPLINK, {"serial": "CN-GW-A", "uplink": "WAN1", "link_status": "UP"}, at=T0)
    wan_down = raw(WAN_HEALTH, {"serial": "CN-GW-A", "uplink": "wan1", "wan_status": "DOWN"}, at=T0)
    unknown_uplink = raw(
        WAN_HEALTH, {"serial": "CN-GW-A", "uplink": "WAN9", "wan_status": "UP"}, at=T0
    )
    await add(db_session, link, wan_down, unknown_uplink)

    counts = await run(db_session)

    assert (await status_of(db_session, net.router)).status == "online"
    assert (await status_of(db_session, net.wan)).status == "offline"
    assert (await status_of(db_session, net.gateway)).status == "unknown"
    assert (await stored(db_session, link)).matched_by == "gateway_uplink"
    assert (await stored(db_session, unknown_uplink)).matched_by == "none"
    assert counts.unmatched == 1


async def test_mac_fallback(db_session: AsyncSession, net: Net) -> None:
    event = raw(
        DEVICE_STATUS,
        {"serial": "SERIAL-NOT-KNOWN", "macaddr": "02-00-00-00-00-A1", "status": "DOWN"},
        at=T0,
    )
    await add(db_session, event)
    await run(db_session)
    assert (await stored(db_session, event)).matched_by == "device_mac"
    assert (await status_of(db_session, net.ap)).status == "offline"


async def test_unknown_serial_is_stored_as_unmatched(db_session: AsyncSession, net: Net):
    ghost = raw(DEVICE_STATUS, ap_status("UP", serial="CN-GHOST"), at=T0)
    await add(db_session, ghost)
    counts = await run(db_session)
    row = await stored(db_session, ghost)
    assert row.matched_by == "none" and row.network_element_id is None
    assert row.store_id == net.store_a
    assert await raw_status(db_session, ghost) == ("unmatched", "no matching network element")
    assert counts.unmatched == 1 and counts.events_processed == 0


async def test_other_stores_element_is_never_attached(db_session: AsyncSession, net: Net):
    wrong_site = raw(DEVICE_STATUS, ap_status("DOWN", serial="CN-AP-B"), at=T0, site="01234")
    await add(db_session, wrong_site)
    await run(db_session)
    assert (await stored(db_session, wrong_site)).matched_by == "none"
    assert (await status_of(db_session, net.ap_b)).status == "unknown"


async def test_unknown_site(db_session: AsyncSession, net: Net) -> None:
    event = raw(DEVICE_STATUS, ap_status("UP"), at=T0, site="99999")
    await add(db_session, event)
    counts = await run(db_session)
    assert await stored(db_session, event) is None
    assert await raw_status(db_session, event) == ("unmatched", "unknown site 99999")
    assert counts.unmatched == 1


# --- dedupe, errors, as_of -------------------------------------------------------
async def test_duplicate_vendor_event(db_session: AsyncSession, net: Net) -> None:
    first = raw(DEVICE_STATUS, ap_status("DOWN"), at=T0, vendor_id="same-aruba-id")
    again = raw(
        DEVICE_STATUS,
        ap_status("DOWN"),
        at=T0,
        arrived=T0 + timedelta(minutes=2),
        vendor_id="same-aruba-id",
    )
    await add(db_session, first, again)
    counts = await run(db_session)
    assert counts.duplicates == 1 and counts.events_processed == 1
    assert await raw_status(db_session, again) == ("duplicate", None)
    assert await stored(db_session, again) is None


async def test_bad_events_do_not_stop_the_run(db_session: AsyncSession, net: Net) -> None:
    unsupported = raw("aruba.something.new", ap_status("UP"), at=T0)
    malformed = raw(DEVICE_STATUS, {}, at=T0, payload={"id": "x1", "details": "nope"})
    bad_status = raw(DEVICE_STATUS, ap_status("FLAPPING"), at=T0)
    good = raw(DEVICE_STATUS, ap_status("DOWN"), at=T0 + timedelta(minutes=1))
    await add(db_session, unsupported, malformed, bad_status, good)

    counts = await run(db_session)

    assert counts.errors == 3 and counts.events_processed == 1
    assert (await raw_status(db_session, unsupported))[1] == (
        "unsupported event type 'aruba.something.new'"
    )
    assert (await raw_status(db_session, malformed))[0] == "error"
    assert (await status_of(db_session, net.ap)).status == "offline"


async def test_as_of_leaves_later_arrivals_pending(db_session: AsyncSession, net: Net) -> None:
    early = raw(DEVICE_STATUS, ap_status("UP"), at=T0)
    later = raw(DEVICE_STATUS, ap_status("DOWN"), at=T0 + timedelta(hours=5))
    await add(db_session, early, later)

    counts = await run(db_session, as_of=T0 + timedelta(hours=1))
    assert counts.events_read == 1
    assert await raw_status(db_session, later) == ("pending", None)
    assert (await status_of(db_session, net.ap)).status == "online"

    counts = await run(db_session, rebuild=False, as_of=T0 + timedelta(hours=6))
    assert counts.events_read == 1
    assert (await status_of(db_session, net.ap)).status == "offline"


async def test_incremental_dedupes_against_earlier_runs(db_session: AsyncSession, net: Net):
    first = raw(DEVICE_STATUS, ap_status("DOWN"), at=T0, vendor_id="aruba-1")
    await add(db_session, first)
    await run(db_session)
    again = raw(DEVICE_STATUS, ap_status("DOWN"), at=T0, vendor_id="aruba-1")
    await add(db_session, again)
    counts = await run(db_session, rebuild=False)
    assert counts.duplicates == 1


async def test_rebuild_twice_gives_the_same_result(db_session: AsyncSession, net: Net) -> None:
    await add(
        db_session,
        raw(DEVICE_STATUS, ap_status("UP"), at=T0),
        raw(DEVICE_STATUS, ap_status("DOWN"), at=T0 + timedelta(hours=1)),
        raw(WAN_HEALTH, {"serial": "CN-GW-A", "uplink": "WAN1", "wan_status": "UP"}, at=T0),
    )

    async def snapshot() -> list[tuple]:
        rows = await db_session.execute(
            select(
                NetworkElementCurrentStatus.network_element_id,
                NetworkElementCurrentStatus.status,
                NetworkElementCurrentStatus.status_since,
                NetworkElementCurrentStatus.last_status_event_id,
            ).order_by(NetworkElementCurrentStatus.network_element_id)
        )
        return [tuple(r) for r in rows]

    first_counts = await run(db_session)
    first = await snapshot()
    second_counts = await run(db_session)
    assert await snapshot() == first
    assert first_counts == second_counts
    total = await db_session.scalar(select(func.count()).select_from(NetworkEvent))
    assert total == 3


# --- run record and lock ---------------------------------------------------------
async def test_run_record(db_session: AsyncSession, net: Net) -> None:
    await add(db_session, raw(DEVICE_STATUS, ap_status("UP"), at=T0))
    run_row, counts = await run_processor(
        db_session, mode="rebuild", as_of=AS_OF, triggered_by="test"
    )
    saved = await db_session.get(EventProcessingRun, run_row.id)
    assert saved.status == "succeeded"
    assert saved.mode == "rebuild" and saved.as_of == AS_OF and saved.triggered_by == "test"
    assert (saved.events_read, saved.events_processed, saved.status_changes) == (1, 1, 1)
    assert saved.finished_at is not None


async def test_second_run_at_the_same_time_is_refused(
    db_session: AsyncSession, net: Net, migrated_db: str
) -> None:
    other = create_async_engine(migrated_db, poolclass=NullPool)
    try:
        async with other.connect() as connection:
            await connection.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
            )
            with pytest.raises(ProcessorBusyError):
                await run(db_session)
            await connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY}
            )
    finally:
        await other.dispose()
