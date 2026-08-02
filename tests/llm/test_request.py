"""Request assembly: prompt layering, cache breakpoints, and the cache key.

The cache key is the part with teeth. Anything that can change a response must be
in it, or a stale entry gets served and the result is silently wrong — the worst
possible failure for an evaluation system, because it looks like success.
"""
from __future__ import annotations

import unittest

from agent.llm.provider_catalog import PROVIDERS, model
from agent.llm.request import MAX_CACHE_BREAKPOINTS, PromptFragment, SystemPrompt, build
from agent.llm.types import Message, TextBlock, ToolResultBlock, ToolUseBlock

TOOL = {
    "name": "query_metrics",
    "description": "Query metrics",
    "input_schema": {"type": "object", "properties": {"promql": {"type": "string"}}},
}


def _messages(text: str = "hi"):
    return [Message(role="user", content=[TextBlock(text=text)])]


def _layered() -> SystemPrompt:
    return SystemPrompt.of(
        PromptFragment("methodology", "[A]", stable_across="project"),
        PromptFragment("output_contract", "[B]", stable_across="project"),
        PromptFragment("integration", "[C]", stable_across="integration"),
        PromptFragment("budget", "[D]", stable_across="investigation"),
    )


class TestPromptLayering(unittest.TestCase):
    def test_ordering_is_most_static_first(self):
        """A cache prefix must be a literal prefix, so stability determines order."""
        prompt = SystemPrompt.of(
            PromptFragment("budget", "[D]", stable_across="investigation"),
            PromptFragment("integration", "[C]", stable_across="integration"),
            PromptFragment("methodology", "[A]", stable_across="project"),
        )
        self.assertEqual([f.name for f in prompt.ordered()], ["methodology", "integration", "budget"])

    def test_authored_order_is_preserved_within_a_stability_rank(self):
        prompt = _layered()
        names = [f.name for f in prompt.ordered()]
        self.assertLess(names.index("methodology"), names.index("output_contract"))

    def test_breakpoints_mark_the_end_of_each_stable_run(self):
        prompt = _layered()
        # ordered: [A](project) [B](project) [C](integration) [D](investigation)
        # → one breakpoint after [B], one after [C]
        self.assertEqual(prompt.breakpoint_indices(), (1, 2))

    def test_per_investigation_fragment_is_never_a_breakpoint(self):
        """Marking a fragment that changes every run writes a cache entry nobody
        ever reads, which pays the write premium for nothing."""
        prompt = _layered()
        ordered = prompt.ordered()
        for index in prompt.breakpoint_indices():
            self.assertNotEqual(ordered[index].stable_across, "investigation")

    def test_breakpoints_are_capped_at_the_provider_limit(self):
        prompt = SystemPrompt.of(
            *(
                PromptFragment(f"f{i}", f"t{i}", stable_across=("project" if i % 2 else "integration"))
                for i in range(12)
            )
        )
        self.assertLessEqual(len(prompt.breakpoint_indices()), MAX_CACHE_BREAKPOINTS)

    def test_empty_prompt_is_legal(self):
        """W3 L1 authors the content; L3 must work before it exists."""
        self.assertEqual(SystemPrompt().breakpoint_indices(), ())
        self.assertEqual(SystemPrompt().text(), "")


class TestCacheKey(unittest.TestCase):
    def test_identical_requests_share_a_key(self):
        a = build("deepseek-chat", _messages(), [TOOL], system=_layered())
        b = build("deepseek-chat", _messages(), [TOOL], system=_layered())
        self.assertEqual(a.cache_key(), b.cache_key())

    def test_tool_schema_change_invalidates_the_key(self):
        """Deliberate. Omitting tools would let a changed tool set hit a stale entry
        and produce a wrong answer cheaply. 'Adding a tool invalidates the cache' is
        correct behaviour — adding a tool *should* require re-running eval."""
        changed = dict(TOOL)
        changed["input_schema"] = {"type": "object", "properties": {"query": {"type": "string"}}}
        a = build("deepseek-chat", _messages(), [TOOL])
        b = build("deepseek-chat", _messages(), [changed])
        self.assertNotEqual(a.cache_key(), b.cache_key())

    def test_adding_a_tool_invalidates_the_key(self):
        a = build("deepseek-chat", _messages(), [TOOL])
        b = build("deepseek-chat", _messages(), [TOOL, {**TOOL, "name": "query_logs"}])
        self.assertNotEqual(a.cache_key(), b.cache_key())

    def test_model_system_messages_and_params_all_participate(self):
        base = build("deepseek-chat", _messages(), [TOOL], system=_layered())
        variants = {
            "model": build("qwen-plus", _messages(), [TOOL], system=_layered()),
            "messages": build("deepseek-chat", _messages("different"), [TOOL], system=_layered()),
            "system": build(
                "deepseek-chat",
                _messages(),
                [TOOL],
                system=SystemPrompt.of(PromptFragment("methodology", "changed", stable_across="project")),
            ),
            "temperature": build(
                "deepseek-chat", _messages(), [TOOL], system=_layered(), temperature=0.7
            ),
            "max_tokens": build(
                "deepseek-chat", _messages(), [TOOL], system=_layered(), max_tokens=99
            ),
            "extra param": build(
                "deepseek-chat", _messages(), [TOOL], system=_layered(), thinking_budget=1000
            ),
        }
        for label, variant in variants.items():
            with self.subTest(changed=label):
                self.assertNotEqual(base.cache_key(), variant.cache_key())

    def test_tool_results_and_tool_uses_are_hashable(self):
        messages = [
            Message(role="assistant", content=[ToolUseBlock(id="t1", name="query_metrics", input={"promql": "up"})]),
            Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="{}")]),
        ]
        key = build("deepseek-chat", messages).cache_key()
        self.assertEqual(len(key), 64)

    def test_error_flag_on_a_tool_result_changes_the_key(self):
        """An error result and a successful one are different context, so they must
        not collide — otherwise a degraded run could serve a healthy run's answer."""
        ok = [Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="{}")])]
        bad = [
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id="t1", content="{}", is_error=True)],
            )
        ]
        self.assertNotEqual(build("deepseek-chat", ok).cache_key(), build("deepseek-chat", bad).cache_key())

    def test_unknown_block_type_fails_loudly(self):
        class Weird:
            type = "future_block"

        with self.assertRaises(TypeError):
            build("deepseek-chat", [Message(role="user", content=[Weird()])]).cache_key()


class TestCatalogInvariants(unittest.TestCase):
    def test_every_model_points_at_a_known_provider(self):
        from agent.llm.provider_catalog import MODELS

        for model_id, spec in MODELS.items():
            with self.subTest(model=model_id):
                self.assertIn(spec.provider, PROVIDERS)

    def test_prices_are_marked_unverified(self):
        """Until L3-smoke checks them against a real call, any cost derived from
        them is reported as unverified rather than as measured fact."""
        self.assertFalse(model("deepseek-chat").price.verified)

    def test_only_anthropic_claims_explicit_cache_support(self):
        """The asymmetry that shapes request rendering: DeepSeek caches prefixes
        automatically, so it gets no markers — but still benefits from ordering."""
        self.assertTrue(model("claude-sonnet-5").supports_explicit_cache)
        self.assertFalse(model("deepseek-chat").supports_explicit_cache)


if __name__ == "__main__":
    unittest.main()
