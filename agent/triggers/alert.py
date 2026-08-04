"""Alert trigger — webhook → `AlertGroup` → dedup decision → `Investigation`.

The real one of the three entry modes, and the only one with a dedup layer: a human
asking the same question twice is asking twice, but an alert firing twice is usually
the same outage.

**What the payload shape actually is** — measured against `prom/alertmanager:v0.27.0`,
the version `mock/docker-compose.yml` pins, by pointing a real AlertManager at a
webhook receiver and posting one condition at P2 and then at P1:

| Field | What was observed | Consequence here |
|---|---|---|
| `alerts[].fingerprint` | **changes with severity** — `327b605fce1b794f` at P2, `3277605fce179078` at P1 | cannot be the dedup key; `Alert.Fingerprint()` hashes every label |
| `groupKey` | `{}:{alertname="HighErrorRate", service="auth"}` — **no severity** | this is the dedup key |
| grouping | both severities arrived in **one** webhook | AlertManager already treats them as one condition |
| `commonLabels` | `severity` **absent** in that webhook | severity must be read per member |
| resolved | `status: "resolved"`, same `groupKey`, member carries `endsAt` | `resolved` is a state transition on the same key |

That last row of the third column is the trap worth naming: reading severity from
`commonLabels` works on every single-severity webhook and returns nothing on a
mixed-severity one — so it fails precisely when an escalation happens, which is the
one case dedup rule R0 exists for. `parse_webhook` reads
`alerts[].labels.severity` and takes the worst.

Two payload shapes are accepted. A **real webhook** (`alerts[]`, `groupKey`,
`status`), and the **Week-1 simplified fixture** in `eval/golden/*/alert.json`, which
predates this and carries none of them. For the fixture the group key is derived from
`commonLabels` minus `severity` and marked `key_derived`, and the member's
`fingerprint` is left **empty rather than invented** — a fabricated identifier that
looks like AlertManager's is exactly the second, divergent definition
[TRADEOFFS §38](../../TRADEOFFS.md#38-alert-processing-alertmanager-owns-the-notification-layer-we-own-the-investigation-layer)
says not to create. W2 L4c reshapes the fixtures, at which point the derived path
stops being exercised by them and stays only for sources that genuinely send no
group key.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from ..core.dedup import (
    AlertEvent,
    AlertGroup,
    AlertLedger,
    Decision,
    DedupPolicy,
    decide,
)
from ..core.investigation import Investigation, ToolBudget, Window, mint_id
from .policy import BudgetTiers, load_alerting, load_budgets
from .registry import TriggerOutcome

#: What a payload with no `alert_source` is assumed to have come from. The Week-1
#: fixtures carry the field explicitly; real AlertManager webhooks do not carry any
#: equivalent, and this is the only source wired.
DEFAULT_SOURCE = "alertmanager"

#: Labels excluded when a group key has to be derived. `severity` is the whole point
#: (see the module docstring); a production deployment would also need to exclude
#: volatile labels like `instance` or `pod`, and does not have to, because the normal
#: path uses `groupKey` and the operator already chose those labels in `group_by`.
DERIVED_KEY_EXCLUDES = frozenset({"severity"})

#: Marks a key as ours rather than AlertManager's, in the key itself. A grep across
#: logs for `derived:` answers "how often are we guessing?" with no extra field.
DERIVED_KEY_PREFIX = "derived:"

#: How many absorbed conditions a fleet-wide aggregate lists in its prompt before it
#: stops naming them. Not a config knob: it is a context-window guard, not a policy.
STORM_NOTE_LIMIT = 20


def strip_fixture_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Drop keys beginning with `_` before a payload can reach the model.

    Not cosmetic. Every golden case's `alert.json` carries a `_meta.purpose` written
    for human readers, and those descriptions *state the root cause* ("Redis bgsave
    failed under memory pressure, session writes rejected"). Passing the payload
    through verbatim would hand the agent the answer and silently invalidate every
    accuracy number the eval suite produces — a failure that looks like success,
    which is the worst kind.

    The rule is structural rather than a `_meta` special case: anything
    underscore-prefixed is fixture bookkeeping and never crosses into a prompt.

    Moved here from `core/investigation.py` in W2 L4b. It guards *alert payloads*,
    and the kernel should not know what an alert payload looks like.
    """
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def derive_dedup_key(labels: Mapping[str, str]) -> str:
    """A group key for a source that sends none.

    Deliberately formatted like AlertManager's own so the two are diffable by eye,
    and deliberately prefixed so they are never confused.
    """
    kept = sorted(
        (k, v) for k, v in labels.items() if k not in DERIVED_KEY_EXCLUDES
    )
    inner = ", ".join(f'{k}="{v}"' for k, v in kept)
    return f"{DERIVED_KEY_PREFIX}{{{inner}}}"


def parse_webhook(
    payload: Mapping[str, Any], *, source: str | None = None
) -> AlertGroup:
    """Normalise either payload shape into one `AlertGroup`.

    One webhook is one group and therefore one investigation — §38's resolution of
    "two systems have opinions about grouping": AlertManager may batch four alerts
    into one delivery and we accept that batch as one diagnostic unit. Correlation
    *across* deliveries is W2 L4c.
    """
    common_labels = _str_map(payload.get("commonLabels"))
    common_annotations = _str_map(payload.get("commonAnnotations"))
    members = _members(payload, common_labels, common_annotations)

    group_key = str(payload.get("groupKey") or "").strip()
    derived = not group_key
    if derived:
        group_key = derive_dedup_key(common_labels or _labels_union(members))

    return AlertGroup(
        dedup_key=group_key,
        status=str(payload.get("status") or _status_of(members)),
        members=members,
        source=str(payload.get("alert_source") or source or DEFAULT_SOURCE),
        correlation_id=_correlation_id(common_annotations, members),
        t0=_earliest_start(members) or _parse_time(payload.get("startsAt")),
        key_derived=derived,
        # Only a real group key can contain severity, since the derived one excludes
        # it by construction. If it does, the operator put `severity` in AlertManager's
        # `group_by`, an escalation will arrive under a *different* key, and R0/R3
        # cannot see it. Their config is not ours to read — but the key is a string.
        key_contains_severity=not derived and "severity=" in group_key,
    )


def investigation_from_payload(
    payload: Mapping[str, Any],
    *,
    t0: datetime | None = None,
    budget: ToolBudget | None = None,
    integration: str | None = None,
) -> Investigation:
    """Build an alert-triggered investigation from a payload, with no dedup.

    Relocated from `Investigation.from_alert` in W2 L4b, as that method's own
    docstring said it would be. Kept as a function because it is the one-shot path —
    `srectl replay`, a test, an eval case that has already decided the alert is
    worth investigating — while `AlertTrigger.preprocess` is the path that decides.

    `t0` is the incident's start, not the current time: the window derives from when
    the alert fired. Real payloads carry `startsAt`; the Week-1 fixtures are static
    and have none, so callers pass `t0` explicitly and the wall clock is a last
    resort.
    """
    group = parse_webhook(payload)
    return _build(
        group,
        payload,
        severity=_worst(group),
        budget=budget or ToolBudget(),
        integration=integration,
        t0=t0,
        note="",
    )


class AlertTrigger:
    """Entry mode ①: an AlertManager webhook.

    Holds the dedup ledger, so it is stateful by design — R1-R4 are all questions
    about what happened before. The `store` is where investigations it created can be
    found again for a join; W2 L7's ingress passes its own registry in, and the
    default is a plain dict so this is usable and testable now.
    """

    kind = "alert"

    def __init__(
        self,
        *,
        policy: DedupPolicy | None = None,
        budgets: BudgetTiers | None = None,
        ledger: AlertLedger | None = None,
        store: MutableMapping[str, Investigation] | None = None,
        now: Callable[[], datetime] | None = None,
        integration: str | None = None,
    ) -> None:
        self.ledger = ledger or AlertLedger(policy=policy or load_alerting())
        # One policy, not two: `decide` reads the ledger's, so a caller that passed
        # both a ledger and a policy would otherwise get thresholds from one and
        # tiering from the other.
        self.policy = self.ledger.policy
        self.budgets = budgets or load_budgets()
        self.store: MutableMapping[str, Investigation] = {} if store is None else store
        self._now = now or _utcnow
        self.integration = integration

    def preprocess(self, payload: Mapping[str, Any]) -> TriggerOutcome:
        """One webhook in, zero or one investigation out, always with a reason."""
        now = self._now()
        group = parse_webhook(payload)
        decision = decide(group, self.ledger, now)

        # Drains buffers this delivery pruned *and* any whose window closed while
        # nothing was arriving, so held counts are reported without a timer having to
        # exist yet. O(keys) per delivery, which the storm cap bounds in the only
        # scenario where key count grows fast.
        outcome = TriggerOutcome(
            decisions=[decision], flushes=self.ledger.flush_expired(now)
        )

        if decision.action == "join":
            joined = self.store.get(decision.investigation_id)
            if joined is not None:
                self._absorb(joined, group, decision)
                outcome.joined.append(joined)
                return outcome
            # The investigation the ledger points at is gone — a restart, or a store
            # that never held it. Failing safe means investigating, not dropping: the
            # decision is rewritten so the record says what actually happened rather
            # than claiming a join that did not occur.
            decision = dataclasses.replace(
                decision,
                action="new",
                reason=(
                    f"{decision.reason} — but investigation "
                    f"{decision.investigation_id} is no longer held in memory, so a "
                    f"new one is opened rather than the alert being lost"
                ),
                investigation_id="",
                escalates_from=decision.investigation_id,
            )
            outcome.decisions = [decision]

        if decision.action in ("new", "aggregate"):
            inv = _build(
                group,
                payload,
                severity=decision.severity,
                budget=self.budgets.for_severity(decision.severity),
                integration=self.integration,
                t0=None,
                note=_note(decision) if decision.is_noteworthy else "",
            )
            self.store[inv.id] = inv
            self.ledger.mark_in_flight(group.dedup_key, inv.id, decision.severity)
            if decision.rule == "storm":
                # This one stands in for every further condition the cap catches, so
                # the ledger has to know which investigation that is — otherwise the
                # next excess condition opens a second aggregate and the cap caps
                # nothing.
                self.ledger.mark_storm(inv.id)
            outcome.investigations.append(inv)

        return outcome

    def _absorb(
        self, inv: Investigation, group: AlertGroup, decision: Decision
    ) -> None:
        """Fold a repeat or a resolution into the investigation already running.

        A compact note, not the whole payload again: what the running investigation
        learns is *that* the condition fired again or cleared, and re-dumping several
        kilobytes of JSON mid-conversation would spend context to say it.
        """
        if decision.rule == "storm" and decision.count > STORM_NOTE_LIMIT:
            # A fleet-wide event has no bound on how many conditions it absorbs, and
            # one line each would eventually be the whole context window — in the one
            # investigation that must not abort. The *count* is never lost: it is on
            # every decision record and therefore in the JSONL log. What is bounded is
            # only what the model is shown.
            if decision.count == STORM_NOTE_LIMIT + 1:
                inv.add_user_text(
                    f'<alert-update rule="storm">\n'
                    f"More than {STORM_NOTE_LIMIT} conditions now belong to this "
                    f"fleet-wide event; further ones will not be listed here. Treat "
                    f"this as infrastructure-wide rather than service-specific.\n"
                    f"</alert-update>"
                )
            return
        inv.add_user_text(_join_note(group, decision))
        if decision.escalated:
            # R1's escalate-in-place. `loop.run` re-reads `inv.budget` on every
            # iteration, so a bigger ceiling takes effect on the next turn — the
            # condition gets the budget its new severity deserves without a second
            # investigation being opened against it.
            inv.budget = self.budgets.for_severity(decision.severity)


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def _build(
    group: AlertGroup,
    payload: Mapping[str, Any],
    *,
    severity: str,
    budget: ToolBudget,
    integration: str | None,
    t0: datetime | None,
    note: str,
) -> Investigation:
    anchor = t0 or group.t0 or _utcnow()
    inv = Investigation(
        id=mint_id(),
        trigger="alert",
        window=Window.around(anchor),
        budget=budget,
        integration=integration,
        correlation_id=group.correlation_id,
    )
    body = json.dumps(
        strip_fixture_metadata(payload), ensure_ascii=False, indent=2
    )
    inv.add_user_text(f"<alert>\n{body}\n</alert>")
    if note:
        inv.add_user_text(note)
    return inv


def _note(decision: Decision) -> str:
    """The dedup decision, handed to the agent as evidence it may argue with.

    Same shape [§39](../../TRADEOFFS.md#39-merging-is-advisory-not-destructive)
    requires of a correlation merge, one lesson earlier: the rule, the reason, and
    permission to disagree. A dedup decision that raised the severity or reopened a
    closed condition is a fact about the fault, not bookkeeping.
    """
    lines = [
        f'<dedup rule="{decision.rule}" action="{decision.action}">',
        decision.reason + ".",
    ]
    if decision.escalated and decision.escalates_from:
        lines.append(
            f"Severity was raised from {decision.escalates_from} to "
            f"{decision.severity} by this rule, not by the alert."
        )
    if decision.severity_unrecognised:
        lines.append(
            "The incoming severity is not on the configured ladder, so it was "
            "treated as the most severe and could not be suppressed. Read it as a "
            "possible mislabelled alert rule."
        )
    if decision.key_contains_severity:
        lines.append(
            "This group key contains a severity term, which means AlertManager is "
            "grouping by severity: an escalation of this same condition will arrive "
            "under a different key and will not be recognised as a recurrence."
        )
    lines.append(
        "This grouping is advisory. If the evidence says otherwise, say so in the "
        "report."
    )
    lines.append("</dedup>")
    return "\n".join(lines)


def _join_note(group: AlertGroup, decision: Decision) -> str:
    """What an in-flight investigation is told when another delivery lands on it."""
    if decision.rule == "storm":
        # One line, and it **names the condition**. The first version repeated the
        # whole "N investigations in flight (cap M), further conditions are
        # aggregated…" paragraph per absorbed condition — 5.9 KB across twenty of
        # them, and it never once said *which* services were affected, which is the
        # only thing a fleet-wide aggregate exists to know.
        return (
            f'<alert-update rule="storm">\n'
            f"Also affected: {group.dedup_key} at {decision.severity} "
            f"({decision.count} conditions in this fleet-wide event).\n"
            f"</alert-update>"
        )
    if decision.rule == "resolved":
        ends = _latest_end(group.members)
        when = f" at {ends.isoformat()}" if ends else ""
        return (
            f'<alert-update rule="resolved">\n'
            f"The condition you are investigating RESOLVED{when} — AlertManager "
            f"reports it is no longer firing. Self-healing is evidence about the "
            f"fault, not a reason to stop: a condition that cleared on its own "
            f"points at something transient or externally mitigated, and the report "
            f"should say which. Do not conclude the fault was fixed unless something "
            f"in the evidence fixed it.\n"
            f"</alert-update>"
        )
    lines = [
        f'<alert-update rule="{decision.rule}">',
        decision.reason + ".",
    ]
    summary = _summary(group)
    if summary:
        lines.append(f"Latest: {summary}")
    if decision.escalated:
        lines.append(
            f"Severity is now {decision.severity} (was {decision.escalates_from}); "
            f"this investigation's budget was raised in place rather than a second "
            f"investigation being opened."
        )
    lines.append("</alert-update>")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Payload readers. Total by design: a malformed field reads as absent, because an
# alert that arrives slightly wrong still has to be investigated.
# --------------------------------------------------------------------------- #


def _members(
    payload: Mapping[str, Any],
    common_labels: Mapping[str, str],
    common_annotations: Mapping[str, str],
) -> list[AlertEvent]:
    raw = payload.get("alerts")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and raw:
        return [
            AlertEvent(
                fingerprint=str(item.get("fingerprint") or ""),
                labels=_str_map(item.get("labels")) or dict(common_labels),
                annotations=_str_map(item.get("annotations")) or dict(common_annotations),
                starts_at=_parse_time(item.get("startsAt")),
                ends_at=_parse_time(item.get("endsAt")),
                status=str(item.get("status") or "firing"),
            )
            for item in raw
            if isinstance(item, Mapping)
        ]

    # Week-1 simplified fixture: one alert, flat. `fingerprint` stays empty rather
    # than being invented — see the module docstring.
    labels = dict(common_labels)
    if "severity" not in labels and isinstance(payload.get("severity"), str):
        labels["severity"] = payload["severity"]
    if "alertname" not in labels and isinstance(payload.get("alert_name"), str):
        labels["alertname"] = payload["alert_name"]
    return [
        AlertEvent(
            fingerprint="",
            labels=labels,
            annotations=dict(common_annotations),
            starts_at=_parse_time(payload.get("startsAt")),
            ends_at=_parse_time(payload.get("endsAt")),
            status=str(payload.get("status") or "firing"),
        )
    ]


def _status_of(members: Sequence[AlertEvent]) -> str:
    """A group is resolved only when every member is.

    Matches AlertManager, which sets the group status the same way. Getting this
    backwards would let one recovered member cancel an ongoing outage.
    """
    if members and all(m.status == "resolved" for m in members):
        return "resolved"
    return "firing"


def _labels_union(members: Sequence[AlertEvent]) -> dict[str, str]:
    """Labels shared by every member — the stand-in for `commonLabels`."""
    if not members:
        return {}
    shared = dict(members[0].labels)
    for member in members[1:]:
        shared = {k: v for k, v in shared.items() if member.labels.get(k) == v}
    return shared


def _correlation_id(
    annotations: Mapping[str, str], members: Sequence[AlertEvent]
) -> str:
    """The alert's own id for this incident, from `commonAnnotations`.

    Read from the annotation rather than recomputed, for the same reason the group
    key comes from AlertManager: a second definition of an identifier is a second
    thing that can disagree. Every Week-1 fixture carries one, and
    `mock/services/_shared/observability.py` propagates the same id by header, which
    is what makes the trace join possible at all.

    Empty when absent — the trace still works, it just cannot be joined to the
    observed system's logs, which is worth reading as a missing annotation on the
    alert rule rather than as an agent-side failure.
    """
    value = annotations.get("correlation_id", "")
    if value:
        return value
    for member in members:
        found = member.annotations.get("correlation_id", "")
        if found:
            return found
    return ""


def _summary(group: AlertGroup) -> str:
    for member in group.members:
        text = member.annotations.get("summary") or member.annotations.get("description")
        if text:
            return text
    return ""


def _worst(group: AlertGroup) -> str:
    """The group's severity, using the default ladder.

    Used only by the no-dedup path, which has no policy in hand.
    """
    return DedupPolicy().ladder.worst(group.severities)


def _earliest_start(members: Sequence[AlertEvent]) -> datetime | None:
    starts = [m.starts_at for m in members if m.starts_at is not None]
    return min(starts) if starts else None


def _latest_end(members: Sequence[AlertEvent]) -> datetime | None:
    ends = [m.ends_at for m in members if m.ends_at is not None]
    return max(ends) if ends else None


def _parse_time(raw: Any) -> datetime | None:
    """Parse an RFC-3339 timestamp, treating Go's zero time as absent.

    A firing alert's `endsAt` is literally `"0001-01-01T00:00:00Z"` — measured, and it
    parses cleanly into a year-1 `datetime`. Taken at face value it is a timestamp
    from two thousand years ago, which would sort ahead of everything in any
    min/max over times and read as "this alert ended long before it started".
    """
    if isinstance(raw, datetime):
        return raw if raw.year > 1 else None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.year > 1 else None


def _str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "AlertTrigger",
    "DEFAULT_SOURCE",
    "derive_dedup_key",
    "investigation_from_payload",
    "parse_webhook",
    "strip_fixture_metadata",
]
