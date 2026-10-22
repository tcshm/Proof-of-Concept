# scripts/demo.py
"""Rebuild the whole demo state in one command, then verify it.

    uv run python -m scripts.demo --reset --yes    rebuild the schema, then everything below
    uv run python -m scripts.demo                  seed all -> worker drain -> verify
    uv run python -m scripts.demo --anchor 2026-10-13T03:30:00Z   exact times (09:00 IST)
    uv run python -m scripts.demo --max-calls 10   stop the worker after 10 LLM calls
    uv run python -m scripts.demo --skip-worker    seed only (no LLM call), then verify

The worker uses the LLM configured in .env (about 25 calls with the seed data;
LLM_PROVIDER=fake needs no key). Exit code 1 when a verification check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine_from_settings, create_session_factory, session_scope
from app.llm.factory import build_provider
from app.llm.types import LlmConfigError
from app.models import Notification, NotificationTarget, Store, StoreAction, StoreAlert
from app.models.values import ActionStatus, AlertStatus
from app.seed.anchor import resolve_anchor
from app.seed.loader import SeedDataError, load_seed_data
from app.seed.runner import STEP_ORDER, STEPS, SeedDependencyError
from app.verify import run_checks
from app.worker.runner import Worker, WorkerConfig
from scripts.seed import reset_schema
from scripts.verify import print_results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild and verify the RIC demo data.")
    parser.add_argument("--reset", action="store_true", help="drop and recreate the schema first")
    parser.add_argument("--yes", action="store_true", help="confirm --reset (all data is lost)")
    parser.add_argument("--anchor", help="end of the event window, e.g. 2026-10-13T03:30:00Z")
    parser.add_argument("--max-calls", type=int, default=None, help="stop after N LLM calls")
    parser.add_argument("--skip-worker", action="store_true", help="do not run the worker")
    parser.add_argument(
        "--no-wait", action="store_true", help="do not wait for entries scheduled for a retry"
    )
    return parser.parse_args()


async def chennai_summary(session) -> list[str]:
    store_id = await session.scalar(select(Store.id).where(Store.site_code == "01234"))
    alerts = (
        await session.scalars(
            select(StoreAlert.title).where(
                StoreAlert.store_id == store_id, StoreAlert.status == AlertStatus.ACTIVE
            )
        )
    ).all()
    actions = (
        await session.scalars(
            select(StoreAction.title).where(
                StoreAction.store_id == store_id, StoreAction.status == ActionStatus.OPEN
            )
        )
    ).all()
    notices = await session.scalar(
        select(func.count())
        .select_from(NotificationTarget)
        .join(Notification, Notification.id == NotificationTarget.notification_id)
        .where(NotificationTarget.store_id == store_id)
    )
    lines = [f"Chennai (01234): {len(alerts)} active alerts, {len(actions)} open actions, "
             f"{notices} notifications"]  # fmt: skip
    lines += [f"  alert:  {title}" for title in sorted(alerts)]
    lines += [f"  action: {title}" for title in sorted(actions)]
    return lines


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    data = load_seed_data()
    anchor = resolve_anchor(args.anchor)
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    try:
        print(f"1) seed (anchor {anchor.isoformat()})")
        for name in STEP_ORDER:
            async with session_scope(factory) as session:
                result = await STEPS[name](session, data, anchor)
            print(f"   [OK] {name:<13} {result}")

        if args.skip_worker:
            print("2) worker skipped (--skip-worker)")
        else:
            provider = build_provider(settings)
            print(f"2) worker with {provider.name}:{provider.model} "
                  f"(max {settings.worker_max_calls_per_minute} calls/min)")  # fmt: skip
            worker = Worker(factory, provider, SystemClock(), WorkerConfig.from_settings(settings))
            try:
                stats = await worker.drain(
                    max_calls=args.max_calls, wait_for_retries=not args.no_wait
                )
            finally:
                await provider.aclose()
            print(f"   [OK] {stats.summary()}")

        print("3) verify")
        async with factory() as session:
            results = await run_checks(session, now=SystemClock().now())
            summary = await chennai_summary(session)
        passed = print_results(results)
        print()
        print("\n".join(summary))
        print(
            "\nNext: uv run uvicorn app.main:app --port 8000, then open http://127.0.0.1:8000/docs"
        )
        return 0 if passed else 1
    finally:
        await engine.dispose()


def main() -> int:
    args = parse_args()
    try:
        if args.reset:
            reset_schema(args.yes)
        return asyncio.run(run(args))
    except (SeedDataError, SeedDependencyError, LlmConfigError, ValueError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
