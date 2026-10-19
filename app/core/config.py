# app/core/config.py
"""Application settings, loaded from environment variables and .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
