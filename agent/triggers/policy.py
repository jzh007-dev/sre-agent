"""Config → policy objects. The only place in the trigger layer that reads a file.

`config/alerting.yaml` becomes a `DedupPolicy`; `config/budgets.yaml` becomes a
`BudgetTiers`. Both live here rather than in `agent/core/` because parsing YAML is
I/O against a concrete format, and the kernel takes policy as a parameter — the same
rule that keeps serialization in `agent/store/` instead of in `core/`.

Both loaders fall back to the dataclass defaults for anything the file omits, so a
config that is missing a key is a *partial* override rather than a crash. That is
the right direction for an incident-response system: a typo in a threshold should
not stop an alert being investigated.

Durations use Prometheus syntax (`5m`, `90s`, `1h`) because that is what an SRE
reads in every other duration in this stack, including the `for:` clauses in
`mock/prometheus/alerts.yml` that R4 is scoped around.
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping

import yaml

from ..core.dedup import DedupPolicy, SeverityLadder
from ..core.investigation import ToolBudget

#: Repo root, derived from this file rather than from the cwd: `srectl` and the eval
#: runner are both invoked from wherever the operator happens to be standing, and a
#: config file that only loads from one directory is a config file that will one day
#: silently fall back to defaults.
_REPO = pathlib.Path(__file__).resolve().parents[2]

ALERTING_PATH = _REPO / "config" / "alerting.yaml"
BUDGETS_PATH = _REPO / "config" / "budgets.yaml"

_DURATION = re.compile(r"^\s*(\d+)\s*(ms|s|m|h|d)\s*$")
_UNITS = {
    "ms": timedelta(milliseconds=1),
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
}


def parse_duration(value: Any, default: timedelta) -> timedelta:
    """Prometheus-style duration, or `default` if the value is absent or malformed.

    A bare number is read as seconds — the one ambiguity worth resolving explicitly,
    since `5` in a file full of `5m` almost certainly means seconds to whoever wrote
    the parser and minutes to whoever wrote the file. Seconds is the SI-ish reading
    and it errs *shorter*, so a mistyped suppression window suppresses less.
    """
    if value is None:
        return default
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(seconds=float(value))
    match = _DURATION.match(str(value))
    if match is None:
        return default
    return int(match.group(1)) * _UNITS[match.group(2)]


def load_alerting(path: str | pathlib.Path = ALERTING_PATH) -> DedupPolicy:
    """Read `config/alerting.yaml` into a `DedupPolicy`."""
    raw = _read(path)
    defaults = DedupPolicy()

    order = _str_list(_dig(raw, "severity", "order"))
    ladder = SeverityLadder(order=tuple(order)) if order else defaults.ladder

    dedup = _mapping(raw.get("dedup"))
    burst = _mapping(raw.get("burst"))
    storm = _mapping(raw.get("storm"))

    return DedupPolicy(
        ladder=ladder,
        repeat_suppress=parse_duration(
            dedup.get("repeat_suppress"), defaults.repeat_suppress
        ),
        recurrence_window=parse_duration(
            dedup.get("recurrence_window"), defaults.recurrence_window
        ),
        recurrence_min_arrivals=_int(
            dedup.get("recurrence_min_arrivals"), defaults.recurrence_min_arrivals
        ),
        burst_sources=frozenset(_str_list(burst.get("sources")) or defaults.burst_sources),
        burst_window=parse_duration(burst.get("window"), defaults.burst_window),
        burst_threshold=_int(burst.get("threshold"), defaults.burst_threshold),
        burst_hold_severities=frozenset(
            _str_list(burst.get("hold_severities")) or defaults.burst_hold_severities
        ),
        storm_max_concurrent=_int(
            storm.get("max_concurrent"), defaults.storm_max_concurrent
        ),
    )


@dataclass(frozen=True)
class BudgetTiers:
    """Severity → `ToolBudget`, plus the per-trigger budgets for chat and patrol."""

    tiers: Mapping[str, ToolBudget] = field(default_factory=dict)
    triggers: Mapping[str, ToolBudget] = field(default_factory=dict)
    #: Which tier an unrecognised or missing severity gets. Deliberately not the top
    #: tier — see the note in `config/budgets.yaml`.
    default_severity: str = "P2"

    def for_severity(self, severity: str) -> ToolBudget:
        budget = self.tiers.get(severity)
        if budget is not None:
            return budget
        return self.tiers.get(self.default_severity) or ToolBudget()

    def for_trigger(self, trigger: str) -> ToolBudget:
        return self.triggers.get(trigger) or ToolBudget()


def load_budgets(path: str | pathlib.Path = BUDGETS_PATH) -> BudgetTiers:
    """Read `config/budgets.yaml` into a `BudgetTiers`."""
    raw = _read(path)
    return BudgetTiers(
        tiers={
            name: _budget(spec)
            for name, spec in _mapping(raw.get("tiers")).items()
        },
        triggers={
            name: _budget(spec)
            for name, spec in _mapping(raw.get("triggers")).items()
        },
        default_severity=str(raw.get("default") or "P2"),
    )


def _budget(spec: Any) -> ToolBudget:
    """One tier. Absent fields keep `ToolBudget`'s own defaults.

    `max_cost` is merged into the defaults rather than replacing them: a tier that
    sets only USD would otherwise leave CNY *unset*, and an unset currency raises at
    the gate — so a partial override would turn every DeepSeek call into an error.
    """
    fields = _mapping(spec)
    base = ToolBudget()
    costs = dict(base.max_cost)
    costs.update(
        {
            str(currency): float(amount)
            for currency, amount in _mapping(fields.get("max_cost")).items()
        }
    )
    return ToolBudget(
        max_turns=_int(fields.get("max_turns"), base.max_turns),
        max_tool_calls=_int(fields.get("max_tool_calls"), base.max_tool_calls),
        max_cost=costs,
        per_tool_calls=dict(_mapping(fields.get("per_tool_calls"))),
        repeat_tool_calls=_int(
            fields.get("repeat_tool_calls"), base.repeat_tool_calls
        ),
    )


# --------------------------------------------------------------------------- #
# Readers. Deliberately total: a missing file, an empty file, or a scalar where a
# mapping was expected all read as "no override", because the fallback is a working
# default rather than a broken one.
# --------------------------------------------------------------------------- #


def _read(path: str | pathlib.Path) -> dict[str, Any]:
    file = pathlib.Path(path)
    if not file.exists():
        return {}
    loaded = yaml.safe_load(file.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _dig(raw: Mapping[str, Any], *keys: str) -> Any:
    node: Any = raw
    for key in keys:
        node = _mapping(node).get(key)
    return node


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


def _int(value: Any, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


__all__ = [
    "ALERTING_PATH",
    "BUDGETS_PATH",
    "BudgetTiers",
    "load_alerting",
    "load_budgets",
    "parse_duration",
]
