# app/services/action_rules.py
"""Pure rules for a store's reply to an action (no database).

    validate_reply  check the reply against the action's expected_response
    journal_text    the store comment written on the incident for IT

The reply is only recorded: it is never classified or sent to the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import BusinessRuleError
from app.models.values import ExpectedResponse

MAX_REPLY_TEXT = 1000


@dataclass(frozen=True)
class ValidReply:
    answer_yes_no: bool | None
    response_text: str | None


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = " ".join(text.split())
    return cleaned or None


def validate_reply(
    expected_response: str, answer_yes_no: bool | None, response_text: str | None
) -> ValidReply:
    """acknowledge: optional note, no yes/no.
    yes_no:      answerYesNo (true / false) required, optional note.
    free_text:   responseText required, no yes/no."""
    text = _clean(response_text)
    if text is not None and len(text) > MAX_REPLY_TEXT:
        raise BusinessRuleError(f"The reply must be at most {MAX_REPLY_TEXT} characters.")
    expected = ExpectedResponse(expected_response)
    if expected == ExpectedResponse.YES_NO:
        if not isinstance(answer_yes_no, bool):
            raise BusinessRuleError("This action needs a yes or no answer (answerYesNo).")
        return ValidReply(answer_yes_no=answer_yes_no, response_text=text)
    if answer_yes_no is not None:
        raise BusinessRuleError("This action does not take a yes or no answer.")
    if expected == ExpectedResponse.FREE_TEXT and text is None:
        raise BusinessRuleError("This action needs a written answer (responseText).")
    return ValidReply(answer_yes_no=None, response_text=text)


def journal_text(action_title: str, expected_response: str, reply: ValidReply) -> str:
    """'Store reply to "Is Printer 02 printing correctly now?": Yes. Receipts print fine.'"""
    prefix = f'Store reply to "{action_title}":'
    expected = ExpectedResponse(expected_response)
    if expected == ExpectedResponse.YES_NO:
        answer = "Yes." if reply.answer_yes_no else "No."
    elif expected == ExpectedResponse.ACKNOWLEDGE:
        answer = "Acknowledged."
    else:
        answer = ""
    parts = [part for part in (answer, reply.response_text) if part]
    return f"{prefix} {' '.join(parts)}"
