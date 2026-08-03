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

import hashlib
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

#: How much of a tool result is retained for the repeat guard to hand back. Enough
#: to be useful, short enough that keeping one per distinct call costs nothing.
PREVIOUS_RESULT_CHARS = 2_000


def args_hash(arguments: Mapping[str, Any]) -> str:
    """A stable short digest of a tool call's arguments.

    Two calls are "the same call" when the tool and this digest match. `sort_keys`
    is what makes that true regardless of the order the model emitted the keys in,
    and `default=str` keeps an unexpected value type from raising inside the guard —
    a hash that occasionally over-distinguishes is a missed detection, but one that
    raises would take down the dispatch path this exists to protect.

    Twelve hex characters: collision-irrelevant at forty calls per investigation, and
    short enough to read in a span attribute or a replay tree.
    """
    blob = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


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
    #: The model-side circuit breaker's threshold: which *identical* call, counting
    #: from one, is refused. 3 means two identical calls run and the third does not.
    #: A ceiling like the others, so it belongs here — but unlike the others it
    #: degrades the call rather than ending the investigation. See `repeat_guard`.
    repeat_tool_calls: int = 3

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
    #: The observed system's own id for this incident, adopted from the alert rather
    #: than invented. Week 1 already propagates it by header and stamps it on every
    #: JSON log line, so carrying it makes "the agent's fourth query was slow"
    #: answerable from ClickHouse's own logs. Empty for chat and patrol.
    #:
    #: Deliberately *not* the investigation id: reruns of one golden case, and
    #: L4b's R3 rule, both produce several investigations sharing one correlation
    #: id. `id` identifies our enquiry; this identifies their incident.
    correlation_id: str = ""
    #: (tool_name, args_hash) → count. The name-keyed counter above cannot tell
    #: twelve different queries from the same query twelve times, which is the gap
    #: [TRADEOFFS §42](../../TRADEOFFS.md#42-traceability-one-id-four-sinks--and-an-honest-audit-of-what-is-currently-wired)
    #: records: a runaway already stops, but the recorded reason says `max_turns`
    #: either way.
    repeat_calls: Counter[tuple[str, str]] = field(default_factory=Counter)
    #: (tool_name, args_hash) → a truncated copy of what that call returned, so a
    #: refusal can hand the previous answer back instead of just saying no.
    _results: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)

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

    def record_tool_call(
        self, name: str, args_hash: str = "", result: str | None = None
    ) -> None:
        """Count a dispatched call, by name for the budget and by (name, args) for
        the repeat guard.

        `result` is kept truncated: it exists only to be handed back on a refusal,
        and a full ClickHouse page per distinct call would multiply the memory the
        investigation holds for no benefit. Bounded overall by `max_tool_calls`.
        """
        self.tool_calls[name] += 1
        if args_hash:
            key = (name, args_hash)
            self.repeat_calls[key] += 1
            if result is not None:
                self._results[key] = result[:PREVIOUS_RESULT_CHARS]

    def repeat_guard(self, name: str, args_hash: str) -> str | None:
        """The refusal payload if this exact call has been made too often, else None.

        Symmetric with the provider circuit breaker by design, and deliberately
        *recoverable* where that one is fatal: rather than aborting, the call comes
        back as an error result carrying the previous answer and a nudge. Same
        pattern as `safe_dispatch` turning a failed backend into evidence — the
        model can route around it on its next turn, and an investigation that was
        merely stuck does not become an investigation that failed.

        Returns JSON, matching `dispatch._error`, so the model reads structure
        rather than prose.
        """
        limit = self.budget.repeat_tool_calls
        if limit <= 0:
            return None
        key = (name, args_hash)
        already = self.repeat_calls[key]
        if already + 1 < limit:
            return None
        previous = self._results.get(key)
        payload: dict[str, Any] = {
            "error": "repeated identical call",
            "tool": name,
            "args_hash": args_hash,
            "identical_calls": already + 1,
            "hint": (
                f"you have already called {name} with identical arguments "
                f"{already} time(s); the result has not changed. Use the previous "
                f"result below, or query something different."
            ),
        }
        if previous is not None:
            payload["previous_result"] = previous
        return json.dumps(payload, ensure_ascii=False)

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
            correlation_id=_alert_correlation_id(alert),
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


def _alert_correlation_id(alert: Mapping[str, Any]) -> str:
    """The alert's own correlation id, from `commonAnnotations`.

    Read from the annotation rather than recomputed, for the same reason the
    fingerprint will be taken from AlertManager in L4b: a second definition of an
    identifier is a second thing that can disagree. Every Week-1 golden fixture
    carries one, and `mock/services/_shared/observability.py` propagates the same id
    by header, which is what makes the join possible at all.

    Empty when absent — the trace still works, it just cannot be joined to the
    observed system's logs, and that is worth reading as a missing annotation on the
    alert rule rather than as an agent-side failure.
    """
    annotations = alert.get("commonAnnotations")
    if isinstance(annotations, Mapping):
        value = annotations.get("correlation_id")
        if isinstance(value, str):
            return value
    return ""


def _alert_start_time(alert: Mapping[str, Any]) -> datetime | None:
    """Best-effort T0 from an AlertManager-shaped payload."""
    raw = alert.get("startsAt")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
