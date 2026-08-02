"""Transport policy: retry, circuit breaker, concurrency, classification.

Every path here runs offline — the adapter's `send` is a scripted fake and the
sleeper is a recorder, so retry and breaker behaviour is tested without network
access, without API keys, and without real delays.
"""
from __future__ import annotations

import asyncio
import random
import unittest

from agent.llm import errors
from agent.llm.protocol import ProviderUnavailable
from agent.llm.request import build
from agent.llm.transport import BreakerState, CircuitBreaker, RetryPolicy, Transport
from agent.llm.types import Message, Response, StopReason, TextBlock
from agent.llm.usage import Usage

OK = (Response(stop_reason=StopReason.END_TURN, content=[TextBlock(text="ok")]), Usage(10, 5))


def _request():
    return build("deepseek-chat", [Message(role="user", content=[TextBlock(text="hi")])])


class ScriptedAdapter:
    """Yields one scripted outcome per call: an exception to raise, or a result."""

    provider = "deepseek"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def send(self, request):
        self.calls += 1
        if not self.script:
            raise AssertionError("ScriptedAdapter ran out of script")
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Recorder:
    """Sleeper substitute — records requested delays instead of waiting."""

    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _transport(script, **kwargs):
    sleeper = Recorder()
    clock = kwargs.pop("clock", lambda: 0.0)
    transport = Transport(
        adapter=ScriptedAdapter(script),
        policy=kwargs.pop("policy", RetryPolicy(max_attempts=3, jitter=False)),
        breaker=kwargs.pop("breaker", CircuitBreaker(now=clock)),
        sleeper=sleeper,
        rng=random.Random(1),
        **kwargs,
    )
    return transport, sleeper


class TestRetry(unittest.IsolatedAsyncioTestCase):
    async def test_success_on_first_attempt_does_not_sleep(self):
        transport, sleeper = _transport([OK])
        (result, _), attempts = await transport.call(_request())
        self.assertEqual(len(attempts), 1)
        self.assertEqual(sleeper.delays, [])

    async def test_retryable_error_then_success(self):
        transport, sleeper = _transport([errors.ServerError("500", status=500), OK])
        _, attempts = await transport.call(_request())
        self.assertEqual([a.error_class for a in attempts], ["ServerError", None])
        self.assertEqual(sleeper.delays, [0.5], "one backoff before the second attempt")

    async def test_backoff_is_exponential_and_capped(self):
        policy = RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=4.0, jitter=False)
        transport, sleeper = _transport(
            [errors.ServerError("500", status=500)] * 4 + [OK], policy=policy
        )
        await transport.call(_request())
        self.assertEqual(sleeper.delays, [1.0, 2.0, 4.0, 4.0])

    async def test_provider_retry_after_wins_over_our_curve(self):
        transport, sleeper = _transport(
            [errors.RateLimit("429", status=429, retry_after=7.0), OK]
        )
        await transport.call(_request())
        self.assertEqual(sleeper.delays, [7.0], "the provider knows its own window")

    async def test_non_retryable_error_fails_immediately(self):
        transport, sleeper = _transport([errors.InvalidRequest("400", status=400), OK])
        with self.assertRaises(errors.InvalidRequest):
            await transport.call(_request())
        self.assertEqual(transport.adapter.calls, 1, "must not resend a broken request")
        self.assertEqual(sleeper.delays, [])

    async def test_retries_exhaust_and_raise_the_last_error(self):
        transport, _ = _transport([errors.Timeout("t")] * 3)
        with self.assertRaises(errors.Timeout):
            await transport.call(_request())
        self.assertEqual(transport.adapter.calls, 3)


class TestCircuitBreaker(unittest.IsolatedAsyncioTestCase):
    async def test_three_retries_of_one_bad_request_do_not_open_the_breaker(self):
        """The delta that matters: a malformed request fails repeatedly while the
        provider is healthy. Opening the breaker there would force a pointless
        fallback. See TRADEOFFS §33 delta 6."""
        breaker = CircuitBreaker(now=lambda: 0.0)
        transport, _ = _transport([errors.InvalidRequest("400", status=400)], breaker=breaker)
        with self.assertRaises(errors.InvalidRequest):
            await transport.call(_request())
        self.assertEqual(breaker.consecutive_failures, 0)
        self.assertIs(breaker.state(), BreakerState.CLOSED)

    async def test_rate_limit_never_opens_the_breaker(self):
        """429 is a quota condition, not an outage — breaking on it would fall back
        and hide a quota misconfiguration. Delta 7."""
        breaker = CircuitBreaker(now=lambda: 0.0)
        transport, _ = _transport([errors.RateLimit("429", status=429)] * 3, breaker=breaker)
        with self.assertRaises(errors.RateLimit):
            await transport.call(_request())
        self.assertEqual(breaker.consecutive_failures, 0)
        self.assertIs(breaker.state(), BreakerState.CLOSED)

    async def test_three_consecutive_retryable_failures_open_it(self):
        breaker = CircuitBreaker(threshold=3, now=lambda: 0.0)
        for _ in range(3):
            transport, _ = _transport([errors.ServerError("500", status=500)] * 3, breaker=breaker)
            with self.assertRaises(errors.ServerError):
                await transport.call(_request())
            if breaker.state() is BreakerState.OPEN:
                break
        self.assertIs(breaker.state(), BreakerState.OPEN)

    async def test_open_breaker_refuses_without_calling_the_provider(self):
        breaker = CircuitBreaker(now=lambda: 0.0)
        breaker.force_open()
        transport, _ = _transport([OK], breaker=breaker)
        with self.assertRaises(ProviderUnavailable):
            await transport.call(_request())
        self.assertEqual(transport.adapter.calls, 0)

    async def test_half_open_probe_closes_on_success(self):
        clock = {"t": 0.0}
        breaker = CircuitBreaker(open_seconds=30.0, now=lambda: clock["t"])
        breaker.force_open()
        clock["t"] = 31.0
        self.assertIs(breaker.state(), BreakerState.HALF_OPEN)

        transport, _ = _transport([OK], breaker=breaker, clock=lambda: clock["t"])
        await transport.call(_request())
        self.assertIs(breaker.state(), BreakerState.CLOSED)

    async def test_half_open_probe_reopens_on_failure(self):
        clock = {"t": 0.0}
        breaker = CircuitBreaker(threshold=3, open_seconds=30.0, now=lambda: clock["t"])
        breaker.force_open()
        clock["t"] = 31.0
        transport, _ = _transport(
            [errors.ServerError("500", status=500)] * 3, breaker=breaker, clock=lambda: clock["t"]
        )
        with self.assertRaises(errors.ServerError):
            await transport.call(_request())
        self.assertIs(breaker.state(), BreakerState.OPEN)

    async def test_success_resets_the_failure_count(self):
        breaker = CircuitBreaker(threshold=3, now=lambda: 0.0)
        transport, _ = _transport([errors.ServerError("500", status=500), OK], breaker=breaker)
        await transport.call(_request())
        self.assertEqual(breaker.consecutive_failures, 0)


class TestConcurrencyLimit(unittest.IsolatedAsyncioTestCase):
    async def test_semaphore_bounds_in_flight_calls(self):
        """Justified by eval throughput, not production load: 30 concurrent golden
        cases will hit 429 without it."""
        peak = {"now": 0, "max": 0}

        class Counting:
            provider = "deepseek"

            async def send(self, request):
                peak["now"] += 1
                peak["max"] = max(peak["max"], peak["now"])
                await asyncio.sleep(0.01)
                peak["now"] -= 1
                return OK

        transport = Transport(adapter=Counting(), max_concurrency=2, sleeper=Recorder())
        await asyncio.gather(*(transport.call(_request()) for _ in range(8)))
        self.assertLessEqual(peak["max"], 2)


class TestTaxonomyPolicy(unittest.TestCase):
    def test_policy_matrix_is_pinned(self):
        """Guards the matrix against a silent edit — these flags drive retry and
        breaker behaviour, so a wrong one is a subtle production bug."""
        expected = {
            "AuthError": (False, False),
            "InvalidRequest": (False, False),
            "ContextLimit": (False, False),
            "RateLimit": (True, False),
            "ServerError": (True, True),
            "Overloaded": (True, True),
            "Timeout": (True, True),
            "ContentFilter": (False, False),
            "MalformedResponse": (True, True),
        }
        actual = {
            cls.__name__: (cls.retryable, cls.counts_toward_breaker) for cls in errors.TAXONOMY
        }
        self.assertEqual(actual, expected)

    def test_context_limit_is_an_invalid_request_subtype(self):
        """So a caller that handles InvalidRequest generically still catches it,
        while code that wants to compact and retry can match it specifically."""
        self.assertTrue(issubclass(errors.ContextLimit, errors.InvalidRequest))


if __name__ == "__main__":
    unittest.main()
