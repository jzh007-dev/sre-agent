"""Gateway: cache, budget gate, fallback, ledger, tracing, and loop integration.

The two tests that matter most here are `test_cache_hit_still_charges_the_budget`
and `test_fallback_is_disabled_when_configured_off` — both encode decisions that
are invisible until they are wrong, and wrong in a way that quietly invalidates
eval numbers rather than crashing.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from agent.core.events import Aborted, Done
from agent.core.investigation import Investigation, ToolBudget, Window
from agent.core.loop import run_to_completion
from agent.core.trace import ATTEMPT, LLM_CALL, Span, Trace
from agent.llm import errors
from agent.llm.cache import FileStore, MemoryStore, ResponseCache
from agent.llm.cost import Ledger
from agent.llm.gateway import Gateway
from agent.llm.protocol import BudgetExceeded, ProviderUnavailable
from agent.llm.routing import CallKind, RoutingConfig, default_config
from agent.llm.transport import CircuitBreaker, RetryPolicy, Transport
from agent.llm.types import Message, Response, StopReason, TextBlock, ToolUseBlock
from agent.llm.usage import Usage
from agent.tools.stubs import default_tool_registry

T0 = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _other_messages():
    """A distinct conversation, so the cache key differs and the budget gate is
    the thing under test rather than a cache hit."""
    return [Message(role="user", content=[TextBlock(text="a different question")])]


def _investigation(**budget_kwargs) -> Investigation:
    return Investigation(
        id="inv_test",
        trigger="alert",
        window=Window.around(T0),
        budget=ToolBudget(**budget_kwargs) if budget_kwargs else ToolBudget(),
    )


#: DeepSeek bills in CNY (rates verified from its invoice), so a CNY ceiling is what
#: gates it. Ceilings are per currency because costs are never converted.
def _ceiling(amount: float, currency: str = "CNY") -> dict[str, float]:
    return {currency: amount}


class FakeAdapter:
    """Scripted adapter. Each entry is an exception to raise or a (Response, Usage)."""

    def __init__(self, provider: str, script=None, usage=Usage(1000, 100)):
        self.provider = provider
        self.script = list(script or [])
        self.usage = usage
        self.calls = 0

    async def send(self, request):
        self.calls += 1
        if self.script:
            outcome = self.script.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return (
            Response(
                stop_reason=StopReason.END_TURN,
                content=[TextBlock(text=f"answer from {self.provider}")],
            ),
            self.usage,
        )


async def _noop_sleep(_: float) -> None:
    return None


def _gateway(adapters: dict, routing=None, **kwargs) -> Gateway:
    transports = {
        name: Transport(
            adapter=adapter,
            policy=RetryPolicy(max_attempts=2, jitter=False),
            breaker=CircuitBreaker(now=lambda: 0.0),
            sleeper=_noop_sleep,
        )
        for name, adapter in adapters.items()
    }
    return Gateway(
        routing=routing or default_config(),
        transports=transports,
        cache=kwargs.pop("cache", ResponseCache(store=MemoryStore())),
        **kwargs,
    )


class TestCacheAndBudget(unittest.IsolatedAsyncioTestCase):
    async def test_second_identical_call_is_served_from_cache(self):
        adapter = FakeAdapter("deepseek")
        gw = _gateway({"deepseek": adapter})
        inv = _investigation()
        llm = gw.bind(inv)

        first = await llm.call([], [])
        second = await llm.call([], [])

        self.assertEqual(adapter.calls, 1, "the provider must be hit once")
        self.assertEqual(first.content[0].text, second.content[0].text)
        self.assertEqual(gw.cache.hits, 1)
        self.assertEqual(gw.cache.hit_rate, 0.5)

    async def test_cache_hit_still_charges_the_budget(self):
        """The delta that keeps eval reproducible: a hit costs no money but must
        still charge the ceiling, or a run that degraded on budget would stop
        degrading on rerun. TRADEOFFS §33 delta 3."""
        gw = _gateway({"deepseek": FakeAdapter("deepseek")})
        ledger = Ledger(investigation_id="inv_test")
        llm = gw.bind(_investigation(), ledger=ledger)

        await llm.call([], [])
        await llm.call([], [])

        self.assertEqual(ledger.calls, 2)
        self.assertEqual(ledger.cached_calls, 1)
        # Money counts one call; the budget counts both. Per currency, no conversion.
        self.assertAlmostEqual(
            ledger.budget_charged["CNY"], ledger.money_spent["CNY"] * 2, places=9
        )
        self.assertGreater(ledger.money_spent["CNY"], 0.0)

    async def test_budget_exhaustion_refuses_before_sending(self):
        adapter = FakeAdapter("deepseek", usage=Usage(1_000_000, 0))  # 1.00 CNY
        gw = _gateway({"deepseek": adapter})
        inv = _investigation(max_cost=_ceiling(0.10))
        llm = gw.bind(inv)

        await llm.call([], [])  # spends past the ceiling
        with self.assertRaises(BudgetExceeded) as ctx:
            await llm.call(_other_messages(), [])  # different key, so no cache hit

        self.assertEqual(adapter.calls, 1, "the second call must not reach the provider")
        self.assertGreater(ctx.exception.spent, ctx.exception.ceiling)
        self.assertEqual(ctx.exception.currency, "CNY")

    async def test_cache_hit_is_free_of_money_but_can_still_trip_the_budget(self):
        """A hit is served even when it would exceed the ceiling — it costs nothing,
        so refusing it would be pointless — but the charge it replays is what makes
        the *next* uncached call refuse."""
        adapter = FakeAdapter("deepseek", usage=Usage(1_000_000, 0))
        gw = _gateway({"deepseek": adapter})
        ledger = Ledger(investigation_id="inv_test")
        llm = gw.bind(_investigation(max_cost=_ceiling(0.10)), ledger=ledger)

        await llm.call([], [])
        await llm.call([], [])  # cache hit — served, not refused

        self.assertEqual(adapter.calls, 1)
        self.assertEqual(ledger.cached_calls, 1)

    async def test_file_store_survives_a_new_process(self):
        import tempfile
        import pathlib

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "cache.jsonl"

            adapter = FakeAdapter("deepseek")
            gw = _gateway({"deepseek": adapter}, cache=ResponseCache(store=FileStore(path)))
            await gw.bind(_investigation()).call([], [])
            self.assertEqual(adapter.calls, 1)

            # A fresh gateway with a fresh store reading the same file: this is the
            # scenario that matters — the same eval run repeated after a prompt edit.
            adapter2 = FakeAdapter("deepseek")
            gw2 = _gateway({"deepseek": adapter2}, cache=ResponseCache(store=FileStore(path)))
            response = await gw2.bind(_investigation()).call([], [])

            self.assertEqual(adapter2.calls, 0, "must be served from the file")
            self.assertEqual(response.content[0].text, "answer from deepseek")

    async def test_tool_use_response_round_trips_through_the_cache(self):
        script = [
            (
                Response(
                    stop_reason=StopReason.TOOL_USE,
                    content=[
                        TextBlock(text="checking"),
                        ToolUseBlock(id="t1", name="query_metrics", input={"promql": "up"}),
                    ],
                ),
                Usage(100, 20),
            )
        ]
        import tempfile
        import pathlib

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "c.jsonl"
            gw = _gateway(
                {"deepseek": FakeAdapter("deepseek", script)},
                cache=ResponseCache(store=FileStore(path)),
            )
            await gw.bind(_investigation()).call([], [])

            gw2 = _gateway(
                {"deepseek": FakeAdapter("deepseek")}, cache=ResponseCache(store=FileStore(path))
            )
            restored = await gw2.bind(_investigation()).call([], [])

        self.assertEqual(restored.stop_reason, StopReason.TOOL_USE)
        block = restored.content[1]
        self.assertEqual(block.name, "query_metrics")
        self.assertEqual(block.input, {"promql": "up"})


class TestFallback(unittest.IsolatedAsyncioTestCase):
    def _two_provider_routing(self) -> RoutingConfig:
        return RoutingConfig(
            agent_model="deepseek-v4-flash",
            assignments={
                CallKind.JUDGE: "claude-sonnet-5",
                CallKind.REVIEWER: "claude-sonnet-5",
            },
            fallbacks={CallKind.MAIN_LOOP: ("qwen-plus",)},
        )

    async def test_fallback_runs_the_second_provider_and_tags_the_ledger(self):
        dead = FakeAdapter("deepseek", [errors.ServerError("500", status=500)] * 2)
        alive = FakeAdapter("qwen")
        gw = _gateway(
            {"deepseek": dead, "qwen": alive}, routing=self._two_provider_routing()
        )
        ledger = Ledger(investigation_id="inv_test")

        response = await gw.bind(_investigation(), ledger=ledger).call([], [])

        self.assertEqual(response.content[0].text, "answer from qwen")
        self.assertTrue(ledger.fell_back, "a fallen-back run must be identifiable")

    async def test_fallback_is_disabled_when_configured_off(self):
        """Eval wiring. A run that silently continued on a second provider has an
        accuracy attributable to neither model and a cost mixing two price sheets.
        TRADEOFFS §33 delta 9."""
        dead = FakeAdapter("deepseek", [errors.ServerError("500", status=500)] * 2)
        alive = FakeAdapter("qwen")
        gw = _gateway(
            {"deepseek": dead, "qwen": alive},
            routing=self._two_provider_routing(),
            allow_fallback=False,
        )

        with self.assertRaises(ProviderUnavailable):
            await gw.bind(_investigation()).call([], [])

        self.assertEqual(alive.calls, 0, "eval must never silently switch providers")

    async def test_budget_exhaustion_does_not_burn_the_fallback_chain(self):
        """A ceiling is ours, not the provider's — another provider would refuse
        identically, so trying one would only obscure the reason."""
        adapter = FakeAdapter("deepseek", usage=Usage(1_000_000, 0))
        alive = FakeAdapter("qwen")
        gw = _gateway(
            {"deepseek": adapter, "qwen": alive}, routing=self._two_provider_routing()
        )
        llm = gw.bind(_investigation(max_cost=_ceiling(0.10)))

        await llm.call([], [])
        with self.assertRaises(BudgetExceeded):
            await llm.call(_other_messages(), [])

        self.assertEqual(alive.calls, 0)

    async def test_non_retryable_error_still_tries_the_fallback_provider(self):
        """A 400 from one provider may be a schema quirk the next one accepts, so
        fallback is worth attempting even though retrying the same provider is not."""
        picky = FakeAdapter("deepseek", [errors.InvalidRequest("400", status=400)])
        alive = FakeAdapter("qwen")
        gw = _gateway(
            {"deepseek": picky, "qwen": alive}, routing=self._two_provider_routing()
        )
        response = await gw.bind(_investigation()).call([], [])
        self.assertEqual(response.content[0].text, "answer from qwen")
        self.assertEqual(picky.calls, 1, "no retry against the same provider")


class TestLedgerAndTrace(unittest.IsolatedAsyncioTestCase):
    async def test_trace_carries_version_stamps_and_retry_count(self):
        spans: list[Span] = []
        # One timeout then success: the adapter falls through to its default result
        # once the script is exhausted, so the second attempt succeeds.
        gw = _gateway(
            {"deepseek": FakeAdapter("deepseek", [errors.Timeout("t")])},
            trace=Trace(trace_id="t", sinks=[spans.append]),
        )

        await gw.bind(_investigation()).call([], [])

        calls = [s for s in spans if s.name == LLM_CALL]
        self.assertEqual(len(calls), 1)
        payload = calls[0].as_dict()
        self.assertEqual(payload["attempts"], 2)
        self.assertTrue(payload["retried"])
        self.assertFalse(payload["cache_hit"])
        self.assertIn("price_table_version", payload)
        self.assertTrue(payload["prices_verified"], "verified against the provider invoice")
        self.assertEqual(payload["currency"], "CNY")
        self.assertEqual(payload["call_kind"], "main_loop")
        # Duration is the point of the shape change: the retry count alone cannot
        # distinguish a slow provider from two attempts.
        self.assertIsNotNone(payload["duration_ms"])

    async def test_each_transport_attempt_becomes_a_child_span(self):
        """The audit's second finding: `Attempt.error_class` and `delay_before` were
        written on every attempt and read by nothing."""
        spans: list[Span] = []
        gw = _gateway(
            {"deepseek": FakeAdapter("deepseek", [errors.Timeout("t")])},
            trace=Trace(trace_id="t", sinks=[spans.append]),
        )

        await gw.bind(_investigation()).call([], [])

        attempts = [s for s in spans if s.name == ATTEMPT]
        call = next(s for s in spans if s.name == LLM_CALL)
        self.assertEqual([s.attrs["attempt"] for s in attempts], [1, 2])
        self.assertEqual(attempts[0].attrs["error_class"], "Timeout")
        self.assertEqual(attempts[0].status, "error")
        self.assertEqual(attempts[1].attrs["error_class"], "")
        self.assertTrue(
            all(s.parent_id == call.span_id for s in attempts),
            "attempts must nest under the llm.call they belong to",
        )

    async def test_attempts_are_traced_even_when_the_call_ultimately_fails(self):
        """The path where the returned attempt list never arrives, which is the one
        worth having evidence from."""
        spans: list[Span] = []
        gw = _gateway(
            {"deepseek": FakeAdapter("deepseek", [errors.ServerError("500", status=500)] * 2)},
            trace=Trace(trace_id="t", sinks=[spans.append]),
            allow_fallback=False,
        )

        with self.assertRaises(ProviderUnavailable):
            await gw.bind(_investigation()).call([], [])

        self.assertEqual(len([s for s in spans if s.name == ATTEMPT]), 2)
        call = next(s for s in spans if s.name == LLM_CALL)
        self.assertEqual(call.status, "error")
        self.assertIn("ServerError", call.attrs["error"])

    async def test_ledger_summary_reports_both_totals(self):
        gw = _gateway({"deepseek": FakeAdapter("deepseek")})
        ledger = Ledger(investigation_id="inv_test")
        llm = gw.bind(_investigation(), ledger=ledger)
        await llm.call([], [])
        await llm.call([], [])

        summary = ledger.summary()
        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["cached_calls"], 1)
        self.assertLess(summary["money_spent"]["CNY"], summary["budget_charged"]["CNY"])
        self.assertTrue(
            summary["prices_verified"],
            "DeepSeek rates are verified against its own invoice — see test_billing_csv",
        )
        self.assertFalse(summary["mixed_currencies"])

    async def test_by_kind_breakdown_excludes_cache_hits(self):
        gw = _gateway({"deepseek": FakeAdapter("deepseek")})
        ledger = Ledger(investigation_id="inv_test")
        await gw.bind(_investigation(), CallKind.MAIN_LOOP, ledger=ledger).call([], [])
        await gw.bind(_investigation(), CallKind.MAIN_LOOP, ledger=ledger).call([], [])
        self.assertEqual(list(ledger.by_kind()), ["main_loop"])

    async def test_a_currency_with_no_ceiling_is_refused_not_run_unbounded(self):
        """A newly-added provider must not quietly escape the budget. Refusing is
        loud; treating an absent ceiling as infinite is the silent failure."""
        gw = _gateway({"deepseek": FakeAdapter("deepseek")})
        inv = _investigation(max_cost={"USD": 0.40})  # no CNY ceiling
        with self.assertRaises(BudgetExceeded) as ctx:
            await gw.bind(inv).call([], [])
        self.assertIn("no CNY ceiling", str(ctx.exception))

    async def test_missing_transport_is_reported_clearly(self):
        gw = _gateway({})  # nothing wired
        with self.assertRaises(ProviderUnavailable) as ctx:
            await gw.bind(_investigation()).call([], [])
        self.assertIn("no transport wired", str(ctx.exception))


class TestLoopIntegration(unittest.IsolatedAsyncioTestCase):
    """The contract exceptions must become Aborted events, preserving the L2
    invariant that every run emits exactly one Done or Aborted."""

    async def test_budget_exceeded_becomes_an_aborted_event(self):
        adapter = FakeAdapter("deepseek", usage=Usage(1_000_000, 0))
        gw = _gateway({"deepseek": adapter})
        inv = _investigation(max_cost=_ceiling(0.10))
        inv.add_user_text("<alert>{}</alert>")
        llm = gw.bind(inv)

        # First call spends past the ceiling; the loop's second turn is refused.
        outcome = await run_to_completion(inv, llm=llm, tools=default_tool_registry())

        self.assertIsInstance(outcome, Aborted)
        assert isinstance(outcome, Aborted)
        self.assertIn(outcome.reason, {"budget", "no_report"})

    async def test_provider_unavailable_becomes_an_aborted_event(self):
        dead = FakeAdapter("deepseek", [errors.ServerError("500", status=500)] * 2)
        gw = _gateway({"deepseek": dead}, allow_fallback=False)
        inv = _investigation()
        inv.add_user_text("<alert>{}</alert>")

        outcome = await run_to_completion(inv, llm=gw.bind(inv), tools=default_tool_registry())

        self.assertIsInstance(outcome, Aborted)
        assert isinstance(outcome, Aborted)
        self.assertEqual(outcome.reason, "provider_unavailable")

    async def test_a_terminal_tool_call_through_the_gateway_produces_a_report(self):
        script = [
            (
                Response(
                    stop_reason=StopReason.TOOL_USE,
                    content=[
                        ToolUseBlock(
                            id="t1",
                            name="submit_report",
                            input={"root_cause": "redis oom", "confidence": "high"},
                        )
                    ],
                ),
                Usage(100, 20),
            )
        ]
        gw = _gateway({"deepseek": FakeAdapter("deepseek", script)})
        inv = _investigation()
        inv.add_user_text("<alert>{}</alert>")

        outcome = await run_to_completion(inv, llm=gw.bind(inv), tools=default_tool_registry())

        self.assertIsInstance(outcome, Done)
        assert isinstance(outcome, Done)
        assert outcome.report is not None
        self.assertEqual(outcome.report["root_cause"], "redis oom")


if __name__ == "__main__":
    unittest.main()
