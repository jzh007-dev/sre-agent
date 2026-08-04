"""The trigger registry, and the seam rule it exists to keep.

The claim being defended: **adding a trigger type is a new module plus a `register()`
call, and `agent/core/` does not change.** `tests/test_architecture.py` enforces the
import half of that continuously; this file enforces the behavioural half by
registering a trigger that does not exist in `agent/` at all and running it through
the same dispatch path the built-ins use.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any, Mapping

from agent.core.investigation import Investigation, Window, mint_id
from agent.triggers import registry
from agent.triggers.chat import ChatTrigger
from agent.triggers.patrol import PatrolTrigger

T0 = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


class WebhookTrigger:
    """A fourth entry mode, defined entirely in this test file."""

    kind = "webhook"

    def preprocess(self, payload: Mapping[str, Any]) -> registry.TriggerOutcome:
        inv = Investigation(
            id=mint_id(), trigger="alert", window=Window.around(T0)
        )
        inv.add_user_text(str(payload.get("body", "")))
        return registry.TriggerOutcome(investigations=[inv])


class TestRegistry(unittest.TestCase):
    def setUp(self) -> None:
        registry.install_defaults()

    def tearDown(self) -> None:
        registry.unregister("webhook")

    def test_the_three_entry_modes_are_registered(self):
        self.assertEqual(registry.available(), ["alert", "chat", "patrol"])

    def test_an_unknown_kind_names_what_is_registered(self):
        with self.assertRaises(KeyError) as caught:
            registry.get("alerts")
        self.assertIn("alert", str(caught.exception))

    def test_a_fourth_trigger_needs_no_change_anywhere_else(self):
        registry.register(WebhookTrigger())
        outcome = registry.dispatch("webhook", {"body": "something happened"})
        self.assertEqual(outcome.created, 1)
        self.assertIn("webhook", registry.available())

    def test_registering_replaces_rather_than_raising(self):
        """Eval and tests both swap in a trigger with an injected clock, and a
        registry that refused would force every test to undo module state."""
        first = WebhookTrigger()
        second = WebhookTrigger()
        registry.register(first)
        registry.register(second)
        self.assertIs(registry.get("webhook"), second)

    def test_all_three_triggers_satisfy_the_protocol(self):
        for trigger in registry.registered():
            with self.subTest(kind=trigger.kind):
                self.assertIsInstance(trigger, registry.Trigger)

    def test_summarise_aggregates_over_a_sequence(self):
        """The exit-table numbers are properties of a sequence of deliveries, not of
        one webhook."""
        registry.register(WebhookTrigger())
        outcomes = [registry.dispatch("webhook", {"body": str(i)}) for i in range(3)]
        summary = registry.summarise(outcomes)
        self.assertEqual(summary["deliveries"], 3)
        self.assertEqual(summary["created"], 3)


class TestChatTrigger(unittest.TestCase):
    def test_a_question_becomes_an_investigation_that_needs_no_report(self):
        inv = ChatTrigger(now=lambda: T0).preprocess(
            {"session_id": "s1", "text": "why is checkout slow?"}
        ).investigations[0]
        self.assertEqual(inv.trigger, "chat")
        self.assertFalse(inv.requires_report, "answering and stopping is legitimate")

    def test_the_same_session_resumes_rather_than_forking(self):
        chat = ChatTrigger(now=lambda: T0)
        first = chat.preprocess({"session_id": "s1", "text": "why is checkout slow?"})
        second = chat.preprocess({"session_id": "s1", "text": "and payment?"})

        self.assertEqual(second.created, 0)
        self.assertIs(second.joined[0], first.investigations[0])
        self.assertEqual(len(first.investigations[0].messages), 1, "merged, not appended")
        self.assertEqual(len(first.investigations[0].messages[0].content), 2)

    def test_a_different_session_is_a_different_investigation(self):
        chat = ChatTrigger(now=lambda: T0)
        a = chat.preprocess({"session_id": "s1", "text": "q"})
        b = chat.preprocess({"session_id": "s2", "text": "q"})
        self.assertNotEqual(a.investigations[0].id, b.investigations[0].id)

    def test_the_same_question_twice_is_never_deduplicated(self):
        """A human asking twice is asking twice. Semantic reuse may one day *offer* an
        existing report and may never suppress the question — TRADEOFFS §41."""
        chat = ChatTrigger(now=lambda: T0)
        first = chat.preprocess({"session_id": "s1", "text": "why is checkout slow?"})
        second = chat.preprocess({"session_id": "s2", "text": "why is checkout slow?"})
        self.assertEqual(first.created, 1)
        self.assertEqual(second.created, 1)
        self.assertEqual(second.decisions, [])

    def test_the_window_is_anchored_on_now_because_a_question_has_no_t0(self):
        inv = ChatTrigger(now=lambda: T0).preprocess({"text": "q"}).investigations[0]
        self.assertLess(inv.window.start, T0)
        self.assertGreaterEqual(inv.window.end, T0)


class TestPatrolTrigger(unittest.TestCase):
    def test_a_scope_becomes_one_investigation_per_target(self):
        outcome = PatrolTrigger(now=lambda: T0).preprocess(
            {"targets": ["checkout", "payment", "auth"]}
        )
        self.assertEqual(outcome.created, 3)
        self.assertEqual(len({inv.id for inv in outcome.investigations}), 3)

    def test_each_target_has_its_own_budget(self):
        """The reason the stub earned its keep: fifty targets sharing one ceiling
        would have meant the fiftieth got nothing."""
        outcome = PatrolTrigger(now=lambda: T0).preprocess({"targets": ["a", "b"]})
        budgets = [inv.budget for inv in outcome.investigations]
        self.assertEqual(budgets[0].max_tool_calls, budgets[1].max_tool_calls)
        self.assertLess(budgets[0].max_tool_calls, 40, "a sweep is cheaper than an alert")

    def test_all_targets_share_one_pinned_window(self):
        """Two targets examined ninety seconds apart otherwise read different data,
        and "checkout looks worse than payment" becomes a statement about the clock."""
        outcome = PatrolTrigger(now=lambda: T0).preprocess({"targets": ["a", "b"]})
        self.assertEqual(
            outcome.investigations[0].window, outcome.investigations[1].window
        )

    def test_the_target_is_named_in_the_prompt(self):
        inv = PatrolTrigger(now=lambda: T0).preprocess(
            {"targets": ["checkout"]}
        ).investigations[0]
        text = inv.messages[0].content[0].text
        self.assertIn("checkout", text)

    def test_no_targets_produces_nothing_rather_than_raising(self):
        self.assertEqual(PatrolTrigger(now=lambda: T0).preprocess({}).created, 0)

    def test_patrol_must_report(self):
        inv = PatrolTrigger(now=lambda: T0).preprocess(
            {"targets": ["a"]}
        ).investigations[0]
        self.assertTrue(inv.requires_report)


if __name__ == "__main__":
    unittest.main()
