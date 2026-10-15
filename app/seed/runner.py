# app/seed/runner.py
"""Seed steps. Each step writes inside the session it is given and never commits;
the caller (scripts/seed.py or a test) owns the transaction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeviceType, NetworkElement, NetworkElementType, Store
from app.seed import ids
from app.seed.events import generate_events, write_events
from app.seed.inventory import build_inventory, write_inventory
from app.seed.loader import SeedData
from app.seed.master import write_master


class SeedDependencyError(RuntimeError):
    """A step was run before the step it depends on."""


async def _require(session: AsyncSession, label: str, found: int, expected: int, step: str) -> None:
    if found < expected:
        raise SeedDependencyError(
            f"{label}: found {found} of {expected} in the database. "
            f"Run 'uv run python -m scripts.seed {step}' first."
        )


async def seed_master(session: AsyncSession, data: SeedData, anchor: datetime) -> str:
    await write_master(session, data)
    return (
        f"{len(data.stores.stores)} stores, {len(data.master.assignment_groups)} groups, "
        f"{len(data.master.network_element_types)} element types, "
        f"{len(data.master.device_types)} device types, {len(data.prompts.prompts)} prompts"
    )


async def seed_inventory(session: AsyncSession, data: SeedData, anchor: datetime) -> str:
    store_ids = [ids.store_id(s.site_code) for s in data.stores.stores]
    stores = await session.scalar(
        select(func.count()).select_from(Store).where(Store.id.in_(store_ids))
    )
    await _require(session, "stores", stores or 0, len(store_ids), "master")
    element_types = await session.scalar(select(func.count()).select_from(NetworkElementType))
    device_types = await session.scalar(select(func.count()).select_from(DeviceType))
    await _require(
        session,
        "types",
        (element_types or 0) + (device_types or 0),
        len(data.master.network_element_types) + len(data.master.device_types),
        "master",
    )

    inventories = build_inventory(data, anchor)
    await write_inventory(session, inventories)
    elements = sum(len(i.elements) for i in inventories)
    devices = sum(len(i.devices) for i in inventories)
    return f"{elements} network elements, {devices} devices"


async def seed_events(session: AsyncSession, data: SeedData, anchor: datetime) -> str:
    inventories = build_inventory(data, anchor)
    element_ids = [row["id"] for inv in inventories for row in inv.elements]
    found = await session.scalar(
        select(func.count()).select_from(NetworkElement).where(NetworkElement.id.in_(element_ids))
    )
    await _require(session, "network elements", found or 0, len(element_ids), "inventory")

    events = generate_events(data, inventories, anchor)
    await write_events(session, events)
    return f"{len(events)} raw events (window ends {anchor.isoformat()})"


Step = Callable[[AsyncSession, SeedData, datetime], Awaitable[str]]

STEPS: dict[str, Step] = {
    "master": seed_master,
    "inventory": seed_inventory,
    "events": seed_events,
}
STEP_ORDER = ("master", "inventory", "events")
