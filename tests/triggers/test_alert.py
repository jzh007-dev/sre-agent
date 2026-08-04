"""The alert trigger, asserted against payloads a real AlertManager actually sent.

`FIRING_P2`, `ESCALATED` and `RESOLVED` below are **captured**, not written from
memory: `prom/alertmanager:v0.27.0` (the version `mock/docker-compose.yml` pins) was
run in a container with a webhook receiver on the host, and one condition was posted
at P2 and then at P1. Trimmed only of `receiver` / `externalURL` / `generatorURL` /
`version`, which nothing here reads.

They are inlined rather than kept as fixture files because three of this module's
decisions rest on their exact shape, and a reader checking those decisions should not
have to open another file:

1. `alerts[].fingerprint` **differs between the two severities of one condition**, so
   it cannot be the dedup key;
2. `groupKey` is severity-free, and AlertManager delivered both severities under
   **one** of them;
3. `commonLabels` **drops `severity`** in that mixed webhook, so reading severity
   there returns nothing exactly when an escalation happens.
"""
from __future__ import annotations

import json
import pathlib
import unittest
from datetime import datetime, timedelta, timezone

from agent.core.dedup import AlertLedger, DedupPolicy
from agent.core.investigation import ToolBudget
from agent.triggers.alert import (
    AlertTrigger,
    derive_dedup_key,
    investigation_from_payload,
    parse_webhook,
)
from agent.triggers.policy import BudgetTiers

GOLDEN = pathlib.Path(__file__).resolve().parents[2] / "eval" / "golden"

T0 = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
GROUP_KEY = '{}:{alertname="HighErrorRate", service="auth"}'

FIRING_P2 = {
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "HighErrorRate",
                "env": "mock",
                "service": "auth",
                "severity": "P2",
            },
            "annotations": {"summary": "auth 5xx"},
            "startsAt": "2026-08-04T10:00:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "fingerprint": "327b605fce1b794f",
        }
    ],
    "groupLabels": {"alertname": "HighErrorRate", "service": "auth"},
    "commonLabels": {
        "alertname": "HighErrorRate",
        "env": "mock",
        "service": "auth",
        "severity": "P2",
    },
    "commonAnnotations": {"summary": "auth 5xx"},
    "groupKey": GROUP_KEY,
    "truncatedAlerts": 0,
}

#: The escalation: same condition, now also firing at P1. Note both members arrive in
#: ONE webhook under the SAME groupKey, and `commonLabels` has no `severity`.
ESCALATED = {
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "HighErrorRate",
                "env": "mock",
                "service": "auth",
                "severity": "P1",
            },
            "annotations": {"summary": "auth 5xx"},
            "startsAt": "2026-08-04T10:02:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "fingerprint": "3277605fce179078",
        },
        {
            "status": "firing",
            "labels": {
                "alertname": "HighErrorRate",
                "env": "mock",
                "service": "auth",
                "severity": "P2",
            },
            "annotations": {"summary": "auth 5xx"},
            "startsAt": "2026-08-04T10:00:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "fingerprint": "327b605fce1b794f",
        },
    ],
    "groupLabels": {"alertname": "HighErrorRate", "service": "auth"},
    "commonLabels": {"alertname": "HighErrorRate", "env": "mock", "service": "auth"},
    "commonAnnotations": {"summary": "auth 5xx"},
    "groupKey": GROUP_KEY,
    "truncatedAlerts": 0,
}

RESOLVED = {
    "status": "resolved",
    "alerts": [
        {
            "status": "resolved",
            "labels": {
                "alertname": "HighLatency",
                "env": "mock",
                "service": "auth",
                "severity": "P1",
            },
            "annotations": {"summary": "auth slow"},
            "startsAt": "2026-08-04T10:02:00Z",
            "endsAt": "2026-08-04T10:05:00Z",
            "fingerprint": "babc2aef7b5a3822",
        }
    ],
    "groupLabels": {"alertname": "HighLatency", "service": "auth"},
    "commonLabels": {
        "alertname": "HighLatency",
        "env": "mock",
        "service": "auth",
        "severity": "P1",
    },
    "commonAnnotations": {"summary": "auth slow"},
    "groupKey": '{}:{alertname="HighLatency", service="auth"}',
    "truncatedAlerts": 0,
}


def clock(*times: datetime):
    """A clock that returns each time in turn, then repeats the last."""
    remaining = list(times)

    def tick() -> datetime:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return tick


def trigger(*times: datetime, **kwargs) -> AlertTrigger:
    kwargs.setdefault("budgets", BudgetTiers(
        tiers={
            "P1": ToolBudget(max_turns=20, max_tool_calls=60),
            "P2": ToolBudget(max_turns=15, max_tool_calls=40),
            "P3": ToolBudget(max_turns=8, max_tool_calls=20),
        },
    ))
    return AlertTrigger(now=clock(*(times or (T0,))), **kwargs)


class TestMeasuredPayloadShape(unittest.TestCase):
    """What the captured payloads prove, asserted rather than described."""

    def test_one_condition_has_two_fingerprints_at_two_severities(self):
        fingerprints = {
            alert["labels"]["severity"]: alert["fingerprint"]
            for alert in ESCALATED["alerts"]
        }
        self.assertNotEqual(
            fingerprints["P1"],
            fingerprints["P2"],
            "AlertManager hashes every label, severity included — so the fingerprint "
            "cannot be the dedup key",
        )

    def test_both_severities_share_one_group_key(self):
        self.assertEqual(parse_webhook(FIRING_P2).dedup_key, GROUP_KEY)
        self.assertEqual(parse_webhook(ESCALATED).dedup_key, GROUP_KEY)

    def test_the_group_key_contains_no_severity(self):
        self.assertNotIn("severity", parse_webhook(ESCALATED).dedup_key)
        self.assertFalse(parse_webhook(ESCALATED).key_contains_severity)

    def test_severity_is_read_from_members_because_commonLabels_drops_it(self):
        self.assertNotIn("severity", ESCALATED["commonLabels"])
        group = parse_webhook(ESCALATED)
        self.assertEqual(sorted(group.severities), ["P1", "P2"])

    def test_a_real_group_key_is_not_marked_derived(self):
        self.assertFalse(parse_webhook(FIRING_P2).key_derived)

    def test_t0_is_the_earliest_member_not_the_latest(self):
        """The incident started when the condition first fired, not when it got
        worse — and the window is anchored on T0."""
        self.assertEqual(parse_webhook(ESCALATED).t0, T0)

    def test_gos_zero_endsAt_reads_as_absent(self):
        """A firing alert carries `endsAt: 0001-01-01T00:00:00Z`, which parses into a
        year-1 datetime and would sort ahead of every real timestamp."""
        self.assertIsNone(parse_webhook(FIRING_P2).members[0].ends_at)
        self.assertIsNotNone(parse_webhook(RESOLVED).members[0].ends_at)

    def test_a_group_is_resolved_only_when_every_member_is(self):
        self.assertTrue(parse_webhook(RESOLVED).is_resolved)
        self.assertFalse(parse_webhook(ESCALATED).is_resolved)

    def test_fingerprints_are_carried_verbatim(self):
        """Not the dedup key, but still the join to AlertManager's own notification
        log and to the incident tracker, both of which key on it."""
        self.assertEqual(
            sorted(parse_webhook(ESCALATED).fingerprints),
            ["3277605fce179078", "327b605fce1b794f"],
        )


class TestGroupKeyWithSeverityIsFlagged(unittest.TestCase):
    def test_severity_in_group_by_is_detected_from_the_key_itself(self):
        """We cannot read the operator's `group_by`, but the key is a readable
        string, and if severity is in it an escalation arrives under a different key
        and R0/R3 go blind."""
        payload = dict(FIRING_P2)
        payload["groupKey"] = '{}:{alertname="HighErrorRate", severity="P2"}'
        self.assertTrue(parse_webhook(payload).key_contains_severity)


class TestSimplifiedFixtureShape(unittest.TestCase):
    """The Week-1 golden fixtures predate all of this: no `alerts[]`, no `groupKey`,
    no `status`, no `startsAt`. W2 L4c reshapes them."""

    def test_a_key_is_derived_and_marked_as_ours(self):
        payload = json.loads((GOLDEN / "GS-RES-001-redis-oom" / "alert.json").read_text())
        group = parse_webhook(payload)
        self.assertTrue(group.key_derived)
        self.assertTrue(group.dedup_key.startswith("derived:"))

    def test_the_derived_key_excludes_severity(self):
        key = derive_dedup_key(
            {"alertname": "HighErrorRate", "service": "auth", "severity": "P1"}
        )
        self.assertNotIn("severity", key)
        self.assertEqual(
            key, derive_dedup_key({"alertname": "HighErrorRate", "service": "auth"})
        )

    def test_a_fingerprint_is_left_empty_rather_than_invented(self):
        """Fabricating something that looks like AlertManager's id is exactly the
        second, divergent definition TRADEOFFS §38 says not to create."""
        payload = json.loads((GOLDEN / "GS-RES-001-redis-oom" / "alert.json").read_text())
        self.assertEqual(parse_webhook(payload).fingerprints, [])

    def test_severity_is_recovered_from_the_flat_field(self):
        group = parse_webhook({"alert_name": "X", "severity": "P1"})
        self.assertEqual(group.severities, ["P1"])

    def test_the_source_is_read_from_the_fixture_field(self):
        payload = json.loads((GOLDEN / "GS-RES-001-redis-oom" / "alert.json").read_text())
        self.assertEqual(parse_webhook(payload).source, "alertmanager")

    def test_all_eight_golden_cases_parse_to_one_group_each(self):
        cases = sorted(GOLDEN.glob("*/alert.json"))
        self.assertGreaterEqual(len(cases), 8)
        for path in cases:
            with self.subTest(case=path.parent.name):
                group = parse_webhook(json.loads(path.read_text()))
                self.assertTrue(group.dedup_key)
                self.assertIn(group.severities[0], ("P1", "P2"))
                self.assertTrue(group.correlation_id, "needed for the trace join")


class TestFixtureMetadataGuard(unittest.TestCase):
    """Every golden case's `alert.json` carries a `_meta.purpose` that states the root
    cause in prose. If that reached the prompt the agent would be graded on reading
    the answer key, and every accuracy number in the project would be meaningless
    while still looking healthy.

    Moved here from `tests/agent/test_investigation.py` with the function it guards.
    """

    def test_the_answer_never_reaches_the_first_message(self):
        inv = investigation_from_payload(
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

    def test_underscore_keys_are_dropped(self):
        from agent.triggers.alert import strip_fixture_metadata

        cleaned = strip_fixture_metadata(
            {
                "_meta": {"purpose": "root cause is Redis OOM"},
                "_internal": "bookkeeping",
                "alert_name": "HighErrorRate",
            }
        )
        self.assertEqual(cleaned, {"alert_name": "HighErrorRate"})

    def test_real_golden_fixtures_are_all_scrubbed(self):
        """Runs against the actual fixtures, so a future case that reintroduces the
        leak fails here rather than in a silent eval."""
        from agent.triggers.alert import strip_fixture_metadata

        cases = sorted(GOLDEN.glob("*/alert.json"))
        self.assertGreaterEqual(len(cases), 8, "expected the Week 1 golden cases")
        for path in cases:
            with self.subTest(case=path.parent.name):
                cleaned = strip_fixture_metadata(json.loads(path.read_text()))
                self.assertNotIn("_meta", cleaned)
                self.assertTrue(cleaned, "scrubbing must not empty the payload")

    def test_the_scrub_survives_the_dedup_path_too(self):
        """`preprocess` builds its own message, so the guard has to hold on both
        construction paths and not only on the one-shot helper."""
        payload = json.loads((GOLDEN / "GS-RES-001-redis-oom" / "alert.json").read_text())
        inv = trigger().preprocess(payload).investigations[0]
        text = "".join(
            getattr(b, "text", "") for msg in inv.messages for b in msg.content
        )
        self.assertNotIn("bgsave", text.lower())


class TestInvestigationConstruction(unittest.TestCase):
    def test_window_is_anchored_on_the_alert_not_on_now(self):
        inv = trigger(T0 + timedelta(hours=3)).preprocess(FIRING_P2).investigations[0]
        self.assertLess(inv.window.start, T0)
        self.assertLess(inv.window.end, T0 + timedelta(hours=1))

    def test_correlation_id_is_adopted_from_the_annotation(self):
        payload = json.loads((GOLDEN / "GS-RES-001-redis-oom" / "alert.json").read_text())
        inv = trigger().preprocess(payload).investigations[0]
        self.assertEqual(inv.correlation_id, "gs-res-001-redis-oom")

    def test_a_missing_correlation_id_is_empty_not_invented(self):
        inv = trigger().preprocess(FIRING_P2).investigations[0]
        self.assertEqual(inv.correlation_id, "")

    def test_severity_selects_the_budget_tier(self):
        p2 = trigger().preprocess(FIRING_P2).investigations[0]
        self.assertEqual(p2.budget.max_tool_calls, 40)

        payload = json.loads((GOLDEN / "GS-RES-001-redis-oom" / "alert.json").read_text())
        p1 = trigger().preprocess(payload).investigations[0]
        self.assertEqual(p1.budget.max_tool_calls, 60)

    def test_explicit_t0_wins_over_the_payload(self):
        inv = investigation_from_payload(FIRING_P2, t0=T0 - timedelta(days=1))
        self.assertLess(inv.window.end, T0)


class TestPreprocessDedup(unittest.TestCase):
    def test_a_duplicate_delivery_creates_one_investigation(self):
        """Week-2 exit table: duplicate delivery → investigations created = 1."""
        alert = trigger(T0, T0 + timedelta(seconds=30))
        first = alert.preprocess(FIRING_P2)
        second = alert.preprocess(FIRING_P2)

        self.assertEqual(first.created, 1)
        self.assertEqual(second.created, 0)
        self.assertEqual(second.joined[0].id, first.investigations[0].id)

    def test_an_escalation_mid_flight_raises_the_budget_in_place(self):
        alert = trigger(T0, T0 + timedelta(minutes=2))
        inv = alert.preprocess(FIRING_P2).investigations[0]
        self.assertEqual(inv.budget.max_tool_calls, 40)

        outcome = alert.preprocess(ESCALATED)

        self.assertEqual(outcome.created, 0, "one condition, one report")
        self.assertEqual(outcome.joined[0].id, inv.id)
        self.assertEqual(
            inv.budget.max_tool_calls, 60, "the P1 tier applies to the running run"
        )

    def test_the_join_is_visible_in_the_investigation_messages(self):
        alert = trigger(T0, T0 + timedelta(minutes=2))
        inv = alert.preprocess(FIRING_P2).investigations[0]
        alert.preprocess(ESCALATED)
        text = "".join(
            getattr(b, "text", "") for msg in inv.messages for b in msg.content
        )
        self.assertIn("<alert-update", text)
        self.assertIn("P1", text)

    def test_a_resolution_mid_flight_is_appended_as_evidence(self):
        alert = trigger(T0, T0 + timedelta(minutes=5))
        firing = dict(RESOLVED, status="firing")
        firing["alerts"] = [dict(RESOLVED["alerts"][0], status="firing", endsAt="0001-01-01T00:00:00Z")]
        inv = alert.preprocess(firing).investigations[0]

        outcome = alert.preprocess(RESOLVED)

        self.assertEqual(outcome.created, 0)
        text = "".join(
            getattr(b, "text", "") for msg in inv.messages for b in msg.content
        )
        self.assertIn("RESOLVED", text)
        self.assertIn("not a reason to stop", text)

    def test_a_resolution_for_nothing_in_flight_creates_nothing(self):
        outcome = trigger().preprocess(RESOLVED)
        self.assertEqual(outcome.created, 0)
        self.assertEqual(outcome.decisions[0].action, "drop")

    def test_a_recurrence_note_reaches_the_new_investigation(self):
        alert = trigger(
            T0,
            T0 + timedelta(minutes=1),
            T0 + timedelta(minutes=2),
            T0 + timedelta(minutes=7),
        )
        first = alert.preprocess(FIRING_P2).investigations[0]
        alert.ledger.mark_delivered(GROUP_KEY, T0, "P2")
        alert.preprocess(FIRING_P2)
        alert.preprocess(FIRING_P2)
        outcome = alert.preprocess(FIRING_P2)

        self.assertEqual(outcome.decisions[0].rule, "R3")
        reopened = outcome.investigations[0]
        self.assertNotEqual(reopened.id, first.id)
        text = "".join(
            getattr(b, "text", "") for msg in reopened.messages for b in msg.content
        )
        self.assertIn("<dedup", text)
        self.assertIn("the fix did not take", text)
        self.assertIn("advisory", text, "the agent is allowed to disagree")

    def test_an_ordinary_new_alert_gets_no_dedup_preamble(self):
        """R5 tells an investigation nothing the alert does not. Padding every prompt
        with "no prior investigation for this condition" only costs tokens."""
        inv = trigger().preprocess(FIRING_P2).investigations[0]
        text = "".join(
            getattr(b, "text", "") for msg in inv.messages for b in msg.content
        )
        self.assertNotIn("<dedup", text)

    def test_a_join_whose_investigation_is_gone_fails_safe_to_a_new_one(self):
        """The ledger points at an investigation the store no longer holds — a
        restart. Failing safe means investigating, and the decision is rewritten so
        the record does not claim a join that never happened."""
        alert = trigger(T0, T0 + timedelta(seconds=30))
        first = alert.preprocess(FIRING_P2).investigations[0]
        alert.store.clear()

        outcome = alert.preprocess(FIRING_P2)

        self.assertEqual(outcome.created, 1)
        self.assertEqual(outcome.decisions[0].action, "new")
        self.assertEqual(outcome.decisions[0].escalates_from, first.id)
        self.assertIn("no longer held in memory", outcome.decisions[0].reason)

    def test_held_alerts_are_reported_on_the_delivery_that_expires_them(self):
        alert = trigger(T0, T0 + timedelta(minutes=5), ledger=AlertLedger(
            policy=DedupPolicy(burst_sources=frozenset({"log_pattern"}))
        ))
        log_alert = dict(FIRING_P2, alert_source="log_pattern")
        log_alert["alerts"] = [
            dict(FIRING_P2["alerts"][0], labels={**FIRING_P2["alerts"][0]["labels"], "severity": "P3"})
        ]

        held = alert.preprocess(log_alert)
        self.assertEqual(held.decisions[0].action, "hold")

        later = alert.preprocess(dict(FIRING_P2))
        self.assertEqual([f.count for f in later.flushes], [1])
        self.assertEqual(later.summary()["flushed"], 1)


class TestStormCapEndToEnd(unittest.TestCase):
    """The cap's whole purpose is to stop forking a budget per condition, so the test
    that matters counts investigations, not rule names."""

    def _storm(self, cap: int, conditions: int = 40):
        alert = trigger(
            T0, ledger=AlertLedger(policy=DedupPolicy(storm_max_concurrent=cap))
        )
        outcomes = [
            alert.preprocess(
                dict(FIRING_P2, groupKey=f'{{}}:{{alertname="A", service="svc{i}"}}')
            )
            for i in range(conditions)
        ]
        return alert, outcomes

    def test_forty_conditions_under_a_cap_of_eight_spend_nine_budgets(self):
        alert, outcomes = self._storm(cap=8)
        created = sum(o.created for o in outcomes)
        self.assertEqual(created, 9, "eight ordinary, plus one fleet-wide aggregate")
        self.assertEqual(sum(len(o.joined) for o in outcomes), 31)
        self.assertEqual(len(alert.store), 9)

    def test_without_the_cap_every_condition_spends_its_own(self):
        _, outcomes = self._storm(cap=10**9)
        self.assertEqual(sum(o.created for o in outcomes), 40)

    def test_the_aggregate_reports_how_many_conditions_it_covers(self):
        _, outcomes = self._storm(cap=8)
        last = outcomes[-1].decisions[0]
        self.assertEqual(last.rule, "storm")
        self.assertEqual(last.count, 32)

    def test_the_prompt_stops_listing_conditions_but_the_count_does_not_stop(self):
        """A fleet-wide event has no bound on how many conditions it absorbs, and one
        line each would eventually be the whole context window — in the one
        investigation that must not abort."""
        alert, outcomes = self._storm(cap=2, conditions=60)
        aggregate = next(
            o.investigations[0]
            for o in outcomes
            if o.investigations and o.decisions[0].rule == "storm"
        )
        text = "".join(
            getattr(b, "text", "") for msg in aggregate.messages for b in msg.content
        )
        self.assertIn("infrastructure-wide", text)
        self.assertLess(len(text), 4_000, "the note count is bounded")
        self.assertEqual(
            outcomes[-1].decisions[0].count, 58, "the count is not — 60 less the 2 uncapped"
        )

    def test_the_aggregate_is_told_which_conditions_it_covers(self):
        """A fleet-wide investigation that cannot name the affected services has
        nothing to report."""
        alert, outcomes = self._storm(cap=2, conditions=6)
        aggregate = next(
            o.investigations[0]
            for o in outcomes
            if o.investigations and o.decisions[0].rule == "storm"
        )
        text = "".join(
            getattr(b, "text", "") for msg in aggregate.messages for b in msg.content
        )
        self.assertIn("svc5", text)
        self.assertIn("Also affected", text)


class TestStoreIsInjectable(unittest.TestCase):
    def test_the_caller_may_own_the_registry(self):
        """W2 L7's ingress holds the in-memory registry; the default dict is what
        makes the trigger usable before that exists."""
        store: dict = {}
        alert = trigger(store=store)
        inv = alert.preprocess(FIRING_P2).investigations[0]
        self.assertEqual(store[inv.id], inv)


if __name__ == "__main__":
    unittest.main()
