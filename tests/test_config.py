# tests/test_config.py
import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_accepts_asyncpg_url() -> None:
    s = Settings(database_url="postgresql+asyncpg://u:p@localhost/db", _env_file=None)
    assert s.database_url.startswith("postgresql+asyncpg://")


def test_rejects_sync_driver_url() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://u:p@localhost/db", _env_file=None)
