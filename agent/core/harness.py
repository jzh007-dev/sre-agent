"""The harness — the deterministic shell around the one non-deterministic step.

Six steps in a fixed order: `route` → `preprocess` → `loadout` → `run_loop` → `parse` →
`fanout`. Steps ①②⑥ vary by trigger; **③④⑤ are identical across alert, chat and
patrol**, which is what makes chat and patrol new triggers rather than new
architectures. See [TRADEOFFS §23](../../TRADEOFFS.md#23-harness-deterministic-pipeline-around-the-agent-loop-refines-22).

**What it is for.** [ARCHITECTURE — who decides what](../../ARCHITECTURE.md#who-decides-what)
splits the decisions: the model chooses which tool next, how often, in what order, and
when it is done; the code chooses **which tools exist, the budget, the time window, the
output shape, and where the result goes**. Each of those five has an enforcement point
the model cannot reach around:

| code decides | enforced |
|---|---|
| which tools exist | ③, from `tools.bundle` — the model can only call what it was handed |
| the budget | ② sets the tier, ④'s loop checks the ceilings, the gateway refuses past the cost ceiling. It lives on the `Investigation`, and no tool takes it as an argument |
| the time window | ② pins it; `tool_schemas()` raises at wiring time if any tool declares `window` |
| the output shape | the report tool validates its own input at dispatch, so a bad report returns `is_error` and the model can fix it. ⑤ only ever reads something already valid |
| where the result goes | ⑥, from the trigger's sink binding. Nothing the model emits selects a destination |

**Three layers, and the third is not a step.**

    L1 composition   intake() ①②   ·   investigate() ③④⑤⑥
                     owns ORDER and nothing else
    L2 six steps     module functions; collaborators passed in explicitly
    L3 context       Trace · InvestigationLog · event sinks
                     wraps every step, which is why it is not a seventh one —
                     exactly as the gateway's cache/budget/tracing are not a
                     fifth layer

**Two entry points rather than one `run(payload)`.** ①② are synchronous, cheap, and
produce **0..N** investigations — dedup can drop one, patrol fans out to N. ③-⑥ are
long, async and strictly per-investigation. A single entry point would have to tag every
event with an investigation id and invent a meaning for "the payload was suppressed". It
is also the shape an HTTP caller needs: `POST` → `intake` → either ids or *"suppressed by
R2, and here is why"*, then one event stream per investigation. Patrol's fan-out is
`asyncio.gather` over `investigate()` at L1 — the only concurrency in the shell, kept out
of the steps.

**The harness never retries.** Retry lives in exactly one layer, the transport. The loop
does not retry, this module does not, and a failed sink becomes a `Delivery` rather than
a second attempt. Retries at three layers multiply into a number nobody can predict; with
one layer, "how many attempts did that take" is answerable from `Trace.profile()`.

**Cost is read off the trace, not off the `Ledger`.** The gateway already stamps
`ledger.summary()` on every `llm.call` span, and the ledger is cumulative per
investigation, so the last such span carries the final total. That keeps `agent/core/`
from needing to know what money is, and means the sink, the JSONL footer and the console
quote one set of numbers rather than three that can drift.

W2 L6a-1 builds the happy path. The error invariant, the dedup lifecycle close, and the
abandoned-consumer guard are L6a-2.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Mapping

from ..llm.protocol import LLMFactory
from ..prompts import assemble as prompts
from ..sinks import registry as sinks
from ..store import jsonl
from ..tools import bundle as tool_bundle
from ..tools.protocol import Tool
from ..triggers import registry as triggers
from . import loop as agent_loop
from . import trace as tracing
from .events import Aborted, Done, Event, EventSink
from .investigation import Investigation, ToolBudget

_LOGGER_NAME = "sre_agent.harness"

#: The shell's own span, wrapping ③④⑤⑥ — **so the loop's `investigation` span is a child
#: of it rather than a sibling**. Without it, `loadout` / `parse` / `fanout` are separate
#: roots, `Trace.profile()` reports whichever it happens to find first as `elapsed_ms`,
#: and `srectl replay` prints four trees for one run. Found by looking at the tree; the
#: first version of the test that was supposed to catch it asserted only that the step
#: names were *present*, not that they were nested.
RUN_SPAN = "harness.run"

#: Span names for the five non-loop steps. `run_loop` gets no span of its own — `RUN_SPAN`
#: brackets it, the loop's `investigation` span sits directly inside, and `turn` spans
#: already break that down; a further level between them would add depth without adding
#: information.
#:
#: ① and ② are *not* under `RUN_SPAN`: they belong to a **delivery**, which may produce
#: several investigations or none, while ③-⑥ belong to **one** investigation. Different
#: lifetimes, so different traces — `intake()` records into whatever trace its caller
#: installed.
STEP_SPANS = {
    "route": "harness.route",
    "preprocess": "harness.preprocess",
    "loadout": "harness.loadout",
    "parse": "harness.parse",
    "fanout": "harness.fanout",
}


# --------------------------------------------------------------------------- #
# Data between steps
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Route:
    """① — which trigger handles this delivery, and under which integration."""

    trigger: str
    #: `None` until W2 L5a wires the integration registry. The field exists now so the
    #: call site is already correct when it starts carrying a value.
    integration: str | None = None


@dataclass(frozen=True)
class Loadout:
    """③ — what the model is given: the tools, and the assembled system prompt.

    `prompt` is typed `Any` deliberately. Its real type is `llm.request.SystemPrompt`,
    which this module may not name (`tests/test_architecture.py`), and it has to stay a
    structured object rather than a string so `stable_across` can keep placing the cache
    breakpoints W2 L3b measured at a 94.9% saving. The harness forwards it to the
    `LLMFactory` without inspecting it.
    """

    tools: Mapping[str, Tool]
    prompt: Any
    #: Per-tool call caps this bundle asked for, already applied to `inv.budget`.
    caps: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    """⑤ — the outcome, resolved into a report or a stated failure."""

    outcome: Done | Aborted
    report: Mapping[str, Any] | None = None
    #: Empty when a report was produced; otherwise the abort reason verbatim, because it
    #: goes in front of a human as the explanation for an empty report.
    failure: str = ""

    @property
    def has_report(self) -> bool:
        return self.report is not None


@dataclass
class HarnessContext:
    """L3 — what wraps every step rather than sitting between two of them.

    The membership rule is exactly that: cross-cutting, used at every step.
    Observability qualifies. `llm_factory` (only ④) and the sink list (only ⑥) do not, so
    they are parameters of the step that needs them — a context object holding everything
    is global state wearing a parameter's clothes.

    `now` is deliberately absent: timing already lives on `Trace`, which injects both
    `clock` and `wall`, and the trigger has its own clock. A third time source is a third
    thing that can disagree.
    """

    trace: tracing.Trace
    #: `None` means "do not persist". The JSONL log is a seam, not a requirement.
    log: jsonl.InvestigationLog | None = None
    #: Side consumers of the event stream — the log is one, L6b's SSE hub is the other.
    event_sinks: list[EventSink] = field(default_factory=list)
    #: Monotonic start, taken at construction. ⑥ runs *inside* the root span, so
    #: `Trace.profile()["elapsed_ms"]` is still `None` when a sink asks how long the run
    #: took — the first version of this reported `elapsed 0.0ms` on a real report.
    started_mono: float = field(default=0.0)

    def __post_init__(self) -> None:
        if not self.started_mono:
            self.started_mono = self.trace.clock()

    def elapsed_ms(self) -> float:
        """Elapsed at the moment of asking, from the same clock the spans use."""
        return (self.trace.clock() - self.started_mono) * 1000.0

    def emit(self, event: Event) -> None:
        for sink in self.event_sinks:
            # A broken side consumer must not end an investigation, for the same reason
            # `Trace._emit` contains its sinks: losing telemetry is bad, losing the
            # incident response because telemetry broke is worse.
            try:
                sink(event)
            except Exception:  # noqa: BLE001
                logging.getLogger(_LOGGER_NAME).exception("harness event sink failed")


# --------------------------------------------------------------------------- #
# L1 — composition. Owns order, nothing else.
# --------------------------------------------------------------------------- #


def intake(
    kind: str,
    payload: Mapping[str, Any],
    *,
    trace: tracing.Trace | None = None,
) -> triggers.TriggerOutcome:
    """Steps ① and ②. Synchronous, and may produce **zero** investigations.

    Zero is a normal result — a duplicate alert, a held burst, a `resolved` notification
    for something nobody is investigating. The `TriggerOutcome` carries the decision and
    its reason either way, which is what makes a suppression reportable rather than
    invisible.

    A failure here propagates: no `Investigation` exists yet, so there is nothing to
    report on and nowhere to report it. An HTTP caller turns it into a 4xx.
    """
    tracer = trace or tracing.current_trace()
    return preprocess(route(kind, payload, trace=tracer), payload, trace=tracer)


async def investigate(
    inv: Investigation,
    *,
    llm_factory: LLMFactory,
    trace: tracing.Trace | None = None,
    log: jsonl.InvestigationLog | None = None,
    on_event: EventSink | None = None,
    integration_facet: str = "",
    sink_names: tuple[str, ...] | None = None,
) -> AsyncIterator[Event]:
    """Steps ③ to ⑥ for one investigation, yielding the loop's events as they happen.

    An async generator rather than a coroutine returning the outcome, because the event
    stream is the point: L6b's console subscribes to it, and `run_to_completion` already
    demonstrated that generating events and keeping only the terminal one is an
    instrument produced and discarded.

    The context is created here — the `Trace`, and the JSONL log's header — because
    lifecycle belongs to the composition layer. A step that opened its own log could not
    be called twice from a test.
    """
    tracer = trace or tracing.Trace(trace_id=inv.id, correlation_id=inv.correlation_id)
    ctx = HarnessContext(trace=tracer, log=log)
    if log is not None:
        ctx.event_sinks.append(log.event)
        if log.span not in tracer.sinks:
            tracer.sinks.append(log.span)
    if on_event is not None:
        ctx.event_sinks.append(on_event)

    names = sink_names if sink_names is not None else sinks_for(inv)

    with tracer.install(), tracer.span(
        RUN_SPAN,
        investigation_id=inv.id,
        trigger=inv.trigger,
        integration=inv.integration or "",
    ) as run_span:
        loadout_result = loadout(inv, ctx, integration_facet=integration_facet)

        outcome: Done | Aborted | None = None
        async for event in run_loop(inv, loadout_result, llm_factory, ctx):
            ctx.emit(event)
            if isinstance(event, (Done, Aborted)):
                outcome = event
            yield event

        if outcome is None:  # pragma: no cover — the loop always emits one
            raise agent_loop.LoopError("loop finished without emitting Done or Aborted")

        deliveries = fanout(parse(outcome, ctx), inv, names, ctx)
        run_span.set(
            outcome=type(outcome).__name__,
            delivered=sum(1 for d in deliveries if d.delivered),
        )


async def investigate_to_completion(
    inv: Investigation,
    *,
    llm_factory: LLMFactory,
    trace: tracing.Trace | None = None,
    log: jsonl.InvestigationLog | None = None,
    on_event: EventSink | None = None,
    integration_facet: str = "",
    sink_names: tuple[str, ...] | None = None,
) -> Done | Aborted:
    """Collapse the stream to its outcome. What a CLI and the eval runner want."""
    outcome: Done | Aborted | None = None
    async for event in investigate(
        inv,
        llm_factory=llm_factory,
        trace=trace,
        log=log,
        on_event=on_event,
        integration_facet=integration_facet,
        sink_names=sink_names,
    ):
        if isinstance(event, (Done, Aborted)):
            outcome = event
    if outcome is None:  # pragma: no cover
        raise agent_loop.LoopError("investigate finished without an outcome")
    return outcome


# --------------------------------------------------------------------------- #
# L2 — the six steps
# --------------------------------------------------------------------------- #


def route(
    kind: str,
    payload: Mapping[str, Any],
    *,
    trace: tracing.Trace | None = None,
) -> Route:
    """① Which trigger handles this, and under which integration.

    Deliberately does not look inside the payload — reading it is the trigger's job, and
    a router that peeked would be a second place that knows a webhook's shape.

    Integration resolution is W2 L5a. `KeyError` on an unknown kind is the right failure:
    the registry's message lists what *is* registered, which turns a typo in a route into
    a one-second fix rather than a debugging session.
    """
    tracer = trace or tracing.current_trace()
    with tracer.span(STEP_SPANS["route"], trigger=kind) as span:
        trigger = triggers.get(kind)
        result = Route(trigger=trigger.kind, integration=None)
        span.set(integration=result.integration or "")
        return result


def preprocess(
    route_result: Route,
    payload: Mapping[str, Any],
    *,
    trace: tracing.Trace | None = None,
) -> triggers.TriggerOutcome:
    """② Normalise, deduplicate, and tier the budget — all of it inside the trigger.

    This step owns nothing itself; it is the seam call. Dedup rules R0-R5, the group-key
    identity, severity → budget tier and the pinned window were all built in W2 L4b, and
    re-deriving any of them here would create a second place they can disagree.
    """
    tracer = trace or tracing.current_trace()
    with tracer.span(STEP_SPANS["preprocess"], trigger=route_result.trigger) as span:
        outcome = triggers.dispatch(route_result.trigger, payload)
        for key, value in outcome.summary().items():
            span.set(**{key: value if isinstance(value, int) else str(value)})
        return outcome


def loadout(
    inv: Investigation,
    ctx: HarnessContext,
    *,
    integration_facet: str = "",
) -> Loadout:
    """③ Assemble what the model is given: the tool bundle, and the system prompt.

    **Does not touch `window` or the severity ceilings.** W2 L4b moved both into the
    trigger — the window is pinned from the alert's T0, the ceilings come from the
    severity tier — so recomputing either here would silently overwrite the budget a P1
    was granted. Narrowing `per_tool_calls` is a different thing and is this step's
    business: the bundle knows `query_logs` is expensive; the severity tier does not.
    """
    with ctx.trace.span(STEP_SPANS["loadout"], integration=inv.integration or "") as span:
        tools = tool_bundle.for_integration(inv.integration)
        tool_bundle.verify(tools, requires_report=inv.requires_report)

        caps = caps_for(tools)
        if caps:
            inv.budget = with_caps(inv.budget, caps)

        prompt = prompts.assemble(inv, integration_facet=integration_facet)
        span.set(
            tools=len(tools),
            tool_names=",".join(sorted(tools)),
            capped=",".join(sorted(caps)),
            prompt_fragments=",".join(f.name for f in prompt.ordered()),
            prompt_chars=len(prompt.text()),
            cache_breakpoints=len(prompt.breakpoint_indices()),
        )
        return Loadout(tools=tools, prompt=prompt, caps=caps)


async def run_loop(
    inv: Investigation,
    loadout_result: Loadout,
    llm_factory: LLMFactory,
    ctx: HarnessContext,
) -> AsyncIterator[Event]:
    """④ Bind the LLM to this investigation's prompt, then hand over to the kernel.

    The binding is the only thing this step does, and it exists because `SystemPrompt` is
    bound when an `LLM` is constructed rather than passed to `call()` — see `LLMFactory`.
    """
    llm = llm_factory(inv, loadout_result.prompt)
    async for event in agent_loop.run(inv, llm, loadout_result.tools, trace=ctx.trace):
        yield event


def parse(outcome: Done | Aborted, ctx: HarnessContext) -> Result:
    """⑤ Resolve the outcome into a report or a stated failure.

    Narrow on purpose. It does **not** validate the report — the report tool does that at
    dispatch, where an `is_error` result can still reach the model and be corrected. Once
    the loop has ended there is no feedback path left, so a check here would be a
    constraint with no way to satisfy it.
    """
    with ctx.trace.span(STEP_SPANS["parse"]) as span:
        if isinstance(outcome, Done):
            report = dict(outcome.report) if outcome.report is not None else None
            if report is None and outcome.text:
                # Chat's legitimate ending: an answer, no report. Wrapped so ⑥ renders
                # one shape rather than two.
                report = {"answer": outcome.text}
            span.set(outcome="Done", has_report=report is not None)
            return Result(outcome=outcome, report=report)

        span.set(outcome="Aborted", reason=outcome.reason)
        detail = f"{outcome.reason}: {outcome.detail}" if outcome.detail else outcome.reason
        return Result(outcome=outcome, report=None, failure=detail)


def fanout(
    result: Result,
    inv: Investigation,
    sink_names: tuple[str, ...],
    ctx: HarnessContext,
) -> list[sinks.Delivery]:
    """⑥ Deliver to every bound sink.

    Runs on an `Aborted` too: "budget exhausted after 12 tool calls, here is what was
    established" is the message on-call needs most, and a fanout that only handled
    success would drop exactly those.

    Closing the trigger's lifecycle on the strength of what was actually delivered — and
    the per-sink containment that makes that flag trustworthy — is L6a-2.
    """
    with ctx.trace.span(STEP_SPANS["fanout"], sinks=",".join(sink_names)) as span:
        context = delivery_context(result, inv, ctx)
        deliveries = [
            sink.deliver(result.report, context) for sink in sinks.resolve(sink_names)
        ]
        unresolved = sinks.missing(sink_names)
        span.set(
            delivered=sum(1 for d in deliveries if d.delivered),
            declined=sum(1 for d in deliveries if not d.delivered),
            unresolved=",".join(unresolved),
        )
        if ctx.log is not None:
            ctx.log.close({"deliveries": [d.as_dict() for d in deliveries]})
        return deliveries


def delivery_context(
    result: Result, inv: Investigation, ctx: HarnessContext
) -> dict[str, Any]:
    """Everything a sink needs to qualify the report, all of it read off the trace.

    `ledger` comes from the last `llm.call` span rather than from a `Ledger` object: the
    gateway stamps `ledger.summary()` on every one of them and the ledger is cumulative,
    so the last is the final total. That is why this module never imports `llm.cost`, and
    why the sink, the log footer and the console cannot disagree about what a run cost.

    `profile.elapsed_ms` is overridden with the context's own reading, because ⑥ runs
    *inside* the root span by construction, so the root's duration is not set yet. The
    shares are recomputed against it rather than left pointing at the old denominator —
    two numbers from different denominators in one dict is worse than either alone.
    """
    profile = ctx.trace.profile()
    elapsed = ctx.elapsed_ms()
    profile["elapsed_ms"] = round(elapsed, 3)
    if elapsed > 0:
        profile["llm_share"] = round(profile["llm_ms"] / elapsed, 4)
        profile["tool_share"] = round(profile["tool_ms"] / elapsed, 4)
        profile["overhead_ms"] = round(
            max(0.0, elapsed - profile["llm_ms"] - profile["tool_ms"]), 3
        )
    return {
        "investigation_id": inv.id,
        "correlation_id": inv.correlation_id,
        "trigger": inv.trigger,
        "integration": inv.integration or "",
        "window": str(inv.window),
        "outcome": type(result.outcome).__name__,
        "failure": result.failure,
        "turns": inv.turn + 1,
        "tool_calls": sum(inv.tool_calls.values()),
        "profile": profile,
        "ledger": latest_ledger(ctx.trace),
    }


def latest_ledger(trace: tracing.Trace) -> dict[str, Any]:
    """The cost summary from the most recent `llm.call` span, or `{}` if none ran.

    Empty is the honest answer for a run that made no call: a report saying "0 llm calls"
    and one saying "0.00" describe the same amount, and only the first says why.
    """
    for span in reversed(trace.spans):
        if span.name == tracing.LLM_CALL and isinstance(span.attrs.get("ledger"), dict):
            return dict(span.attrs["ledger"])
    return {}


# --------------------------------------------------------------------------- #
# Helpers — public because tests and L6b's API read them directly
# --------------------------------------------------------------------------- #


def sinks_for(inv: Investigation) -> tuple[str, ...]:
    """The trigger's default sink binding.

    Resolved from `inv.trigger` rather than threaded down from `intake`, because the
    investigation already carries its own kind. One lookup beats a parameter on every
    call, and it means `investigate()` can be handed an investigation rebuilt from the
    JSONL log with nothing else alongside it.

    An unregistered kind yields no sinks rather than raising: ⑥ still runs and the trace
    records `unresolved`, which is a reportable state. Raising here would lose a finished
    investigation over a registry lookup.
    """
    try:
        return tuple(triggers.get(inv.trigger).sinks)
    except KeyError:
        return ()


def caps_for(tools: Mapping[str, Tool]) -> dict[str, int]:
    """Per-tool ceilings the bundle declares.

    Nothing declares one yet: `ToolMeta` grows `max_calls` when a tool proves it needs
    one, and W3 L2's real ClickHouse queries are the expected first case. The plumbing is
    here now because retrofitting it means revisiting every caller of `loadout`.
    """
    caps: dict[str, int] = {}
    for name, tool in tools.items():
        declared = getattr(tool.meta, "max_calls", None)
        if isinstance(declared, int) and declared > 0:
            caps[name] = declared
    return caps


def with_caps(budget: ToolBudget, caps: Mapping[str, int]) -> ToolBudget:
    """A copy with `per_tool_calls` merged in — and the three tier ceilings untouched.

    Rebuilt rather than mutated because `ToolBudget` is frozen, and *narrowed* rather
    than replaced because `max_turns` / `max_tool_calls` / `max_cost` belong to the
    severity tier ② assigned. A test asserts all three survive this step unchanged.
    """
    merged = dict(budget.per_tool_calls)
    merged.update(caps)
    return ToolBudget(
        max_turns=budget.max_turns,
        max_tool_calls=budget.max_tool_calls,
        max_cost=budget.max_cost,
        per_tool_calls=merged,
        repeat_tool_calls=budget.repeat_tool_calls,
    )


def open_log(
    inv: Investigation,
    root: str | None = None,
    *,
    now: Callable[[], float] = time.time,
) -> jsonl.InvestigationLog:
    """Open this investigation's JSONL log, header written.

    A convenience so a caller need not import the store to get the default behaviour, and
    so "durability is on" reads as one line rather than four.
    """
    return jsonl.InvestigationLog(inv, root=root or jsonl.DEFAULT_ROOT, now=now).open()


__all__ = [
    "HarnessContext",
    "Loadout",
    "RUN_SPAN",
    "Result",
    "Route",
    "STEP_SPANS",
    "caps_for",
    "delivery_context",
    "fanout",
    "intake",
    "investigate",
    "investigate_to_completion",
    "latest_ledger",
    "loadout",
    "open_log",
    "parse",
    "preprocess",
    "route",
    "run_loop",
    "sinks_for",
    "with_caps",
]
