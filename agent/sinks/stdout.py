"""Stdout sink — the first place a human sees a report.

Prints the verdict **and the numbers that qualify it**: turns, tool calls, elapsed,
cost, and — the part easy to leave out — `retries` and `fell_back`. A report that does
not say "three provider retries, one fallback" reads identically whether the run was
clean or fought the provider for forty seconds, and those two mean different things for
what to do next.

Every figure comes from the context the harness assembles out of the trace
(`Trace.profile()`, plus the `ledger` attribute the gateway stamps on each `llm.call`
span). Nothing here computes a number, so nothing here can disagree with the log, the
console, or eval about what a run cost.

Plain text rather than JSON: the reader is a person at a terminal, and `srectl replay`
already prints the machine-shaped view.
"""
from __future__ import annotations

import sys
from typing import Any, Mapping, TextIO

from .registry import Delivery

#: Report keys printed first, in this order, when present. Everything else follows
#: alphabetically — so a report that gains a field shows it rather than dropping it,
#: which is the usual failure of a hand-written formatter.
LEAD_KEYS = ("verdict", "first_action", "confidence", "root_cause")


class StdoutSink:
    """Writes to a stream. Real, and the only real sink until W7."""

    name = "stdout"

    def __init__(self, stream: TextIO | None = None) -> None:
        #: Injected so a test can assert the rendered text, and so `srectl` can send it
        #: elsewhere without a second sink existing.
        self._stream = stream

    @property
    def stream(self) -> TextIO:
        # Resolved late rather than bound at construction: `sys.stdout` is replaced by
        # test capture and by `redirect_stdout`, and a sink holding the original object
        # would write straight past both.
        return self._stream if self._stream is not None else sys.stdout

    def deliver(
        self, report: Mapping[str, Any] | None, context: Mapping[str, Any]
    ) -> Delivery:
        text = render(report, context)
        self.stream.write(text + "\n")
        self.stream.flush()
        return Delivery(
            sink=self.name, delivered=True, detail=f"printed {len(text)} chars"
        )


def render(report: Mapping[str, Any] | None, context: Mapping[str, Any]) -> str:
    """The text. Separate from the sink so a test can read it without a stream."""
    lines = [
        "─" * 68,
        f"investigation {context.get('investigation_id', '?')}   "
        f"{context.get('outcome', '?')}",
    ]

    if context.get("correlation_id"):
        lines.append(f"  correlation_id {context['correlation_id']}")
    if context.get("window"):
        lines.append(f"  window         {context['window']}")
    if context.get("failure"):
        # An abort is the case on-call most needs to read, so the reason goes above the
        # body rather than into a footnote.
        lines.append(f"  reason         {context['failure']}")

    lines.append("")
    lines.extend(_report_lines(report))
    lines.append("")
    lines.append(_numbers_line(context))
    lines.append(_spend_line(context))
    lines.append("─" * 68)
    return "\n".join(lines)


def _report_lines(report: Mapping[str, Any] | None) -> list[str]:
    if not report:
        return ["  (no report — see reason above)"]
    ordered = [k for k in LEAD_KEYS if k in report]
    ordered += sorted(k for k in report if k not in LEAD_KEYS)
    width = max(len(k) for k in ordered)
    out: list[str] = []
    for key in ordered:
        value = report[key]
        rendered = (
            ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        )
        first, *rest = rendered.splitlines() or [""]
        out.append(f"  {key.upper().ljust(width)}  {first}")
        out.extend(f"  {' ' * width}  {line}" for line in rest)
    return out


def _numbers_line(context: Mapping[str, Any]) -> str:
    profile: Mapping[str, Any] = context.get("profile") or {}
    parts = [
        f"turns {context.get('turns', '?')}",
        f"tool calls {context.get('tool_calls', '?')}",
        f"elapsed {_ms(profile.get('elapsed_ms'))}",
        f"llm {profile.get('llm_calls', 0)}",
    ]
    # Shown only when non-zero. A line that always reads "retries 0" trains the reader
    # to skip it, which is the opposite of why it is printed.
    if profile.get("retries"):
        parts.append(f"retries {profile['retries']}")
    if profile.get("cache_hits"):
        parts.append(f"cached {profile['cache_hits']}")
    if profile.get("errors"):
        parts.append(f"errors {profile['errors']}")
    return "  " + " · ".join(parts)


def _spend_line(context: Mapping[str, Any]) -> str:
    """Money, per currency, never summed across — costs are not converted.

    A run that made no LLM call reports "0 llm calls" rather than `0.00`: the two are
    the same amount, and only the first says why.
    """
    ledger: Mapping[str, Any] = context.get("ledger") or {}
    spent: Mapping[str, float] = ledger.get("money_spent") or {}
    charged: Mapping[str, float] = ledger.get("budget_charged") or {}
    if not spent and not charged:
        return "  no cost recorded (0 llm calls)"

    money = " ".join(f"{amount:.6f} {ccy}" for ccy, amount in sorted(spent.items()))
    line = f"  spent {money or '0'}"
    # `budget_charged` exceeding `money_spent` means cache hits replayed their original
    # cost — the mechanism that keeps a degraded run degrading on rerun. Showing the gap
    # is what makes that visible instead of looking like an accounting error.
    if charged != spent:
        billed = " ".join(
            f"{amount:.6f} {ccy}" for ccy, amount in sorted(charged.items())
        )
        line += f"   (budget charged {billed})"
    if ledger.get("prices_verified") is False:
        line += "   [prices unverified]"
    return line


def _ms(value: Any) -> str:
    return "?" if value is None else f"{float(value):.1f}ms"


__all__ = ["LEAD_KEYS", "StdoutSink", "render"]
