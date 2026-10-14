# app/db/migrate.py
"""Build an Alembic Config from code, so scripts and tests can migrate any database.

The database URL is passed through config.attributes rather than alembic.ini:
passwords stay out of committed files and '%' characters in URL-encoded
passwords do not clash with ini-file interpolation.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def alembic_config(database_url: str | None = None) -> Config:
    """Alembic config for DATABASE_URL, or for database_url when given."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    if database_url is not None:
        config.attributes["database_url"] = database_url
    return config
