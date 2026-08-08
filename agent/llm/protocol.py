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

**The contract exceptions below are part of this contract, not provider details.**
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
    """Base for failures the loop is expected to handle rather than propagate.

    Each subclass declares a `reason`, and the loop turns any contract error into
    `Aborted(exc.reason, str(exc))` with a single `except` clause. That is the seam
    rule applied to itself: a fourth contract error needs no change to the kernel.
    """

    #: Short, stable label. Ends up in eval metrics and in the JSONL log, so it is
    #: part of the observable contract rather than a message detail.
    reason: str = "llm_contract_error"


class ContextOverflow(LLMContractError):
    """The request does not fit the model's context window.

    Distinct from `errors.ContextLimit`, which is the *provider-detection* class the
    transport taxonomy keys on. This is the contract-level concept, and the gateway
    translates one into the other — which is precisely the chokepoint's job.

    Two consequences follow from it being a contract error rather than a provider
    failure:

    - **It never triggers cross-provider fallback.** Falling back to a model with a
      bigger window would postpone the problem rather than fix it, and the context
      would keep growing until nothing fits. The fix is local: compact (W3 L6).
    - **It carries the numbers compaction needs** — how much was estimated and how
      much room there is — so the caller does not have to re-derive them.
    """

    reason = "context_overflow"

    def __init__(
        self,
        message: str,
        *,
        estimated_tokens: int = 0,
        limit_tokens: int = 0,
        model_id: str = "",
    ) -> None:
        super().__init__(message)
        self.estimated_tokens = estimated_tokens
        self.limit_tokens = limit_tokens
        self.model_id = model_id

    @property
    def excess_tokens(self) -> int:
        """Roughly how much has to go. What compaction targets."""
        return max(self.estimated_tokens - self.limit_tokens, 0)


class BudgetExceeded(LLMContractError):
    """The investigation's cost or token ceiling was reached, so the call was
    refused before it was sent.

    Refusing rather than truncating is what turns a cost target into a mechanism:
    the harness degrades to an "insufficient evidence" report naming the ceiling
    that stopped it, instead of quietly spending more.
    """

    reason = "budget"

    def __init__(
        self,
        message: str,
        *,
        spent: float = 0.0,
        ceiling: float = 0.0,
        currency: str = "",
    ) -> None:
        super().__init__(message)
        #: In `currency` — costs are never converted, so a ceiling is per currency.
        self.spent = spent
        self.ceiling = ceiling
        self.currency = currency


class ProviderUnavailable(LLMContractError):
    """Every candidate provider failed, or the circuit breaker is open on all of
    them, and fallback either exhausted its candidates or was disabled.

    Fallback is disabled during eval on purpose — a run that silently continued
    on a second provider would produce an accuracy figure attributable to neither
    model. See [TRADEOFFS §33](../../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators)
    delta 9.
    """

    reason = "provider_unavailable"

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

        May raise any `LLMContractError`; the loop converts each into an `Aborted`
        event using the exception's `reason`. Any other exception is a bug and
        propagates.
        """
        ...


class LLMFactory(Protocol):
    """Binds an `LLM` to one investigation and one assembled system prompt.

    Declared here, in the module the seam test lets `agent/core/` import, because the
    harness has to bind the LLM itself rather than receive one already bound. The reason
    is specific: the system prompt does **not** travel through `LLM.call` — it is bound
    at construction, by `Gateway.bind(inv, kind, system=…)`. So a harness handed a
    finished `LLM` could not own step ③'s prompt assembly, and prompt assembly would
    have to move out of the pipeline it belongs to.

    `prompt` is typed loosely on purpose. Its real type is `llm.request.SystemPrompt`,
    which `agent/core/` may not name — and it must stay a structured object rather than
    a string, because `PromptFragment.stable_across` is what places the cache
    breakpoints that W2 L3b measured as a 94.9% saving on a warm call. Flattening it to
    text to get it across this boundary would spend that to satisfy a type annotation.

    `Gateway.bind` already satisfies this shape, so nothing has to implement it.
    """

    def __call__(self, inv: Any, prompt: Any) -> LLM: ...
