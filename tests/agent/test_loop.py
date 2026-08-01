"""Week 2 L2 verification of the loop's four structural guarantees.

Each test here fails against the L1 loop, which is the point — these are the
behaviours the refactor exists to establish:

1. the run is an event stream, terminated by a terminal tool call,
2. a throwing or hanging tool becomes evidence rather than a crash,
3. `messages` belongs to the investigation and can be resumed,
4. the pinned window reaches every tool without the model being able to set it.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from typing import Any, Sequence

from agent.core.events import (
    Aborted,
    Done,
    TextDelta,
    ToolCalled,
    ToolReturned,
    TurnStarted,
)
from agent.core.investigation import Investigation, ToolBudget, Window
from agent.core.loop import run, run_to_completion
from agent.llm.stub import StubLLM
from agent.llm.types import Message, Response, StopReason, TextBlock, ToolUseBlock
from agent.tools.protocol import Tool, ToolMeta
from agent.tools.stubs import default_tool_registry

T0 = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)

FAKE_ALERT = {
    "_meta": {"purpose": "the answer is Redis OOM — must never reach the model"},
    "alert_name": "HighErrorRate",
    "commonLabels": {"service": "auth", "severity": "P1"},
}


def _alert_investigation(**kwargs: Any) -> Investigation:
    return Investigation.from_alert(FAKE_ALERT, t0=T0, **kwargs)


def _chat_investigation() -> Investigation:
    inv = Investigation(id="inv_chat", trigger="chat", window=Window.around(T0))
    inv.add_user_text("why is checkout slow?")
    return inv


def _submit(root_cause: str = "Redis OOM rejected auth session writes") -> ToolUseBlock:
    return ToolUseBlock(
        id="t_submit",
        name="submit_report",
        input={"root_cause": root_cause, "confidence": "high", "evidence": ["m1"]},
    )


def _tool_use_response(*blocks: Any) -> Response:
    return Response(stop_reason=StopReason.TOOL_USE, content=list(blocks))


# --------------------------------------------------------------------------- #
# Test doubles for the failure paths. Kept here rather than in tools/stubs.py:
# production code should not ship a tool whose job is to explode.
# --------------------------------------------------------------------------- #


class ExplodingTool:
    name = "query_logs"
    description = "a backend that is part of the outage"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    meta = ToolMeta(timeout_s=5.0)

    async def run(self, *, window: Window, **kwargs: Any) -> str:
        raise ConnectionError("clickhouse: connection refused")


class WindowRecordingTool:
    name = "query_metrics"
    description = "records the window it was handed"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"promql": {"type": "string"}},
        "required": ["promql"],
    }
    meta = ToolMeta()

    def __init__(self) -> None:
        self.seen: list[Window] = []

    async def run(self, *, window: Window, **kwargs: Any) -> str:
        self.seen.append(window)
        return json.dumps({"window": window.as_tool_args()})


class TestTerminationAndEvents(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_tool_call_ends_the_run_with_a_report(self):
        llm = StubLLM(
            script=[
                _tool_use_response(
                    TextBlock(text="auth alert — checking the error rate first."),
                    ToolUseBlock(id="t1", name="query_metrics", input={"promql": "rate(...)"}),
                ),
                _tool_use_response(
                    TextBlock(text="Error rate confirmed; delivering."),
                    _submit(),
                ),
            ]
        )
        inv = _alert_investigation()

        outcome = await run_to_completion(inv, llm=llm, tools=default_tool_registry())

        self.assertIsInstance(outcome, Done)
        assert isinstance(outcome, Done)
        self.assertIsNotNone(outcome.report)
        assert outcome.report is not None
        self.assertEqual(outcome.report["confidence"], "high")
        self.assertIn("Redis OOM", outcome.report["root_cause"])
        self.assertEqual(llm.turn, 2, "expected exactly 2 LLM calls")

    async def test_event_stream_order(self):
        llm = StubLLM(
            script=[
                _tool_use_response(
                    TextBlock(text="thinking"),
                    ToolUseBlock(id="t1", name="query_metrics", input={"promql": "up"}),
                ),
                _tool_use_response(_submit()),
            ]
        )
        events = [e async for e in run(_alert_investigation(), llm=llm, tools=default_tool_registry())]
        kinds = [type(e).__name__ for e in events]

        self.assertEqual(
            kinds,
            [
                "TurnStarted",
                "TextDelta",
                "ToolCalled",
                "ToolReturned",
                "TurnStarted",
                "ToolCalled",
                "ToolReturned",
                "Done",
            ],
        )
        # ToolCalled must precede its ToolReturned so a consumer can render
        # "querying…" while the call is in flight.
        called = next(e for e in events if isinstance(e, ToolCalled))
        returned = next(e for e in events if isinstance(e, ToolReturned))
        self.assertEqual(called.tool_use_id, returned.tool_use_id)
        self.assertLess(events.index(called), events.index(returned))

    async def test_terminal_call_still_records_a_tool_result(self):
        """`messages` must stay API-valid: every tool_use needs a tool_result,
        including on the concluding turn, or a chat resume would be rejected."""
        llm = StubLLM(script=[_tool_use_response(_submit())])
        inv = _alert_investigation()

        await run_to_completion(inv, llm=llm, tools=default_tool_registry())

        self.assertEqual(_unanswered_tool_uses(inv.messages), [])

    async def test_alert_without_terminal_call_is_aborted(self):
        """Plain end_turn is 'ran out of things to say', not 'delivered'."""
        llm = StubLLM(
            script=[Response(stop_reason=StopReason.END_TURN, content=[TextBlock(text="dunno")])]
        )
        outcome = await run_to_completion(
            _alert_investigation(), llm=llm, tools=default_tool_registry()
        )

        self.assertIsInstance(outcome, Aborted)
        assert isinstance(outcome, Aborted)
        self.assertEqual(outcome.reason, "no_report")

    async def test_chat_may_finish_without_a_terminal_call(self):
        """Same loop, different trigger: answering a question is a valid ending."""
        llm = StubLLM(
            script=[
                Response(
                    stop_reason=StopReason.END_TURN,
                    content=[TextBlock(text="checkout is slow because payment is timing out.")],
                )
            ]
        )
        outcome = await run_to_completion(
            _chat_investigation(), llm=llm, tools=default_tool_registry()
        )

        self.assertIsInstance(outcome, Done)
        assert isinstance(outcome, Done)
        self.assertIsNone(outcome.report)
        self.assertIn("payment", outcome.text or "")

    async def test_truncated_response_aborts_rather_than_acting(self):
        llm = StubLLM(
            script=[
                Response(
                    stop_reason=StopReason.MAX_TOKENS,
                    content=[
                        TextBlock(text="I'll check met"),
                        ToolUseBlock(id="t1", name="query_metrics", input={}),
                    ],
                )
            ]
        )
        outcome = await run_to_completion(
            _alert_investigation(), llm=llm, tools=default_tool_registry()
        )

        self.assertIsInstance(outcome, Aborted)
        assert isinstance(outcome, Aborted)
        self.assertEqual(outcome.reason, "max_tokens")

    async def test_max_turns_aborts_with_the_reason(self):
        llm = StubLLM(
            script=[
                _tool_use_response(
                    ToolUseBlock(id=f"t{i}", name="query_metrics", input={"promql": "up"})
                )
                for i in range(5)
            ]
        )
        inv = _alert_investigation(budget=ToolBudget(max_turns=2))

        outcome = await run_to_completion(inv, llm=llm, tools=default_tool_registry())

        self.assertIsInstance(outcome, Aborted)
        assert isinstance(outcome, Aborted)
        self.assertEqual(outcome.reason, "max_turns")
        self.assertEqual(llm.turn, 2, "must stop calling the LLM once the ceiling is hit")

    async def test_per_tool_ceiling_aborts_with_a_specific_reason(self):
        llm = StubLLM(
            script=[
                _tool_use_response(
                    ToolUseBlock(id=f"t{i}", name="query_logs", input={"service": "auth"})
                )
                for i in range(5)
            ]
        )
        inv = _alert_investigation(budget=ToolBudget(per_tool_calls={"query_logs": 2}))

        outcome = await run_to_completion(inv, llm=llm, tools=default_tool_registry())

        assert isinstance(outcome, Aborted)
        self.assertEqual(outcome.reason, "budget")
        self.assertIn("query_logs", outcome.detail)


class TestToolFailureIsContained(unittest.IsolatedAsyncioTestCase):
    async def test_throwing_tool_becomes_evidence_not_a_crash(self):
        """The observability stack is often part of the outage. A dead backend
        must arrive as an error result the model can reason around."""
        tools = default_tool_registry()
        tools["query_logs"] = ExplodingTool()

        llm = StubLLM(
            script=[
                _tool_use_response(
                    ToolUseBlock(id="t1", name="query_logs", input={"service": "auth"})
                ),
                _tool_use_response(
                    TextBlock(text="Logs unavailable; concluding from metrics alone."),
                    _submit("Redis OOM (logs unavailable, metrics only)"),
                ),
            ]
        )
        inv = _alert_investigation()

        events = [e async for e in run(inv, llm=llm, tools=tools)]

        failed = next(e for e in events if isinstance(e, ToolReturned) and e.is_error)
        self.assertIn("connection refused", failed.content)
        self.assertIsInstance(events[-1], Done)
        # And the model got a chance to react to the failure.
        self.assertEqual(llm.turn, 2)

    async def test_unknown_tool_is_an_error_result_listing_what_exists(self):
        llm = StubLLM(
            script=[
                _tool_use_response(ToolUseBlock(id="tx", name="nonexistent_tool", input={})),
                _tool_use_response(_submit("recovered from a bad tool name")),
            ]
        )
        events = [
            e async for e in run(_alert_investigation(), llm=llm, tools=default_tool_registry())
        ]

        failed = next(e for e in events if isinstance(e, ToolReturned) and e.is_error)
        payload = json.loads(failed.content)
        self.assertIn("unknown tool", payload["error"])
        self.assertIn("query_metrics", payload["hint"])
        self.assertIsInstance(events[-1], Done)

    async def test_parallel_calls_in_one_response_are_all_dispatched(self):
        llm = StubLLM(
            script=[
                _tool_use_response(
                    ToolUseBlock(id="t1", name="query_metrics", input={"promql": "up"}),
                    ToolUseBlock(id="t2", name="query_logs", input={"service": "auth"}),
                    ToolUseBlock(id="t3", name="search_runbook", input={"service": "auth"}),
                ),
                _tool_use_response(_submit()),
            ]
        )
        inv = _alert_investigation()

        events = [e async for e in run(inv, llm=llm, tools=default_tool_registry())]

        returned = [e for e in events if isinstance(e, ToolReturned)]
        self.assertEqual(len([e for e in returned if not e.is_error]), 4)  # 3 + submit
        self.assertEqual(inv.tool_calls["query_metrics"], 1)
        self.assertEqual(sum(inv.tool_calls.values()), 4)


class TestInvestigationOwnsMessages(unittest.IsolatedAsyncioTestCase):
    async def test_chat_can_resume_on_the_same_investigation(self):
        """The refactor's whole point: state survives the call, so a second user
        turn continues the same conversation instead of starting over."""
        inv = _chat_investigation()
        tools = default_tool_registry()

        first = StubLLM(
            script=[
                Response(
                    stop_reason=StopReason.END_TURN,
                    content=[TextBlock(text="payment p99 is up.")],
                )
            ]
        )
        await run_to_completion(inv, llm=first, tools=tools)
        after_first = len(inv.messages)

        inv.add_user_text("and what about inventory?")
        second = StubLLM(
            script=[
                Response(
                    stop_reason=StopReason.END_TURN,
                    content=[TextBlock(text="inventory is healthy.")],
                )
            ]
        )
        outcome = await run_to_completion(inv, llm=second, tools=tools)

        self.assertGreater(len(inv.messages), after_first)
        assert isinstance(outcome, Done)
        self.assertIn("inventory", outcome.text or "")
        # The second call saw the first exchange.
        self.assertIn("payment p99 is up.", _all_text(inv.messages))
        self.assertIn("why is checkout slow?", _all_text(inv.messages))


class TestWindowPropagation(unittest.IsolatedAsyncioTestCase):
    async def test_every_tool_receives_the_pinned_window(self):
        recorder = WindowRecordingTool()
        tools = default_tool_registry()
        tools["query_metrics"] = recorder

        llm = StubLLM(
            script=[
                _tool_use_response(
                    ToolUseBlock(id="t1", name="query_metrics", input={"promql": "up"})
                ),
                _tool_use_response(_submit()),
            ]
        )
        inv = _alert_investigation()

        await run_to_completion(inv, llm=llm, tools=tools)

        self.assertEqual(recorder.seen, [inv.window])
        # And the window is anchored on the alert's T0, not on the wall clock.
        self.assertLess(inv.window.start, T0)
        self.assertGreater(inv.window.end, T0)

    async def test_model_supplied_window_is_ignored_not_honoured(self):
        """If the model tries to pass `window`, dispatch's keyword wins.

        `tool_schemas` already rejects a tool that *declares* `window`, so the
        model has no documented channel. This covers it inventing one anyway.
        """
        recorder = WindowRecordingTool()
        tools = {"query_metrics": recorder, "submit_report": default_tool_registry()["submit_report"]}

        llm = StubLLM(
            script=[
                _tool_use_response(
                    ToolUseBlock(
                        id="t1",
                        name="query_metrics",
                        input={"promql": "up", "window": {"start": "1970-01-01", "end": "1970-01-02"}},
                    )
                ),
                _tool_use_response(_submit()),
            ]
        )
        inv = _alert_investigation()

        events = [e async for e in run(inv, llm=llm, tools=tools)]

        # Duplicate keyword: the call fails loudly as a contained error rather
        # than silently using the model's range.
        failed = [e for e in events if isinstance(e, ToolReturned) and e.is_error]
        self.assertEqual(len(failed), 1)
        self.assertIn("invalid arguments", json.loads(failed[0].content)["error"])
        self.assertEqual(recorder.seen, [], "the tool must not have run with a model-set window")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _all_text(messages: Sequence[Message]) -> str:
    out: list[str] = []
    for msg in messages:
        for block in msg.content:
            text = getattr(block, "text", None)
            if text:
                out.append(text)
    return "\n".join(out)


def _unanswered_tool_uses(messages: Sequence[Message]) -> list[str]:
    requested: list[str] = []
    answered: set[str] = set()
    for msg in messages:
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                requested.append(block.id)
            tool_use_id = getattr(block, "tool_use_id", None)
            if tool_use_id:
                answered.add(tool_use_id)
    return [rid for rid in requested if rid not in answered]


if __name__ == "__main__":
    unittest.main()
