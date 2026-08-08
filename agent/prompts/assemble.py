"""System prompt assembly — the fragments, and where their cache boundaries fall.

Four fragments, and their order is not a naming convention — it is a **stability
ranking** that `SystemPrompt.ordered()` sorts on and `breakpoint_indices()` places
cache breakpoints from:

| fragment | `stable_across` | content lands | breakpoint |
|---|---|---|---|
| `[A] role_methodology` | `project` | W3 L1 (the D-rules) | — |
| `[B] output_contract` | `project` | L6e (the report contract) | ✅ end of run |
| `[C] integration` | `integration` | L5a (from the integration YAML) | ✅ end of run |
| `[D] budget_window` | `investigation` | **here** | ❌ never |

Two breakpoints today, of the four `MAX_CACHE_BREAKPOINTS` allows. Two consequences:
adding more `project`-stable text is free, and `[D]` must stay last and unmarked —
marking a fragment that changes every run buys a cache entry that is never read and pays
the write premium for it.

**`[D]` carries ceilings, not a countdown.** No "9 turns remaining": that is state, and
state belongs in `messages`, not in the policy layer. It would also make `[D]` change
every *turn* rather than every investigation, which would quietly falsify the
`stable_across` label — and the byte-identity test in `tests/prompts/` is the only guard
that label has. Whether telling the model its remaining budget helps at all is an eval
question for W3, not a default.

**Untrusted data never comes through here.** The alert payload, log lines and tool
results all live in `messages`, wrapped in named tags by the trigger. Two independent
reasons point the same way: the system prompt is the trust boundary
([SECURITY](../../SECURITY.md) layer 1), and per-investigation text in a `project`-stable
position would destroy the cache prefix.

Plain markdown files, no jinja2 — four fragments do not need a template engine, and the
one substitution `[D]` needs is an f-string.
"""
from __future__ import annotations

import pathlib

from ..core.investigation import Investigation
from ..llm.request import PromptFragment, SystemPrompt

#: Where the authored fragments live. `[A]` and `[B]` are files because they are prose a
#: human edits and reviews; `[C]` comes from an integration's YAML (L5a) and `[D]` is
#: computed, so neither is a file.
PROMPT_DIR = pathlib.Path(__file__).resolve().parent

#: Fragment names, fixed so a trace attribute or an eval row can key on them.
ROLE = "role_methodology"
CONTRACT = "output_contract"
INTEGRATION = "integration"
BUDGET = "budget_window"


def assemble(
    inv: Investigation,
    *,
    integration_facet: str = "",
    root: pathlib.Path | None = None,
) -> SystemPrompt:
    """Build the four fragments for one investigation.

    Returns `SystemPrompt` rather than a string so the caller — harness ③ — can forward
    an opaque value it never has to name. That is what keeps `agent/core/` clear of
    `agent.llm.request`, which the seam test does not allow it to import.
    """
    directory = root or PROMPT_DIR
    fragments = [
        PromptFragment(
            name=ROLE, text=_read(directory / "methodology.md"), stable_across="project"
        ),
        PromptFragment(
            name=CONTRACT,
            text=_read(directory / "output_contract.md"),
            stable_across="project",
        ),
    ]
    if integration_facet:
        fragments.append(
            PromptFragment(
                name=INTEGRATION, text=integration_facet, stable_across="integration"
            )
        )
    fragments.append(
        PromptFragment(
            name=BUDGET, text=budget_fragment(inv), stable_across="investigation"
        )
    )
    return SystemPrompt.of(*fragments)


def budget_fragment(inv: Investigation) -> str:
    """`[D]`: the pinned window and the ceilings — facts, not instructions.

    The window is stated here and **enforced** in code: `tool_schemas()` refuses any
    tool declaring `window`, so the model reads the range it is reasoning about and has
    no channel to move it. Stated in the prompt, enforced in the code — the prompt is
    never the enforcement mechanism.
    """
    budget = inv.budget
    ceilings = ", ".join(
        f"{amount:g} {currency}" for currency, amount in sorted(budget.max_cost.items())
    )
    return "\n".join(
        [
            "## This investigation",
            "",
            f"- Time range under investigation: {inv.window}. Every query you make is "
            "pinned to it; you cannot widen or move it.",
            f"- Ceilings: {budget.max_turns} turns, {budget.max_tool_calls} tool calls, "
            f"{ceilings}. They are enforced by the code, not by you.",
            "- Calling the same tool with identical arguments a third time is refused "
            "and returns the previous result.",
        ]
    )


def project_fragments(prompt: SystemPrompt) -> tuple[PromptFragment, ...]:
    """The `project`-stable fragments — the cache prefix, isolated for the guard test."""
    return tuple(f for f in prompt.fragments if f.stable_across == "project")


#: Fragment text, cached per path for the life of the process.
_CACHE: dict[pathlib.Path, str] = {}


def _read(path: pathlib.Path) -> str:
    """Read a fragment file once per process, tolerating absence.

    **Cached, and that is a correctness property as much as a speed one.** These
    fragments are `project`-stable *by definition*, and re-reading them per
    investigation means a file edited mid-run silently changes the cache prefix between
    two investigations — exactly what the byte-identity test guards against, except no
    test can catch it if the value is read fresh each time. Caching makes the guarantee
    structural.

    Measured: re-reading cost **70 µs per loadout**, which was most of the harness's
    whole per-investigation overhead, for two files totalling 285 bytes.

    The cost, stated: editing a prompt file now needs a restart. That is the right
    trade — a prompt change is a deployment, not a hot edit, and `prompt_version` (L6d)
    is what records which text served a given run.

    Absence reads as empty rather than raising: `[A]`'s content arrives in W3 L1 and
    `[B]`'s in L6e. An empty fragment is dropped by `SystemPrompt.text()` and never
    carries a breakpoint, so a missing file costs its own content and nothing structural.
    """
    if path not in _CACHE:
        try:
            _CACHE[path] = path.read_text(encoding="utf-8").strip()
        except OSError:
            _CACHE[path] = ""
    return _CACHE[path]


def forget_cached_fragments() -> None:
    """Drop the fragment cache. For tests that write a fragment file mid-process."""
    _CACHE.clear()


__all__ = [
    "BUDGET",
    "CONTRACT",
    "INTEGRATION",
    "PROMPT_DIR",
    "ROLE",
    "assemble",
    "budget_fragment",
    "forget_cached_fragments",
    "project_fragments",
]
