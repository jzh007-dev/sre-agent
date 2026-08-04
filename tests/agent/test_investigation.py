"""Investigation and Window — the trigger-agnostic kernel.

The fixture-metadata guard and everything else that had to know what an alert
payload looks like moved to `tests/triggers/test_alert.py` in W2 L4b, following the
code: `Investigation` is the noun all three entry modes share, and it should not be
able to parse a webhook.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from agent.core.investigation import (
    DEFAULT_LOOKAHEAD,
    DEFAULT_LOOKBACK,
    Investigation,
    ToolBudget,
    Window,
    mint_id,
)
from agent.llm.types import Message, TextBlock, ToolResultBlock

T0 = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


class TestWindow(unittest.TestCase):
    def test_window_is_anchored_on_t0_not_now(self):
        w = Window.around(T0)
        self.assertEqual(w.start, T0 - DEFAULT_LOOKBACK)
        self.assertEqual(w.end, T0 + DEFAULT_LOOKAHEAD)
        self.assertEqual(w.duration, DEFAULT_LOOKBACK + DEFAULT_LOOKAHEAD)

    def test_same_alert_yields_the_same_window_every_run(self):
        """The reproducibility guarantee EVAL.md depends on: rerunning a golden
        case must read the same range, not 'the last 30 minutes of whenever'."""
        self.assertEqual(Window.around(T0), Window.around(T0))

    def test_as_tool_args_is_iso8601(self):
        args = Window.around(T0, lookback=timedelta(minutes=10)).as_tool_args()
        self.assertEqual(args["start"], "2026-07-27T11:50:00+00:00")
        self.assertIn("2026-07-27T12:05", args["end"])


class TestUserText(unittest.TestCase):
    """`add_user_text` merges into a trailing user message.

    Load-bearing for every mid-flight path — dedup R1 absorbing a repeat alert, a
    `resolved` notification, a chat follow-up. Mid-investigation the last message is
    the `user` message carrying that turn's `tool_result` blocks, and appending a
    *second* consecutive user message produces a `messages` array that some providers
    reject outright.
    """

    def _inv(self) -> Investigation:
        return Investigation(id="i", trigger="alert", window=Window.around(T0))

    def test_first_text_creates_a_message(self):
        inv = self._inv()
        inv.add_user_text("hello")
        self.assertEqual(len(inv.messages), 1)
        self.assertEqual(inv.messages[0].role, "user")

    def test_second_text_merges_rather_than_appending_a_user_turn(self):
        inv = self._inv()
        inv.add_user_text("one")
        inv.add_user_text("two")
        self.assertEqual(len(inv.messages), 1, "consecutive user messages are invalid")
        self.assertEqual(len(inv.messages[0].content), 2)

    def test_absorption_after_tool_results_keeps_the_results_first(self):
        """Anthropic requires tool_result blocks at the start of a user message, so
        the merge has to append after them rather than in front."""
        inv = self._inv()
        inv.add_user_text("<alert>…</alert>")
        inv.messages.append(Message(role="assistant", content=[TextBlock(text="ok")]))
        inv.messages.append(
            Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="{}")])
        )
        inv.add_user_text("<alert-update>the condition resolved</alert-update>")

        self.assertEqual(len(inv.messages), 3)
        blocks = inv.messages[-1].content
        self.assertEqual(blocks[0].type, "tool_result")
        self.assertEqual(blocks[-1].type, "text")

    def test_a_reply_after_an_assistant_turn_is_a_new_message(self):
        inv = self._inv()
        inv.add_user_text("first question")
        inv.messages.append(Message(role="assistant", content=[TextBlock(text="answer")]))
        inv.add_user_text("follow-up")
        self.assertEqual([m.role for m in inv.messages], ["user", "assistant", "user"])


class TestInvestigation(unittest.TestCase):
    def test_alert_and_patrol_require_a_report_chat_does_not(self):
        window = Window.around(T0)
        self.assertTrue(Investigation(id="a", trigger="alert", window=window).requires_report)
        self.assertTrue(Investigation(id="p", trigger="patrol", window=window).requires_report)
        self.assertFalse(Investigation(id="c", trigger="chat", window=window).requires_report)

    def test_budget_exhaustion_reports_which_ceiling(self):
        inv = Investigation(
            id="i",
            trigger="alert",
            window=Window.around(T0),
            budget=ToolBudget(max_tool_calls=3, per_tool_calls={"query_logs": 1}),
        )
        self.assertIsNone(inv.budget_exhausted())

        inv.record_tool_call("query_logs")
        reason = inv.budget_exhausted()
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("query_logs", reason, "the reason ends up in the degraded report")

    def test_total_ceiling_independent_of_per_tool(self):
        inv = Investigation(
            id="i", trigger="alert", window=Window.around(T0), budget=ToolBudget(max_tool_calls=2)
        )
        inv.record_tool_call("query_metrics")
        self.assertIsNone(inv.budget_exhausted())
        inv.record_tool_call("search_runbook")
        reason = inv.budget_exhausted()
        assert reason is not None
        self.assertIn("2/2", reason)

    def test_ids_are_unique(self):
        """`srectl replay`, the JSONL store and the trace all key on this id."""
        self.assertNotEqual(mint_id(), mint_id())
        self.assertTrue(mint_id().startswith("inv_"))


if __name__ == "__main__":
    unittest.main()
