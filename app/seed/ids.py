# app/seed/ids.py
"""Deterministic identifiers.

uuid5(namespace, name) always returns the same UUID for the same name, so every
machine and every seed run produces the same ids for the same business keys.
"""

from __future__ import annotations

import hashlib
import uuid

RIC_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://ric-poc.local/seed")


def _uuid(*parts: str) -> uuid.UUID:
    return uuid.uuid5(RIC_NAMESPACE, ":".join(parts))


def store_id(site_code: str) -> uuid.UUID:
    return _uuid("store", site_code)


def assignment_group_id(name: str) -> uuid.UUID:
    return _uuid("assignment_group", name)


def network_element_id(site_code: str, name: str) -> uuid.UUID:
    return _uuid("network_element", site_code, name)


def device_id(site_code: str, name: str) -> uuid.UUID:
    return _uuid("device", site_code, name)


def prompt_version_id(workflow: str, version: int) -> uuid.UUID:
    return _uuid("ai_prompt_version", workflow, str(version))


def aruba_event_uuid(site_code: str, key: str) -> uuid.UUID:
    """Aruba's own event id (payload.id) for a generated event."""
    return _uuid("aruba_event", site_code, key)


def mac_address(*parts: str) -> str:
    """A stable, locally administered MAC address (first octet 02)."""
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).digest()
    return ":".join(f"{octet:02x}" for octet in (0x02, *digest[:5]))
