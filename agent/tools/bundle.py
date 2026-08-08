"""Tool bundle assembly — which tools an investigation may call.

Thin on purpose. Its whole job in W2 L6a is to be **the one place that names a concrete
tool**, so `agent/core/harness.py` does not have to: `tests/test_architecture.py`
allows the harness to import `agent.tools.bundle` and forbids `agent.tools.stubs`, and
loadout has to get a tool dict from somewhere.

W2 L5b replaces `_default()`'s body with per-integration MCP assembly — an integration
declares its server in YAML and this resolves it — and **the harness does not change**.
That is the seam rule paying for itself one lesson after being written down rather than
in Week 5.

`verify()` is here rather than in the harness because a bundle's coherence is a property
of the bundle. It is called at loadout time, which makes a mis-wired tool set a startup
failure instead of an `Aborted("no_report")` fifteen turns and a whole budget later.
"""
from __future__ import annotations

from typing import Mapping

from .protocol import Tool


class BundleError(RuntimeError):
    """A tool set that cannot satisfy the investigation it was assembled for."""


def default_bundle() -> dict[str, Tool]:
    """The canned bundle. Returns a fresh dict, so a caller may narrow it safely."""
    from .stubs import default_tool_registry

    return dict(default_tool_registry())


def for_integration(name: str | None) -> dict[str, Tool]:
    """The bundle for one integration.

    Every integration gets the same canned set today. The parameter exists so the
    call site is already correct when L5b makes it mean something — a signature that
    has to change is a signature every caller has to revisit.
    """
    return default_bundle()


def verify(tools: Mapping[str, Tool], *, requires_report: bool) -> None:
    """Refuse a bundle that cannot end the investigation it is for.

    Alert and patrol investigations finish by calling a terminal tool; chat may simply
    answer. So a bundle with **no** terminal tool guarantees `Aborted("no_report")`
    after the turn ceiling, having spent the entire budget to discover a wiring
    mistake — and one with *two* makes "which tool concluded this" ambiguous in a way
    the loop resolves silently by taking the first.

    Same discipline as `routing.validate()` and `tool_schemas()` rejecting a declared
    `window`: fail at wiring time, where the fix is one line, rather than mid-incident.
    """
    if not tools:
        raise BundleError("empty tool bundle: the model would have no action to take")

    terminal = sorted(name for name, tool in tools.items() if tool.meta.terminal)
    if requires_report and not terminal:
        raise BundleError(
            "no terminal tool in the bundle, so this investigation can only end by "
            f"hitting the turn ceiling; have {sorted(tools)}"
        )
    if len(terminal) > 1:
        raise BundleError(
            f"{len(terminal)} terminal tools in the bundle ({', '.join(terminal)}); "
            "exactly one tool may conclude an investigation"
        )


__all__ = ["BundleError", "default_bundle", "for_integration", "verify"]
