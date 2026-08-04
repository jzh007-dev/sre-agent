# sre-agent

An **incident copilot** for SRE — an AI agent that receives alerts, autonomously gathers signals from observability tools, forms hypotheses about root cause, verifies them, and posts a structured report back to on-call channels.

> Positioning: personal project engineered as **Tier 1.5** — a single-deployable POC that carries the architectural patterns of a mid-scale (Tier 2) production system. Simple where scale doesn't matter; sophisticated where design taste matters.

---

## Why this project

Most "AI + observability" demos stop at "chat with logs." This project is designed to demonstrate:

1. **Agent architecture with an explicit boundary** — a deterministic pipeline containing a ReAct kernel, where the split between "code decides" and "model decides" is written down and enforced by a test.
2. **A written diagnostic methodology** — numbered rules, each mapped to a golden case or a deterministic checker, rather than reasoning outsourced to whatever the model infers from context. See [DIAGNOSIS.md](./DIAGNOSIS.md).
3. **Tool design discipline** — side-effect classes, gates, cost hints, and failure containment (a dead backend is evidence, not a crash).
4. **Evaluation that can fail informatively** — nightly regression, LLM-as-judge with a kappa-gated anchor set, and metrics that watch the watchers: `precompute_override_rate` catches the case where the model contributes nothing while accuracy looks excellent.
5. **Cost & latency engineering** — one gateway chokepoint for routing, cache breakpoints, cost accounting, and budget enforcement.
6. **Observability of the agent itself** — four span levels with durations, joined to the observed system by the alert's own `correlation_id`, and a replayable JSONL log per investigation. The section that documents it starts with an audit of what was instrumented and *not* wired, and what each gap cost ([TRADEOFFS §42](./TRADEOFFS.md#42-traceability-one-id-four-sinks--and-an-honest-audit-of-what-is-currently-wired)).

The full rationale, tradeoffs, and interview-mapped depth points are in the docs below.

---

## Documents

| Doc | Purpose |
|---|---|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Shape, topology, who-decides-what, repository layout, data flow, scale targets, Tier 2/3 evolution |
| [DIAGNOSIS.md](./DIAGNOSIS.md) | The reasoning procedure — precompute rules, elimination rules with pruning conditions, the in-model boundary, the report contract |
| [docs/CALL_WALKTHROUGH.md](./docs/CALL_WALKTHROUGH.md) | One LLM call traced nine ways — cache hit, retry, breaker open, budget refusal, context overflow, repetition loop — with each file's role, and a run replayed off disk |
| [TRADEOFFS.md](./TRADEOFFS.md) | Every key decision with A/B alternatives and reasoning |
| [SECURITY.md](./SECURITY.md) | Threat model, five-layer prompt injection defense, known gaps |
| [EVAL.md](./EVAL.md) | Metrics, golden set spec, judge design, regression methodology |
| [RAG.md](./RAG.md) | Runbook retrieval subsystem — Tier 1.5 build + Tier 2 evolution triggers |
| [INCIDENT_WALKTHROUGH.md](./INCIDENT_WALKTHROUGH.md) | One incident traced end-to-end through every subsystem, with numbers |
| [INTERVIEW_CHECKLIST.md](./INTERVIEW_CHECKLIST.md) | 10 depth dimensions with per-item checklist for interview prep |
| [docs/ROADMAP.md](./docs/ROADMAP.md) | 7-week build plan, five pillars, lesson-by-lesson status, open gaps, session handoff pointer |

---

## Status

**Week 1 done** — the target environment the agent observes is live: 13 real containers (7 microservices + Prometheus + AlertManager + ClickHouse + Vector + Redis), real fault injection, and 8 golden cases that reproduce real incidents end-to-end.

**Week 2 in progress** — the agent skeleton. Loop, gateway (routing / cache / cost / budget), the traceability spine, and the trigger registry with layered alert dedup are in: 363 tests, all offline except the two commands that deliberately hit a live provider. See [docs/ROADMAP.md](./docs/ROADMAP.md) for the 7-week plan and current pointer.

## Shape

A **linear pipeline shell containing a ReAct kernel** — not a DAG, not a bare loop:

```
ingress → ① route → ② preprocess → ③ loadout → ④ ReAct loop → ⑤ parse → ⑥ fanout
                                                     ↑
                              only step ④ is non-deterministic
```

The LLM has **freedom of ordering, not freedom over resources or contracts**: it decides what to query next; the code decides what it may query, for how long, over which time window, in what output shape, and who receives the result. See [ARCHITECTURE.md](./ARCHITECTURE.md#who-decides-what).

## Stack

- **Language**: Python 3.11+
- **Agent shape**: 6-step deterministic harness + ReAct loop, no orchestration framework ([TRADEOFFS §22](./TRADEOFFS.md#22-agent-architecture-agent-loop-over-workflow-graph), [§23](./TRADEOFFS.md#23-harness-deterministic-pipeline-around-the-agent-loop-refines-22))
- **Entry modes**: alert webhook (real) / chat / scheduled patrol — one `Investigation` type ([§25](./TRADEOFFS.md#25-trigger-registry-alert-is-one-entry-mode-of-three))
- **LLM gateway**: routing by task nature, `cache_control` placement, cost accounting, budget enforcement, response cache, Langfuse tracing ([§3](./TRADEOFFS.md#3-llm-gateway-in-process-wrapper-not-litellmportkey-service))
- **LLM providers**: DeepSeek / Qwen (DashScope) / Moonshot (Kimi) through one OpenAI-compat adapter; Anthropic as the different-family class required by the judge and reviewer designs. Routing pins **concrete models, never provider aliases** — an alias breaks per-model pricing and silently invalidates eval's `model_version` key
- **Cost accounting**: per billing currency, never converted; DeepSeek rates **verified against the account's own invoice** (`srectl prices` recomputes each billed day to ten decimal places)
- **Integrations**: one YAML + one MCP server each — **zero Python per integration** ([§24](./TRADEOFFS.md#24-integrations-are-configuration-not-code))
- **Tool layer**: MCP over stdio, split by side-effect class (read-only vs WRITE)
- **Durability**: per-investigation JSONL append log; Temporal is out of scope, documented as a Tier 2 seam ([§32](./TRADEOFFS.md#32-temporal-is-out-of-scope-not-deferred))
- **Memory**: Postgres + pgvector, namespaced per integration
- **Cache**: Redis (alert fingerprints, gateway response cache)
- **Trace / eval**: Langfuse Cloud + `eval/run.py` from Week 2 onward
- **Observability pillars**: metrics (Prometheus) + logs (ClickHouse) + traces (Tempo, W3 — scoped to per-request causal chains and kept only if it earns a measured margin, [§29](./TRADEOFFS.md#29-trace-scope-per-request-causal-chains-not-a-third-pillar))
- **Target environment**: docker-compose, 13 real containers — 7 microservices + Prometheus + AlertManager + ClickHouse + Vector + Redis + redis-exporter

## Acceptance rule

> **Every component ships with a number, or it doesn't ship.**

Breadth of half-features is the failure mode this project is designed to avoid. Each week's exit criteria is a row of measured values, not a list of files that exist — see [EVAL.md](./EVAL.md#metric-growth-by-week).

## License

MIT (planned).
