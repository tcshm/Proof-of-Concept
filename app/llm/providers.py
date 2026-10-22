# app/llm/providers.py
"""One provider class for every OpenAI-compatible chat-completions API.

    nvidia   NVIDIA NIM (build.nvidia.com)   https://integrate.api.nvidia.com/v1
    groq     Groq                            https://api.groq.com/openai/v1
    foundry  Azure AI Foundry / Azure OpenAI your resource's .../openai/v1/ endpoint

The differences between them are data (PRESETS), not code: the base URL, the
default model, how schema-constrained JSON is requested, and the name of the
output-token limit. Switching provider is a .env change.

Free endpoints can be slow, so the timeout is generous (LLM_TIMEOUT_SECONDS,
default 120 s) and the SDK's own retries are switched off: the worker retries
with its 1 / 5 / 15 minute backoff.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

import openai

from app.llm.types import (
    LlmConfigError,
    LlmInvalidOutput,
    LlmRejected,
    LlmRequest,
    LlmResult,
    LlmUnavailable,
)
from app.llm.validation import check_schema, parse_output, schema_instructions

logger = logging.getLogger("app.llm")

ProviderKind = Literal["nvidia", "groq", "foundry"]
StructuredMode = Literal["nvext", "json_schema", "json_object", "none"]
_ERROR_EXCERPT = 300


@dataclass(frozen=True)
class Preset:
    base_url: str | None
    default_model: str | None
    structured_output: StructuredMode
    token_param: Literal["max_tokens", "max_completion_tokens"]


PRESETS: dict[str, Preset] = {
    "nvidia": Preset(
        base_url="https://integrate.api.nvidia.com/v1",
        default_model="nvidia/nemotron-3.5-lightning-30b-a3b",
        structured_output="json_schema",
        token_param="max_tokens",
    ),
    "groq": Preset(
        base_url="https://api.groq.com/openai/v1",
        default_model="openai/gpt-oss-20b",
        structured_output="json_object",
        token_param="max_tokens",
    ),
    "foundry": Preset(
        base_url=None,  # required: https://<resource>.openai.azure.com/openai/v1/
        default_model=None,  # required: your deployment name
        structured_output="json_object",
        token_param="max_completion_tokens",
    ),
}


@dataclass(frozen=True)
class ProviderConfig:
    kind: ProviderKind
    model: str
    api_key: str
    base_url: str
    structured_output: StructuredMode
    timeout_seconds: float
    max_output_tokens: int
    extra_body: dict[str, Any]


def model_extras(kind: str, model: str) -> dict[str, Any]:
    """Request fields some models need for short structured answers.

    Nemotron on NVIDIA: switch thinking off, so no reasoning text lands in the answer.
    gpt-oss (any provider): keep reasoning effort low; its reasoning comes back in a
    separate field and only the answer is parsed.
    """
    lowered = model.lower()
    extras: dict[str, Any] = {}
    if kind == "nvidia" and "nemotron" in lowered:
        extras["chat_template_kwargs"] = {"enable_thinking": False}
    if "gpt-oss" in lowered:
        extras["reasoning_effort"] = "low"
    return extras


def _structured_fields(mode: StructuredMode, schema: dict[str, Any]) -> dict[str, Any]:
    if mode == "nvext":
        return {"nvext": {"guided_json": schema}}
    if mode == "json_schema":
        return {
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": schema, "strict": False},
            }
        }
    if mode == "json_object":
        return {"response_format": {"type": "json_object"}}
    return {}


class OpenAICompatProvider:
    def __init__(self, config: ProviderConfig, *, http_client: Any | None = None) -> None:
        if not config.api_key:
            raise LlmConfigError(f"LLM provider {config.kind!r} needs an API key")
        self.config = config
        self.name = config.kind
        self.model = config.model
        headers = {"api-key": config.api_key} if config.kind == "foundry" else None
        self._client = openai.AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
            default_headers=headers,
            http_client=http_client,
        )

    def build_payload(self, request: LlmRequest) -> dict[str, Any]:
        """The keyword arguments for chat.completions.create (exposed for tests)."""
        check_schema(request.output_schema)
        config = self.config
        preset = PRESETS[config.kind]
        extra_body = {
            **model_extras(config.kind, config.model),
            **_structured_fields(config.structured_output, request.output_schema),
            **config.extra_body,
        }
        system = f"{request.system_prompt.rstrip()}\n\n{schema_instructions(request.output_schema)}"
        payload: dict[str, Any] = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": request.user_prompt},
            ],
            "temperature": request.temperature,
            preset.token_param: request.max_output_tokens or config.max_output_tokens,
        }
        if extra_body:
            payload["extra_body"] = extra_body
        return payload

    def _error(self, cls: type, message: str, raw_text: str | None = None):
        return cls(message, provider=self.name, model=self.model, raw_text=raw_text)

    async def generate(self, request: LlmRequest) -> LlmResult:
        payload = self.build_payload(request)
        started = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(**payload)
        except openai.APITimeoutError as exc:
            raise self._error(
                LlmUnavailable, f"no answer within {self.config.timeout_seconds:g} s"
            ) from exc
        except openai.APIConnectionError as exc:
            raise self._error(LlmUnavailable, "could not connect to the provider") from exc
        except openai.APIStatusError as exc:
            status = exc.status_code
            detail = str(getattr(exc, "message", "") or "")[:_ERROR_EXCERPT]
            message = f"HTTP {status}: {detail}".rstrip(": ")
            if status == 429 or status >= 500:
                raise self._error(LlmUnavailable, message) from exc
            raise self._error(LlmRejected, message) from exc
        except openai.OpenAIError as exc:
            raise self._error(LlmUnavailable, exc.__class__.__name__) from exc
        latency_ms = round((time.perf_counter() - started) * 1000)

        if not response.choices:
            raise self._error(LlmInvalidOutput, "the provider returned no choices")
        choice = response.choices[0]
        text = choice.message.content or ""
        if choice.finish_reason == "length":
            raise self._error(
                LlmInvalidOutput,
                "the answer was cut off at the output token limit (raise LLM_MAX_OUTPUT_TOKENS)",
                raw_text=text,
            )
        if not text.strip():
            raise self._error(LlmInvalidOutput, "the provider returned an empty answer")
        try:
            output = parse_output(text, request.output_schema)
        except LlmInvalidOutput as exc:
            raise self._error(LlmInvalidOutput, exc.message, raw_text=text) from exc

        usage = response.usage
        result = LlmResult(
            output=output,
            text=text,
            provider=self.name,
            model=response.model or self.model,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            latency_ms=latency_ms,
            finish_reason=choice.finish_reason,
        )
        logger.info(
            "llm call succeeded",
            extra={
                "provider": result.provider,
                "model": result.model,
                "latencyMs": latency_ms,
                "promptTokens": result.prompt_tokens,
                "completionTokens": result.completion_tokens,
            },
        )
        return result

    async def aclose(self) -> None:
        await self._client.close()
