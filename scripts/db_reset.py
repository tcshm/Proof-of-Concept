# scripts/db_reset.py
"""Drop every RIC table and recreate the schema from the migrations.

    uv run python -m scripts.db_reset --yes

Only allowed when ENVIRONMENT is local or test. All data is lost.
"""

from __future__ import annotations

import argparse
import sys

from alembic import command
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.db.migrate import alembic_config

ALLOWED_ENVIRONMENTS = ("local", "test")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yes", action="store_true", help="confirm that all data will be lost")
    args = parser.parse_args()

    settings = get_settings()
    database = make_url(settings.database_url).database
    if settings.environment not in ALLOWED_ENVIRONMENTS:
        print(f"Refusing to reset: ENVIRONMENT is {settings.environment!r}", file=sys.stderr)
        return 1
    if not args.yes:
        print(f"This drops all tables in {database!r}. Run again with --yes.", file=sys.stderr)
        return 1

    config = alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    print(f"database {database}: reset to head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
