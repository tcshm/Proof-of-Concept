# app/llm/fallback.py
"""Use a second provider when the first one is unavailable.

Falls back ONLY on LlmUnavailable (timeout, network error, rate limit, 5xx).
LlmRejected (bad key, unknown model) and LlmInvalidOutput are raised as they
are: a configuration problem or a bad answer must not be hidden.
The result says which provider actually answered.

Note: on NVIDIA's free tier the request limit is shared by every model on one
key, so a fallback on the same key does not help with rate limits; use another
provider (e.g. Groq) as the fallback for that.
"""

from __future__ import annotations

import logging

from app.llm.types import LlmProvider, LlmRequest, LlmResult, LlmUnavailable

logger = logging.getLogger("app.llm")


class FallbackProvider:
    def __init__(self, primary: LlmProvider, secondary: LlmProvider) -> None:
        self.primary = primary
        self.secondary = secondary
        self.name = primary.name
        self.model = primary.model

    async def generate(self, request: LlmRequest) -> LlmResult:
        try:
            return await self.primary.generate(request)
        except LlmUnavailable as exc:
            logger.warning(
                "primary llm unavailable; using fallback",
                extra={
                    "primary": f"{self.primary.name}:{self.primary.model}",
                    "fallback": f"{self.secondary.name}:{self.secondary.model}",
                    "error": exc.message,
                },
            )
            return await self.secondary.generate(request)

    async def aclose(self) -> None:
        await self.primary.aclose()
        await self.secondary.aclose()
