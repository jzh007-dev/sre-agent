"""Tool dispatch with failure containment.

The invariant: **a tool call never raises out of the loop.** Unknown tool name,
timeout, bad arguments, a backend that is down — all become
`ToolResultBlock(is_error=True)`, and the model decides how to route around it on
its next turn.

This is more than robustness hygiene. In an SRE agent the observability stack is
frequently *part of the outage*: when Redis is out of memory the path that writes
logs may also be broken, so "ClickHouse timed out" is an expected mid-incident
condition, not an exceptional one. An agent that dies when a query fails is
useless in exactly the incidents that matter most. What we want instead is a
report that says "logs were unavailable; the following is established from
metrics alone" — which requires the failure to arrive as evidence.

The behavioural half of this (reasoning well under partial observability) is
W3 L7. This module is the structural half.
"""
from __future__ import annotations

import asyncio
import json
from typing import Mapping

from ..llm.types import ToolResultBlock, ToolUseBlock
from .protocol import Tool

#: Hard ceiling on a single result. A ClickHouse page can be megabytes, and one
#: unbounded result can blow the context window for every subsequent turn.
#: Proper token-aware shaping (per-tool caps, folding, compaction) is W3 L4; this
#: is the safety valve that keeps L2 from being able to wedge itself.
MAX_RESULT_CHARS = 20_000
TRUNCATION_NOTICE = "\n… [truncated by dispatch: result exceeded {limit} chars]"


async def safe_dispatch(
    tools: Mapping[str, Tool],
    call: ToolUseBlock,
    window,
) -> ToolResultBlock:
    """Run one tool call, converting every failure mode into an error result."""
    tool = tools.get(call.name)
    if tool is None:
        return _error(
            call,
            f"unknown tool: {call.name}",
            hint=f"available tools: {sorted(tools)}",
        )

    try:
        async with asyncio.timeout(tool.meta.timeout_s):
            result = await tool.run(window=window, **call.input)
    except asyncio.TimeoutError:
        return _error(call, f"timed out after {tool.meta.timeout_s}s")
    except TypeError as exc:
        # The model supplied arguments the tool does not accept. Distinguished
        # from a generic failure because the model can fix this one itself by
        # re-reading the schema and calling again.
        return _error(call, f"invalid arguments: {exc}", hint="check the tool's input_schema")
    except asyncio.CancelledError:
        # Cancellation is the caller shutting us down, not a tool failure.
        raise
    except Exception as exc:
        return _error(call, f"{type(exc).__name__}: {exc}")

    if len(result) > MAX_RESULT_CHARS:
        result = result[:MAX_RESULT_CHARS] + TRUNCATION_NOTICE.format(limit=MAX_RESULT_CHARS)

    return ToolResultBlock(tool_use_id=call.id, content=result)


def _error(call: ToolUseBlock, message: str, hint: str | None = None) -> ToolResultBlock:
    """Error results are JSON so the model reads them as structure, not prose."""
    payload: dict[str, object] = {"error": message, "tool": call.name}
    if hint:
        payload["hint"] = hint
    return ToolResultBlock(
        tool_use_id=call.id,
        content=json.dumps(payload, ensure_ascii=False),
        is_error=True,
    )
