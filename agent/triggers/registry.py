"""Trigger registry — harness steps ① route and ② pre-process.

Alert is one entry mode of three ([TRADEOFFS §25](../../TRADEOFFS.md#25-trigger-registry-alert-is-one-entry-mode-of-three)).
Each trigger owns exactly one job: turn whatever arrived at the door — an
AlertManager webhook, a chat message, a patrol schedule — into `Investigation`
objects. Everything downstream (loadout, loop, parse, fanout) is identical across
all three, which is why the registry stops here and does not grow a second method.

The seam rule in one sentence: **adding a fourth trigger is a new module plus a
`register()` call, and `agent/core/` does not change.** `tests/triggers/test_registry.py`
asserts it by registering a fake trigger and running it end to end.

`TriggerOutcome` lives here rather than in `core/` because a trigger's *result* is a
seam concept — the harness may import this module (it is in
`HARNESS_EXTRA_ALLOWED`), the kernel may not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from ..core.dedup import Decision, HeldFlush
from ..core.investigation import Investigation, TriggerKind


@dataclass
class TriggerOutcome:
    """What pre-processing produced from one delivery.

    Zero investigations is a normal, common result — a duplicate alert, a held
    burst, a `resolved` notification for something nobody is investigating. The
    `decisions` list is why it is not silent: an investigation that was *not*
    created is exactly as reportable as one that was, which is the exit-table row
    "suppressed / held / escalated counts recorded, not silent".
    """

    investigations: list[Investigation] = field(default_factory=list)
    #: Investigations that absorbed this delivery instead of a new one being made.
    joined: list[Investigation] = field(default_factory=list)
    #: One per delivery for alert; empty for chat and patrol, which have no dedup
    #: layer (a human asking the same question twice is asking twice).
    decisions: list[Decision] = field(default_factory=list)
    #: Hold buffers that expired during this delivery — see `AlertLedger`.
    flushes: list[HeldFlush] = field(default_factory=list)

    @property
    def created(self) -> int:
        return len(self.investigations)

    def summary(self) -> dict[str, Any]:
        """Counts for a span attribute or an eval row."""
        return {
            "created": len(self.investigations),
            "joined": len(self.joined),
            "rules": [d.rule for d in self.decisions],
            "actions": [d.action for d in self.decisions],
            "flushed": sum(f.count for f in self.flushes),
        }


@runtime_checkable
class Trigger(Protocol):
    """One entry mode.

    Deliberately narrow. A trigger may not run the loop, choose tools, or write to a
    sink — those are steps ③④⑥ and they are shared. What varies between alert, chat
    and patrol is only *what an investigation is made of*.
    """

    kind: TriggerKind

    def preprocess(self, payload: Mapping[str, Any]) -> TriggerOutcome: ...


_REGISTRY: dict[str, Trigger] = {}


def register(trigger: Trigger) -> Trigger:
    """Install a trigger, replacing any previous one of the same kind.

    Replacing rather than refusing: eval and tests both swap in a trigger with an
    injected clock and ledger, and a registry that raised on re-registration would
    force every test to reach into module state to undo itself.
    """
    _REGISTRY[trigger.kind] = trigger
    return trigger


def get(kind: str) -> Trigger:
    """The trigger for one entry mode.

    Raises `KeyError` listing the registered kinds — the failure mode here is a typo
    in a route, and "unknown trigger 'alerts' (registered: alert, chat, patrol)" is
    the difference between a one-second fix and a debugging session.
    """
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise KeyError(
            f"unknown trigger {kind!r} "
            f"(registered: {', '.join(sorted(_REGISTRY)) or 'none'})"
        ) from None


def available() -> list[str]:
    return sorted(_REGISTRY)


def unregister(kind: str) -> None:
    """Remove a trigger. Used by tests that register a fake one."""
    _REGISTRY.pop(kind, None)


def install_defaults() -> list[str]:
    """Register the three built-in triggers.

    Imported inside the function, so importing the registry does not drag in every
    trigger implementation — which is what keeps `available()` meaningful for a
    caller that registered its own instead.
    """
    from .alert import AlertTrigger
    from .chat import ChatTrigger
    from .patrol import PatrolTrigger

    for trigger in (AlertTrigger(), ChatTrigger(), PatrolTrigger()):
        register(trigger)
    return available()


def dispatch(kind: str, payload: Mapping[str, Any]) -> TriggerOutcome:
    """Route one delivery to its trigger — harness steps ① and ② in one call."""
    return get(kind).preprocess(payload)


def summarise(outcomes: Iterable[TriggerOutcome]) -> dict[str, Any]:
    """Aggregate counts over a sequence of deliveries.

    Exists because the numbers the exit table asks for (alerts in → investigations
    created, per rule) are properties of a *sequence*, not of one webhook.
    """
    collected: list[TriggerOutcome] = list(outcomes)
    rules: dict[str, int] = {}
    actions: dict[str, int] = {}
    for outcome in collected:
        for decision in outcome.decisions:
            rules[decision.rule] = rules.get(decision.rule, 0) + 1
            actions[decision.action] = actions.get(decision.action, 0) + 1
    return {
        "deliveries": len(collected),
        "created": sum(o.created for o in collected),
        "joined": sum(len(o.joined) for o in collected),
        "flushed": sum(f.count for o in collected for f in o.flushes),
        "by_rule": rules,
        "by_action": actions,
    }


def registered() -> Sequence[Trigger]:
    return tuple(_REGISTRY[k] for k in sorted(_REGISTRY))


__all__ = [
    "Trigger",
    "TriggerOutcome",
    "available",
    "dispatch",
    "get",
    "install_defaults",
    "register",
    "registered",
    "summarise",
    "unregister",
]
