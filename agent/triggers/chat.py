"""Chat trigger — a human asking, over a resumable investigation.

A **stub in scope, not in shape**: it normalises to a real `Investigation` and
resumes a real one, and it deliberately does no intent recognition, no runbook
lookup, and no dedup. What it exists to prove is the part that would be expensive to
retrofit, and that part is already load-bearing:

- `messages` belongs to the `Investigation`, so a second question appends and the
  loop runs again over the same state (W2 L2's reason for moving it there);
- `requires_report` is False for chat, so answering and stopping is a legitimate
  `Done` rather than `Aborted("no_report")` — the first place the trigger-agnostic
  design actually paid;
- **no dedup layer.** A human asking the same question twice is asking twice.
  Semantic reuse across sessions may one day *offer* an existing report and may never
  suppress the question — [TRADEOFFS §41](../../TRADEOFFS.md#41-semantic-similarity-may-inform-it-may-never-suppress) —
  and it needs embeddings, so it lands in W4.

The window is anchored on *now* rather than on an incident start, because a question
has no T0. That is the one place chat genuinely differs from alert below the trigger
layer, and it is why `Investigation.window` is set by the trigger rather than by the
harness.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, MutableMapping

from ..core.investigation import Investigation, Window, mint_id
from .policy import BudgetTiers, load_budgets
from .registry import TriggerOutcome

#: How far back a question looks by default. Shorter than an alert's 30 minutes on
#: purpose: "why is checkout slow" means now, and a wide window is a wide bill.
DEFAULT_LOOKBACK = timedelta(minutes=15)


class ChatTrigger:
    """Entry mode ②: a person. One session, one investigation, many turns."""

    kind = "chat"

    #: A chat answer goes back to the person who asked, not to an alerting surface —
    #: paging a channel because someone asked a question is how a copilot gets muted.
    #: Streaming back to the caller is the real delivery (post-W5); `stdout` is what a
    #: CLI session sees today.
    sinks = ("stdout",)

    def __init__(
        self,
        *,
        budgets: BudgetTiers | None = None,
        store: MutableMapping[str, Investigation] | None = None,
        now: Callable[[], datetime] | None = None,
        integration: str | None = None,
    ) -> None:
        self.budgets = budgets or load_budgets()
        #: session id → investigation. Keyed on the *session*, not on the
        #: investigation id, because a session id is what a chat client has in hand.
        self.store: MutableMapping[str, Investigation] = {} if store is None else store
        self._now = now or _utcnow
        self.integration = integration

    def preprocess(self, payload: Mapping[str, Any]) -> TriggerOutcome:
        """`{"session_id": ..., "text": ...}` → a new or resumed investigation.

        A resume is reported as `joined`, exactly like an alert absorbed by dedup R1:
        both mean "this input went into an investigation that already existed", and
        nothing downstream of the trigger layer needs to care which one produced it.
        """
        session = str(payload.get("session_id") or "").strip()
        text = str(payload.get("text") or "")

        existing = self.store.get(session) if session else None
        if existing is not None:
            existing.add_user_text(text)
            return TriggerOutcome(joined=[existing])

        inv = Investigation(
            id=mint_id(),
            trigger="chat",
            window=Window.around(self._now(), lookback=DEFAULT_LOOKBACK),
            budget=self.budgets.for_trigger("chat"),
            integration=self.integration,
        )
        inv.add_user_text(text)
        if session:
            self.store[session] = inv
        return TriggerOutcome(investigations=[inv])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["ChatTrigger", "DEFAULT_LOOKBACK"]
