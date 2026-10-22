# app/llm/validation.py
"""Turn the model's text into a JSON object that matches the output schema.

Two layers:
    1. parse   strip <think>...</think> blocks and ``` code fences, cut any text
               around the outermost {...}, then json.loads
    2. check   validate against the prompt version's JSON schema (required
               fields, allowed values, lengths, no extra fields)
Any failure raises LlmInvalidOutput with the raw text kept for the run record.
Business checks (e.g. "due time must be in the future") belong to each workflow.
"""

from __future__ import annotations

import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.llm.types import LlmConfigError, LlmInvalidOutput

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*(.*?)\s*```$", re.DOTALL)
MAX_REPORTED_ERRORS = 5


def check_schema(schema: dict[str, Any]) -> None:
    """Raise LlmConfigError if the output schema itself is not a valid JSON schema."""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise LlmConfigError(f"invalid output schema: {exc.message}") from exc


def extract_json_text(text: str | None) -> str:
    cleaned = _THINK.sub("", text or "").strip()
    if cleaned.lower().startswith("<think>"):
        raise LlmInvalidOutput("the answer contains only unfinished reasoning", raw_text=text)
    fenced = _FENCE.match(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    if not cleaned.startswith("{"):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise LlmInvalidOutput("the answer contains no JSON object", raw_text=text)
        cleaned = cleaned[start : end + 1]
    return cleaned


def schema_errors(output: Any, schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(output), key=lambda e: list(e.absolute_path))
    messages = []
    for error in errors[:MAX_REPORTED_ERRORS]:
        path = ".".join(str(part) for part in error.absolute_path) or "(root)"
        messages.append(f"{path}: {error.message}")
    return messages


def parse_output(text: str | None, schema: dict[str, Any]) -> dict[str, Any]:
    """The validated JSON object, or LlmInvalidOutput."""
    candidate = extract_json_text(text)
    try:
        output = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LlmInvalidOutput(f"the answer is not valid JSON: {exc.msg}", raw_text=text) from exc
    if not isinstance(output, dict):
        raise LlmInvalidOutput("the answer is not a JSON object", raw_text=text)
    problems = schema_errors(output, schema)
    if problems:
        raise LlmInvalidOutput(
            "the answer does not match the schema: " + "; ".join(problems), raw_text=text
        )
    return output


def schema_instructions(schema: dict[str, Any]) -> str:
    """Appended to every system prompt, so even providers without schema-constrained
    decoding know the exact shape to return."""
    return (
        "Respond with a single JSON object and nothing else (no markdown, no explanation). "
        "It must match this JSON schema:\n" + json.dumps(schema, indent=2, ensure_ascii=False)
    )
