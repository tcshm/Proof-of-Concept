# tests/itsm_helpers.py
"""Small builders shared by the ITSM service tests (not a test module)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import ActorContext
from app.models import (
    AssignmentGroup,
    Device,
    DeviceType,
    NetworkElement,
    NetworkElementType,
    Store,
)
from app.models.values import ActorType, AuditSource
from app.services.incidents import NewIncident

NOW = datetime(2026, 10, 13, 3, 30, tzinfo=UTC)

IT = ActorContext(actor_type=ActorType.IT_STAFF, actor="r.kumar", source=AuditSource.ADMIN_PORTAL)
STORE_USER = ActorContext(
    actor_type=ActorType.STORE_USER, actor="store-01234", source=AuditSource.MOBILE_APP
)
AGENT = ActorContext(actor_type=ActorType.AGENT, actor="ric-agent", source=AuditSource.AGENT)


@dataclass
class Itsm:
    store_a: uuid.UUID
    store_b: uuid.UUID
    group: uuid.UUID
    ap: uuid.UUID
    pos: uuid.UUID
    ap_b: uuid.UUID


async def build_itsm_fixture(session: AsyncSession) -> Itsm:
    for model, code in ((NetworkElementType, "access_point"), (DeviceType, "pos")):
        if await session.get(model, code) is None:
            session.add(model(code=code, display_name=code, business_criticality="high"))
    a = Store(site_code="01234", name="A", region="R", country_code="IN", timezone="Asia/Kolkata")
    b = Store(site_code="05678", name="B", region="R", country_code="IN", timezone="Asia/Kolkata")
    group = AssignmentGroup(name=f"Network Ops {uuid.uuid4().hex[:6]}")
    session.add_all([a, b, group])
    await session.flush()
    ap = NetworkElement(
        store_id=a.id, element_type_code="access_point", name="AP-A", serial_number="SN-AP-A"
    )
    ap_b = NetworkElement(
        store_id=b.id, element_type_code="access_point", name="AP-B", serial_number="SN-AP-B"
    )
    pos = Device(store_id=a.id, device_type_code="pos", name="POS Terminal 03")
    session.add_all([ap, ap_b, pos])
    await session.flush()
    return Itsm(a.id, b.id, group.id, ap.id, pos.id, ap_b.id)


def new_incident(**overrides) -> NewIncident:
    values = {
        "short_description": "Wireless - Access Point A004",
        "category": "network",
        "impact": 2,
        "urgency": 2,
        "contact_type": "monitoring",
    }
    values.update(overrides)
    return NewIncident(**values)
