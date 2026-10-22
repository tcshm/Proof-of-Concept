# app/llm/types.py
"""Requests, results and errors shared by every provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class LlmRequest:
    """One structured call: the model must answer with JSON matching output_schema."""

    system_prompt: str
    user_prompt: str
    output_schema: dict[str, Any]
    temperature: float = 0.0
    max_output_tokens: int | None = None  # None: the provider's configured limit


@dataclass(frozen=True)
class LlmResult:
    """A validated answer. output always matches the request's output_schema."""

    output: dict[str, Any]
    text: str
    provider: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    finish_reason: str | None = None

    @property
    def model_label(self) -> str:
        """'nvidia:nvidia/nemotron-3.5-lightning-30b-a3b' (stored in ai_workflow_runs)."""
        return f"{self.provider}:{self.model}"


class LlmProvider(Protocol):
    """What the worker depends on. Implementations: OpenAICompatProvider,
    FakeProvider, FallbackProvider."""

    name: str
    model: str

    async def generate(self, request: LlmRequest) -> LlmResult: ...

    async def aclose(self) -> None: ...


@dataclass(eq=False)
class LlmError(Exception):
    """Base class. message never contains an API key."""

    message: str
    provider: str = ""
    model: str = ""
    raw_text: str | None = field(default=None, repr=False)

    def __str__(self) -> str:
        where = f"{self.provider}:{self.model} " if self.provider else ""
        return f"{where}{self.message}".strip()


class LlmUnavailable(LlmError):
    """Timeout, network error, rate limit (429) or provider error (5xx): retry later."""


class LlmRejected(LlmError):
    """Bad key, unknown model, bad request (4xx): fix the configuration, do not retry."""


class LlmInvalidOutput(LlmError):
    """The model answered, but not with JSON matching the schema."""


class LlmConfigError(ValueError):
    """The LLM settings in .env are incomplete or inconsistent."""
