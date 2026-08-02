# Roadmap

Seven-week build plan for sre-agent, Tier 1.5 target. Each week is 5-8 lessons (`L1`, `L2`, ...). Each lesson has a **concept** (why it exists) and a **deliverable** (code, config, or docs). Update status here as you go — this file is the single source of truth for progress and the handoff point between sessions.

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

## The five pillars

Weeks 2-7 are not "features stacked by week." They are five pillars crossing one grid:

| Pillar | What it owns | Where it lives |
|---|---|---|
| **Harness** | the deterministic 6-step shell around the loop: route → pre-process → loadout → **loop** → parse → sink | `agent/core/harness.py` |
| **Loop** | the only non-deterministic step; LLM decides each next action | `agent/core/loop.py` |
| **Gateway** | every LLM call's chokepoint: routing, cache breakpoints, cost accounting, budget enforcement, response cache, tracing | `agent/llm/gateway.py` |
| **RAG** | runbook knowledge + episodic memory, namespaced per integration | `agent/memory/` |
| **Integrations** | declarative config + an MCP server per middleware; **zero Python per integration** | `config/integrations/*.yaml` |

**The seam rule**: adding a trigger type, an integration, a sink, or an LLM provider must not change `agent/core/`. Week 5 L7 measures whether this held.

The package layout enforces it rather than merely documenting it — `agent/core/` holds the non-pluggable spine and every sibling directory is a seam (see [ARCHITECTURE — repository layout](../ARCHITECTURE.md#repository-layout)). `tests/test_architecture.py` (Week 2 L2) fails the build if `core/loop.py`, `core/investigation.py`, or `core/events.py` imports any concrete implementation. That turns the seam rule from a measurement taken once in Week 5 into an invariant checked on every commit.

## Two planning rules

1. **Acceptance is a number, not a file.** Each week's exit criteria is a row of measured values, not a list of files that exist. Structural lessons (pure refactors) are exempt from producing a number, but must ship a test that would have failed before the change.
2. **Detail follows the skeleton.** Weeks 2-3 are planned to lesson level. Weeks 4-7 carry a title and an exit number only — their lesson tables get written when the skeleton has run against a real LLM and the real failure modes are visible. Planning them now would repeat the Week-3 mistake of the pre-pivot plan (detailed lessons built on an architecture that changed underneath them).

---

## Week 1 — Mock environment (the target the agent will observe) — DONE

Goal: a docker-compose local stack that produces realistic metrics + logs + can be induced to fail.

| L | Concept | Deliverable | Status |
|---|---|---|---|
| L1 | Mock env vs agent boundary; 3 pillars (metrics/logs/traces); Prometheus pull vs push; ClickHouse/Loki/ES tradeoff | (conceptual, no artifact) | `[x]` |
| L2 | FastAPI ≈ Spring Boot; ContextVar as async-safe MDC; prometheus_client primitives; middleware-owned observability | `mock/services/checkout/{app.py, requirements.txt, Dockerfile}` | `[x]` |
| L3 | Counter/Gauge/Histogram/Summary internals; label cardinality math; PromQL essentials; RED / USE frameworks | (conceptual — informs future metric design) | `[x]` |
| L4 | Prometheus scrape config, ClickHouse schema (LowCardinality, partition, sort key, TTL), Vector 3-stage pipeline, log timestamp normalization | `mock/{docker-compose.yml, prometheus/, clickhouse/init.sql, vector/vector.yaml}` | `[x]` |
| L5 | Multi-service call graph; httpx client; correlation_id propagation via header; route-pattern label to avoid cardinality explosion; client-side downstream metrics | `mock/services/{gateway, payment, inventory}/` + updates to checkout to call downstreams | `[x]` |
| L6 | Alerting layer + fault injection: real `AlertManager` with SLO-based rules; per-service `/admin/faults`; PD-shaped `incident-tracker` closing the webhook loop; real Redis + `redis_exporter` | `mock/alertmanager/`, `mock/prometheus/alerts.yml`, `mock/services/_shared/fault.py`, `mock/services/incident_tracker/`, real `redis` + `redis-exporter` in compose | `[x]` |
| L7 | Golden case structure + case runner: 3-file case layout (`alert.json` + `setup.yaml` + `expected.yaml`), auth-svc for user-facing SLO surface, deploy-history mock for change-induced cases, 3 story cases + 5 primitive cases adapted from OpenSRE chaos experiments | `eval/golden/GS-*/`, `mock/services/auth/`, `mock/services/deploy_history/`, `mock/scripts/case_runner.py` | `[x]` |

**Week 1 exit criteria** — met: `python mock/scripts/case_runner.py <case-id>` applies the case's `setup.yaml`, waits for the expected AlertManager alert to fire, and dumps the tracked incident — end-to-end against real Prometheus + AlertManager + Redis + ClickHouse. 8 cases pass.

---

## Week 2 — Skeleton: five seams under power

Goal: **a fully wired system with zero intelligence.** One command runs alert → harness → loop → report end-to-end. Every seam (trigger / integration / LLM / sink / eval) has one working implementation and one stub proving it's swappable. All tool returns are canned; all prompts are placeholders. What ships is the *shape* and a **baseline number table**.

> **Re-planned 2026-08-01.** The prior Week 2 table (5 lessons: loop / Anthropic adapter / MCP / ingress / smoke) was scoped for an alert-only, single-integration, no-gateway system. Three additions forced a rewrite: (a) the harness — a deterministic shell around the loop, see [TRADEOFFS §23](../TRADEOFFS.md#23-harness-deterministic-pipeline-around-the-agent-loop-refines-22); (b) the gateway — silently dropped during the pivot even though [EVAL.md](../EVAL.md) depends on it as the cost source, see [§3 revision](../TRADEOFFS.md#3-llm-gateway-in-process-wrapper-not-litellmportkey-service); (c) trigger and integration registries, because alert is only the first of three entry modes (alert / chat / patrol) and integrations must be config not code, see [§24](../TRADEOFFS.md#24-integrations-are-configuration-not-code) and [§25](../TRADEOFFS.md#25-trigger-registry-alert-is-one-entry-mode-of-three).

| L | Concept | Deliverable | Status |
|---|---|---|---|
| L1 | Agent loop shape: `while` + injectable `LLM` protocol + injectable tool dict; `messages` array **is** state; `stop_reason` drives termination; provider-agnostic domain types | `agent/core/loop.py`, `agent/llm/{types,protocol,stub}.py`, `agent/tools/` + `tests/agent/test_loop.py` | `[x]` |
| L2 | **Loop refactored to trigger-agnostic shape.** Four changes, each cheap now and invasive later: (a) `Investigation` replaces the `alert` parameter — `messages` belongs to the investigation, so it is persistable and resumable (chat needs multi-turn; alert storms need mid-flight absorption); (b) `run()` becomes an async generator yielding `Event`s (`TurnStarted` / `TextDelta` / `ToolCalled` / `ToolReturned` / `Done` / `Aborted`) with `run_to_completion()` as a thin wrapper — streaming is required by chat and by patrol's 50-target fan-out, and [ARCHITECTURE](../ARCHITECTURE.md) already claims it as a loop-over-graph advantage; (c) tool exceptions become `ToolResultBlock(is_error=True)` instead of killing the investigation — the current code has no `try` around tool dispatch, and the observability stack is often *part of* the outage; (d) `Investigation.window` — a pinned time range every tool call inherits, without which a golden case rerun 10 minutes later reads different data and [EVAL.md](../EVAL.md) reproducibility is unachievable. Termination moves from `end_turn` to a `submit_report` tool call. | `agent/core/{investigation,events,loop}.py`, `agent/tools/{protocol,dispatch,stubs}.py` (split from `tools.py`), root `pyproject.toml`; tests: multi-turn resume, loop survives a throwing tool, event ordering, window propagation, plus `tests/test_architecture.py` enforcing the seam rule as an import invariant | `[x]` |
| L3 | **Gateway — four layers plus three cross-cutting decorators.** Layering per [TRADEOFFS §33](../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators): routing (`CallKind` → model, with the judge/reviewer family rule validated at *wiring* time) → construction (`provider_catalog`, `credentials`, `request` with `cache_control` breakpoint placement) → transport (error taxonomy, retry, circuit breaker, per-provider semaphore) → shared (`types`, `usage`, `errors`), with **cache**, **budget gate** and **tracing** as decorators rather than a fifth layer. Cache sits *before* the budget gate and a hit still charges the budget by replaying the original cost, or a run that degraded on exhaustion would stop degrading on rerun. Fallback lives in the gateway (it is a routing decision) and is **disabled in eval**. `BudgetExceeded` / `ProviderUnavailable` live in `llm/protocol.py` so the loop may convert them to `Aborted` under the seam rule. LiteLLM evaluated and declined ([§34](../TRADEOFFS.md#34-litellm-not-adopted-kept-as-a-documented-swap-in)); `litellm_adapter.py` records the swap shape and the threshold. | `agent/llm/{errors,usage,protocol,provider_catalog,credentials,request,routing,transport,cache,cost,gateway,openai_compat,anthropic,litellm_adapter}.py`, `.env.example`; `tests/llm/{test_transport,test_request,test_routing,test_gateway,test_adapters}.py` | `[x]` |
| L3b | **Live smoke + the two deferred pieces.** Real SDK clients (`max_retries=0`, since transport owns retry), a `live_gateway()` factory that wires only credentialled providers, `srectl smoke` reporting real usage and cost, **streaming** in transport plus the OpenAI-compat adapter, and a **context-window pre-check** in construction. Verified live against DeepSeek: the second call with a shared prefix reported **1536 of 1550 input tokens served from the provider's prompt cache — cost $0.000454 → $0.000067, an 85% reduction**, which is the number behind the claim that prefix ordering is the highest-leverage cost decision in the gateway. **Price verification remains open**: the published pricing page was not reachable from this environment, so `verified=False` stands and `prices_verified: false` propagates into every ledger summary. Internal consistency *is* verified — recomputed cost matches the ledger to 1e-9. | `agent/llm/{clients,transport,openai_compat,request,protocol,gateway}.py`, `srectl/{__main__,commands/smoke}.py`, `.env.example`, `tests/llm/test_streaming.py` | `[~]` |
| L4 | **Trigger registry + rule-based alert correlation.** Alert is one entry mode of three; per-trigger pre-processor plugs into harness step ②. `alert` real (fingerprint dedup, severity → budget tier, and **correlation grouping**), `chat` stub (intent recognition), `patrol` stub (scope expansion). All three normalize to the same `Investigation`.<br><br>**Correlation is here rather than in W5** because the rule version needs neither an LLM nor vectors: alerts inside a correlation window whose services are topologically adjacent join the in-flight investigation via `Investigation.add_user_text` instead of forking a new one. Roughly 40 lines, and it is the single highest-value behaviour a real incident copilot has — `GS-P-DEPENDENCY-DOWN-001` already delivers a 4-alert cascade that must produce **1** investigation, not 4. Similarity-based deduplication of *repeat* incidents waits for episodic memory in W4. See [DIAGNOSIS P6](../DIAGNOSIS.md#layer-1--precompute-p-rules). | `agent/triggers/{registry,alert,chat,patrol}.py`, `config/budgets.yaml` | `[ ]` |
| L5 | **Integration config layer + MCP transport.** An integration is a YAML file, not a Python class: `match` rules, `mcp` server command, `runbook_namespace`, `prompt_fragment`, `notifier` list, declarative `inbound_mapping` (jsonpath field mapping covers the common webhook; an optional Python normalizer is the escape hatch). Plus `observability-mcp` over real stdio with canned returns, and `ToolMeta` (`side_effect` / `cost_hint` / `timeout`). Why 2 servers not 1: deploy has WRITE side effects and belongs in its own permission scope (Week 6). | `config/integrations/observability.yaml`, `agent/integrations/{registry,config,mapping}.py`, `mcp_servers/observability/` | `[ ]` |
| L6 | **Harness + sink registry.** Six steps as independently testable functions: `route` → `preprocess` → `loadout` (tool bundle + `ToolBudget` + assembled system prompt) → `run_loop` → `parse` → `fanout`. Steps ③④⑤ are identical across all three trigger types; only ①②⑥ vary. Sink registry: `stdout` real, `slack` / `jira` stubs. | `agent/core/harness.py`, `agent/sinks/` | `[ ]` |
| L7 | **FastAPI ingress + `srectl`.** Single entry dispatching by trigger type; idempotent on `alert_id` (AlertManager retries webhooks); returns 202 + `investigation_id`; in-process asyncio task + in-memory registry + per-investigation JSONL append log (enough to reconstruct `messages` on restart — Temporal stays deferred, see [TRADEOFFS §2 revision](../TRADEOFFS.md#2-durable-execution-temporal-not-plain-async)). | `agent/api/entrypoint.py`, `srectl/` | `[ ]` |
| L8 | **`eval/run.py` + baseline table.** Harness-layer metrics are measurable under stub tools because they are properties of the harness and gateway, not of intelligence: termination rate, median turns, median tool calls, median cost, p90 latency, cache hit rate. Also aligns [EVAL.md](../EVAL.md) to Week 1's actual 3-file golden format (real fault injection beats fixtures — it exercises the tool layer too). Also rewrites [INCIDENT_WALKTHROUGH.md](../INCIDENT_WALKTHROUGH.md) — currently pre-pivot prose with illustrative numbers — against a real `GS-*` run. It ships **here, with ugly real numbers**, rather than in W7 with pretty invented ones: a project whose thesis is measurement cannot have its flagship narrative be fiction. | `eval/run.py`, `eval/metrics.py`, first baseline table in `docs/`, rewritten `INCIDENT_WALKTHROUGH.md` | `[ ]` |

**Week 2 exit criteria** — a number table, not a file list:

| Metric | Expectation |
|---|---|
| Termination rate over the 8 Week-1 cases | 100% |
| Median turns / median tool calls | **measured, published as-is** — no target ([§30](../TRADEOFFS.md#30-unmeasured-targets-are-labelled-hypotheses)) |
| Median cost per investigation | measured, published as-is |
| p90 latency | measured, published as-is |
| Gateway cache hit rate on an identical rerun | >90% |
| Python lines changed to add the 2nd integration YAML | 0 |
| Duplicate `alert_id` delivery → investigations created | 1 |
| `GS-P-DEPENDENCY-DOWN-001` 4-alert cascade → investigations created | **1** |

The three "published as-is" rows are the point of this week. `median tool calls < 12` and `p99 cost < $0.40` were written before a loop had ever run; unassisted agentic RCA plausibly costs 30-100 tool calls. If the real figure is 40, we publish 40 and W3's precompute layer attacks it — we do not quietly move the target.

Plus: `srectl trigger --scenario GS-RES-001-redis-oom` runs end-to-end with a real LLM call, real MCP transport, canned tool returns, and a Langfuse trace showing every LLM and tool call. **No reasoning quality is claimed.**

---

## Week 3 — Loop intelligence

Goal: real reasoning inside the loop. Real queries against the Week-1 stack, a written methodology, a deterministic precompute layer, and the first accuracy number.

> **Re-planned twice.** The pre-pivot table (one prompt per node, routing by phase, `graph/nodes/*.py`) was void — those nodes do not exist. The 2026-08-01 capacity review then added three things and paid for them by cutting elsewhere (see **Scope cuts** in the pointer): a written [DIAGNOSIS.md](../DIAGNOSIS.md) replacing the one-line "elimination tree", a **precompute layer** because unassisted agentic RCA rediscovers topology and change timelines every incident, and **scoped tracing** for per-request causal chains. Nine lessons; this is the intelligence week and the depth went here.

| L | Concept | Deliverable | Status |
|---|---|---|---|
| L1 | **Methodology, written down + layered system prompt.** [DIAGNOSIS.md](../DIAGNOSIS.md) is authored first: numbered precompute rules (P1-P6), elimination rules with explicit activation *and* pruning conditions (D1-D7), and a named boundary for what stays in-model. Every rule maps to a golden case or a deterministic checker; rules without one are marked as gaps rather than assumed. The governing constraint is that DIAGNOSIS holds middleware-**agnostic** procedure only — "require a change to precede the symptom" is procedure, "Redis noeviction fails writes" is a runbook chunk ([§20](../TRADEOFFS.md#20-middleware-specific-knowledge-lives-in-rag-not-in-agent-code-or-cases)). Prompt assembly order is dictated by cache prefixing (most static first): `[A]` role + methodology → `[B]` output contract → `[C]` integration facet → `[D]` budget. Plain markdown + `assemble()`; no jinja2 for four fragments. | `DIAGNOSIS.md`, `agent/prompts/{methodology,output_contract}.md`, `agent/prompts/integrations/*.md`, `agent/prompts/assemble.py` | `[ ]` |
| L2 | **Real observability tools.** PromQL against real Prometheus; SQL against real ClickHouse; topology derived from the client-side `downstream_requests_total{service,downstream,outcome}` metrics Week 1 L5 already emits. Every query inherits `Investigation.window`. Results shaped to a token budget — a raw ClickHouse page is megabytes. | `mcp_servers/observability/` real implementations | `[ ]` |
| L3 | **Traces, scoped and falsifiable.** OTel instrumentation (the `correlation_id` propagation from W1 L5 is the foundation) + a Tempo container + one `query_traces` tool returning **edge-level aggregates**, with a single request's span tree only on explicit `trace_id` drill-down. **Never raw span dumps** — that is the fastest way to destroy a context window. Framing matters: per-edge RED is already available from Prometheus, so what traces genuinely add is *where the time went inside one slow request*. Kept only if it earns a measured margin — see the exit table. [TRADEOFFS §29](../TRADEOFFS.md#29-trace-scope-per-request-causal-chains-not-a-third-pillar). | `mock/tempo/`, OTel wiring in `mock/services/_shared/`, `query_traces` in `mcp_servers/observability/` | `[ ]` |
| L4 | **Precompute layer in harness ②/③.** The deterministic joins the model should never be doing by eye: P1 merged timeline (alert firing, metric onset, log-error onset, deploys — with pairwise lead-lag in seconds), P2 topology graph, P3 blast radius including services that are sick without paging, P4 ranked candidate shortlist, P5 cascade root-vs-symptom. Onset *ordering* is arithmetic, and the entire elimination tree is built on it — leaving it to the model puts a silent error class under everything. **The hard constraint: a shortlist, never a conclusion.** If the final root cause is always the top candidate, the LLM is a narrator for a heuristic, which is why `precompute_override_rate` is in the exit table ([TRADEOFFS §28](../TRADEOFFS.md#28-precompute-produces-a-shortlist-never-a-conclusion)). | `agent/core/precompute.py`, shortlist logged into the trace so precompute bugs are separable from reasoning bugs | `[ ]` |
| L5 | **Parallel tool calls.** One assistant response can carry several `tool_use` blocks; `asyncio.gather` is already wired in L2's dispatch — this lesson measures it on `GS-P-DEPENDENCY-DOWN-001` (a real 4-alert cascade) and tunes concurrency. | wall-clock delta recorded | `[ ]` |
| L6 | **Context management.** What the phase graph gave for free and a loop must do explicitly: per-result token cap (the 20k-char valve in `tools/dispatch.py` is a stopgap, not a policy), oldest-result folding, compaction above a threshold. | `agent/core/context.py` | `[ ]` |
| L7 | **Report contract, not just a schema.** `submit_report` with a forced Pydantic schema, and the first three lines fixed: `VERDICT` (origin / symptom-of / undetermined), `FIRST ACTION` (**exactly one**, or "none — investigate further"), `CONFIDENCE`. A list of five actions is a way of not deciding, and it pushes the decision back onto someone already overloaded. Mitigation and root-cause fix labelled separately where they differ. Body: root cause, evidence, ruled-out D-rules with the reason each was pruned, open questions, assumptions, undo path. `confidence: "unknown"` is legal. Harness ⑤ reads a validated object instead of parsing prose. | `agent/core/report.py`, contract section in [DIAGNOSIS.md](../DIAGNOSIS.md) | `[ ]` |
| L8 | **Adversarial verify + degradation.** Refute sub-loop per hypothesis, own budget, strongest tier; `submit_report` returns `is_error` when no refutation is on record, making verification structurally mandatory rather than prompt-suggested. Plus partial observability: with ClickHouse faulted the report says what it could not check and reasons from metrics alone; budget exhaustion produces an "insufficient evidence" report naming the ceiling that stopped it; two-stage output (preliminary verdict in seconds, full report after) over the L2 event stream. | `agent/core/{verify,degrade}.py`; new golden case with the observability stack itself faulted | `[ ]` |
| L9 | **Judge online, with an anchor set first.** The judge cannot be validated against reports that do not exist, so before it runs: hand-author 3 report variants per case at different rubric levels (~24 labelled examples), score them, and only then check Cohen's kappa. Without that anchor the judge has nothing to drift *from*. Deterministic `hallucination_count` checker as the guard against judge drift. New metrics: `human_first_action` alignment (author ground truth — the one metric in the suite that is not model-judged), `precompute_override_rate`, `report_actionability` (judge-scored, therefore a diagnostic rather than a target). | `eval/judge.py`, `eval/checkers.py`, `eval/anchors/`, `human_first_action` added to every `expected.yaml`, accuracy table committed | `[ ]` |

**Week 3 exit criteria**:

| Metric | Expectation |
|---|---|
| `root_cause_accuracy` over 8 cases (judge, 0-5) | ≥ 3.0 mean |
| Judge / author Cohen's kappa on the 24-example anchor set | ≥ 0.7 **before** the judge is trusted for anything |
| `human_first_action` alignment | measured — the only non-model-judged quality number |
| `precompute_override_rate` | measured; **near-zero is a red flag, not a win** |
| Cascade root-vs-symptom correctness on `GS-P-DEPENDENCY-DOWN-001` | deterministic pass |
| Accuracy delta from `query_traces` on the 3 latency/cascade cases | measured; **under ~2% and the tool is removed** and we report that edge metrics sufficed |
| Median tool calls, with precompute vs without | measured — this is the R5 question answered |
| Accuracy retained with ClickHouse faulted vs healthy | measured (degradation, not collapse) |
| Hallucination rate | < 5% of cases |
| Fraction of hypotheses killed by refute | measured |

---

## Week 4 — RAG + memory + the learning loop

Runbook knowledge and episodic memory, both namespaced per integration. Episodic memory feeds back into harness ② so repeat-incident deduplication stops being a pure time-window rule. Also the minimum viable learning loop, which is a *process* more than it is code: each eval run, take the worst 5 cases → one prompt or tool change per case → rerun the full set. Episodic write-back admits only reports above the judge threshold, each carrying provenance. Runbook no-hit events finally get a consumer — the agent proposes a draft chunk when a failure mode matched nothing, which closes [RAG.md](../RAG.md)'s dangling no-hit log.

One caution carried from `GS-LOAD-001`: a similar past incident can be a **liability**. Its Singles-Day precedent was fixed by scaling up, and that is the wrong answer for the retry-storm case. Memory that anchors the agent onto a stale resolution is worse than no memory, so the A/B below has to be read in both directions.

**Exit numbers**: `recall@5 ≥ 0.85`, `hit@3 ≥ 0.75`, first two rows of the [RAG.md](../RAG.md) ablation table with real values, an A/B delta with and without episodic memory, **and the anchoring check** — accuracy on `GS-LOAD-001` with its misleading precedent present.

*Lesson table written at the end of Week 3.*

---

## Week 5 — Integration breadth (config-only) + abstraction audit

**The deliverable is not a count of integrations — it is a measured abstraction cost.** One real backend added as a second live integration (a Kafka broker: one container), plus Grafana as inbound-only (proving the inbound schema is pluggable independently of the tool bundle), plus Jira as the first WRITE-class sink. k8s, and anything else, registers as a config-only seam.

> **k3d was cut** on 2026-08-01 — see [TRADEOFFS §26](../TRADEOFFS.md#26-k8s-integration-a-config-only-seam-no-k3d-cluster). Three deliberately-broken pods do not demonstrate operating Kubernetes, and those days paid for tracing, the methodology document, and the precompute layer. The reversal is one YAML, one MCP server, and a k3d compose file with **zero changes to `agent/core/`** — which is the payoff of integrations being configuration.

**Exit numbers**: Python lines changed to add integration #3 = **0** (enforced continuously by `tests/test_architecture.py`, not measured once); per-integration accuracy recorded; one WRITE-class tool reaching the gate.

*Lesson table written at the end of Week 4.*

---

## Week 6 — Security + evaluation depth

Five-layer prompt injection defence; golden set grown to **~30** (20 ordinary + 10 adversarial — see [§27b](../TRADEOFFS.md#27b-golden-set-target-30-cases-not-55); the adversarial ten are load-bearing because per-layer bypass rate needs roughly two per layer). Nightly regression. Injection surface spans integrations — Kafka topic names and Jira descriptions are untrusted input exactly like log lines. Layer 4's gate finally has a real WRITE tool to gate.

Also realises `GS-LOAD-001` out of `eval/backlog/`, which is now urgent rather than nice-to-have: **three DIAGNOSIS rules depend on it alone** (D1 pruning / no-deploy hallucination guard, D4, D5 amplification), and it is the golden set's only case where the correct answer is *not* the top precompute candidate — so it is also the test that `precompute_override_rate` is measuring something real.

**Exit numbers**: adversarial success rate 100%; per-layer bypass rate 0% for L1-L5; nightly regression green with cost per full run recorded; the two [DIAGNOSIS coverage gaps](../DIAGNOSIS.md#rule-to-case-coverage) closed or explicitly accepted.

*Lesson table written at the end of Week 5.*

---

## Week 7 — Polish + demo

Slack integration (the human-loop surface, so it is not cosmetic), cost/latency dashboard, 3-minute demo video, `INTERVIEW_CHECKLIST.md` filled with real numbers, repo hygiene. [INCIDENT_WALKTHROUGH.md](../INCIDENT_WALKTHROUGH.md) is *not* here — it moved to W2 L8, because a project whose thesis is measurement cannot leave its flagship narrative as fiction until the last week.

**Exit numbers**: `EVAL_REPORT.md` with per-difficulty breakdown; 30-day accuracy trend annotated with prompt and model changes; someone unfamiliar clones the repo, runs `make demo`, and understands what they saw in 60 seconds.

*Lesson table written at the end of Week 6.*

---

## Open gaps

Reviewed 2026-08-01. Items 1-6 were identified when the plan was re-scoped; 9-14 came from the capacity review the same day.

| # | Gap | Why it matters | Landing |
|---|---|---|---|
| 1 | **Alert-storm correlation** | The highest-value behaviour a real incident copilot has, and `GS-P-DEPENDENCY-DOWN-001` already reproduces it. | **W2 L4** (rules) + W4 (similarity) — pulled forward from W5 |
| 2 | **`Investigation.window`** | Without it a golden case rerun reads different data and reproducibility is unachievable. | ✅ W2 L2 |
| 3 | **Partial observability** | The observability stack is frequently part of the outage. | ✅ W2 L2 (structure) + W3 L8 (behaviour) |
| 4 | **Confidence contract** | "Narrowed 40 services to 2" is real value; the gate needs a level to grade on. | W3 L7 |
| 5 | **Two-stage output** | What on-call actually wants on a P1. Nearly free given the event stream. | W3 L8 |
| 6 | **Runbook feedback loop** | RAG.md logs no-hit events and nothing consumes them. | W4 |
| 7 | **Patrol's value proposition — undecided** | "The loop on a cron" is just slower alerting. The differentiator implies different tools and a different output shape. | **Formally deferred** — [§31](../TRADEOFFS.md#31-patrol-stays-a-stub-until-its-value-proposition-is-settled). Stub stays; it already earned its keep by forcing per-investigation budgets and the event stream |
| 8 | **Agent read-permission boundary** | "Your agent can read PII out of logs" is a standard interview objection; SECURITY covers egress but not what may be *read*. | W6 |
| 9 | **DIAGNOSIS coverage gaps** | D3-pruning (local onset preceding downstream's) has no case; D7 needs a genuinely undetermined case where `confidence: "unknown"` is *correct*. Without the latter the set cannot detect an agent that never admits uncertainty. | W6 |
| 10 | **`GS-LOAD-001` is load-bearing and not runnable** | Three D-rules depend on it alone, and it is the only case whose right answer is not the top precompute candidate. | W6 — realise out of `eval/backlog/` |
| 11 | **Precompute self-certification** | If the shortlist is good enough the LLM becomes a narrator and every accuracy metric still looks excellent. | `precompute_override_rate` from W3 L4; [§28](../TRADEOFFS.md#28-precompute-produces-a-shortlist-never-a-conclusion) |
| 12 | **Eval self-certification** | Golden set is self-authored and the judge is an LLM. `human_first_action` is the only planned metric with author ground truth. | W3 L9 anchor set + kappa gate |
| 13 | **No real on-call user** | Every quality signal is the agent or a judge grading itself. Adoption and trust are unmeasurable without a human reader. | Unresolved. `human_first_action` is a proxy, not a substitute — stated as a limitation in `EVAL_REPORT.md` rather than papered over |
| 14 | **ARCHITECTURE.md is ~35KB** | A map that nobody finishes reading is not a map. | Split into map + `docs/COMPONENTS.md` — pending |

---

## Cross-week tracks (run in parallel, small time slices)

- **Eval as a track, not a deliverable.** [EVAL.md](../EVAL.md) opens by calling the eval system "the load-bearing artifact of this project" — so `eval/run.py` exists from Week 2 L8 and the metric set grows weekly. Every week's exit criteria is a row from it.
- **Integration parity**: every new capability gets asked "does this work for all registered integrations, or only observability?" A capability that only works for one is a leak in the seam rule.
- **Interview checklist**: after every substantive change, add a data point / decision line to `INTERVIEW_CHECKLIST.md`.
- **Tradeoffs**: any non-obvious decision → a new entry in `TRADEOFFS.md` with Alternatives + Why + Cost + Reconsider-when.
- **Langfuse observability**: from Week 2 L3 onward every LLM call is traced through the gateway — gaps are bugs.

---

## Current pointer

**Session date**: 2026-08-01
**Last completed**: Week 2 L1 (agent loop skeleton + StubLLM + 3 stub tools, commit `05a157b`).

Key events this session — **Week 2-7 re-planned**, no code changes:

- Named the five pillars (harness / loop / gateway / RAG / integrations) and the **seam rule**: adding a trigger, integration, sink, or provider must not touch `loop.py` or `harness.py`.
- **Harness recognized as a missing layer.** The system is not a bare agent loop; it is a deterministic 6-step pipeline *containing* one. Steps ①②⑥ vary by trigger; ③④⑤ are identical across alert / chat / patrol. New [TRADEOFFS §23](../TRADEOFFS.md#23-harness-deterministic-pipeline-around-the-agent-loop-refines-22).
- **Gateway recovered.** [§3](../TRADEOFFS.md#3-llm-gateway-in-process-wrapper-not-litellmportkey-service) promised routing / cache management / cost accounting / tracing; the pivot renamed it "LLM adapter layer" and dropped four of the five responsibilities behind "SDK-native" — which is true only for retry. [EVAL.md](../EVAL.md) lists the gateway as the sole source of `cost_usd`, and gateway-level response caching is what makes eval both affordable and reproducible. Scheduled W2 L3.
- **Multi-provider scoped down.** Two adapter classes total (`OpenAICompatLLM` covers DeepSeek / Qwen / Kimi by `base_url`; `AnthropicLLM` is the different-family class). Justification changed from cost to *requirement*: SECURITY L3 needs a different-family reviewer and EVAL needs a judge that differs from the agent. Quality parity across providers is explicitly **not** promised.
- **Integrations became configuration.** A YAML file plus an MCP server; zero Python per integration. Abstraction cost becomes "0 lines of Python" rather than "47 lines."
- **Trigger registry added.** Alert is one entry mode of three (alert / chat / patrol). Chat forces `messages` to belong to a persistable `Investigation` and forces the loop to yield events; patrol forces per-investigation budgets. Both are cheap now and invasive later, so they land in W2 L2.
- **Integration breadth cut from 5 real to 2 real.** The claim worth making is a measured abstraction cost, not a count. Freed budget went to gateway and eval.
- **Eval promoted from Week-6 deliverable to cross-week track**, with harness-layer baselines measurable in Week 2 under stub tools.
- Week 2 grew from 5 to 8 lessons and is now the heaviest week — all mechanical, no prompt tuning.
- Eight **open gaps** recorded above; two of them (patrol's value proposition, agent read-permission boundary) are unresolved design questions, not scheduled work.

Structural decisions taken the same session:
- **Package organized by replaceability**: `agent/core/` holds the eight non-pluggable spine files; every sibling directory is a seam or a support layer. No loose modules at the package top. See [ARCHITECTURE — repository layout](../ARCHITECTURE.md#repository-layout).
- **The seam rule became a test** — `tests/test_architecture.py` fails if `core/loop.py` imports a concrete implementation. The Week 5 L7 abstraction-cost claim is now enforced continuously, not measured once.
- Renames: `alertctl` → **`srectl`** (three trigger modes, not one); integration YAML moved to `config/integrations/` to stop colliding with the `agent/integrations/` loader package; gateway lives at `agent/llm/gateway.py` as the front door of that package.
- `agent/tools.py` splits into `agent/tools/` at L2 rather than L5, since L2 adds `dispatch.py` anyway.
- Root `pyproject.toml` added — the agent gets its own dependency set on Python 3.11+, deliberately separate from `mock/.venv` (3.14). The observer and the observed must be able to fail independently.
- `eval/golden/GS-LOAD-001.jsonc` moved to `eval/backlog/` — it was the last pre-Week-1 single-file fixture case, so it broke the "every case is a 3-file directory" invariant and would fail the case runner. **Kept, not deleted**: its design is the most valuable in the set (misleading dominant signal, memory-anchoring trap, no-recent-deploy hallucination trap, mitigation-vs-root-cause distinction). The file header records what the mock needs before it can be realized — a retry-storm fault, a pool-saturation surface, and dropping the JVM/GC metrics. Target: Week 6 golden-set expansion, and it is the natural regression case for Week 3 L6 (refute) and Week 4 L5 (memory as liability).

**Week 2 L2 completed the same session.** 41 tests green (`python3 -m unittest discover -s tests -t .`). Beyond the four planned changes:

- **Termination keys on `ToolMeta.terminal`, not on a tool name.** `core/loop.py` contains no occurrence of the string `submit_report`; it looks for "some called tool is marked terminal". The seam rule holds at the level of the report tool too.
- **`window` is a reserved keyword, not a schema field.** Every tool's `run()` takes `window` as a keyword the harness supplies, and `tool_schemas()` raises at wiring time if a tool declares `window` in its `input_schema`. The model has no channel through which to move the time range — structural, not a prompt instruction it might ignore.
- **Termination policy differs by trigger.** Alert and patrol must call a terminal tool; plain `end_turn` is `Aborted("no_report")`. For chat, answering and stopping is a legitimate `Done`. This is `Investigation.requires_report`, and it is the first place the trigger-agnostic design actually pays.
- **Results are recorded even on the concluding turn.** Every `tool_use` gets a `tool_result` including the terminal one, because an unanswered `tool_use` makes `messages` invalid for any later API call — which would break both chat resume and W5's alert-storm absorption.
- **`budget_exhausted()` returns the reason, not a bool**, so the degraded report can name which ceiling stopped the investigation.
- **Found and fixed an eval-validity bug**: all 8 golden `alert.json` files carry a `_meta.purpose` that states the root cause in prose ("Redis bgsave failed under memory pressure, session writes rejected"). Passing the payload through verbatim would have handed the agent the answer and made every future accuracy number meaningless while still looking healthy. `strip_fixture_metadata()` drops any underscore-prefixed key before a payload can reach a prompt, and `test_investigation.py` asserts it against the real fixture files so a future case cannot reintroduce the leak.
- The three key guards were mutation-tested (remove the scrub → 10 failures; narrow dispatch's `except` → 1 error; add a provider import to `core/loop.py` → seam test fails), so the tests are known to bite rather than assumed to.

### Capacity review — 2026-08-01 (same session)

A risk review produced ten findings. Rather than absorb them by extending the schedule, every addition was priced and paid for by a cut, on the rule that **each new item must arrive with something it displaces**. Net effect: still seven weeks, with breadth traded for depth.

**Added (≈10-12 days)**

| Item | Landing | Why it was P0 |
|---|---|---|
| Scoped tracing | W3 L3 | Per-request causal chains are the one thing edge-level RED cannot give. Kept only if it earns a measured margin ([§29](../TRADEOFFS.md#29-trace-scope-per-request-causal-chains-not-a-third-pillar)) |
| [DIAGNOSIS.md](../DIAGNOSIS.md) | W3 L1 | "An elimination tree" in one line meant the reasoning was outsourced to whatever the model inferred, and could not be regression-tested |
| Precompute layer | W3 L4 | Unassisted agentic RCA rediscovers topology and change timelines every incident — that is why 30-100 tool calls is the realistic figure and why `<12` was fiction |
| Report contract (`VERDICT` / single `FIRST ACTION`) | W3 L7 | The most common death of an incident copilot is on-call not reading it |
| `human_first_action` + anchor set + kappa gate | W3 L9 | Everything else in the eval is model-judged; this is the only planned number with author ground truth |
| Alert-storm correlation pulled forward | W2 L4 | Rule version needs no LLM and no vectors; ~40 lines |
| Learning loop (minimum viable) | W4 | Episodic memory currently writes nothing back; no-hit log has no consumer |
| Walkthrough rewrite moved forward | W2 L8 | Ugly real numbers beat pretty invented ones |

**Cut (≈11.5 days)**

| Cut | Saves | Evidence lost |
|---|---|---|
| **k3d / real k8s integration** → config-only seam ([§26](../TRADEOFFS.md#26-k8s-integration-a-config-only-seam-no-k3d-cluster)) | ~5d | Little. Three broken pods do not demonstrate operating Kubernetes, and the abstraction claim needs *a* second live integration, not specifically k8s. One container (Kafka) serves. Reversal costs one YAML + one MCP server + zero changes to `agent/core/` |
| **Golden set 55 → 30** ([§27b](../TRADEOFFS.md#27b-golden-set-target-30-cases-not-55)) | ~3d | Weaker per-bucket statistics, reported as a caveat. The 10 adversarial cases stay — per-layer bypass rate needs them |
| **Patrol stays a stub** ([§31](../TRADEOFFS.md#31-patrol-stays-a-stub-until-its-value-proposition-is-settled)) | ~1.5d | None. The seam already paid for itself by forcing per-investigation budgets and the event stream |
| **Temporal out of scope, not deferred** ([§32](../TRADEOFFS.md#32-temporal-is-out-of-scope-not-deferred)) | ~2d latent | No crash-resume demo. It had been "deferred one more phase" three times, which is how scope pretends to be planned |

Three findings were accepted with the scope changed rather than as written:

- **Traces were framed as "a missing third pillar"** — an overstatement. W1 L5's client-side `downstream_requests_total` already gives per-edge RED and the topology graph, so the genuine gap is narrower: single-request time attribution. That reframing is what made the item affordable, and it set the acceptance number.
- **"Score the 8 cases in half an hour"** is not possible — the judge grades `(case, report)` pairs and no reports exist yet. The implementable version is hand-authoring ~24 report variants at known rubric levels as an anchor set, which is 2-4 hours and must precede the judge.
- **`report_actionability` is judge-scored**, so it inherits the self-certification problem it was proposed to solve. `human_first_action` is author ground truth and is therefore the stronger half; priority was reversed accordingly.

Also corrected: the compose stack has **13** services, not 12 — `redis-exporter` was omitted from the count in three documents.

### Week 2 L3 completed — 2026-08-02

Gateway built to the four-layer design, with the deltas recorded in [TRADEOFFS §33](../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators). 127 tests green; **every transport policy path runs offline** — the adapter's `send` and the sleeper are injected, so retry, backoff, breaker states and error classification are all tested with no network and no API key.

Both open questions from the previous session resolved:

- **The cache key includes the tool schemas.** Omitting them would let a changed tool set hit a stale entry, and wrong-but-cheap is the worst outcome for an eval system. "Adding a tool invalidates the cache" is correct — adding a tool *should* require re-running eval.
- **Budget exhaustion raises `BudgetExceeded` from the gateway**, declared in `llm/protocol.py` because it is part of that contract rather than a provider detail. The loop may therefore import it under the seam rule and convert it to `Aborted("budget", …)`, which preserves the L2 invariant that every run emits exactly one `Done` or `Aborted`.

Decisions taken beyond the specified design, each recorded as a delta in §33:

- **The budget gate sits after the cache lookup, and a cache hit still charges the budget** by replaying the original cost. A hit costs no money, so gating first would refuse free calls; but if hits were free of *budget* too, a run that degraded on exhaustion would stop degrading on rerun. Hence two reported totals: `money_spent_usd` and `budget_charged_usd`.
- **Termination of a retry sequence never counts as provider unavailability unless the error class says so.** A malformed request failing three times must not trip the breaker on a healthy provider, and 429 never trips it either — breaking on a quota condition would fall back and hide the misconfiguration.
- **Fallback is disabled in eval.** A run that silently continued on a second provider has an accuracy attributable to neither model and a cost mixing two price sheets. Real runs that did fall back are tagged in the ledger and excluded from model comparison.
- **Prices are marked unverified** and every cost carries its price-table version. A project whose thesis is measured cost cannot report figures from an invented price table as fact.
- **A bug caught by its own test**: the backoff exponent was off by one, so the first retry waited twice `base_delay`. Invisible in production — it would have surfaced only as unexplained latency.

**Deliberately deferred out of L3 into L3b**, rather than quietly omitted: **streaming** (transport) and the **context-window pre-check** (construction). Both were named in the design; neither is needed until chat, two-stage output, or compaction exist, and both are cheap to add behind the interfaces now in place.

### Week 2 L3b — live, with one number and one honest gap

139 tests green. Real DeepSeek call through the full stack: routing → construction → transport → parse → cache → ledger.

**The number**: a second call sharing the system-prompt prefix reported **1536 of 1550 input tokens served from the provider's prompt cache**, taking that call from **$0.000454 to $0.000067 — an 85% reduction**. That is the measured basis for the claim that prefix ordering is the highest-leverage cost decision in the gateway ([§3](../TRADEOFFS.md#3-llm-gateway-in-process-wrapper-not-litellmportkey-service)), and it holds on DeepSeek *without any explicit markers* — which is exactly the provider asymmetry [§33](../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators) delta 4 predicted. Streaming verified at 18 text chunks.

**The gap, left open rather than papered over**: the published pricing page was not reachable from this environment, so the price table stays `verified=False` and `prices_verified: false` propagates into every ledger summary and every eval row. Internal consistency *is* verified — recomputed cost matches the ledger to 1e-9 — so the arithmetic is sound and only the rates are unconfirmed. Setting `verified=True` on unchecked numbers is precisely the dishonesty [§30](../TRADEOFFS.md#30-unmeasured-targets-are-labelled-hypotheses) exists to prevent. **To close it**: check DeepSeek's published per-1M rates against `provider_catalog.MODELS["deepseek-chat"].price` (currently input 0.28 / output 1.10 / cache-read 0.028) and flip the flag.

One design change came out of the first failing test, and it is worth more than the test: **a context overflow must not trigger cross-provider fallback.** Falling back to a model with a bigger window postpones compaction until *nothing* fits — fallback as a way of deferring a problem. So `ContextOverflow` became a **contract** error in `llm/protocol.py`, distinct from the provider-detection class `errors.ContextLimit`, and the gateway translates between them. It carries `excess_tokens` so W3 L6's compaction knows how much to remove. The loop now handles all contract errors in **one** `except` clause keyed on each exception's own `reason` — so a fourth contract error needs no kernel change, which is the seam rule applied to itself.

Also fixed: `srectl smoke` counted uncredentialled providers as "reachable", and reported no credentials directly above a successful call because `env_summary()` ran before `load_env()`.

**Next up**: Week 2 L4 — trigger registry plus **rule-based alert correlation**, the item pulled forward from W5. Acceptance is the existing `GS-P-DEPENDENCY-DOWN-001` 4-alert cascade producing **1** investigation rather than 4.
**Blockers**: none.

**Positioning reminder** (portfolio project for a mature-company SRE role, not a teaching project):
- Default reference frame: "how would Netflix / Airbnb / Coinbase SRE do this"; scope compromises are labelled as scope trade-offs, not teaching simplifications.
- Real off-the-shelf components always preferred over Python mocks; mocks reserved for cases where the real thing has irreducible ops burden (PagerDuty needs an account → `incident-tracker` mock stays).
- Three-layer architecture: agent code middleware-agnostic; cases pattern-based; middleware specifics in RAG. New middleware in prod = new runbook chunk + one YAML, 0 code changes.
- Alert design: only user-facing SLO violations page; infra signals stay in dashboards + query surface.
- **Every sophisticated component ships with a number or it doesn't ship.** Breadth of half-features is the failure mode this project is designed to avoid.

**Week 1 verified live** (unchanged): 13 real containers healthy; 8 golden cases pass via `python mock/scripts/case_runner.py <id>`; `GS-RES-001-redis-oom` drives a real Redis OOM → real alert → AlertManager webhook → tracked incident; `GS-P-DEPENDENCY-DOWN-001` produces a real 4-alert cascade; in-mock metric names match production verbatim.
