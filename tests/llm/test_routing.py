"""Routing: capability requirements, and the family rule enforced at wiring time.

The family-difference rule is a correctness requirement from EVAL.md (the judge
must differ from the agent) and SECURITY.md L3 (the reviewer must be a different
family). A violation must therefore fail at construction — discovering it at the
first judged run means a batch of invalid scores already exists.
"""
from __future__ import annotations

import unittest

from agent.llm.provider_catalog import family_of, model
from agent.llm.routing import (
    MUST_DIFFER_FROM_AGENT,
    CallKind,
    RoutingConfig,
    RoutingError,
    default_config,
    requirements_for,
    route,
    satisfies,
    validate,
)


class TestFamilyRule(unittest.TestCase):
    def test_default_config_is_valid(self):
        validate(default_config())  # must not raise

    def test_judge_sharing_the_agent_family_fails_at_wiring_time(self):
        bad = RoutingConfig(
            agent_model="deepseek-chat",
            assignments={CallKind.JUDGE: "deepseek-reasoner"},  # same family
        )
        with self.assertRaises(RoutingError) as ctx:
            validate(bad)
        self.assertIn("judge", str(ctx.exception))
        self.assertIn("must differ", str(ctx.exception))

    def test_reviewer_sharing_the_agent_family_also_fails(self):
        bad = RoutingConfig(
            agent_model="claude-sonnet-5",
            assignments={
                CallKind.JUDGE: "deepseek-chat",
                CallKind.REVIEWER: "claude-opus-5",  # same family as the agent
            },
        )
        with self.assertRaises(RoutingError) as ctx:
            validate(bad)
        self.assertIn("reviewer", str(ctx.exception))

    def test_family_is_compared_not_model_name(self):
        """Two models from one lab share a family even with unrelated names, which
        is why the rule keys on family rather than on model id."""
        self.assertEqual(family_of("deepseek-chat"), family_of("deepseek-reasoner"))
        self.assertNotEqual(family_of("deepseek-chat"), family_of("claude-sonnet-5"))

    def test_only_judge_and_reviewer_carry_the_constraint(self):
        agent_family = family_of("deepseek-chat")
        for kind in CallKind:
            req = requirements_for(kind, "deepseek-chat")
            with self.subTest(kind=kind.value):
                if kind in MUST_DIFFER_FROM_AGENT:
                    self.assertEqual(req.excluded_family, agent_family)
                else:
                    self.assertIsNone(req.excluded_family)

    def test_all_problems_are_reported_at_once(self):
        """One pass to fix a misconfiguration, not one error at a time."""
        bad = RoutingConfig(
            agent_model="deepseek-chat",
            assignments={CallKind.JUDGE: "deepseek-reasoner", CallKind.REVIEWER: "deepseek-chat"},
        )
        with self.assertRaises(RoutingError) as ctx:
            validate(bad)
        message = str(ctx.exception)
        self.assertIn("judge", message)
        self.assertIn("reviewer", message)


class TestCapabilityRequirements(unittest.TestCase):
    def test_main_loop_requires_reliable_tool_use(self):
        """The loop dies without valid tool_use, so this is hard here and nowhere
        else — a model that writes well but emits malformed calls is unusable."""
        req = requirements_for(CallKind.MAIN_LOOP, "deepseek-chat")
        self.assertTrue(req.needs_tools)
        self.assertTrue(req.needs_reliable_tool_use)

    def test_unreliable_tool_use_disqualifies_a_candidate(self):
        from dataclasses import replace

        spec = replace(model("deepseek-chat"), reliable_tool_use=False)
        ok, why = satisfies(spec, requirements_for(CallKind.MAIN_LOOP, "deepseek-chat"))
        self.assertFalse(ok)
        self.assertIn("unreliable", why)

    def test_too_small_a_context_disqualifies_a_candidate(self):
        from dataclasses import replace

        spec = replace(model("deepseek-chat"), context_window=1_000)
        ok, why = satisfies(spec, requirements_for(CallKind.MAIN_LOOP, "deepseek-chat"))
        self.assertFalse(ok)
        self.assertIn("context", why)

    def test_unknown_model_in_a_fallback_chain_is_caught(self):
        bad = RoutingConfig(
            agent_model="deepseek-chat",
            assignments={
                CallKind.JUDGE: "claude-sonnet-5",
                CallKind.REVIEWER: "claude-sonnet-5",
            },
            fallbacks={CallKind.MAIN_LOOP: ("does-not-exist",)},
        )
        with self.assertRaises(RoutingError) as ctx:
            validate(bad)
        self.assertIn("does-not-exist", str(ctx.exception))

    def test_unassigned_judge_defaults_to_the_agent_and_is_therefore_rejected(self):
        """An unassigned kind falls back to the agent model, which for judge and
        reviewer is always a family violation. Failing loudly is correct: a judge
        that silently shares the agent's family produces same-family-biased scores
        that look perfectly normal."""
        with self.assertRaises(RoutingError) as ctx:
            validate(RoutingConfig(agent_model="deepseek-chat"))
        self.assertIn("judge", str(ctx.exception))
        self.assertIn("reviewer", str(ctx.exception))

    def test_unknown_agent_model_fails_first(self):
        with self.assertRaises(RoutingError):
            validate(RoutingConfig(agent_model="nope"))


class TestCandidateOrdering(unittest.TestCase):
    def test_primary_comes_first_then_fallbacks(self):
        config = RoutingConfig(
            agent_model="deepseek-chat",
            assignments={
                CallKind.JUDGE: "claude-sonnet-5",
                CallKind.REVIEWER: "claude-sonnet-5",
            },
            fallbacks={CallKind.MAIN_LOOP: ("qwen-plus", "moonshot-v1-128k")},
        )
        validate(config)
        candidates = [spec.id for spec in route(config, CallKind.MAIN_LOOP)]
        self.assertEqual(candidates, ["deepseek-chat", "qwen-plus", "moonshot-v1-128k"])

    def test_duplicate_candidates_are_collapsed(self):
        config = RoutingConfig(
            agent_model="deepseek-chat",
            assignments={
                CallKind.JUDGE: "claude-sonnet-5",
                CallKind.REVIEWER: "claude-sonnet-5",
            },
            fallbacks={CallKind.MAIN_LOOP: ("deepseek-chat", "qwen-plus")},
        )
        candidates = [spec.id for spec in route(config, CallKind.MAIN_LOOP)]
        self.assertEqual(candidates, ["deepseek-chat", "qwen-plus"])

    def test_unassigned_kind_falls_back_to_the_agent_model(self):
        config = default_config()
        self.assertEqual(config.model_for(CallKind.MAIN_LOOP), config.agent_model)


if __name__ == "__main__":
    unittest.main()
