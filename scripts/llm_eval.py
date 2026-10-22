# scripts/llm_eval.py
"""Scorecard for the two LLM workflows against the configured model.

    uv run python -m scripts.llm_eval                  every case once (about 18 LLM calls)
    uv run python -m scripts.llm_eval --case NAME      one case (repeat --case for more)
    uv run python -m scripts.llm_eval --repeat 3       every case 3 times (consistency)
    uv run python -m scripts.llm_eval --list           list the cases, no LLM call

Uses LLM quota; calls are paced by WORKER_MAX_CALLS_PER_MINUTE. Nothing is written
to the database. Exit code 1 when a case fails.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from app.core.config import get_settings
from app.llm.factory import build_provider
from app.llm.types import LlmConfigError
from app.worker.evaluate import active_prompts, describe, load_cases, run_case


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the LLM scorecard.")
    parser.add_argument("--case", action="append", default=[], help="run only this case")
    parser.add_argument("--repeat", type=int, default=1, help="run each case N times")
    parser.add_argument("--list", action="store_true", help="list the cases and stop")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    cases = load_cases()
    if args.case:
        unknown = sorted(set(args.case) - {c.name for c in cases})
        if unknown:
            print(f"[FAIL] unknown case(s): {', '.join(unknown)}", file=sys.stderr)
            return 1
        cases = [c for c in cases if c.name in args.case]
    if args.list:
        for case in cases:
            print(f"{case.name:<34} {case.task.value:<20} expect {describe(case.expect)}")
        return 0

    settings = get_settings()
    try:
        provider = build_provider(settings)
    except LlmConfigError as exc:
        print(f"[FAIL] configuration: {exc}", file=sys.stderr)
        return 1
    prompts = active_prompts()
    interval = 60.0 / settings.worker_max_calls_per_minute
    versions = ", ".join(f"{w} v{p.version}" for w, p in sorted(prompts.items()))
    print(f"model: {provider.name}:{provider.model}   prompts: {versions}")
    print(f"{'case':<34} {'expected':<26} {'got':<28} result")

    passed = total = calls = 0
    last_call = 0.0
    try:
        for case in cases:
            for _ in range(max(args.repeat, 1)):
                wait = last_call + interval - time.monotonic()
                if calls and wait > 0:
                    await asyncio.sleep(wait)
                last_call = time.monotonic()
                calls += 1
                result = await run_case(provider, prompts, case)
                total += 1
                passed += result.ok
                mark = "PASS" if result.ok else "FAIL"
                print(f"{result.name:<34} {result.expected:<26} {result.got:<28} {mark}")
                for note in result.adjustments:
                    print(f"{'':<36}guard: {note}")
                if result.error:
                    print(f"{'':<36}{result.error}")
    finally:
        await provider.aclose()
    print(f"\n{passed}/{total} passed, {calls} LLM calls")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
