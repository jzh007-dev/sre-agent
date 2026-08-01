"""Stub tools — canned returns behind real signatures.

Real implementations arrive in two steps: W2 L5 puts these behind actual MCP
stdio transport (still canned), and W3 L2 swaps the canned bodies for real
PromQL against Prometheus and real SQL against ClickHouse. Until then the shapes
are what matter, because the loop, the budget accounting, the dispatch failure
paths and the eval harness can all be exercised without a single real query.

Every tool echoes the `window` it was given. That is not padding: a real query
backend does report the range it actually covered, and it makes window
propagation observable end-to-end instead of a promise.
"""
from __future__ import annotations

import json
from typing import Any

from ..core.investigation import Window
from .protocol import Tool, ToolMeta


class StubMetricsTool:
    name = "query_metrics"
    description = (
        "Query time-series metrics by PromQL. The time range is fixed by the "
        "investigation and cannot be passed in."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "promql": {"type": "string", "description": "PromQL expression"},
        },
        "required": ["promql"],
    }
    meta = ToolMeta(side_effect="READ", cost_hint="cheap", timeout_s=15.0)

    async def run(self, *, window: Window, **kwargs: Any) -> str:
        return json.dumps(
            {
                "query": kwargs.get("promql"),
                "window": window.as_tool_args(),
                "series": [
                    {
                        "labels": {"service": "auth"},
                        "values": [["1690000000", "0.15"], ["1690000060", "0.18"]],
                    }
                ],
                "_stub": True,
            },
            ensure_ascii=False,
        )


class StubLogsTool:
    name = "query_logs"
    description = (
        "Query logs by service and level. The time range is fixed by the "
        "investigation and cannot be passed in."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "level": {"type": "string", "enum": ["error", "warn", "info"]},
            "limit": {"type": "integer", "default": 50},
        },
        "required": ["service"],
    }
    meta = ToolMeta(side_effect="READ", cost_hint="expensive", timeout_s=30.0)

    async def run(self, *, window: Window, **kwargs: Any) -> str:
        return json.dumps(
            {
                "service": kwargs.get("service"),
                "window": window.as_tool_args(),
                "logs": [
                    {
                        "ts": "2026-07-27T12:00:00Z",
                        "level": "error",
                        "msg": "redis SETEX failed: OOM command not allowed",
                    }
                ],
                "_stub": True,
            },
            ensure_ascii=False,
        )


class StubRunbookTool:
    name = "search_runbook"
    description = "Search runbook chunks by service and symptom."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "symptom": {"type": "string"},
        },
        "required": ["service"],
    }
    meta = ToolMeta(side_effect="READ", cost_hint="cheap", timeout_s=10.0)

    async def run(self, *, window: Window, **kwargs: Any) -> str:
        return json.dumps(
            {
                "chunks": [
                    "auth-svc common failures: Redis saturation, DB slow query, "
                    "upstream JWT verifier down.",
                    "For Redis OOM: check redis_memory_used_bytes and inspect "
                    "the maxmemory-policy setting.",
                ],
                "_stub": True,
            },
            ensure_ascii=False,
        )


class SubmitReportTool:
    """The terminal tool — calling it concludes the investigation.

    Terminating on a tool call rather than on `end_turn` buys three things: the
    report is structured by construction, the stop signal means "delivered"
    instead of "ran out of things to say", and this handler becomes a place that
    can *refuse* a delivery. W3 L6 uses that refusal to make verification
    mandatory — a report with no refute on record comes back as an error result
    and the model has to go do the work.

    The schema hardens into a Pydantic model in `core/report.py` at W3 L5. Here
    it is a shape, not yet a contract.
    """

    name = "submit_report"
    description = (
        "Deliver the final incident report and end the investigation. "
        "Call this exactly once — when the evidence supports a conclusion, or "
        "when it does not, with confidence set to 'unknown'."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "root_cause": {"type": "string"},
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low", "unknown"],
                "description": "'unknown' is a valid answer; do not guess to fill it.",
            },
            "evidence": {"type": "array", "items": {"type": "string"}},
            "ruled_out": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["root_cause", "confidence"],
    }
    meta = ToolMeta(side_effect="READ", cost_hint="cheap", timeout_s=5.0, terminal=True)

    async def run(self, *, window: Window, **kwargs: Any) -> str:
        # Acceptance is unconditional at L2. W3 L6 makes it conditional on a
        # refute being on record, which is why this returns a verdict rather
        # than an empty acknowledgement.
        return json.dumps({"accepted": True}, ensure_ascii=False)


def default_tool_registry() -> dict[str, Tool]:
    """The L2 bundle. W2 L5 replaces this with per-integration assembly."""
    tools: list[Tool] = [
        StubMetricsTool(),
        StubLogsTool(),
        StubRunbookTool(),
        SubmitReportTool(),
    ]
    return {tool.name: tool for tool in tools}
