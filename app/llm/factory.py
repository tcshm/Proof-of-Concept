# app/llm/factory.py
"""Build the configured LLM provider from settings.

Switching provider is a .env change only:

    LLM_PROVIDER=nvidia   LLM_API_KEY=nvapi-...                     (model: nemotron default)
    LLM_PROVIDER=groq     LLM_API_KEY=gsk_...                       (model: gpt-oss-20b default)
    LLM_PROVIDER=foundry  LLM_API_KEY=...  LLM_MODEL=<deployment>
                          LLM_BASE_URL=https://<resource>.openai.azure.com/openai/v1/
    LLM_PROVIDER=fake                                               (no network, no key)

Add LLM_FALLBACK_PROVIDER (+ _MODEL / _API_KEY / _BASE_URL) for a second provider
that is used when the first is unavailable.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.llm.fake import FakeProvider
from app.llm.fallback import FallbackProvider
from app.llm.providers import PRESETS, OpenAICompatProvider, ProviderConfig
from app.llm.types import LlmConfigError, LlmProvider


def provider_config(
    settings: Settings,
    kind: str,
    model: str | None,
    api_key: str | None,
    base_url: str | None,
    *,
    extra_body: dict[str, Any] | None = None,
) -> ProviderConfig:
    preset = PRESETS[kind]
    resolved_model = model or preset.default_model
    resolved_url = base_url or preset.base_url
    if not resolved_model:
        raise LlmConfigError(f"set the model (deployment name) for the {kind} provider")
    if not resolved_url:
        raise LlmConfigError(f"set the base URL for the {kind} provider")
    if not api_key:
        raise LlmConfigError(f"set the API key for the {kind} provider")
    structured = settings.llm_structured_output
    return ProviderConfig(
        kind=kind,  # type: ignore[arg-type]
        model=resolved_model,
        api_key=api_key,
        base_url=resolved_url,
        structured_output=preset.structured_output if structured == "auto" else structured,
        timeout_seconds=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
        extra_body=dict(extra_body or {}),
    )


def _secret(value) -> str | None:
    secret = value.get_secret_value() if value is not None else None
    return secret or None


def _single(
    settings: Settings,
    kind: str,
    model: str | None,
    api_key: str | None,
    base_url: str | None,
    *,
    extra_body: dict[str, Any] | None,
    http_client: Any | None,
) -> LlmProvider:
    if kind == "fake":
        return FakeProvider(model=model or "fake-model")
    config = provider_config(settings, kind, model, api_key, base_url, extra_body=extra_body)
    return OpenAICompatProvider(config, http_client=http_client)


def build_provider(settings: Settings, *, http_client: Any | None = None) -> LlmProvider:
    """The primary provider, wrapped with the fallback provider when one is set."""
    primary_key = _secret(settings.llm_api_key)
    primary = _single(
        settings,
        settings.llm_provider,
        settings.llm_model,
        primary_key,
        settings.llm_base_url,
        extra_body=settings.llm_extra_body,
        http_client=http_client,
    )
    kind = settings.llm_fallback_provider
    if kind == "none":
        return primary
    fallback_key = _secret(settings.llm_fallback_api_key)
    if fallback_key is None and kind == settings.llm_provider:
        fallback_key = primary_key
    secondary = _single(
        settings,
        kind,
        settings.llm_fallback_model,
        fallback_key,
        settings.llm_fallback_base_url,
        extra_body=None,
        http_client=http_client,
    )
    return FallbackProvider(primary, secondary)


def build_fallback_only(settings: Settings, *, http_client: Any | None = None) -> LlmProvider:
    """Just the fallback provider (scripts.llm_check --fallback)."""
    kind = settings.llm_fallback_provider
    if kind == "none":
        raise LlmConfigError("LLM_FALLBACK_PROVIDER is not set")
    key = _secret(settings.llm_fallback_api_key)
    if key is None and kind == settings.llm_provider:
        key = _secret(settings.llm_api_key)
    return _single(
        settings,
        kind,
        settings.llm_fallback_model,
        key,
        settings.llm_fallback_base_url,
        extra_body=None,
        http_client=http_client,
    )
