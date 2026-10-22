# app/services/action_responses.py
"""A store's reply to an action: the ONLY code that writes store_action_responses.

respond_to_action, in ONE transaction:
    1. find the action in the caller's store (another store's action: 404)
    2. lock the incident, then the action (same lock order as the incident
       service, so closing an incident and replying never deadlock)
    3. same clientRequestId as an earlier reply to this action: return that
       reply, write nothing (safe retries from the phone)
    4. refuse answered, closed and overdue actions (409)
    5. validate the reply against expected_response (422)
    6. write the reply as a store comment on the incident (process_status
       'skipped': the worker never classifies it), the response row,
       action status 'responded' and an audit row; commit

The reply is only recorded for IT; nothing else happens automatically.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import ActorContext
from app.core.errors import BusinessRuleError, ConflictError, NotFoundError
from app.models import StoreAction, StoreActionResponse
from app.models.values import (
    ActionStatus,
    ActorType,
    AuditAction,
    JournalElement,
    ResponseSource,
)
from app.services.action_rules import journal_text, validate_reply
from app.services.audit import record_audit
from app.services.incidents import add_journal_entry, lock_incident


@dataclass(frozen=True)
class NewReply:
    client_request_id: uuid.UUID | None
    answer_yes_no: bool | None = None
    response_text: str | None = None


@dataclass(frozen=True)
class ActionReply:
    response: StoreActionResponse
    action: StoreAction
    created: bool  # False: an earlier reply with the same clientRequestId


async def _finish(session: AsyncSession, commit: bool) -> None:
    if commit:
        await session.commit()
    else:
        await session.flush()


def _source(actor: ActorContext) -> str:
    if actor.actor_type != ActorType.STORE_USER:
        raise BusinessRuleError("Only store users reply to store actions.")
    try:
        return str(ResponseSource(actor.source))
    except ValueError as exc:
        raise BusinessRuleError(f"Replies cannot come from {actor.source!r}.") from exc


async def respond_to_action(
    session: AsyncSession,
    actor: ActorContext,
    store_id: uuid.UUID,
    action_id: uuid.UUID,
    reply: NewReply,
    now: datetime,
    *,
    commit: bool = True,
) -> ActionReply:
    source = _source(actor)
    incident_id = await session.scalar(
        select(StoreAction.incident_id).where(
            StoreAction.id == action_id, StoreAction.store_id == store_id
        )
    )
    if incident_id is None:
        raise NotFoundError(f"Action {action_id} was not found.")
    await lock_incident(session, incident_id)
    action = await session.scalar(
        select(StoreAction)
        .where(StoreAction.id == action_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    if reply.client_request_id is not None:
        existing = await session.scalar(
            select(StoreActionResponse).where(
                StoreActionResponse.client_request_id == reply.client_request_id
            )
        )
        if existing is not None:
            if existing.action_id != action.id:
                raise ConflictError("That request ID was already used for another action.")
            await _finish(session, commit)
            return ActionReply(response=existing, action=action, created=False)

    if action.status == ActionStatus.RESPONDED:
        raise ConflictError("This action has already been answered.")
    if action.status != ActionStatus.OPEN:
        raise ConflictError("This action is closed.")
    if action.due_by is not None and action.due_by <= now:
        raise ConflictError("This action has expired.")

    valid = validate_reply(action.expected_response, reply.answer_yes_no, reply.response_text)
    entry = await add_journal_entry(
        session,
        actor,
        action.incident_id,
        JournalElement.COMMENTS,
        journal_text(action.title, action.expected_response, valid),
        now,
        commit=False,
    )
    response = StoreActionResponse(
        id=uuid.uuid4(),
        action_id=action.id,
        store_id=action.store_id,
        response_type=action.expected_response,
        answer_yes_no=valid.answer_yes_no,
        response_text=valid.response_text,
        responded_by=actor.actor,
        source=source,
        client_request_id=reply.client_request_id,
        journal_id=entry.id,
        created_at=now,
    )
    session.add(response)
    action.status = ActionStatus.RESPONDED
    await session.flush()
    record_audit(
        session,
        actor,
        action=AuditAction.RESPOND,
        entity_type="store_actions",
        entity_id=action.id,
        store_id=action.store_id,
        changes={
            "status": {"old": "open", "new": "responded"},
            "response_type": action.expected_response,
            "answer_yes_no": valid.answer_yes_no,
            "response_text": valid.response_text,
        },
    )
    await _finish(session, commit)
    return ActionReply(response=response, action=action, created=True)
