# Roadmap

Six-week build plan for sre-agent, Tier 1.5 target. Each week is 5-8 lessons (`L1`, `L2`, ...). Each lesson has a **concept** (why it exists) and a **deliverable** (code, config, or docs). Update status here as you go — this file is the single source of truth for progress and the handoff point between sessions.

## Status legend

- `[ ]` not started
- `[~]` in progress
- `[x]` done
- `[⏭]` intentionally skipped (with note)

## Session handoff protocol

1. Update this file's status marks before ending a session.
2. Add a one-line note under **Current pointer** at the bottom.
3. Next session opens by re-reading this file and jumping to the next `[ ]`.

---

## Week 1 — Mock environment (the target the agent will observe)

Goal: a docker-compose local stack that produces realistic metrics + logs + can be induced to fail.

| L | Concept | Deliverable | Status |
|---|---|---|---|
| L1 | Mock env vs agent boundary; 3 pillars (metrics/logs/traces); Prometheus pull vs push; ClickHouse/Loki/ES tradeoff | (conceptual, no artifact) | `[x]` |
| L2 | FastAPI ≈ Spring Boot; ContextVar as async-safe MDC; prometheus_client primitives; middleware-owned observability | `mock/services/checkout/{app.py, requirements.txt, Dockerfile}` | `[x]` |
| L3 | Counter/Gauge/Histogram/Summary internals; label cardinality math; PromQL essentials; RED / USE frameworks | (conceptual — informs future metric design) | `[x]` |
| L4 | Prometheus scrape config, ClickHouse schema (LowCardinality, partition, sort key, TTL), Vector 3-stage pipeline, log timestamp normalization | `mock/{docker-compose.yml, prometheus/, clickhouse/init.sql, vector/vector.yaml}` | `[x]` |
| L5 | Multi-service call graph; httpx client; correlation_id propagation via header; route-pattern label to avoid cardinality explosion; client-side downstream metrics | `mock/services/{gateway, payment, inventory}/` + updates to checkout to call downstreams | `[ ]` |
| L6 | Fault injection framework: admin endpoint on each service; fault types (error_rate, latency, dependency_fail, pool_exhaust); YAML scenarios; simple load generator | `mock/services/_shared/fault.py`, `mock/scenarios/*.yaml`, `mock/scripts/load.py` | `[ ]` |

**Week 1 exit criteria**: can run 3-5 scenarios via CLI, see metrics spike in Prometheus and error logs in ClickHouse.

---

## Week 2 — Agent core skeleton

Goal: an agent that can accept an alert, walk its 5-node LangGraph, and produce a stub report. No intelligence yet — just the plumbing works end-to-end.

| L | Concept | Deliverable | Status |
|---|---|---|---|
| L1 | MCP protocol basics; stdio transport; tool schema (name, description, params, side_effect, cost_hint); why 2 servers not 1 | `agent/mcp/observability_mcp/` skeleton with 3 read tools stubbed | `[ ]` |
| L2 | Query implementation: PromQL against Prometheus HTTP API; SQL against ClickHouse HTTP; response shape design (structured, not raw) | Real `query_metrics`, `query_logs`, `get_service_topology` calling live mock env | `[ ]` |
| L3 | LLM Gateway module: model routing by phase label, prompt cache stable prefix, retry, cost accounting, Langfuse forwarding | `agent/llm_gateway.py` (~200 lines), unit tests, single-tenant | `[ ]` |
| L4 | LangGraph state schema; TypedDict IncidentState; conditional edges; per-node tool budget enforcement | `agent/graph/{state.py, graph.py}` with 5 empty nodes wired | `[ ]` |
| L5 | End-to-end smoke: alert → workflow start → walk graph → stub report → post to CLI | `agent/entrypoint.py` + `alertctl trigger` CLI | `[ ]` |

**Week 2 exit criteria**: `alertctl trigger --scenario S1` runs the empty graph, produces a stub report, and a Langfuse trace shows every step.

---

## Week 3 — Agent intelligence

Goal: real reasoning per node. Structured outputs. Real prompts. Sonnet reasoning working.

| L | Concept | Deliverable | Status |
|---|---|---|---|
| L1 | Prompt structure: system + phase + retrieved context + scratchpad; XML-tag data isolation (L1 of SECURITY.md defense) | `agent/prompts/*.jinja2` — one per node | `[ ]` |
| L2 | Structured output via Pydantic; forced schema in Anthropic tool_use; retry on schema fail | `agent/graph/outputs.py`; every node returns typed object | `[ ]` |
| L3 | Model routing in practice: Haiku for triage, Sonnet for reasoning, Opus for verify; measure cost delta | Config in `llm_gateway.py`, cost logs in Postgres | `[ ]` |
| L4 | Parallel tool calls in `collect` phase; asyncio.gather across MCP calls; timing wins | `agent/graph/nodes/collect.py` with parallel section | `[ ]` |
| L5 | Adversarial verify: for each hypothesis, spawn refute sub-agent; verdict schema; kill-on-majority-refute | `agent/graph/nodes/verify.py`; report includes verdicts | `[ ]` |
| L6 | Temporal workflow: wrap LangGraph run as a workflow; kill-worker test; human signal for approval | `agent/workflow.py`, `docker-compose.yml` add temporal | `[ ]` |

**Week 3 exit criteria**: 5 hand-authored incidents end-to-end produce plausible root-cause reports; Temporal survives a worker kill mid-verify.

---

## Week 4 — Memory + retrieval

Goal: agent has episodic memory (past incidents) and semantic memory (runbooks). Prefetch in triage; RAG in report generation.

| L | Concept | Deliverable | Status |
|---|---|---|---|
| L1 | Postgres + pgvector setup; embedding provider (Voyage / OpenAI text-embed / BGE self-host tradeoff) | `agent/memory/schema.sql`, embed client wrapper | `[ ]` |
| L2 | Hybrid retrieval: dense + BM25 (Postgres full-text) + metadata filter; scoring blend; benchmarking | `agent/memory/retrieval.py` + `EVAL.md` retrieval benchmarks | `[ ]` |
| L3 | Incident schema: what fields to embed vs store literal; deduplication policy; TTL on stale memories | `agent/memory/incident.py`; incident writer at end of workflow | `[ ]` |
| L4 | Runbook RAG: chunking, storage, retrieval; when to fetch (report phase) | `agent/memory/runbook.py`; sample runbooks in `mock/runbooks/*.md` | `[ ]` |
| L5 | Triage prefetch: on alert receipt, async fetch top-3 similar incidents; latency budget | `agent/graph/nodes/triage.py` speculative retrieval | `[ ]` |

**Week 4 exit criteria**: eval shows incidents that match a similar past incident get resolved 30%+ faster or with 30%+ higher accuracy.

---

## Week 5 — Security + evaluation

Goal: five-layer prompt injection defense implemented; 30-50 case golden set; nightly regression running.

| L | Concept | Deliverable | Status |
|---|---|---|---|
| L1 | SECURITY.md Layer 1 (XML data isolation) + sanitization: control chars, zero-width, fake closing tags, length caps, NFC normalization | `agent/context_builder.py` | `[ ]` |
| L2 | SECURITY.md Layer 2 (structured output constraints); enum on target_service; bounded list sizes; retry-on-fail | Enforced in every node; schema-fail rate logged | `[ ]` |
| L3 | SECURITY.md Layer 3 (second-model reviewer); different-family LLM; PROCEED/FLAG/BLOCK verdict | `agent/graph/nodes/review.py` + `prompts/reviewer.jinja2` | `[ ]` |
| L4 | SECURITY.md Layer 4 (gate): tool side-effect classes; native dry-run integration; Temporal signal for approval; audit log | `agent/gate.py`; audit_log table | `[ ]` |
| L5 | SECURITY.md Layer 5 (egress filter); URL whitelist; image stripping; secret regex; report as Pydantic-serialized Markdown | `agent/egress.py` | `[ ]` |
| L6 | Golden set construction: 10 easy + 15 medium + 15 hard + 5 pathological + 10 adversarial; per-difficulty accuracy; per-layer bypass rate | `eval/golden/*.json`, `eval/run.py`, GitHub Actions nightly | `[ ]` |
| L7 | LLM judge design: rubric, judge model selection (must differ from agent), kappa validation against manual labels | `eval/judge.py`, `docs/JUDGE_VALIDATION.md` | `[ ]` |

**Week 5 exit criteria**: nightly eval runs green; adversarial success rate = 100%; multi-dimensional metrics reported; regression triggers on PR alerts.

---

## Week 6 — Polish + demo

Goal: presentable to interviewers. Runnable end-to-end. Recorded demo. Polished README.

| L | Concept | Deliverable | Status |
|---|---|---|---|
| L1 | Slack integration: webhook-driven alerts in; report + gate approval prompts out; correlation with Langfuse trace URL | `agent/notify/slack.py`, Slack app config | `[ ]` |
| L2 | Cost / latency dashboard: per-phase histogram, per-incident total, model-mix breakdown | `agent/dashboards/` (Grafana JSON or Langfuse views) | `[ ]` |
| L3 | 3-minute demo video: pick 2 scenarios, script the walkthrough, screen record | `README.md` embeds video | `[ ]` |
| L4 | Fill `INTERVIEW_CHECKLIST.md` end-to-end with real numbers | All 10 sections have data points | `[ ]` |
| L5 | Public repo hygiene: remove commented dead code, tighten README hook, write ARCHITECTURE.md addendum with real numbers, add LICENSE | Cleaned repo | `[ ]` |

**Week 6 exit criteria**: someone unfamiliar can clone the repo, run `make demo`, watch a real incident get resolved, and read a README that answers "what did I just see" in 60 seconds.

---

## Cross-week tracks (run in parallel, small time slices)

These aren't in a specific week — bit-by-bit each week:

- **Interview checklist**: after every substantive change, add a data point / decision line to `INTERVIEW_CHECKLIST.md`
- **Tradeoffs**: any non-obvious decision → a new entry in `TRADEOFFS.md` with Alternatives + Why + Cost + Reconsider-when
- **Langfuse observability**: from Week 2 L3 onward, every LLM call is traced — treat gaps as bugs

---

## Current pointer

**Session end date**: 2026-07-19  
**Last completed**: Week 1 L4 (docker-compose + Prometheus + ClickHouse + Vector, tested end-to-end)  
**Next up**: Week 1 L5 (add gateway/payment/inventory + correlation_id propagation via httpx headers)  
**Blockers**: none  
**Notes for next session**:
- Mock env local stack tested and healthy (all 4 containers running)
- User has completed Q&A checkpoints for L1-L3; L3 revealed weak spots on server-side vs client-side metric distinction and gauge-vs-counter (revisit when writing gateway/payment client-side metrics in L5)
- Teaching mode reminders: concise responses, Socratic/decision-first, Java analogies, interview framing on every decision
