"""Agent loop — run_incident.

Given an alert dict, drive a while-loop of LLM-directed tool calls until the
LLM returns end_turn (or MAX_TURNS is hit as a runaway backstop). The loop
follows the "agent" pattern per TRADEOFFS §22 — LLM-driven ordering, no phase
state, no orchestration framework. The `messages` array is the state; there
is no separate state class.
"""
from __future__ import annotations

import json
from typing import Any

from .llm.protocol import LLM
from .llm.types import (
    Message,
    StopReason,
    TextBlock,
    ToolResultBlock,
)
from .tools import Tool, tool_schemas


MAX_TURNS = 15


class LoopError(RuntimeError):
    """Raised when the loop exits without producing a final assistant text."""


async def run_incident(
    alert: dict[str, Any],
    llm: LLM,
    tools: dict[str, Tool],
    max_turns: int = MAX_TURNS,
) -> str:
    """Run one incident through the agent loop; return the final report text.

    The alert is wrapped in <alert>...</alert> XML tags — this is the first
    piece of the Week 5 L1 data-isolation defense. At L1 the tag is just
    packaging; sanitization arrives with SECURITY.md Layer 1.
    """
    messages: list[Message] = [
        Message(
            role="user",
            content=[TextBlock(text=f"<alert>{json.dumps(alert)}</alert>")],
        ),
    ]

    turn = 0
    done = False
    schemas = tool_schemas(tools)

    while not done and turn < max_turns:
        response = await llm.call(messages, tools=schemas)
        messages.append(Message(role="assistant", content=list(response.content)))

        if response.stop_reason == StopReason.END_TURN:
            done = True
        elif response.stop_reason == StopReason.TOOL_USE:
            tool_results: list[Any] = []
            for tc in response.tool_uses():
                if tc.name not in tools:
                    tool_results.append(
                        ToolResultBlock(
                            tool_use_id=tc.id,
                            content=json.dumps({"error": f"unknown tool: {tc.name}"}),
                            is_error=True,
                        )
                    )
                    continue
                result = await tools[tc.name].run(**tc.input)
                tool_results.append(ToolResultBlock(tool_use_id=tc.id, content=result))
            messages.append(Message(role="user", content=tool_results))
        else:
            done = True

        turn += 1

    return _extract_final_text(messages)


def _extract_final_text(messages: list[Message]) -> str:
    """Return concatenated text of the last assistant message; raise if none."""
    for msg in reversed(messages):
        if msg.role != "assistant":
            continue
        text = "\n".join(b.text for b in msg.content if isinstance(b, TextBlock))
        if text:
            return text
        break
    raise LoopError("agent loop terminated without producing final assistant text")
