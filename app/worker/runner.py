# app/worker/runner.py
"""The worker loop.

For each pending journal entry:
    1. claim it (short transaction)
    2. plan: impact assessment, update classification, or skip (no LLM call)
    3. build the context, wait for the pacing slot, call the LLM: the 'running'
       ai_workflow_runs row is committed first and the model is called outside
       any transaction (app.llm.runs.call_and_record)
    4. check the answer, then apply it in ONE transaction: alert / action /
       notification + run 'succeeded' + entry 'done'
    5. on failure: retry later (WORKER_RETRY_DELAYS_MINUTES), or after the last
       attempt write a template alert (impact) / mark the entry 'failed'

Free-tier quotas: calls are paced (WORKER_MAX_CALLS_PER_MINUTE), a 429 pauses
the worker (WORKER_RATE_LIMIT_PAUSE_SECONDS), entries that need no LLM are
skipped, a rejected request (bad key / model) is never retried, and drain() can
stop after a number of calls (--max-calls).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.actor import ActorContext
from app.core.clock import Clock
from app.core.config import Settings
from app.llm.prompts import PromptError, load_active_prompt
from app.llm.runs import call_and_record, fail_stale_runs, record_failure, record_success
from app.llm.types import LlmConfigError, LlmError, LlmProvider, LlmRejected, LlmResult
from app.models import AiWorkflowRun, Incident
from app.models.values import (
    ACTIVE_INCIDENT_STATES,
    ActionSource,
    AiOutcome,
    AiRunStatus,
    AiWorkflow,
    AlertSource,
    AuditSource,
    JournalProcessStatus,
    SystemEvent,
)
from app.services.actions import create_action, expire_overdue_actions
from app.services.alerts import create_alert
from app.services.notification_templates import action_required, incident_update
from app.services.notifications import create_system_notification
from app.worker.context import IncidentInfo, build_context, load_incident
from app.worker.queue import (
    ClaimedEntry,
    claim_next,
    fail_entry,
    finish_entry,
    pending_count,
    release_for_retry,
    reset_processing,
)
from app.worker.rules import (
    AlertText,
    ClassificationPlan,
    Decision,
    Plan,
    Route,
    check_alert_output,
    check_classification,
    is_rate_limited,
    plan_entry,
    retry_delay,
    template_alert,
)

logger = logging.getLogger("app.worker")

WORKER_ACTOR = ActorContext.system("change-worker", AuditSource.WORKER)

# Outcome labels returned by process_next (and counted by drain).
ALERT_CREATED = "alert_created"
ACTION_CREATED = "action_created"
NOTIFICATION_CREATED = "notification_created"
NO_ACTION = "no_action"
SKIPPED = "skipped"
TEMPLATE_ALERT = "template_alert"
RETRY = "retry"
FAILED = "failed"


@dataclass(frozen=True)
class WorkerConfig:
    poll_seconds: float = 5.0
    min_call_interval: float = 3.0
    max_attempts: int = 3
    retry_delays: tuple[timedelta, ...] = (timedelta(minutes=2), timedelta(minutes=10))
    rate_limit_pause: float = 60.0
    sweep_seconds: float = 60.0

    @classmethod
    def from_settings(cls, settings: Settings) -> WorkerConfig:
        try:
            delays = tuple(
                timedelta(minutes=float(part))
                for part in settings.worker_retry_delays_minutes.split(",")
                if part.strip()
            )
        except ValueError as exc:
            raise ValueError(
                "WORKER_RETRY_DELAYS_MINUTES must be comma-separated minutes, e.g. 2,10"
            ) from exc
        return cls(
            poll_seconds=settings.worker_poll_seconds,
            min_call_interval=60.0 / settings.worker_max_calls_per_minute,
            max_attempts=settings.worker_max_attempts,
            retry_delays=delays or (timedelta(minutes=2),),
            rate_limit_pause=settings.worker_rate_limit_pause_seconds,
            sweep_seconds=settings.worker_sweep_seconds,
        )


@dataclass(frozen=True)
class EntryResult:
    entry_id: Any
    incident_number: str
    outcome: str
    detail: str = ""


@dataclass
class DrainStats:
    outcomes: Counter = field(default_factory=Counter)
    llm_calls: int = 0
    expired_actions: int = 0
    still_pending: int = 0

    def summary(self) -> str:
        parts = ", ".join(f"{name} {count}" for name, count in sorted(self.outcomes.items()))
        return (
            f"entries: {parts or 'none'}; LLM calls {self.llm_calls}; "
            f"expired actions {self.expired_actions}; still pending {self.still_pending}"
        )


class Worker:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        provider: LlmProvider,
        clock: Clock,
        config: WorkerConfig | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.factory = factory
        self.provider = provider
        self.clock = clock
        self.config = config or WorkerConfig()
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_call: float | None = None
        self._paused_until = 0.0
        self.llm_calls = 0

    # --- housekeeping -------------------------------------------------------------------
    async def startup(self) -> None:
        """Recover from a stopped worker: entries left 'processing' go back to the
        queue; AI runs left 'running' for a long time are marked failed."""
        async with self.factory() as session:
            reset = await reset_processing(session)
            stale = await fail_stale_runs(session, self.clock.now())
            await session.commit()
        if reset or stale:
            logger.warning(
                "worker recovered unfinished work",
                extra={"entriesReset": reset, "runsFailed": stale},
            )

    async def sweep(self) -> int:
        """Expire open actions whose due time has passed."""
        async with self.factory() as session:
            expired = await expire_overdue_actions(session, WORKER_ACTOR, self.clock.now())
        if expired:
            logger.info("expired overdue actions", extra={"count": expired})
        return expired

    # --- pacing -------------------------------------------------------------------------
    async def _pace(self) -> None:
        now = self._monotonic()
        wait = self._paused_until - now
        if self._last_call is not None:
            wait = max(wait, self._last_call + self.config.min_call_interval - now)
        if wait > 0:
            await self._sleep(wait)
        self._last_call = self._monotonic()

    def _pause_for_rate_limit(self) -> None:
        self._paused_until = self._monotonic() + self.config.rate_limit_pause
        logger.warning(
            "LLM rate limit reached; pausing calls",
            extra={"seconds": self.config.rate_limit_pause},
        )

    # --- one entry ----------------------------------------------------------------------
    async def process_next(self) -> EntryResult | None:
        """Process the next due entry. None when nothing is due."""
        now = self.clock.now()
        async with self.factory() as session:
            entry = await claim_next(session, now)
            await session.commit()
        if entry is None:
            return None

        try:
            async with self.factory() as session:
                info = await load_incident(session, entry.incident_id)
        except Exception as exc:  # unexpected (e.g. database error)
            logger.exception("could not load the incident of a journal entry")
            return await self._unexpected(entry, "?", exc)

        plan = plan_entry(
            element=entry.element,
            author_type=entry.author_type,
            incident_state=info.state,
            resolved_at=info.resolved_at,
            entry_created_at=entry.created_at,
        )
        if plan.route == Route.SKIP:
            async with self.factory() as session:
                await finish_entry(session, entry.id, JournalProcessStatus.SKIPPED, now)
                await session.commit()
            logger.info(
                "journal entry skipped",
                extra={"incident": info.number, "entry": str(entry.id), "reason": plan.reason},
            )
            return EntryResult(entry.id, info.number, SKIPPED, plan.reason)

        workflow = AiWorkflow(plan.route.value)
        try:
            async with self.factory() as session:
                prompt = await load_active_prompt(session, workflow)
                context = await build_context(session, info, entry, plan, now)
        except (PromptError, LlmConfigError) as exc:
            return await self._fail(entry, info, f"{exc.__class__.__name__}: {exc}")

        await self._pace()
        self.llm_calls += 1
        run_id = None
        try:
            call = await call_and_record(
                self.factory,
                self.provider,
                prompt,
                context,
                journal_id=entry.id,
                incident_id=entry.incident_id,
                clock=self.clock,
            )
            run_id = call.run_id
            if plan.route == Route.IMPACT:
                alert = check_alert_output(call.result.output)
                return await self._apply_alert(entry, info, run_id, call.result, alert)
            classification = check_classification(
                call.result.output,
                element=entry.element,
                entry_text=entry.value,
                is_resolution=plan.is_resolution,
                tz=info.timezone,
                now=self.clock.now(),
            )
            return await self._apply_classification(
                entry, info, run_id, call.result, classification
            )
        except LlmError as exc:
            if run_id is not None:  # the answer failed our checks: record it on the run
                await self._record_run_failure(run_id, exc)
            return await self._llm_error(entry, info, plan, exc)
        except Exception as exc:
            logger.exception("unexpected error while applying an LLM result")
            if run_id is not None:
                await self._record_run_failure(run_id, exc)
            return await self._unexpected(entry, info.number, exc)

    # --- applying results -----------------------------------------------------------------
    async def _apply_alert(
        self,
        entry: ClaimedEntry,
        info: IncidentInfo,
        run_id: Any,
        result: LlmResult,
        alert: AlertText,
    ) -> EntryResult:
        now = self.clock.now()
        async with self.factory() as session:
            state = await session.scalar(
                select(Incident.state).where(Incident.id == info.id).with_for_update()
            )
            if state in ACTIVE_INCIDENT_STATES:
                await create_alert(
                    session,
                    WORKER_ACTOR,
                    store_id=info.store_id,
                    incident_id=info.id,
                    title=alert.title,
                    detail=alert.detail,
                    severity=alert.severity,
                    source=AlertSource.LLM,
                    ai_workflow_run_id=run_id,
                    created_at=entry.created_at,
                )
                outcome = ALERT_CREATED
            else:  # resolved or cancelled while the model was answering
                outcome = NO_ACTION
            await record_success(session, run_id, result, AiOutcome(outcome), now)
            await finish_entry(session, entry.id, JournalProcessStatus.DONE, now)
            await session.commit()
        logger.info("alert created", extra={"incident": info.number, "outcome": outcome})
        return EntryResult(entry.id, info.number, outcome, alert.title)

    async def _apply_classification(
        self,
        entry: ClaimedEntry,
        info: IncidentInfo,
        run_id: Any,
        result: LlmResult,
        plan: ClassificationPlan,
    ) -> EntryResult:
        now = self.clock.now()
        async with self.factory() as session:
            if plan.decision == Decision.ACTION:
                action, _ = await create_action(
                    session,
                    WORKER_ACTOR,
                    store_id=info.store_id,
                    incident_id=info.id,
                    source_journal_id=entry.id,
                    title=plan.title or "",
                    detail=plan.detail or "",
                    severity=plan.severity,
                    expected_response=plan.expected_response or "acknowledge",
                    due_by=plan.due_by,
                    source=ActionSource.LLM,
                    ai_workflow_run_id=run_id,
                    created_at=entry.created_at,
                )
                content = action_required(
                    incident_number=info.number,
                    action_title=action.title,
                    action_detail=action.detail,
                    publish_at=entry.created_at,
                )
                await create_system_notification(
                    session,
                    WORKER_ACTOR,
                    event=SystemEvent.ACTION_REQUIRED,
                    store_id=info.store_id,
                    content=content,
                    publish_at=entry.created_at,
                    incident_id=info.id,
                    action_id=action.id,
                    source_journal_id=entry.id,
                )
                outcome, detail = ACTION_CREATED, action.title
            elif plan.decision == Decision.NOTIFY:
                content = incident_update(
                    incident_number=info.number,
                    message=plan.message or "",
                    publish_at=entry.created_at,
                )
                await create_system_notification(
                    session,
                    WORKER_ACTOR,
                    event=SystemEvent.INCIDENT_UPDATE,
                    store_id=info.store_id,
                    content=content,
                    publish_at=entry.created_at,
                    incident_id=info.id,
                    source_journal_id=entry.id,
                )
                outcome, detail = NOTIFICATION_CREATED, content.title
            else:
                outcome, detail = NO_ACTION, "internal update"
            await record_success(session, run_id, result, AiOutcome(outcome), now)
            await finish_entry(session, entry.id, JournalProcessStatus.DONE, now)
            await session.commit()
        logger.info("update classified", extra={"incident": info.number, "outcome": outcome})
        return EntryResult(entry.id, info.number, outcome, detail)

    async def _apply_template_alert(self, entry: ClaimedEntry, info: IncidentInfo) -> EntryResult:
        now = self.clock.now()
        async with self.factory() as session:
            run_id = await session.scalar(
                select(AiWorkflowRun.id)
                .where(AiWorkflowRun.journal_id == entry.id)
                .order_by(AiWorkflowRun.started_at.desc())
                .limit(1)
            )
            state = await session.scalar(
                select(Incident.state).where(Incident.id == info.id).with_for_update()
            )
            if run_id is None:
                await fail_entry(session, entry.id, "no AI run to attach the alert to", now)
                await session.commit()
                return EntryResult(entry.id, info.number, FAILED, "no AI run")
            if state not in ACTIVE_INCIDENT_STATES:
                await finish_entry(session, entry.id, JournalProcessStatus.SKIPPED, now)
                await session.commit()
                return EntryResult(entry.id, info.number, SKIPPED, "the incident is not active")
            text = template_alert(
                short_description=info.short_description,
                symptom=info.u_symptom,
                priority=info.priority,
            )
            await create_alert(
                session,
                WORKER_ACTOR,
                store_id=info.store_id,
                incident_id=info.id,
                title=text.title,
                detail=text.detail,
                severity=text.severity,
                source=AlertSource.SYSTEM,
                ai_workflow_run_id=run_id,
                created_at=entry.created_at,
            )
            await session.execute(
                update(AiWorkflowRun)
                .where(AiWorkflowRun.id == run_id)
                .values(status=AiRunStatus.NEEDS_REVIEW, outcome=AiOutcome.ALERT_CREATED)
                .execution_options(synchronize_session=False)
            )
            await finish_entry(session, entry.id, JournalProcessStatus.DONE, now)
            await session.commit()
        logger.warning("template alert written", extra={"incident": info.number})
        return EntryResult(entry.id, info.number, TEMPLATE_ALERT, text.title)

    # --- failures --------------------------------------------------------------------------
    async def _record_run_failure(self, run_id: Any, error: Exception) -> None:
        try:
            async with self.factory() as session:
                await record_failure(session, run_id, error, self.clock.now())
                await session.commit()
        except Exception:
            logger.exception("could not record the AI run failure")

    async def _retry(self, entry: ClaimedEntry, number: str, error: str) -> EntryResult:
        available_at = self.clock.now() + retry_delay(entry.attempts, self.config.retry_delays)
        async with self.factory() as session:
            await release_for_retry(session, entry.id, error, available_at)
            await session.commit()
        logger.warning(
            "journal entry will be retried",
            extra={"incident": number, "attempt": entry.attempts, "error": error},
        )
        return EntryResult(entry.id, number, RETRY, error)

    async def _fail(self, entry: ClaimedEntry, info: IncidentInfo, error: str) -> EntryResult:
        async with self.factory() as session:
            await fail_entry(session, entry.id, error, self.clock.now())
            await session.commit()
        logger.error("journal entry failed", extra={"incident": info.number, "error": error})
        return EntryResult(entry.id, info.number, FAILED, error)

    async def _llm_error(
        self, entry: ClaimedEntry, info: IncidentInfo, plan: Plan, error: LlmError
    ) -> EntryResult:
        message = f"{error.__class__.__name__}: {error}"
        if is_rate_limited(error):
            self._pause_for_rate_limit()
        final = isinstance(error, LlmRejected) or entry.attempts >= self.config.max_attempts
        if not final:
            return await self._retry(entry, info.number, message)
        if plan.route == Route.IMPACT:
            return await self._apply_template_alert(entry, info)
        return await self._fail(entry, info, message)

    async def _unexpected(self, entry: ClaimedEntry, number: str, error: Exception) -> EntryResult:
        message = f"{error.__class__.__name__}: {error}"
        if entry.attempts < self.config.max_attempts:
            return await self._retry(entry, number, message)
        async with self.factory() as session:
            await fail_entry(session, entry.id, message, self.clock.now())
            await session.commit()
        return EntryResult(entry.id, number, FAILED, message)

    # --- loops -----------------------------------------------------------------------------
    async def drain(self, *, max_calls: int | None = None, wait_for_retries: bool = True):
        """Process until no entry is pending (or max_calls LLM calls were made)."""
        stats = DrainStats()
        await self.startup()
        stats.expired_actions += await self.sweep()
        while True:
            if max_calls is not None and self.llm_calls >= max_calls:
                break
            result = await self.process_next()
            if result is not None:
                stats.outcomes[result.outcome] += 1
                continue
            async with self.factory() as session:
                waiting = await pending_count(session)
            if waiting == 0 or not wait_for_retries:
                break
            await self._sleep(self.config.poll_seconds)
        stats.expired_actions += await self.sweep()
        async with self.factory() as session:
            stats.still_pending = await pending_count(session)
        stats.llm_calls = self.llm_calls
        return stats

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """Keep processing entries as they arrive; sweep expired actions regularly."""
        stop = stop or asyncio.Event()
        await self.startup()
        next_sweep = 0.0
        while not stop.is_set():
            if self._monotonic() >= next_sweep:
                await self.sweep()
                next_sweep = self._monotonic() + self.config.sweep_seconds
            result = await self.process_next()
            if result is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.config.poll_seconds)
                except TimeoutError:
                    pass
