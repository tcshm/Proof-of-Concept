# scripts/llm_check.py
"""Send one small structured request to the configured LLM provider.

    uv run python -m scripts.llm_check               the configured provider (+ fallback)
    uv run python -m scripts.llm_check --fallback    only the fallback provider

Prints the provider, model, latency, tokens and the validated JSON answer.
Use it after changing LLM_* settings in .env. Exit code 1 on any error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.core.config import get_settings
from app.llm.factory import build_fallback_only, build_provider
from app.llm.types import LlmConfigError, LlmError, LlmRequest

CHECK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["severity", "title", "detail"],
    "properties": {
        "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
        "title": {"type": "string", "maxLength": 80},
        "detail": {"type": "string", "maxLength": 300},
    },
}

CHECK_REQUEST = LlmRequest(
    system_prompt=(
        "You write short, plain-language alerts for retail store staff about IT problems."
    ),
    user_prompt=(
        "The store's primary internet link (AT&T) is down. The store is running on its "
        "backup link, so card payments still work but may be slower. Write the alert."
    ),
    output_schema=CHECK_SCHEMA,
    temperature=0.2,
)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Check the configured LLM provider.")
    parser.add_argument("--fallback", action="store_true", help="test only the fallback provider")
    args = parser.parse_args()

    settings = get_settings()
    try:
        provider = build_fallback_only(settings) if args.fallback else build_provider(settings)
    except LlmConfigError as exc:
        print(f"[FAIL] configuration: {exc}", file=sys.stderr)
        return 1

    print(f"provider: {provider.name}   model: {provider.model}")
    print(
        f"timeout: {settings.llm_timeout_seconds:g} s   max output tokens: "
        f"{settings.llm_max_output_tokens}"
    )
    try:
        result = await provider.generate(CHECK_REQUEST)
    except LlmError as exc:
        print(f"[FAIL] {exc.__class__.__name__}: {exc}", file=sys.stderr)
        if exc.raw_text:
            print(f"raw answer:\n{exc.raw_text[:1000]}", file=sys.stderr)
        return 1
    finally:
        await provider.aclose()

    print(
        f"[OK] answered by {result.model_label} in {result.latency_ms} ms "
        f"(tokens in/out: {result.prompt_tokens}/{result.completion_tokens})"
    )
    print(json.dumps(result.output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
