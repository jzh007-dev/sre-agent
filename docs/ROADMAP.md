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

Goal: an agent that can accept an alert, walk its 5-node LangGraph, and produce a stub report. No intelligence yet — just the plumbing works end-to-end.

Order reflects the strict "plumbing not intelligence" reading of the week goal: harness skeleton first (defines the shape), then the two seams the harness talks to (LLM gateway + MCP tools), then the ingress that drives it. Every node/tool/gateway is a **stub** returning canned data — real prompts and real queries land in Week 3. See TRADEOFFS §16 (agent-code middleware-agnostic) — Week 2 is where that principle first becomes code.

| L | Concept | Deliverable | Status |
|---|---|---|---|
| L1 | LangGraph state schema first — harness IS the shape. TypedDict `IncidentState`, 5 nodes as **pass-through stubs** that log their phase and hand `state` along, conditional edges, per-node tool budget enforcement. No LLM, no tool call — just the state-machine skeleton. | `agent/graph/{state.py, graph.py, nodes/*.py}`; `pytest` proves a stub run visits triage → collect → hypothesize → verify → report in order | `[ ]` |
| L2 | LLM Gateway skeleton (stubbed responses). Model routing by phase label (`triage → haiku`, `collect/hypothesize/report → sonnet`, `verify → opus`), stable-prefix prompt cache markers, Langfuse tracing wired, cost accounting counters. Returns hardcoded strings — real Anthropic calls in Week 3. Chosen before MCP because every phase needs it. | `agent/llm_gateway.py` (~200 lines); test that verifies routing table + Langfuse trace emitted per call | `[ ]` |
| L3 | MCP protocol basics; stdio transport; tool schema (name, description, params, side_effect, cost_hint); **why 2 servers not 1** (observability read-only vs deploy has WRITE). Three `observability_mcp` tools as **stubs** returning canned JSON. Real PromQL / ClickHouse queries land in Week 3 Lx. | `agent/mcp/observability_mcp/` skeleton with `query_metrics`, `query_logs`, `get_service_topology` all stubbed | `[ ]` |
| L4 | Temporal workflow wrapping the graph + FastAPI ingress + `alertctl trigger` CLI. `POST /alerts` accepts an alert.json (the shape a real AlertManager webhook produces — matches `eval/golden/GS-*/alert.json`), starts a Temporal workflow, returns 202 with an incident id. Ingress is the surface a real AlertManager webhook (from our mock env) would hit. | `agent/entrypoint.py`, `agent/workflow.py`, `alertctl/` CLI | `[ ]` |
| L5 | End-to-end smoke: `alertctl trigger --scenario GS-RES-001-redis-oom` sends the case's `alert.json` to the ingress, the workflow starts, the graph walks all 5 stub nodes, tool stubs are called, LLM gateway is called (returning canned data), a stub Markdown report is emitted, Langfuse trace shows every step. **This is the hello-world agent — nothing intelligent, but every seam is real.** | working `alertctl trigger` command; Langfuse trace URL printed; stub report on stdout | `[ ]` |

**Week 2 exit criteria**: `alertctl trigger --scenario GS-RES-001-redis-oom` runs the empty graph, produces a stub report, and a Langfuse trace shows every phase + tool call + LLM call. No real reasoning, no real queries — every capability is a stub. Week 3's job is to fill those stubs with real logic against the same wiring.

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

**Session end date**: 2026-07-23  
**Last completed**: **Week 1 fully done**. Final mock env has 12 real containers; case runner drives 8 runnable golden cases end-to-end.

Key milestones from this session:
- L6 Commit A `d87067c` — AlertManager + fault framework + incident tracker (alert loop closed)
- L6 Commit B `0695fe6` — session-cache mock + memory_pressure/log_pattern/dependency_fail faults (later partially rolled back in recalibration)
- L6 Commit `92698cd` — **architectural recalibration**: Python session-cache mock replaced by real `redis:7-alpine` + `oliver006/redis_exporter`; `memory_pressure` fault + `SessionCacheMemoryPressure` alert removed (infra metrics scraped for query, not paged — see [TRADEOFFS.md §17](../TRADEOFFS.md)); case naming shifted to 6 diagnostic patterns (LOAD/CHANGE/DEP/RES/DATA/ENV) — middleware knowledge lives in RAG runbooks, not in cases or agent code (see [TRADEOFFS.md §16](../TRADEOFFS.md))
- L7 Commit C1 `81ce3cc` — auth-svc using real Redis client; deploy-history mock; case runner; 3-file case structure; GS-P-HTTP-ABORT-001 primitive
- L7 Commit C2 (this commit) — 3 story cases (RES/DEP/CHANGE) reproducing author's real production experience + 4 more primitive cases adapted from OpenSRE (network-delay, io-latency, dependency-down, crashloop)

**Next up**: Week 2 L1 — **LangGraph state schema + 5 pass-through stub nodes** (harness skeleton). Reordered from the original ROADMAP where MCP was L1: under the "plumbing not intelligence" reading of the week goal, harness comes first because it defines the SHAPE all other Week 2 seams (LLM Gateway, MCP) plug into. See revised Week 2 table above.  
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
