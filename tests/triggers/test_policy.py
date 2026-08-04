"""Config loading — and the guarantee that a bad config degrades rather than crashes.

Two properties matter more than the parsing:

- **the shipped files load into the values they read as**, so a threshold someone
  changes in YAML actually changes behaviour;
- **a missing or malformed key is a partial override, not an exception.** A typo in a
  suppression window must not stop an alert being investigated.
"""
from __future__ import annotations

import pathlib
import tempfile
import unittest
from datetime import timedelta

from agent.core.dedup import DedupPolicy
from agent.triggers.policy import (
    ALERTING_PATH,
    BUDGETS_PATH,
    load_alerting,
    load_budgets,
    parse_duration,
)


class TestDurations(unittest.TestCase):
    def test_prometheus_syntax(self):
        default = timedelta(seconds=1)
        self.assertEqual(parse_duration("5m", default), timedelta(minutes=5))
        self.assertEqual(parse_duration("90s", default), timedelta(seconds=90))
        self.assertEqual(parse_duration("1h", default), timedelta(hours=1))

    def test_a_bare_number_is_seconds(self):
        """The one ambiguity worth resolving explicitly. Seconds errs *shorter*, so a
        mistyped suppression window suppresses less."""
        self.assertEqual(parse_duration(30, timedelta(days=1)), timedelta(seconds=30))

    def test_a_malformed_value_falls_back(self):
        default = timedelta(minutes=5)
        self.assertEqual(parse_duration("soon", default), default)
        self.assertEqual(parse_duration(None, default), default)
        self.assertEqual(parse_duration("5 minutes", default), default)


class TestShippedFiles(unittest.TestCase):
    def test_both_config_files_exist_where_the_loader_looks(self):
        self.assertTrue(ALERTING_PATH.exists(), ALERTING_PATH)
        self.assertTrue(BUDGETS_PATH.exists(), BUDGETS_PATH)

    def test_alerting_yaml_loads_the_documented_thresholds(self):
        policy = load_alerting()
        self.assertEqual(policy.ladder.order, ("P1", "P2", "P3", "P4"))
        self.assertEqual(policy.repeat_suppress, timedelta(minutes=5))
        self.assertEqual(policy.recurrence_window, timedelta(minutes=10))
        self.assertEqual(policy.recurrence_min_arrivals, 3)

    def test_r4_is_scoped_off_every_source_the_stack_actually_has(self):
        """Every Prometheus rule in the mock stack sets `for: 1m`, so aggregating
        again would only add latency to a real signal."""
        policy = load_alerting()
        self.assertNotIn("alertmanager", policy.burst_sources)
        self.assertIn("log_pattern", policy.burst_sources)

    def test_severity_selects_a_budget_and_p1_gets_more_than_p3(self):
        tiers = load_budgets()
        self.assertGreater(
            tiers.for_severity("P1").max_tool_calls,
            tiers.for_severity("P3").max_tool_calls,
        )

    def test_the_p2_tier_still_matches_the_pre_tiering_defaults(self):
        """So nothing changed silently when tiering arrived — the L2 defaults were
        15 turns / 40 tool calls."""
        p2 = load_budgets().for_severity("P2")
        self.assertEqual((p2.max_turns, p2.max_tool_calls), (15, 40))

    def test_an_unknown_severity_gets_the_default_tier_not_the_biggest(self):
        """The two asymmetries point in different directions: an unorderable severity
        must never be *suppressed*, but handing it the largest budget would let one
        mislabelled alert rule spend the most money in the system."""
        tiers = load_budgets()
        self.assertEqual(
            tiers.for_severity("catastrophe").max_tool_calls,
            tiers.for_severity("P2").max_tool_calls,
        )

    def test_chat_and_patrol_are_cheaper_than_any_alert_tier(self):
        tiers = load_budgets()
        cheapest_alert = min(t.max_tool_calls for t in tiers.tiers.values())
        self.assertLessEqual(tiers.for_trigger("chat").max_tool_calls, cheapest_alert)
        self.assertLessEqual(tiers.for_trigger("patrol").max_tool_calls, cheapest_alert)

    def test_every_tier_keeps_a_ceiling_in_both_currencies(self):
        """A currency with no ceiling raises at the gate, so a tier that set only USD
        would turn every DeepSeek call into an error."""
        for name, budget in load_budgets().tiers.items():
            with self.subTest(tier=name):
                self.assertIsNotNone(budget.ceiling_for("USD"))
                self.assertIsNotNone(budget.ceiling_for("CNY"))


class TestDegradation(unittest.TestCase):
    def _write(self, text: str) -> pathlib.Path:
        tmp = pathlib.Path(tempfile.mkdtemp()) / "conf.yaml"
        tmp.write_text(text, encoding="utf-8")
        return tmp

    def test_a_missing_file_yields_the_dataclass_defaults(self):
        self.assertEqual(
            load_alerting("/nonexistent/alerting.yaml").repeat_suppress,
            DedupPolicy().repeat_suppress,
        )

    def test_an_empty_file_yields_the_defaults(self):
        self.assertEqual(
            load_alerting(self._write("")).recurrence_min_arrivals,
            DedupPolicy().recurrence_min_arrivals,
        )

    def test_a_partial_file_overrides_only_what_it_sets(self):
        policy = load_alerting(self._write("dedup:\n  repeat_suppress: 90s\n"))
        self.assertEqual(policy.repeat_suppress, timedelta(seconds=90))
        self.assertEqual(policy.recurrence_window, DedupPolicy().recurrence_window)

    def test_a_scalar_where_a_mapping_belongs_is_ignored(self):
        policy = load_alerting(self._write("dedup: 5\nburst: nonsense\n"))
        self.assertEqual(policy.repeat_suppress, DedupPolicy().repeat_suppress)

    def test_a_tier_setting_only_one_currency_keeps_the_other_ceiling(self):
        tiers = load_budgets(
            self._write("tiers:\n  P1:\n    max_turns: 30\n    max_cost: {USD: 2.0}\n")
        )
        p1 = tiers.for_severity("P1")
        self.assertEqual(p1.max_turns, 30)
        self.assertEqual(p1.ceiling_for("USD"), 2.0)
        self.assertIsNotNone(p1.ceiling_for("CNY"), "an unset currency raises at the gate")


if __name__ == "__main__":
    unittest.main()
