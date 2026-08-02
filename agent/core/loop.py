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
from ..llm.types import ContentBlock, Message, StopReason, TextBlock, ToolUseBlock
from ..tools.dispatch import safe_dispatch
from ..tools.protocol import Tool, tool_schemas
from .events import Aborted, Done, Event, TextDelta, ToolCalled, ToolReturned, TurnStarted
from .investigation import Investigation


class LoopError(RuntimeError):
    """Raised only for a broken contract, never for a failed investigation."""


async def run(
    inv: Investigation,
    llm: LLM,
    tools: Mapping[str, Tool],
) -> AsyncIterator[Event]:
    """Drive the investigation, yielding events as they happen.

    Exactly one `Done` or `Aborted` is emitted before the generator finishes. A
    failing tool never ends the run; a failing *LLM call* propagates, because
    retry and backoff belong to the gateway (L3), not here.
    """
    schemas = tool_schemas(tools)

    while True:
        if inv.turn >= inv.budget.max_turns:
            yield Aborted(
                "max_turns",
                f"reached the turn ceiling ({inv.turn}/{inv.budget.max_turns}) "
                f"without a terminal tool call",
            )
            return

        yield TurnStarted(turn=inv.turn)

        try:
            response = await llm.call(inv.messages, tools=schemas)
        except LLMContractError as exc:
            # Budget exhausted, context overflowed, or every provider unavailable.
            # All three are refusals rather than crashes, and everything gathered so
            # far is still on `inv.messages` — so the harness can emit a partial
            # report naming what stopped the investigation.
            #
            # One clause, keyed on the exception's own `reason`: adding a fourth
            # contract error needs no change here. Provider-specific errors never
            # reach this point — they are retried, contained, or collapsed into
            # ProviderUnavailable by the gateway.
            yield Aborted(exc.reason, str(exc))
            return

        inv.messages.append(Message(role="assistant", content=list(response.content)))

        for block in response.content:
            if isinstance(block, TextBlock) and block.text:
                yield TextDelta(text=block.text)

        if response.stop_reason == StopReason.MAX_TOKENS:
            # The response was cut off mid-thought, so any trailing tool_use
            # block may carry truncated arguments. Acting on a partial decision
            # is worse than stopping and saying why.
            yield Aborted("max_tokens", "the model's response was truncated by the token limit")
            return

        calls = list(response.tool_uses())
        if not calls:
            if inv.requires_report:
                yield Aborted(
                    "no_report",
                    "the model ended its turn without calling a terminal tool",
                )
            else:
                # Chat: answering and stopping is a legitimate ending.
                yield Done(text=_assistant_text(response.content))
            return

        for call in calls:
            yield ToolCalled(tool_use_id=call.id, name=call.name, input=dict(call.input))

        results = await asyncio.gather(
            *(safe_dispatch(tools, call, inv.window) for call in calls)
        )

        # Every tool_use must receive a tool_result, including the terminal one.
        # An unanswered tool_use makes `messages` invalid for any subsequent API
        # call, which would break chat resume and alert-storm absorption — so
        # results are recorded even on the turn the investigation concludes.
        inv.messages.append(Message(role="user", content=list(results)))

        for call, result in zip(calls, results):
            inv.record_tool_call(call.name)
            yield ToolReturned(
                tool_use_id=call.id,
                name=call.name,
                is_error=result.is_error,
                content=result.content,
            )

        terminal = _terminal_call(tools, calls)
        if terminal is not None:
            yield Done(report=dict(terminal.input))
            return

        inv.turn += 1

        exhausted = inv.budget_exhausted()
        if exhausted:
            yield Aborted("budget", exhausted)
            return


async def run_to_completion(
    inv: Investigation,
    llm: LLM,
    tools: Mapping[str, Tool],
) -> Done | Aborted:
    """Collapse the event stream to its single outcome.

    What alert and patrol callers use — they want the report, not the
    play-by-play. Chat consumes `run()` directly.
    """
    outcome: Done | Aborted | None = None
    async for event in run(inv, llm, tools):
        if isinstance(event, (Done, Aborted)):
            outcome = event
    if outcome is None:  # pragma: no cover — the loop always emits one
        raise LoopError("loop finished without emitting Done or Aborted")
    return outcome


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
