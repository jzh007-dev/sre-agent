"""Sink registry — where a finished investigation goes.

Deliberately the same shape as `triggers/registry.py`: a protocol, a module-level
dict, `register` / `get` / `available`. Two registries solving the same problem in two
different styles is a codebase that has to be read twice.

**A sink reports whether it delivered.** That flag is the one non-obvious part of the
contract, and it is load-bearing twice over:

- `slack` and `jira` are stubs until W7 / W5, and a stub that returns success is
  indistinguishable from a working integration — the same failure class as the
  golden-set `_meta` leak, `prices_verified` on unchecked numbers, and the storm cap
  that renamed a rule while capping nothing.
- Harness ⑥ marks the alert ledger `delivered` **only** if some sink actually
  delivered, because dedup rule R2 suppresses five minutes of alerts on the strength
  of a delivered report. A sink that lied would turn a broken webhook into silent
  alert suppression. (That wiring lands in L6a-2; the flag it reads is here.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class Delivery:
    """What one sink did with one result.

    `detail` is written for a human reading a post-mortem — "printed to stdout",
    "would have posted to #sre-alerts (stub)" — not for a log grep.
    """

    sink: str
    delivered: bool
    detail: str = ""
    #: What the sink would have sent. Kept by stubs so W5/W7 have a shape to implement
    #: against, and so a test can assert content without a network.
    payload: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"sink": self.sink, "delivered": self.delivered, "detail": self.detail}


@runtime_checkable
class Sink(Protocol):
    """One destination for a finished investigation.

    Takes the outcome as a whole rather than just a report, because an **aborted**
    investigation is delivered too: "budget exhausted after 12 tool calls, here is
    what was established" is the message on-call needs most, and a sink that only
    handled success would drop exactly those.

    Synchronous, matching `EventSink` and `SpanSink`. A sink that needs the network
    (W7's Slack) does its blocking call in a thread rather than making every caller
    async — the harness must not be shaped by the slowest notifier.
    """

    name: str

    def deliver(
        self, report: Mapping[str, Any] | None, context: Mapping[str, Any]
    ) -> Delivery: ...


_REGISTRY: dict[str, Sink] = {}


def register(sink: Sink) -> Sink:
    """Install a sink, replacing any previous one of the same name.

    Replacing rather than refusing, for the reason `triggers.registry.register` gives:
    tests and eval swap in a recording sink, and a registry that raised would make
    every one of them reach into module state to undo itself.
    """
    _REGISTRY[sink.name] = sink
    return sink


def get(name: str) -> Sink:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown sink {name!r} (registered: {', '.join(sorted(_REGISTRY)) or 'none'})"
        ) from None


def available() -> list[str]:
    return sorted(_REGISTRY)


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def install_defaults() -> list[str]:
    """Register the built-in sinks. Imported inside, as the trigger registry does.

    `slack` and `jira` join in L6a-2 — registering a name whose implementation does
    not exist yet would make `available()` a promise rather than a fact.
    """
    from .stdout import StdoutSink

    register(StdoutSink())
    return available()


def resolve(names: Sequence[str]) -> list[Sink]:
    """Look up several sinks by name, skipping any that are not registered.

    Skipping rather than raising: a trigger's default binding names `slack`, and a
    deployment that never registered it should still deliver to the sinks it *does*
    have. The skip is not silent — `missing()` reports it, and harness ⑥ records the
    unresolved names alongside the deliveries it made.
    """
    return [_REGISTRY[name] for name in names if name in _REGISTRY]


def missing(names: Sequence[str]) -> list[str]:
    return [name for name in names if name not in _REGISTRY]


__all__ = [
    "Delivery",
    "Sink",
    "available",
    "get",
    "install_defaults",
    "missing",
    "register",
    "resolve",
    "unregister",
]
