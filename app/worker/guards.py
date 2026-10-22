# app/worker/guards.py
"""Checks applied to every model answer before anything reaches the store.

Prompts guide the model; these guards guarantee the rules (pure functions, no
database). Every change a guard makes is returned as an "adjustment" and stored
on the AI run (ai_workflow_runs.output: answer / applied / adjustments).

Update classification (three gates, in this order):
    1. request      action ONLY when IT's update explicitly asks the store for
                    something: the model must quote those words
                    (store_request_quote); the quote must be in the update and
                    read as a request to the store. Otherwise -> notify.
    2. information  notify: news for the store (progress, visit, workaround).
    3. nothing      none: internal detail. A comment is never none.
Plus: at most ONE open action per incident (a second request -> notify, held
back for IT); a resolution always asks a yes/no "is it fixed" question; a due
time needs a quoted clock time from the update; no internal details in text.

Impact assessment: severity within the range allowed by the incident priority
and the main CI's business criticality; no clock times or internal details.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.llm.types import LlmInvalidOutput
from app.models.values import ExpectedResponse, Severity
from app.worker.rules import MAX_DETAIL, MAX_TITLE, AlertText, Task, clip, parse_due_by_local

DECISIONS = ("action", "notify", "none")
_EXPECTED = {e.value for e in ExpectedResponse}
_LEVELS = (Severity.INFO.value, Severity.WARNING.value, Severity.CRITICAL.value)
MIN_QUOTE_WORDS = 3

# --- request barrier --------------------------------------------------------------------
_EXCLUDED = re.compile(
    r"\b(trying to (reach|contact|call)|will (contact|call|reach out)|reaching out)\b"
)
_ADDRESSEE = re.compile(r"\b(store|stores|manager|managers|staff|you|your)\b")
_POLITE = re.compile(r"\b(please|kindly|can you|could you|would you)\b")
_ASK = re.compile(
    r"\b(must|should|need to|needs to|required to|requested to|check|confirm|provide|verify"
    r"|restart|reboot|power cycle|ensure|make sure|keep|give|allow|open|arrange|report|share"
    r"|send|reply|respond|unplug|reconnect|let us know|tell us|inform us)\b"
)
_CLOCK_TIME = re.compile(r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b|\b\d{1,2}:\d{2}\b", re.IGNORECASE)

# --- internal details -------------------------------------------------------------------
_INTERNAL_PATTERNS = (
    ("an IP address", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")),
    ("a MAC address", re.compile(r"\b[0-9a-f]{2}(?:[:-][0-9a-f]{2}){5}\b", re.IGNORECASE)),
    ("an email address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")),
    ("a ticket or circuit id", re.compile(r"\b[A-Za-z]{2,5}-\d{4,}\b")),
    ("a staff user name", re.compile(r"\b[a-z]\.[a-z]{2,}\b")),
)
_TOKEN = re.compile(r"[A-Za-z0-9-]{6,}")
_DATE_OR_TIME = re.compile(
    r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b|\b\d{1,2}:\d{2}\b|\b\d{4}-\d{2}-\d{2}\b", re.IGNORECASE
)


def normalize(text: str | None) -> str:
    """Lower case, every run of non-letters/digits becomes one space."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def quote_in_text(quote: str | None, text: str) -> bool:
    q = normalize(quote)
    return bool(q) and f" {q} " in f" {normalize(text)} "


def is_store_request(quote: str | None) -> bool:
    """Does the quoted text ask the store team to do or report something?"""
    q = normalize(quote)
    if not q or _EXCLUDED.search(q):
        return False
    return bool(_ASK.search(q)) and bool(_ADDRESSEE.search(q) or _POLITE.search(q))


def request_problem(quote: Any, entry_text: str) -> str | None:
    """None when the quote is a real request from the update; otherwise why not."""
    if not isinstance(quote, str) or not quote.strip():
        return "no store_request_quote"
    if len(normalize(quote).split()) < MIN_QUOTE_WORDS:
        return "store_request_quote is too short"
    if not quote_in_text(quote, entry_text):
        return "store_request_quote is not in the update"
    if not is_store_request(quote):
        return "store_request_quote does not ask the store to do anything"
    return None


def find_internal_detail(*texts: str | None) -> str | None:
    """The kind of internal detail found in store-facing text, or None."""
    for text in texts:
        if not text:
            continue
        for label, pattern in _INTERNAL_PATTERNS:
            if pattern.search(text):
                return label
        for token in _TOKEN.findall(text):
            if token.upper().startswith("INC"):
                continue
            digits = sum(ch.isdigit() for ch in token)
            letters = sum(ch.isalpha() for ch in token)
            if digits >= 5 and letters >= 2:
                return "a device code or serial number"
    return None


def has_clock_time(*texts: str | None) -> bool:
    return any(text and _DATE_OR_TIME.search(text) for text in texts)


def allowed_severities(priority: int | None, criticality: str | None) -> tuple[str, ...]:
    """Severities an alert may have for this incident (lowest first)."""
    low, high = (1, 2) if priority in (1, 2) else (0, 1)
    if criticality == "high" and priority == 3:
        high = 2
    if criticality == "low":
        high = min(high, 1)
    return _LEVELS[low : high + 1]


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _invalid(message: str, output: dict[str, Any]) -> LlmInvalidOutput:
    return LlmInvalidOutput(message, raw_text=json.dumps(output, ensure_ascii=False))


# --- impact assessment ------------------------------------------------------------------
@dataclass(frozen=True)
class AlertDecision:
    alert: AlertText
    adjustments: list[str] = field(default_factory=list)


def check_alert(output: dict[str, Any], allowed: tuple[str, ...]) -> AlertDecision:
    title, detail = _text(output.get("title")), _text(output.get("detail"))
    severity = output.get("severity")
    if not title or not detail:
        raise _invalid("the alert needs a title and a detail", output)
    if severity not in _LEVELS:
        raise _invalid(f"unknown severity {severity!r}", output)
    found = find_internal_detail(title, detail)
    if found:
        raise _invalid(f"the alert contains {found}", output)
    if has_clock_time(title, detail):
        raise _invalid("the alert mentions a time or date", output)

    adjustments: list[str] = []
    affected = output.get("affected")
    if isinstance(affected, list) and affected == ["no_visible_impact"] and severity != "info":
        adjustments.append(f"severity {severity} -> info: no visible impact")
        severity = "info"
    if allowed and severity not in allowed:
        index = _LEVELS.index(severity)
        lowest, highest = _LEVELS.index(allowed[0]), _LEVELS.index(allowed[-1])
        clamped = _LEVELS[min(max(index, lowest), highest)]
        adjustments.append(f"severity {severity} -> {clamped}: allowed {', '.join(allowed)}")
        severity = clamped
    alert = AlertText(clip(title, MAX_TITLE), clip(detail, MAX_DETAIL), severity)
    return AlertDecision(alert=alert, adjustments=adjustments)


# --- update classification --------------------------------------------------------------
@dataclass(frozen=True)
class OpenActionInfo:
    """The incident's action that is still waiting for the store (status 'open')."""

    title: str
    created_at: datetime


@dataclass
class ClassificationDecision:
    decision: str  # action, notify or none
    title: str | None = None
    detail: str | None = None
    severity: str = Severity.WARNING.value
    expected_response: str | None = None
    due_by: datetime | None = None
    message: str | None = None  # notification text for "notify"
    replace_open_action: bool = False  # confirm_fix replaces an older open action
    held_back: bool = False  # a request held back: an action was already open
    adjustments: list[str] = field(default_factory=list)

    def applied(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "title": self.title,
            "detail": self.detail,
            "severity": self.severity,
            "expected_response": self.expected_response,
            "due_by": self.due_by.isoformat() if self.due_by else None,
            "message": self.message,
            "replace_open_action": self.replace_open_action,
            "held_back": self.held_back,
        }


def check_classification(
    output: dict[str, Any],
    *,
    task: Task | str,
    entry_text: str,
    tz: ZoneInfo,
    now: datetime,
    open_action: OpenActionInfo | None,
    resolved_at: datetime | None,
) -> ClassificationDecision:
    task = Task(task)
    decision = output.get("decision")
    if decision not in DECISIONS:
        raise _invalid(f"unknown decision {decision!r}", output)
    severity = output.get("severity")
    if severity not in _LEVELS:
        severity = Severity.WARNING.value
    title, detail = _text(output.get("title")), _text(output.get("detail"))
    expected = output.get("expected_response")
    result = ClassificationDecision(decision=decision, severity=severity)
    adjust = result.adjustments.append

    # Task rules decided in code.
    if task == Task.CONFIRM_FIX:
        if decision != "action":
            adjust(f"confirm_fix: {decision} -> action (a resolution asks to confirm the fix)")
            decision = "action"
        if expected != ExpectedResponse.YES_NO:
            expected = ExpectedResponse.YES_NO.value
    elif task == Task.CLASSIFY_COMMENT and decision == "none":
        adjust("a comment is written to the store: none -> notify")
        decision = "notify"

    # Gate 1: an action only for an explicit request to the store.
    if decision == "action" and task != Task.CONFIRM_FIX:
        problem = request_problem(output.get("store_request_quote"), entry_text)
        if problem:
            adjust(f"request barrier: {problem}; action -> notify")
            decision = "notify"

    # At most one open action per incident.
    if decision == "action" and open_action is not None:
        older = resolved_at is None or open_action.created_at < resolved_at
        if task == Task.CONFIRM_FIX and older:
            result.replace_open_action = True
            adjust(f'replaces the open action "{open_action.title}" with the fix confirmation')
        else:
            result.held_back = True
            adjust(f'one open action per incident: "{open_action.title}" is still waiting; '
                   "action -> notify")  # fmt: skip
            decision = "notify"

    result.decision = decision
    if decision == "action":
        if not title or not detail:
            raise _invalid("an action needs a title and a detail", output)
        if expected not in _EXPECTED:
            raise _invalid("an action needs expected_response acknowledge, yes_no or free_text",
                           output)  # fmt: skip
        found = find_internal_detail(title, detail)
        if found:
            raise _invalid(f"the action contains {found}", output)
        result.title, result.detail = clip(title, MAX_TITLE), clip(detail, MAX_DETAIL)
        result.expected_response = str(expected)
        if task != Task.CONFIRM_FIX:
            result.due_by = _grounded_due(output, entry_text, tz, now, adjust)
        return result

    if decision == "notify":
        if task == Task.CLASSIFY_COMMENT:
            result.message = " ".join(entry_text.split())  # sent as IT wrote it
        else:
            if not detail:
                raise _invalid("a notification needs a detail", output)
            found = find_internal_detail(detail)
            if found:
                raise _invalid(f"the notification contains {found}", output)
            result.message = clip(detail, MAX_DETAIL)
    return result


def _grounded_due(output, entry_text, tz, now, adjust) -> datetime | None:
    value = output.get("due_by_local")
    if not isinstance(value, str) or not value.strip():
        return None
    quote = output.get("due_time_quote")
    if not (
        isinstance(quote, str) and _CLOCK_TIME.search(quote) and quote_in_text(quote, entry_text)
    ):
        adjust("due time dropped: due_time_quote with a clock time is not in the update")
        return None
    due = parse_due_by_local(value, tz, now)
    if due is None:
        adjust(f"due time dropped: {value!r} is not between now and 3 days ahead")
    return due


def answer_of(output: Any) -> dict[str, Any]:
    """The model's own answer from an ai_workflow_runs.output value."""
    if isinstance(output, dict) and isinstance(output.get("answer"), dict):
        return output["answer"]
    return output if isinstance(output, dict) else {}
