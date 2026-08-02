"""Investigation — the central noun of the system.

An investigation is one budgeted enquiry. An incident is one *kind* of
investigation; a chat session and a single patrol target are two others (see
[TRADEOFFS §25](../../TRADEOFFS.md)). Calling it "incident" would have welded the
system to one of its three entry modes.

`messages` lives here rather than inside the loop function, and that one move
buys four things that are each expensive to retrofit:

- **chat can resume** — append a user message and run the loop again,
- **alert storms can be absorbed** — a later correlated alert appends into an
  in-flight investigation instead of forking a fourth one (W5),
- **the JSONL store has something to serialize** (W2 L7),
- **a future Temporal migration has something to checkpoint** (W6-W7).
"""
from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping

from ..llm.types import Message, TextBlock

TriggerKind = Literal["alert", "chat", "patrol"]

#: How far either side of T0 an investigation looks by default. Lookback covers
#: the run-up to the alert; the small lookahead covers alert propagation delay
#: and the `for:` duration a rule waited before firing.
DEFAULT_LOOKBACK = timedelta(minutes=30)
DEFAULT_LOOKAHEAD = timedelta(minutes=5)

#: Triggers whose investigations must finish by calling a terminal tool. Chat is
#: excluded: answering a question and stopping is a legitimate ending there.
REPORT_REQUIRED_TRIGGERS: frozenset[str] = frozenset({"alert", "patrol"})


@dataclass(frozen=True)
class Window:
    """The pinned time range every tool call inherits.

    Without this, tools query "now" while the incident began at T0 — so an agent
    reaching a metric eight minutes late may see a signal that already
    recovered, and the same golden case rerun ten minutes later reads different
    data. That second consequence is fatal to [EVAL.md](../../EVAL.md)'s
    reproducibility principle: a case would not be comparable to itself.

    Loadout computes the window once; `safe_dispatch` passes it to every tool as
    a reserved keyword. It deliberately never appears in any tool's
    `input_schema`, so the model cannot widen or move it.
    """

    start: datetime
    end: datetime

    @classmethod
    def around(
        cls,
        t0: datetime,
        lookback: timedelta = DEFAULT_LOOKBACK,
        lookahead: timedelta = DEFAULT_LOOKAHEAD,
    ) -> Window:
        return cls(start=t0 - lookback, end=t0 + lookahead)

    def as_tool_args(self) -> dict[str, str]:
        """ISO-8601 form for passing into a query backend."""
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def __str__(self) -> str:  # shows up in prompts and traces
        return f"{self.start.isoformat()} .. {self.end.isoformat()}"


@dataclass(frozen=True)
class ToolBudget:
    """Ceilings the code sets and the model spends against.

    The model chooses *what* to spend on and in *what order*; it never sets or
    raises these.

    `max_cost` is **per currency** because costs are never converted — a provider
    billing in CNY is gated by the CNY ceiling. A currency with no ceiling raises at
    the gate rather than running unbounded, so a newly-added provider cannot quietly
    escape the budget.
    """

    max_turns: int = 15
    max_tool_calls: int = 40
    #: currency → ceiling. Defaults cover the two currencies this project bills in.
    max_cost: Mapping[str, float] = field(
        default_factory=lambda: {"USD": 0.40, "CNY": 3.00}
    )
    #: Per-tool caps, e.g. {"query_logs": 6}. Absent tool names are uncapped.
    per_tool_calls: Mapping[str, int] = field(default_factory=dict)

    def ceiling_for(self, currency: str) -> float | None:
        """The ceiling in one currency, or None if none is configured."""
        return self.max_cost.get(currency)


@dataclass
class Investigation:
    """One budgeted enquiry, and the conversation that constitutes its state."""

    id: str
    trigger: TriggerKind
    window: Window
    budget: ToolBudget = field(default_factory=ToolBudget)
    #: Which integration's tool bundle and runbook namespace apply. Resolved by
    #: the integration registry in W2 L5; None means "not yet routed".
    integration: str | None = None
    messages: list[Message] = field(default_factory=list)
    turn: int = 0
    tool_calls: Counter[str] = field(default_factory=Counter)

    @property
    def requires_report(self) -> bool:
        """Whether ending without a terminal tool call counts as a failure."""
        return self.trigger in REPORT_REQUIRED_TRIGGERS

    def add_user_text(self, text: str) -> None:
        """Append a user turn.

        This is the chat resume path and, in W5, the alert-storm absorption path
        — a correlated alert arriving mid-flight is just another user message on
        an existing investigation.
        """
        self.messages.append(Message(role="user", content=[TextBlock(text=text)]))

    def record_tool_call(self, name: str) -> None:
        self.tool_calls[name] += 1

    def budget_exhausted(self) -> str | None:
        """Return a human-readable reason if any ceiling is hit, else None.

        Returning the reason rather than a bool is deliberate: the reason ends up
        verbatim in the degraded report, so the operator learns *which* limit
        stopped the investigation.
        """
        total = sum(self.tool_calls.values())
        if total >= self.budget.max_tool_calls:
            return f"tool call ceiling reached ({total}/{self.budget.max_tool_calls})"
        for name, cap in self.budget.per_tool_calls.items():
            used = self.tool_calls[name]
            if used >= cap:
                return f"per-tool ceiling reached for {name} ({used}/{cap})"
        return None

    @classmethod
    def from_alert(
        cls,
        alert: Mapping[str, Any],
        *,
        t0: datetime | None = None,
        budget: ToolBudget | None = None,
        integration: str | None = None,
    ) -> Investigation:
        """Build an alert-triggered investigation from a webhook payload.

        Relocates to `triggers/alert.py` in W2 L4, where fingerprint dedup and
        severity-to-budget-tier mapping join it. It lives here for L2 so the loop
        has something real to run against.

        `t0` is the incident's start, not the current time — the window derives
        from when the alert fired. Real AlertManager payloads carry `startsAt`;
        the golden fixtures are static and have none, so callers pass `t0`
        explicitly and the wall clock is only a last resort.
        """
        anchor = t0 or _alert_start_time(alert) or datetime.now(timezone.utc)
        inv = cls(
            id=f"inv_{uuid.uuid4().hex[:12]}",
            trigger="alert",
            window=Window.around(anchor),
            budget=budget or ToolBudget(),
            integration=integration,
        )
        inv.add_user_text(f"<alert>\n{json.dumps(strip_fixture_metadata(alert), ensure_ascii=False, indent=2)}\n</alert>")
        return inv


def strip_fixture_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Drop keys beginning with `_` before a payload can reach the model.

    Not cosmetic. Every golden case's `alert.json` carries a `_meta.purpose`
    written for human readers, and those descriptions *state the root cause*
    ("Redis bgsave failed under memory pressure, session writes rejected").
    Passing the payload through verbatim would hand the agent the answer and
    silently invalidate every accuracy number the eval suite produces — a
    failure that looks like success, which is the worst kind.

    The rule is structural rather than a `_meta` special case: anything
    underscore-prefixed is fixture bookkeeping and never crosses into a prompt.
    """
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def _alert_start_time(alert: Mapping[str, Any]) -> datetime | None:
    """Best-effort T0 from an AlertManager-shaped payload."""
    raw = alert.get("startsAt")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
