# tests/test_worker.py
"""The worker on the rolled-back test database, with a scripted fake LLM (no network)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FixedClock
from app.llm.fake import FakeProvider
from app.llm.types import LlmRejected, LlmUnavailable
from app.models import (
    AiPromptVersion,
    AiWorkflowRun,
    Incident,
    IncidentJournal,
    Notification,
    StoreAction,
    StoreAlert,
)
from app.seed.db import upsert
from app.seed.loader import load_seed_data
from app.seed.master import prompt_rows
from app.services import incidents as incident_svc
from app.worker.runner import Worker, WorkerConfig
from tests.itsm_helpers import IT, NOW, Itsm, build_itsm_fixture, new_incident

LATER = NOW + timedelta(hours=1)  # 10:00 IST: when the worker runs

IMPACT_OK = {
    "severity": "warning",
    "title": "Sales floor Wi-Fi down",
    "detail": "Scanners on the sales floor may disconnect. IT is working on it.",
    "rationale": "One access point is down.",
}


def classification(**values) -> dict:
    answer = {
        "decision": "action",
        "title": "Provide comms room access at 1 PM",
        "detail": "The AT&T engineer needs access to the comms room at 1 PM.",
        "severity": "warning",
        "expected_response": "acknowledge",
        "due_by_local": "2026-10-13T13:00",
        "rationale": "The store must give access.",
    }
    answer.update(values)
    return answer


NONE_ANSWER = {
    "decision": "none",
    "title": "",
    "detail": "",
    "severity": "info",
    "expected_response": "not_applicable",
    "due_by_local": "",
    "rationale": "Internal detail.",
}


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
async def itsm(db_session: AsyncSession) -> Itsm:
    # The seeded prompts become the only active prompt versions.
    await db_session.execute(update(AiPromptVersion).values(is_active=False))
    await upsert(db_session, AiPromptVersion, prompt_rows(load_seed_data()))
    return await build_itsm_fixture(db_session)


@pytest.fixture
def factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Worker sessions on the test's connection: their commits are savepoints."""
    return async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


def make_worker(factory, provider, **config) -> Worker:
    values = {
        "poll_seconds": 0,
        "min_call_interval": 0,
        "max_attempts": 3,
        "retry_delays": (timedelta(minutes=2), timedelta(minutes=10)),
        "rate_limit_pause": 0,
        "sweep_seconds": 60,
    }
    values.update(config)
    return Worker(factory, provider, FixedClock(LATER), WorkerConfig(**values), sleep=_no_sleep)


async def make_incident(db_session: AsyncSession, itsm: Itsm) -> Incident:
    return await incident_svc.create_incident(
        db_session, IT, itsm.store_a, new_incident(network_element_id=itsm.ap), NOW, commit=False
    )


async def add_note(db_session, incident, element: str, text: str, minutes: int):
    return await incident_svc.add_journal_entry(
        db_session, IT, incident.id, element, text, NOW + timedelta(minutes=minutes), commit=False
    )


async def entry_state(db_session: AsyncSession, entry_id: uuid.UUID):
    return (
        await db_session.execute(
            select(
                IncidentJournal.process_status,
                IncidentJournal.attempts,
                IncidentJournal.available_at,
                IncidentJournal.last_error,
            ).where(IncidentJournal.id == entry_id)
        )
    ).one()


async def created_entry_id(db_session: AsyncSession, incident: Incident) -> uuid.UUID:
    return await db_session.scalar(
        select(IncidentJournal.id).where(
            IncidentJournal.incident_id == incident.id, IncidentJournal.element == "system"
        )
    )


async def runs(db_session: AsyncSession, journal_id: uuid.UUID):
    return (
        await db_session.execute(
            select(AiWorkflowRun.status, AiWorkflowRun.outcome, AiWorkflowRun.model_name)
            .where(AiWorkflowRun.journal_id == journal_id)
            .order_by(AiWorkflowRun.started_at)
        )
    ).all()


# --- impact assessment -----------------------------------------------------------------
async def test_impact_creates_alert_and_no_notification(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    await add_note(db_session, incident, "work_notes", "Remote PoE reset on port 11 failed.", 1)
    provider = FakeProvider([IMPACT_OK])
    result = await make_worker(factory, provider).process_next()

    assert (result.incident_number, result.outcome) == (incident.number, "alert_created")
    alert = (
        await db_session.execute(
            select(
                StoreAlert.title,
                StoreAlert.severity,
                StoreAlert.source,
                StoreAlert.status,
                StoreAlert.created_at,
                StoreAlert.ai_workflow_run_id,
            ).where(StoreAlert.incident_id == incident.id)
        )
    ).one()
    assert (alert.title, alert.severity, alert.source, alert.status) == (
        "Sales floor Wi-Fi down",
        "warning",
        "llm",
        "active",
    )
    assert alert.created_at == NOW and alert.ai_workflow_run_id is not None
    notices = await db_session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.incident_id == incident.id)
    )
    assert notices == 0  # no notification when an alert is created

    entry_id = await created_entry_id(db_session, incident)
    assert (await entry_state(db_session, entry_id)).process_status == "done"
    assert await runs(db_session, entry_id) == [("succeeded", "alert_created", "fake:fake-model")]
    request = provider.requests[0]
    assert incident.number in request.user_prompt
    assert "PoE reset" not in request.user_prompt  # work notes never reach the impact prompt
    assert request.system_prompt.startswith("You are the store communication assistant")


async def test_existing_alert_is_reused(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    db_session.add(
        StoreAlert(
            store_id=itsm.store_a, incident_id=incident.id, title="Seeded alert",
            detail="Seeded.", source="seed", created_by="seed",
        )
    )  # fmt: skip
    await db_session.flush()
    result = await make_worker(factory, FakeProvider([IMPACT_OK])).process_next()
    assert result.outcome == "alert_created"
    titles = (
        await db_session.scalars(
            select(StoreAlert.title).where(StoreAlert.incident_id == incident.id)
        )
    ).all()
    assert titles == ["Seeded alert"]


async def test_final_invalid_answer_writes_template_alert(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    worker = make_worker(factory, FakeProvider(["Sorry, I cannot help."]), max_attempts=1)
    result = await worker.process_next()
    assert result.outcome == "template_alert"
    alert = (
        await db_session.execute(
            select(StoreAlert.title, StoreAlert.severity, StoreAlert.source).where(
                StoreAlert.incident_id == incident.id
            )
        )
    ).one()
    assert tuple(alert) == ("Wireless - Access Point A004", "warning", "system")
    entry_id = await created_entry_id(db_session, incident)
    assert (await entry_state(db_session, entry_id)).process_status == "done"
    assert [(r.status, r.outcome) for r in await runs(db_session, entry_id)] == [
        ("needs_review", "alert_created")
    ]


# --- update classification -------------------------------------------------------------
async def test_work_note_action(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    note = await add_note(
        db_session,
        incident,
        "work_notes",
        "AT&T engineer onsite at 1 PM; store must give access.",
        5,
    )
    provider = FakeProvider([IMPACT_OK, classification()])
    worker = make_worker(factory, provider)
    await worker.process_next()
    result = await worker.process_next()

    assert result.outcome == "action_created"
    action = (
        await db_session.execute(
            select(
                StoreAction.id,
                StoreAction.title,
                StoreAction.expected_response,
                StoreAction.due_by,
                StoreAction.source,
                StoreAction.created_at,
            ).where(StoreAction.source_journal_id == note.id)
        )
    ).one()
    assert (action.title, action.expected_response, action.source) == (
        "Provide comms room access at 1 PM",
        "acknowledge",
        "llm",
    )
    assert action.due_by == NOW + timedelta(hours=4)  # 13:00 IST
    assert action.created_at == note.created_at
    notice = (
        await db_session.execute(
            select(
                Notification.system_event,
                Notification.title,
                Notification.action_id,
                Notification.publish_at,
            ).where(Notification.source_journal_id == note.id)
        )
    ).one()
    assert tuple(notice) == (
        "action_required",
        "Action Required: Provide comms room access at 1 PM",
        action.id,
        note.created_at,
    )
    assert [(r.status, r.outcome) for r in await runs(db_session, note.id)] == [
        ("succeeded", "action_created")
    ]
    assert '"type": "work_note"' in provider.requests[1].user_prompt
    assert "AT&T engineer onsite" in provider.requests[1].user_prompt


async def test_comment_is_always_sent_as_written(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    text = "The access point will be replaced today. Use the checkout area Wi-Fi meanwhile."
    comment = await add_note(db_session, incident, "comments", text, 5)
    worker = make_worker(factory, FakeProvider([IMPACT_OK, NONE_ANSWER]))
    await worker.process_next()
    result = await worker.process_next()
    assert result.outcome == "notification_created"
    notice = (
        await db_session.execute(
            select(Notification.system_event, Notification.title, Notification.description).where(
                Notification.source_journal_id == comment.id
            )
        )
    ).one()
    assert tuple(notice) == ("incident_update", f"Incident {incident.number} Updated", text)


async def test_internal_work_note_sends_nothing(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    note = await add_note(db_session, incident, "work_notes", "Checked switch logs.", 5)
    worker = make_worker(factory, FakeProvider([IMPACT_OK, NONE_ANSWER]))
    await worker.process_next()
    result = await worker.process_next()
    assert result.outcome == "no_action"
    notices = await db_session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.source_journal_id == note.id)
    )
    assert notices == 0
    assert (await entry_state(db_session, note.id)).process_status == "done"


async def test_resolution_asks_yes_no_and_old_entries_are_skipped(db_session, itsm, factory):
    incident = await make_incident(db_session, itsm)
    await add_note(db_session, incident, "work_notes", "Investigating.", 1)
    await incident_svc.resolve_incident(
        db_session, IT, incident.id, "Solved (Permanently)",
        "Replaced the access point. Store to confirm the sales floor Wi-Fi works.",
        NOW + timedelta(minutes=2), commit=False,
    )  # fmt: skip
    answer = classification(
        title="Is the sales floor Wi-Fi working now?",
        due_by_local="",
        expected_response="acknowledge",
    )
    provider = FakeProvider([answer])
    worker = make_worker(factory, provider)
    outcomes = [(await worker.process_next()).outcome for _ in range(3)]

    assert outcomes == ["skipped", "skipped", "action_created"]
    assert len(provider.requests) == 1  # skipped entries never call the LLM
    assert '"type": "resolution"' in provider.requests[0].user_prompt
    action = (
        await db_session.execute(
            select(StoreAction.title, StoreAction.expected_response, StoreAction.due_by).where(
                StoreAction.incident_id == incident.id
            )
        )
    ).one()
    assert tuple(action) == ("Is the sales floor Wi-Fi working now?", "yes_no", None)
    assert await worker.process_next() is None


# --- failures --------------------------------------------------------------------------
async def test_unavailable_is_retried_and_blocks_later_entries(db_session, itsm, factory):
    incident = await make_incident(db_session, itsm)
    await add_note(db_session, incident, "work_notes", "Later note.", 5)
    provider = FakeProvider([LlmUnavailable("HTTP 503: busy")])
    worker = make_worker(factory, provider)
    result = await worker.process_next()

    assert result.outcome == "retry"
    entry_id = await created_entry_id(db_session, incident)
    state = await entry_state(db_session, entry_id)
    assert (state.process_status, state.attempts) == ("pending", 1)
    assert state.available_at == LATER + timedelta(minutes=2)
    assert "HTTP 503" in state.last_error
    assert [r.status for r in await runs(db_session, entry_id)] == ["failed"]
    # The created entry is not due yet, and the later note waits for it.
    assert await worker.process_next() is None
    assert len(provider.requests) == 1


async def test_rejected_classification_fails_at_once(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    note = await add_note(db_session, incident, "work_notes", "A note.", 5)
    worker = make_worker(factory, FakeProvider([IMPACT_OK, LlmRejected("HTTP 401: invalid key")]))
    await worker.process_next()
    result = await worker.process_next()
    assert result.outcome == "failed"
    state = await entry_state(db_session, note.id)
    assert (state.process_status, state.attempts) == ("failed", 1)
    assert "HTTP 401" in state.last_error


async def test_incomplete_action_is_retried(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    note = await add_note(db_session, incident, "work_notes", "A note.", 5)
    worker = make_worker(factory, FakeProvider([IMPACT_OK, classification(title="")]))
    await worker.process_next()
    result = await worker.process_next()
    assert result.outcome == "retry"
    assert [(r.status, r.outcome) for r in await runs(db_session, note.id)] == [
        ("failed", "invalid_output")
    ]
    assert (await entry_state(db_session, note.id)).process_status == "pending"


# --- housekeeping and loops ------------------------------------------------------------
async def test_sweep_expires_overdue_actions(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)

    def action(title: str, due) -> StoreAction:
        return StoreAction(
            store_id=itsm.store_a, incident_id=incident.id, title=title, detail="d",
            expected_response="acknowledge", due_by=due, source="seed", created_by="seed",
        )  # fmt: skip

    db_session.add_all(
        [
            action("overdue", NOW + timedelta(minutes=30)),
            action("later", NOW + timedelta(hours=3)),
            action("no due time", None),
        ]
    )
    await db_session.flush()
    assert await make_worker(factory, FakeProvider([])).sweep() == 1
    rows = dict(
        (
            await db_session.execute(
                select(StoreAction.title, StoreAction.close_reason).where(
                    StoreAction.incident_id == incident.id
                )
            )
        ).all()
    )
    assert rows == {"overdue": "expired", "later": None, "no due time": None}


async def test_drain_with_call_limit(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    await add_note(db_session, incident, "comments", "An engineer visits today.", 5)
    notify = classification(decision="notify", expected_response="not_applicable", due_by_local="")
    worker = make_worker(factory, FakeProvider([IMPACT_OK, notify]))

    first = await worker.drain(max_calls=1, wait_for_retries=False)
    assert (first.llm_calls, dict(first.outcomes), first.still_pending) == (
        1,
        {"alert_created": 1},
        1,
    )
    second = await worker.drain(wait_for_retries=False)
    assert (second.llm_calls, dict(second.outcomes), second.still_pending) == (
        2,
        {"notification_created": 1},
        0,
    )
    assert "LLM calls 2" in second.summary()


async def test_startup_requeues_unfinished_entries(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    entry_id = await created_entry_id(db_session, incident)
    await db_session.execute(
        update(IncidentJournal)
        .where(IncidentJournal.id == entry_id)
        .values(process_status="processing")
    )
    await make_worker(factory, FakeProvider([])).startup()
    assert (await entry_state(db_session, entry_id)).process_status == "pending"


async def test_pacing_and_rate_limit_pause() -> None:
    now = [100.0]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    config = WorkerConfig(min_call_interval=10, rate_limit_pause=60)
    worker = Worker(None, FakeProvider([]), FixedClock(LATER), config, sleep=sleep,
                    monotonic=lambda: now[0])  # type: ignore[arg-type]  # fmt: skip
    await worker._pace()
    now[0] += 4
    await worker._pace()
    worker._pause_for_rate_limit()
    await worker._pace()
    assert sleeps == [6.0, 60.0]
