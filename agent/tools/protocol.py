"""Tool protocol and metadata — the abstraction `core/` is allowed to know.

`core/loop.py` imports this module and `dispatch.py`, and nothing else from the
tools seam. It never imports `stubs.py`, `bundle.py`, or `mcp_client.py`, which
is enforced by `tests/test_architecture.py`.

Two design points live here rather than in the loop:

1. **`window` is a reserved keyword, not a schema field.** Every tool receives
   the investigation's time window; no tool declares it in `input_schema`. The
   model therefore has no channel through which to move or widen the window —
   the constraint is structural, not a prompt instruction it might ignore.
2. **Termination is a property of a tool, not a name in the loop.** A tool with
   `meta.terminal` set ends the investigation when called. The loop looks for
   "some called tool is terminal" and so contains no knowledge of the string
   `submit_report`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, runtime_checkable

from ..core.investigation import Window

SideEffect = Literal["READ", "WRITE", "DESTRUCTIVE"]
CostHint = Literal["cheap", "expensive"]


@dataclass(frozen=True)
class ToolMeta:
    """Everything about a tool that is policy rather than behaviour.

    `side_effect` and `cost_hint` are carried from L2 but only consumed later —
    the gate reads `side_effect` in W6 L4, and the cache reads `cost_hint`. They
    are declared now because retrofitting metadata onto a populated registry
    means touching every tool.
    """

    side_effect: SideEffect = "READ"
    cost_hint: CostHint = "cheap"
    timeout_s: float = 30.0
    #: Calling this tool concludes the investigation. Exactly one tool in a
    #: bundle should set it (the report submission tool).
    terminal: bool = False


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]
    meta: ToolMeta

    async def run(self, *, window: Window, **kwargs: Any) -> str:
        """Execute the tool and return a serialized result (JSON by convention).

        The return value becomes the `content` of a `ToolResultBlock`. Raising is
        permitted — `safe_dispatch` converts any exception into an error result
        so one broken backend cannot end the investigation.
        """
        ...


def tool_schemas(tools: Mapping[str, Tool]) -> list[dict[str, Any]]:
    """Render a tool registry into the schema list an LLM API expects.

    Also enforces the reserved-keyword invariant: a tool that declared `window`
    in its `input_schema` would hand the model control of the time range, and
    every downstream reproducibility guarantee would quietly depend on the model
    choosing not to use it. Fail loudly at wiring time instead.
    """
    schemas: list[dict[str, Any]] = []
    for tool in tools.values():
        properties = tool.input_schema.get("properties", {})
        for reserved in RESERVED_KWARGS:
            if reserved in properties:
                raise ValueError(
                    f"tool {tool.name!r} declares reserved argument {reserved!r} in its "
                    f"input_schema; reserved arguments are supplied by the harness and "
                    f"must not be model-controlled"
                )
        schemas.append(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
        )
    return schemas


#: Arguments the harness supplies and the model may never set.
RESERVED_KWARGS: frozenset[str] = frozenset({"window"})


def terminal_tool_names(tools: Mapping[str, Tool]) -> set[str]:
    return {name for name, tool in tools.items() if tool.meta.terminal}
