"""Week 2 L4a: the traceability spine.

Every test here fails against the L3b code, which is the point — the audit in
[TRADEOFFS §42](../../TRADEOFFS.md) found four instrument sources and zero sinks, and
no timing anywhere in `agent/`, so before this lesson there was nothing to assert on.

Four things are being established:

1. four span levels come out with correct parenting, including under the concurrent
   tool dispatch `asyncio.gather` performs,
2. durations exist and are real numbers, so the declared latency metrics become
   computable,
3. every exit path stamps an outcome — an aborted run is the case that used to leave
   nothing behind,
4. tracing off changes nothing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import unittest
from datetime import datetime, timezone
from typing import Any

from agent.core.investigation import Investigation, ToolBudget, Window
from agent.core.loop import run, run_to_completion
from agent.core.trace import (
    ATTEMPT,
    INVESTIGATION,
    LLM_CALL,
    TOOL_CALL,
    TURN,
    NULL_TRACE,
    Span,
    Trace,
    current_correlation_id,
    current_parent,
    current_trace,
    log_sink,
    span,
    tree,
)
from agent.llm.stub import StubLLM
from agent.llm.types import Response, StopReason, TextBlock, ToolUseBlock
from agent.tools.protocol import ToolMeta
from agent.tools.stubs import default_tool_registry

T0 = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)

ALERT = {
    "_meta": {"purpose": "must never reach the model"},
    "alert_name": "HighErrorRate",
    "commonLabels": {"service": "auth", "severity": "P1"},
    "commonAnnotations": {"correlation_id": "gs-res-001-redis-oom"},
}


def _tracked() -> tuple[Trace, list[Span]]:
    """A trace with a list sink and a fake clock.

    The clock advances one millisecond per reading, so every duration is exact and
    non-zero — a real `perf_counter` on a stub run can legitimately return the same
    value twice and make a "duration is positive" assertion flaky.
    """
    ticks = iter(range(1_000_000))
    spans: list[Span] = []
    trace = Trace(
        trace_id="inv_test",
        correlation_id="gs-res-001-redis-oom",
        sinks=[spans.append],
        clock=lambda: next(ticks) / 1000.0,
        wall=lambda: 1_780_000_000.0,
    )
    return trace, spans


def _response(*blocks: Any) -> Response:
    return Response(stop_reason=StopReason.TOOL_USE, content=list(blocks))


def _submit() -> ToolUseBlock:
    return ToolUseBlock(
        id="t_submit",
        name="submit_report",
        input={"root_cause": "redis oom", "confidence": "high"},
    )


class SlowTool:
    """Two concurrent calls to this overlap, which is what makes the gather-context
    question a real one rather than a theoretical one."""

    name = "query_metrics"
    description = "sleeps, so concurrency is observable"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"promql": {"type": "string"}},
        "required": ["promql"],
    }
    meta = ToolMeta()

    async def run(self, *, window: Window, **kwargs: Any) -> str:
        await asyncio.sleep(0.01)
        return json.dumps({"promql": kwargs.get("promql")})


class TestSpanLevels(unittest.IsolatedAsyncioTestCase):
    async def test_investigation_and_turn_spans_nest(self):
        trace, spans = _tracked()
        llm = StubLLM(script=[_response(TextBlock(text="checking"), _submit())])
        inv = Investigation.from_alert(ALERT, t0=T0)

        await run_to_completion(inv, llm=llm, tools=default_tool_registry(), trace=trace)

        by_name = {s.name: s for s in spans}
        self.assertIn(INVESTIGATION, by_name)
        self.assertIn(TURN, by_name)
        self.assertIn(TOOL_CALL, by_name)
        self.assertIsNone(by_name[INVESTIGATION].parent_id)
        self.assertEqual(by_name[TURN].parent_id, by_name[INVESTIGATION].span_id)
        self.assertEqual(by_name[TOOL_CALL].parent_id, by_name[TURN].span_id)

    async def test_every_span_carries_the_alerts_correlation_id(self):
        """The join into the observed system's own logs. Week 1 stamps this id on
        every JSON log line the mock services emit."""
        trace, spans = _tracked()
        llm = StubLLM(script=[_response(_submit())])
        inv = Investigation.from_alert(ALERT, t0=T0)

        self.assertEqual(inv.correlation_id, "gs-res-001-redis-oom")
        await run_to_completion(inv, llm=llm, tools=default_tool_registry(), trace=trace)

        self.assertTrue(spans)
        self.assertTrue(all(s.correlation_id == "gs-res-001-redis-oom" for s in spans))

    async def test_trace_id_is_ours_and_the_correlation_id_is_theirs(self):
        """Two investigations from one alert share a correlation id and must not
        share a trace id — L4b's R3 rule mints exactly that pair, and a rerun of a
        golden case does too."""
        first = Investigation.from_alert(ALERT, t0=T0)
        second = Investigation.from_alert(ALERT, t0=T0)

        self.assertEqual(first.correlation_id, second.correlation_id)
        self.assertNotEqual(first.id, second.id)

    async def test_concurrent_tool_calls_each_parent_to_their_turn(self):
        """`asyncio.gather` wraps each coroutine in a Task and a Task copies the
        context, so sibling spans cannot steal each other's parent. If they could,
        the tree would nest tool calls inside one another at random."""
        trace, spans = _tracked()
        llm = StubLLM(
            script=[
                _response(
                    ToolUseBlock(id="t1", name="query_metrics", input={"promql": "a"}),
                    ToolUseBlock(id="t2", name="query_metrics", input={"promql": "b"}),
                    ToolUseBlock(id="t3", name="query_metrics", input={"promql": "c"}),
                ),
                _response(_submit()),
            ]
        )
        inv = Investigation.from_alert(ALERT, t0=T0)
        tools = {**default_tool_registry(), "query_metrics": SlowTool()}

        await run_to_completion(inv, llm=llm, tools=tools, trace=trace)

        turns = [s for s in spans if s.name == TURN]
        first_turn = turns[0]
        siblings = [
            s for s in spans if s.name == TOOL_CALL and s.parent_id == first_turn.span_id
        ]
        self.assertEqual(len(siblings), 3, "all three tool calls parent to turn 0")
        self.assertEqual(len({s.attrs["args_hash"] for s in siblings}), 3)

    async def test_durations_are_recorded_at_every_level(self):
        trace, spans = _tracked()
        llm = StubLLM(
            script=[
                _response(
                    TextBlock(text="looking"),
                    ToolUseBlock(id="t1", name="query_metrics", input={"promql": "a"}),
                ),
                _response(_submit()),
            ]
        )
        inv = Investigation.from_alert(ALERT, t0=T0)

        await run_to_completion(inv, llm=llm, tools=default_tool_registry(), trace=trace)

        self.assertTrue(spans)
        for recorded in spans:
            self.assertIsNotNone(recorded.duration_ms, f"{recorded.name} has no duration")
            self.assertGreater(recorded.duration_ms, 0.0)

        profile = trace.profile()
        self.assertGreater(profile["elapsed_ms"], 0.0)
        self.assertEqual(profile["tool_calls"], 2)
        # No gateway in this test, so no llm.call spans — the profile still computes
        # rather than dividing by zero, which is what a stub run must not do.
        self.assertEqual(profile["llm_calls"], 0)


class TestOutcomeIsAlwaysRecorded(unittest.IsolatedAsyncioTestCase):
    async def test_done_stamps_the_root_span(self):
        trace, spans = _tracked()
        llm = StubLLM(script=[_response(_submit())])
        inv = Investigation.from_alert(ALERT, t0=T0)

        await run_to_completion(inv, llm=llm, tools=default_tool_registry(), trace=trace)

        root = trace.root()
        assert root is not None
        self.assertEqual(root.attrs["outcome"], "Done")
        self.assertEqual(root.attrs["reason"], "")
        self.assertEqual(root.attrs["trigger"], "alert")

    async def test_an_aborted_run_leaves_the_reason_on_the_root_span(self):
        """The case §42 singles out: before L4a a failed run left nothing behind but
        an `Aborted` reason string, with no evidence of what it had done first."""
        trace, spans = _tracked()
        llm = StubLLM(
            script=[
                _response(
                    ToolUseBlock(id="t1", name="query_metrics", input={"promql": "a"})
                ),
                Response(stop_reason=StopReason.END_TURN, content=[TextBlock(text="dunno")]),
            ]
        )
        inv = Investigation.from_alert(ALERT, t0=T0)

        await run_to_completion(inv, llm=llm, tools=default_tool_registry(), trace=trace)

        root = trace.root()
        assert root is not None
        self.assertEqual(root.attrs["outcome"], "Aborted")
        self.assertEqual(root.attrs["reason"], "no_report")
        # And the work done before the abort is still on the record.
        self.assertEqual(root.attrs["tool_calls"], 1)
        self.assertEqual(len([s for s in spans if s.name == TOOL_CALL]), 1)

    async def test_max_turns_records_how_far_it_got(self):
        trace, spans = _tracked()
        script = [
            _response(ToolUseBlock(id=f"t{i}", name="query_metrics", input={"promql": f"q{i}"}))
            for i in range(4)
        ]
        inv = Investigation.from_alert(
            ALERT, t0=T0, budget=ToolBudget(max_turns=2, repeat_tool_calls=0)
        )

        outcome = await run_to_completion(
            inv, llm=StubLLM(script=script), tools=default_tool_registry(), trace=trace
        )

        self.assertEqual(getattr(outcome, "reason", None), "max_turns")
        root = trace.root()
        assert root is not None
        self.assertEqual(root.attrs["reason"], "max_turns")
        self.assertEqual(len([s for s in spans if s.name == TURN]), 2)


class TestAmbientContext(unittest.IsolatedAsyncioTestCase):
    async def test_nothing_installed_means_nothing_recorded(self):
        """Tracing off must be free and invisible, or every call site needs an
        `if tracing:` and coverage becomes something to remember."""
        self.assertIs(current_trace(), NULL_TRACE)
        with span("anything", a=1) as sp:
            sp.set(b=2)
            self.assertEqual(sp.span_id, "")
        self.assertEqual(NULL_TRACE.spans, [])

    async def test_the_run_restores_the_previous_ambient_state(self):
        trace, _ = _tracked()
        llm = StubLLM(script=[_response(_submit())])
        inv = Investigation.from_alert(ALERT, t0=T0)

        await run_to_completion(inv, llm=llm, tools=default_tool_registry(), trace=trace)

        self.assertIs(current_trace(), NULL_TRACE)
        self.assertIsNone(current_parent())

    async def test_spans_survive_a_stream_driven_from_a_task_then_abandoned(self):
        """Why the parent is restored by value rather than by token.

        The `turn` span stays open across `yield`, and an async generator runs in its
        *caller's* context. Drive one step from a Task and close it from the caller —
        which is what chat does when a request is cancelled — and the token was minted
        in a context that no longer exists:

            ValueError: <Token ...> was created in a different Context

        Verified against CPython 3.14: `reset(token)` raises here, and so does the
        variant where the generator is stepped from two different tasks. Restoring by
        value cannot fail that way. Without it, closing an abandoned investigation
        raises out of `aclose()` and buries the real reason the run ended.
        """
        trace, spans = _tracked()
        llm = StubLLM(
            script=[
                _response(
                    TextBlock(text="thinking"),
                    ToolUseBlock(id="t1", name="query_metrics", input={"promql": "a"}),
                ),
                _response(_submit()),
            ]
        )
        inv = Investigation.from_alert(ALERT, t0=T0)

        stream = run(inv, llm=llm, tools=default_tool_registry(), trace=trace)
        # A Task gets its own copy of the context, so the span opened in here is
        # closed later from a context that never saw the token.
        await asyncio.create_task(stream.__anext__())  # TurnStarted; turn span open
        await stream.aclose()

        self.assertEqual([s.status for s in spans if s.name == TURN], ["abandoned"])
        self.assertIsNone(current_parent())

    async def test_a_tool_can_read_the_correlation_id_without_a_new_parameter(self):
        """How a real backend query gets tagged in W3 L2 — ambient, so `Tool.run`
        needs no second reserved keyword beside `window`."""
        trace, _ = _tracked()
        with trace.install():
            self.assertEqual(current_correlation_id(), "gs-res-001-redis-oom")
        self.assertEqual(current_correlation_id(), "")


class TestSinks(unittest.TestCase):
    def test_a_broken_sink_does_not_end_the_investigation(self):
        """Losing telemetry is bad. Losing the incident response because telemetry
        broke is worse."""
        good: list[Span] = []

        def explode(_: Span) -> None:
            raise RuntimeError("langfuse is down")

        trace = Trace(trace_id="t", sinks=[explode, good.append])
        with self.assertLogs("sre_agent.trace", level="ERROR"):
            with trace.span("investigation"):
                pass

        self.assertEqual(len(good), 1)

    def test_log_sink_emits_one_json_line_per_span(self):
        logger = logging.getLogger("test.trace.sink")
        trace = Trace(trace_id="t", correlation_id="cid-1", sinks=[log_sink(logger)])

        with self.assertLogs(logger, level="INFO") as captured:
            with trace.span(TOOL_CALL, tool="query_logs"):
                pass

        payload = json.loads(captured.records[0].getMessage())
        self.assertEqual(payload["name"], TOOL_CALL)
        self.assertEqual(payload["tool"], "query_logs")
        # Same key the mock's own `_JsonFormatter` uses, so both streams grep alike.
        self.assertEqual(payload["correlation_id"], "cid-1")

    def test_tree_orders_parents_before_children(self):
        """Completion order is leaf-first, because a parent closes after its
        children. Replay would print the tree upside down without this."""
        trace = Trace(trace_id="t")
        with trace.span(INVESTIGATION):
            with trace.span(TURN):
                with trace.span(LLM_CALL):
                    trace.record(ATTEMPT, duration_ms=1.0, attempt=1)

        self.assertEqual(
            [s.name for s in trace.spans],
            [ATTEMPT, LLM_CALL, TURN, INVESTIGATION],
            "sanity: the raw list really is leaf-first",
        )
        self.assertEqual(
            [(depth, r["name"]) for depth, r in tree(trace.spans)],
            [(0, INVESTIGATION), (1, TURN), (2, LLM_CALL), (3, ATTEMPT)],
        )

    def test_an_orphaned_span_is_shown_rather_than_dropped(self):
        """A log truncated mid-write loses the parent. A partial tree is still
        evidence; a silently shorter tree is a lie."""
        orphan = {"span_id": "s9", "parent_id": "s8", "name": "tool.call"}
        self.assertEqual([d for d, _ in tree([orphan])], [0])


if __name__ == "__main__":
    unittest.main()
