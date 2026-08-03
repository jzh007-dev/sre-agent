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
- **Tracing is a span, and the span parents itself.** `llm.call` and its `attempt`
  children attach to whatever span is ambient — the loop's current turn, normally —
  which is why per-call tracing needed no change to the `LLM` signature.

  Emission stays here, so coverage is structural: a call cannot reach a provider from
  this module without producing a span, whatever the wiring. Where those spans *go* is
  `Trace.sinks`, which is why adding Langfuse in W2 L8 will touch no code in this file.
  This replaced a `tracer` callable taking a flat dict — the dict carried no duration
  and no parent, so it could not answer "was the provider slow, or did we retry three
  times", and keeping both shapes for one event would have meant writing the duration
  in two places. `Span.as_dict()` is a superset of that old payload.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..core import trace as tracing
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

@dataclass
class Gateway:
    """Process-level. Holds the routing table, transports, and the shared cache."""

    routing: RoutingConfig = field(default_factory=default_config)
    #: One transport per provider name. Each owns that provider's breaker and
    #: semaphore, so a Kimi outage cannot open DeepSeek's breaker.
    transports: dict[str, Transport] = field(default_factory=dict)
    cache: ResponseCache = field(default_factory=lambda: ResponseCache(store=MemoryStore()))
    #: Fallback trace, used **only** when no trace is installed on the context.
    #:
    #: The ambient one wins whenever there is one, because parenting an `llm.call`
    #: under the turn that made it matters more than any wiring preference, and a
    #: second trace id for the same run would defeat the point of having one. This
    #: field exists for callers with no loop above them — `srectl smoke`, and tests
    #: that exercise the gateway directly.
    trace: tracing.Trace | None = None
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

    def active_trace(self) -> tracing.Trace:
        """The ambient trace if a loop installed one, else the wired fallback."""
        ambient = tracing.current_trace()
        if ambient is not tracing.NULL_TRACE:
            return ambient
        return self.trace or tracing.NULL_TRACE


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
        trace = self.gateway.active_trace()

        # The span wraps everything, so a call that ends in ContextOverflow or a dead
        # provider is recorded too. Before L4a those paths left only an exception,
        # which is the opposite of what a post-mortem needs.
        with trace.span(
            tracing.LLM_CALL,
            investigation_id=self.inv.id,
            call_kind=self.kind.value,
            model_id=spec.id,
            provider=spec.provider,
            cache_key=key,
            turn=self.inv.turn,
        ) as span:
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
                self._stamp(span, spec, cached=True, attempts=0, fell_back=fell_back)
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
                (response, usage), attempts = await transport.call(
                    request, on_attempt=_attempt_span(trace)
                )
            except ContextLimit as exc:
                # The provider disagreed with our estimate. Translate its detection
                # class into the contract concept so the caller gets one thing to
                # handle.
                raise ContextOverflow(
                    f"{spec.id} rejected the request as too long: {exc}",
                    estimated_tokens=estimate_tokens(request),
                    limit_tokens=int(spec.context_window * CONTEXT_HEADROOM),
                    model_id=spec.id,
                ) from exc

            # The provider may have served a different model than we asked for — model
            # names are frequently aliases. Recording the mismatch matters because cost
            # is per model and EVAL keys reproducibility on model_version; routing
            # already refuses catalogued aliases, so a mismatch here means an
            # *uncatalogued* one.
            served = response.served_model
            alias_mismatch = bool(served and served != spec.id)

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
            self._stamp(
                span,
                spec,
                cached=False,
                attempts=len(attempts),
                fell_back=fell_back,
                served_model=served,
                alias_mismatch=alias_mismatch,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cost_native=round(cost.native, 8),
            )
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

    def _stamp(
        self,
        span: tracing.Span,
        spec: ModelSpec,
        *,
        cached: bool,
        attempts: int,
        fell_back: bool,
        served_model: str = "",
        alias_mismatch: bool = False,
        **extra: Any,
    ) -> None:
        """Everything about the call that is only known once it has returned."""
        span.set(
            # What the provider says answered. Differing from `model_id` means we
            # asked for an uncatalogued alias, so the price and the recorded
            # model_version are both attached to the wrong thing.
            served_model=served_model,
            alias_mismatch=alias_mismatch,
            cache_hit=cached,
            attempts=attempts,
            retried=attempts > 1,
            fell_back=fell_back,
            # Version stamps: EVAL.md's reproducibility matrix is keyed on these, so
            # a trace missing them cannot be placed in it.
            currency=spec.price.currency,
            price_table_version=spec.price.as_of,
            prices_verified=spec.price.verified,
            ledger=self.ledger.summary(),
            **extra,
        )


def _attempt_span(trace: tracing.Trace) -> Callable[[Attempt], None]:
    """Turn each transport attempt into a child span of the current `llm.call`.

    This is the fix for the audit's second finding: `Attempt.error`,
    `error_class` and `delay_before` were written on every attempt and read by
    nothing, so a run that retried twice and one that hit a slow provider were
    indistinguishable after the fact.

    `trace.record` rather than `trace.span`, because transport already measured the
    send — timing this callback would measure the callback.
    """

    def sink(attempt: Attempt) -> None:
        trace.record(
            tracing.ATTEMPT,
            duration_ms=attempt.duration_ms,
            status="error" if attempt.error else "ok",
            attempt=attempt.attempt,
            provider=attempt.provider,
            model_id=attempt.model_id,
            error=attempt.error or "",
            error_class=attempt.error_class or "",
            # Kept separate from `duration_ms`: backoff is our latency, the send is
            # the provider's, and one number covering both would read as a slow
            # provider whenever our own curve was steep.
            delay_before_ms=round(attempt.delay_before * 1000.0, 3),
        )

    return sink
