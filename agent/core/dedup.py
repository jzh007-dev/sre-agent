"""Deduplication — which alerts constitute one investigation.

[TRADEOFFS §38](../../TRADEOFFS.md#38-alert-processing-alertmanager-owns-the-notification-layer-we-own-the-investigation-layer)
divides the work: **AlertManager owns the notification layer** (which pages fire,
batched how, so a human is not woken four times) and **we own the investigation
layer** (which alerts are one diagnostic unit, so the agent spends one budget
rather than four). This module is the second half, and nothing else.

The rules are [DIAGNOSIS P6a](../../DIAGNOSIS.md), ordered, first match wins —
**the order is the policy**:

| # | Condition | Decision |
|---|---|---|
| R0 | severity higher than what was already *delivered* | new, never suppressed |
| R1 | investigation in flight | join (escalating it in place if needed) |
| R2 | delivered, Δ < 5 min | drop |
| R3 | delivered, 5-10 min, arrivals ≥ 3 | new, severity raised |
| R4 | low severity, burst count below threshold | hold |
| R5 | otherwise | new |

A suppression bug here looks exactly like correct behaviour until it drops a P1,
so `tests/agent/test_dedup.py` asserts the *ordering* rather than only the rules,
and each guard was mutation-tested.

**The key is AlertManager's `groupKey`, not `alerts[].fingerprint`** — measured,
not assumed. Against `prom/alertmanager:v0.27.0`, one condition posted at P2 and
then at P1 produces two different fingerprints (`327b605fce1b794f` /
`3277605fce179078`) because `Alert.Fingerprint()` hashes every label, severity
included. Keying on that would make an escalation look like an unrelated alert and
silently disable R0 and R3 — the exact failure P6a warns about. `groupKey`
(`{}:{alertname="HighErrorRate", service="auth"}`) is AlertManager's own definition
of one notification group and is severity-free under any sane `group_by`, so
consuming it satisfies §38 better than a hash of our own would. Per-member
fingerprints are still carried verbatim, for the join to their log and for L4c.

Pure policy: no I/O, no config parsing, no clock of its own. `now` is a parameter
for the same reason `transport.py` injects its sleeper — the time-window rules are
most of the behaviour and they must be testable offline and exactly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Mapping, Sequence

#: What `decide` concluded should happen to an incoming group.
#:
#: `aggregate` is deliberately distinct from `new`: it is a fleet-wide event
#: standing in for several keys, so a consumer that treats it as an ordinary
#: investigation is still correct, and one that wants to say "N services affected"
#: has the information to.
Action = Literal["new", "join", "drop", "hold", "aggregate"]

#: Severity ranks, best-to-worst. Index is the rank, so a *lower* index is *more*
#: severe — P1 is rank 0. Overridden from `config/alerting.yaml`.
DEFAULT_SEVERITY_ORDER: tuple[str, ...] = ("P1", "P2", "P3", "P4")


@dataclass(frozen=True)
class AlertEvent:
    """One member of a webhook — one alert instance, as AlertManager sends it.

    `fingerprint` is theirs, verbatim. It is not the dedup key (see the module
    docstring) but it is the join to their notification log and to the
    incident-tracker's records, both of which key on it.
    """

    fingerprint: str
    labels: Mapping[str, str] = field(default_factory=dict)
    annotations: Mapping[str, str] = field(default_factory=dict)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    #: "firing" | "resolved", per member. A group can be `firing` overall while one
    #: member has already resolved.
    status: str = "firing"

    @property
    def severity(self) -> str:
        return self.labels.get("severity", "")


@dataclass(frozen=True)
class AlertGroup:
    """One webhook — and therefore, per §38, already one investigation.

    That is the resolution of the "two systems have opinions about grouping"
    problem: AlertManager may batch four alerts into one delivery, and we accept
    that batch as one diagnostic unit. Correlation *across* webhooks is L4c.
    """

    #: AlertManager's `groupKey`, or a derived stand-in when the source does not
    #: send one — see `key_derived`.
    dedup_key: str
    #: "firing" | "resolved" — the group's status, which is `resolved` only when
    #: every member has resolved.
    status: str
    members: Sequence[AlertEvent] = ()
    #: Which pipeline produced this. Decides whether R4 applies at all: a source
    #: with its own `for:`/duration semantics has already done burst aggregation.
    source: str = "alertmanager"
    #: The observed system's own incident id, for the trace join.
    correlation_id: str = ""
    #: Incident start — the earliest member's `startsAt`. Anchors `Window`.
    t0: datetime | None = None
    #: True when `dedup_key` was computed by us because the payload carried no
    #: `groupKey`. Surfaced on every decision rather than hidden: it means the key
    #: is *our* definition, which is what §38 says to avoid where possible.
    key_derived: bool = False
    #: True when the group key contains a `severity=` term, which happens if the
    #: operator put `severity` in AlertManager's `group_by`. Then an escalation
    #: arrives under a different key and R0/R3 cannot see it. We cannot read their
    #: config, but the key is a readable string, so we can say so.
    key_contains_severity: bool = False

    @property
    def severities(self) -> list[str]:
        return [m.severity for m in self.members if m.severity]

    @property
    def is_resolved(self) -> bool:
        return self.status == "resolved"

    @property
    def fingerprints(self) -> list[str]:
        return [m.fingerprint for m in self.members if m.fingerprint]


@dataclass(frozen=True)
class SeverityLadder:
    """Orders severities and escalates with a ceiling.

    An **unrecognised** severity ranks as the most severe. The asymmetry from
    [§41](../../TRADEOFFS.md#41-semantic-similarity-may-inform-it-may-never-suppress)
    decides it: a duplicate investigation costs tokens, a suppressed P1 costs an
    outage, so a severity we cannot order must never be suppressible. It is
    reported (`severity_unrecognised` on the decision) so the fail-open is counted
    rather than mistaken for a policy that worked.
    """

    order: tuple[str, ...] = DEFAULT_SEVERITY_ORDER

    def rank(self, severity: str) -> int:
        """Lower is more severe. An unknown severity ranks above everything."""
        try:
            return self.order.index(severity)
        except ValueError:
            return -1

    def known(self, severity: str) -> bool:
        return severity in self.order

    def worst(self, severities: Sequence[str]) -> str:
        """The most severe of a group's members.

        Read from `alerts[].labels.severity`, never from `commonLabels`: measured
        against a real AlertManager, a webhook whose members differ in severity
        omits `severity` from `commonLabels` entirely — so `commonLabels` goes
        blank in exactly the case R0 exists to catch.
        """
        present = [s for s in severities if s]
        if not present:
            return ""
        return min(present, key=self.rank)

    def escalate(self, severity: str) -> str:
        """One step more severe, capped at the top of the ladder.

        The ceiling is the point: P6a notes that unbounded `severity+1` on repeated
        recurrence eventually pages everyone, which is indistinguishable from having
        no severity at all.
        """
        if not self.known(severity):
            return severity  # cannot climb a ladder it is not on
        return self.order[max(0, self.rank(severity) - 1)]

    def is_higher(self, incoming: str, than: str) -> bool:
        if not incoming or not than:
            return False
        return self.rank(incoming) < self.rank(than)


@dataclass(frozen=True)
class DedupPolicy:
    """Thresholds. Defaults mirror `config/alerting.yaml`, which is authoritative.

    Defaults exist so `core/` stays runnable without config — the kernel must not
    require a file to be parseable, and a test asserting rule order should not have
    to know YAML.
    """

    ladder: SeverityLadder = field(default_factory=SeverityLadder)
    #: R2: a report this recently delivered makes a re-alert noise.
    repeat_suppress: timedelta = timedelta(minutes=5)
    #: R3: recurrence between `repeat_suppress` and here means the fix did not take.
    recurrence_window: timedelta = timedelta(minutes=10)
    #: R3: how many arrivals of this key before recurrence counts as recurrence
    #: rather than as a straggler.
    recurrence_min_arrivals: int = 3
    #: R4: sources whose alerts have *no* duration semantics of their own and
    #: therefore need burst aggregation from us. Every Prometheus rule in the mock
    #: stack has `for: 1m`, so `alertmanager` is deliberately absent.
    burst_sources: frozenset[str] = frozenset({"log_pattern"})
    #: R4: how long a hold buffer accumulates before it flushes.
    burst_window: timedelta = timedelta(minutes=2)
    #: R4: arrivals within `burst_window` that turn noise into a signal.
    burst_threshold: int = 3
    #: R4: severities low enough to be worth holding. A P1 is never held.
    burst_hold_severities: frozenset[str] = frozenset({"P3", "P4"})
    #: Storm cap: concurrent investigations past which further distinct keys are
    #: aggregated into one fleet-wide event instead of each forking a budget.
    storm_max_concurrent: int = 8


@dataclass(frozen=True)
class Decision:
    """What to do with one group, and **why** — the record, not just the verdict.

    [§39](../../TRADEOFFS.md#39-merging-is-advisory-not-destructive) requires every
    merge to carry its reason so the agent can disagree with it. A dedup decision is
    the same shape one lesson earlier: `reason` is written for a prompt and for a
    post-mortem, not for a log grep.
    """

    action: Action
    #: "R0".."R5", "resolved", or "storm".
    rule: str
    dedup_key: str
    reason: str
    #: The severity the investigation should run at — raised by R3, so it differs
    #: from the incoming severity exactly when escalation happened.
    severity: str = ""
    #: Set for `join`: which investigation to append to.
    investigation_id: str = ""
    #: Set when a *new* investigation supersedes or follows one at lower severity,
    #: so the two are linkable in the trace rather than looking unrelated.
    escalates_from: str = ""
    #: True when R3 or a burst aggregate raised the severity above the incoming one.
    escalated: bool = False
    #: The severity ladder did not recognise the incoming severity, so it was
    #: treated as most-severe and is unsuppressible. Counted, never silent.
    severity_unrecognised: bool = False
    key_derived: bool = False
    key_contains_severity: bool = False
    #: For `hold` and `aggregate`: how many arrivals this decision stands for.
    count: int = 1

    @property
    def suppressed(self) -> bool:
        return self.action in ("drop", "hold")

    @property
    def is_noteworthy(self) -> bool:
        """Whether the agent should be *told* about this decision.

        R5 and a plain R1 are bookkeeping — "this alert is new" tells an
        investigation nothing it cannot see in the alert. R0, R3, `resolved`, an
        aggregate and any escalation are different: each says something about the
        fault that the payload does not. *"The condition returned six minutes after
        its report"* changes what is worth investigating; padding every prompt with
        *"no prior investigation for this condition"* only costs cache-warm tokens.
        """
        return (
            self.escalated
            or self.action == "aggregate"
            or self.rule in ("R0", "R3", "resolved", "storm")
        )

    def as_dict(self) -> dict[str, Any]:
        """Flat form for a span attribute, a JSONL record, or an eval row."""
        return {
            "action": self.action,
            "rule": self.rule,
            "dedup_key": self.dedup_key,
            "reason": self.reason,
            "severity": self.severity,
            "investigation_id": self.investigation_id,
            "escalates_from": self.escalates_from,
            "escalated": self.escalated,
            "severity_unrecognised": self.severity_unrecognised,
            "key_derived": self.key_derived,
            "key_contains_severity": self.key_contains_severity,
            "count": self.count,
        }


@dataclass(frozen=True)
class HeldFlush:
    """A hold buffer that expired without reaching the burst threshold.

    P6a: *held alerts must be flushed and counted at window expiry rather than
    silently dropped.* Without this record the exit-table row "suppressed / held /
    escalated counts recorded, not silent" cannot be produced, and a threshold set
    too high looks like a quiet week.
    """

    dedup_key: str
    count: int
    severity: str
    first_seen: datetime
    expired_at: datetime


@dataclass
class _KeyState:
    """Per-key history. Everything the ordered rules read."""

    #: Firing arrivals **since the last report was delivered**, including ones R2
    #: dropped — those are exactly the evidence that the condition is recurring, which
    #: is why R3 counts arrivals rather than investigations.
    #:
    #: Reset on delivery, and that reset is load-bearing. A lifetime counter would sit
    #: permanently above R3's threshold after the third arrival ever, so every later
    #: recurrence would reopen escalated and "count >= 3" would stop meaning anything.
    #: Found by measuring a scripted sequence: R3 reported "5 arrivals" for what was
    #: the third arrival since the report.
    arrivals: int = 0
    delivered_at: datetime | None = None
    delivered_severity: str = ""
    in_flight_id: str = ""
    in_flight_severity: str = ""
    resolved_at: datetime | None = None
    #: R4's buffer: arrival times of held alerts, and the severity to flush with.
    held: list[datetime] = field(default_factory=list)
    held_severity: str = ""


@dataclass
class AlertLedger:
    """Per-key state, and the counters the exit table asks for.

    **In-memory, and a restart forgets suppression state.** That fails safe: an
    alert that would have been dropped creates an investigation instead, which costs
    one budget rather than an outage. L4a's per-investigation JSONL log holds
    everything needed to rebuild it if that ever stops being acceptable — noted
    rather than built, because building it now would be a persistence layer with no
    consumer.

    Lifecycle transitions are called by the harness (W2 L6), not by `decide`: a
    policy function that mutated the world could not be run twice on the same input,
    and `decide` is called in tests, in eval, and in a dry-run.
    """

    policy: DedupPolicy = field(default_factory=DedupPolicy)
    _keys: dict[str, _KeyState] = field(default_factory=dict, repr=False)
    #: rule -> how many decisions carried it. The observable half of dedup: a
    #: suppression that is not counted is indistinguishable from an alert that never
    #: arrived.
    counts: dict[str, int] = field(default_factory=dict)
    #: Buffers that expired because a *later arrival* pruned them rather than because
    #: the timer ran. Drained by `flush_expired`, so there is exactly one place a
    #: caller has to look and no held count is lost between timer ticks.
    pending_flushes: list[HeldFlush] = field(default_factory=list)
    #: The investigation currently standing in for a fleet-wide event, if any.
    storm_investigation_id: str = ""
    #: Which conditions that aggregate covers — the "N services affected" number.
    storm_keys: set[str] = field(default_factory=set)

    def state(self, dedup_key: str) -> _KeyState:
        return self._keys.setdefault(dedup_key, _KeyState())

    # ---- lifecycle, called by the harness --------------------------------- #

    def mark_in_flight(self, dedup_key: str, investigation_id: str, severity: str) -> None:
        st = self.state(dedup_key)
        st.in_flight_id = investigation_id
        st.in_flight_severity = severity

    def mark_storm(self, investigation_id: str) -> None:
        """Nominate one investigation as the fleet-wide aggregate.

        Set by the trigger when it acts on an `aggregate` decision from the storm
        rule. Without it the cap would change which *rule* fired and not the number of
        budgets spent, which is the only thing it exists to do.
        """
        self.storm_investigation_id = investigation_id

    def mark_delivered(self, dedup_key: str, at: datetime, severity: str = "") -> None:
        """A report went out. This is what arms R2 and R3, and disarms R3's counter."""
        st = self.state(dedup_key)
        previous = st.in_flight_id
        st.delivered_at = at
        st.delivered_severity = severity or st.in_flight_severity
        st.in_flight_id = ""
        st.in_flight_severity = ""
        st.arrivals = 0
        self._clear_storm_if(previous)

    def mark_finished(self, dedup_key: str) -> None:
        """An investigation ended without delivering. R1 must stop joining it."""
        st = self.state(dedup_key)
        previous = st.in_flight_id
        st.in_flight_id = ""
        st.in_flight_severity = ""
        self._clear_storm_if(previous)

    def _clear_storm_if(self, investigation_id: str) -> None:
        """Retire the fleet aggregate when the investigation holding it ends.

        Otherwise the next storm would join an investigation that is already reported,
        and the affected-condition count would keep growing across unrelated events.
        """
        if investigation_id and investigation_id == self.storm_investigation_id:
            self.storm_investigation_id = ""
            self.storm_keys.clear()

    @property
    def in_flight(self) -> int:
        return sum(1 for st in self._keys.values() if st.in_flight_id)

    # ---- held alerts ------------------------------------------------------- #

    def flush_expired(self, now: datetime) -> list[HeldFlush]:
        """Drop hold buffers whose window has passed, returning what was dropped.

        Called on a timer by the harness, and the single drain point: anything
        `decide` pruned since the last call comes out here too. Returning records
        rather than logging them keeps this module free of I/O and lets the caller
        decide whether an expired burst is a metric, a log line, or both.
        """
        out = list(self.pending_flushes)
        self.pending_flushes.clear()
        for key, st in self._keys.items():
            record = self._expire_held(key, st, now)
            if record is not None:
                out.append(record)
        return out

    def _expire_held(self, key: str, st: _KeyState, now: datetime) -> HeldFlush | None:
        """Expire one key's buffer if its window has closed.

        Called from `decide` as well as from the timer, because a buffer that only
        expires on someone else's schedule is a correctness bug: two alerts an hour
        apart with a two-minute window would otherwise sit in one buffer and let a
        third arrival aggregate three alerts that were never a burst.
        """
        if not st.held:
            return None
        first = min(st.held)
        if now - first < self.policy.burst_window:
            return None
        record = HeldFlush(
            dedup_key=key,
            count=len(st.held),
            severity=st.held_severity,
            first_seen=first,
            expired_at=now,
        )
        self._bump("flushed")
        st.held.clear()
        st.held_severity = ""
        return record

    def _bump(self, rule: str) -> None:
        self.counts[rule] = self.counts.get(rule, 0) + 1


def decide(group: AlertGroup, ledger: AlertLedger, now: datetime) -> Decision:
    """Apply P6a's ordered rules to one webhook. First match wins.

    Mutates only the arrival bookkeeping the rules themselves need — the arrival
    count, the hold buffer, the set of conditions a storm covers. Never the in-flight
    or delivered state, which the harness owns.

    `resolved` is handled *before* R0 and is not a suppression rule: a resolved
    webhook is a state transition, and self-healing mid-investigation is diagnostic
    information rather than noise. It may never create an investigation.
    """
    policy = ledger.policy
    ladder = policy.ladder
    st = ledger.state(group.dedup_key)

    incoming = ladder.worst(group.severities)
    unrecognised = bool(incoming) and not ladder.known(incoming)
    flags = {
        "dedup_key": group.dedup_key,
        "severity_unrecognised": unrecognised,
        "key_derived": group.key_derived,
        "key_contains_severity": group.key_contains_severity,
    }

    def made(action: Action, rule: str, reason: str, **kw: Any) -> Decision:
        ledger._bump(rule)
        return Decision(action=action, rule=rule, reason=reason, **flags, **kw)

    # ---- pre-rule: resolved ------------------------------------------------ #
    if group.is_resolved:
        st.resolved_at = now
        held = len(st.held)
        st.held.clear()
        if st.in_flight_id:
            return made(
                "join",
                "resolved",
                f"the condition cleared while investigation {st.in_flight_id} was "
                f"in flight; self-healing is evidence about the fault, not noise",
                severity=st.in_flight_severity or incoming,
                investigation_id=st.in_flight_id,
            )
        return made(
            "drop",
            "resolved",
            "the condition cleared and nothing was investigating it"
            + (f"; {held} held alert(s) discarded with it" if held else ""),
            severity=incoming,
            count=max(1, held),
        )

    st.arrivals += 1

    # ---- R0: an escalation above what was already delivered ---------------- #
    # First, and the reason is asymmetry: dropping a P1 because a P2 report shipped
    # five minutes ago prolongs an outage, while an extra investigation costs one
    # budget. Note the condition is "higher than *delivered*" — an escalation
    # arriving while an investigation is still in flight is R1's case, where joining
    # and raising that investigation in place beats spending a second budget on the
    # same condition.
    if st.delivered_at is not None and ladder.is_higher(incoming, st.delivered_severity):
        return made(
            "new",
            "R0",
            f"severity rose to {incoming} after a {st.delivered_severity} report was "
            f"delivered; an escalation is never suppressed",
            severity=incoming,
            escalates_from=st.delivered_severity,
        )

    # ---- R1: an investigation is already running on this key --------------- #
    if st.in_flight_id:
        if ladder.is_higher(incoming, st.in_flight_severity):
            # Escalate in place. The loop re-reads `inv.budget` every iteration, so
            # raising the tier here takes effect on the next turn — one condition
            # still produces one report, at the higher budget it now deserves.
            previous = st.in_flight_severity
            st.in_flight_severity = incoming
            return made(
                "join",
                "R1",
                f"already investigating this condition as {previous}; severity rose "
                f"to {incoming}, so the running investigation is escalated in place "
                f"rather than forked",
                severity=incoming,
                investigation_id=st.in_flight_id,
                escalates_from=previous,
                escalated=True,
            )
        return made(
            "join",
            "R1",
            f"investigation {st.in_flight_id} is already running on this condition",
            severity=st.in_flight_severity or incoming,
            investigation_id=st.in_flight_id,
        )

    # ---- R2 / R3: something was already delivered -------------------------- #
    # A *negative* elapsed time means the ledger holds a delivery timestamp in the
    # future — a replayed sequence with scripted times, or a clock that stepped. Both
    # suppression rules are skipped rather than applied to nonsense: "a report was
    # delivered -180s ago" was a real reason string this produced, and it would have
    # dropped the alert. Same fail-open direction as an unorderable severity.
    if st.delivered_at is not None and now >= st.delivered_at:
        since = now - st.delivered_at
        if since < policy.repeat_suppress:
            return made(
                "drop",
                "R2",
                f"a report for this condition was delivered "
                f"{_minutes(since)} ago; re-alerting only pages twice",
                severity=incoming,
            )
        if (
            since < policy.recurrence_window
            and st.arrivals >= policy.recurrence_min_arrivals
        ):
            raised = ladder.escalate(incoming)
            return made(
                "new",
                "R3",
                f"the condition returned {_minutes(since)} after its report "
                f"({st.arrivals} arrivals since that report): the fix did not take, "
                f"so it reopens at {raised}",
                severity=raised,
                escalates_from=incoming,
                escalated=raised != incoming,
            )

    # ---- R4: burst aggregation, for sources with no `for:` of their own ----- #
    if (
        group.source in policy.burst_sources
        and incoming in policy.burst_hold_severities
    ):
        stale = ledger._expire_held(group.dedup_key, st, now)
        if stale is not None:
            ledger.pending_flushes.append(stale)
        st.held.append(now)
        st.held_severity = incoming
        held = len(st.held)
        if held < policy.burst_threshold:
            return made(
                "hold",
                "R4",
                f"{held} of {policy.burst_threshold} within "
                f"{_minutes(policy.burst_window)}: one {incoming} is noise, N in a "
                f"window is a signal",
                severity=incoming,
                count=held,
            )
        raised = ladder.escalate(incoming)
        st.held.clear()
        st.held_severity = ""
        return made(
            "aggregate",
            "R4",
            f"{held} {incoming} alerts within {_minutes(policy.burst_window)} "
            f"aggregate into one {raised} event — the aggregate is more severe than "
            f"any member",
            severity=raised,
            escalated=raised != incoming,
            count=held,
        )

    # ---- storm cap: protect the budget, not the pager ---------------------- #
    # Checked after every rule that could have joined or suppressed, so a storm can
    # never make us drop an escalation or fork what is already being investigated.
    # This is the budget-layer cap; L4c's correlation cap (stop scoring O(n²) pairs)
    # reads the same counter.
    #
    # The first excess condition **creates** one fleet-wide investigation and every
    # later one **joins** it. Returning `aggregate` for all of them was the first
    # implementation and it did not cap anything: forty conditions still produced forty
    # investigations, and the only thing the cap changed was which rule name appeared
    # in the record. Caught by measuring created-vs-delivered rather than by a test,
    # because every rule was individually correct.
    if ledger.in_flight >= policy.storm_max_concurrent:
        ledger.storm_keys.add(group.dedup_key)
        affected = len(ledger.storm_keys)
        shared = (
            f"{ledger.in_flight} investigations already in flight "
            f"(cap {policy.storm_max_concurrent}): this is a fleet-wide event, so "
            f"further conditions are aggregated instead of each spending a budget"
        )
        if ledger.storm_investigation_id:
            return made(
                "join",
                "storm",
                f"{shared}. {affected} conditions now belong to this event",
                severity=incoming,
                investigation_id=ledger.storm_investigation_id,
                count=affected,
            )
        return made(
            "aggregate",
            "storm",
            f"{shared}. This investigation stands in for all of them",
            severity=incoming,
            count=affected,
        )

    return made("new", "R5", "no prior investigation for this condition", severity=incoming)


def _minutes(delta: timedelta) -> str:
    """Durations as prose, because these reasons are read by a model and a human.

    Seconds below a minute: "delivered 0 minutes ago" reads like a bug in the
    sentence that is supposed to justify a suppression.
    """
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    rest = seconds % 60
    return f"{minutes}m" if rest == 0 else f"{minutes}m{rest}s"


__all__ = [
    "Action",
    "AlertEvent",
    "AlertGroup",
    "AlertLedger",
    "Decision",
    "DedupPolicy",
    "HeldFlush",
    "SeverityLadder",
    "DEFAULT_SEVERITY_ORDER",
    "decide",
]
