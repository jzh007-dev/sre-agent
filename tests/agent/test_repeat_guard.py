"""Week 2 L4a: the model-side circuit breaker.

Six ceilings already guarantee a runaway *stops* — `max_turns`, `max_tool_calls`,
`per_tool_calls`, per-currency `max_cost`, the provider breaker, and `max_tokens`.
What was missing is the *diagnosis*: `Investigation.tool_calls` counts by tool name,
so twelve different queries and the same query twelve times were indistinguishable
and the recorded reason said `max_turns` either way
([TRADEOFFS §42](../../TRADEOFFS.md)).

The guard is deliberately **recoverable where the provider breaker is fatal**: it
returns an error result carrying the previous answer and a nudge, so the model can
route around it. Same shape as `safe_dispatch` turning a dead backend into evidence.

The two tests that matter are the pair: the third *identical* call is refused, and
twelve *different* calls are not. A guard that only satisfied the first would be
indistinguishable from a broken tool-call limit.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from typing import Any

from agent.core.investigation import Investigation, ToolBudget, Window, args_hash
from agent.core.loop import run_to_completion
from agent.core.trace import TOOL_CALL, Span, Trace
from agent.llm.stub import StubLLM
from agent.llm.types import Response, StopReason, ToolUseBlock
from agent.tools.stubs import default_tool_registry

T0 = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _investigation(**budget: Any) -> Investigation:
    inv = Investigation(
        id="inv_repeat", trigger="alert", window=Window.around(T0), budget=ToolBudget(**budget)
    )
    inv.add_user_text("<alert>{}</alert>")
    return inv


def _call(n: int, promql: str) -> Response:
    return Response(
        stop_reason=StopReason.TOOL_USE,
        content=[ToolUseBlock(id=f"t{n}", name="query_metrics", input={"promql": promql})],
    )


def _submit(n: int) -> Response:
    return Response(
        stop_reason=StopReason.TOOL_USE,
        content=[
            ToolUseBlock(
                id=f"t{n}",
                name="submit_report",
                input={"root_cause": "redis oom", "confidence": "low"},
            )
        ],
    )


def _tracked() -> tuple[Trace, list[Span]]:
    spans: list[Span] = []
    return Trace(trace_id="inv_repeat", sinks=[spans.append]), spans


class TestArgsHash(unittest.TestCase):
    def test_key_order_does_not_change_the_hash(self):
        """Two calls the model emitted with the keys in a different order are the
        same call. Without `sort_keys` the guard would never fire on a real model."""
        self.assertEqual(
            args_hash({"promql": "up", "step": "1m"}),
            args_hash({"step": "1m", "promql": "up"}),
        )

    def test_different_arguments_hash_differently(self):
        self.assertNotEqual(args_hash({"promql": "up"}), args_hash({"promql": "down"}))

    def test_an_unserializable_value_does_not_raise(self):
        """A hash that over-distinguishes is a missed detection; one that raises would
        take down the dispatch path the guard exists to protect."""
        self.assertTrue(args_hash({"when": datetime(2026, 1, 1)}))


class TestRepeatGuard(unittest.IsolatedAsyncioTestCase):
    async def test_the_third_identical_call_is_refused_and_carries_the_previous_result(self):
        trace, spans = _tracked()
        inv = _investigation(max_turns=8)
        llm = StubLLM(
            script=[_call(1, "up"), _call(2, "up"), _call(3, "up"), _submit(4)]
        )

        outcome = await run_to_completion(
            inv, llm=llm, tools=default_tool_registry(), trace=trace
        )

        refused = [s for s in spans if s.name == TOOL_CALL and s.attrs.get("repeat_refused")]
        self.assertEqual(len(refused), 1, "exactly the third identical call")
        self.assertFalse(refused[0].attrs["dispatched"])

        # The refusal is the tool_result the model saw on its next turn.
        results = [
            block
            for message in inv.messages
            if message.role == "user"
            for block in message.content
            if getattr(block, "is_error", False)
        ]
        payload = json.loads(results[0].content)
        self.assertEqual(payload["error"], "repeated identical call")
        self.assertEqual(payload["identical_calls"], 3)
        self.assertIn("previous_result", payload)
        self.assertIn("query_metrics", payload["hint"])
        # Recoverable, not fatal: the run went on to deliver.
        self.assertEqual(type(outcome).__name__, "Done")

    async def test_twelve_different_calls_are_never_refused(self):
        """The other half of the pair. A guard keyed on tool name alone would fire
        here, and firing here would make the agent worse at legitimate work."""
        trace, spans = _tracked()
        inv = _investigation(max_turns=20, max_tool_calls=40)
        llm = StubLLM(script=[_call(i, f"query_{i}") for i in range(12)] + [_submit(99)])

        await run_to_completion(inv, llm=llm, tools=default_tool_registry(), trace=trace)

        self.assertEqual(
            [s for s in spans if s.attrs.get("repeat_refused")],
            [],
            "twelve distinct queries are legitimate investigation, not a runaway",
        )
        self.assertEqual(inv.tool_calls["query_metrics"], 12)
        distinct = [key for key in inv.repeat_calls if key[0] == "query_metrics"]
        self.assertEqual(len(distinct), 12, "twelve distinct (tool, args) keys")
        self.assertEqual(
            max(inv.repeat_calls[key] for key in distinct), 1, "none of them repeated"
        )

    async def test_the_guard_stays_shut_if_the_nudge_is_ignored(self):
        """A model that keeps repeating gets refused every time rather than sneaking
        through once the counter has passed the threshold."""
        trace, spans = _tracked()
        inv = _investigation(max_turns=10)
        llm = StubLLM(script=[_call(i, "up") for i in range(6)] + [_submit(99)])

        await run_to_completion(inv, llm=llm, tools=default_tool_registry(), trace=trace)

        refused = [s for s in spans if s.attrs.get("repeat_refused")]
        self.assertEqual(len(refused), 4, "calls 3 through 6")

    async def test_a_refused_call_still_counts_toward_the_tool_ceiling(self):
        """Otherwise a model stuck in a loop would spin for free until max_turns, and
        the ceiling that is supposed to bound cost would not bound anything."""
        inv = _investigation(max_turns=20, max_tool_calls=5)
        llm = StubLLM(script=[_call(i, "up") for i in range(10)])

        outcome = await run_to_completion(inv, llm=llm, tools=default_tool_registry())

        self.assertEqual(getattr(outcome, "reason", None), "budget")
        self.assertEqual(sum(inv.tool_calls.values()), 5)

    async def test_the_root_span_reports_how_much_was_repetition(self):
        """What turns `max_turns` from a verdict into a diagnosis."""
        trace, _ = _tracked()
        inv = _investigation(max_turns=6)
        llm = StubLLM(script=[_call(i, "up") for i in range(4)] + [_submit(99)])

        await run_to_completion(inv, llm=llm, tools=default_tool_registry(), trace=trace)

        root = trace.root()
        assert root is not None
        self.assertEqual(root.attrs["repeated_calls"], 3, "four calls, one distinct")

    async def test_the_guard_can_be_switched_off(self):
        """Eval needs a configuration where the agent's own behaviour is measured
        rather than the guard's, so zero disables it."""
        trace, spans = _tracked()
        inv = _investigation(max_turns=8, repeat_tool_calls=0)
        llm = StubLLM(script=[_call(i, "up") for i in range(5)] + [_submit(99)])

        await run_to_completion(inv, llm=llm, tools=default_tool_registry(), trace=trace)

        self.assertEqual([s for s in spans if s.attrs.get("repeat_refused")], [])


class TestGuardUnit(unittest.TestCase):
    def test_the_guard_is_silent_until_the_threshold(self):
        inv = _investigation()
        digest = args_hash({"promql": "up"})

        self.assertIsNone(inv.repeat_guard("query_metrics", digest))
        inv.record_tool_call("query_metrics", digest, "first result")
        self.assertIsNone(inv.repeat_guard("query_metrics", digest))
        inv.record_tool_call("query_metrics", digest, "second result")

        refusal = inv.repeat_guard("query_metrics", digest)
        assert refusal is not None
        self.assertEqual(json.loads(refusal)["previous_result"], "second result")

    def test_a_stored_result_is_truncated(self):
        """One full ClickHouse page per distinct call would multiply what the
        investigation holds, for a message that only needs to be recognisable."""
        inv = _investigation()
        digest = args_hash({"promql": "up"})
        for _ in range(2):
            inv.record_tool_call("query_metrics", digest, "x" * 50_000)

        refusal = inv.repeat_guard("query_metrics", digest)
        assert refusal is not None
        self.assertEqual(len(json.loads(refusal)["previous_result"]), 2_000)


if __name__ == "__main__":
    unittest.main()
