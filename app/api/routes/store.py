# app/api/routes/store.py
"""GET /api/v1/store: the store the mobile app is working for."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.context import StoreDep
from app.api.deps import ClockDep
from app.api.schemas.store import StoreResponse
from app.core.timefmt import is_open, to_local, tz_abbreviation

router = APIRouter(prefix="/store", tags=["store"])


@router.get("", response_model=StoreResponse)
async def get_store(store: StoreDep, clock: ClockDep) -> StoreResponse:
    now = clock.now()
    return StoreResponse(
        site_code=store.site_code,
        name=store.name,
        city=store.city,
        region=store.region,
        timezone=store.timezone.key,
        timezone_abbreviation=tz_abbreviation(now, store.timezone),
        local_time=to_local(now, store.timezone).isoformat(timespec="seconds"),
        is_open_now=is_open(store.business_hours, store.timezone, now),
    )
