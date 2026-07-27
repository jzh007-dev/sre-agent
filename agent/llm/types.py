"""LLM domain types.

Provider-agnostic representations of messages, content blocks, and LLM
responses. Adapters in agent/llm/anthropic.py (Week 2 L2) and future
openai.py / gemini.py translate between these types and vendor SDK shapes.
The loop and stubs only ever see these types.

Shape follows Anthropic's Messages API closely (three block types, role in
{user, assistant}, tool_result blocks nested inside a role=user message)
because that's the API we hit first. Other providers cost the adapter a
mapping step but not the loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Union


class StopReason(str, Enum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"


@dataclass(frozen=True)
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"


@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


ContentBlock = Union[TextBlock, ToolUseBlock, ToolResultBlock]


@dataclass
class Message:
    role: Literal["user", "assistant"]
    content: list[ContentBlock]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Response:
    stop_reason: StopReason
    content: list[ContentBlock]
    usage: Usage = field(default_factory=Usage)

    def text_blocks(self) -> list[TextBlock]:
        return [b for b in self.content if isinstance(b, TextBlock)]

    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]
