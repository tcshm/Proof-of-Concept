# tests/test_portal_notifications_route.py
"""Portal notification routes: publish -> visible on mobile -> read -> delivery report."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.deps import get_clock
from app.core.clock import FixedClock
from app.models import Notification
from app.seed.itsm import appointment_notification_id
from tests.conftest import ITSM_ANCHOR

PORTAL = {"X-User": "cc.operator", "X-Client-App": "admin_portal"}
URL = "/api/v1/portal/notifications"


@pytest.fixture
def clock(app: FastAPI) -> FixedClock:
    fixed = FixedClock(ITSM_ANCHOR)
    app.dependency_overrides[get_clock] = lambda: fixed
    return fixed


def body(**overrides) -> dict:
    values = {
        "category": "maintenance",
        "title": "Emergency firewall patch tonight",
        "description": "Card payments may pause for 2 minutes at 11 PM.",
        "targetSiteCodes": ["01234", "02556"],
    }
    values.update(overrides)
    return values


async def _titles(client: AsyncClient, site: str) -> list[str]:
    response = await client.get("/api/v1/notifications", headers={"X-Site-Id": site})
    return [item["title"] for item in response.json()]


async def test_publish_reaches_only_the_target_stores(
    client: AsyncClient, itsm_engine: AsyncEngine, clock
) -> None:
    response = await client.post(
        URL, json=body(linkUrl="https://intranet.example/fw"), headers=PORTAL
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert (created["status"], created["source"], created["targetCount"]) == (
        "published",
        "admin_portal",
        2,
    )
    assert (created["createdBy"], created["publishAt"]) == ("cc.operator", "2026-10-13T03:30:00Z")
    assert created["linkUrl"] == "https://intranet.example/fw"

    assert (await _titles(client, "01234"))[0] == "Emergency firewall patch tonight"
    assert (await _titles(client, "02556"))[0] == "Emergency firewall patch tonight"
    assert "Emergency firewall patch tonight" not in await _titles(client, "02112")


async def test_start_and_end_times(client: AsyncClient, itsm_engine: AsyncEngine, clock) -> None:
    response = await client.post(
        URL,
        json=body(publishAt="2026-10-13T10:00:00+05:30", expiresAt="2026-10-14T10:00:00+05:30"),
        headers=PORTAL,
    )
    created = response.json()
    assert (created["publishAt"], created["expiresAt"]) == (
        "2026-10-13T04:30:00Z",
        "2026-10-14T04:30:00Z",
    )
    assert "Emergency firewall patch tonight" not in await _titles(client, "01234")
    clock.set(ITSM_ANCHOR.replace(hour=4, minute=30))
    assert "Emergency firewall patch tonight" in await _titles(client, "01234")


@pytest.mark.parametrize(
    ("overrides", "status", "code"),
    [
        ({"targetSiteCodes": ["01234", "99999"]}, 404, "not_found"),
        ({"targetSiteCodes": []}, 422, "validation_error"),
        ({"targetSiteCodes": ["1234"]}, 422, "validation_error"),
        ({"category": "news"}, 422, "validation_error"),
        ({"linkUrl": "http://intranet.example"}, 422, "validation_error"),
        ({"publishAt": "2026-10-13T10:00:00"}, 422, "validation_error"),  # no timezone
        ({"expiresAt": "2026-10-13T03:00:00Z"}, 422, "business_rule"),
    ],
)
async def test_publish_errors(
    client: AsyncClient, itsm_engine: AsyncEngine, clock, overrides: dict, status: int, code: str
) -> None:
    response = await client.post(URL, json=body(**overrides), headers=PORTAL)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    if status == 404:
        assert response.json()["error"]["details"] == [{"siteCode": "99999"}]


async def test_withdraw(client: AsyncClient, itsm_engine: AsyncEngine, clock) -> None:
    created = (await client.post(URL, json=body(), headers=PORTAL)).json()
    response = await client.post(f"{URL}/{created['id']}/withdraw", headers=PORTAL)
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["targetCount"]) == ("withdrawn", 2)
    assert "Emergency firewall patch tonight" not in await _titles(client, "01234")
    again = await client.post(f"{URL}/{created['id']}/withdraw", headers=PORTAL)
    assert again.status_code == 409

    system = appointment_notification_id("apt-002")
    refused = await client.post(f"{URL}/{system}/withdraw", headers=PORTAL)
    assert refused.status_code == 409
    assert refused.json()["error"]["message"] == "System notifications cannot be withdrawn."


async def test_delivery_report(client: AsyncClient, itsm_engine: AsyncEngine, clock) -> None:
    created = (await client.post(URL, json=body(), headers=PORTAL)).json()
    await client.get("/api/v1/notifications", headers={"X-Site-Id": "01234"})
    await client.post(
        f"/api/v1/notifications/{created['id']}/read",
        headers={"X-Site-Id": "01234", "X-User": "priya.store-manager"},
    )
    report = (await client.get(f"{URL}/{created['id']}/delivery", headers=PORTAL)).json()
    assert (report["targets"], report["delivered"], report["read"]) == (2, 1, 1)
    assert [s["siteCode"] for s in report["stores"]] == ["01234", "02556"]
    chennai, paris = report["stores"]
    assert (chennai["readBy"], chennai["readAt"]) == ("priya.store-manager", "2026-10-13T03:30:00Z")
    assert (paris["deliveredAt"], paris["readAt"]) == (None, None)


async def test_seeded_announcement_report(
    client: AsyncClient, itsm_engine: AsyncEngine, clock
) -> None:
    async with itsm_engine.connect() as connection:
        policy = await connection.scalar(
            select(Notification.id).where(
                Notification.title == "Policy Update: New IT Security Guidelines"
            )
        )
    report = (await client.get(f"{URL}/{policy}/delivery", headers=PORTAL)).json()
    assert (report["targets"], report["read"]) == (10, 1)


async def test_unknown_notification(client: AsyncClient, itsm_engine: AsyncEngine, clock) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.post(f"{URL}/{missing}/withdraw", headers=PORTAL)).status_code == 404
    assert (await client.get(f"{URL}/{missing}/delivery", headers=PORTAL)).status_code == 404
