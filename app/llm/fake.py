# app/llm/fake.py
"""A provider that never calls the network.

    FakeProvider()                         answers with a sample object built from
                                           the request's schema (key-less demos)
    FakeProvider(responses=[...])          answers from a script, in order:
        dict                -> that JSON object (validated against the schema)
        str                 -> that raw text (parsed and validated like a real answer)
        LlmError instance   -> raised (simulate timeouts, rate limits, bad keys)

Every request is kept in .requests so tests can check what was sent.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable
from typing import Any

from app.llm.types import LlmError, LlmInvalidOutput, LlmRequest, LlmResult
from app.llm.validation import parse_output

FAKE_MODEL = "fake-model"


def sample_from_schema(schema: dict[str, Any], name: str = "value") -> Any:
    """A small value that satisfies simple schemas (objects, enums, strings, numbers)."""
    kind = schema.get("type")
    if isinstance(kind, list):
        if "null" in kind:
            return None
        kind = kind[0]
    if "enum" in schema:
        return schema["enum"][0]
    if kind == "object":
        properties = schema.get("properties", {})
        return {
            key: sample_from_schema(properties.get(key, {}), key)
            for key in schema.get("required", [])
        }
    if kind == "string":
        text = f"Sample {name.replace('_', ' ')}"
        return text[: schema.get("maxLength", len(text))]
    if kind in ("integer", "number"):
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    return None


class FakeProvider:
    def __init__(
        self,
        responses: Iterable[dict[str, Any] | str | LlmError] | None = None,
        *,
        name: str = "fake",
        model: str = FAKE_MODEL,
    ) -> None:
        self.name = name
        self.model = model
        self.scripted = responses is not None
        self._responses: deque[dict[str, Any] | str | LlmError] = deque(responses or [])
        self.requests: list[LlmRequest] = []

    async def generate(self, request: LlmRequest) -> LlmResult:
        self.requests.append(request)
        if self.scripted:
            if not self._responses:
                raise AssertionError("FakeProvider ran out of scripted responses")
            item = self._responses.popleft()
        else:
            item = sample_from_schema(request.output_schema, "answer")
        if isinstance(item, LlmError):
            item.provider = item.provider or self.name
            item.model = item.model or self.model
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        try:
            output = parse_output(text, request.output_schema)
        except LlmInvalidOutput as exc:
            raise LlmInvalidOutput(
                exc.message, provider=self.name, model=self.model, raw_text=text
            ) from exc
        return LlmResult(
            output=output,
            text=text,
            provider=self.name,
            model=self.model,
            prompt_tokens=len(request.system_prompt + request.user_prompt) // 4,
            completion_tokens=len(text) // 4,
            latency_ms=1,
            finish_reason="stop",
        )

    async def aclose(self) -> None:
        return None
