"""The ordered dedup rules — R0-R5, and the order itself.

Every test here asserts a *rule*, and several assert the **order**, because that is
where this module's failure mode lives: a suppression bug looks exactly like correct
behaviour until it drops a P1, and nothing downstream notices. The order tests are
the ones that were mutation-checked — see the ROADMAP pointer for W2 L4b.

`now` is passed explicitly everywhere. A time-window rule tested against the wall
clock is a test that passes on a fast machine.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from agent.core.dedup import (
    AlertEvent,
    AlertGroup,
    AlertLedger,
    DedupPolicy,
    SeverityLadder,
    decide,
)

T0 = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
KEY = '{}:{alertname="HighErrorRate", service="auth"}'


def group(
    severity: str = "P2",
    *,
    key: str = KEY,
    status: str = "firing",
    source: str = "alertmanager",
    severities: tuple[str, ...] | None = None,
    ends_at: datetime | None = None,
) -> AlertGroup:
    levels = severities if severities is not None else (severity,)
    return AlertGroup(
        dedup_key=key,
        status=status,
        source=source,
        members=tuple(
            AlertEvent(
                fingerprint=f"fp-{i}-{level}",
                labels={"alertname": "HighErrorRate", "service": "auth", "severity": level},
                status="resolved" if status == "resolved" else "firing",
                starts_at=T0,
                ends_at=ends_at,
            )
            for i, level in enumerate(levels)
        ),
        t0=T0,
    )


def ledger(**policy_kwargs) -> AlertLedger:
    return AlertLedger(policy=DedupPolicy(**policy_kwargs))


class TestSeverityLadder(unittest.TestCase):
    def test_lower_index_is_more_severe(self):
        ladder = SeverityLadder()
        self.assertTrue(ladder.is_higher("P1", than="P2"))
        self.assertFalse(ladder.is_higher("P2", than="P1"))
        self.assertFalse(ladder.is_higher("P2", than="P2"))

    def test_the_worst_of_a_mixed_group_wins(self):
        self.assertEqual(SeverityLadder().worst(["P3", "P1", "P2"]), "P1")

    def test_escalation_stops_at_the_top(self):
        """Unbounded `severity + 1` on repeated recurrence eventually pages everyone,
        which is indistinguishable from having no severity at all."""
        ladder = SeverityLadder()
        self.assertEqual(ladder.escalate("P3"), "P2")
        self.assertEqual(ladder.escalate("P2"), "P1")
        self.assertEqual(ladder.escalate("P1"), "P1")

    def test_an_unknown_severity_ranks_as_most_severe(self):
        """The asymmetry decides it: a duplicate investigation costs tokens, a
        suppressed P1 costs an outage. So a severity we cannot order is
        unsuppressible."""
        ladder = SeverityLadder()
        self.assertTrue(ladder.is_higher("catastrophe", than="P1"))
        self.assertFalse(ladder.known("catastrophe"))

    def test_an_unknown_severity_cannot_be_escalated(self):
        self.assertEqual(SeverityLadder().escalate("catastrophe"), "catastrophe")


class TestRuleR5(unittest.TestCase):
    def test_a_first_arrival_is_new(self):
        decision = decide(group(), ledger(), T0)
        self.assertEqual((decision.action, decision.rule), ("new", "R5"))
        self.assertEqual(decision.severity, "P2")

    def test_severity_comes_from_the_members_not_from_the_group(self):
        """Measured trap: a real AlertManager omits `severity` from `commonLabels`
        when a group's members differ, so reading it there returns nothing exactly
        when an escalation happens."""
        decision = decide(group(severities=("P2", "P1")), ledger(), T0)
        self.assertEqual(decision.severity, "P1")


class TestRuleR1Join(unittest.TestCase):
    def test_a_repeat_joins_the_investigation_in_flight(self):
        led = ledger()
        led.mark_in_flight(KEY, "inv_1", "P2")
        decision = decide(group(), led, T0)
        self.assertEqual((decision.action, decision.rule), ("join", "R1"))
        self.assertEqual(decision.investigation_id, "inv_1")

    def test_an_escalation_mid_flight_joins_and_raises_in_place(self):
        """R0's condition is "higher than what was already *delivered*", so an
        escalation while an investigation is running is R1's case. Joining and
        raising the severity in place keeps one condition producing one report;
        forking would spend two budgets on one fault."""
        led = ledger()
        led.mark_in_flight(KEY, "inv_1", "P2")
        decision = decide(group("P1"), led, T0)
        self.assertEqual((decision.action, decision.rule), ("join", "R1"))
        self.assertEqual(decision.severity, "P1")
        self.assertEqual(decision.escalates_from, "P2")
        self.assertTrue(decision.escalated)
        self.assertEqual(led.state(KEY).in_flight_severity, "P1")

    def test_a_lower_severity_mid_flight_does_not_lower_the_investigation(self):
        led = ledger()
        led.mark_in_flight(KEY, "inv_1", "P1")
        decision = decide(group("P3"), led, T0)
        self.assertEqual(decision.severity, "P1")
        self.assertFalse(decision.escalated)


class TestRuleR2Drop(unittest.TestCase):
    def test_a_repeat_just_after_delivery_is_dropped(self):
        led = ledger()
        led.mark_delivered(KEY, T0, "P2")
        decision = decide(group("P2"), led, T0 + timedelta(minutes=2))
        self.assertEqual((decision.action, decision.rule), ("drop", "R2"))
        self.assertTrue(decision.suppressed)

    def test_the_reason_names_the_elapsed_time(self):
        """The reason is read by a human in a post-mortem asking why nothing ran."""
        led = ledger()
        led.mark_delivered(KEY, T0, "P2")
        decision = decide(group("P2"), led, T0 + timedelta(minutes=2))
        self.assertIn("2m", decision.reason)

    def test_past_the_recurrence_window_it_is_a_fresh_incident(self):
        led = ledger()
        led.mark_delivered(KEY, T0, "P2")
        decision = decide(group("P2"), led, T0 + timedelta(minutes=30))
        self.assertEqual((decision.action, decision.rule), ("new", "R5"))


class TestRuleR0Escalation(unittest.TestCase):
    """R0 precedes every suppression rule. This is the class that would notice a
    reordering, and the one the mutation test targets."""

    def test_a_higher_severity_after_a_delivered_report_is_never_dropped(self):
        led = ledger()
        led.mark_delivered(KEY, T0, "P2")
        decision = decide(group("P1"), led, T0 + timedelta(minutes=2))
        self.assertEqual(
            (decision.action, decision.rule),
            ("new", "R0"),
            "R2's window covers this arrival; R0 has to win or a P1 is lost",
        )
        self.assertEqual(decision.severity, "P1")
        self.assertEqual(decision.escalates_from, "P2")

    def test_the_same_severity_after_delivery_is_still_dropped(self):
        """R0 must not swallow R2 — otherwise nothing is ever deduplicated."""
        led = ledger()
        led.mark_delivered(KEY, T0, "P2")
        self.assertEqual(decide(group("P2"), led, T0 + timedelta(minutes=1)).rule, "R2")

    def test_an_unrecognised_severity_after_delivery_survives_and_is_flagged(self):
        led = ledger()
        led.mark_delivered(KEY, T0, "P1")
        decision = decide(group("SEV0"), led, T0 + timedelta(minutes=1))
        self.assertEqual(decision.action, "new")
        self.assertTrue(
            decision.severity_unrecognised,
            "the fail-open has to be counted, not silent",
        )

    def test_r0_does_not_fire_before_anything_was_delivered(self):
        decision = decide(group("P1"), ledger(), T0)
        self.assertEqual(decision.rule, "R5")


class TestRuleR3Recurrence(unittest.TestCase):
    def test_recurrence_after_the_suppression_window_reopens_escalated(self):
        led = ledger()
        led.mark_delivered(KEY, T0, "P2")
        # Two arrivals inside R2's window, dropped but counted: they are the evidence
        # that the condition kept firing.
        decide(group("P2"), led, T0 + timedelta(minutes=1))
        decide(group("P2"), led, T0 + timedelta(minutes=2))
        decision = decide(group("P2"), led, T0 + timedelta(minutes=7))

        self.assertEqual((decision.action, decision.rule), ("new", "R3"))
        self.assertEqual(decision.severity, "P1", "a returning incident is worse")
        self.assertTrue(decision.escalated)

    def test_too_few_arrivals_is_a_straggler_not_a_recurrence(self):
        led = ledger()
        led.mark_delivered(KEY, T0, "P2")
        decision = decide(group("P2"), led, T0 + timedelta(minutes=7))
        self.assertEqual(decision.rule, "R5")
        self.assertEqual(decision.severity, "P2")

    def test_the_arrival_counter_resets_on_delivery(self):
        """Found by measuring, not by a test: R3 reported "5 arrivals" for what was
        the third arrival since the report. A lifetime counter sits permanently above
        the threshold after the third arrival ever, so every later recurrence reopens
        escalated and "count >= 3" stops meaning anything."""
        led = ledger()
        for minute in (0, 1, 2, 3):
            decide(group("P2"), led, T0 + timedelta(minutes=minute))
        self.assertEqual(led.state(KEY).arrivals, 4)

        led.mark_delivered(KEY, T0 + timedelta(minutes=5), "P2")
        self.assertEqual(led.state(KEY).arrivals, 0)

        decision = decide(group("P2"), led, T0 + timedelta(minutes=11))
        self.assertEqual(
            decision.rule, "R5", "one arrival since the report is not a recurrence"
        )

    def test_a_delivery_timestamp_in_the_future_does_not_suppress(self):
        """A replayed sequence with scripted times, or a clock that stepped. This
        produced the reason "a report was delivered -180s ago" and dropped the alert;
        fail-open is the same direction as an unorderable severity."""
        led = ledger()
        led.mark_delivered(KEY, T0 + timedelta(minutes=3), "P2")
        decision = decide(group("P2"), led, T0)
        self.assertEqual(decision.action, "new")
        self.assertNotIn("-", decision.reason)

    def test_escalation_has_a_ceiling_under_repeated_recurrence(self):
        led = ledger()
        severity = "P3"
        for _ in range(5):
            led.mark_delivered(KEY, T0, severity)
            for minute in (1, 2):
                decide(group(severity), led, T0 + timedelta(minutes=minute))
            decision = decide(group(severity), led, T0 + timedelta(minutes=7))
            severity = decision.severity
        self.assertEqual(severity, "P1", "escalation must stop at the top of the ladder")


class TestRuleR4Burst(unittest.TestCase):
    """R4 is scoped to sources with no `for:` semantics of their own.

    Every Prometheus rule in `mock/prometheus/alerts.yml` sets `for: 1m`, so no
    current source opts in and R4 has **no golden case** — recorded as a coverage gap
    in DIAGNOSIS rather than left looking load-bearing.
    """

    def test_alertmanager_alerts_are_never_held(self):
        decision = decide(group("P3", source="alertmanager"), ledger(), T0)
        self.assertEqual(decision.rule, "R5")

    def test_a_single_low_severity_log_alert_is_held(self):
        decision = decide(group("P3", source="log_pattern"), ledger(), T0)
        self.assertEqual((decision.action, decision.rule), ("hold", "R4"))
        self.assertEqual(decision.count, 1)

    def test_the_threshold_turns_a_burst_into_one_escalated_event(self):
        led = ledger()
        for _ in range(2):
            decide(group("P3", source="log_pattern"), led, T0)
        decision = decide(group("P3", source="log_pattern"), led, T0)

        self.assertEqual((decision.action, decision.rule), ("aggregate", "R4"))
        self.assertEqual(decision.count, 3)
        self.assertEqual(
            decision.severity, "P2", "the aggregate is more severe than any member"
        )

    def test_a_p1_is_never_held(self):
        decision = decide(group("P1", source="log_pattern"), ledger(), T0)
        self.assertEqual(decision.action, "new")

    def test_a_stale_buffer_cannot_aggregate_alerts_that_were_never_a_burst(self):
        """Two alerts an hour apart are not a burst. Without pruning inside `decide`
        they would sit in one buffer and let a third arrival aggregate three."""
        led = ledger()
        decide(group("P3", source="log_pattern"), led, T0)
        decide(group("P3", source="log_pattern"), led, T0 + timedelta(hours=1))
        decision = decide(
            group("P3", source="log_pattern"), led, T0 + timedelta(hours=1, seconds=1)
        )
        self.assertEqual(decision.action, "hold")
        self.assertEqual(decision.count, 2)
        self.assertEqual(
            [f.count for f in led.pending_flushes],
            [1],
            "the pruned arrival must still be counted somewhere",
        )

    def test_expiry_flushes_with_a_count_rather_than_dropping_silently(self):
        led = ledger()
        decide(group("P3", source="log_pattern"), led, T0)
        decide(group("P4", source="log_pattern"), led, T0 + timedelta(seconds=30))

        self.assertEqual(led.flush_expired(T0 + timedelta(minutes=1)), [])
        flushed = led.flush_expired(T0 + timedelta(minutes=5))

        self.assertEqual(len(flushed), 1)
        self.assertEqual(flushed[0].count, 2)
        self.assertEqual(led.counts["flushed"], 1)


class TestResolved(unittest.TestCase):
    """`resolved` is a state transition, handled before every rule — self-healing
    mid-investigation is diagnostic information, not noise."""

    def test_a_resolved_alert_never_creates_an_investigation(self):
        decision = decide(group("P1", status="resolved"), ledger(), T0)
        self.assertEqual((decision.action, decision.rule), ("drop", "resolved"))

    def test_a_resolution_mid_flight_joins_the_investigation(self):
        led = ledger()
        led.mark_in_flight(KEY, "inv_1", "P1")
        decision = decide(group("P1", status="resolved"), led, T0)
        self.assertEqual((decision.action, decision.rule), ("join", "resolved"))
        self.assertEqual(decision.investigation_id, "inv_1")

    def test_a_resolution_clears_a_hold_buffer_and_reports_the_count(self):
        led = ledger()
        decide(group("P3", source="log_pattern"), led, T0)
        decision = decide(group("P3", status="resolved", source="log_pattern"), led, T0)
        self.assertEqual(decision.action, "drop")
        self.assertEqual(decision.count, 1)
        self.assertEqual(led.state(KEY).held, [])

    def test_a_resolution_does_not_count_as_an_arrival(self):
        """Otherwise a flapping condition would reach R3's arrival threshold on
        resolutions alone, and reopen at a raised severity for having recovered."""
        led = ledger()
        decide(group("P2", status="resolved"), led, T0)
        self.assertEqual(led.state(KEY).arrivals, 0)


class TestStormCap(unittest.TestCase):
    def _saturate(self, cap: int) -> AlertLedger:
        led = ledger(storm_max_concurrent=cap)
        for i in range(cap):
            key = f"key-{i}"
            decision = decide(group("P2", key=key), led, T0)
            led.mark_in_flight(key, f"inv_{i}", decision.severity)
        return led

    def test_the_first_excess_condition_opens_one_fleet_aggregate(self):
        led = self._saturate(3)
        decision = decide(group("P2", key="key-98"), led, T0)
        self.assertEqual((decision.action, decision.rule), ("aggregate", "storm"))
        self.assertEqual(decision.count, 1)

    def test_every_later_condition_joins_it_rather_than_opening_another(self):
        """The bug this replaces: returning `aggregate` for all of them changed which
        rule name appeared in the record and capped nothing — forty conditions still
        produced forty investigations. Every rule was individually correct, which is
        why only a created-vs-delivered count found it."""
        led = self._saturate(3)
        first = decide(group("P2", key="key-98"), led, T0)
        led.mark_storm("inv_storm")
        led.mark_in_flight("key-98", "inv_storm", first.severity)

        joins = [decide(group("P2", key=f"key-{i}"), led, T0) for i in range(50, 60)]

        self.assertTrue(all(d.action == "join" for d in joins))
        self.assertTrue(all(d.investigation_id == "inv_storm" for d in joins))
        self.assertEqual(joins[-1].count, 11, "N conditions affected, counted")

    def test_the_aggregate_retires_when_its_investigation_reports(self):
        """Otherwise the next storm joins an already-reported investigation and the
        affected-condition count grows across unrelated events."""
        led = self._saturate(1)
        decide(group("P2", key="key-98"), led, T0)
        led.mark_storm("inv_storm")
        led.mark_in_flight("key-98", "inv_storm", "P2")

        led.mark_delivered("key-98", T0 + timedelta(minutes=5), "P2")

        self.assertEqual(led.storm_investigation_id, "")
        self.assertEqual(led.storm_keys, set())

    def test_an_abandoned_aggregate_also_retires(self):
        led = self._saturate(1)
        decide(group("P2", key="key-98"), led, T0)
        led.mark_storm("inv_storm")
        led.mark_in_flight("key-98", "inv_storm", "P2")
        led.mark_finished("key-98")
        self.assertEqual(led.storm_investigation_id, "")

    def test_an_escalation_is_never_lost_to_the_cap(self):
        """R0 and R3 are checked before the cap, so a fleet-wide event cannot bury
        the one alert that got worse."""
        led = ledger(storm_max_concurrent=1)
        led.mark_delivered("key-a", T0, "P2")
        led.mark_in_flight("key-b", "inv_b", "P2")

        decision = decide(group("P1", key="key-a"), led, T0 + timedelta(minutes=1))
        self.assertEqual((decision.action, decision.rule), ("new", "R0"))

    def test_a_join_is_never_lost_to_the_cap(self):
        led = ledger(storm_max_concurrent=1)
        led.mark_in_flight(KEY, "inv_1", "P2")
        self.assertEqual(decide(group("P2"), led, T0).action, "join")


class TestObservability(unittest.TestCase):
    def test_every_decision_is_counted_by_rule(self):
        """"Suppressed / held / escalated counts recorded, not silent" is a Week-2
        exit-table row, and a suppression that is not counted is indistinguishable
        from an alert that never arrived."""
        led = ledger()
        decide(group("P2"), led, T0)
        led.mark_delivered(KEY, T0, "P2")
        decide(group("P2"), led, T0 + timedelta(minutes=1))
        decide(group("P2"), led, T0 + timedelta(minutes=2))

        self.assertEqual(led.counts, {"R5": 1, "R2": 2})

    def test_a_derived_key_is_flagged_on_the_decision(self):
        derived = AlertGroup(dedup_key="derived:{}", status="firing", key_derived=True)
        self.assertTrue(decide(derived, ledger(), T0).key_derived)

    def test_a_group_key_containing_severity_is_flagged(self):
        """If the operator put `severity` in AlertManager's `group_by`, an escalation
        arrives under a different key and R0/R3 cannot see it."""
        risky = AlertGroup(
            dedup_key='{}:{alertname="X", severity="P1"}',
            status="firing",
            key_contains_severity=True,
        )
        self.assertTrue(decide(risky, ledger(), T0).key_contains_severity)

    def test_only_informative_decisions_are_shown_to_the_agent(self):
        led = ledger()
        self.assertFalse(decide(group("P2"), led, T0).is_noteworthy)

        led.mark_delivered(KEY, T0, "P2")
        self.assertTrue(
            decide(group("P1"), led, T0 + timedelta(minutes=1)).is_noteworthy
        )

    def test_a_decision_serializes_flat_for_a_span_or_an_eval_row(self):
        record = decide(group("P2"), ledger(), T0).as_dict()
        self.assertEqual(record["rule"], "R5")
        self.assertIn("dedup_key", record)
        self.assertIsInstance(record["escalated"], bool)


class TestPolicyIsPure(unittest.TestCase):
    def test_decide_does_not_mark_an_investigation_in_flight(self):
        """Lifecycle belongs to the harness. A policy function that mutated the world
        could not be run twice on one input — which is what eval, tests and a dry-run
        all do."""
        led = ledger()
        decide(group("P2"), led, T0)
        self.assertEqual(led.state(KEY).in_flight_id, "")
        self.assertIsNone(led.state(KEY).delivered_at)

    def test_a_restart_fails_safe_towards_investigating(self):
        """A fresh ledger has no suppression state, so an alert that would have been
        dropped creates an investigation instead: one budget, not an outage."""
        led = ledger()
        led.mark_delivered(KEY, T0, "P2")
        self.assertEqual(decide(group("P2"), led, T0 + timedelta(minutes=1)).rule, "R2")
        self.assertEqual(decide(group("P2"), ledger(), T0 + timedelta(minutes=1)).rule, "R5")


if __name__ == "__main__":
    unittest.main()
