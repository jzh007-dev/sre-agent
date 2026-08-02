"""Streaming and the context pre-check — the two pieces deferred out of L3.

Streaming is a *side channel for latency*, not a different result type: the stream
still ends with a complete `Response`, because the loop needs assembled `tool_use`
blocks to dispatch and the cache needs something to store. That is why the `LLM`
protocol signature never had to change.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from agent.llm import errors
from agent.llm.openai_compat import StreamingOpenAICompatAdapter
from agent.llm.provider_catalog import PROVIDERS, model
from agent.llm.request import (
    CONTEXT_HEADROOM,
    PromptFragment,
    SystemPrompt,
    build,
    check_context,
    estimate_tokens,
)
from agent.llm.transport import (
    CircuitBreaker,
    StreamDone,
    TextChunk,
    Transport,
)
from agent.llm.types import Message, StopReason, TextBlock


def _delta(content=None, tool_calls=None, finish_reason=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=tool_calls or []),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
    )


def _usage_event(prompt=100, completion=20, cached=0):
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion, prompt_cache_hit_tokens=cached
        ),
    )


def _tool_delta(index, call_id=None, name=None, arguments=None):
    return [
        SimpleNamespace(
            index=index,
            id=call_id,
            function=SimpleNamespace(name=name, arguments=arguments),
        )
    ]


class FakeStream:
    def __init__(self, events):
        self.events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event


class StreamingClient:
    def __init__(self, events):
        self.events = events
        self.payload = None

    async def create(self, **payload):
        self.payload = payload
        return FakeStream(self.events)


def _adapter(events) -> StreamingOpenAICompatAdapter:
    spec = model("deepseek-chat")
    return StreamingOpenAICompatAdapter(
        provider_spec=PROVIDERS[spec.provider], model_spec=spec, client=StreamingClient(events)
    )


def _request():
    return build("deepseek-chat", [Message(role="user", content=[TextBlock(text="hi")])])


class TestStreaming(unittest.IsolatedAsyncioTestCase):
    async def test_text_arrives_as_chunks_and_ends_assembled(self):
        adapter = _adapter([_delta("Hel"), _delta("lo"), _usage_event()])
        chunks = [c async for c in adapter.stream(_request())]

        texts = [c.text for c in chunks if isinstance(c, TextChunk)]
        self.assertEqual(texts, ["Hel", "lo"])

        done = chunks[-1]
        self.assertIsInstance(done, StreamDone)
        assert isinstance(done, StreamDone)
        self.assertEqual(done.response.content[0].text, "Hello")
        self.assertEqual(done.usage.input_tokens, 100)

    async def test_stream_option_requests_usage(self):
        adapter = _adapter([_usage_event()])
        [_ async for _ in adapter.stream(_request())]
        self.assertTrue(adapter.client.payload["stream"])
        self.assertEqual(adapter.client.payload["stream_options"], {"include_usage": True})

    async def test_tool_calls_are_reassembled_from_fragments(self):
        """Streamed tool calls arrive split — the name in one chunk, the JSON
        arguments across several — so nothing is parseable until the stream ends.
        That is exactly why StreamDone carries the assembled Response."""
        adapter = _adapter(
            [
                _delta(tool_calls=_tool_delta(0, call_id="t1", name="query_metrics")),
                _delta(tool_calls=_tool_delta(0, arguments='{"prom')),
                _delta(tool_calls=_tool_delta(0, arguments='ql": "up"}')),
                _delta(finish_reason="tool_calls"),
                _usage_event(),
            ]
        )
        chunks = [c async for c in adapter.stream(_request())]
        done = chunks[-1]
        assert isinstance(done, StreamDone)

        self.assertEqual(done.response.stop_reason, StopReason.TOOL_USE)
        block = done.response.content[0]
        self.assertEqual(block.name, "query_metrics")
        self.assertEqual(block.input, {"promql": "up"})

    async def test_parallel_tool_calls_keep_their_indices(self):
        adapter = _adapter(
            [
                _delta(tool_calls=_tool_delta(0, call_id="t1", name="query_metrics", arguments="{}")),
                _delta(tool_calls=_tool_delta(1, call_id="t2", name="query_logs", arguments="{}")),
                _usage_event(),
            ]
        )
        chunks = [c async for c in adapter.stream(_request())]
        done = chunks[-1]
        assert isinstance(done, StreamDone)
        self.assertEqual([b.name for b in done.response.content], ["query_metrics", "query_logs"])

    async def test_mid_stream_failure_is_classified(self):
        exc = RuntimeError("connection reset")
        exc.status_code = 503  # type: ignore[attr-defined]
        adapter = _adapter([_delta("partial"), exc])

        seen: list = []
        with self.assertRaises(errors.ServerError):
            async for chunk in adapter.stream(_request()):
                seen.append(chunk)

        self.assertEqual(len(seen), 1, "the partial text was already delivered")

    async def test_transport_stream_does_not_retry(self):
        """Deliberate: once partial text has been shown to a human or appended to a
        digest, silently restarting would replay output the caller already saw."""
        exc = RuntimeError("boom")
        exc.status_code = 503  # type: ignore[attr-defined]
        adapter = _adapter([exc])
        transport = Transport(adapter=adapter, breaker=CircuitBreaker(now=lambda: 0.0))

        with self.assertRaises(errors.ServerError):
            async for _ in transport.stream(_request()):
                pass

        # The failure still informs the breaker — a failed stream is evidence about
        # the provider even though it is not retried.
        self.assertEqual(transport.breaker.consecutive_failures, 1)

    async def test_non_streaming_adapter_is_rejected_clearly(self):
        from agent.llm.openai_compat import OpenAICompatAdapter

        spec = model("deepseek-chat")
        plain = OpenAICompatAdapter(
            provider_spec=PROVIDERS[spec.provider], model_spec=spec, client=StreamingClient([])
        )
        transport = Transport(adapter=plain, breaker=CircuitBreaker(now=lambda: 0.0))
        with self.assertRaises(errors.InvalidRequest):
            async for _ in transport.stream(_request()):
                pass


class TestContextPreCheck(unittest.TestCase):
    def test_a_normal_request_fits(self):
        fits, why = check_context(_request(), context_window=64_000)
        self.assertTrue(fits)
        self.assertEqual(why, "")

    def test_headroom_is_reserved_for_the_response(self):
        """Not 1.0: output tokens come out of the same window, so a request that
        fits with nothing to spare yields a truncated answer rather than a clean
        failure."""
        self.assertLess(CONTEXT_HEADROOM, 1.0)

    def test_an_oversized_request_reports_what_is_needed(self):
        huge = build(
            "deepseek-chat",
            [Message(role="user", content=[TextBlock(text="x" * 400_000)])],
        )
        fits, why = check_context(huge, context_window=64_000)
        self.assertFalse(fits)
        self.assertIn("compaction needed", why)

    def test_estimate_counts_system_messages_tools_and_reserved_output(self):
        bare = build("deepseek-chat", [])
        with_system = build(
            "deepseek-chat",
            [],
            system=SystemPrompt.of(PromptFragment("m", "x" * 3500, stable_across="project")),
        )
        with_tools = build(
            "deepseek-chat",
            [],
            [{"name": "t", "description": "d" * 3500, "input_schema": {}}],
        )
        self.assertGreater(estimate_tokens(with_system), estimate_tokens(bare))
        self.assertGreater(estimate_tokens(with_tools), estimate_tokens(bare))
        # max_tokens is reserved, so a bare request is never estimated at zero.
        self.assertGreaterEqual(estimate_tokens(bare), bare.max_tokens)


class TestGatewayContextGate(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_request_is_refused_before_transport(self):
        """Anticipating ContextLimit beats catching it: a 400 costs a round trip, and
        some providers do not distinguish 'too long' from other bad requests."""
        from datetime import datetime, timezone

        from agent.core.investigation import Investigation, Window
        from agent.llm.cache import MemoryStore, ResponseCache
        from agent.llm.clients import smoke_routing
        from agent.llm.gateway import Gateway

        class NeverCalled:
            provider = "deepseek"

            async def send(self, request):
                raise AssertionError("transport must not be reached")

        gateway = Gateway(
            routing=smoke_routing("deepseek-chat"),
            transports={"deepseek": Transport(adapter=NeverCalled())},
            cache=ResponseCache(store=MemoryStore()),
            allow_fallback=False,
        )
        inv = Investigation(
            id="inv_ctx",
            trigger="alert",
            window=Window.around(datetime(2026, 8, 2, tzinfo=timezone.utc)),
        )
        from agent.llm.protocol import ContextOverflow

        with self.assertRaises(ContextOverflow) as ctx:
            await gateway.bind(inv).call(
                [Message(role="user", content=[TextBlock(text="x" * 400_000)])], []
            )

        # ContextOverflow is a *contract* error, not a provider failure: it never
        # triggers fallback (a bigger window would postpone compaction until nothing
        # fits) and it carries the numbers compaction needs.
        self.assertEqual(ctx.exception.reason, "context_overflow")
        self.assertGreater(ctx.exception.excess_tokens, 0)
        self.assertEqual(ctx.exception.model_id, "deepseek-chat")


if __name__ == "__main__":
    unittest.main()
