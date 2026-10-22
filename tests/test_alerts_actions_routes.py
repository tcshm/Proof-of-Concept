# tests/test_alerts_actions_routes.py
"""GET /api/v1/alerts and GET /api/v1/actions on the seeded database (09:00 IST)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.deps import get_clock
from app.core.clock import FixedClock
from app.models import StoreAction, StoreAlert
from app.seed import ids
from app.seed.itsm import incident_id
from tests.conftest import ITSM_ANCHOR

CHENNAI = {"X-Site-Id": "01234"}
CHENNAI_ID = ids.store_id("01234")
PARIS_ID = ids.store_id("02556")


@pytest.fixture
def clock(app: FastAPI) -> FixedClock:
    fixed = FixedClock(ITSM_ANCHOR)
    app.dependency_overrides[get_clock] = lambda: fixed
    return fixed


def alert(number: str, store_id, severity: str, minutes_ago: int, **values) -> dict:
    return {
        "store_id": store_id,
        "incident_id": incident_id(number),
        "title": f"Alert {number}",
        "detail": f"Detail {number}",
        "severity": severity,
        "source": "seed",
        "created_by": "seed",
        "status": "active",
        "close_reason": None,
        "closed_at": None,
        "created_at": ITSM_ANCHOR - timedelta(minutes=minutes_ago),
        **values,
    }


def action(title: str, number: str, store_id, due_minutes: int | None, **values) -> dict:
    return {
        "store_id": store_id,
        "incident_id": incident_id(number),
        "title": title,
        "detail": f"Detail {title}",
        "severity": "warning",
        "expected_response": "acknowledge",
        "due_by": None if due_minutes is None else ITSM_ANCHOR + timedelta(minutes=due_minutes),
        "source": "seed",
        "created_by": "seed",
        "status": "open",
        "created_at": ITSM_ANCHOR - timedelta(hours=1),
        **values,
    }


async def test_alerts(client: AsyncClient, itsm_engine: AsyncEngine, clock) -> None:
    async with itsm_engine.begin() as connection:
        await connection.execute(
            insert(StoreAlert),
            [
                alert("INC00001237", CHENNAI_ID, "warning", 120),
                alert("INC00001234", CHENNAI_ID, "critical", 60),
                alert("INC00001235", CHENNAI_ID, "warning", 30),
                alert(
                    "INC00001238", CHENNAI_ID, "info", 300, status="closed",
                    close_reason="incident_resolved", closed_at=ITSM_ANCHOR,
                ),
                alert("INC00001303", PARIS_ID, "critical", 10),
            ],
        )  # fmt: skip
    response = await client.get("/api/v1/alerts", headers=CHENNAI)
    assert response.status_code == 200, response.text
    alerts = response.json()
    assert [(a["incidentId"], a["severity"]) for a in alerts] == [
        ("INC00001234", "critical"),
        ("INC00001235", "warning"),
        ("INC00001237", "warning"),
    ]
    assert set(alerts[0]) == {"id", "title", "detail", "severity", "incidentId", "createdAt"}
    assert alerts[0]["createdAt"] == "2026-10-13T02:30:00Z"
    assert alerts[0]["title"] == "Alert INC00001234"


async def test_actions(client: AsyncClient, itsm_engine: AsyncEngine, clock) -> None:
    async with itsm_engine.begin() as connection:
        await connection.execute(
            insert(StoreAction),
            [
                action("Comms room access", "INC00001234", CHENNAI_ID, 240),
                action("Check Register 04", "INC00001236", CHENNAI_ID, None),
                action("Overdue", "INC00001235", CHENNAI_ID, -10),
                action("Restart the AP", "INC00001237", CHENNAI_ID, 60),
                action("Already answered", "INC00001234", CHENNAI_ID, 120, status="responded"),
                action("Paris action", "INC00001303", PARIS_ID, 60),
            ],
        )
    response = await client.get("/api/v1/actions", headers=CHENNAI)
    assert response.status_code == 200, response.text
    actions = response.json()
    assert [(a["title"], a["dueByText"]) for a in actions] == [
        ("Restart the AP", "Today at 10:00 AM"),
        ("Comms room access", "Today at 1:00 PM"),
        ("Check Register 04", None),
    ]
    first = actions[0]
    assert (first["incidentId"], first["expectedResponse"], first["dueBy"]) == (
        "INC00001237",
        "acknowledge",
        "2026-10-13T04:30:00Z",
    )


async def test_store_header_rules(client: AsyncClient, itsm_engine: AsyncEngine, clock) -> None:
    assert (await client.get("/api/v1/alerts")).status_code == 422
    assert (await client.get("/api/v1/actions")).status_code == 422
    unknown = await client.get("/api/v1/actions", headers={"X-Site-Id": "99999"})
    assert unknown.status_code == 404
