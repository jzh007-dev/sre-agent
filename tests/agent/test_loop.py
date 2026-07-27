"""Week 2 L1 verification: the stub loop terminates with a non-empty report,
using exactly the LLM calls its script implies. Also verifies that an unknown
tool name is wrapped as an is_error tool_result rather than crashing.
"""
from __future__ import annotations

import unittest

from agent.llm.stub import StubLLM
from agent.llm.types import Response, StopReason, TextBlock, ToolUseBlock
from agent.loop import run_incident
from agent.tools import default_tool_registry


FAKE_ALERT = {
    "alertname": "HighErrorRate",
    "service": "auth",
    "value": 0.15,
    "for": "2m",
}


def _three_turn_script() -> list[Response]:
    """query_metrics → search_runbook → end_turn(report)."""
    return [
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[
                TextBlock(text="auth 告警，先查错误率指标。"),
                ToolUseBlock(
                    id="t1",
                    name="query_metrics",
                    input={"promql": "rate(http_errors_total{service='auth'}[5m])"},
                ),
            ],
        ),
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[
                TextBlock(text="错误率确实高，查 auth 的 runbook。"),
                ToolUseBlock(
                    id="t2",
                    name="search_runbook",
                    input={"service": "auth", "symptom": "high error rate"},
                ),
            ],
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[
                TextBlock(text="Root cause: Redis OOM 导致 auth 会话写入失败。"),
            ],
        ),
    ]


class TestStubLoop(unittest.IsolatedAsyncioTestCase):
    async def test_three_turn_stub_loop_produces_report(self):
        llm = StubLLM(script=_three_turn_script())
        tools = default_tool_registry()

        report = await run_incident(FAKE_ALERT, llm=llm, tools=tools)

        self.assertIn("Root cause", report)
        self.assertEqual(llm.turn, 3, "expected exactly 3 LLM calls")

    async def test_unknown_tool_wrapped_as_error_result(self):
        """When the LLM asks for a tool that isn't registered, the loop
        should return an is_error tool_result rather than raise — the LLM
        gets to react on the next turn.
        """
        script = [
            Response(
                stop_reason=StopReason.TOOL_USE,
                content=[
                    ToolUseBlock(id="tx", name="nonexistent_tool", input={}),
                ],
            ),
            Response(
                stop_reason=StopReason.END_TURN,
                content=[TextBlock(text="handled the missing tool gracefully.")],
            ),
        ]
        llm = StubLLM(script=script)

        report = await run_incident(FAKE_ALERT, llm=llm, tools={})

        self.assertIn("handled", report)
        self.assertEqual(llm.turn, 2)


if __name__ == "__main__":
    unittest.main()
