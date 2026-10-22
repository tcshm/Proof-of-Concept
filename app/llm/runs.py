# app/llm/runs.py
"""Record every LLM call in ai_workflow_runs.

    start_run       before the call: status 'running', the exact input context
    record_success  after the result has been applied (in the SAME transaction
                    that writes the alert / action / notification): output,
                    outcome, tokens, latency. output defaults to the model's
                    answer; the worker passes {"answer", "applied", "adjustments"}
                    when its guards changed the answer.
    record_failure  after an LlmError: status 'failed' (outcome 'invalid_output'
                    for a bad answer), the error and any raw text
    call_and_record the usual sequence: commit the 'running' row in its own short
                    transaction, call the model OUTSIDE any transaction, record a
                    failure in a new transaction and re-raise
    fail_stale_runs rows still 'running' long after they started (the process
                    stopped mid-call) become 'failed'

model_name holds 'provider:model' of the model that actually answered.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.llm.prompts import PromptVersion, build_request
from app.llm.types import LlmError, LlmInvalidOutput, LlmProvider, LlmResult
from app.models import AiWorkflowRun
from app.models.values import AiOutcome, AiRunStatus

MAX_ERROR = 2000
MAX_RAW_TEXT = 4000
STALE_AFTER = timedelta(minutes=15)


@dataclass(frozen=True)
class CallOutcome:
    run_id: uuid.UUID
    result: LlmResult


def start_run(
    session: AsyncSession,
    *,
    workflow: str,
    journal_id: uuid.UUID,
    incident_id: uuid.UUID,
    prompt: PromptVersion,
    model_label: str,
    input_context: dict[str, Any],
    now: datetime,
) -> AiWorkflowRun:
    run = AiWorkflowRun(
        id=uuid.uuid4(),
        workflow=workflow,
        journal_id=journal_id,
        incident_id=incident_id,
        prompt_version_id=prompt.id,
        model_name=model_label,
        input_context=input_context,
        status=AiRunStatus.RUNNING,
        started_at=now,
    )
    session.add(run)
    return run


async def record_success(
    session: AsyncSession,
    run_id: uuid.UUID,
    result: LlmResult,
    outcome: AiOutcome | str,
    now: datetime,
    *,
    status: AiRunStatus | str = AiRunStatus.SUCCEEDED,
    output: dict[str, Any] | None = None,
) -> None:
    await session.execute(
        update(AiWorkflowRun)
        .where(AiWorkflowRun.id == run_id)
        .values(
            status=str(AiRunStatus(status)),
            outcome=str(AiOutcome(outcome)),
            output=result.output if output is None else output,
            model_name=result.model_label,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=result.latency_ms,
            error_message=None,
            completed_at=now,
        )
    )


async def record_failure(
    session: AsyncSession, run_id: uuid.UUID, error: Exception, now: datetime
) -> None:
    invalid = isinstance(error, LlmInvalidOutput)
    raw = getattr(error, "raw_text", None)
    values: dict[str, Any] = {
        "status": AiRunStatus.FAILED,
        "error_message": f"{error.__class__.__name__}: {error}"[:MAX_ERROR],
        "completed_at": now,
    }
    if invalid:
        values["outcome"] = AiOutcome.INVALID_OUTPUT
    if raw:
        values["output"] = {"raw_text": raw[:MAX_RAW_TEXT]}
    await session.execute(update(AiWorkflowRun).where(AiWorkflowRun.id == run_id).values(**values))


async def call_and_record(
    factory: async_sessionmaker[AsyncSession],
    provider: LlmProvider,
    prompt: PromptVersion,
    context: dict[str, Any],
    *,
    journal_id: uuid.UUID,
    incident_id: uuid.UUID,
    clock: Clock,
) -> CallOutcome:
    """Run row committed first, model called outside any transaction. On an
    LlmError the failure is recorded and the error re-raised for the worker."""
    request = build_request(prompt, context)
    async with factory() as session:
        run = start_run(
            session,
            workflow=prompt.workflow,
            journal_id=journal_id,
            incident_id=incident_id,
            prompt=prompt,
            model_label=f"{provider.name}:{provider.model}",
            input_context=context,
            now=clock.now(),
        )
        await session.commit()
    try:
        result = await provider.generate(request)
    except LlmError as exc:
        async with factory() as session:
            await record_failure(session, run.id, exc, clock.now())
            await session.commit()
        raise
    return CallOutcome(run_id=run.id, result=result)


async def fail_stale_runs(
    session: AsyncSession, now: datetime, older_than: timedelta = STALE_AFTER
) -> int:
    rows = await session.scalars(
        update(AiWorkflowRun)
        .where(
            AiWorkflowRun.status == AiRunStatus.RUNNING,
            AiWorkflowRun.started_at < now - older_than,
        )
        .values(
            status=AiRunStatus.FAILED,
            error_message="The worker stopped before the call finished.",
            completed_at=now,
        )
        .returning(AiWorkflowRun.id)
        .execution_options(synchronize_session=False)
    )
    return len(rows.all())
