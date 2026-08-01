"""Investigation, Window, and the fixture-metadata guard."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from agent.core.investigation import (
    DEFAULT_LOOKAHEAD,
    DEFAULT_LOOKBACK,
    Investigation,
    ToolBudget,
    Window,
    strip_fixture_metadata,
)

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


class TestFixtureMetadataGuard(unittest.TestCase):
    """Every golden case's alert.json carries a `_meta.purpose` that states the
    root cause in prose. If that reached the prompt the agent would be graded on
    reading the answer key, and every accuracy number in the project would be
    meaningless while still looking healthy.
    """

    def test_underscore_keys_are_dropped(self):
        payload = {
            "_meta": {"purpose": "root cause is Redis OOM"},
            "_internal": "bookkeeping",
            "alert_name": "HighErrorRate",
        }
        cleaned = strip_fixture_metadata(payload)
        self.assertEqual(cleaned, {"alert_name": "HighErrorRate"})

    def test_the_answer_never_reaches_the_first_message(self):
        inv = Investigation.from_alert(
            {
                "_meta": {"purpose": "root cause is Redis OOM under memory pressure"},
                "alert_name": "HighErrorRate",
                "commonLabels": {"service": "auth"},
            },
            t0=T0,
        )
        prompt = "".join(
            getattr(b, "text", "") for msg in inv.messages for b in msg.content
        )
        self.assertNotIn("root cause", prompt.lower())
        self.assertIn("HighErrorRate", prompt)
        self.assertIn("<alert>", prompt)

    def test_real_golden_fixtures_are_all_scrubbed(self):
        """Runs against the actual eval fixtures, so a future case that
        reintroduces the leak fails here rather than in a silent eval."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "eval" / "golden"
        cases = sorted(root.glob("*/alert.json"))
        self.assertGreaterEqual(len(cases), 8, "expected the Week 1 golden cases")
        for path in cases:
            with self.subTest(case=path.parent.name):
                raw = json.loads(path.read_text())
                cleaned = strip_fixture_metadata(raw)
                self.assertNotIn("_meta", cleaned)
                self.assertTrue(cleaned, "scrubbing must not empty the payload")


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
        a = Investigation.from_alert({"alert_name": "x"}, t0=T0)
        b = Investigation.from_alert({"alert_name": "x"}, t0=T0)
        self.assertNotEqual(a.id, b.id)

    def test_startsAt_is_used_as_t0_when_present(self):
        inv = Investigation.from_alert(
            {"alert_name": "x", "startsAt": "2026-07-27T12:00:00Z"}
        )
        self.assertEqual(inv.window.end, T0 + DEFAULT_LOOKAHEAD)


if __name__ == "__main__":
    unittest.main()
