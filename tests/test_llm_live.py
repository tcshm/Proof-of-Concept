# tests/test_llm_live.py
"""One real call to the configured provider. Skipped unless LLM_LIVE_TEST=true in .env.

uv run pytest tests/test_llm_live.py -v      (after setting LLM_LIVE_TEST=true)
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.llm.factory import build_provider
from scripts.llm_check import CHECK_REQUEST

pytestmark = pytest.mark.skipif(
    not get_settings().llm_live_test, reason="set LLM_LIVE_TEST=true to call the real provider"
)


async def test_live_provider_returns_valid_json() -> None:
    provider = build_provider(get_settings())
    try:
        result = await provider.generate(CHECK_REQUEST)
    finally:
        await provider.aclose()
    assert result.output["severity"] in ("critical", "warning", "info")
    assert 0 < len(result.output["title"]) <= 80
    assert result.latency_ms > 0
