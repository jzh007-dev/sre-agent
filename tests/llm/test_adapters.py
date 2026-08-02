"""Adapter codecs and error classification.

Both adapters take an injected client, so these run offline. What is being tested
is the part that has no network in it anyway: translating domain types to and from
each provider's shape, placing cache breakpoints, and mapping SDK exceptions onto
the taxonomy that drives transport policy.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from agent.llm import errors
from agent.llm.anthropic import AnthropicAdapter
from agent.llm.openai_compat import OpenAICompatAdapter
from agent.llm.provider_catalog import PROVIDERS, model
from agent.llm.request import PromptFragment, SystemPrompt, build
from agent.llm.types import Message, StopReason, TextBlock, ToolResultBlock, ToolUseBlock

TOOL = {
    "name": "query_metrics",
    "description": "Query metrics",
    "input_schema": {"type": "object", "properties": {"promql": {"type": "string"}}},
}


def _layered() -> SystemPrompt:
    return SystemPrompt.of(
        PromptFragment("methodology", "[A] methodology", stable_across="project"),
        PromptFragment("contract", "[B] contract", stable_across="project"),
        PromptFragment("integration", "[C] k8s facet", stable_across="integration"),
        PromptFragment("budget", "[D] 12 turns left", stable_across="investigation"),
    )


class NullClient:
    async def create(self, **payload):
        raise AssertionError("not used in this test")


class RaisingClient:
    def __init__(self, exc):
        self.exc = exc

    async def create(self, **payload):
        raise self.exc


def _openai(model_id: str = "deepseek-chat", client=None) -> OpenAICompatAdapter:
    spec = model(model_id)
    return OpenAICompatAdapter(
        provider_spec=PROVIDERS[spec.provider], model_spec=spec, client=client or NullClient()
    )


def _anthropic(model_id: str = "claude-sonnet-5", client=None) -> AnthropicAdapter:
    spec = model(model_id)
    return AnthropicAdapter(
        provider_spec=PROVIDERS[spec.provider], model_spec=spec, client=client or NullClient()
    )


class TestAnthropicCacheBreakpoints(unittest.TestCase):
    def test_system_goes_out_as_blocks_not_one_string(self):
        """One string would make the whole prompt a single cache unit, so any
        per-investigation content would invalidate the project-stable methodology
        on every call — exactly what the layering exists to prevent."""
        payload = _anthropic().render(build("claude-sonnet-5", [], system=_layered()))
        self.assertIsInstance(payload["system"], list)
        self.assertEqual(len(payload["system"]), 4)

    def test_breakpoints_land_after_each_stable_run(self):
        payload = _anthropic().render(build("claude-sonnet-5", [], system=_layered()))
        marked = [i for i, block in enumerate(payload["system"]) if "cache_control" in block]
        self.assertEqual(marked, [1, 2], "after [B] and after [C]")

    def test_per_investigation_block_is_never_marked(self):
        payload = _anthropic().render(build("claude-sonnet-5", [], system=_layered()))
        self.assertNotIn("cache_control", payload["system"][-1])

    def test_no_markers_when_the_model_does_not_support_them(self):
        """DeepSeek caches its prefix automatically. Sending markers it does not
        understand risks a 400, and the ordering already does the work."""
        adapter = OpenAICompatAdapter(
            provider_spec=PROVIDERS["deepseek"], model_spec=model("deepseek-chat"), client=NullClient()
        )
        payload = adapter.render(build("deepseek-chat", [], system=_layered()))
        self.assertNotIn("cache_control", json.dumps(payload))
        # ...but the layered ordering still reaches the provider.
        self.assertLess(
            payload["messages"][0]["content"].index("[A]"),
            payload["messages"][0]["content"].index("[D]"),
        )


class TestOpenAICodec(unittest.TestCase):
    def test_tool_results_become_one_tool_message_each(self):
        """Anthropic nests tool_result blocks in a user message; OpenAI wants one
        role=tool message per result. This structural difference is the reason the
        codec is not a field rename."""
        message = Message(
            role="user",
            content=[
                ToolResultBlock(tool_use_id="t1", content="{}"),
                ToolResultBlock(tool_use_id="t2", content="{}"),
            ],
        )
        payload = _openai().render(build("deepseek-chat", [message]))
        tool_messages = [m for m in payload["messages"] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_messages], ["t1", "t2"])

    def test_assistant_tool_use_becomes_tool_calls_with_json_arguments(self):
        message = Message(
            role="assistant",
            content=[
                TextBlock(text="checking"),
                ToolUseBlock(id="t1", name="query_metrics", input={"promql": "up"}),
            ],
        )
        payload = _openai().render(build("deepseek-chat", [message]))
        call = payload["messages"][0]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "query_metrics")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"promql": "up"})

    def test_tools_are_rendered_in_function_shape(self):
        payload = _openai().render(build("deepseek-chat", [], [TOOL]))
        self.assertEqual(payload["tools"][0]["type"], "function")
        self.assertEqual(payload["tools"][0]["function"]["parameters"], TOOL["input_schema"])

    def test_parse_promotes_a_response_with_tool_calls_to_tool_use(self):
        """Providers are inconsistent about `finish_reason` when tool calls are
        present, and the loop keys on the blocks — so the blocks decide."""
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content="checking",
                        tool_calls=[
                            SimpleNamespace(
                                id="t1",
                                function=SimpleNamespace(
                                    name="query_metrics", arguments='{"promql": "up"}'
                                ),
                            )
                        ],
                    ),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )
        response, usage = _openai().parse(raw)
        self.assertEqual(response.stop_reason, StopReason.TOOL_USE)
        self.assertEqual(response.content[1].input, {"promql": "up"})
        self.assertEqual(usage.input_tokens, 100)

    def test_deepseek_cached_tokens_are_subtracted_from_input(self):
        """DeepSeek reports cache hits *inside* prompt_tokens. Not subtracting them
        would bill cached tokens at the full input rate and make the cache look
        worthless."""
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content="hi", tool_calls=None)
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=1000, completion_tokens=10, prompt_cache_hit_tokens=800
            ),
        )
        _, usage = _openai().parse(raw)
        self.assertEqual(usage.input_tokens, 200)
        self.assertEqual(usage.cache_read_tokens, 800)

    def test_malformed_tool_arguments_are_a_provider_error(self):
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="t1",
                                function=SimpleNamespace(name="query_metrics", arguments="{not json"),
                            )
                        ],
                    ),
                )
            ],
            usage=None,
        )
        with self.assertRaises(errors.MalformedResponse):
            _openai().parse(raw)

    def test_length_finish_reason_maps_to_max_tokens(self):
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="length", message=SimpleNamespace(content="trunc", tool_calls=None)
                )
            ],
            usage=None,
        )
        response, _ = _openai().parse(raw)
        self.assertEqual(response.stop_reason, StopReason.MAX_TOKENS)


class TestAnthropicCodec(unittest.TestCase):
    def test_tool_result_error_flag_is_preserved(self):
        message = Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="t1", content="boom", is_error=True)],
        )
        payload = _anthropic().render(build("claude-sonnet-5", [message]))
        self.assertTrue(payload["messages"][0]["content"][0]["is_error"])

    def test_cache_tokens_are_reported_separately(self):
        raw = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hi")],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=200,
                output_tokens=10,
                cache_creation_input_tokens=50,
                cache_read_input_tokens=800,
            ),
        )
        _, usage = _anthropic().parse(raw)
        self.assertEqual(usage.input_tokens, 200, "not nested, so nothing to subtract")
        self.assertEqual(usage.cache_read_tokens, 800)
        self.assertEqual(usage.cache_write_tokens, 50)

    def test_unknown_block_types_are_dropped_not_fatal(self):
        raw = SimpleNamespace(
            content=[
                SimpleNamespace(type="thinking", thinking="..."),
                SimpleNamespace(type="text", text="answer"),
            ],
            stop_reason="end_turn",
            usage=None,
        )
        response, _ = _anthropic().parse(raw)
        self.assertEqual(len(response.content), 1)
        self.assertEqual(response.content[0].text, "answer")


class TestClassification(unittest.IsolatedAsyncioTestCase):
    async def test_status_codes_map_to_the_taxonomy(self):
        cases = [
            (401, errors.AuthError),
            (403, errors.AuthError),
            (429, errors.RateLimit),
            (400, errors.InvalidRequest),
            (422, errors.InvalidRequest),
            (500, errors.ServerError),
            (503, errors.ServerError),
        ]
        adapter = _openai()
        for status, expected in cases:
            with self.subTest(status=status):
                self.assertIsInstance(adapter.classify(_make(status, "boom")), expected)

    async def test_context_limit_is_separated_from_other_400s(self):
        """So the caller can compact and retry instead of giving up, while ordinary
        400s stay non-retryable."""
        adapter = _openai()
        exc = _make(400, "This model's maximum context length is 64000 tokens")
        self.assertIsInstance(adapter.classify(exc), errors.ContextLimit)
        self.assertIsInstance(adapter.classify(_make(400, "bad parameter")), errors.InvalidRequest)

    async def test_retry_after_header_is_carried_through(self):
        classified = _openai().classify(_make(429, "slow down", {"retry-after": "12"}))
        self.assertEqual(classified.retry_after, 12.0)

    async def test_anthropic_529_is_overloaded_and_retryable(self):
        classified = _anthropic().classify(_make(529, "overloaded_error"))
        self.assertIsInstance(classified, errors.Overloaded)
        self.assertTrue(classified.retryable)
        self.assertTrue(classified.counts_toward_breaker)

    async def test_timeout_is_recognised_without_a_status(self):
        classified = _openai().classify(TimeoutError("read timed out"))
        self.assertIsInstance(classified, errors.Timeout)

    async def test_unknown_failure_is_treated_as_retryable(self):
        """A transient network fault is far more likely than a novel permanent one,
        and the breaker bounds the damage if that guess is wrong."""
        classified = _openai().classify(RuntimeError("something odd"))
        self.assertIsInstance(classified, errors.ServerError)
        self.assertTrue(classified.retryable)

    async def test_send_classifies_whatever_the_client_raises(self):
        adapter = _openai(client=RaisingClient(_make(503, "unavailable")))
        with self.assertRaises(errors.ServerError):
            await adapter.send(build("deepseek-chat", []))

    async def test_already_classified_errors_pass_through_unwrapped(self):
        adapter = _openai(client=RaisingClient(errors.RateLimit("429", status=429)))
        with self.assertRaises(errors.RateLimit):
            await adapter.send(build("deepseek-chat", []))


def _make(status: int, message: str, headers: dict | None = None) -> Exception:
    """An exception shaped like an SDK error: status code plus response headers."""
    exc = RuntimeError(message)
    exc.status_code = status  # type: ignore[attr-defined]
    exc.response = SimpleNamespace(status_code=status, headers=headers or {})  # type: ignore[attr-defined]
    return exc


if __name__ == "__main__":
    unittest.main()
