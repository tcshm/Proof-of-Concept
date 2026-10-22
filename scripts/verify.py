# scripts/verify.py
"""Check that the database follows the agreed rules (read-only, no LLM call).

    uv run python -m scripts.verify

Prints PASS / FAIL per rule; exit code 1 when any rule fails. Run it after
scripts.seed all + scripts.worker --drain, or use scripts.demo which runs it.
"""

from __future__ import annotations

import asyncio
import sys

from app.core.clock import SystemClock
from app.db.session import create_engine_from_settings, create_session_factory
from app.verify import CheckResult, run_checks


def print_results(results: list[CheckResult]) -> bool:
    for result in results:
        mark = "PASS" if result.ok else "FAIL"
        print(f"[{mark}] {result.area:<13} {result.name:<38} {result.detail}")
    failed = sum(not r.ok for r in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return failed == 0


async def main() -> int:
    engine = create_engine_from_settings()
    try:
        async with create_session_factory(engine)() as session:
            results = await run_checks(session, now=SystemClock().now())
    finally:
        await engine.dispose()
    return 0 if print_results(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
