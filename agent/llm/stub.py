"""StubLLM — returns canned responses from a fixed script.

Used by Week 2 L1 to prove the loop shape works without any real LLM call.
The script is a list of pre-built Response objects; each `call()` returns
the next one and advances a counter.

Week 2+ optimization idea (not implemented): a messages-aware stub that
inspects the last tool_result to choose the next canned response
dynamically, so a test can cover branching without hand-editing the script.
Fixed script is chosen for L1 because it makes tests fully deterministic
and readable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .types import Message, Response


@dataclass
class StubLLM:
    script: list[Response]
    turn: int = field(default=0, init=False)

    async def call(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> Response:
        if self.turn >= len(self.script):
            raise RuntimeError(
                f"StubLLM ran out of scripted responses at turn {self.turn}; "
                f"script has {len(self.script)} entries"
            )
        response = self.script[self.turn]
        self.turn += 1
        return response
