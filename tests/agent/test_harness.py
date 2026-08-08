"""The harness — each step alone, then one payload driven ① → ⑥.

The end-to-end test is the one that could not have existed before this step: every layer
was real and nothing composed them, so producing a report meant wiring trace + log + loop
+ tools by hand at the call site.

L6a-1 is the happy path. The error invariant, the abandoned-consumer guard and the dedup
lifecycle close are L6a-2, and so are their mutations.
"""
from __future__ import annotations

import io
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from agent.core import harness
from agent.core.events import Aborted, Done, TextDelta, ToolCalled, ToolReturned, TurnStarted
from agent.core.investigation import Investigation, ToolBudget, Window, mint_id
from agent.core.trace import Trace
from agent.llm.types import Message, Response, StopReason, TextBlock, ToolUseBlock
from agent.sinks import registry as sinks
from agent.sinks.registry import Delivery
from agent.sinks.stdout import StdoutSink, render
from agent.tools import bundle as tool_bundle
from agent.triggers import registry as triggers

GOLDEN = pathlib.Path(__file__).resolve().parents[2] / "eval" / "golden"
T0 = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


class ScriptedLLM:
    """Returns queued responses. Records the messages and prompt it was given."""

    def __init__(self, prompt: Any, script: Sequence[Response]) -> None:
        self.prompt = prompt
        self.script = list(script)
        self.seen: list[list[Message]] = []

    async def call(self, messages, tools):
        # Copied, not referenced: `messages` keeps growing, and a test asserting what
        # turn 2 saw would otherwise read turn 5's state.
        self.seen.append(list(messages))
        if not self.script:
            raise RuntimeError("ScriptedLLM ran out of responses")
        return self.script.pop(0)


class Factory:
    """An `LLMFactory`. Keeps the built client so a test can inspect the prompt."""

    def __init__(self, *script: Response) -> None:
        self.script = list(script)
        self.built: list[ScriptedLLM] = []

    def __call__(self, inv, prompt):
        llm = ScriptedLLM(prompt, self.script)
        self.built.append(llm)
        return llm


class RecordingSink:
    name = "recording"

    def __init__(self, *, delivered: bool = True) -> None:
        self.delivered = delivered
        self.calls: list[tuple[Mapping[str, Any] | None, Mapping[str, Any]]] = []

    def deliver(self, report, context):
        self.calls.append((report, dict(context)))
        return Delivery(sink=self.name, delivered=self.delivered, detail="recorded")


def report_call(**payload: Any) -> Response:
    return Response(
        stop_reason=StopReason.TOOL_USE,
        content=[ToolUseBlock(id="t-report", name="submit_report", input=payload)],
    )


def query_call(promql: str = "up") -> Response:
    return Response(
        stop_reason=StopReason.TOOL_USE,
        content=[
            TextBlock(text="checking"),
            ToolUseBlock(id="t-q", name="query_metrics", input={"promql": promql}),
        ],
    )


def investigation(trigger: str = "alert", **kwargs: Any) -> Investigation:
    kwargs.setdefault("budget", ToolBudget())
    return Investigation(
        id=mint_id(), trigger=trigger, window=Window.around(T0), **kwargs
    )


def context_for(inv: Investigation) -> harness.HarnessContext:
    return harness.HarnessContext(trace=Trace(trace_id=inv.id))


class HarnessTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        triggers.install_defaults()
        sinks.install_defaults()
        self.sink = RecordingSink()
        sinks.register(self.sink)

    def tearDown(self) -> None:
        sinks.unregister("recording")


# --------------------------------------------------------------------------- #
# ① route
# --------------------------------------------------------------------------- #


class TestRoute(HarnessTestCase):
    def test_resolves_the_trigger(self):
        self.assertEqual(harness.route("alert", {}).trigger, "alert")

    def test_integration_is_none_until_l5a(self):
        self.assertIsNone(harness.route("alert", {}).integration)

    def test_an_unknown_kind_names_what_is_registered(self):
        with self.assertRaises(KeyError) as caught:
            harness.route("alerts", {})
        self.assertIn("alert", str(caught.exception))

    def test_it_does_not_read_the_payload(self):
        """Reading it is the trigger's job; a router that peeked would be a second place
        that knows a webhook's shape."""
        self.assertEqual(harness.route("alert", {}).trigger, "alert")


# --------------------------------------------------------------------------- #
# ② preprocess / intake
# --------------------------------------------------------------------------- #


class TestIntake(HarnessTestCase):
    def _payload(self) -> dict[str, Any]:
        return json.loads((GOLDEN / "GS-RES-001-redis-oom" / "alert.json").read_text())

    def test_a_golden_alert_produces_one_investigation(self):
        outcome = harness.intake("alert", self._payload())
        self.assertEqual(outcome.created, 1)
        self.assertEqual(outcome.decisions[0].rule, "R5")

    def test_severity_tiering_survives_intake(self):
        inv = harness.intake("alert", self._payload()).investigations[0]
        self.assertEqual(inv.budget.max_tool_calls, 60, "the P1 tier")

    def test_zero_investigations_is_a_normal_result(self):
        """A duplicate delivery. The decision still carries its rule and reason, which is
        what makes a suppression reportable rather than invisible."""
        trigger = triggers.get("alert")
        payload = self._payload()
        harness.intake("alert", payload)
        second = harness.intake("alert", payload)
        self.assertEqual(second.created, 0)
        self.assertEqual(second.decisions[0].rule, "R1")
        self.assertTrue(second.joined)
        del trigger

    def test_patrol_fans_out_to_n(self):
        outcome = harness.intake("patrol", {"targets": ["checkout", "payment", "auth"]})
        self.assertEqual(outcome.created, 3)

    def test_spans_are_emitted_for_both_steps(self):
        trace = Trace(trace_id="t")
        harness.intake("alert", self._payload(), trace=trace)
        names = [s.name for s in trace.spans]
        self.assertIn(harness.STEP_SPANS["route"], names)
        self.assertIn(harness.STEP_SPANS["preprocess"], names)


# --------------------------------------------------------------------------- #
# ③ loadout
# --------------------------------------------------------------------------- #


class TestLoadout(HarnessTestCase):
    def test_provides_tools_and_a_prompt(self):
        inv = investigation()
        result = harness.loadout(inv, context_for(inv))
        self.assertIn("query_metrics", result.tools)
        self.assertIn("submit_report", result.tools)
        self.assertTrue(result.prompt.fragments)

    def test_it_does_not_recompute_the_window_or_the_tier_ceilings(self):
        """W2 L4b moved both into the trigger. Recomputing either here would silently
        overwrite the budget a P1 was granted."""
        inv = investigation(budget=ToolBudget(max_turns=20, max_tool_calls=60))
        window, budget = inv.window, inv.budget

        harness.loadout(inv, context_for(inv))

        self.assertEqual(inv.window, window)
        self.assertEqual(inv.budget.max_turns, budget.max_turns)
        self.assertEqual(inv.budget.max_tool_calls, budget.max_tool_calls)
        self.assertEqual(inv.budget.max_cost, budget.max_cost)

    def test_per_tool_caps_narrow_the_budget_without_touching_the_ceilings(self):
        inv = investigation()
        capped = harness.with_caps(inv.budget, {"query_logs": 6})
        self.assertEqual(capped.per_tool_calls["query_logs"], 6)
        self.assertEqual(capped.max_turns, inv.budget.max_turns)
        self.assertEqual(capped.max_cost, inv.budget.max_cost)

    def test_a_bundle_with_no_terminal_tool_is_refused_when_a_report_is_required(self):
        """Otherwise the wiring mistake surfaces as `Aborted("no_report")` fifteen turns
        and a whole budget later."""
        tools = {
            name: tool
            for name, tool in tool_bundle.default_bundle().items()
            if not tool.meta.terminal
        }
        with self.assertRaises(tool_bundle.BundleError) as caught:
            tool_bundle.verify(tools, requires_report=True)
        self.assertIn("turn ceiling", str(caught.exception))

    def test_chat_may_run_without_a_terminal_tool(self):
        tools = {
            name: tool
            for name, tool in tool_bundle.default_bundle().items()
            if not tool.meta.terminal
        }
        tool_bundle.verify(tools, requires_report=False)  # must not raise

    def test_two_terminal_tools_is_also_refused(self):
        tools = dict(tool_bundle.default_bundle())
        clone = tools["submit_report"]
        tools["submit_report_again"] = clone
        with self.assertRaises(tool_bundle.BundleError):
            tool_bundle.verify(tools, requires_report=True)

    def test_an_empty_bundle_is_refused(self):
        with self.assertRaises(tool_bundle.BundleError):
            tool_bundle.verify({}, requires_report=False)


class TestPromptFragments(HarnessTestCase):
    """The fragment table is L6a-1's, even though the fragments' *content* is L6d's:
    `stable_across` is what places the cache breakpoints, and a wrong label breaks the
    prefix without raising anything. Only the bill notices."""

    def _prompt(self, inv: Investigation | None = None, facet: str = ""):
        inv = inv or investigation()
        return harness.loadout(inv, context_for(inv), integration_facet=facet).prompt

    def test_the_budget_fragment_is_investigation_stable_and_last(self):
        ordered = self._prompt().ordered()
        self.assertEqual(ordered[-1].name, "budget_window")
        self.assertEqual(ordered[-1].stable_across, "investigation")

    def test_no_breakpoint_falls_on_the_per_investigation_fragment(self):
        """Marking a fragment that changes every run buys a cache entry never read and
        pays the write premium for it."""
        prompt = self._prompt()
        ordered = prompt.ordered()
        for index in prompt.breakpoint_indices():
            self.assertNotEqual(ordered[index].stable_across, "investigation")

    def test_the_integration_facet_sits_between_project_and_investigation(self):
        ordered = self._prompt(facet="Redis notes").ordered()
        self.assertEqual(
            [f.stable_across for f in ordered],
            ["project", "project", "integration", "investigation"],
        )

    def test_project_fragments_are_byte_identical_across_investigations(self):
        """A `project`-stable fragment whose text varies per run breaks the cache prefix
        silently. Trivially true today — this is the tripwire for L6d's content."""
        from agent.prompts.assemble import project_fragments

        first = project_fragments(self._prompt(investigation()))
        second = project_fragments(self._prompt(investigation()))
        self.assertEqual(
            [(f.name, f.text) for f in first], [(f.name, f.text) for f in second]
        )

    def test_the_budget_fragment_states_the_window_and_the_ceilings(self):
        inv = investigation(budget=ToolBudget(max_turns=20, max_tool_calls=60))
        text = self._prompt(inv).ordered()[-1].text
        self.assertIn(str(inv.window.start.isoformat()), text)
        self.assertIn("20 turns", text)
        self.assertIn("60 tool calls", text)

    def test_it_carries_ceilings_not_a_countdown(self):
        """A countdown is state, and state belongs in `messages`. It would also make the
        fragment change every *turn*, quietly falsifying its `stable_across` label."""
        inv = investigation()
        before = self._prompt(inv).ordered()[-1].text
        inv.turn = 7
        inv.record_tool_call("query_metrics")
        self.assertEqual(self._prompt(inv).ordered()[-1].text, before)

    def test_untrusted_alert_text_never_reaches_the_system_prompt(self):
        """Two independent reasons point the same way: the system prompt is the trust
        boundary, and per-investigation text in a project-stable slot would destroy the
        cache prefix."""
        payload = json.loads((GOLDEN / "GS-RES-001-redis-oom" / "alert.json").read_text())
        inv = harness.intake("alert", payload).investigations[0]
        prompt_text = self._prompt(inv).text()
        self.assertNotIn("bgsave", prompt_text.lower())
        self.assertNotIn("HighErrorRate", prompt_text)
        # …while the payload is in `messages`, where it belongs.
        messages = "".join(
            getattr(b, "text", "") for m in inv.messages for b in m.content
        )
        self.assertIn("HighErrorRate", messages)


# --------------------------------------------------------------------------- #
# ⑤ parse
# --------------------------------------------------------------------------- #


class TestParse(HarnessTestCase):
    def test_a_done_with_a_report(self):
        inv = investigation()
        result = harness.parse(Done(report={"verdict": "origin"}), context_for(inv))
        self.assertTrue(result.has_report)
        self.assertEqual(result.failure, "")

    def test_a_chat_answer_becomes_a_report_shaped_payload(self):
        inv = investigation("chat")
        result = harness.parse(Done(text="checkout is slow"), context_for(inv))
        self.assertEqual(result.report, {"answer": "checkout is slow"})

    def test_an_abort_carries_its_reason_forward(self):
        inv = investigation()
        result = harness.parse(Aborted("budget", "spent 0.41 USD of 0.40"), context_for(inv))
        self.assertIsNone(result.report)
        self.assertIn("budget", result.failure)
        self.assertIn("0.41", result.failure)

    def test_it_does_not_validate_the_report(self):
        """Validation lives at the tool boundary, where an `is_error` can still reach the
        model. Here the loop has ended and there is no feedback path left."""
        inv = investigation()
        result = harness.parse(Done(report={"nonsense": True}), context_for(inv))
        self.assertEqual(result.report, {"nonsense": True})


# --------------------------------------------------------------------------- #
# ⑥ fanout
# --------------------------------------------------------------------------- #


class TestFanout(HarnessTestCase):
    def test_delivers_to_the_named_sinks(self):
        inv = investigation()
        ctx = context_for(inv)
        result = harness.parse(Done(report={"verdict": "origin"}), ctx)
        deliveries = harness.fanout(result, inv, ("recording",), ctx)
        self.assertEqual([d.sink for d in deliveries], ["recording"])
        self.assertTrue(deliveries[0].delivered)

    def test_it_runs_on_an_abort_too(self):
        """"Budget exhausted after 12 tool calls, here is what was established" is the
        message on-call needs most."""
        inv = investigation()
        ctx = context_for(inv)
        result = harness.parse(Aborted("budget", "ceiling reached"), ctx)
        deliveries = harness.fanout(result, inv, ("recording",), ctx)
        self.assertEqual(len(deliveries), 1)
        report, context = self.sink.calls[0]
        self.assertIsNone(report)
        self.assertIn("budget", context["failure"])

    def test_an_unregistered_sink_is_skipped_and_recorded(self):
        inv = investigation()
        ctx = context_for(inv)
        result = harness.parse(Done(report={}), ctx)
        harness.fanout(result, inv, ("recording", "pagerduty"), ctx)
        span = next(s for s in ctx.trace.spans if s.name == harness.STEP_SPANS["fanout"])
        self.assertEqual(span.attrs["unresolved"], "pagerduty")
        self.assertEqual(span.attrs["delivered"], 1)

    def test_the_default_binding_comes_from_the_trigger(self):
        self.assertEqual(harness.sinks_for(investigation("alert")), ("stdout", "slack", "jira"))
        self.assertEqual(harness.sinks_for(investigation("chat")), ("stdout",))

    def test_context_carries_the_numbers_a_sink_needs(self):
        inv = investigation()
        inv.record_tool_call("query_metrics")
        ctx = context_for(inv)
        result = harness.parse(Done(report={}), ctx)
        harness.fanout(result, inv, ("recording",), ctx)
        _, context = self.sink.calls[0]
        self.assertEqual(context["investigation_id"], inv.id)
        self.assertEqual(context["tool_calls"], 1)
        self.assertIn("profile", context)
        self.assertIn("ledger", context)

    def test_cost_is_read_off_the_trace_not_from_a_ledger_object(self):
        """`agent/core/` may not import the cost module, and it does not have to: the
        gateway already stamps `ledger.summary()` on every `llm.call` span."""
        inv = investigation()
        ctx = context_for(inv)
        ctx.trace.record(
            "llm.call", duration_ms=12.0, ledger={"money_spent": {"CNY": 0.5}, "calls": 1}
        )
        ctx.trace.record(
            "llm.call", duration_ms=9.0, ledger={"money_spent": {"CNY": 0.9}, "calls": 2}
        )
        self.assertEqual(harness.latest_ledger(ctx.trace)["money_spent"], {"CNY": 0.9})

    def test_no_llm_call_means_no_ledger_rather_than_zero(self):
        inv = investigation()
        self.assertEqual(harness.latest_ledger(context_for(inv).trace), {})

    def test_elapsed_is_non_zero_at_delivery_even_though_the_root_is_still_open(self):
        """⑥ runs inside the root span, so the root's duration is not set yet. Reading it
        straight off `profile()` printed `elapsed 0.0ms` on a real report."""
        inv = investigation()
        ctx = context_for(inv)
        result = harness.parse(Done(report={}), ctx)
        harness.fanout(result, inv, ("recording",), ctx)
        _, context = self.sink.calls[0]
        self.assertGreater(context["profile"]["elapsed_ms"], 0.0)


# --------------------------------------------------------------------------- #
# ① → ⑥, the test that could not have existed before this step
# --------------------------------------------------------------------------- #


class TestEndToEnd(HarnessTestCase):
    def _payload(self) -> dict[str, Any]:
        return json.loads((GOLDEN / "GS-RES-001-redis-oom" / "alert.json").read_text())

    async def test_a_golden_alert_becomes_a_delivered_report(self):
        inv = harness.intake("alert", self._payload()).investigations[0]
        factory = Factory(query_call(), report_call(verdict="origin", root_cause="redis oom"))

        outcome = await harness.investigate_to_completion(
            inv, llm_factory=factory, sink_names=("recording",)
        )

        self.assertIsInstance(outcome, Done)
        self.assertEqual(len(self.sink.calls), 1)
        report, context = self.sink.calls[0]
        self.assertEqual(report["verdict"], "origin")
        self.assertEqual(context["outcome"], "Done")
        self.assertEqual(context["correlation_id"], "gs-res-001-redis-oom")

    async def test_the_event_stream_is_consumable_by_an_outside_subscriber(self):
        """L6b's console subscribes to exactly this. `run_to_completion` collapsing the
        stream to one terminal event is what made it a discarded instrument."""
        inv = investigation()
        factory = Factory(query_call(), report_call(verdict="origin"))

        seen = [
            type(event).__name__
            async for event in harness.investigate(
                inv, llm_factory=factory, sink_names=("recording",)
            )
        ]

        self.assertEqual(seen[0], "TurnStarted")
        self.assertIn("TextDelta", seen)
        self.assertIn("ToolCalled", seen)
        self.assertIn("ToolReturned", seen)
        self.assertEqual(seen[-1], "Done")

    async def test_side_sinks_see_every_event_the_subscriber_sees(self):
        inv = investigation()
        factory = Factory(report_call(verdict="origin"))
        side: list[str] = []

        streamed = [
            type(e).__name__
            async for e in harness.investigate(
                inv,
                llm_factory=factory,
                on_event=lambda e: side.append(type(e).__name__),
                sink_names=("recording",),
            )
        ]
        self.assertEqual(streamed, side)

    async def test_the_run_is_one_tree_not_four(self):
        """Asserts **nesting**, not presence. The first version of this test checked only
        that the step names appeared, and passed while `loadout` / `parse` / `fanout` were
        three separate roots — so `Trace.profile()` reported one step's duration as the
        whole run's `elapsed_ms`, and `srectl replay` printed four trees for one run.
        """
        from agent.core.trace import tree

        inv = investigation()
        trace = Trace(trace_id=inv.id)
        factory = Factory(query_call(), report_call(verdict="origin"))

        await harness.investigate_to_completion(
            inv, llm_factory=factory, trace=trace, sink_names=("recording",)
        )

        roots = [s for s in trace.spans if s.parent_id is None]
        self.assertEqual([s.name for s in roots], [harness.RUN_SPAN], "exactly one root")

        depths = {record["name"]: depth for depth, record in tree(trace.spans)}
        self.assertEqual(depths[harness.RUN_SPAN], 0)
        for step in ("loadout", "parse", "fanout"):
            self.assertEqual(depths[harness.STEP_SPANS[step]], 1, step)
        self.assertEqual(depths["investigation"], 1, "the loop sits inside the shell")
        self.assertEqual(depths["turn"], 2)
        self.assertEqual(depths["tool.call"], 3)

    async def test_elapsed_covers_the_whole_shell_not_just_the_loop(self):
        inv = investigation()
        trace = Trace(trace_id=inv.id)
        await harness.investigate_to_completion(
            inv,
            llm_factory=Factory(query_call(), report_call(verdict="origin")),
            trace=trace,
            sink_names=("recording",),
        )
        elapsed = trace.profile()["elapsed_ms"]
        steps = [
            s.duration_ms
            for s in trace.spans
            if s.name.startswith("harness.") and s.name != harness.RUN_SPAN
        ]
        self.assertGreaterEqual(elapsed, sum(steps), "every step is inside the root")

    async def test_the_step_spans_split_the_overhead_lump(self):
        """`Trace.profile()` reported one undifferentiated `overhead_ms` where §42
        promised a critical-path profile. Now it reads by step name."""
        inv = investigation()
        trace = Trace(trace_id=inv.id)
        factory = Factory(query_call(), report_call(verdict="origin"))

        await harness.investigate_to_completion(
            inv, llm_factory=factory, trace=trace, sink_names=("recording",)
        )

        named = {
            s.name: s.duration_ms
            for s in trace.spans
            if s.name.startswith("harness.")
        }
        self.assertGreaterEqual(len(named), 3)
        self.assertTrue(all(d is not None for d in named.values()))

    async def test_the_prompt_reaches_the_llm_through_the_factory(self):
        inv = investigation()
        factory = Factory(report_call(verdict="origin"))
        await harness.investigate_to_completion(
            inv, llm_factory=factory, sink_names=("recording",)
        )
        prompt = factory.built[0].prompt
        self.assertEqual(
            [f.name for f in prompt.ordered()],
            ["role_methodology", "output_contract", "budget_window"],
        )

    async def test_the_jsonl_log_holds_the_whole_run(self):
        inv = investigation()
        factory = Factory(query_call(), report_call(verdict="origin"))
        with tempfile.TemporaryDirectory() as tmp:
            log = harness.open_log(inv, root=tmp)
            await harness.investigate_to_completion(
                inv, llm_factory=factory, log=log, sink_names=("recording",)
            )

            from agent.store import jsonl

            records = jsonl.load(pathlib.Path(tmp) / f"{inv.id}.jsonl")
            kinds = {r["t"] for r in records}
            self.assertEqual(kinds, {"header", "span", "event", "outcome", "footer"})
            footer = jsonl.footer_of(records)
            self.assertEqual(footer["ledger"]["deliveries"][0]["sink"], "recording")
            rebuilt = jsonl.rebuild_messages(records)
            self.assertEqual(len(rebuilt), len(inv.messages))

    async def test_steps_three_to_five_behave_the_same_for_all_three_triggers(self):
        """③④⑤ being trigger-independent is what makes chat and patrol new triggers
        rather than new architectures."""
        shapes = {}
        for kind, script in (
            ("alert", [report_call(verdict="origin")]),
            ("patrol", [report_call(verdict="origin")]),
            ("chat", [Response(stop_reason=StopReason.END_TURN, content=[TextBlock(text="hi")])]),
        ):
            inv = investigation(kind)
            trace = Trace(trace_id=inv.id)
            outcome = await harness.investigate_to_completion(
                inv, llm_factory=Factory(*script), trace=trace, sink_names=("recording",)
            )
            shapes[kind] = (
                type(outcome).__name__,
                [s.name for s in trace.spans if s.name.startswith("harness.")],
            )

        self.assertEqual(shapes["alert"][1], shapes["patrol"][1])
        self.assertEqual(shapes["alert"][1], shapes["chat"][1])
        self.assertEqual(shapes["chat"][0], "Done", "answering and stopping is legitimate")

    async def test_a_message_injected_at_turn_start_reaches_the_next_call(self):
        """A guard, not a fix — it passes today. Two scheduled lessons stand on it: W3
        L6's compaction rewrites `messages` between turns, and W3 L8's soft-ceiling
        warning injects one. Both work only because `loop.run` yields `TurnStarted`
        *before* awaiting the LLM, and an async generator suspends at `yield`.

        If that ordering is ever moved, nothing else in the suite goes red.
        """
        inv = investigation()
        factory = Factory(query_call(), report_call(verdict="origin"))

        async for event in harness.investigate(
            inv, llm_factory=factory, sink_names=("recording",)
        ):
            if isinstance(event, TurnStarted) and event.turn == 1:
                inv.add_user_text("<note>12 of 15 turns used</note>")

        second_call = factory.built[0].seen[1]
        text = "".join(
            getattr(b, "text", "") for m in second_call for b in m.content
        )
        self.assertIn("12 of 15 turns used", text)


# --------------------------------------------------------------------------- #
# The stdout sink
# --------------------------------------------------------------------------- #


class TestStdoutSink(HarnessTestCase):
    def _context(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "investigation_id": "inv_abc",
            "outcome": "Done",
            "correlation_id": "gs-res-001",
            "window": "start .. end",
            "failure": "",
            "turns": 3,
            "tool_calls": 4,
            "profile": {"elapsed_ms": 91.2, "llm_calls": 3, "retries": 0},
            "ledger": {},
        }
        base.update(overrides)
        return base

    def test_it_prints_the_report_and_the_numbers(self):
        text = render({"verdict": "origin", "root_cause": "redis oom"}, self._context())
        self.assertIn("VERDICT", text)
        self.assertIn("redis oom", text)
        self.assertIn("turns 3", text)
        self.assertIn("91.2ms", text)

    def test_lead_keys_come_first_and_unknown_keys_are_not_dropped(self):
        text = render({"zzz_extra": "kept", "verdict": "origin"}, self._context())
        self.assertLess(text.index("VERDICT"), text.index("ZZZ_EXTRA"))

    def test_an_abort_shows_its_reason_above_the_body(self):
        text = render(None, self._context(outcome="Aborted", failure="budget: ceiling"))
        self.assertIn("budget: ceiling", text)
        self.assertIn("no report", text)
        self.assertLess(text.index("budget: ceiling"), text.index("no report"))

    def test_retries_and_errors_are_shown_only_when_non_zero(self):
        """A line that always reads "retries 0" trains the reader to skip it."""
        clean = render({}, self._context())
        self.assertNotIn("retries", clean)

        fought = render(
            {}, self._context(profile={"elapsed_ms": 1.0, "llm_calls": 2, "retries": 3, "errors": 1})
        )
        self.assertIn("retries 3", fought)
        self.assertIn("errors 1", fought)

    def test_no_llm_call_reports_zero_calls_rather_than_zero_money(self):
        self.assertIn("0 llm calls", render({}, self._context()))

    def test_money_is_per_currency_and_never_summed(self):
        text = render(
            {},
            self._context(
                ledger={"money_spent": {"CNY": 0.0123, "USD": 0.004}, "budget_charged": {"CNY": 0.0123, "USD": 0.004}}
            ),
        )
        self.assertIn("CNY", text)
        self.assertIn("USD", text)

    def test_the_budget_charged_gap_is_shown(self):
        """`budget_charged` above `money_spent` means cache hits replayed their original
        cost — the mechanism that keeps a degraded run degrading on rerun."""
        text = render(
            {},
            self._context(
                ledger={"money_spent": {"CNY": 0.01}, "budget_charged": {"CNY": 0.03}}
            ),
        )
        self.assertIn("budget charged", text)

    def test_unverified_prices_are_labelled(self):
        text = render(
            {},
            self._context(
                ledger={"money_spent": {"USD": 0.01}, "budget_charged": {"USD": 0.01}, "prices_verified": False}
            ),
        )
        self.assertIn("prices unverified", text)

    def test_the_sink_writes_to_its_stream_and_reports_delivered(self):
        stream = io.StringIO()
        delivery = StdoutSink(stream).deliver({"verdict": "origin"}, self._context())
        self.assertTrue(delivery.delivered)
        self.assertIn("VERDICT", stream.getvalue())


class TestSinkRegistry(HarnessTestCase):
    def test_only_implemented_sinks_are_registered(self):
        """`available()` is a fact, not a promise — `slack` and `jira` join in L6a-2."""
        self.assertIn("stdout", sinks.available())
        self.assertNotIn("slack", sinks.available())

    def test_resolve_skips_what_is_not_installed_and_missing_reports_it(self):
        self.assertEqual([s.name for s in sinks.resolve(("stdout", "slack"))], ["stdout"])
        self.assertEqual(sinks.missing(("stdout", "slack")), ["slack"])

    def test_an_unknown_sink_names_what_is_registered(self):
        with self.assertRaises(KeyError) as caught:
            sinks.get("pagerduty")
        self.assertIn("stdout", str(caught.exception))

    def test_the_stdout_sink_satisfies_the_protocol(self):
        self.assertIsInstance(StdoutSink(), sinks.Sink)


if __name__ == "__main__":
    unittest.main()
