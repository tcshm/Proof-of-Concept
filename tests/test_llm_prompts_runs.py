# tests/test_llm_prompts_runs.py
"""Prompt loading / rendering and the ai_workflow_runs recorder (rolled-back test database)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FixedClock
from app.llm.fake import FakeProvider
from app.llm.prompts import (
    PromptError,
    PromptVersion,
    build_request,
    load_active_prompt,
    render_template,
)
from app.llm.runs import (
    call_and_record,
    fail_stale_runs,
    record_failure,
    record_success,
    start_run,
)
from app.llm.types import LlmConfigError, LlmInvalidOutput, LlmUnavailable
from app.models import AiPromptVersion, AiWorkflowRun, IncidentJournal
from app.services import incidents as incident_svc
from tests.itsm_helpers import IT, NOW, build_itsm_fixture, new_incident

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["severity", "title"],
    "properties": {
        "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
        "title": {"type": "string", "maxLength": 80},
    },
}
CONTEXT = {"incident": {"number": "INC00001234", "short_description": "ISP - Primary Link"}}


async def add_prompt(session: AsyncSession, **values) -> AiPromptVersion:
    """A new active impact_assessment prompt (any existing active one is switched off)."""
    workflow = values.pop("workflow", "impact_assessment")
    await session.execute(
        update(AiPromptVersion).where(AiPromptVersion.workflow == workflow).values(is_active=False)
    )
    row = AiPromptVersion(
        workflow=workflow,
        version=values.pop("version", 99),
        system_prompt="You assess the impact of IT incidents on a store.",
        user_prompt_template=values.pop(
            "user_prompt_template", "Incident:\n{context_json}\nStore: {store_name}"
        ),
        output_schema=values.pop("output_schema", SCHEMA),
        model_name="nvidia/nemotron-3.5-lightning-30b-a3b",
        temperature=Decimal("0.2"),
        is_active=True,
        created_by="test",
        **values,
    )
    session.add(row)
    await session.flush()
    return row


# --- prompts ---------------------------------------------------------------------------
async def test_load_active_prompt(db_session: AsyncSession) -> None:
    row = await add_prompt(db_session)
    prompt = await load_active_prompt(db_session, "impact_assessment")
    assert isinstance(prompt, PromptVersion)
    assert (prompt.id, prompt.version, prompt.temperature) == (row.id, 99, 0.2)
    assert prompt.output_schema == SCHEMA


async def test_no_active_prompt(db_session: AsyncSession) -> None:
    await db_session.execute(
        update(AiPromptVersion)
        .where(AiPromptVersion.workflow == "update_classification")
        .values(is_active=False)
    )
    with pytest.raises(PromptError, match="no active prompt version for workflow"):
        await load_active_prompt(db_session, "update_classification")


async def test_invalid_output_schema_is_rejected(db_session: AsyncSession) -> None:
    await add_prompt(db_session, output_schema={"type": "objectt"})
    with pytest.raises(LlmConfigError, match="invalid output schema"):
        await load_active_prompt(db_session, "impact_assessment")


async def test_build_request(db_session: AsyncSession) -> None:
    await add_prompt(db_session)
    prompt = await load_active_prompt(db_session, "impact_assessment")
    request = build_request(prompt, CONTEXT, extra_values={"store_name": "Chennai Anna Nagar"})
    assert request.system_prompt == "You assess the impact of IT incidents on a store."
    assert '"number": "INC00001234"' in request.user_prompt
    assert request.user_prompt.endswith("Store: Chennai Anna Nagar")
    assert (request.temperature, request.output_schema) == (0.2, SCHEMA)
    with pytest.raises(PromptError, match="store_name"):
        build_request(prompt, CONTEXT)


def test_render_template() -> None:
    assert render_template("A {x} and {{literal}}", {"x": 1}) == "A 1 and {literal}"
    with pytest.raises(PromptError, match="a, b"):
        render_template("{a} {b}", {})


# --- runs ------------------------------------------------------------------------------
@pytest.fixture
async def journal(db_session: AsyncSession) -> IncidentJournal:
    itsm = await build_itsm_fixture(db_session)
    incident = await incident_svc.create_incident(
        db_session, IT, itsm.store_a, new_incident(network_element_id=itsm.ap), NOW, commit=False
    )
    return await db_session.scalar(
        select(IncidentJournal).where(IncidentJournal.incident_id == incident.id)
    )


@pytest.fixture
async def prompt(db_session: AsyncSession) -> PromptVersion:
    await add_prompt(db_session, user_prompt_template="Incident:\n{context_json}")
    return await load_active_prompt(db_session, "impact_assessment")


async def _run(session: AsyncSession, run_id: uuid.UUID) -> AiWorkflowRun:
    return await session.get(AiWorkflowRun, run_id, populate_existing=True)


def _start(session: AsyncSession, journal: IncidentJournal, prompt: PromptVersion, **values):
    return start_run(
        session,
        workflow="impact_assessment",
        journal_id=journal.id,
        incident_id=journal.incident_id,
        prompt=prompt,
        model_label="nvidia:nvidia/nemotron-3.5-lightning-30b-a3b",
        input_context=CONTEXT,
        now=values.get("now", NOW),
    )


async def test_start_and_success(db_session: AsyncSession, journal, prompt) -> None:
    run = _start(db_session, journal, prompt)
    await db_session.flush()
    saved = await _run(db_session, run.id)
    assert (saved.status, saved.started_at, saved.completed_at) == ("running", NOW, None)
    assert saved.input_context == CONTEXT and saved.prompt_version_id == prompt.id

    result = await FakeProvider([{"severity": "warning", "title": "Primary link down"}]).generate(
        build_request(prompt, CONTEXT)
    )
    done = NOW + timedelta(seconds=40)
    await record_success(db_session, run.id, result, "alert_created", done)
    saved = await _run(db_session, run.id)
    assert (saved.status, saved.outcome, saved.completed_at) == ("succeeded", "alert_created", done)
    assert saved.output == {"severity": "warning", "title": "Primary link down"}
    assert saved.model_name == "fake:fake-model"
    assert saved.prompt_tokens is not None and saved.latency_ms == 1


async def test_failures(db_session: AsyncSession, journal, prompt) -> None:
    invalid_run = _start(db_session, journal, prompt)
    unavailable_run = _start(db_session, journal, prompt)
    await db_session.flush()

    await record_failure(
        db_session,
        invalid_run.id,
        LlmInvalidOutput("the answer is not valid JSON", raw_text="Sorry!"),
        NOW,
    )
    await record_failure(
        db_session, unavailable_run.id, LlmUnavailable("no answer within 120 s"), NOW
    )
    invalid = await _run(db_session, invalid_run.id)
    assert (invalid.status, invalid.outcome) == ("failed", "invalid_output")
    assert invalid.output == {"raw_text": "Sorry!"}
    assert invalid.error_message == "LlmInvalidOutput: the answer is not valid JSON"
    unavailable = await _run(db_session, unavailable_run.id)
    assert (unavailable.status, unavailable.outcome, unavailable.output) == ("failed", None, None)
    assert unavailable.completed_at == NOW


@pytest.fixture
def factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Sessions on the test's connection: their commits are savepoints, rolled back later."""
    return async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


async def test_call_and_record(db_session: AsyncSession, journal, prompt, factory) -> None:
    provider = FakeProvider([{"severity": "info", "title": "ok"}])
    outcome = await call_and_record(
        factory,
        provider,
        prompt,
        CONTEXT,
        journal_id=journal.id,
        incident_id=journal.incident_id,
        clock=FixedClock(NOW),
    )
    assert outcome.result.output == {"severity": "info", "title": "ok"}
    run = await _run(db_session, outcome.run_id)
    assert (run.status, run.model_name) == ("running", "fake:fake-model")
    assert '"INC00001234"' in provider.requests[0].user_prompt


async def test_call_and_record_failure(db_session: AsyncSession, journal, prompt, factory):
    with pytest.raises(LlmUnavailable):
        await call_and_record(
            factory,
            FakeProvider([LlmUnavailable("HTTP 429: rate limited")]),
            prompt,
            CONTEXT,
            journal_id=journal.id,
            incident_id=journal.incident_id,
            clock=FixedClock(NOW),
        )
    run = await db_session.scalar(
        select(AiWorkflowRun).where(AiWorkflowRun.journal_id == journal.id)
    )
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_message == "LlmUnavailable: fake:fake-model HTTP 429: rate limited"


async def test_fail_stale_runs(db_session: AsyncSession, journal, prompt) -> None:
    old = _start(db_session, journal, prompt, now=NOW - timedelta(hours=1))
    recent = _start(db_session, journal, prompt, now=NOW - timedelta(minutes=5))
    await db_session.flush()
    assert await fail_stale_runs(db_session, NOW) == 1
    assert (await _run(db_session, old.id)).status == "failed"
    assert (await _run(db_session, recent.id)).status == "running"
