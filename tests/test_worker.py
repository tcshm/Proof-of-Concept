# tests/test_worker.py
"""The worker on the rolled-back test database, with a scripted fake LLM (no network).
The seeded prompt version 2 is active, so answers use the v2 output schema."""

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
ACCESS_NOTE = "AT&T engineer onsite at 1 PM; store must give access to the comms room."

IMPACT_OK = {
    "affected": ["wifi"],
    "severity": "warning",
    "title": "Sales floor Wi-Fi down",
    "detail": "Scanners on the sales floor may disconnect. IT is working on it.",
    "rationale": "One access point is down.",
}


def classification(**values) -> dict:
    answer = {
        "decision": "action",
        "store_request_quote": "store must give access to the comms room",
        "title": "Provide comms room access at 1 PM",
        "detail": "The AT&T engineer needs access to the comms room at 1 PM.",
        "severity": "warning",
        "expected_response": "acknowledge",
        "due_time_quote": "onsite at 1 PM",
        "due_by_local": "2026-10-13T13:00",
        "rationale": "Gate 1.",
    }
    answer.update(values)
    return answer


def notify(detail: str = "An engineer visits the store today.") -> dict:
    return classification(
        decision="notify",
        store_request_quote="",
        title="Engineer visit today",
        detail=detail,
        expected_response="not_applicable",
        due_time_quote="",
        due_by_local="",
    )


NONE_ANSWER = classification(
    decision="none", store_request_quote="", title="", detail="", severity="info",
    expected_response="not_applicable", due_time_quote="", due_by_local="",
)  # fmt: skip


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
async def itsm(db_session: AsyncSession) -> Itsm:
    # The seeded prompts become the only prompt versions (version 2 active).
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


async def add_open_action(db_session, itsm, incident, title: str, due=None) -> StoreAction:
    action = StoreAction(
        store_id=itsm.store_a, incident_id=incident.id, title=title, detail="Please do it.",
        expected_response="acknowledge", due_by=due, source="seed", created_by="seed",
        created_at=NOW - timedelta(minutes=30),
    )  # fmt: skip
    db_session.add(action)
    await db_session.flush()
    return action


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


async def runs(db_session: AsyncSession, journal_id: uuid.UUID) -> list[tuple]:
    rows = (
        await db_session.execute(
            select(AiWorkflowRun.status, AiWorkflowRun.outcome, AiWorkflowRun.output)
            .where(AiWorkflowRun.journal_id == journal_id)
            .order_by(AiWorkflowRun.started_at)
        )
    ).all()
    return [tuple(row) for row in rows]


async def open_titles(db_session: AsyncSession, incident: Incident) -> list[str]:
    return list(
        (
            await db_session.scalars(
                select(StoreAction.title).where(
                    StoreAction.incident_id == incident.id, StoreAction.status == "open"
                )
            )
        ).all()
    )


async def notices_for(db_session: AsyncSession, journal_id: uuid.UUID) -> list[tuple]:
    rows = (
        await db_session.execute(
            select(Notification.system_event, Notification.title, Notification.description).where(
                Notification.source_journal_id == journal_id
            )
        )
    ).all()
    return [tuple(row) for row in rows]


# --- impact assessment -----------------------------------------------------------------
async def test_impact_creates_alert_and_no_notification(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    await add_note(db_session, incident, "work_notes", "Remote PoE reset on port 11 failed.", 1)
    provider = FakeProvider([IMPACT_OK])
    result = await make_worker(factory, provider).process_next()

    assert (result.incident_number, result.outcome) == (incident.number, "alert_created")
    alert = (
        await db_session.execute(
            select(StoreAlert.title, StoreAlert.severity, StoreAlert.source).where(
                StoreAlert.incident_id == incident.id
            )
        )
    ).one()
    assert tuple(alert) == ("Sales floor Wi-Fi down", "warning", "llm")
    notices = await db_session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.incident_id == incident.id)
    )
    assert notices == 0
    entry_id = await created_entry_id(db_session, incident)
    [(status, outcome, output)] = await runs(db_session, entry_id)
    assert (status, outcome, output) == ("succeeded", "alert_created", IMPACT_OK)
    request = provider.requests[0]
    assert '"task": "assess_impact"' in request.user_prompt
    assert '"allowed_severities"' in request.user_prompt
    assert "PoE reset" not in request.user_prompt  # work notes never reach the impact prompt
    assert "AP-A" not in request.user_prompt  # the CI hostname is not sent


async def test_impact_guard_adjustment_is_recorded(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    answer = {**IMPACT_OK, "affected": ["no_visible_impact"], "severity": "critical"}
    await make_worker(factory, FakeProvider([answer])).process_next()
    severity = await db_session.scalar(
        select(StoreAlert.severity).where(StoreAlert.incident_id == incident.id)
    )
    assert severity == "info"
    [(_, _, output)] = await runs(db_session, await created_entry_id(db_session, incident))
    assert output["answer"] == answer and output["applied"]["severity"] == "info"
    assert output["adjustments"] == ["severity critical -> info: no visible impact"]


async def test_final_invalid_impact_writes_template_alert(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    result = await make_worker(factory, FakeProvider(["Sorry."]), max_attempts=1).process_next()
    assert result.outcome == "template_alert"
    alert = (
        await db_session.execute(
            select(StoreAlert.title, StoreAlert.source).where(StoreAlert.incident_id == incident.id)
        )
    ).one()
    assert tuple(alert) == ("Wireless - Access Point A004", "system")
    entry_id = await created_entry_id(db_session, incident)
    assert [(s, o) for s, o, _ in await runs(db_session, entry_id)] == [
        ("needs_review", "alert_created")
    ]


# --- update classification: the three gates ------------------------------------------------
async def test_grounded_request_creates_one_action(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    note = await add_note(db_session, incident, "work_notes", ACCESS_NOTE, 5)
    provider = FakeProvider([IMPACT_OK, classification()])
    worker = make_worker(factory, provider)
    await worker.process_next()
    result = await worker.process_next()

    assert result.outcome == "action_created"
    action = (
        await db_session.execute(
            select(StoreAction.id, StoreAction.expected_response, StoreAction.due_by).where(
                StoreAction.source_journal_id == note.id
            )
        )
    ).one()
    assert action.expected_response == "acknowledge"
    assert action.due_by == NOW + timedelta(hours=4)  # 13:00 IST
    [notice] = await notices_for(db_session, note.id)
    assert notice[:2] == ("action_required", "Action Required: Provide comms room access at 1 PM")
    [(status, outcome, output)] = await runs(db_session, note.id)
    assert (status, outcome) == ("succeeded", "action_created")
    assert output == classification()  # no adjustment: the plain answer is stored
    assert '"task": "classify_work_note"' in provider.requests[1].user_prompt


async def test_description_without_request_becomes_notify(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    note = await add_note(
        db_session, incident, "work_notes", "Register 04 is offline. Trying to reach store.", 5
    )
    answer = classification(store_request_quote="Register 04 is offline",
                            detail="Register 04 is offline; IT is investigating.")  # fmt: skip
    worker = make_worker(factory, FakeProvider([IMPACT_OK, answer]))
    await worker.process_next()
    result = await worker.process_next()

    assert result.outcome == "notification_created"
    assert await open_titles(db_session, incident) == []
    [notice] = await notices_for(db_session, note.id)
    assert notice == (
        "incident_update",
        f"Incident {incident.number} Updated",
        "Register 04 is offline; IT is investigating.",
    )
    [(status, _, output)] = await runs(db_session, note.id)
    assert status == "succeeded"
    assert output["applied"]["decision"] == "notify"
    assert output["adjustments"][0].startswith("request barrier:")


async def test_second_request_is_held_back(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    await add_open_action(db_session, itsm, incident, "Check Register 04 cable")
    note = await add_note(db_session, incident, "work_notes", ACCESS_NOTE, 5)
    worker = make_worker(factory, FakeProvider([IMPACT_OK, classification()]))
    await worker.process_next()
    result = await worker.process_next()

    assert result.outcome == "notification_created"
    assert await open_titles(db_session, incident) == ["Check Register 04 cable"]
    [notice] = await notices_for(db_session, note.id)
    assert notice[0] == "incident_update"
    [(status, outcome, output)] = await runs(db_session, note.id)
    assert (status, outcome) == ("needs_review", "notification_created")
    assert output["applied"]["held_back"] is True


async def test_comment_is_sent_as_written(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    text = "The access point will be replaced today. Use the checkout area Wi-Fi meanwhile."
    comment = await add_note(db_session, incident, "comments", text, 5)
    worker = make_worker(factory, FakeProvider([IMPACT_OK, NONE_ANSWER]))
    await worker.process_next()
    result = await worker.process_next()
    assert result.outcome == "notification_created"
    [notice] = await notices_for(db_session, comment.id)
    assert notice == ("incident_update", f"Incident {incident.number} Updated", text)


async def test_internal_work_note_sends_nothing(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    note = await add_note(db_session, incident, "work_notes", "Checked switch logs.", 5)
    worker = make_worker(factory, FakeProvider([IMPACT_OK, NONE_ANSWER]))
    await worker.process_next()
    assert (await worker.process_next()).outcome == "no_action"
    assert await notices_for(db_session, note.id) == []


# --- resolution ----------------------------------------------------------------------------
async def test_resolution_replaces_the_open_action(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    await add_open_action(db_session, itsm, incident, "Provide comms room access")
    await incident_svc.resolve_incident(
        db_session, IT, incident.id, "Solved (Permanently)",
        "Replaced the access point. Store to confirm the sales floor Wi-Fi works.",
        NOW + timedelta(minutes=2), commit=False,
    )  # fmt: skip
    answer = classification(
        decision="notify", store_request_quote="", title="Is the sales floor Wi-Fi working now?",
        detail="IT replaced the access point. Please check the Wi-Fi.",
        expected_response="not_applicable", due_time_quote="", due_by_local="",
    )  # fmt: skip
    provider = FakeProvider([answer])
    worker = make_worker(factory, provider)
    outcomes = [(await worker.process_next()).outcome for _ in range(2)]

    assert outcomes == ["skipped", "action_created"]
    assert len(provider.requests) == 1
    assert '"task": "confirm_fix"' in provider.requests[0].user_prompt
    assert await open_titles(db_session, incident) == ["Is the sales floor Wi-Fi working now?"]
    closed = await db_session.scalar(
        select(StoreAction.close_reason).where(StoreAction.title == "Provide comms room access")
    )
    assert closed == "manual"
    response = await db_session.scalar(
        select(StoreAction.expected_response).where(
            StoreAction.incident_id == incident.id, StoreAction.status == "open"
        )
    )
    assert response == "yes_no"


async def test_resolution_fallback_asks_template_question(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    await incident_svc.resolve_incident(
        db_session, IT, incident.id, "Solved", "Replaced the access point.",
        NOW + timedelta(minutes=2), commit=False,
    )  # fmt: skip
    worker = make_worker(factory, FakeProvider(["not json"]), max_attempts=1)
    outcomes = [(await worker.process_next()).outcome for _ in range(2)]
    assert outcomes == ["skipped", "fallback"]
    assert await open_titles(db_session, incident) == [
        'Is "Wireless - Access Point A004" fixed now?'
    ]


# --- failures --------------------------------------------------------------------------
async def test_unavailable_is_retried_and_blocks_later_entries(db_session, itsm, factory):
    incident = await make_incident(db_session, itsm)
    await add_note(db_session, incident, "work_notes", "Later note.", 5)
    provider = FakeProvider([LlmUnavailable("HTTP 503: busy")])
    worker = make_worker(factory, provider)
    assert (await worker.process_next()).outcome == "retry"
    entry_id = await created_entry_id(db_session, incident)
    state = await entry_state(db_session, entry_id)
    assert (state.process_status, state.attempts) == ("pending", 1)
    assert state.available_at == LATER + timedelta(minutes=2)
    assert await worker.process_next() is None
    assert len(provider.requests) == 1


async def test_rejected_work_note_fails_at_once(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    note = await add_note(db_session, incident, "work_notes", "A note.", 5)
    worker = make_worker(factory, FakeProvider([IMPACT_OK, LlmRejected("HTTP 401: invalid key")]))
    await worker.process_next()
    assert (await worker.process_next()).outcome == "failed"
    assert (await entry_state(db_session, note.id)).process_status == "failed"


async def test_failed_comment_is_still_sent(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    comment = await add_note(db_session, incident, "comments", "Engineer arrives at noon.", 5)
    worker = make_worker(factory, FakeProvider([IMPACT_OK, "not json"]), max_attempts=1)
    await worker.process_next()
    assert (await worker.process_next()).outcome == "fallback"
    [notice] = await notices_for(db_session, comment.id)
    assert notice[2] == "Engineer arrives at noon."


# --- housekeeping and loops ------------------------------------------------------------
async def test_sweep_expires_overdue_actions(db_session, itsm, factory) -> None:
    first, second = await make_incident(db_session, itsm), await make_incident(db_session, itsm)
    await add_open_action(db_session, itsm, first, "overdue", NOW + timedelta(minutes=30))
    await add_open_action(db_session, itsm, second, "later", NOW + timedelta(hours=3))
    assert await make_worker(factory, FakeProvider([])).sweep() == 1
    reasons = dict(
        (
            await db_session.execute(
                select(StoreAction.title, StoreAction.close_reason).where(
                    StoreAction.incident_id.in_([first.id, second.id])
                )
            )
        ).all()
    )
    assert reasons == {"overdue": "expired", "later": None}


async def test_drain_with_call_limit(db_session, itsm, factory) -> None:
    incident = await make_incident(db_session, itsm)
    await add_note(db_session, incident, "comments", "An engineer visits today.", 5)
    worker = make_worker(factory, FakeProvider([IMPACT_OK, notify()]))
    first = await worker.drain(max_calls=1, wait_for_retries=False)
    assert (first.llm_calls, dict(first.outcomes), first.still_pending) == (
        1,
        {"alert_created": 1},
        1,
    )
    second = await worker.drain(wait_for_retries=False)
    assert (dict(second.outcomes), second.still_pending) == ({"notification_created": 1}, 0)


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
