"""Cost ledger — per-investigation accounting.

[EVAL.md](../../EVAL.md) names the gateway as the sole source of cost, so
this is where one of the project's headline numbers is produced. Two properties
follow from that, and both exist to keep the number honest rather than flattering.

**Two totals, not one.** A cache hit costs no money but still charges the budget
(see `cache.py` and [TRADEOFFS §33](../../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators)
delta 3), so the ledger reports:

- `money_spent` — what was actually paid. This is what eval reports as cost.
- `budget_charged` — what the ceiling was measured against, hits included. This is
  what makes budget-driven degradation reproduce on a rerun.

Reporting one number for both would mean either overstating spend or losing
reproducibility.

**Both are per currency, and there is no conversion.** Costs are reported in whatever
each provider bills in. That means no single scalar total across providers billing
differently — which turns out not to be needed, because the agent runs on one
provider and the judge on another, and those are separate lines in `by_kind` rather
than a sum. See `usage.Cost` for why a conversion would be actively harmful.

**Provenance on every entry.** Each entry records the price table version it was
costed with, and whether that table was verified against published pricing. A
total computed from unverified prices is reported as unverified, not as measured.

"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .usage import PRICE_TABLE_VERSION, Cost, Price, Usage, cache_savings, cost_of


@dataclass(frozen=True)
class CostEntry:
    kind: str
    model_id: str
    provider: str
    usage: Usage
    #: In the provider's billing currency. Never converted.
    cost: Cost
    cached: bool = False
    #: Attempts consumed in transport. >1 means retries happened, which is one of
    #: the few things that explains a latency outlier.
    attempts: int = 1
    fell_back: bool = False
    price_table_version: str = PRICE_TABLE_VERSION
    price_verified: bool = False
    #: The date past which this entry's price table should be re-checked. Carried on
    #: the entry so a historical cost stays interpretable after the table moves on.
    price_stale_after: str = ""
    cache_savings: Cost = field(default_factory=lambda: Cost(native=0.0))

    @property
    def currency(self) -> str:
        return self.cost.currency


@dataclass
class Ledger:
    """Cost accounting for one investigation."""

    investigation_id: str
    entries: list[CostEntry] = field(default_factory=list)

    def record(
        self,
        *,
        kind: str,
        model_id: str,
        provider: str,
        usage: Usage,
        price: Price,
        cached: bool = False,
        attempts: int = 1,
        fell_back: bool = False,
        cost: Cost | None = None,
    ) -> CostEntry:
        """Append an entry.

        `cost` is passed explicitly on a cache replay, where the authoritative figure
        is what the *original* call cost at the rate and FX that applied then —
        recomputing it from the current table would silently reprice history.
        """
        entry = CostEntry(
            kind=kind,
            model_id=model_id,
            provider=provider,
            usage=usage,
            cost=cost_of(usage, price) if cost is None else cost,
            cached=cached,
            attempts=attempts,
            fell_back=fell_back,
            price_table_version=price.as_of,
            price_verified=price.verified,
            price_stale_after=price.stale_after,
            cache_savings=cache_savings(usage, price),
        )
        self.entries.append(entry)
        return entry

    @property
    def money_spent(self) -> dict[str, float]:
        """What was actually paid, per billing currency — cache hits excluded.

        A dict rather than a scalar because two providers may bill differently, and
        adding those amounts would produce a number that is a cost in neither.
        """
        return self._totals(include_cached=False)

    @property
    def budget_charged(self) -> dict[str, float]:
        """What the ceiling is measured against, per currency — cache hits included,
        so a run that degraded on budget degrades identically on rerun."""
        return self._totals(include_cached=True)

    def _totals(self, *, include_cached: bool) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for entry in self.entries:
            if include_cached or not entry.cached:
                totals[entry.currency] += entry.cost.native
        return dict(totals)

    def spent_in(self, currency: str) -> float:
        """Budget-charged amount in one currency. What the gateway's gate compares."""
        return self.budget_charged.get(currency, 0.0)

    @property
    def currencies(self) -> set[str]:
        return {e.currency for e in self.entries}

    @property
    def usage_total(self) -> Usage:
        total = Usage()
        for entry in self.entries:
            total = total + entry.usage
        return total

    @property
    def calls(self) -> int:
        return len(self.entries)

    @property
    def cached_calls(self) -> int:
        return sum(1 for e in self.entries if e.cached)

    @property
    def total_attempts(self) -> int:
        """Includes retries, so `total_attempts > calls` means the transport worked."""
        return sum(e.attempts for e in self.entries)

    @property
    def fell_back(self) -> bool:
        """True if any call ran on a fallback provider.

        A run with this set is **excluded from model comparison** in eval: its
        accuracy is attributable to no single model and its cost mixes two price
        sheets. See [TRADEOFFS §33](../../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators) delta 9.
        """
        return any(e.fell_back for e in self.entries)

    @property
    def fully_verified_prices(self) -> bool:
        return all(e.price_verified for e in self.entries) if self.entries else True

    def prices_stale(self, today: str) -> bool:
        """Whether any entry was priced with a table that has aged out.

        `today` is a parameter so a cost report stays a pure function of its inputs —
        the same ledger must not describe itself differently tomorrow.
        """
        return any(e.price_stale_after and today > e.price_stale_after for e in self.entries)

    @property
    def price_table_versions(self) -> set[str]:
        """Distinct tables involved. More than one means a price edit landed
        mid-run, and the total mixes rates — worth flagging rather than summing
        quietly."""
        return {e.price_table_version for e in self.entries}

    def by_kind(self) -> dict[str, dict[str, float]]:
        """Money by `CallKind`, then by currency. The breakdown the interview
        checklist asks for ("main loop A, refute B, judge C").

        Nested rather than flat because the judge deliberately runs on a different
        provider family than the agent, and that provider may bill in a different
        currency. Flattening would require the conversion this design removed.
        """
        out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for entry in self.entries:
            if not entry.cached:
                out[entry.kind][entry.currency] += entry.cost.native
        return {kind: dict(by_ccy) for kind, by_ccy in out.items()}

    def summary(self) -> dict[str, object]:
        """One flat dict for the trace, the JSONL log, and eval metrics."""
        return {
            "investigation_id": self.investigation_id,
            "calls": self.calls,
            "cached_calls": self.cached_calls,
            "total_attempts": self.total_attempts,
            "money_spent": {c: round(v, 6) for c, v in self.money_spent.items()},
            "budget_charged": {c: round(v, 6) for c, v in self.budget_charged.items()},
            "cache_savings": {
                c: round(v, 6) for c, v in self._savings_by_currency().items()
            },
            "by_kind": {
                kind: {c: round(v, 6) for c, v in by_ccy.items()}
                for kind, by_ccy in self.by_kind().items()
            },
            "input_tokens": self.usage_total.input_tokens,
            "output_tokens": self.usage_total.output_tokens,
            "cache_read_tokens": self.usage_total.cache_read_tokens,
            "fell_back": self.fell_back,
            "currencies": sorted(self.currencies),
            "mixed_currencies": len(self.currencies) > 1,
            "prices_verified": self.fully_verified_prices,
            "price_table_versions": sorted(self.price_table_versions),
            "mixed_price_tables": len(self.price_table_versions) > 1,
        }

    def _savings_by_currency(self) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for entry in self.entries:
            totals[entry.cache_savings.currency] += entry.cache_savings.native
        return dict(totals)
