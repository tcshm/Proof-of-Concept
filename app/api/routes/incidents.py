# app/api/routes/incidents.py
"""GET /api/v1/incidents (the store's active incidents) and
GET /api/v1/incidents/{number} (one incident with its store-visible comments)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path

from app.api.context import StoreContext, StoreDep
from app.api.deps import ClockDep, SessionDep
from app.api.mappers import incidents as m
from app.api.schemas.incidents import IncidentComment, IncidentDetail, IncidentItem
from app.core.errors import NotFoundError
from app.queries.incidents import IncidentRow, active_incidents, incident_detail

router = APIRouter(prefix="/incidents", tags=["incidents"])

INCIDENT_NUMBER_PATTERN = r"^INC[0-9]{7,}$"


def _item_fields(row: IncidentRow, store: StoreContext, now: datetime) -> dict:
    return {
        "id": row.number,
        "title": row.short_description,
        "subtitle": row.u_symptom,
        "service": row.business_service,
        "summary": m.summary(row.latest_it_comment, row.description),
        "ci_type": row.ci_type,
        "severity": m.severity(row.priority),
        "status": m.list_status(row.state),
        "badge": m.badge(row.state, row.hold_reason),
        "reported_at": row.opened_at,
        "incident_type": m.incident_type(row.contact_type, row.ci_kind),
        "incident_status_new": m.status_new(row.state),
        "resolution_eta": m.resolution_eta(
            row.u_resolution_eta, row.u_eta_note, store.timezone, now
        ),
        "resolution_group": row.group_name,
    }


@router.get("", response_model=list[IncidentItem])
async def list_incidents(
    store: StoreDep, session: SessionDep, clock: ClockDep
) -> list[IncidentItem]:
    now = clock.now()
    return [
        IncidentItem(**_item_fields(row, store, now))
        for row in await active_incidents(session, store.id)
    ]


@router.get("/{number}", response_model=IncidentDetail)
async def get_incident(
    store: StoreDep,
    session: SessionDep,
    clock: ClockDep,
    number: Annotated[str, Path(pattern=INCIDENT_NUMBER_PATTERN, max_length=20)],
) -> IncidentDetail:
    found = await incident_detail(session, store.id, number)
    if found is None:
        # Also for another store's incident: do not reveal that the number exists.
        raise NotFoundError(f"Incident {number} was not found.")
    row, comments = found
    return IncidentDetail(
        **_item_fields(row, store, clock.now()),
        description=row.description,
        assigned_to=row.assigned_to,
        comments=[
            IncidentComment(
                id=c.id,
                text=c.value,
                author_type=c.author_type,
                author=c.created_by,
                created_at=c.created_at,
            )
            for c in comments
        ],
    )
