"""The ReAct kernel — step ④ of the harness, and the only non-deterministic one.

Structured ReAct: Thought is an optional assistant text block, Action is a
`tool_use` block, Observation is a `tool_result` block. Same idea as the 2022
ReAct paper, except the API guarantees the structure instead of a prompt
convention being parsed out of free text.

What the model decides: which tool to call next, how many times, in what order,
and when it is finished. What the code decides: which tools exist, the budget,
the time window, the output shape, and where the result goes. Freedom of
ordering, not freedom over resources or contracts — see
[ARCHITECTURE — who decides what](../../ARCHITECTURE.md#who-decides-what).

This module is deliberately ignorant of providers, integrations, triggers and
sinks. It knows two protocols (`LLM`, `Tool`), one dispatch policy, and its own
data types. `tests/test_architecture.py` fails the build if that stops being
true, which turns the seam rule from a claim into an invariant.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Mapping, Sequence

from ..llm.protocol import LLM, LLMContractError
from ..llm.types import ContentBlock, Message, StopReason, TextBlock, ToolResultBlock, ToolUseBlock
from ..tools.dispatch import safe_dispatch
from ..tools.protocol import Tool, tool_schemas
from . import trace as tracing
from .events import (
    Aborted,
    Done,
    Event,
    EventSink,
    TextDelta,
    ToolCalled,
    ToolReturned,
    TurnStarted,
)
from .investigation import Investigation, args_hash


class LoopError(RuntimeError):
    """Raised only for a broken contract, never for a failed investigation."""


async def run(
    inv: Investigation,
    llm: LLM,
    tools: Mapping[str, Tool],
    *,
    trace: tracing.Trace | None = None,
) -> AsyncIterator[Event]:
    """Drive the investigation, yielding events as they happen.

    Exactly one `Done` or `Aborted` is emitted before the generator finishes. A
    failing tool never ends the run; a failing *LLM call* propagates, because
    retry and backoff belong to the gateway (L3), not here.

    `trace` is installed as the ambient trace for the duration, which is how the
    gateway's `llm.call` spans come out nested under the right turn without the
    `LLM` protocol growing a parameter. Omitting it leaves `NULL_TRACE` in place and
    the run behaves exactly as it did before spans existed.
    """
    schemas = tool_schemas(tools)
    tracer = trace or tracing.current_trace()

    with tracer.install(), tracer.span(
        tracing.INVESTIGATION,
        investigation_id=inv.id,
        trigger=inv.trigger,
        integration=inv.integration or "",
        window=str(inv.window),
    ) as root:
        while True:
            if inv.turn >= inv.budget.max_turns:
                yield _outcome(
                    root,
                    inv,
                    Aborted(
                        "max_turns",
                        f"reached the turn ceiling ({inv.turn}/{inv.budget.max_turns}) "
                        f"without a terminal tool call",
                    ),
                )
                return

            with tracer.span(tracing.TURN, turn=inv.turn) as turn_span:
                yield TurnStarted(turn=inv.turn)

                try:
                    response = await llm.call(inv.messages, tools=schemas)
                except LLMContractError as exc:
                    # Budget exhausted, context overflowed, or every provider
                    # unavailable. All three are refusals rather than crashes, and
                    # everything gathered so far is still on `inv.messages` — so the
                    # harness can emit a partial report naming what stopped the
                    # investigation.
                    #
                    # One clause, keyed on the exception's own `reason`: adding a
                    # fourth contract error needs no change here. Provider-specific
                    # errors never reach this point — they are retried, contained, or
                    # collapsed into ProviderUnavailable by the gateway.
                    turn_span.set(refused=exc.reason)
                    yield _outcome(root, inv, Aborted(exc.reason, str(exc)))
                    return

                inv.messages.append(
                    Message(role="assistant", content=list(response.content))
                )
                turn_span.set(stop_reason=response.stop_reason.value)

                for block in response.content:
                    if isinstance(block, TextBlock) and block.text:
                        yield TextDelta(text=block.text)

                if response.stop_reason == StopReason.MAX_TOKENS:
                    # The response was cut off mid-thought, so any trailing tool_use
                    # block may carry truncated arguments. Acting on a partial
                    # decision is worse than stopping and saying why.
                    yield _outcome(
                        root,
                        inv,
                        Aborted(
                            "max_tokens",
                            "the model's response was truncated by the token limit",
                        ),
                    )
                    return

                calls = list(response.tool_uses())
                turn_span.set(tool_calls=len(calls))
                if not calls:
                    if inv.requires_report:
                        yield _outcome(
                            root,
                            inv,
                            Aborted(
                                "no_report",
                                "the model ended its turn without calling a terminal tool",
                            ),
                        )
                    else:
                        # Chat: answering and stopping is a legitimate ending.
                        yield _outcome(
                            root, inv, Done(text=_assistant_text(response.content))
                        )
                    return

                for call in calls:
                    yield ToolCalled(
                        tool_use_id=call.id, name=call.name, input=dict(call.input)
                    )

                # gather wraps each coroutine in a Task, and a Task copies the
                # current context — so each tool's span parents to this turn and
                # concurrent siblings cannot corrupt each other's parent.
                results = await asyncio.gather(
                    *(_dispatch_one(inv, tools, call, tracer) for call in calls)
                )

                # Every tool_use must receive a tool_result, including the terminal
                # one. An unanswered tool_use makes `messages` invalid for any
                # subsequent API call, which would break chat resume and alert-storm
                # absorption — so results are recorded even on the turn the
                # investigation concludes.
                inv.messages.append(Message(role="user", content=list(results)))

                for call, result in zip(calls, results):
                    yield ToolReturned(
                        tool_use_id=call.id,
                        name=call.name,
                        is_error=result.is_error,
                        content=result.content,
                    )

                terminal = _terminal_call(tools, calls)
                if terminal is not None:
                    yield _outcome(root, inv, Done(report=dict(terminal.input)))
                    return

                inv.turn += 1

                exhausted = inv.budget_exhausted()
                if exhausted:
                    yield _outcome(root, inv, Aborted("budget", exhausted))
                    return


async def run_to_completion(
    inv: Investigation,
    llm: LLM,
    tools: Mapping[str, Tool],
    *,
    trace: tracing.Trace | None = None,
    on_event: EventSink | None = None,
) -> Done | Aborted:
    """Collapse the event stream to its single outcome.

    What alert and patrol callers use — they want the report, not the
    play-by-play. Chat consumes `run()` directly.

    `on_event` is the sink the stream lacked: before L4a this function generated
    every event and kept only the terminal one, which is why a failed run left
    nothing behind but an `Aborted` reason string.
    """
    outcome: Done | Aborted | None = None
    async for event in run(inv, llm, tools, trace=trace):
        if on_event is not None:
            on_event(event)
        if isinstance(event, (Done, Aborted)):
            outcome = event
    if outcome is None:  # pragma: no cover — the loop always emits one
        raise LoopError("loop finished without emitting Done or Aborted")
    return outcome


async def _dispatch_one(
    inv: Investigation,
    tools: Mapping[str, Tool],
    call: ToolUseBlock,
    tracer: tracing.Trace,
) -> ToolResultBlock:
    """One tool call: repeat guard, dispatch, timing, accounting.

    The guard runs *before* dispatch and is the model-side circuit breaker — the
    counterpart to the provider one in `transport.py`, keyed on
    `(tool_name, args_hash)` because a name-keyed count cannot tell twelve
    different queries from the same query twelve times.
    """
    digest = args_hash(call.input)
    with tracer.span(
        tracing.TOOL_CALL, tool=call.name, args_hash=digest, tool_use_id=call.id
    ) as span:
        refusal = inv.repeat_guard(call.name, digest)
        if refusal is not None:
            # Counted, so the tool-call ceiling still bites, and the repeat counter
            # keeps climbing so the guard stays shut if the nudge is ignored. The
            # refusal is deliberately *not* stored as the result — overwriting the
            # previous answer with the message that quotes it would lose it on the
            # next repeat.
            inv.record_tool_call(call.name, digest)
            span.set(is_error=True, repeat_refused=True, dispatched=False)
            return ToolResultBlock(
                tool_use_id=call.id, content=refusal, is_error=True
            )

        result = await safe_dispatch(tools, call, inv.window)
        inv.record_tool_call(call.name, digest, result.content)
        span.set(
            is_error=result.is_error,
            dispatched=True,
            result_chars=len(result.content),
        )
        return result


def _outcome(span: tracing.Span, inv: Investigation, event: Done | Aborted) -> Done | Aborted:
    """Stamp the run's outcome on the root span and hand the event back.

    A helper rather than a line at each of the seven exits, because an exit that
    forgot to record its outcome is exactly the silent gap L4a exists to close.
    """
    span.set(
        outcome="Done" if isinstance(event, Done) else "Aborted",
        reason="" if isinstance(event, Done) else event.reason,
        turns=inv.turn + 1,
        tool_calls=sum(inv.tool_calls.values()),
        repeated_calls=sum(c - 1 for c in inv.repeat_calls.values() if c > 1),
    )
    return event


def _terminal_call(
    tools: Mapping[str, Tool], calls: Sequence[ToolUseBlock]
) -> ToolUseBlock | None:
    """The first called tool whose metadata marks it terminal, if any.

    Keyed on metadata rather than on a tool name, so this module never learns the
    string `submit_report`.
    """
    for call in calls:
        tool = tools.get(call.name)
        if tool is not None and tool.meta.terminal:
            return call
    return None


def _assistant_text(content: Sequence[ContentBlock]) -> str:
    return "\n".join(b.text for b in content if isinstance(b, TextBlock) and b.text)
