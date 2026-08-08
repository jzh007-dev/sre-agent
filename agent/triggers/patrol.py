"""Patrol trigger — one schedule, N targets, N budgets.

**Formally deferred, and kept honest about it** ([TRADEOFFS §31](../../TRADEOFFS.md#31-patrol-stays-a-stub-until-its-value-proposition-is-settled),
ROADMAP open gap #7): "the loop on a cron" is just slower alerting, and the
differentiator implies different tools and a different output shape. Until that is
settled, patrol stays the shape and none of the behaviour.

The stub has already paid for itself twice, which is the argument for keeping it:

- **it forced budgets to be per-investigation.** Fifty targets sharing one ceiling
  would have meant the fiftieth target got nothing, and discovering that after the
  gateway was built would have been invasive;
- **it forced the loop to yield events**, because a fan-out with no progress stream
  is a fan-out you cannot watch.

So the one thing implemented here is the fan-out: a scope becomes N independent
`Investigation`s at the patrol tier, not one investigation with N times the budget.
No LLM call, no scheduling, no scope *discovery* — the target list is passed in.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from ..core.investigation import Investigation, Window, mint_id
from .policy import BudgetTiers, load_budgets
from .registry import TriggerOutcome

#: A sweep looks at the recent past, not at an incident's run-up.
DEFAULT_LOOKBACK = timedelta(minutes=10)


class PatrolTrigger:
    """Entry mode ③: a schedule. One delivery, one investigation per target."""

    kind = "patrol"

    #: A sweep's findings are a digest, not N pages. Aggregation across the fan-out is
    #: post-W5 work; until then each target's result prints, which is honest about the
    #: fact that patrol has no aggregation yet rather than hiding it behind a sink name.
    sinks = ("stdout",)

    def __init__(
        self,
        *,
        budgets: BudgetTiers | None = None,
        now: Callable[[], datetime] | None = None,
        integration: str | None = None,
    ) -> None:
        self.budgets = budgets or load_budgets()
        self._now = now or _utcnow
        self.integration = integration

    def preprocess(self, payload: Mapping[str, Any]) -> TriggerOutcome:
        """`{"targets": ["checkout", "payment"], "question": ...}` → one per target.

        All targets share one pinned `Window`, so a sweep is internally comparable:
        two targets examined ninety seconds apart otherwise read different data and
        "checkout looks worse than payment" becomes a statement about the clock.
        """
        targets = _targets(payload)
        question = str(payload.get("question") or "").strip() or _DEFAULT_QUESTION
        window = Window.around(self._now(), lookback=DEFAULT_LOOKBACK)
        budget = self.budgets.for_trigger("patrol")

        outcome = TriggerOutcome()
        for target in targets:
            inv = Investigation(
                id=mint_id(),
                trigger="patrol",
                window=window,
                budget=budget,
                integration=self.integration,
            )
            inv.add_user_text(
                f"<patrol target=\"{target}\">\n{question}\n</patrol>"
            )
            outcome.investigations.append(inv)
        return outcome


_DEFAULT_QUESTION = (
    "Is anything wrong with this service right now that has not paged? "
    "Report nothing if nothing is wrong."
)


def _targets(payload: Mapping[str, Any]) -> list[str]:
    raw = payload.get("targets")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, Sequence):
        return [str(t) for t in raw if str(t)]
    return []


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["DEFAULT_LOOKBACK", "PatrolTrigger"]
