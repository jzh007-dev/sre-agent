"""The gateway — assembly point for every LLM call.

Order of operations, and the reasoning for it:

    route → construct → [cache lookup] → budget gate → transport → cache store
      └──────────────────── trace wraps all of it ────────────────────┘

- **Cache before budget.** A hit costs no money, so gating first would refuse free
  calls. But a hit still *charges* the budget, replaying the original call's cost,
  or a run that degraded on exhaustion would stop degrading on rerun and stop
  reproducing. See [TRADEOFFS §33](../../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators) delta 3.
- **Fallback lives here, not in transport.** Falling back means choosing a
  different provider, which is a routing decision. Transport only knows one
  adapter.
- **Fallback is disabled in eval.** A run that silently continued on a second
  provider has an accuracy attributable to neither model and a cost mixing two
  price sheets. `allow_fallback=False` is the eval wiring, and any real run that
  did fall back is tagged in the ledger and excluded from model comparison
  (delta 9).
- **The investigation is bound at construction.** `bind()` returns an object
  implementing the `LLM` protocol, so the loop keeps seeing a plain `LLM` and
  per-investigation accounting never leaks into that signature (delta 12).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..core.investigation import Investigation
from .cache import CacheEntry, MemoryStore, ResponseCache
from .cost import Ledger
from .errors import ContextLimit, ProviderError
from .protocol import BudgetExceeded, ContextOverflow, LLMContractError, ProviderUnavailable
from .provider_catalog import ModelSpec, provider_of
from .request import (
    CONTEXT_HEADROOM,
    LLMRequest,
    SystemPrompt,
    build,
    check_context,
    estimate_tokens,
)
from .routing import CallKind, RoutingConfig, default_config, route, validate
from .transport import Attempt, Transport
from .types import Message, Response
from .usage import cost_of

#: Called once per completed call with a flat dict. Langfuse wiring lands in
#: W2 L8; a callable keeps the gateway free of a tracing dependency and keeps the
#: trace payload assertable in tests.
Tracer = Callable[[Mapping[str, Any]], None]


def _noop_tracer(_: Mapping[str, Any]) -> None:
    return None


@dataclass
class Gateway:
    """Process-level. Holds the routing table, transports, and the shared cache."""

    routing: RoutingConfig = field(default_factory=default_config)
    #: One transport per provider name. Each owns that provider's breaker and
    #: semaphore, so a Kimi outage cannot open DeepSeek's breaker.
    transports: dict[str, Transport] = field(default_factory=dict)
    cache: ResponseCache = field(default_factory=lambda: ResponseCache(store=MemoryStore()))
    tracer: Tracer = _noop_tracer
    allow_fallback: bool = True

    def __post_init__(self) -> None:
        # Wiring-time validation. A routing table that cannot satisfy a call kind —
        # or that points judge/reviewer at the agent's own family — is a
        # configuration bug, and finding it mid-incident is strictly worse than
        # refusing to start. See routing.validate.
        validate(self.routing)

    def bind(
        self,
        inv: Investigation,
        kind: CallKind = CallKind.MAIN_LOOP,
        system: SystemPrompt | None = None,
        ledger: Ledger | None = None,
    ) -> BoundLLM:
        """Return an `LLM` for one investigation and one call kind."""
        return BoundLLM(
            gateway=self,
            inv=inv,
            kind=kind,
            system=system or SystemPrompt(),
            ledger=ledger if ledger is not None else Ledger(investigation_id=inv.id),
        )

    def transport_for(self, spec: ModelSpec) -> Transport:
        try:
            return self.transports[spec.provider]
        except KeyError as exc:
            raise ProviderUnavailable(
                f"no transport wired for provider {spec.provider!r}; "
                f"wired: {sorted(self.transports)}",
                tried=[spec.provider],
            ) from exc


@dataclass
class BoundLLM:
    """An `LLM` bound to one investigation. Implements `llm.protocol.LLM`."""

    gateway: Gateway
    inv: Investigation
    kind: CallKind
    system: SystemPrompt
    ledger: Ledger

    async def call(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> Response:
        candidates = route(self.gateway.routing, self.kind)
        if not self.gateway.allow_fallback:
            candidates = candidates[:1]

        errors: list[str] = []
        for index, spec in enumerate(candidates):
            request = build(
                spec.id,
                messages,
                tools,
                system=self.system,
                max_tokens=4096,
                temperature=0.0,
            )
            try:
                return await self._attempt(spec, request, fell_back=index > 0)
            except LLMContractError:
                # Contract errors are ours, not the provider's — a budget ceiling or
                # an oversized request would be refused identically by every
                # candidate, so do not burn the fallback chain on them. Falling back
                # on a context overflow would be actively harmful: a bigger window
                # postpones compaction until nothing fits.
                raise
            except (ProviderUnavailable, ProviderError) as exc:
                errors.append(f"{spec.provider}/{spec.id}: {type(exc).__name__}: {exc}")
                continue

        raise ProviderUnavailable(
            "every candidate failed for "
            f"{self.kind.value} (fallback {'on' if self.gateway.allow_fallback else 'off'}): "
            + "; ".join(errors),
            tried=[spec.provider for spec in candidates],
        )

    async def _attempt(self, spec: ModelSpec, request: LLMRequest, *, fell_back: bool) -> Response:
        key = request.cache_key()
        provider = provider_of(spec)

        cached = self.gateway.cache.get(key)
        if cached is not None:
            # Replay the original cost so the budget behaves identically to the
            # uncached run; `cached=True` keeps it out of money_spent.
            self.ledger.record(
                kind=self.kind.value,
                model_id=cached.model_id,
                provider=provider.name,
                usage=cached.usage,
                price=spec.price,
                cached=True,
                cost=cached.cost,
            )
            self._trace(spec, key, cached=True, attempts=[], fell_back=fell_back)
            return cached.response

        # Pre-flight context check. Anticipating ContextLimit beats catching it: a
        # 400 costs a round trip, and some providers do not distinguish "too long"
        # from other bad requests, so the caller would have to guess whether
        # compaction is the fix. W3 L6 catches this and compacts; until then it
        # surfaces as a clean, actionable error instead of a provider 400.
        fits, why = check_context(request, spec.context_window)
        if not fits:
            raise ContextOverflow(
                f"request for {spec.id} does not fit: {why}",
                estimated_tokens=estimate_tokens(request),
                limit_tokens=int(spec.context_window * CONTEXT_HEADROOM),
                model_id=spec.id,
            )

        self._check_budget(spec)

        transport = self.gateway.transport_for(spec)
        try:
            (response, usage), attempts = await transport.call(request)
        except ContextLimit as exc:
            # The provider disagreed with our estimate. Translate its detection class
            # into the contract concept so the caller gets one thing to handle.
            raise ContextOverflow(
                f"{spec.id} rejected the request as too long: {exc}",
                estimated_tokens=estimate_tokens(request),
                limit_tokens=int(spec.context_window * CONTEXT_HEADROOM),
                model_id=spec.id,
            ) from exc

        cost = cost_of(usage, spec.price)
        self.gateway.cache.put(
            key,
            CacheEntry(
                response=response,
                usage=usage,
                cost=cost,
                model_id=spec.id,
                price_table_version=spec.price.as_of,
            ),
        )
        self.ledger.record(
            kind=self.kind.value,
            model_id=spec.id,
            provider=provider.name,
            usage=usage,
            price=spec.price,
            attempts=len(attempts),
            fell_back=fell_back,
        )
        self._trace(spec, key, cached=False, attempts=attempts, fell_back=fell_back)
        return response

    def _check_budget(self, spec: ModelSpec) -> None:
        """Refuse rather than truncate, in the currency this provider bills in.

        Refusing is what turns a cost target into a mechanism: the harness degrades
        to an "insufficient evidence" report naming the ceiling that stopped it,
        instead of quietly spending more. The loop converts this into
        `Aborted("budget", ...)`.

        A currency with no configured ceiling is refused too. Treating it as
        unbounded would let a newly-added provider escape the budget silently, which
        is the failure this gate exists to prevent.
        """
        currency = spec.price.currency
        ceiling = self.inv.budget.ceiling_for(currency)
        if ceiling is None:
            raise BudgetExceeded(
                f"investigation {self.inv.id} has no {currency} ceiling configured, so "
                f"a {spec.id} call cannot be gated; add {currency} to ToolBudget.max_cost",
                currency=currency,
            )
        spent = self.ledger.spent_in(currency)
        if spent >= ceiling:
            raise BudgetExceeded(
                f"investigation {self.inv.id} has spent {spent:.4f} {currency} of its "
                f"{ceiling:.4f} {currency} ceiling; refusing a further "
                f"{self.kind.value} call to {spec.id}",
                spent=spent,
                ceiling=ceiling,
                currency=currency,
            )

    def _trace(
        self,
        spec: ModelSpec,
        key: str,
        *,
        cached: bool,
        attempts: Sequence[Attempt],
        fell_back: bool,
    ) -> None:
        self.gateway.tracer(
            {
                "investigation_id": self.inv.id,
                "call_kind": self.kind.value,
                "model_id": spec.id,
                "provider": spec.provider,
                "cache_key": key,
                "cache_hit": cached,
                "attempts": len(attempts),
                "retried": len(attempts) > 1,
                "fell_back": fell_back,
                "turn": self.inv.turn,
                # Version stamps: EVAL.md's reproducibility matrix is keyed on
                # these, so a trace missing them cannot be placed in it.
                "currency": spec.price.currency,
                "price_table_version": spec.price.as_of,
                "prices_verified": spec.price.verified,
                "ledger": self.ledger.summary(),
            }
        )
