# tests/test_app_errors.py
"""Every error comes back in the same JSON shape with the request id."""

from __future__ import annotations

import logging
from typing import Annotated

import pytest
from fastapi import APIRouter, FastAPI, Query
from httpx import AsyncClient

from app.core.errors import (
    AppError,
    BadRequestError,
    BusinessRuleError,
    ConflictError,
    NotFoundError,
)

ERRORS: dict[str, AppError] = {
    "bad_request": BadRequestError("The request is malformed."),
    "not_found": NotFoundError("Store 99999 was not found."),
    "conflict": ConflictError("The action is already closed."),
    "business_rule": BusinessRuleError(
        "A yes/no action needs a yes/no answer.", details=[{"field": "answerYesNo"}]
    ),
}

extra_router = APIRouter(prefix="/_test")


@extra_router.get("/items")
async def list_items(limit: Annotated[int, Query(ge=1, le=100)]) -> dict[str, int]:
    return {"limit": limit}


@extra_router.get("/raise/{name}")
async def raise_app_error(name: str) -> None:
    raise ERRORS[name]


@extra_router.get("/crash")
async def crash() -> None:
    raise RuntimeError("secret connection string postgres://user:pw@db")


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    """The shared app fixture plus test-only routes."""
    app.include_router(extra_router)
    return app


def _assert_error_shape(response, status: int, code: str) -> dict:
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) == {"code", "message", "requestId", "details"}
    assert error["code"] == code
    assert error["requestId"] == response.headers["X-Request-ID"]
    return error


async def test_unknown_path_is_404_in_error_shape(client: AsyncClient) -> None:
    error = _assert_error_shape(await client.get("/does-not-exist"), 404, "not_found")
    assert error["details"] == []


async def test_wrong_method_is_405(client: AsyncClient) -> None:
    _assert_error_shape(await client.post("/health"), 405, "method_not_allowed")


async def test_validation_error_lists_fields(client: AsyncClient) -> None:
    error = _assert_error_shape(
        await client.get("/_test/items", params={"limit": "abc"}), 422, "validation_error"
    )
    assert error["details"][0]["field"] == "query.limit"
    assert error["details"][0]["type"] == "int_parsing"

    error = _assert_error_shape(await client.get("/_test/items"), 422, "validation_error")
    assert error["details"][0]["type"] == "missing"


@pytest.mark.parametrize(
    ("name", "status"),
    [("bad_request", 400), ("not_found", 404), ("conflict", 409), ("business_rule", 422)],
)
async def test_app_errors_map_to_status_and_code(
    client: AsyncClient, name: str, status: int
) -> None:
    error = _assert_error_shape(await client.get(f"/_test/raise/{name}"), status, name)
    assert error["message"] == ERRORS[name].message
    assert error["details"] == ERRORS[name].details


async def test_unexpected_error_is_generic_500(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    response = await client.get("/_test/crash")
    error = _assert_error_shape(response, 500, "internal_error")
    assert error["message"] == "An unexpected error occurred."
    assert "secret" not in response.text

    crash_logs = [r for r in caplog.records if r.getMessage() == "unhandled error"]
    assert len(crash_logs) == 1
    assert crash_logs[0].exc_info is not None  # the stack trace is in the log
    completed = [r for r in caplog.records if r.getMessage() == "request completed"]
    assert completed[-1].status == 500
