"""Tool protocol + 3 stub tool implementations for Week 2 L1.

Real implementations behind these names land at Week 2 L3 (MCP transport +
canned data) and Week 3 (real Prometheus / ClickHouse queries).

The tool set here matches what the loop's stub LLM script expects:
- query_metrics    — Prometheus range query by PromQL
- query_logs       — ClickHouse query by service + level + range
- search_runbook   — RAG lookup by service + symptom
"""
from __future__ import annotations

import json
from typing import Any, Protocol


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]

    async def run(self, **kwargs: Any) -> str:
        """Execute the tool. Returns a serialized string (JSON usually) that
        becomes the `content` of a ToolResultBlock."""
        ...


class StubMetricsTool:
    name = "query_metrics"
    description = "Query time-series metrics from Prometheus by PromQL."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "promql": {"type": "string", "description": "PromQL query"},
            "duration": {"type": "string", "description": "Range like '5m'", "default": "5m"},
        },
        "required": ["promql"],
    }

    async def run(self, **kwargs: Any) -> str:
        return json.dumps({
            "series": [
                {
                    "labels": {"service": "auth"},
                    "values": [["1690000000", "0.15"], ["1690000060", "0.18"]],
                }
            ],
            "_stub": True,
        })


class StubLogsTool:
    name = "query_logs"
    description = "Query logs from ClickHouse by service and time range."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "level": {"type": "string", "enum": ["error", "warn", "info"]},
            "limit": {"type": "integer", "default": 50},
        },
        "required": ["service"],
    }

    async def run(self, **kwargs: Any) -> str:
        return json.dumps({
            "logs": [
                {
                    "ts": "2026-07-27T12:00:00Z",
                    "level": "error",
                    "msg": "redis SETEX failed: OOM command not allowed",
                },
            ],
            "_stub": True,
        })


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

    async def run(self, **kwargs: Any) -> str:
        return json.dumps({
            "chunks": [
                "auth-svc common failures: Redis saturation, DB slow query, upstream JWT verifier down.",
                "For Redis OOM: check redis_memory_used_bytes and inspect maxmemory-policy setting.",
            ],
            "_stub": True,
        })


def default_tool_registry() -> dict[str, Tool]:
    return {
        "query_metrics": StubMetricsTool(),
        "query_logs": StubLogsTool(),
        "search_runbook": StubRunbookTool(),
    }


def tool_schemas(tools: dict[str, Tool]) -> list[dict[str, Any]]:
    """Convert a Tool registry to the JSON-schema list an LLM expects on tools=[...]."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools.values()
    ]
