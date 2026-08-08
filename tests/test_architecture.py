"""The seam rule, enforced.

ROADMAP states it as: *adding a trigger type, an integration, a sink, or an LLM
provider must not change `agent/core/`.* Week 5 L7 measures the outcome once
("integration #3 cost zero lines of Python"). This test defends it continuously,
which matters because a described boundary and a leaking boundary look identical
in a code review.

Two tiers:

- **pure core** — `loop.py`, `investigation.py`, `events.py`, `trace.py`, `dedup.py`
  may import stdlib, their siblings, and protocol/policy modules. Naming any concrete
  implementation fails the build. `trace.py` is here rather than among the seams
  because a *sink* is pluggable and the span model is not; `dedup.py` is here because
  the rule *order* is the policy and it must not be able to reach a YAML parser, a
  clock, or an alert payload format.
- **harness** — may additionally reach each seam's registry, since dispatching
  through registries is its job. Naming a concrete implementation still fails.

Imports are read with `ast`, not by importing the modules, so this test costs
nothing and needs no dependencies installed.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]

#: Abstractions the kernel is allowed to know: domain types and protocols, plus
#: the dispatch policy (which is provider- and integration-agnostic by design).
PURE_CORE_ALLOWED = (
    "agent.core.",
    "agent.llm.types",
    "agent.llm.protocol",
    "agent.tools.protocol",
    "agent.tools.dispatch",
)

#: Registries are lookups, not implementations — the harness routes through them.
HARNESS_EXTRA_ALLOWED = (
    "agent.triggers.registry",
    "agent.integrations.registry",
    "agent.sinks.registry",
    "agent.tools.bundle",
    "agent.prompts.assemble",
    "agent.store.jsonl",
)

PURE_CORE = ("loop.py", "investigation.py", "events.py", "trace.py", "dedup.py")
HARNESS = ("harness.py",)


def _is_module(dotted: str) -> bool:
    """Whether `agent.x.y` is a module on disk.

    Checked against the filesystem rather than by importing: this test reads source with
    `ast` precisely so it costs nothing and needs no dependency installed.
    """
    base = REPO / pathlib.Path(*dotted.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _agent_imports(path: pathlib.Path) -> list[tuple[str, int]]:
    """Every `agent.*` module this file imports, with line numbers.

    Relative imports are resolved against the file's package so that
    `from ..llm.types import X` inside `agent/core/loop.py` is reported as
    `agent.llm.types` rather than as something unclassifiable.

    `from . import trace` needs its own case. With no `module`, every name is by
    definition a submodule, so each is resolved individually — otherwise the form
    reports as a bare `agent.core` and the classifier can say nothing about *what*
    was imported, which is both a false positive on a legal sibling import and no
    information at all on an illegal one.

    `from ..triggers import registry` is the same imprecision one level along, and it
    is resolved the same way: if an imported name is a module on disk, report
    `agent.triggers.registry` rather than the bare package. **Strictly more precise,
    never more permissive** — `from ..triggers import alert` now reports
    `agent.triggers.alert` and is rejected, where before it reported `agent.triggers`
    and was rejected for the wrong reason. Importing a package *root* is still caught,
    by both this classifier and `test_core_never_imports_the_tools_package_root`.
    """
    return _imports_of_source(path.read_text(), path)


def _imports_of_source(source: str, path: pathlib.Path) -> list[tuple[str, int]]:
    """The parsing half, split out so a test can feed it a synthetic module."""
    tree = ast.parse(source, filename=str(path))
    package = ".".join(path.relative_to(REPO).with_suffix("").parts[:-1])
    found: list[tuple[str, int]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("agent"):
                    found.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
            else:
                base = (node.module or "").split(".")

            if node.module is None:
                targets = [".".join([*base, alias.name]) for alias in node.names]
            else:
                prefix = ".".join([*base, node.module]) if node.level else node.module
                submodules = [
                    f"{prefix}.{alias.name}"
                    for alias in node.names
                    if _is_module(f"{prefix}.{alias.name}")
                ]
                targets = submodules or [prefix]

            for target in targets:
                if target.startswith("agent"):
                    found.append((target, node.lineno))
    return found


class TestSeamRule(unittest.TestCase):
    def _check(self, filenames: tuple[str, ...], allowed: tuple[str, ...]) -> None:
        core = REPO / "agent" / "core"
        checked = 0
        for name in filenames:
            path = core / name
            if not path.exists():
                continue  # not yet written; the lesson that adds it inherits this test
            checked += 1
            for module, lineno in _agent_imports(path):
                with self.subTest(file=name, imports=module, line=lineno):
                    self.assertTrue(
                        any(module == a or module.startswith(a) for a in allowed),
                        f"agent/core/{name}:{lineno} imports {module!r}, which is a concrete "
                        f"implementation. The kernel may only depend on protocols and policy "
                        f"— take it as a parameter instead. Allowed prefixes: {allowed}",
                    )
        self.assertGreater(checked, 0, "no core modules found to check")

    def test_pure_core_imports_no_implementation(self):
        self._check(PURE_CORE, PURE_CORE_ALLOWED)

    def test_harness_imports_registries_but_no_implementation(self):
        self._check(HARNESS, PURE_CORE_ALLOWED + HARNESS_EXTRA_ALLOWED)

    def test_core_never_imports_the_tools_package_root(self):
        """`import agent.tools` would execute `agent/tools/__init__.py` and could
        pull a concrete tool in transitively, making the checks above vacuous."""
        for path in (REPO / "agent" / "core").glob("*.py"):
            for module, lineno in _agent_imports(path):
                with self.subTest(file=path.name, line=lineno):
                    self.assertNotEqual(module, "agent.tools")
                    self.assertNotEqual(module, "agent.llm")

    def test_the_check_would_actually_catch_a_violation(self):
        """A guard whose failure mode is untested is decoration.

        Parses a synthetic module that imports a provider adapter and asserts the
        classifier rejects it.
        """
        sample = ast.parse("from ..llm.openai_compat import OpenAICompatLLM\n")
        node = sample.body[0]
        assert isinstance(node, ast.ImportFrom)
        resolved = f"agent.{node.module}"
        self.assertFalse(
            any(resolved == a or resolved.startswith(a) for a in PURE_CORE_ALLOWED),
            "agent.llm.openai_compat must not be classified as allowed",
        )

    def test_a_submodule_import_resolves_to_the_submodule(self):
        """`from ..triggers import registry` must report `agent.triggers.registry`.

        The resolver was sharpened in L6a-1 because it reported the bare package, which
        made a legal registry import fail while saying nothing about *what* was
        imported. This asserts the sharpening did not become permissive: the allowed
        sibling resolves precisely, and a concrete sibling in the same statement is
        still caught.
        """
        source = (
            "from ..triggers import registry\n"
            "from ..triggers import alert\n"
            "from ..tools import bundle, stubs\n"
        )
        path = REPO / "agent" / "core" / "_synthetic_for_test.py"
        found = {module for module, _ in _imports_of_source(source, path)}
        self.assertIn("agent.triggers.registry", found)
        self.assertIn("agent.triggers.alert", found)
        self.assertIn("agent.tools.bundle", found)
        self.assertIn("agent.tools.stubs", found)

        allowed = PURE_CORE_ALLOWED + HARNESS_EXTRA_ALLOWED
        rejected = {m for m in found if not any(m == a or m.startswith(a) for a in allowed)}
        self.assertEqual(rejected, {"agent.triggers.alert", "agent.tools.stubs"})


class TestLayout(unittest.TestCase):
    def test_core_holds_only_the_spine(self):
        """A loose module appearing at `agent/` top level is the drift this
        layout exists to prevent — the kernel and the seams become
        indistinguishable again."""
        loose = sorted(
            p.name
            for p in (REPO / "agent").glob("*.py")
            if p.name != "__init__.py"
        )
        self.assertEqual(loose, [], f"move these into a package: {loose}")

    def test_every_agent_package_is_importable(self):
        for pkg in sorted(p for p in (REPO / "agent").iterdir() if p.is_dir()):
            if pkg.name == "__pycache__":
                continue
            with self.subTest(package=pkg.name):
                self.assertTrue(
                    (pkg / "__init__.py").exists(), f"agent/{pkg.name}/ has no __init__.py"
                )


if __name__ == "__main__":
    unittest.main()
