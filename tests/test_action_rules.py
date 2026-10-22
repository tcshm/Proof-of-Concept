# tests/test_action_rules.py
"""Reply validation and the incident comment text. No database."""

from __future__ import annotations

import pytest

from app.core.errors import BusinessRuleError
from app.services.action_rules import MAX_REPLY_TEXT, ValidReply, journal_text, validate_reply


@pytest.mark.parametrize(
    ("expected", "answer", "text", "result"),
    [
        ("yes_no", True, None, ValidReply(True, None)),
        ("yes_no", False, "  Still  jamming. ", ValidReply(False, "Still jamming.")),
        ("acknowledge", None, None, ValidReply(None, None)),
        (
            "acknowledge",
            None,
            "Comms room open at 1 PM",
            ValidReply(None, "Comms room open at 1 PM"),
        ),
        ("acknowledge", None, "   ", ValidReply(None, None)),
        ("free_text", None, "Cable was unplugged.", ValidReply(None, "Cable was unplugged.")),
    ],
)
def test_valid_replies(expected, answer, text, result) -> None:
    assert validate_reply(expected, answer, text) == result


@pytest.mark.parametrize(
    ("expected", "answer", "text", "message"),
    [
        ("yes_no", None, "yes", "needs a yes or no answer"),
        ("acknowledge", True, None, "does not take a yes or no answer"),
        ("free_text", False, "x", "does not take a yes or no answer"),
        ("free_text", None, None, "needs a written answer"),
        ("free_text", None, " \n ", "needs a written answer"),
        ("yes_no", True, "x" * (MAX_REPLY_TEXT + 1), "at most 1000 characters"),
    ],
)
def test_invalid_replies(expected, answer, text, message) -> None:
    with pytest.raises(BusinessRuleError, match=message):
        validate_reply(expected, answer, text)


def test_journal_text() -> None:
    title = "Is Printer 02 printing correctly now?"
    assert journal_text(title, "yes_no", ValidReply(True, "Receipts print fine.")) == (
        'Store reply to "Is Printer 02 printing correctly now?": Yes. Receipts print fine.'
    )
    assert journal_text(title, "yes_no", ValidReply(False, None)) == (
        'Store reply to "Is Printer 02 printing correctly now?": No.'
    )
    assert journal_text("Open the comms room", "acknowledge", ValidReply(None, None)) == (
        'Store reply to "Open the comms room": Acknowledged.'
    )
    assert journal_text("Check Register 04", "free_text", ValidReply(None, "Cable loose.")) == (
        'Store reply to "Check Register 04": Cable loose.'
    )
