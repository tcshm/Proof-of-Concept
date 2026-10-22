# tests/test_notifications_route.py
"""Mobile notification routes on the fully seeded database (09:00 IST in Chennai)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.deps import get_clock
from app.core.clock import FixedClock
from app.models import Notification, NotificationTarget
from app.seed import ids
from app.seed.itsm import appointment_id
from tests.conftest import ITSM_ANCHOR

CHENNAI = {"X-Site-Id": "01234"}
PARIS = {"X-Site-Id": "02556"}


@pytest.fixture
def clock(app: FastAPI) -> FixedClock:
    fixed = FixedClock(ITSM_ANCHOR)
    app.dependency_overrides[get_clock] = lambda: fixed
    return fixed


async def _list(client: AsyncClient, headers=CHENNAI, **params) -> list[dict]:
    response = await client.get("/api/v1/notifications", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _unread(client: AsyncClient, headers=CHENNAI) -> int:
    response = await client.get("/api/v1/notifications/unread-count", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["unreadCount"]


async def _id_by_title(engine: AsyncEngine, title: str) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(select(Notification.id).where(Notification.title == title))
        )


async def test_chennai_list(client: AsyncClient, itsm_engine: AsyncEngine, clock) -> None:
    items = await _list(client)
    assert len(items) == 10
    first = items[0]
    assert first == {
        "id": first["id"],
        "category": "Maintenance",
        "title": (
            "Network maintenance activity scheduled tonight at 1 AM - No action needed from stores"
        ),
        "description": (
            "Planned maintenance window tonight at 1 AM. No action is needed from stores at "
            "this time."
        ),
        "linkUrl": None,
        "createdAt": "2026-10-13T03:29:00Z",
        "systemEvent": None,
        "incidentId": None,
        "appointmentId": None,
        "actionId": None,
        "isRead": False,
    }
    isp = items[2]
    assert (isp["category"], isp["title"], isp["systemEvent"]) == (
        "Appointment",
        "New Appointment: ISP Technician at 01:00 PM",
        "appointment_scheduled",
    )
    assert (isp["incidentId"], isp["appointmentId"]) == (
        "INC00001234",
        str(appointment_id("apt-002")),
    )
    policy = items[-1]
    assert policy["linkUrl"] == "https://sharepoint.contoso.com/sites/IT/Security-Guidelines"
    assert (policy["category"], policy["isRead"]) == ("Updates", True)
    assert [i["category"] for i in items].count("Others") == 1  # n-009, Chennai only


async def test_fetch_marks_delivered_once(
    client: AsyncClient, itsm_engine: AsyncEngine, clock
) -> None:
    async def delivered() -> int:
        async with itsm_engine.connect() as connection:
            return await connection.scalar(
                select(func.count())
                .select_from(NotificationTarget)
                .where(
                    NotificationTarget.store_id == ids.store_id("01234"),
                    NotificationTarget.delivered_at.is_not(None),
                )
            )

    assert await delivered() == 3  # the three read in the seed
    await _list(client)
    assert await delivered() == 10  # every live one; not the scheduled, expired or withdrawn
    await _list(client)
    assert await delivered() == 10


async def test_limit(client: AsyncClient, itsm_engine: AsyncEngine, clock) -> None:
    assert len(await _list(client, limit=3)) == 3
    response = await client.get("/api/v1/notifications", headers=CHENNAI, params={"limit": 0})
    assert response.status_code == 422


async def test_read_and_unread_count(client: AsyncClient, itsm_engine: AsyncEngine, clock):
    assert await _unread(client) == 7
    first = (await _list(client))[0]["id"]
    response = await client.post(f"/api/v1/notifications/{first}/read", headers=CHENNAI)
    assert response.status_code == 200
    body = response.json()
    assert body == {"id": first, "isRead": True, "readAt": "2026-10-13T03:30:00Z"}
    assert await _unread(client) == 6

    clock.advance(timedelta(minutes=5))
    again = await client.post(f"/api/v1/notifications/{first}/read", headers=CHENNAI)
    assert again.json()["readAt"] == "2026-10-13T03:30:00Z"  # first read time is kept
    assert await _unread(client) == 6


async def test_read_all(client: AsyncClient, itsm_engine: AsyncEngine, clock) -> None:
    response = await client.post("/api/v1/notifications/read-all", headers=CHENNAI)
    assert response.json() == {"marked": 7}
    assert await _unread(client) == 0
    assert all(item["isRead"] for item in await _list(client))
    assert await _unread(client, PARIS) == 6  # other stores are untouched


async def test_hidden_notifications_cannot_be_read(
    client: AsyncClient, itsm_engine: AsyncEngine, clock
) -> None:
    paris_visit = await _id_by_title(
        itsm_engine, "New Appointment: Site Visit - Field Tech Engineer at 04:00 PM"
    )
    withdrawn = await _id_by_title(itsm_engine, "Store closing time change (sent in error)")
    scheduled = await _id_by_title(
        itsm_engine, "Wi-Fi controller upgrade tomorrow at 2 AM - No action needed from stores"
    )
    for notification_id in (paris_visit, withdrawn, scheduled):
        response = await client.post(
            f"/api/v1/notifications/{notification_id}/read", headers=CHENNAI
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    bad = await client.post("/api/v1/notifications/not-a-uuid/read", headers=CHENNAI)
    assert bad.status_code == 422


async def test_scheduled_notice_appears_on_time(
    client: AsyncClient, itsm_engine: AsyncEngine, clock
) -> None:
    clock.advance(timedelta(hours=6))
    titles = [item["title"] for item in await _list(client)]
    assert titles[0] == "Wi-Fi controller upgrade tomorrow at 2 AM - No action needed from stores"


async def test_store_header_rules(client: AsyncClient, itsm_engine: AsyncEngine, clock) -> None:
    assert (await client.get("/api/v1/notifications")).status_code == 422
    assert (await client.get("/api/v1/notifications/unread-count")).status_code == 422
    response = await client.get("/api/v1/notifications", headers={"X-Site-Id": "99999"})
    assert response.status_code == 404
