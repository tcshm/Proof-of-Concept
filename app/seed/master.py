# app/seed/master.py
"""Master data and prompt versions: build rows from the seed files, then upsert."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiPromptVersion, AssignmentGroup, DeviceType, NetworkElementType, Store
from app.seed import ids
from app.seed.db import upsert
from app.seed.loader import SeedData


def store_rows(data: SeedData) -> list[dict[str, Any]]:
    return [
        {
            "id": ids.store_id(store.site_code),
            "site_code": store.site_code,
            "name": store.name,
            "region": store.region,
            "country_code": store.country_code,
            "city": store.city,
            "address_line": store.address_line,
            "postal_code": store.postal_code,
            "latitude": store.latitude,
            "longitude": store.longitude,
            "timezone": store.timezone,
            "business_hours": {
                day: [list(interval) for interval in intervals]
                for day, intervals in store.business_hours.items()
            },
            "contact_phone": store.contact_phone,
            "contact_email": store.contact_email,
            "status": "open",
        }
        for store in data.stores.stores
    ]


def assignment_group_rows(data: SeedData) -> list[dict[str, Any]]:
    return [
        {
            "id": ids.assignment_group_id(group.name),
            "name": group.name,
            "description": group.description,
            "email": group.email,
        }
        for group in data.master.assignment_groups
    ]


def element_type_rows(data: SeedData) -> list[dict[str, Any]]:
    return [t.model_dump(mode="json") for t in data.master.network_element_types]


def device_type_rows(data: SeedData) -> list[dict[str, Any]]:
    return [t.model_dump(mode="json") for t in data.master.device_types]


def prompt_rows(data: SeedData) -> list[dict[str, Any]]:
    return [
        {
            "id": ids.prompt_version_id(prompt.workflow, prompt.version),
            "workflow": str(prompt.workflow),
            "version": prompt.version,
            "system_prompt": prompt.system_prompt,
            "user_prompt_template": prompt.user_prompt_template,
            "output_schema": prompt.output_schema,
            "model_name": prompt.model_name,
            "temperature": prompt.temperature,
            "is_active": prompt.is_active,
            "change_notes": prompt.change_notes,
            "created_by": prompt.created_by,
        }
        for prompt in data.prompts.prompts
    ]


async def write_master(session: AsyncSession, data: SeedData) -> None:
    await upsert(session, Store, store_rows(data))
    await upsert(session, AssignmentGroup, assignment_group_rows(data))
    await upsert(session, NetworkElementType, element_type_rows(data), conflict=("code",))
    await upsert(session, DeviceType, device_type_rows(data), conflict=("code",))
    await upsert(session, AiPromptVersion, prompt_rows(data))
