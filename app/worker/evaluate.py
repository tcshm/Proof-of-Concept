# app/worker/evaluate.py
"""Evaluation scorecard for the two LLM workflows (used by scripts/llm_eval.py).

Each case in seed/eval/llm_cases.yaml is a ready-made context plus the expected
outcome. run_case() sends it through EXACTLY the worker's path: the active
prompt version from seed/data/prompts.yaml -> the configured model -> schema
validation -> the guards. Nothing is written to the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.db.migrate import PROJECT_ROOT
from app.llm.prompts import PromptVersion, build_request
from app.llm.types import LlmError, LlmInvalidOutput, LlmProvider
from app.models.values import AiWorkflow
from app.seed import ids
from app.seed.loader import load_seed_data
from app.services.incident_rules import state_label
from app.worker.context import TRIGGER_TYPES, assemble_context, local_text, store_facts
from app.worker.guards import OpenActionInfo, allowed_severities, check_alert, check_classification
from app.worker.rules import Task

CASES_FILE = PROJECT_ROOT / "seed" / "eval" / "llm_cases.yaml"
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaseStore(_Model):
    name: str
    timezone: str
    now_local: str  # "2026-11-04T10:00" in store time
    opening_hours: tuple[str, str] = ("10:00", "22:00")


class CaseIncident(_Model):
    number: str = "INC00009001"
    title: str
    description: str | None = None
    symptom: str | None = None
    category: str = "network"
    priority: int = Field(default=3, ge=1, le=5)
    state: int = 2
    hold_reason: str | None = None
    eta_note: str | None = None


class CaseCi(_Model):
    kind: Literal["network element", "store device"] = "store device"
    type: str
    name: str
    location: str | None = None
    business_criticality: Literal["high", "medium", "low"] = "medium"


class Expect(_Model):
    decision: list[Literal["action", "notify", "none"]] | None = None
    expected_response: list[Literal["acknowledge", "yes_no", "free_text"]] | None = None
    due_local: str | None = None  # "13:00", or "none" (no due time)
    severity_in: list[Literal["critical", "warning", "info"]] | None = None
    must_not_contain: list[str] = Field(default_factory=list)


class EvalCase(_Model):
    name: str
    task: Task
    store: CaseStore
    incident: CaseIncident
    main_ci: CaseCi | None = None
    trigger: str | None = None
    open_action: str | None = None
    expect: Expect


@dataclass
class CaseResult:
    name: str
    expected: str
    got: str
    ok: bool
    adjustments: list[str] = field(default_factory=list)
    error: str | None = None


def load_cases(path: Path = CASES_FILE) -> list[EvalCase]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    default_store = raw.get("defaults", {}).get("store", {})
    cases = []
    for item in raw.get("cases", []):
        item = dict(item)
        item["store"] = {**default_store, **item.get("store", {})}
        try:
            cases.append(EvalCase.model_validate(item))
        except ValidationError as exc:
            raise ValueError(f"{path.name}: case {item.get('name')!r}: {exc}") from exc
    names = [c.name for c in cases]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"{path.name}: duplicate case names: {duplicates}")
    return cases


def active_prompts() -> dict[str, PromptVersion]:
    """The active prompt version of each workflow, from seed/data/prompts.yaml."""
    prompts = {}
    for p in load_seed_data().prompts.prompts:
        if p.is_active:
            workflow = str(p.workflow)
            prompts[workflow] = PromptVersion(
                id=ids.prompt_version_id(workflow, p.version),
                workflow=workflow,
                version=p.version,
                system_prompt=p.system_prompt,
                user_prompt_template=p.user_prompt_template,
                output_schema=p.output_schema,
                model_name=p.model_name,
                temperature=float(p.temperature),
            )
    return prompts


@dataclass(frozen=True)
class CaseInput:
    context: dict[str, Any]
    tz: ZoneInfo
    now: datetime
    severities: tuple[str, ...]
    open_action: OpenActionInfo | None
    resolved_at: datetime | None


def case_input(case: EvalCase) -> CaseInput:
    tz = ZoneInfo(case.store.timezone)
    now = datetime.fromisoformat(case.store.now_local).replace(tzinfo=tz)
    hours = {day: [list(case.store.opening_hours)] for day in _DAYS}
    criticality = case.main_ci.business_criticality if case.main_ci else None
    main_ci = None
    if case.main_ci is not None:
        where = f" ({case.main_ci.location})" if case.main_ci.location else ""
        friendly = (
            case.main_ci.name
            if case.main_ci.kind == "store device"
            else (f"{case.main_ci.type}{where}")
        )
        main_ci = {
            "kind": case.main_ci.kind,
            "type": case.main_ci.type,
            "store_friendly_name": friendly,
            "location": case.main_ci.location,
            "business_criticality": criticality,
        }
    incident: dict[str, Any] = {
        "number": case.incident.number,
        "title": case.incident.title,
        "description": case.incident.description,
        "symptom": case.incident.symptom,
        "category": case.incident.category,
        "priority": case.incident.priority,
        "state": state_label(case.incident.state),
        "on_hold_reason": case.incident.hold_reason,
    }
    common = {
        "incident": incident,
        "main_ci": main_ci,
        "store": store_facts(case.store.name, tz, hours, now),
        "assignment_group": {"name": "Store IT Support", "description": "Store IT support."},
        "comments": [],
    }
    severities = allowed_severities(case.incident.priority, criticality)
    if case.task == Task.ASSESS_IMPACT:
        context = assemble_context(task=case.task, severities=severities, **common)
        return CaseInput(context, tz, now, severities, None, None)

    incident["expected_fix_local"] = None
    incident["status_note"] = case.incident.eta_note
    open_info = waiting = None
    if case.open_action:
        created = now - timedelta(hours=1)
        open_info = OpenActionInfo(title=case.open_action, created_at=created)
        waiting = {
            "title": case.open_action,
            "expected_response": "acknowledge",
            "waiting_since_local": local_text(created, tz),
        }
    resolved_at = now - timedelta(minutes=10) if case.task == Task.CONFIRM_FIX else None
    trigger = {
        "type": TRIGGER_TYPES[case.task],
        "text": case.trigger or "",
        "written_at_local": local_text(now - timedelta(minutes=5), tz),
    }
    context = assemble_context(
        task=case.task, trigger=trigger, open_action=waiting, recent_answers=[], visits=[], **common
    )
    return CaseInput(context, tz, now, severities, open_info, resolved_at)


def describe(expect: Expect) -> str:
    parts = []
    if expect.decision:
        parts.append("|".join(expect.decision))
    if expect.expected_response:
        parts.append("|".join(expect.expected_response))
    if expect.due_local:
        parts.append(f"due {expect.due_local}")
    if expect.severity_in:
        parts.append("|".join(expect.severity_in))
    return " ".join(parts) or "-"


def _contains(texts: list[str | None], words: list[str]) -> str | None:
    joined = " ".join(t for t in texts if t).lower()
    return next((w for w in words if w.lower() in joined), None)


async def run_case(
    provider: LlmProvider, prompts: dict[str, PromptVersion], case: EvalCase
) -> CaseResult:
    data = case_input(case)
    impact = case.task == Task.ASSESS_IMPACT
    workflow = (AiWorkflow.IMPACT_ASSESSMENT if impact else AiWorkflow.UPDATE_CLASSIFICATION).value
    expected = describe(case.expect)
    try:
        result = await provider.generate(build_request(prompts[workflow], data.context))
    except LlmInvalidOutput as exc:
        return CaseResult(case.name, expected, "invalid answer", False, error=exc.message)
    except LlmError as exc:
        return CaseResult(case.name, expected, "LLM error", False, error=str(exc))

    try:
        if impact:
            decision = check_alert(result.output, data.severities)
            got = decision.alert.severity
            ok = case.expect.severity_in is None or got in case.expect.severity_in
            leaked = _contains([decision.alert.title, decision.alert.detail],
                               case.expect.must_not_contain)  # fmt: skip
            adjustments = decision.adjustments
        else:
            plan = check_classification(
                result.output,
                task=case.task,
                entry_text=case.trigger or "",
                tz=data.tz,
                now=data.now,
                open_action=data.open_action,
                resolved_at=data.resolved_at,
            )
            got = plan.decision
            ok = case.expect.decision is None or plan.decision in case.expect.decision
            if plan.decision == "action":
                got += f" {plan.expected_response}"
                if case.expect.expected_response:
                    ok = ok and plan.expected_response in case.expect.expected_response
                due = plan.due_by.astimezone(data.tz).strftime("%H:%M") if plan.due_by else "none"
                got += f" due {due}"
                if case.expect.due_local:
                    ok = ok and due == case.expect.due_local
            leaked = _contains([plan.title, plan.detail, plan.message],
                               case.expect.must_not_contain)  # fmt: skip
            adjustments = plan.adjustments
    except LlmInvalidOutput as exc:
        return CaseResult(case.name, expected, "rejected by guards", False, error=exc.message)
    if leaked:
        return CaseResult(case.name, expected, got, False, adjustments, f"text contains {leaked!r}")
    return CaseResult(case.name, expected, got, ok, adjustments)
