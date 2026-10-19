# scripts/process_events.py
"""Turn raw Aruba events into network_events and the current element status.

    uv run python -m scripts.process_events                 process pending events
    uv run python -m scripts.process_events --rebuild       clear and replay everything
    uv run python -m scripts.process_events --rebuild --as-of 2026-10-13T09:00:00Z

--as-of is the time the run treats as "now": only events that had arrived by
then are processed (default: the current time). Safe to run more than once:
--rebuild always gives the same result for the same raw events and --as-of.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from app.db.session import create_engine_from_settings, create_session_factory, session_scope
from app.models.values import ProcessingRunMode
from app.processing.runner import (
    ProcessorBusyError,
    record_failed_run,
    run_processor,
    status_summary,
)


def parse_as_of(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise SystemExit("--as-of must include a timezone, e.g. 2026-10-13T09:00:00Z")
    return parsed.astimezone(UTC)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process raw Aruba network events.")
    parser.add_argument("--rebuild", action="store_true", help="clear and replay all events")
    parser.add_argument("--as-of", help="treat this time as now, e.g. 2026-10-13T09:00:00Z")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    as_of = parse_as_of(args.as_of)
    mode = ProcessingRunMode.REBUILD if args.rebuild else ProcessingRunMode.INCREMENTAL
    print(f"mode: {mode}   as of: {as_of.isoformat()}")

    engine = create_engine_from_settings()
    factory = create_session_factory(engine)
    try:
        try:
            async with session_scope(factory) as session:
                run, counts = await run_processor(
                    session, mode=mode, as_of=as_of, triggered_by="process_events"
                )
        except ProcessorBusyError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            async with session_scope(factory) as session:
                await record_failed_run(
                    session,
                    mode=mode,
                    as_of=as_of,
                    triggered_by="process_events",
                    error=f"{exc.__class__.__name__}: {exc}",
                )
            raise

        print(
            f"[OK] run {run.id}: read {counts.events_read}, processed {counts.events_processed}, "
            f"duplicates {counts.duplicates}, unmatched {counts.unmatched}, "
            f"errors {counts.errors}, status changes {counts.status_changes}"
        )
        async with factory() as session:
            rows = await status_summary(session)
        print()
        print("Element status per store:  site   online  offline  unknown")
        for site, online, offline, unknown in rows:
            print(f"                            {site}  {online:>6}  {offline:>7}  {unknown:>7}")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
