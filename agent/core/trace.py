"""Spans and durations — the sink the rest of the system was missing.

The audit in [TRADEOFFS §42](../../TRADEOFFS.md#42-traceability-one-id-four-sinks--and-an-honest-audit-of-what-is-currently-wired)
found four instrument sources and zero sinks, and no timing anywhere in `agent/`.
Without durations, `p90 latency`, `time_to_first_verdict` and the critical-path
profile are declared [EVAL.md](../../EVAL.md) metrics that cannot be computed, and
a failed run leaves nothing behind but an `Aborted` reason string.

Four span levels:

    investigation                       trigger, correlation_id, outcome
    ├─ turn 0                           stop_reason, tool calls
    │  ├─ llm.call                      model, cache_hit, tokens, cost, duration
    │  │  └─ attempt                    error_class, delay_before, duration
    │  └─ tool.call                     name, args_hash, is_error, duration
    └─ …

**The trace id is ours; theirs is an attribute.** `trace_id` is the investigation
id, which is unique per run. The alert's `correlation_id` — the spine Week 1
already ships, propagated by header and stamped on every JSON log line by
`mock/services/_shared/observability.py` — rides along as an attribute on every
span. So the join is still one grep (*"the agent's fourth query was slow, and here
is what ClickHouse was doing"*), while two runs of one golden case remain two
traces. Adopting the correlation_id *as* the trace id would collide on rerun, and
L4b's R3 rule deliberately mints a second investigation for one fingerprint.

**Parenting is ambient, via ContextVar.** That is what lets the gateway nest an
`llm.call` under the current turn without the `LLM` protocol growing a parameter —
the same reasoning that kept streaming a side channel. It also mirrors the mock,
which uses a ContextVar as an async-safe MDC for exactly this.

Two Python details, either of which silently corrupts the nesting if ignored:

1. `asyncio.gather` wraps coroutines in Tasks, and each Task copies the current
   context. So concurrent `tool.call` spans cannot corrupt each other's parent —
   which is *why* a ContextVar is correct here and a plain attribute is not.
2. The `investigation` and `turn` spans stay open across `yield` inside an async
   generator, and an async generator runs in its **caller's** context. A
   `reset(token)` would then raise `ValueError` if the consumer drives the
   generator from a different context, or abandons it so the span closes during
   `aclose()`/GC. So a span restores the previous parent by `set(previous)`, never
   by `reset(token)`. See `_parent_scope`.

`NULL_TRACE` is what `current_trace()` returns when nothing is installed, and its
`span()` does nothing at all. Tracing off therefore costs one ContextVar read per
span site, which is what keeps the overhead claim in §42 true.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Sequence

#: Span level names. Strings rather than an enum because a sink serializes them and
#: W3 L3 hands them to OTEL, which wants names.
INVESTIGATION = "investigation"
TURN = "turn"
LLM_CALL = "llm.call"
TOOL_CALL = "tool.call"
ATTEMPT = "attempt"

Clock = Callable[[], float]


@dataclass
class Span:
    """One timed unit of work.

    `attrs` is deliberately an open dict rather than a typed payload per level: the
    gateway's trace payload is already sixteen keys and grows with every
    provenance stamp EVAL keys reproducibility on, and a sink either serializes the
    lot or is not a sink.
    """

    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    #: Wall-clock epoch seconds — what a human correlates against a log line.
    started_at: float
    #: Monotonic reading, used for the duration. Separate from `started_at`
    #: because the wall clock can step and `perf_counter` cannot.
    started_mono: float
    duration_ms: float | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    #: "ok" | "error" | "abandoned". Set from the exception path, so a span that
    #: never closed cleanly says so instead of looking merely fast.
    status: str = "ok"
    #: The alert's own id, carried on every span so the join to the observed
    #: system's logs needs no lookup table.
    correlation_id: str = ""

    def set(self, **attrs: Any) -> None:
        """Add attributes discovered while the span was open.

        Most of a span's interesting attributes — token counts, `is_error`, the
        served model — are only known on the way out.
        """
        self.attrs.update(attrs)

    def as_dict(self) -> dict[str, Any]:
        """Flat form for a JSONL line, a log record, or Langfuse."""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "started_at": round(self.started_at, 6),
            "duration_ms": None if self.duration_ms is None else round(self.duration_ms, 3),
            "status": self.status,
            "correlation_id": self.correlation_id,
            **self.attrs,
        }


#: Where a finished span goes. A plain callable, so a sink needs no base class and
#: the trace payload stays assertable in tests with `spans.append`.
SpanSink = Callable[[Span], None]


@dataclass
class Trace:
    """One run's span tree, and the sinks its finished spans are fanned out to.

    Span ids are a per-trace counter (`s0`, `s1`, …) rather than random: replay
    output then diffs cleanly across runs, and the ids are readable in a JSONL file
    a human is grepping mid-incident.
    """

    trace_id: str
    correlation_id: str = ""
    sinks: list[SpanSink] = field(default_factory=list)
    #: Injected so a test can assert exact durations, matching how `transport.py`
    #: injects `sleeper` and `breaker.now` to keep its policy paths offline.
    clock: Clock = time.perf_counter
    wall: Clock = time.time

    seq: int = 0
    #: Every span this trace has closed, in completion order. Bounded by the turn
    #: and tool-call ceilings, so this cannot grow without limit.
    spans: list[Span] = field(default_factory=list)

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        """Open a span parented to whatever is currently open.

        The span is emitted to every sink on the way out, including when the body
        raises — a span that only appears on success is useless for exactly the
        runs worth investigating.
        """
        span = Span(
            trace_id=self.trace_id,
            span_id=f"s{self.seq}",
            parent_id=current_parent(),
            name=name,
            started_at=self.wall(),
            started_mono=self.clock(),
            attrs=dict(attrs),
            correlation_id=self.correlation_id,
        )
        self.seq += 1

        with _parent_scope(span.span_id):
            try:
                yield span
            except GeneratorExit:
                # The consumer abandoned an async generator mid-run. Recorded as its
                # own status rather than as an error: nothing failed, but the
                # duration is not a measurement of anything.
                span.status = "abandoned"
                raise
            except BaseException as exc:
                span.status = "error"
                span.attrs.setdefault("error", f"{type(exc).__name__}: {exc}")
                raise
            finally:
                span.duration_ms = (self.clock() - span.started_mono) * 1000.0
                self._emit(span)

    def record(
        self,
        name: str,
        *,
        duration_ms: float,
        status: str = "ok",
        **attrs: Any,
    ) -> Span:
        """Record a span whose duration was measured somewhere else.

        `attempt` spans need this: transport times each send, and re-timing the
        callback that reports it would measure the callback. Parenting still comes
        from the ambient context, so an attempt reported from inside an `llm.call`
        lands under it.
        """
        span = Span(
            trace_id=self.trace_id,
            span_id=f"s{self.seq}",
            parent_id=current_parent(),
            name=name,
            started_at=self.wall(),
            started_mono=self.clock(),
            duration_ms=duration_ms,
            attrs=dict(attrs),
            status=status,
            correlation_id=self.correlation_id,
        )
        self.seq += 1
        self._emit(span)
        return span

    def _emit(self, span: Span) -> None:
        self.spans.append(span)
        for sink in self.sinks:
            # A broken sink must not end an investigation. Losing telemetry is
            # bad; losing the incident response because telemetry broke is worse.
            try:
                sink(span)
            except Exception:  # noqa: BLE001
                logging.getLogger(_LOGGER_NAME).exception("trace sink failed")

    @contextmanager
    def install(self) -> Iterator[Trace]:
        """Make this the ambient trace, so downstream spans nest under it."""
        with _trace_scope(self):
            yield self

    # ---- derived numbers -------------------------------------------------- #

    def total_ms(self, name: str) -> float:
        """Summed duration of every span at one level.

        Note this is *summed*, not wall-clock: concurrent tool calls overlap, so
        `total_ms(TOOL_CALL)` can exceed the root span's duration. That is the
        honest reading — it measures work done, and the root measures elapsed.
        """
        return sum(s.duration_ms or 0.0 for s in self.spans if s.name == name)

    def root(self) -> Span | None:
        for span in self.spans:
            if span.parent_id is None:
                return span
        return None

    def profile(self) -> dict[str, Any]:
        """The critical-path profile §42 says is currently uncomputable."""
        root = self.root()
        elapsed = (root.duration_ms or 0.0) if root else 0.0
        llm_ms = self.total_ms(LLM_CALL)
        tool_ms = self.total_ms(TOOL_CALL)
        attempts = [s for s in self.spans if s.name == ATTEMPT]
        llm_spans = [s for s in self.spans if s.name == LLM_CALL]
        return {
            "elapsed_ms": round(elapsed, 3),
            "llm_ms": round(llm_ms, 3),
            "tool_ms": round(tool_ms, 3),
            # What is left is our own overhead: parsing, dispatch, accounting.
            "overhead_ms": round(max(0.0, elapsed - llm_ms - tool_ms), 3),
            "llm_share": round(llm_ms / elapsed, 4) if elapsed else 0.0,
            "tool_share": round(tool_ms / elapsed, 4) if elapsed else 0.0,
            "llm_calls": len(llm_spans),
            "tool_calls": sum(1 for s in self.spans if s.name == TOOL_CALL),
            "attempts": len(attempts),
            # attempts > llm_calls means transport retried, which is one of the few
            # things that explains a latency outlier. The count alone does not
            # distinguish "the provider was slow" from "we tried three times".
            "retries": max(0, len(attempts) - len(llm_spans)),
            "cache_hits": sum(1 for s in llm_spans if s.attrs.get("cache_hit")),
            "errors": sum(1 for s in self.spans if s.status == "error"),
        }


class _NullTrace(Trace):
    """The ambient trace when none is installed.

    Exists so every call site can be an unconditional `with span(...)` — the
    alternative is an `if tracing:` at five sites, which is how coverage becomes
    "remembered" rather than structural.
    """

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        yield _DISCARD

    def record(self, name: str, **kwargs: Any) -> Span:
        return _DISCARD

    def _emit(self, span: Span) -> None:  # pragma: no cover — never reached
        return None


class _DiscardSpan(Span):
    """The span handed out with tracing off. `set()` does nothing.

    A plain `Span` would work but would accumulate every attribute ever written at
    every call site for the life of the process — a slow leak in exactly the
    configuration that is supposed to cost nothing.
    """

    def set(self, **attrs: Any) -> None:
        return None


#: One shared instance, since it holds no state.
_DISCARD = _DiscardSpan(
    trace_id="",
    span_id="",
    parent_id=None,
    name="discard",
    started_at=0.0,
    started_mono=0.0,
)

NULL_TRACE = _NullTrace(trace_id="")

_CURRENT_TRACE: ContextVar[Trace] = ContextVar("sre_agent_trace", default=NULL_TRACE)
_CURRENT_PARENT: ContextVar[str | None] = ContextVar("sre_agent_span", default=None)


def current_trace() -> Trace:
    return _CURRENT_TRACE.get()


def current_parent() -> str | None:
    return _CURRENT_PARENT.get()


def current_correlation_id() -> str:
    """The observed system's own id for this incident.

    A tool that reaches a real backend in W3 L2 reads this to tag its query, so the
    backend's logs carry the same id the alert did — no extra plumbing through
    `Tool.run`, which is why the trace id is ambient rather than a second reserved
    keyword alongside `window`.
    """
    return current_trace().correlation_id


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Span]:
    """Open a span on the ambient trace. A no-op when none is installed."""
    with current_trace().span(name, **attrs) as sp:
        yield sp


@contextmanager
def _parent_scope(span_id: str) -> Iterator[None]:
    """Set the ambient parent, then restore the previous value **by `set`**.

    Not `reset(token)`, and this is load-bearing rather than stylistic. Spans here
    stay open across `yield` in an async generator, and an async generator runs in
    its caller's context — so a token minted on one `__anext__` may be reset on
    another, from a different context, which raises `ValueError`. Worse, an
    abandoned generator closes its spans during `aclose()`/GC, where the context is
    whatever happens to be current.

    Restoring by value is immune to all of that. It is very slightly less precise —
    we write our idea of "previous" into whichever context is current — which is
    harmless because this module owns the variable.
    """
    previous = _CURRENT_PARENT.get()
    _CURRENT_PARENT.set(span_id)
    try:
        yield
    finally:
        _CURRENT_PARENT.set(previous)


@contextmanager
def _trace_scope(trace: Trace) -> Iterator[None]:
    """Same restore-by-set discipline, for the ambient trace itself."""
    previous = _CURRENT_TRACE.get()
    _CURRENT_TRACE.set(trace)
    try:
        yield
    finally:
        _CURRENT_TRACE.set(previous)


# ---- the structured-log sink ---------------------------------------------- #

_LOGGER_NAME = "sre_agent.trace"


def log_sink(logger: logging.Logger | None = None) -> SpanSink:
    """A `SpanSink` that writes one JSON line per span.

    Deliberately the same shape as `mock/services/_shared/observability.py`'s
    `_JsonFormatter` output, and keyed on `correlation_id`, so the agent's lines and
    the observed system's lines can be grepped and sorted as one stream. That
    interleaving is the whole payoff of adopting the alert's id.
    """
    log = logger or logging.getLogger(_LOGGER_NAME)

    def sink(span: Span) -> None:
        log.info(json.dumps(span.as_dict(), ensure_ascii=False, default=str))

    return sink


def tree(spans: Sequence[Span] | Sequence[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """Flatten spans into `(depth, record)` pairs in parent-first order.

    Shared by `srectl replay` and by tests. Takes either live `Span`s or the dicts
    read back from a JSONL log, because replay works from the file and tests work
    from memory, and one ordering rule should serve both.

    Completion order is *not* tree order: a parent finishes after its children, so
    the raw list is leaf-first. An orphan — a child whose parent is missing because
    the log was truncated mid-write — is emitted at depth 0 rather than dropped.
    """
    records = [s.as_dict() if isinstance(s, Span) else dict(s) for s in spans]
    children: dict[str | None, list[dict[str, Any]]] = {}
    for record in records:
        children.setdefault(record.get("parent_id"), []).append(record)

    known = {r.get("span_id") for r in records}
    roots = [r for r in records if r.get("parent_id") not in known]

    out: list[tuple[int, dict[str, Any]]] = []

    def walk(record: dict[str, Any], depth: int) -> None:
        out.append((depth, record))
        for child in children.get(record.get("span_id"), []):
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    return out
