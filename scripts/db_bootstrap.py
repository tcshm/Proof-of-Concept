# scripts/db_bootstrap.py
"""Create the RIC application role and databases on a local PostgreSQL server.

Idempotent: safe to run again. Run from the project root:

    uv run python -m scripts.db_bootstrap

Reads from .env:
    ADMIN_DATABASE_URL  superuser connection, used only by this script
    DATABASE_URL        app connection; its user, password and database are created here
"""

from __future__ import annotations

import asyncio
import re
import sys

import asyncpg
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class BootstrapSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    admin_database_url: str
    database_url: str


def _ident(name: str) -> str:
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"Unsafe identifier {name!r}: use lowercase letters, digits and _")
    return f'"{name}"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _connect_kwargs(url: URL, database: str) -> dict[str, object]:
    return {
        "user": url.username,
        "password": url.password,
        "host": url.host or "localhost",
        "port": url.port or 5432,
        "database": database,
    }


async def _ensure_role(conn: asyncpg.Connection, role: str, password: str) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
    verb = "ALTER" if exists else "CREATE"
    await conn.execute(f"{verb} ROLE {_ident(role)} WITH LOGIN PASSWORD {_literal(password)}")
    print(f"role {role}: {'exists, password synced' if exists else 'created'}")


async def _ensure_database(conn: asyncpg.Connection, db: str, owner: str) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db)
    if not exists:
        await conn.execute(f"CREATE DATABASE {_ident(db)} OWNER {_ident(owner)}")
    await conn.execute(f"ALTER DATABASE {_ident(db)} SET timezone TO 'UTC'")
    print(f"database {db}: {'exists' if exists else 'created'}, timezone=UTC")


async def main() -> int:
    settings = BootstrapSettings()
    app_url = make_url(settings.database_url)
    admin_url = make_url(settings.admin_database_url)

    role, password, db = app_url.username, app_url.password, app_url.database
    if not (role and password and db):
        print("DATABASE_URL must include user, password and database name", file=sys.stderr)
        return 1

    admin = await asyncpg.connect(**_connect_kwargs(admin_url, database="postgres"))
    try:
        await _ensure_role(admin, role, password)
        for name in (db, f"{db}_test"):
            await _ensure_database(admin, name, role)
    finally:
        await admin.close()

    print("bootstrap complete")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
