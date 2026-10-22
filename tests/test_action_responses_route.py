# tests/test_action_responses_route.py
"""POST /api/v1/actions/{id}/responses and the updated GET /api/v1/actions (seeded database)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.deps import get_clock
from app.core.clock import FixedClock
from app.models import StoreAction
from app.seed import ids
from app.seed.itsm import incident_id
from tests.conftest import ITSM_ANCHOR

CHENNAI = {"X-Site-Id": "01234", "X-User": "priya.store-manager"}
PRINTER_ACTION = uuid.UUID("00000000-0000-4000-8000-000000000001")
ACCESS_ACTION = uuid.UUID("00000000-0000-4000-8000-000000000002")
REGISTER_ACTION = uuid.UUID("00000000-0000-4000-8000-000000000003")


@pytest.fixture
def clock(app: FastAPI) -> FixedClock:
    fixed = FixedClock(ITSM_ANCHOR)  # 09:00 IST
    app.dependency_overrides[get_clock] = lambda: fixed
    return fixed


def action_row(action_id, number: str, title: str, expected: str, due_minutes=None) -> dict:
    return {
        "id": action_id,
        "store_id": ids.store_id("01234"),
        "incident_id": incident_id(number),
        "title": title,
        "detail": f"Detail: {title}",
        "severity": "warning",
        "expected_response": expected,
        "due_by": None if due_minutes is None else ITSM_ANCHOR + timedelta(minutes=due_minutes),
        "source": "seed",
        "created_by": "seed",
        "status": "open",
        "created_at": ITSM_ANCHOR - timedelta(hours=1),
    }


@pytest.fixture
async def actions(itsm_engine: AsyncEngine) -> None:
    async with itsm_engine.begin() as connection:
        await connection.execute(
            insert(StoreAction),
            [
                action_row(PRINTER_ACTION, "INC00001238", "Is Printer 02 printing correctly now?",
                           "yes_no"),
                action_row(ACCESS_ACTION, "INC00001234", "Provide comms room access at 1 PM",
                           "acknowledge", 240),
                action_row(REGISTER_ACTION, "INC00001236", "Check Register 04 cable", "free_text"),
            ],
        )  # fmt: skip


def url(action_id) -> str:
    return f"/api/v1/actions/{action_id}/responses"


async def test_reply_then_retry(client: AsyncClient, actions, clock) -> None:
    body = {"clientRequestId": str(uuid.uuid4()), "answerYesNo": True,
            "responseText": "Receipts print fine."}  # fmt: skip
    first = await client.post(url(PRINTER_ACTION), json=body, headers=CHENNAI)
    assert first.status_code == 201, first.text
    created = first.json()
    assert created == {
        "id": created["id"],
        "actionId": str(PRINTER_ACTION),
        "responseType": "yes_no",
        "answerYesNo": True,
        "responseText": "Receipts print fine.",
        "respondedBy": "priya.store-manager",
        "respondedAt": "2026-10-13T03:30:00Z",
        "actionStatus": "responded",
    }
    retry = await client.post(url(PRINTER_ACTION), json=body, headers=CHENNAI)
    assert retry.status_code == 200 and retry.json() == created

    other = await client.post(
        url(PRINTER_ACTION),
        json={"clientRequestId": str(uuid.uuid4()), "answerYesNo": False},
        headers=CHENNAI,
    )
    assert other.status_code == 409
    assert other.json()["error"]["message"] == "This action has already been answered."


async def test_reply_is_shown_in_actions_and_incident(client: AsyncClient, actions, clock):
    await client.post(
        url(PRINTER_ACTION),
        json={"clientRequestId": str(uuid.uuid4()), "answerYesNo": False},
        headers=CHENNAI,
    )
    listed = (await client.get("/api/v1/actions", headers=CHENNAI)).json()
    assert [(a["title"], a["status"]) for a in listed] == [
        ("Provide comms room access at 1 PM", "open"),
        ("Check Register 04 cable", "open"),
        ("Is Printer 02 printing correctly now?", "responded"),
    ]
    assert listed[0]["response"] is None
    assert listed[2]["response"] == {
        "answerYesNo": False,
        "responseText": None,
        "respondedBy": "priya.store-manager",
        "respondedAt": "2026-10-13T03:30:00Z",
    }

    detail = (await client.get("/api/v1/incidents/INC00001238", headers=CHENNAI)).json()
    first = detail["comments"][0]
    assert (first["text"], first["authorType"], first["author"]) == (
        'Store reply to "Is Printer 02 printing correctly now?": No.',
        "store_user",
        "priya.store-manager",
    )

    # After 24 hours the answered action is no longer listed.
    clock.advance(timedelta(hours=24, minutes=1))
    titles = [a["title"] for a in (await client.get("/api/v1/actions", headers=CHENNAI)).json()]
    assert "Is Printer 02 printing correctly now?" not in titles


ERRORS = [
    # (action, body without clientRequestId, add a clientRequestId?, status, error code)
    (PRINTER_ACTION, {"answerYesNo": True}, False, 422, "validation_error"),
    (PRINTER_ACTION, {"answerYesNo": "yes"}, True, 422, "validation_error"),
    (PRINTER_ACTION, {"responseText": "fine"}, True, 422, "business_rule"),
    (REGISTER_ACTION, {"responseText": "  "}, True, 422, "business_rule"),
    (ACCESS_ACTION, {"answerYesNo": True}, True, 422, "business_rule"),
    (uuid.UUID(int=99), {"answerYesNo": True}, True, 404, "not_found"),
]


@pytest.mark.parametrize(("action_id", "body", "with_id", "status", "code"), ERRORS)
async def test_errors(client: AsyncClient, actions, clock, action_id, body, with_id, status, code):
    if with_id:
        body = {"clientRequestId": str(uuid.uuid4()), **body}
    response = await client.post(url(action_id), json=body, headers=CHENNAI)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


async def test_other_store_and_expired(client: AsyncClient, actions, clock) -> None:
    body = {"clientRequestId": str(uuid.uuid4()), "answerYesNo": True}
    paris = await client.post(url(PRINTER_ACTION), json=body, headers={"X-Site-Id": "02556"})
    assert paris.status_code == 404
    clock.advance(timedelta(hours=5))  # past the 1 PM due time
    late = await client.post(
        url(ACCESS_ACTION), json={"clientRequestId": str(uuid.uuid4())}, headers=CHENNAI
    )
    assert late.status_code == 409
    assert late.json()["error"]["message"] == "This action has expired."
    assert (await client.post(url(PRINTER_ACTION), json=body)).status_code == 422  # no X-Site-Id
