# app/core/config.py
"""Application settings, loaded from environment variables and .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LlmProviderName = Literal["fake", "nvidia", "groq", "foundry"]
LlmStructuredOutput = Literal["auto", "nvext", "json_schema", "json_object", "none"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "RIC Backend"
    app_version: str = "0.1.0"
    environment: Literal["local", "test", "dev", "prod"] = "local"
    debug: bool = False

    database_url: str
    db_echo: bool = False

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"

    # Comma-separated browser origins allowed to call the API (admin portal, Expo web).
    # Empty means no browser origin is allowed. Never use "*".
    cors_origins: str = ""

    docs_enabled: bool = True

    # Write one api_request_log row per API request (health, docs and OPTIONS are skipped).
    request_log_enabled: bool = True

    # --- LLM (Phase 8) ---------------------------------------------------------------
    # Provider: fake (no network), nvidia (NIM), groq, foundry (Azure AI Foundry / Azure OpenAI).
    llm_provider: LlmProviderName = "fake"
    # Empty: the provider's default (nvidia: nemotron-3.5-lightning, groq: gpt-oss-20b).
    # Foundry: your deployment name (required).
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    # Empty: the provider's default. Foundry: https://<resource>.openai.azure.com/openai/v1/
    llm_base_url: str | None = None
    # How JSON is enforced. auto: nvidia -> json_schema; groq and foundry -> json_object.
    llm_structured_output: LlmStructuredOutput = "auto"
    # Free endpoints can be slow: generous by default.
    llm_timeout_seconds: float = Field(default=120.0, ge=5, le=600)
    llm_max_output_tokens: int = Field(default=2048, ge=64, le=32768)
    # Extra request fields merged last (JSON), e.g. {"chat_template_kwargs": {...}}.
    llm_extra_body: dict[str, Any] = Field(default_factory=dict)

    # Optional second provider, used only when the first is unavailable
    # (timeout, network error, rate limit, 5xx).
    llm_fallback_provider: Literal["none", "fake", "nvidia", "groq", "foundry"] = "none"
    llm_fallback_model: str | None = None
    # Empty: reuse LLM_API_KEY when the fallback is the same provider.
    llm_fallback_api_key: SecretStr | None = None
    llm_fallback_base_url: str | None = None

    # Run the one live LLM test (tests/test_llm_live.py) against the real provider.
    llm_live_test: bool = False

    # --- Worker (Phase 9) ------------------------------------------------------------
    # Seconds to wait when no journal entry is due.
    worker_poll_seconds: float = Field(default=5.0, ge=0.5, le=300)
    # Free-tier quota: at most this many LLM calls per minute (calls are spaced evenly).
    worker_max_calls_per_minute: int = Field(default=20, ge=1, le=600)
    # Attempts per journal entry before it fails (impact assessment: template alert).
    worker_max_attempts: int = Field(default=3, ge=1, le=10)
    # Minutes to wait before attempt 2, 3, ... (comma-separated; the last value repeats).
    worker_retry_delays_minutes: str = "2,10"
    # After an HTTP 429 (rate limit), no LLM call for this many seconds.
    worker_rate_limit_pause_seconds: float = Field(default=60.0, ge=0, le=3600)
    # How often the running worker expires overdue store actions.
    worker_sweep_seconds: float = Field(default=60.0, ge=5, le=3600)

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must start with postgresql+asyncpg://")
        return value

    @field_validator("cors_origins")
    @classmethod
    def _no_wildcard_origin(cls, value: str) -> str:
        if "*" in value:
            raise ValueError("CORS_ORIGINS must list explicit origins; '*' is not allowed")
        return value

    @field_validator("llm_model", "llm_base_url", "llm_fallback_model", "llm_fallback_base_url")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return value.strip() or None if isinstance(value, str) else value

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
