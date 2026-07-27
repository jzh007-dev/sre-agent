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
| L5 | Multi-service call graph; httpx client; correlation_id propagation via header; route-pattern label to avoid cardinality explosion; client-side downstream metrics | `mock/services/{gateway, payment, inventory}/` + updates to checkout to call downstreams | `[x]` |
| L6 | Alerting layer + fault injection: real `AlertManager` with SLO-based rules; per-service `/admin/faults` (types: `error_rate`, `latency_ms`, `log_pattern_emit`, `dependency_fail`); PD-shaped `incident-tracker` closing the webhook loop; real Redis + `redis_exporter` (infra deps are real components, not Python mocks). Recalibration mid-lesson dropped a Python session-cache mock and infra-layer paging in favor of the production-standard shape. | `mock/alertmanager/`, `mock/prometheus/alerts.yml`, `mock/services/_shared/fault.py`, `mock/services/incident_tracker/`, real `redis` + `redis-exporter` in compose | `[x]` |
| L7 | Golden case structure + case runner: 3-file case layout (`alert.json` shaped like an AlertManager webhook + `setup.yaml` for reproducible fault application + `expected.yaml` for LLM-judge eval), auth-svc for user-facing SLO surface, deploy-history mock for change-induced cases, 3 story cases from real production incidents (patterned across `RES / DEP / CHANGE+DATA`) + 5 primitive cases adapted from OpenSRE chaos experiments | `eval/golden/GS-*/`, `mock/services/auth/`, `mock/services/deploy_history/`, `mock/scripts/case_runner.py` | `[x]` |

**Week 1 exit criteria**: `python mock/scripts/case_runner.py <case-id>` applies the case's `setup.yaml` (faults + Redis config + deploy fixture), waits for the expected AlertManager alert to fire, and dumps the tracked incident from `incident-tracker` — all end-to-end against real Prometheus + real AlertManager + real Redis. 3+ story cases + 5+ primitive cases pass; each case documents its expected root cause for the future Week 2+ agent eval.

---

## Week 2 — Agent core skeleton

Goal: an agent that can accept an alert, walk a `while`-loop of LLM-driven tool selection, and produce a stub report. No intelligence yet — just the plumbing works end-to-end.

**Architectural pivot from original plan** (see [TRADEOFFS §22](../TRADEOFFS.md)): Week 2 was originally scoped as a 5-node LangGraph. Mid-planning we reversed that decision — mainstream agents (Claude Code / Cursor / Devin / OpenAI Assistants) are all `while` + LLM-driven tool-use loops, and SRE root cause analysis is open-ended enough that "which tool to call next" cannot be enumerated in advance. LangGraph's phase graph is the wrong shape; a loop is the right shape. `experiment/langgraph` branch preserves the pre-pivot HEAD for later side-by-side comparison. See TRADEOFFS §20 (agent-code middleware-agnostic) — Week 2 is where that principle first becomes code.

| L | Concept | Deliverable | Status |
|---|---|---|---|
| L1 | Agent loop shape: `while` + injectable `LLM` protocol + injectable tool dict; `messages` array **is** state (no separate state class); `stop_reason` drives termination (`end_turn` or `tool_use`); provider-agnostic domain types (`Message` / `TextBlock` / `ToolUseBlock` / `ToolResultBlock` / `Response`) so multi-provider adapters can land in L2. Stub LLM script returns 3 canned turns; 3 stub tools return canned JSON. | `agent/{loop.py, llm/{types,protocol,stub}.py, tools.py}` + `tests/agent/test_loop.py` proving 3-turn stub loop terminates with non-empty report | `[ ]` |
| L2 | Provider adapter: Anthropic Messages API; content-block shape (text / tool_use); tool_result convention (in `role=user` message); streaming as async iterator of SSE events; Langfuse trace per call. Model choice is single (Sonnet) — model routing dropped from Week 2 because phase concept was dropped with the graph pivot; cost optimization returns in Week 3+ if needed. | `agent/llm/anthropic.py` + test with real Anthropic call against a canned prompt | `[ ]` |
| L3 | MCP protocol basics; stdio transport; tool schema (name, description, params, side_effect, cost_hint); **why 2 servers not 1** (observability read-only vs deploy has WRITE, deferred to Week 5). Three `observability_mcp` tools as **MCP-transport stubs**: real MCP protocol wiring, canned JSON returns. Real PromQL / ClickHouse queries land in Week 3. | `agent/mcp/observability_mcp/` with `query_metrics`, `query_logs`, `get_service_topology` all stub returns behind real MCP transport | `[ ]` |
| L4 | FastAPI ingress + `alertctl trigger` CLI. `POST /alerts` accepts an alert.json (shape matches `eval/golden/GS-*/alert.json`), generates `incident_id`, starts a background `run_incident(alert)` task, returns 202 with incident_id. **No Temporal in Week 2** (see [TRADEOFFS §2 revision](../TRADEOFFS.md)) — in-process asyncio task + in-memory incident registry is sufficient for 5-15 min incident windows. Temporal returns Week 5-6 if durability becomes a shipping requirement. | `agent/entrypoint.py`, `alertctl/` CLI | `[ ]` |
| L5 | End-to-end smoke: `alertctl trigger --scenario GS-RES-001-redis-oom` sends the case's `alert.json` to the ingress, the loop starts, real Anthropic call decides which tools to call, real MCP transport dispatches to stub-return tools, a stub Markdown report emerges, Langfuse trace shows every LLM call + tool call. **This is the hello-world agent — no real reasoning quality yet, but every seam is production-shaped.** | working `alertctl trigger` command; Langfuse trace URL printed; stub report on stdout | `[ ]` |

**Week 2 exit criteria**: `alertctl trigger --scenario GS-RES-001-redis-oom` runs the agent loop end-to-end with a real Anthropic call and real MCP transport (stub tool returns), produces a stub Markdown report, and a Langfuse trace shows every LLM call + tool call. No real reasoning quality yet — Week 3's job is to swap stub tool returns for real Prometheus/ClickHouse queries and land tuned prompts.

---

## Week 3 — Agent intelligence

Goal: real reasoning inside the loop. System prompt tuned. Structured outputs where they help. Parallel tool calls. Adversarial verify path (design TBD).

> **This section is pre-pivot and needs re-planning.** The original Week 3 table below was structured around 5 phase nodes (triage/collect/hypothesize/verify/report). After the Week 2 pivot to a `while` loop (see [TRADEOFFS §22](../TRADEOFFS.md#22-agent-architecture-agent-loop-over-workflow-graph)), several concepts need reshaping: (a) "one prompt per node" collapses into a single system prompt + tool schemas, (b) "model routing by phase" is dropped (no phases) — role-based routing may return if Week 5 eval or Week 3 report generation creates the need, (c) "wrap LangGraph in Temporal" no longer applies — durability is a Week 5-6 concern per [TRADEOFFS §2 revision](../TRADEOFFS.md#2-durable-execution-temporal-not-plain-async). Re-plan this table at the end of Week 2 when the loop has run against a real LLM and we know what real failure modes look like.

| L | Concept (pre-pivot; TO BE RE-PLANNED) | Deliverable (pre-pivot) | Status |
|---|---|---|---|
| L1 | Prompt structure: system + phase + retrieved context + scratchpad; XML-tag data isolation (L1 of SECURITY.md defense) | `agent/prompts/*.jinja2` — one per node | `[ ]` |
| L2 | Structured output via Pydantic; forced schema in Anthropic tool_use; retry on schema fail | `agent/graph/outputs.py`; every node returns typed object | `[ ]` |
| L3 | Model routing in practice: Haiku for triage, Sonnet for reasoning, Opus for verify; measure cost delta | Config in `llm_gateway.py`, cost logs in Postgres | `[ ]` |
| L4 | Parallel tool calls in `collect` phase; asyncio.gather across MCP calls; timing wins | `agent/graph/nodes/collect.py` with parallel section | `[ ]` |
| L5 | Adversarial verify: for each hypothesis, spawn refute sub-agent; verdict schema; kill-on-majority-refute | `agent/graph/nodes/verify.py`; report includes verdicts | `[ ]` |
| L6 | ~~Temporal workflow: wrap LangGraph run as a workflow; kill-worker test; human signal for approval~~ **Removed** — Temporal deferred to Week 5-6 (see [TRADEOFFS §2 revision](../TRADEOFFS.md#2-durable-execution-temporal-not-plain-async)) | — | `[⏭]` |

**Week 3 exit criteria** (pre-pivot; TO BE RE-PLANNED): 5 hand-authored incidents end-to-end produce plausible root-cause reports; Temporal survives a worker kill mid-verify. → Revised target: 5+ golden cases end-to-end with real LLM + real MCP produce plausible root-cause reports (Temporal criterion moves out).

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

**Session end date**: 2026-07-27  
**Last completed**: **Week 2 architectural pivot** (docs-only commit). Week 1 remains fully done; no code changes this session.

Key events this session:
- Reviewed Week 2 L1 original plan (LangGraph 5-node state machine) against three concerns raised: (a) LangGraph API churn, (b) SRE incidents don't need checkpoint-recovery, (c) graph topology is overkill for a `while`-shaped agent
- Reversed the architectural decision: agent shape is now a **`while` loop with LLM-driven tool selection**, matching mainstream agents (Claude Code / Cursor / Devin / OpenAI Assistants) and Anthropic's own "Building Effective Agents" *agent* pattern (as opposed to *workflow*)
- Downstream decisions dropped or deferred: 5 phase concept, LangGraph runtime dependency, `IncidentState` TypedDict (replaced by `messages` array), model routing (no phases to route by), Temporal (deferred out of Week 2 — see [TRADEOFFS.md §2 revision](../TRADEOFFS.md#2-durable-execution-temporal-not-plain-async))
- Created `experiment/langgraph` bookmark branch from the pre-pivot HEAD (`603e3d6`) for later side-by-side comparison
- Week 2 table rewritten: L1 = agent loop skeleton, L2 = Anthropic adapter, L3 = MCP-transport stubs, L4 = FastAPI + alertctl (no Temporal), L5 = E2E smoke
- New [TRADEOFFS.md §22](../TRADEOFFS.md#22-agent-architecture-agent-loop-over-workflow-graph) documents the pivot; §1 and §2 marked as superseded/revised

**Next up**: Week 2 L1 — agent loop skeleton (`agent/{loop.py, llm/{types,protocol,stub}.py, tools.py}` + pytest verifying 3-turn stub loop). All Week 2 seams are still stubs; real Anthropic + real MCP transport come L2/L3, real queries come Week 3.  
**Blockers**: none  

**Positioning reminder for next sessions** (this is a portfolio project for a mature-company SRE role, not a teaching project):
- Default reference frame: "how would Netflix / Airbnb / Coinbase SRE do this"; scope compromises are labelled as scope trade-offs, not teaching simplifications
- Real off-the-shelf components (Redis, AlertManager, Prometheus, ClickHouse, Vector) always preferred over Python mocks; mocks reserved for cases where the real thing has irreducible ops burden (e.g. PagerDuty needs an account → `incident-tracker` mock stays)
- Three-layer architecture: agent code middleware-agnostic; cases pattern-based; middleware specifics in RAG. New middleware in prod = new runbook chunk, 0 code changes, 0 new case categories
- Alert design: only user-facing SLO violations page; infra signals stay in dashboards + query surface

**Verified live before end of session**:
- **12 real containers** healthy: `gateway / checkout / payment / inventory / auth / deploy-history / incident-tracker + redis / redis-exporter / prometheus / alertmanager / clickhouse / vector`
- 8 runnable golden cases pass via `python mock/scripts/case_runner.py <id>`:
  - Story: `GS-RES-001-redis-oom`, `GS-DEP-001-card-provider-block`, `GS-CHANGE-001-token-upgrade`
  - Primitive: `GS-P-HTTP-ABORT-001`, `GS-P-NETWORK-DELAY-001`, `GS-P-IO-LATENCY-001`, `GS-P-DEPENDENCY-DOWN-001`, `GS-P-CRASHLOOP-001`
- `GS-RES-001-redis-oom` verified: `redis-cli CONFIG SET maxmemory 5mb` + `DEBUG POPULATE` on real Redis → auth `/login` SETEX fails with real Redis OOM error → `HighErrorRate` alert on `auth` fires within `for: 1m` → AlertManager webhooks `incident-tracker` → incident state=triggered
- `GS-P-DEPENDENCY-DOWN-001` verified: 4-alert cascade (`DownstreamFailureRateHigh checkout→payment` + `DownstreamFailureRateHigh gateway→checkout` + `HighErrorRate` on both checkout and gateway) — real cascade signal set the SRE agent must correlate
- Metric names in-mock match production verbatim: `redis_memory_used_bytes`, `redis_memory_max_bytes`, `http_requests_total`, `downstream_requests_total{outcome=...}` — no per-mock translation layer needed by future agent
