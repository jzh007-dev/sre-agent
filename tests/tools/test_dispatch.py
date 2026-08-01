"""safe_dispatch: every failure mode becomes a result, never an exception."""
from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone
from typing import Any

from agent.core.investigation import Window
from agent.llm.types import ToolUseBlock
from agent.tools.dispatch import MAX_RESULT_CHARS, safe_dispatch
from agent.tools.protocol import RESERVED_KWARGS, ToolMeta, tool_schemas

WINDOW = Window.around(datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc))


class _Base:
    description = "test double"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    meta = ToolMeta()


class OkTool(_Base):
    name = "ok"

    async def run(self, *, window: Window, **kwargs: Any) -> str:
        return json.dumps({"ok": True, "window": window.as_tool_args()})


class SlowTool(_Base):
    name = "slow"
    meta = ToolMeta(timeout_s=0.01)

    async def run(self, *, window: Window, **kwargs: Any) -> str:
        await asyncio.sleep(1.0)
        return "never"


class BoomTool(_Base):
    name = "boom"

    async def run(self, *, window: Window, **kwargs: Any) -> str:
        raise ConnectionError("clickhouse: connection refused")


class HugeTool(_Base):
    name = "huge"

    async def run(self, *, window: Window, **kwargs: Any) -> str:
        return "x" * (MAX_RESULT_CHARS + 5_000)


class StrictTool(_Base):
    name = "strict"

    async def run(self, *, window: Window, service: str) -> str:
        return service


class CancelTool(_Base):
    name = "cancel"

    async def run(self, *, window: Window, **kwargs: Any) -> str:
        raise asyncio.CancelledError()


def _call(name: str, **kwargs: Any) -> ToolUseBlock:
    return ToolUseBlock(id=f"tu_{name}", name=name, input=kwargs)


class TestSafeDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_success_passes_the_window_through(self):
        result = await safe_dispatch({"ok": OkTool()}, _call("ok"), WINDOW)
        self.assertFalse(result.is_error)
        self.assertEqual(json.loads(result.content)["window"], WINDOW.as_tool_args())
        self.assertEqual(result.tool_use_id, "tu_ok")

    async def test_unknown_tool(self):
        result = await safe_dispatch({"ok": OkTool()}, _call("ghost"), WINDOW)
        self.assertTrue(result.is_error)
        payload = json.loads(result.content)
        self.assertIn("unknown tool", payload["error"])
        self.assertIn("ok", payload["hint"])

    async def test_timeout_is_contained_and_names_the_limit(self):
        result = await safe_dispatch({"slow": SlowTool()}, _call("slow"), WINDOW)
        self.assertTrue(result.is_error)
        self.assertIn("timed out after 0.01s", json.loads(result.content)["error"])

    async def test_exception_is_contained_with_its_type(self):
        result = await safe_dispatch({"boom": BoomTool()}, _call("boom"), WINDOW)
        self.assertTrue(result.is_error)
        error = json.loads(result.content)["error"]
        self.assertIn("ConnectionError", error)
        self.assertIn("connection refused", error)

    async def test_bad_arguments_are_reported_as_fixable(self):
        result = await safe_dispatch({"strict": StrictTool()}, _call("strict", wrong=1), WINDOW)
        self.assertTrue(result.is_error)
        payload = json.loads(result.content)
        self.assertIn("invalid arguments", payload["error"])
        self.assertIn("input_schema", payload["hint"])

    async def test_oversized_result_is_truncated_with_a_marker(self):
        result = await safe_dispatch({"huge": HugeTool()}, _call("huge"), WINDOW)
        self.assertFalse(result.is_error)
        self.assertLess(len(result.content), MAX_RESULT_CHARS + 200)
        self.assertIn("truncated by dispatch", result.content)

    async def test_cancellation_propagates(self):
        """Cancellation is the caller shutting us down, not a tool failure —
        swallowing it would make the process unkillable mid-investigation."""
        with self.assertRaises(asyncio.CancelledError):
            await safe_dispatch({"cancel": CancelTool()}, _call("cancel"), WINDOW)

    async def test_concurrent_dispatch_isolates_failures(self):
        tools = {"ok": OkTool(), "boom": BoomTool(), "slow": SlowTool()}
        results = await asyncio.gather(
            *(safe_dispatch(tools, _call(n), WINDOW) for n in ("ok", "boom", "slow"))
        )
        self.assertEqual([r.is_error for r in results], [False, True, True])


class TestReservedKwargInvariant(unittest.TestCase):
    def test_tool_declaring_window_is_rejected_at_wiring_time(self):
        """A tool that put `window` in its schema would hand the model control of
        the time range, and every reproducibility guarantee downstream would
        silently depend on the model choosing not to use it."""

        class Leaky(_Base):
            name = "leaky"
            input_schema = {"type": "object", "properties": {"window": {"type": "string"}}}

            async def run(self, *, window: Window, **kwargs: Any) -> str:
                return ""

        with self.assertRaises(ValueError) as ctx:
            tool_schemas({"leaky": Leaky()})
        self.assertIn("reserved argument", str(ctx.exception))

    def test_schemas_exclude_reserved_kwargs_by_construction(self):
        schemas = tool_schemas({"ok": OkTool()})
        self.assertEqual(len(schemas), 1)
        for reserved in RESERVED_KWARGS:
            self.assertNotIn(reserved, schemas[0]["input_schema"].get("properties", {}))


if __name__ == "__main__":
    unittest.main()
