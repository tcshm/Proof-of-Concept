# tests/test_llm_validation.py
"""Parsing and schema validation of model answers. No network, no database."""

from __future__ import annotations

import pytest

from app.llm.types import LlmConfigError, LlmInvalidOutput
from app.llm.validation import check_schema, extract_json_text, parse_output, schema_errors

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["severity", "title"],
    "properties": {
        "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
        "title": {"type": "string", "maxLength": 20},
        "due_by_local": {"type": ["string", "null"]},
    },
}


@pytest.mark.parametrize(
    "text",
    [
        '{"severity": "warning", "title": "Primary link down"}',
        '```json\n{"severity": "warning", "title": "Primary link down"}\n```',
        '```\n{"severity": "warning", "title": "Primary link down"}\n```',
        'Here is the alert:\n{"severity": "warning", "title": "Primary link down"}\nThanks!',
        '<think>The link is down, so warning.</think>\n{"severity": "warning", '
        '"title": "Primary link down"}',
    ],
)
def test_accepted_answers(text: str) -> None:
    assert parse_output(text, SCHEMA) == {"severity": "warning", "title": "Primary link down"}


def test_nullable_field() -> None:
    output = parse_output('{"severity": "info", "title": "x", "due_by_local": null}', SCHEMA)
    assert output["due_by_local"] is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "no JSON object"),
        ("The link is down.", "no JSON object"),
        ('{"severity": "warning", "title": }', "not valid JSON"),
        ("[1, 2]", "no JSON object"),
        ("<think>still thinking", "unfinished reasoning"),
        ('{"title": "Primary link down"}', "'severity' is a required property"),
        ('{"severity": "urgent", "title": "x"}', "severity: 'urgent' is not one of"),
        ('{"severity": "info", "title": "' + "x" * 30 + '"}', "title: .* is too long"),
        ('{"severity": "info", "title": "x", "extra": 1}', "Additional properties"),
    ],
)
def test_rejected_answers(text: str, message: str) -> None:
    with pytest.raises(LlmInvalidOutput, match=message) as error:
        parse_output(text, SCHEMA)
    assert error.value.raw_text == text


def test_extract_keeps_plain_json() -> None:
    assert extract_json_text('  {"a": 1}  ') == '{"a": 1}'


def test_schema_errors_are_limited_and_ordered() -> None:
    problems = schema_errors({"severity": 1, "title": 2, "x": 3, "y": 4}, SCHEMA)
    assert 1 <= len(problems) <= 5


def test_invalid_schema_is_a_config_error() -> None:
    with pytest.raises(LlmConfigError, match="invalid output schema"):
        check_schema({"type": "objectt"})
