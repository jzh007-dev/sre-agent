"""Per-investigation append-only JSONL log — the first of §42's four sinks.

One file per investigation, one JSON object per line, appended as things happen.
The audit found that the loop generated a complete event stream and then discarded
it, keeping only the terminal event, so a failed run left nothing behind but an
`Aborted` reason string. This is where the stream lands.

Record kinds, in the order they appear:

    header    the investigation itself: ids, trigger, window, budget, and the
              messages present before the run started
    span      one finished span, `Span.as_dict()` verbatim
    event     one loop event
    outcome   Done or Aborted
    footer    optional cost summary, for callers holding a ledger

Two properties are copied from `llm.cache.FileStore`, for the same reasons:

- **Append-only.** Rewriting a growing document on every write loses everything if
  the process dies mid-write, which is precisely when the log is worth having.
- **A truncated final line is skipped, not fatal.** An interrupted write is an
  expected way for this file to end.

**The cost, measured rather than assumed.** Each record is one open-write-close, so
nothing is ever buffered inside this process and `close()` stays genuinely optional.
On a 3-turn investigation that is 25 appends costing **1.15 ms**, against **0.017 ms**
for the spans themselves — so the durable log is ~98% of the price of traceability,
and the whole of it is 0.0016% of the 90-second p90 latency target. Holding the handle
open with a flush per line would remove most of that; it is not worth a file handle
per in-flight investigation until the QPS makes it worth it.

**Why `rebuild_messages` matters more than it looks.** `messages` *is* the
investigation's state, so a log that can reconstruct it is a log that can resume a
chat, absorb a late alert into a finished run, and give W6-W7 something to
checkpoint. Reconstruction fidelity is therefore asserted as an equality against a
live run in `tests/store/test_jsonl.py` rather than assumed.

Serialization lives here rather than in `core/`: the store is a seam, and the kernel
should not know what a file format is.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import time
from typing import Any, Iterable, Mapping, Sequence

from ..core.events import Aborted, Done, Event
from ..core.investigation import Investigation
from ..core.trace import Span
from ..llm.types import ContentBlock, Message, TextBlock, ToolResultBlock, ToolUseBlock

#: Under `var/` rather than a dotfile: these are artefacts an operator reads during
#: a post-mortem, not a cache.
DEFAULT_ROOT = pathlib.Path("var/investigations")

_BLOCK_TYPES: dict[str, type] = {
    "text": TextBlock,
    "tool_use": ToolUseBlock,
    "tool_result": ToolResultBlock,
}


class InvestigationLog:
    """Writes one investigation's log. Provides both sinks the loop accepts.

    Constructing it writes nothing; `open()` writes the header. The split lets a
    caller decide not to log after all without leaving a headerless file behind.
    """

    def __init__(
        self,
        inv: Investigation,
        root: str | pathlib.Path = DEFAULT_ROOT,
        *,
        now=time.time,
    ) -> None:
        self.inv = inv
        self.root = pathlib.Path(root)
        self.path = self.root / f"{inv.id}.jsonl"
        self._now = now

    # ---- writing ---------------------------------------------------------- #

    def open(self) -> InvestigationLog:
        """Append a header, delimiting a new run within this investigation's file.

        A second header rather than a second file: chat resume and W5's storm
        absorption both run the loop again on one investigation, and one file per
        investigation is what keeps `srectl replay <id>` a single lookup. Successive
        headers are what `split_runs` keys on, so no run id has to be invented.
        """
        self._write(
            {
                "t": "header",
                "ts": self._now(),
                "investigation_id": self.inv.id,
                # Ours, unique per run. The alert's own id sits beside it, not
                # instead of it — see `core/trace.py`.
                "trace_id": self.inv.id,
                "correlation_id": self.inv.correlation_id,
                "trigger": self.inv.trigger,
                "integration": self.inv.integration or "",
                "window": {
                    "start": self.inv.window.start.isoformat(),
                    "end": self.inv.window.end.isoformat(),
                },
                "budget": {
                    "max_turns": self.inv.budget.max_turns,
                    "max_tool_calls": self.inv.budget.max_tool_calls,
                    "max_cost": dict(self.inv.budget.max_cost),
                    "repeat_tool_calls": self.inv.budget.repeat_tool_calls,
                },
                "messages": [message_to_json(m) for m in self.inv.messages],
            }
        )
        return self

    def span(self, span: Span) -> None:
        """A `SpanSink`. Attach to a `Trace`."""
        self._write({"t": "span", **span.as_dict()})

    def event(self, event: Event) -> None:
        """An `EventSink`. Pass as `run_to_completion(on_event=...)`.

        `Done` and `Aborted` are written as `outcome` rather than `event`: the verdict
        is the first record a post-mortem looks for, and making a reader filter two
        event kinds to find it would be a poor trade for one fewer record type.
        """
        if isinstance(event, (Done, Aborted)):
            self._write({"t": "outcome", **event_to_json(event)})
        else:
            self._write({"t": "event", **event_to_json(event)})

    def close(self, ledger_summary: Mapping[str, Any] | None = None) -> None:
        """Optional footer. Cost is on the `llm.call` spans too, so this is a
        convenience for a reader rather than the only copy."""
        record: dict[str, Any] = {"t": "footer", "ts": self._now()}
        if ledger_summary is not None:
            record["ledger"] = dict(ledger_summary)
        self._write(record)

    def _write(self, record: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# ---- reading -------------------------------------------------------------- #


def load(path: str | pathlib.Path) -> list[dict[str, Any]]:
    """Every well-formed record, in file order.

    A malformed line is skipped rather than raised on. The common cause is a write
    interrupted by the same crash that makes the log worth reading.
    """
    file = pathlib.Path(path)
    if not file.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def load_investigation(
    investigation_id: str, root: str | pathlib.Path = DEFAULT_ROOT
) -> list[dict[str, Any]]:
    return load(pathlib.Path(root) / f"{investigation_id}.jsonl")


def logged_investigations(root: str | pathlib.Path = DEFAULT_ROOT) -> list[str]:
    directory = pathlib.Path(root)
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.jsonl"))


def split_runs(records: Sequence[Mapping[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group records into runs, each beginning at a `header`.

    Records before the first header — a log truncated at the front, or one written by
    a caller that skipped `open()` — form a leading group rather than being dropped.
    A partial log is still evidence.
    """
    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for record in records:
        if record.get("t") == "header" and current:
            runs.append(current)
            current = []
        current.append(dict(record))
    if current:
        runs.append(current)
    return runs


def header_of(records: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    for record in records:
        if record.get("t") == "header":
            return dict(record)
    return None


def spans_of(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(r) for r in records if r.get("t") == "span"]


def outcome_of(records: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    for record in records:
        if record.get("t") == "outcome":
            return dict(record)
    return None


def footer_of(records: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    for record in records:
        if record.get("t") == "footer":
            return dict(record)
    return None


def rebuild_messages(records: Sequence[Mapping[str, Any]]) -> list[Message]:
    """Reconstruct `inv.messages` from a run's header and events.

    Rebuilt from the *event stream* rather than from a snapshot, deliberately: a
    snapshot would be self-consistent by construction and would prove nothing about
    the stream, which is what chat resume and storm absorption actually consume.

    **One known divergence, tested rather than merely described**: within a turn,
    text blocks are placed ahead of `tool_use` blocks, because the loop yields every
    `TextDelta` before any `ToolCalled` — it must, since the `max_tokens` check sits
    between them and a `ToolCalled` emitted before that check would announce a
    dispatch that never happens. Providers emit text before tool calls in practice,
    so the rebuild is exact for real content and reorders only interleaved content.
    An empty text block is also dropped, because no event is emitted for one.
    """
    header = header_of(records)
    messages: list[Message] = []
    if header is not None:
        messages.extend(message_from_json(m) for m in header.get("messages", []))

    text: list[ContentBlock] = []
    tool_uses: list[ContentBlock] = []
    results: list[ContentBlock] = []

    def flush() -> None:
        if text or tool_uses:
            messages.append(Message(role="assistant", content=[*text, *tool_uses]))
        if results:
            messages.append(Message(role="user", content=list(results)))
        text.clear()
        tool_uses.clear()
        results.clear()

    for record in records:
        kind = record.get("t")
        if kind == "event":
            name = record.get("kind")
            if name == "TurnStarted":
                flush()
            elif name == "TextDelta":
                text.append(TextBlock(text=record.get("text", "")))
            elif name == "ToolCalled":
                tool_uses.append(
                    ToolUseBlock(
                        id=record.get("tool_use_id", ""),
                        name=record.get("name", ""),
                        input=dict(record.get("input", {})),
                    )
                )
            elif name == "ToolReturned":
                results.append(
                    ToolResultBlock(
                        tool_use_id=record.get("tool_use_id", ""),
                        content=record.get("content", ""),
                        is_error=bool(record.get("is_error")),
                    )
                )
        elif kind == "outcome":
            flush()

    flush()
    return messages


# ---- codecs --------------------------------------------------------------- #


def event_to_json(event: Event) -> dict[str, Any]:
    """`kind` plus the event's own fields.

    `dataclasses.asdict` rather than a branch per type: an event type added in a
    later lesson serializes with no change here.
    """
    return {"kind": type(event).__name__, **dataclasses.asdict(event)}


def message_to_json(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": [dataclasses.asdict(b) for b in message.content],
    }


def message_from_json(record: Mapping[str, Any]) -> Message:
    return Message(
        role=record.get("role", "user"),  # type: ignore[arg-type]
        content=[block_from_json(b) for b in record.get("content", [])],
    )


def block_from_json(record: Mapping[str, Any]) -> ContentBlock:
    cls = _BLOCK_TYPES.get(str(record.get("type")))
    if cls is None:
        # An unknown block type becomes visible text rather than an exception, for
        # the same forward-compatibility reason as `event_from_json`.
        return TextBlock(text=json.dumps(dict(record), ensure_ascii=False))
    known = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in record.items() if k in known and k != "type"})
