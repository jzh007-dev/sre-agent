"""LLM Protocol — the seam between the agent loop and anything that can answer.

The loop takes an `LLM` by parameter, not by import, so provider choice is a
wiring decision. Implementations:

- `StubLLM` — canned script for tests and the L1 skeleton.
- `Gateway.bind(inv, kind)` (W2 L3) — the real path: routing, construction,
  cache, budget, transport, tracing. Note the gateway itself is *not* an `LLM`;
  binding an investigation to it produces one, which keeps per-investigation cost
  accounting out of this signature. See [TRADEOFFS §33](../../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators)
  delta 12.
- `LiteLLMAdapter` — a documented swap-in that is deliberately not built
  ([§34](../../TRADEOFFS.md#34-litellm-not-adopted-kept-as-a-documented-swap-in)).

**The two exceptions below are part of this contract, not provider details.**
That is why they live here rather than in `errors.py`: `core/loop.py` is allowed
to import from `llm.protocol` under the seam rule, so the loop can convert them
into `Aborted` events and preserve the L2 invariant that every run emits exactly
one `Done` or `Aborted`. Provider-specific failures never reach the loop — they
are either retried, contained as tool errors, or collapsed into
`ProviderUnavailable`.
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence

from .types import Message, Response


class LLMContractError(Exception):
    """Base for failures the loop is expected to handle rather than propagate."""


class BudgetExceeded(LLMContractError):
    """The investigation's cost or token ceiling was reached, so the call was
    refused before it was sent.

    Refusing rather than truncating is what turns a cost target into a mechanism:
    the harness degrades to an "insufficient evidence" report naming the ceiling
    that stopped it, instead of quietly spending more.
    """

    def __init__(self, message: str, *, spent_usd: float = 0.0, ceiling_usd: float = 0.0) -> None:
        super().__init__(message)
        self.spent_usd = spent_usd
        self.ceiling_usd = ceiling_usd


class ProviderUnavailable(LLMContractError):
    """Every candidate provider failed, or the circuit breaker is open on all of
    them, and fallback either exhausted its candidates or was disabled.

    Fallback is disabled during eval on purpose — a run that silently continued
    on a second provider would produce an accuracy figure attributable to neither
    model. See [TRADEOFFS §33](../../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators)
    delta 9.
    """

    def __init__(self, message: str, *, tried: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.tried = tuple(tried)


class LLM(Protocol):
    async def call(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> Response:
        """Take conversation state + tool schemas, return the next Response.

        May raise `BudgetExceeded` or `ProviderUnavailable`; the loop converts
        both into `Aborted` events. Any other exception is a bug and propagates.
        """
        ...
