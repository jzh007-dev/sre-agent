"""Loop events — what the loop emits as it runs.

The loop is an async generator rather than a function that returns a string.
Three callers need that:

- **chat** streams text back to a human who is waiting,
- **patrol** fans out over N targets and needs progress from each one,
- **the JSONL store** (W2 L7) appends as things happen instead of reconstructing
  the run afterwards.

[ARCHITECTURE](../../ARCHITECTURE.md) already claimed "a `while` loop naturally
yields events" as an advantage of the loop shape over a phase graph. Until this
module existed that claim was not actually true of the code.

`run_to_completion()` in `loop.py` collapses the stream back to a single outcome,
so alert mode pays nothing for the machinery chat needs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union


@dataclass(frozen=True)
class TurnStarted:
    """A new loop iteration began. `turn` is zero-based."""

    turn: int


@dataclass(frozen=True)
class TextDelta:
    """A chunk of assistant text.

    Under `StubLLM` — and under any non-streaming adapter — one `TextDelta`
    carries a whole text block. The type is delta-shaped anyway so that when the
    gateway gains real SSE streaming in L3, no consumer has to change.
    """

    text: str


@dataclass(frozen=True)
class ToolCalled:
    """The model asked for a tool. Emitted before dispatch, so a consumer can
    show "querying metrics…" while the call is in flight."""

    tool_use_id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolReturned:
    """A tool finished. `is_error` is True for failures the loop contained —
    unknown tool, timeout, exception — which are results, not crashes.

    Named `ToolReturned` rather than `ToolResult` to stay distinguishable from
    `llm.types.ToolResultBlock`, which is the wire format this reports on.
    """

    tool_use_id: str
    name: str
    is_error: bool
    content: str


@dataclass(frozen=True)
class Done:
    """The investigation delivered.

    `report` holds the terminal tool's payload, which is how alert and patrol
    investigations finish. For chat, a plain answer is a legitimate ending, so
    `report` is None and `text` carries it instead.

    W3 L5 replaces the raw dict with a validated `Report` object.
    """

    report: dict[str, Any] | None = None
    text: str | None = None


@dataclass(frozen=True)
class Aborted:
    """The investigation ended without delivering.

    Always yielded, never raised. An abort still carries everything gathered so
    far in `inv.messages`, so the harness can emit a partial report naming what
    it could not establish — which beats an exception that discards the work.
    """

    reason: str
    detail: str = ""


Event = Union[TurnStarted, TextDelta, ToolCalled, ToolReturned, Done, Aborted]

#: The two events that end a run. Exactly one of them is emitted per run.
TERMINAL_EVENTS = (Done, Aborted)
