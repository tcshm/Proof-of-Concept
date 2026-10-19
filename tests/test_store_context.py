# tests/test_store_context.py
"""X-Site-Id resolution and the actor dependencies."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from httpx import AsyncClient

from app.api.context import MobileActorDep, PortalActorDep, StoreDep
from tests.conftest import CommittedData

probe = APIRouter(prefix="/_probe")


@probe.get("/store")
async def probe_store(store: StoreDep) -> dict[str, Any]:
    return {
        "id": str(store.id),
        "siteCode": store.site_code,
        "name": store.name,
        "timezone": store.timezone.key,
    }


@probe.get("/mobile-actor")
async def probe_mobile_actor(actor: MobileActorDep) -> dict[str, Any]:
    return {
        "actorType": actor.actor_type,
        "actor": actor.actor,
        "source": actor.source,
        "requestId": str(actor.request_id),
    }


@probe.get("/portal-actor")
async def probe_portal_actor(actor: PortalActorDep) -> dict[str, Any]:
    return {"actorType": actor.actor_type, "actor": actor.actor, "source": actor.source}


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    app.include_router(probe)
    return app


async def test_valid_header_resolves_store(client: AsyncClient, chennai: uuid.UUID) -> None:
    response = await client.get("/_probe/store", headers={"X-Site-Id": "01234"})
    assert response.status_code == 200
    assert response.json() == {
        "id": str(chennai),
        "siteCode": "01234",
        "name": "Chennai Anna Nagar",
        "timezone": "Asia/Kolkata",
    }


async def test_each_header_gets_its_own_store(
    client: AsyncClient, committed_data: CommittedData, chennai: uuid.UUID
) -> None:
    london = await committed_data.add_store(
        "03118", name="London Oxford Street", country_code="GB", timezone="Europe/London"
    )
    a = (await client.get("/_probe/store", headers={"X-Site-Id": "01234"})).json()
    b = (await client.get("/_probe/store", headers={"X-Site-Id": "03118"})).json()
    assert a["id"] == str(chennai) and a["timezone"] == "Asia/Kolkata"
    assert b["id"] == str(london) and b["timezone"] == "Europe/London"


async def test_missing_header_is_422(client: AsyncClient) -> None:
    response = await client.get("/_probe/store")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["field"] == "header.X-Site-Id"
    assert error["details"][0]["type"] == "missing"


@pytest.mark.parametrize("bad", ["1234", "123456", "ABCDE", "01 34", "0123x"])
async def test_malformed_header_is_422(client: AsyncClient, bad: str) -> None:
    response = await client.get("/_probe/store", headers={"X-Site-Id": bad})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["type"] == "string_pattern_mismatch"


async def test_unknown_store_is_404(client: AsyncClient, committed_data: CommittedData) -> None:
    response = await client.get("/_probe/store", headers={"X-Site-Id": "99999"})
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"] == "Store 99999 was not found."


async def test_mobile_actor_defaults_to_store(client: AsyncClient, chennai: uuid.UUID) -> None:
    response = await client.get(
        "/_probe/mobile-actor", headers={"X-Site-Id": "01234", "X-Request-ID": str(chennai)}
    )
    assert response.json() == {
        "actorType": "store_user",
        "actor": "store-01234",
        "source": "mobile_app",
        "requestId": str(chennai),
    }


async def test_mobile_actor_uses_user_header(client: AsyncClient, chennai: uuid.UUID) -> None:
    response = await client.get(
        "/_probe/mobile-actor",
        headers={"X-Site-Id": "01234", "X-User": "  priya.store-manager  "},
    )
    assert response.json()["actor"] == "priya.store-manager"


async def test_portal_actor(client: AsyncClient) -> None:
    default = (await client.get("/_probe/portal-actor")).json()
    assert default == {"actorType": "it_staff", "actor": "portal-user", "source": "admin_portal"}
    named = (await client.get("/_probe/portal-actor", headers={"X-User": "j.smith"})).json()
    assert named["actor"] == "j.smith"
