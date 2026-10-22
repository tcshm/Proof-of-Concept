# scripts/worker.py
"""The change worker: turns pending journal entries into store alerts, actions
and notifications using the configured LLM.

    uv run python -m scripts.worker                    keep running (Ctrl+C to stop)
    uv run python -m scripts.worker --drain            process everything pending, then stop
    uv run python -m scripts.worker --drain --max-calls 5   stop after 5 LLM calls
    uv run python -m scripts.worker --once             process at most one entry

Every LLM call counts against your provider quota. A full drain of the seed data
makes about 26 calls (entries that need no LLM are skipped without a call).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine_from_settings, create_session_factory
from app.llm.factory import build_provider
from app.llm.types import LlmConfigError
from app.worker.runner import Worker, WorkerConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the RIC change worker.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--drain", action="store_true", help="process everything pending, then stop")
    mode.add_argument("--once", action="store_true", help="process at most one entry")
    parser.add_argument(
        "--max-calls", type=int, default=None, help="with --drain: stop after this many LLM calls"
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="with --drain: stop when nothing is due now instead of waiting for retries",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    try:
        provider = build_provider(settings)
        config = WorkerConfig.from_settings(settings)
    except (LlmConfigError, ValueError) as exc:
        print(f"[FAIL] configuration: {exc}", file=sys.stderr)
        return 1

    engine = create_engine_from_settings(settings)
    worker = Worker(create_session_factory(engine), provider, SystemClock(), config)
    print(
        f"worker: provider {provider.name}:{provider.model}, "
        f"max {settings.worker_max_calls_per_minute} LLM calls/min, "
        f"{config.max_attempts} attempts per entry"
    )
    try:
        if args.drain:
            stats = await worker.drain(max_calls=args.max_calls, wait_for_retries=not args.no_wait)
            print(f"[OK] drain finished: {stats.summary()}")
        elif args.once:
            await worker.startup()
            result = await worker.process_next()
            if result is None:
                print("[OK] nothing to process")
            else:
                print(f"[OK] {result.incident_number}: {result.outcome} {result.detail}".rstrip())
        else:
            print("worker running; press Ctrl+C to stop")
            await worker.run_forever()
    finally:
        await provider.aclose()
        await engine.dispose()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("worker stopped")
        sys.exit(0)
