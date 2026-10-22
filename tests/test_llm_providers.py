# tests/test_llm_providers.py
"""Providers, presets, factory, fake and fallback. No network: the OpenAI client
talks to an in-memory transport."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.config import Settings
from app.llm.factory import build_fallback_only, build_provider
from app.llm.fake import FakeProvider, sample_from_schema
from app.llm.fallback import FallbackProvider
from app.llm.providers import OpenAICompatProvider, model_extras
from app.llm.types import (
    LlmConfigError,
    LlmInvalidOutput,
    LlmRejected,
    LlmRequest,
    LlmUnavailable,
)

try:  # the HTTP library used by the installed openai package
    import httpx2 as hx
except ImportError:  # pragma: no cover
    import httpx as hx

KEY = "nvapi-SECRET-KEY-123"
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["severity", "title"],
    "properties": {
        "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
        "title": {"type": "string", "maxLength": 80},
    },
}
REQUEST = LlmRequest(
    system_prompt="You write store alerts.",
    user_prompt="Primary link down.",
    output_schema=SCHEMA,
    temperature=0.2,
)
GOOD = '{"severity": "warning", "title": "Primary internet link down"}'


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@localhost/db", **values)


def completion(content: str | None = GOOD, finish: str = "stop", model: str = "m") -> dict:
    return {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 18, "total_tokens": 138},
    }


class Recorder:
    """An in-memory HTTP server: records requests, returns scripted responses."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    def __call__(self, request):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item if isinstance(item, tuple) else (200, item)
        return hx.Response(status, json=body)

    def client(self):
        return hx.AsyncClient(transport=hx.MockTransport(self))

    def body(self, index: int = 0) -> dict:
        return json.loads(self.requests[index].content)


def provider(recorder: Recorder, **values: Any):
    values = {"llm_provider": "nvidia", "llm_api_key": KEY, **values}
    return build_provider(settings(**values), http_client=recorder.client())


# --- presets -------------------------------------------------------------------------------
async def test_nvidia_request() -> None:
    recorder = Recorder(completion(model="nvidia/nemotron-3.5-lightning-30b-a3b"))
    result = await provider(recorder).generate(REQUEST)

    sent = recorder.requests[0]
    assert str(sent.url) == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    body = recorder.body()
    assert body["model"] == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert body["temperature"] == 0.2 and body["max_tokens"] == 2048
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "nvext" not in body
    system, user = body["messages"]
    assert system["role"] == "system" and system["content"].startswith("You write store alerts.")
    assert "must match this JSON schema" in system["content"]
    assert user == {"role": "user", "content": "Primary link down."}

    assert result.output == {"severity": "warning", "title": "Primary internet link down"}
    assert (result.provider, result.prompt_tokens, result.completion_tokens) == ("nvidia", 120, 18)
    assert result.model_label == "nvidia:nvidia/nemotron-3.5-lightning-30b-a3b"


async def test_nvidia_gpt_oss() -> None:
    recorder = Recorder(completion())
    await provider(recorder, llm_model="openai/gpt-oss-20b").generate(REQUEST)
    body = recorder.body()
    assert body["model"] == "openai/gpt-oss-20b"
    assert body["reasoning_effort"] == "low"
    assert "chat_template_kwargs" not in body


async def test_groq_request() -> None:
    recorder = Recorder(completion())
    await provider(recorder, llm_provider="groq", llm_api_key="gsk_x").generate(REQUEST)
    sent = recorder.requests[0]
    assert str(sent.url) == "https://api.groq.com/openai/v1/chat/completions"
    body = recorder.body()
    assert body["model"] == "openai/gpt-oss-20b"
    assert body["response_format"] == {"type": "json_object"}
    assert "nvext" not in body and "chat_template_kwargs" not in body


async def test_foundry_request() -> None:
    recorder = Recorder(completion())
    await provider(
        recorder,
        llm_provider="foundry",
        llm_api_key="azure-key",
        llm_model="ric-gpt-4o-mini",
        llm_base_url="https://ric-ai.openai.azure.com/openai/v1/",
    ).generate(REQUEST)
    sent = recorder.requests[0]
    assert str(sent.url) == "https://ric-ai.openai.azure.com/openai/v1/chat/completions"
    assert sent.headers["api-key"] == "azure-key"
    body = recorder.body()
    assert body["model"] == "ric-gpt-4o-mini"
    assert body["max_completion_tokens"] == 2048 and "max_tokens" not in body
    assert body["response_format"] == {"type": "json_object"}


async def test_structured_output_and_extra_body_overrides() -> None:
    recorder = Recorder(completion())
    await provider(
        recorder,
        llm_structured_output="json_schema",
        llm_extra_body={"chat_template_kwargs": {"enable_thinking": True}, "top_p": 0.9},
        llm_max_output_tokens=512,
    ).generate(REQUEST)
    body = recorder.body()
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert "nvext" not in body
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert (body["top_p"], body["max_tokens"]) == (0.9, 512)

    recorder = Recorder(completion())
    await provider(recorder, llm_structured_output="none").generate(REQUEST)
    assert "nvext" not in recorder.body() and "response_format" not in recorder.body()


def test_model_extras() -> None:
    assert model_extras("nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b") == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert model_extras("groq", "openai/gpt-oss-20b") == {"reasoning_effort": "low"}
    assert model_extras("foundry", "gpt-4o-mini") == {}


# --- errors ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (429, LlmUnavailable),
        (500, LlmUnavailable),
        (503, LlmUnavailable),
        (400, LlmRejected),
        (401, LlmRejected),
        (403, LlmRejected),
        (404, LlmRejected),
    ],
)
async def test_http_errors(status: int, error: type) -> None:
    recorder = Recorder((status, {"error": {"message": f"problem {status}"}}))
    with pytest.raises(error) as raised:
        await provider(recorder).generate(REQUEST)
    assert f"HTTP {status}" in str(raised.value)
    assert KEY not in str(raised.value) and KEY not in repr(raised.value)
    assert len(recorder.requests) == 1  # no hidden retries


async def test_timeout_and_connection_errors() -> None:
    timeout = Recorder(hx.ReadTimeout("slow"))
    with pytest.raises(LlmUnavailable, match="no answer within 120 s"):
        await provider(timeout).generate(REQUEST)
    down = Recorder(hx.ConnectError("refused"))
    with pytest.raises(LlmUnavailable, match="could not connect"):
        await provider(down).generate(REQUEST)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (completion(content=None), "empty answer"),
        (completion(content="   "), "empty answer"),
        (completion(content='{"severity": "warn', finish="length"), "cut off at the output"),
        (completion(content='{"severity": "urgent", "title": "x"}'), "does not match the schema"),
        (completion(content="Sorry, I cannot help."), "no JSON object"),
    ],
)
async def test_invalid_answers(body: dict, message: str) -> None:
    with pytest.raises(LlmInvalidOutput, match=message) as raised:
        await provider(Recorder(body)).generate(REQUEST)
    assert raised.value.provider == "nvidia"


async def test_reasoning_text_in_answer_is_ignored() -> None:
    recorder = Recorder(completion(content=f"<think>thinking...</think>\n```json\n{GOOD}\n```"))
    result = await provider(recorder).generate(REQUEST)
    assert result.output["severity"] == "warning"


# --- factory --------------------------------------------------------------------------------
def test_factory_configuration_errors() -> None:
    with pytest.raises(LlmConfigError, match="API key for the nvidia"):
        build_provider(settings(llm_provider="nvidia"))
    with pytest.raises(LlmConfigError, match="model .deployment name. for the foundry"):
        build_provider(settings(llm_provider="foundry", llm_api_key="k"))
    with pytest.raises(LlmConfigError, match="base URL for the foundry"):
        build_provider(settings(llm_provider="foundry", llm_api_key="k", llm_model="dep"))
    with pytest.raises(LlmConfigError, match="LLM_FALLBACK_PROVIDER is not set"):
        build_fallback_only(settings())


def test_factory_defaults() -> None:
    assert isinstance(build_provider(settings()), FakeProvider)
    nvidia = build_provider(settings(llm_provider="nvidia", llm_api_key=KEY))
    assert isinstance(nvidia, OpenAICompatProvider)
    assert (nvidia.name, nvidia.model) == ("nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b")
    assert nvidia.config.timeout_seconds == 120
    blank = build_provider(settings(llm_provider="nvidia", llm_api_key=KEY, llm_model=" "))
    assert blank.model == "nvidia/nemotron-3.5-lightning-30b-a3b"


def test_factory_fallback_reuses_the_key_for_the_same_provider() -> None:
    combined = build_provider(
        settings(
            llm_provider="nvidia",
            llm_api_key=KEY,
            llm_fallback_provider="nvidia",
            llm_fallback_model="openai/gpt-oss-20b",
        )
    )
    assert isinstance(combined, FallbackProvider)
    assert combined.secondary.model == "openai/gpt-oss-20b"
    assert combined.secondary.config.api_key == KEY
    with pytest.raises(LlmConfigError, match="API key for the groq"):
        build_provider(
            settings(llm_provider="nvidia", llm_api_key=KEY, llm_fallback_provider="groq")
        )


def test_settings_validation() -> None:
    with pytest.raises(ValueError):
        settings(llm_timeout_seconds=1)
    with pytest.raises(ValueError):
        settings(llm_provider="openrouter")
    assert KEY not in repr(settings(llm_api_key=KEY))


# --- fake and fallback ----------------------------------------------------------------------
def test_sample_from_schema() -> None:
    schema = {
        "type": "object",
        "required": ["decision", "title", "due", "count", "flag"],
        "properties": {
            "decision": {"type": "string", "enum": ["action", "notify", "none"]},
            "title": {"type": "string", "maxLength": 5},
            "due": {"type": ["string", "null"]},
            "count": {"type": "integer", "minimum": 1},
            "flag": {"type": "boolean"},
        },
    }
    assert sample_from_schema(schema) == {
        "decision": "action",
        "title": "Sampl",
        "due": None,
        "count": 1,
        "flag": False,
    }


async def test_fake_provider() -> None:
    default = await FakeProvider().generate(REQUEST)
    assert default.output == {"severity": "critical", "title": "Sample title"}

    fake = FakeProvider([{"severity": "info", "title": "ok"}, "not json", LlmUnavailable("429")])
    assert (await fake.generate(REQUEST)).output == {"severity": "info", "title": "ok"}
    with pytest.raises(LlmInvalidOutput):
        await fake.generate(REQUEST)
    with pytest.raises(LlmUnavailable) as raised:
        await fake.generate(REQUEST)
    assert raised.value.provider == "fake"
    assert len(fake.requests) == 3


async def test_fallback_only_on_unavailable() -> None:
    secondary = FakeProvider([{"severity": "info", "title": "from backup"}], name="groq")
    fallback = FallbackProvider(FakeProvider([LlmUnavailable("HTTP 429")]), secondary)
    result = await fallback.generate(REQUEST)
    assert (result.provider, result.output["title"]) == ("groq", "from backup")

    for error in (LlmRejected("HTTP 401"), LlmInvalidOutput("bad")):
        untouched = FakeProvider([{"severity": "info", "title": "x"}], name="groq")
        with pytest.raises(type(error)):
            await FallbackProvider(FakeProvider([error]), untouched).generate(REQUEST)
        assert untouched.requests == []
