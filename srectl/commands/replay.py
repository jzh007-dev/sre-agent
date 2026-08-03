"""`srectl replay <investigation-id>` — read an investigation back off disk.

The consumer that makes the JSONL sink worth writing. Three questions it answers,
none of which could be asked before W2 L4a:

1. **Where did the time go?** The span tree with durations, and a profile line
   splitting elapsed time between provider calls, tool calls, and our own overhead.
2. **What actually happened?** The outcome, and `--messages` to rebuild the
   conversation from the event stream — the same reconstruction chat resume relies
   on, so exercising it here keeps it honest.
3. **Did it get stuck?** Repeated `(tool, args_hash)` pairs are visible in the tree,
   and a refused repeat is marked. A run that hit `max_turns` used to say only
   `max_turns`.

Run: `.venv/bin/python -m srectl replay <investigation-id>`
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Mapping, Sequence

from agent.core.trace import ATTEMPT, LLM_CALL, TOOL_CALL, tree
from agent.store.jsonl import (
    DEFAULT_ROOT,
    header_of,
    load_investigation,
    logged_investigations,
    outcome_of,
    rebuild_messages,
    spans_of,
    split_runs,
)


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="srectl replay")
    parser.add_argument("investigation_id", nargs="?", help="e.g. inv_9f2a1c3d4e5f")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="log directory")
    parser.add_argument(
        "--messages",
        action="store_true",
        help="rebuild and print the conversation from the event stream",
    )
    parser.add_argument("--list", action="store_true", help="list logged investigations")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(list(argv))

    if args.list or not args.investigation_id:
        ids = logged_investigations(args.root)
        if not ids:
            print(f"no investigation logs under {args.root}/")
            return 0 if args.list else 2
        for name in ids:
            print(name)
        return 0

    records = load_investigation(args.investigation_id, args.root)
    if not records:
        print(f"no log for {args.investigation_id} under {args.root}/")
        return 2

    runs = split_runs(records)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "header": header_of(run),
                        "outcome": outcome_of(run),
                        "profile": _profile(spans_of(run)),
                        "spans": spans_of(run),
                    }
                    for run in runs
                ],
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    for index, run in enumerate(runs, start=1):
        _print_run(run, index, len(runs))
        if args.messages:
            _print_messages(run)
    return 0


def _print_run(run: Sequence[Mapping[str, Any]], index: int, total: int) -> None:
    header = header_of(run)
    spans = spans_of(run)

    print()
    if header is not None:
        print(f"investigation {header.get('investigation_id', '?')}   run {index} of {total}")
        print(f"  trigger        {header.get('trigger', '?')}")
        # The join into the observed system's own logs. Absent means the alert rule
        # is missing the annotation, not that the agent lost it.
        print(f"  correlation_id {header.get('correlation_id') or '(none — cannot join to service logs)'}")
        window = header.get("window", {})
        print(f"  window         {window.get('start', '?')} .. {window.get('end', '?')}")
    else:
        print(f"run {index} of {total}  (no header — log truncated at the front)")

    if not spans:
        print("\n  no spans recorded — the run was not traced")
    else:
        print()
        for depth, span in tree(spans):
            print("  " + "  " * depth + _span_line(span))

    outcome = outcome_of(run)
    if outcome is not None:
        print()
        print(f"  outcome  {_outcome_line(outcome)}")

    profile = _profile(spans)
    if profile:
        print(
            "  profile  "
            f"elapsed {profile['elapsed_ms']:.1f}ms · "
            f"llm {profile['llm_ms']:.1f}ms ({profile['llm_share']:.1%}) · "
            f"tools {profile['tool_ms']:.1f}ms ({profile['tool_share']:.1%}) · "
            f"overhead {profile['overhead_ms']:.1f}ms"
        )
        print(
            "  calls    "
            f"{profile['llm_calls']} llm ({profile['cache_hits']} cached) · "
            f"{profile['tool_calls']} tool · "
            f"{profile['attempts']} attempts ({profile['retries']} retried)"
        )
        if profile["repeat_refused"]:
            print(
                f"  stuck    {profile['repeat_refused']} identical call(s) refused by "
                "the repeat guard"
            )
    print()


def _span_line(span: Mapping[str, Any]) -> str:
    duration = span.get("duration_ms")
    shown = "     —" if duration is None else f"{duration:>8.2f}ms"
    name = str(span.get("name", "?"))
    detail = _span_detail(name, span)
    status = "" if span.get("status") == "ok" else f"  [{span.get('status')}]"
    return f"{shown}  {name}{(' ' + detail) if detail else ''}{status}"


def _span_detail(name: str, span: Mapping[str, Any]) -> str:
    if name == LLM_CALL:
        bits = [str(span.get("model_id", "?"))]
        if span.get("cache_hit"):
            bits.append("cached")
        if span.get("attempts", 1) and int(span.get("attempts") or 1) > 1:
            bits.append(f"{span.get('attempts')} attempts")
        if span.get("alias_mismatch"):
            bits.append(f"served={span.get('served_model')}")
        cost = span.get("cost_native")
        if cost:
            bits.append(f"{cost} {span.get('currency', '')}".strip())
        return " ".join(bits)
    if name == TOOL_CALL:
        bits = [f"{span.get('tool', '?')} {span.get('args_hash', '')}".strip()]
        if span.get("repeat_refused"):
            bits.append("REFUSED (repeat)")
        elif span.get("is_error"):
            bits.append("error")
        return " ".join(bits)
    if name == ATTEMPT:
        bits = [f"#{span.get('attempt', '?')}"]
        if span.get("error_class"):
            bits.append(str(span.get("error_class")))
        delay = span.get("delay_before_ms") or 0
        if delay:
            bits.append(f"after {delay}ms backoff")
        return " ".join(bits)
    if name == "turn":
        bits = [f"#{span.get('turn', '?')}"]
        if span.get("stop_reason"):
            bits.append(str(span.get("stop_reason")))
        if span.get("refused"):
            bits.append(f"refused={span.get('refused')}")
        return " ".join(bits)
    if name == "investigation":
        return str(span.get("trigger", ""))
    return ""


def _outcome_line(outcome: Mapping[str, Any]) -> str:
    if outcome.get("kind") == "Done":
        report = outcome.get("report") or {}
        if report:
            root_cause = report.get("root_cause", "(no root_cause field)")
            return f"Done — {root_cause}  (confidence: {report.get('confidence', '?')})"
        return f"Done — {(outcome.get('text') or '')[:160]}"
    return f"Aborted({outcome.get('reason', '?')}) — {outcome.get('detail', '')}"


def _profile(spans: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Same shape as `Trace.profile()`, computed from the file.

    Duplicated deliberately rather than reconstructing `Span` objects: replay must
    work on a log written by an older version whose spans no longer round-trip into
    the current dataclass.
    """
    if not spans:
        return {}
    roots = [s for s in spans if not s.get("parent_id")]
    elapsed = max((r.get("duration_ms") or 0.0) for r in roots) if roots else 0.0
    llm = [s for s in spans if s.get("name") == LLM_CALL]
    tools = [s for s in spans if s.get("name") == TOOL_CALL]
    attempts = [s for s in spans if s.get("name") == ATTEMPT]
    llm_ms = sum(s.get("duration_ms") or 0.0 for s in llm)
    tool_ms = sum(s.get("duration_ms") or 0.0 for s in tools)
    return {
        "elapsed_ms": elapsed,
        "llm_ms": llm_ms,
        "tool_ms": tool_ms,
        # Concurrent tool calls overlap, so the summed work can exceed elapsed —
        # clamped at zero rather than reported as negative overhead.
        "overhead_ms": max(0.0, elapsed - llm_ms - tool_ms),
        "llm_share": (llm_ms / elapsed) if elapsed else 0.0,
        "tool_share": (tool_ms / elapsed) if elapsed else 0.0,
        "llm_calls": len(llm),
        "tool_calls": len(tools),
        "attempts": len(attempts),
        "retries": max(0, len(attempts) - len(llm)),
        "cache_hits": sum(1 for s in llm if s.get("cache_hit")),
        "repeat_refused": sum(1 for s in tools if s.get("repeat_refused")),
    }


def _print_messages(run: Sequence[Mapping[str, Any]]) -> None:
    messages = rebuild_messages(run)
    print(f"  messages  ({len(messages)} rebuilt from the event stream)")
    for message in messages:
        for block in message.content:
            kind = getattr(block, "type", "?")
            body = (
                getattr(block, "text", None)
                or getattr(block, "content", None)
                or json.dumps(getattr(block, "input", {}), ensure_ascii=False)
            )
            label = f"{message.role}/{kind}"
            print(f"    {label:<22} {_one_line(str(body))}")
    print()


def _one_line(text: str, limit: int = 140) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"
