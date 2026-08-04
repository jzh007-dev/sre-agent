"""Week 2 L4a: the JSONL sink, and whether a run can be reconstructed from it.

The headline assertion is `test_rebuilt_messages_equal_the_live_messages`. `messages`
*is* the investigation's state, so reconstruction fidelity is what makes the log
sufficient for chat resume, for W5's storm absorption, and for anything W6-W7 wants
to checkpoint. It is asserted as an equality against a live run rather than described
as a property.

Reconstruction works from the **event stream**, not from a snapshot of `messages`. A
snapshot would be self-consistent by construction and would prove nothing about the
stream, which is the artefact those three consumers actually read.
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Any

from agent.core.investigation import Investigation, ToolBudget, Window
from agent.core.loop import run_to_completion
from agent.core.trace import Trace
from agent.llm.stub import StubLLM
from agent.llm.types import Response, StopReason, TextBlock, ToolUseBlock
from agent.store.jsonl import (
    InvestigationLog,
    footer_of,
    header_of,
    load,
    load_investigation,
    logged_investigations,
    outcome_of,
    rebuild_messages,
    spans_of,
    split_runs,
)
from agent.tools.stubs import default_tool_registry
from agent.triggers.alert import investigation_from_payload

T0 = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)

ALERT = {
    "_meta": {"purpose": "must never reach the model"},
    "alert_name": "HighErrorRate",
    "commonLabels": {"service": "auth", "severity": "P1"},
    "commonAnnotations": {"correlation_id": "gs-res-001-redis-oom"},
}


def _three_turn_script() -> list[Response]:
    """Text, then a tool call, twice, then a report — the shape a real turn takes."""
    return [
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[
                TextBlock(text="auth is 5xx-ing; checking the error rate first."),
                ToolUseBlock(
                    id="t1", name="query_metrics", input={"promql": "rate(http_requests_total)"}
                ),
            ],
        ),
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[
                TextBlock(text="Rate confirmed. Looking for a downstream cause."),
                ToolUseBlock(
                    id="t2", name="query_logs", input={"service": "auth", "level": "ERROR"}
                ),
                ToolUseBlock(id="t3", name="search_runbook", input={"query": "auth 5xx"}),
            ],
        ),
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[
                TextBlock(text="Delivering."),
                ToolUseBlock(
                    id="t4",
                    name="submit_report",
                    input={
                        "root_cause": "redis rejected session writes",
                        "confidence": "high",
                        "evidence": ["m1", "l1"],
                    },
                ),
            ],
        ),
    ]


class TestRoundTrip(unittest.IsolatedAsyncioTestCase):
    async def _run(self, root: str, script=None) -> tuple[Investigation, list[dict[str, Any]]]:
        inv = investigation_from_payload(ALERT, t0=T0, budget=ToolBudget(max_turns=8))
        log = InvestigationLog(inv, root).open()
        trace = Trace(trace_id=inv.id, correlation_id=inv.correlation_id, sinks=[log.span])

        await run_to_completion(
            inv,
            llm=StubLLM(script=script or _three_turn_script()),
            tools=default_tool_registry(),
            trace=trace,
            on_event=log.event,
        )
        log.close({"calls": 3})
        return inv, load(log.path)

    async def test_rebuilt_messages_equal_the_live_messages(self):
        """The number this lesson ships: reconstruction is exact, not approximate."""
        with tempfile.TemporaryDirectory() as root:
            inv, records = await self._run(root)

            rebuilt = rebuild_messages(records)

            self.assertEqual(len(rebuilt), len(inv.messages))
            self.assertEqual(rebuilt, inv.messages)

    async def test_the_log_holds_every_record_kind(self):
        with tempfile.TemporaryDirectory() as root:
            _, records = await self._run(root)

            kinds = {r["t"] for r in records}
            self.assertEqual(kinds, {"header", "span", "event", "outcome", "footer"})
            self.assertEqual(records[0]["t"], "header", "header first, always")

    async def test_the_header_carries_both_ids_and_the_pinned_window(self):
        with tempfile.TemporaryDirectory() as root:
            inv, records = await self._run(root)

            header = header_of(records)
            assert header is not None
            self.assertEqual(header["investigation_id"], inv.id)
            self.assertEqual(header["trace_id"], inv.id)
            self.assertEqual(header["correlation_id"], "gs-res-001-redis-oom")
            # Reproducibility: the same case rerun must read the same range, so the
            # range has to be in the log or a replay cannot verify it.
            self.assertEqual(header["window"]["start"], inv.window.start.isoformat())

    async def test_the_header_messages_do_not_leak_fixture_metadata(self):
        """`strip_fixture_metadata` protects the prompt; this checks the *log* does
        not reintroduce the leak by another route. Every golden `alert.json` states
        its root cause in `_meta.purpose`."""
        with tempfile.TemporaryDirectory() as root:
            _, records = await self._run(root)

            blob = json.dumps(records, ensure_ascii=False)
            self.assertNotIn("_meta", blob)
            self.assertNotIn("must never reach the model", blob)

    async def test_the_outcome_record_carries_the_report(self):
        with tempfile.TemporaryDirectory() as root:
            _, records = await self._run(root)

            outcome = outcome_of(records)
            assert outcome is not None
            self.assertEqual(outcome["kind"], "Done")
            self.assertEqual(outcome["report"]["root_cause"], "redis rejected session writes")
            self.assertEqual(footer_of(records)["ledger"], {"calls": 3})

    async def test_an_aborted_run_is_fully_on_disk(self):
        """The case §42 names: before L4a a failed run left an `Aborted` reason and
        nothing else."""
        script = [
            Response(
                stop_reason=StopReason.TOOL_USE,
                content=[ToolUseBlock(id="t1", name="query_metrics", input={"promql": "up"})],
            ),
            Response(stop_reason=StopReason.END_TURN, content=[TextBlock(text="no idea")]),
        ]
        with tempfile.TemporaryDirectory() as root:
            inv, records = await self._run(root, script)

            outcome = outcome_of(records)
            assert outcome is not None
            self.assertEqual(outcome["kind"], "Aborted")
            self.assertEqual(outcome["reason"], "no_report")
            # And the evidence gathered before the abort survived.
            self.assertEqual(rebuild_messages(records), inv.messages)
            self.assertTrue(any(s["name"] == "tool.call" for s in spans_of(records)))

    async def test_spans_land_in_the_log_with_durations(self):
        with tempfile.TemporaryDirectory() as root:
            _, records = await self._run(root)

            spans = spans_of(records)
            self.assertTrue(spans)
            self.assertTrue(all(s["duration_ms"] is not None for s in spans))
            self.assertEqual(
                {s["name"] for s in spans}, {"investigation", "turn", "tool.call"}
            )

    async def test_replay_finds_the_log_by_investigation_id(self):
        with tempfile.TemporaryDirectory() as root:
            inv, _ = await self._run(root)

            self.assertEqual(logged_investigations(root), [inv.id])
            self.assertTrue(load_investigation(inv.id, root))


class TestResumeAndPartialLogs(unittest.IsolatedAsyncioTestCase):
    async def test_two_runs_on_one_investigation_split_on_the_header(self):
        """Chat resume and W5's storm absorption both re-run the loop on one
        investigation. One file per investigation keeps `srectl replay <id>` a single
        lookup, and successive headers delimit the runs — so no run id is invented."""
        with tempfile.TemporaryDirectory() as root:
            inv = Investigation(id="inv_chat", trigger="chat", window=Window.around(T0))
            inv.add_user_text("why is checkout slow?")
            log = InvestigationLog(inv, root)

            for answer in ("checking now", "downstream payment latency"):
                log.open()
                await run_to_completion(
                    inv,
                    llm=StubLLM(
                        script=[
                            Response(
                                stop_reason=StopReason.END_TURN,
                                content=[TextBlock(text=answer)],
                            )
                        ]
                    ),
                    tools=default_tool_registry(),
                    on_event=log.event,
                )
                inv.add_user_text("and now?")

            runs = split_runs(load(log.path))
            self.assertEqual(len(runs), 2)
            self.assertEqual(len(header_of(runs[0])["messages"]), 1)
            # The second run's header shows the conversation it resumed from.
            self.assertEqual(len(header_of(runs[1])["messages"]), 3)

    def test_a_truncated_final_line_is_skipped_rather_than_fatal(self):
        """An interrupted write is an expected way for this file to end — usually
        interrupted by whatever made the log worth reading."""
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "inv_x.jsonl"
            path.write_text(
                json.dumps({"t": "header", "investigation_id": "inv_x", "messages": []})
                + "\n"
                + '{"t": "span", "name": "turn", "dur'  # killed mid-write
            )

            records = load(path)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["t"], "header")

    def test_records_before_the_first_header_are_kept(self):
        """A log truncated at the front is still evidence. Dropping the orphans would
        make it look like a shorter run rather than a partial one."""
        records = [
            {"t": "event", "kind": "TurnStarted", "turn": 0},
            {"t": "header", "investigation_id": "inv_x"},
            {"t": "event", "kind": "TurnStarted", "turn": 0},
        ]

        runs = split_runs(records)

        self.assertEqual(len(runs), 2)
        self.assertIsNone(header_of(runs[0]))

    def test_a_missing_log_reads_as_empty_rather_than_raising(self):
        self.assertEqual(load("var/nope/does-not-exist.jsonl"), [])
        self.assertEqual(logged_investigations("var/nope"), [])


class TestKnownDivergence(unittest.TestCase):
    def test_text_blocks_coalesce_ahead_of_tool_use_within_a_turn(self):
        """The one place the rebuild is not byte-exact, asserted rather than only
        described in a docstring.

        The loop yields every `TextDelta` before any `ToolCalled`, and it must: the
        `max_tokens` check sits between them, and a `ToolCalled` emitted before that
        check would announce a dispatch that never happens. So interleaved content
        comes back reordered. Providers put text before tool calls in practice, which
        is why every other test in this file gets an exact equality.
        """
        records = [
            {"t": "header", "messages": []},
            {"t": "event", "kind": "TurnStarted", "turn": 0},
            {"t": "event", "kind": "TextDelta", "text": "before"},
            {
                "t": "event",
                "kind": "ToolCalled",
                "tool_use_id": "t1",
                "name": "query_metrics",
                "input": {"promql": "up"},
            },
            {"t": "event", "kind": "TextDelta", "text": "after"},
            {"t": "outcome", "kind": "Aborted", "reason": "no_report", "detail": ""},
        ]

        rebuilt = rebuild_messages(records)

        self.assertEqual(
            [type(b).__name__ for b in rebuilt[0].content],
            ["TextBlock", "TextBlock", "ToolUseBlock"],
            "both text blocks precede the tool_use, whatever the original order",
        )

    def test_an_unknown_block_type_is_readable_rather_than_fatal(self):
        """A log written by a later version must still post-mortem."""
        records = [
            {
                "t": "header",
                "messages": [{"role": "user", "content": [{"type": "thinking", "x": 1}]}],
            }
        ]

        rebuilt = rebuild_messages(records)

        self.assertEqual(len(rebuilt), 1)
        self.assertIn("thinking", rebuilt[0].content[0].text)


if __name__ == "__main__":
    unittest.main()
