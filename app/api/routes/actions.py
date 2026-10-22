# app/api/routes/actions.py
"""Store actions (all need X-Site-Id).

GET  /api/v1/actions                          open actions + answered in the last 24 h
POST /api/v1/actions/{action_id}/responses    the store's reply:
     201 a new reply; 200 the same clientRequestId again (the earlier reply)
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from app.api.context import MobileActorDep, StoreDep
from app.api.deps import ClockDep, SessionDep
from app.api.schemas.actions import (
    ActionItem,
    ActionReplyInfo,
    ActionReplyRequest,
    ActionReplyResult,
)
from app.core.timefmt import format_when
from app.queries.actions import visible_actions
from app.services.action_responses import NewReply, respond_to_action

router = APIRouter(prefix="/actions", tags=["actions"])


@router.get("", response_model=list[ActionItem])
async def list_actions(store: StoreDep, session: SessionDep, clock: ClockDep) -> list[ActionItem]:
    now = clock.now()
    return [
        ActionItem(
            id=row.id,
            title=row.title,
            detail=row.detail,
            severity=row.severity,
            expected_response=row.expected_response,
            due_by=row.due_by,
            due_by_text=format_when(row.due_by, store.timezone, now) if row.due_by else None,
            status=row.status,
            response=(
                ActionReplyInfo(
                    answer_yes_no=row.reply.answer_yes_no,
                    response_text=row.reply.response_text,
                    responded_by=row.reply.responded_by,
                    responded_at=row.reply.created_at,
                )
                if row.reply
                else None
            ),
            incident_id=row.incident_number,
            created_at=row.created_at,
        )
        for row in await visible_actions(session, store.id, now)
    ]


@router.post(
    "/{action_id}/responses",
    response_model=ActionReplyResult,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "Repeated clientRequestId: the earlier reply"}},
)
async def reply_to_action(
    action_id: uuid.UUID,
    body: ActionReplyRequest,
    store: StoreDep,
    actor: MobileActorDep,
    session: SessionDep,
    clock: ClockDep,
    http_response: Response,
) -> ActionReplyResult:
    result = await respond_to_action(
        session,
        actor,
        store.id,
        action_id,
        NewReply(
            client_request_id=body.client_request_id,
            answer_yes_no=body.answer_yes_no,
            response_text=body.response_text,
        ),
        clock.now(),
    )
    if not result.created:
        http_response.status_code = status.HTTP_200_OK
    reply = result.response
    return ActionReplyResult(
        id=reply.id,
        action_id=reply.action_id,
        response_type=reply.response_type,
        answer_yes_no=reply.answer_yes_no,
        response_text=reply.response_text,
        responded_by=reply.responded_by,
        responded_at=reply.created_at,
        action_status=result.action.status,
    )
