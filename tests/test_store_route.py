# tests/test_store_route.py
"""GET /api/v1/store: every Phase 4 building block in one call."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.deps import get_clock
from app.core.clock import FixedClock

HEADERS = {"X-Site-Id": "01234"}


@pytest.fixture
def clock(app: FastAPI) -> FixedClock:
    fixed = FixedClock(datetime(2026, 10, 13, 5, 12, tzinfo=UTC))  # Tue 10:42 IST
    app.dependency_overrides[get_clock] = lambda: fixed
    return fixed


async def test_store_details_in_camel_case(
    client: AsyncClient, chennai: uuid.UUID, clock: FixedClock
) -> None:
    response = await client.get("/api/v1/store", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        "siteCode": "01234",
        "name": "Chennai Anna Nagar",
        "city": "Chennai",
        "region": "India",
        "timezone": "Asia/Kolkata",
        "timezoneAbbreviation": "IST",
        "localTime": "2026-10-13T10:42:00+05:30",
        "isOpenNow": True,
    }


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 13, 4, 29, tzinfo=UTC), False),  # Tue 09:59 IST
        (datetime(2026, 10, 13, 4, 30, tzinfo=UTC), True),  # Tue 10:00 IST
        (datetime(2026, 10, 13, 16, 30, tzinfo=UTC), False),  # Tue 22:00 IST
        (datetime(2026, 10, 18, 6, 0, tzinfo=UTC), False),  # Sunday: closed
    ],
)
async def test_is_open_now_follows_the_clock(
    client: AsyncClient, chennai: uuid.UUID, clock: FixedClock, now: datetime, expected: bool
) -> None:
    clock.set(now)
    body = (await client.get("/api/v1/store", headers=HEADERS)).json()
    assert body["isOpenNow"] is expected


async def test_store_route_errors(client: AsyncClient, chennai: uuid.UUID) -> None:
    assert (await client.get("/api/v1/store")).status_code == 422
    assert (await client.get("/api/v1/store", headers={"X-Site-Id": "1234"})).status_code == 422
    assert (await client.get("/api/v1/store", headers={"X-Site-Id": "99999"})).status_code == 404


async def test_store_route_in_openapi(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    operation = schema["paths"]["/api/v1/store"]["get"]
    headers = {p["name"] for p in operation["parameters"] if p["in"] == "header"}
    assert headers == {"X-Site-Id"}
