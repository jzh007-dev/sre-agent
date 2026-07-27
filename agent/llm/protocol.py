"""LLM Protocol — the seam between the agent loop and any provider adapter.

The loop takes an LLM by parameter, not by import, so provider choice is a
wiring decision. Implementations:
- StubLLM (this file's sibling) — canned script for tests and L1 skeleton.
- AnthropicLLM (Week 2 L2) — real Anthropic Messages API.
- OpenAILLM / GeminiLLM — future, when multi-provider is exercised.
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence

from .types import Message, Response


class LLM(Protocol):
    async def call(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> Response:
        """Take conversation state + tool schemas, return the next Response.

        Streaming (Week 2 L2): a streaming variant will yield partial events
        while accumulating into the final Response. The signature stays the
        same at the loop boundary — streaming becomes a side channel.
        """
        ...
