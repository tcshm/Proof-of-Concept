# scripts/seed.py
"""Load the POC seed data (phase 2: master data, inventory, Aruba events).

    uv run python -m scripts.seed all                  master -> inventory -> events
    uv run python -m scripts.seed all --reset --yes    rebuild the schema first
    uv run python -m scripts.seed master               stores, groups, types, prompts
    uv run python -m scripts.seed inventory            network elements and devices
    uv run python -m scripts.seed events               replace the 24-hour event window
    uv run python -m scripts.seed all --anchor 2026-10-13T09:00:00Z   fixed times

Safe to run more than once: master data and inventory are upserted, and the
events step always leaves exactly one window.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from alembic import command
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.db.migrate import alembic_config
from app.db.session import create_engine_from_settings, create_session_factory, session_scope
from app.seed.anchor import resolve_anchor
from app.seed.loader import DEFAULT_DATA_DIR, SeedDataError, load_seed_data
from app.seed.runner import STEP_ORDER, STEPS, SeedDependencyError
from app.seed.summary import collect_summary

ALLOWED_RESET_ENVIRONMENTS = ("local", "test")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load the RIC POC seed data.")
    parser.add_argument("step", choices=("all", *STEP_ORDER), help="which step to run")
    parser.add_argument("--reset", action="store_true", help="drop and recreate the schema first")
    parser.add_argument("--yes", action="store_true", help="confirm --reset (all data is lost)")
    parser.add_argument("--anchor", help="end of the event window, e.g. 2026-10-13T09:00:00Z")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    return parser.parse_args()


def reset_schema(confirmed: bool) -> None:
    settings = get_settings()
    database = make_url(settings.database_url).database
    if settings.environment not in ALLOWED_RESET_ENVIRONMENTS:
        raise SystemExit(f"Refusing to reset: ENVIRONMENT is {settings.environment!r}")
    if not confirmed:
        raise SystemExit(f"--reset drops all tables in {database!r}. Add --yes to confirm.")
    config = alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    print(f"database {database}: schema reset to head")


async def run(args: argparse.Namespace) -> int:
    data = load_seed_data(args.data_dir)
    anchor = resolve_anchor(args.anchor)
    steps = STEP_ORDER if args.step == "all" else (args.step,)
    print(f"anchor: {anchor.isoformat()}")

    engine = create_engine_from_settings()
    factory = create_session_factory(engine)
    try:
        for name in steps:
            async with session_scope(factory) as session:
                result = await STEPS[name](session, data, anchor)
            print(f"[OK] {name:<9} {result}")
        async with factory() as session:
            print()
            print("\n".join(await collect_summary(session)))
    finally:
        await engine.dispose()
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.reset:
            reset_schema(args.yes)
        return asyncio.run(run(args))
    except (SeedDataError, SeedDependencyError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
