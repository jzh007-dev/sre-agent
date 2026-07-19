# sre-agent

An **incident copilot** for SRE — an AI agent that receives alerts, autonomously gathers signals from observability tools, forms hypotheses about root cause, verifies them, and posts a structured report back to on-call channels.

> Positioning: personal project engineered as **Tier 1.5** — a single-deployable POC that carries the architectural patterns of a mid-scale (Tier 2) production system. Simple where scale doesn't matter; sophisticated where design taste matters.

---

## Why this project

Most "AI + observability" demos stop at "chat with logs." This project is designed to demonstrate:

1. **Agent architecture** beyond a single tool-calling loop — a workflow-skeleton with agentic sub-nodes.
2. **Structured state** and durable execution — incidents are long-running, checkpointed, resumable.
3. **Tool design discipline** — tools have side-effect classes, gates, cost hints.
4. **Serious evaluation** — nightly regression, LLM-as-judge, multi-dimensional metrics.
5. **Cost & latency engineering** — model routing, prompt caching, parallel tool calls.
6. **Observability of the agent itself** — every decision is traced and replayable.

The full rationale, tradeoffs, and interview-mapped depth points are in the docs below.

---

## Documents

| Doc | Purpose |
|---|---|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | High-level design, component map, data flow, memory layering, scale targets, Tier 2/3 evolution paths |
| [TRADEOFFS.md](./TRADEOFFS.md) | Every key decision with A/B alternatives and reasoning |
| [SECURITY.md](./SECURITY.md) | Threat model, five-layer prompt injection defense, known gaps |
| [EVAL.md](./EVAL.md) | Metrics, golden set spec, judge design, regression methodology |
| [RAG.md](./RAG.md) | Runbook retrieval subsystem — Tier 1.5 build + Tier 2 evolution triggers |
| [INCIDENT_WALKTHROUGH.md](./INCIDENT_WALKTHROUGH.md) | One incident traced end-to-end through every subsystem, with numbers |
| [INTERVIEW_CHECKLIST.md](./INTERVIEW_CHECKLIST.md) | 10 depth dimensions with per-item checklist for interview prep |
| [docs/ROADMAP.md](./docs/ROADMAP.md) | 6-week build plan, lesson-by-lesson status, session handoff pointer |

---

## Status

Currently in **design phase**. Docs first, code second. Implementation kicks off after these four documents are reviewed and stable.

## Stack (planned)

- **Language**: Python 3.11+
- **Agent framework**: LangGraph
- **Workflow durability**: Temporal (single worker, self-hosted or Temporal Cloud free tier)
- **LLM**: Anthropic Claude (Haiku 4.5 / Sonnet 5 / Opus 4.8, routed by phase)
- **Tool layer**: 2 MCP servers (`observability-mcp`, `deploy-mcp`)
- **State + memory**: Postgres + pgvector
- **Cache**: Redis
- **Trace / eval**: Langfuse Cloud
- **Mock environment**: docker-compose (4-5 fake microservices + Prometheus + Loki + fault injection scripts)

## License

MIT (planned).
