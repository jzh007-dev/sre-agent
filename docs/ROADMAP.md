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

### Working rhythm — changed 2026-08-04

Lessons are executed as **steps**, and each step is two exchanges, not one:

1. **Design, signed off first.** Before writing code: the files to touch, the decisions with their alternatives, and the number the step will produce. Short — a screen, not a document. Stop there.
2. **Implement one step, then stop.** One commit's worth: the code, the test that would have failed before, the measured number. Report in a few lines and wait, rather than continuing into the next step.

The reason is not process for its own sake. L4a and L4b were each one large turn, which meant there was **no point at which a decision could be redirected** — and three of L4b's findings were things that would have been cheaper to catch in a design review than after the code was written. A step that ends with a stop is also a step whose direction can still be changed.

Non-negotiables from §3 below still apply per step: every step ships a number or a would-have-failed test, guards on ordered rule sets get mutation-tested, and doc updates may batch across a lesson's steps but not across lessons.

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
| L3b | **Live smoke, streaming, context pre-check, and verified prices.** Real SDK clients (`max_retries=0`, since transport owns retry), a `live_gateway()` factory wiring only credentialled providers, `srectl smoke`, **streaming** in transport plus the OpenAI-compat adapter, and a **context-window pre-check** in construction.<br><br>**Prices verified from the account's own invoice** rather than a pricing page — `srectl prices` derives rates from the billing export's per-token column and cross-checks by recomputing three days of charges, each reproducing the billed total to ten decimal places. `deepseek-v4-flash`: 1.00 / 0.02 / 2.00 CNY per 1M (miss / hit / output). A cache hit costs **2% of a miss**, making the live warm-call saving **94.9%** — the first figure reported was 85%, from a table written from memory, wrong in the favourable direction.<br><br>Also caught here: **`deepseek-chat` is an alias served by `deepseek-v4-flash`**, so the catalogue had been built on names that are not models — which breaks per-model pricing *and* [EVAL.md](../EVAL.md)'s `model_version` reproducibility key, silently. Routing now refuses aliases at wiring time ([§37](../TRADEOFFS.md#37-route-to-concrete-models-never-to-a-provider-alias)). | `agent/llm/{clients,transport,openai_compat,request,protocol,gateway,billing_csv,reconcile}.py`, `srectl/commands/{smoke,prices}.py`, `.env.example`, `tests/llm/{test_streaming,test_pricing,test_billing_csv}.py` | `[x]` |
| L4a | **Traceability spine — sequenced first because everything after it needs a sink.** An audit against the code found four instrument sources and zero sinks, three of them generated-then-discarded: `Gateway.tracer` defaults to a no-op, `transport.Attempt`'s error and delay are written and never read, and `run_to_completion` generates the whole event stream then keeps only the terminal event. There is **no timing anywhere in `agent/`**, which makes `p90 latency`, `time_to_first_verdict` and the critical-path profile uncomputable despite all three being declared metrics. Full audit in [TRADEOFFS §42](../TRADEOFFS.md#42-traceability-one-id-four-sinks--and-an-honest-audit-of-what-is-currently-wired).<br><br>Deliverables: one trace id **adopted from the alert's `correlation_id`**, joining the spine Week 1 already ships (`mock/services/_shared/observability.py` propagates it by header and stamps every JSON log line) — so a slow query becomes attributable to an investigation *from the observed system's own logs*. Four span levels (investigation → turn → llm.call/tool.call → attempt) with durations; `Attempt` detail wired into the trace instead of discarded; an optional event sink on `run_to_completion`; JSONL persistence implemented; structured logs.<br><br>Plus the **model-side circuit breaker**, deliberately symmetric with the provider one: count on `(tool_name, args_hash)` rather than name alone — the current `Investigation.tool_calls` cannot tell twelve different queries from the same query twelve times — and on the third identical call return an `is_error` carrying the previous result and a nudge, rather than aborting. Recoverable beats fatal. | `agent/core/{trace,events,loop,investigation}.py`, `agent/store/jsonl.py`, `agent/llm/{gateway,transport,clients}.py`, `srectl/commands/replay.py`; tests: `tests/agent/{test_trace,test_repeat_guard}.py`, `tests/store/test_jsonl.py` | `[x]` |
| L4b | **Trigger registry + layered dedup.** Alert is one entry mode of three; the per-trigger pre-processor plugs into harness step ②. `alert` real, `chat` / `patrol` stubs, all three normalising to one `Investigation`.<br><br>Dedup is **layered and ordered, and the order is the policy** — see [DIAGNOSIS P6](../DIAGNOSIS.md): **R0** a higher severity is never suppressed (dropping a P1 because a P2 just shipped would prolong an outage, so this precedes every suppression rule); **R1** same fingerprint with an investigation in flight → join; **R2** same fingerprint, already delivered, Δ < 5 min → drop; **R3** delivered, 5-10 min, count ≥ 3 → new investigation at raised severity, because recurrence right after a report means the fix did not take; **R4** burst aggregation (N in window → one event at a higher severity than any member) **scoped to sources without their own `for:` semantics**, since Prometheus `for:` already does this for metric alerts and only log-pattern alerts need it; **R5** otherwise new.<br><br>The key comes from AlertManager rather than being recomputed ([§38](../TRADEOFFS.md#38-alert-processing-alertmanager-owns-the-notification-layer-we-own-the-investigation-layer)), and **excludes `severity`** — including it would make a P2→P1 escalation look like a different alert and break both dedup and R3. Measured against a real AlertManager, that identifier is **`groupKey`, not `alerts[].fingerprint`** ([§38a](../TRADEOFFS.md#38a-the-dedup-key-is-groupkey-not-alertsfingerprint--measured-w2-l4b)). Also: **`resolved` handling** (self-healing mid-investigation is diagnostic information, not noise), a **storm cap** so a fleet-wide event spends one budget instead of N, an escalation ceiling, an observable flush for held alerts, thresholds in `config/alerting.yaml`, severity → budget tier, and the **`ResponseCache` TTL / eval-vs-production mode** gap from [§40](../TRADEOFFS.md#40-three-cache-semantics-and-why-the-gateway-refuses-semantic-matching). | `agent/core/dedup.py`, `agent/triggers/{registry,alert,chat,patrol,policy}.py`, `config/{alerting,budgets}.yaml`, `agent/llm/{cache,clients}.py`, `agent/core/investigation.py` (alert parsing relocated out of the kernel), `pyproject.toml`; tests: `tests/agent/test_dedup.py`, `tests/triggers/{test_alert,test_registry,test_policy}.py`, `tests/llm/test_cache.py` | `[x]` |

### The remaining W2 lessons, split into steps — re-planned 2026-08-04

**Why the split.** L4a and L4b each landed as one very large commit. The work was sound and the numbers real, but the *rhythm* was wrong in two ways that matter more than they sound: there was no point at which a decision could be redirected, and after each lesson there was still nothing to look at — every layer was real except the one that joins them. A plan whose progress is only visible in a test count is a plan with one reader.

So from here the unit of work is a **step**: one commit, one number or one test that would have failed before, and a stop. Steps are lettered inside their lesson so the `Current pointer` history still lines up with L3b / L4a / L4b.

**Why the order changed.** The table below runs `L6 → L7a → L5 → L4c → L7b → L8`, not in numeric order. An audit on 2026-08-04 found: harness 7 lines, prompt assembly 5, report schema 7, every sink a placeholder, no CLI entry. So the loop, the gateway, the trace and the trigger layer are all real and **nothing connects them** — the first end-to-end run of this project has been one lesson away for three lessons. Getting `srectl trigger` working buys three things that all later steps need: a real system prompt, a validated report object, and a place for a report to go. L4c's correlation and L5's MCP transport are both *better* built against a pipeline that runs than against one that does not.

**Two slices pulled forward from Week 3**, marked `[~]` in the W3 table rather than silently double-counted:

- **W3 L1's prompt assembly** → L6b builds `assemble()` and the minimum fragments. W3 L1 still writes the methodology (D-rules) that goes *inside* them.
- **W3 L7's report contract** → L6c ships the schema and the three fixed lines (`VERDICT` / `FIRST ACTION` / `CONFIDENCE`). W3 L7 still adds the body fields that need D-rules to exist (ruled-out rules with reasons, undo path).

| L | Step | Concept | Deliverable | Status |
|---|---|---|---|---|
| L6 | a-1 | **The harness, happy path.** Three layers — composition (owns order and nothing else) / the six step functions / a `HarnessContext` that wraps *every* step (trace, JSONL log, event sinks) rather than sitting between two, exactly as the gateway's cache/budget/tracing are not a fifth layer. **Two entry points, not one**: `intake()` for ①②, which is synchronous and yields **0..N** investigations (dedup can drop, patrol fans out), and `investigate()` for ③-⑥, async and per-investigation. Data between steps: `Route` / `Loadout` / `Result`. ③ **must not** recompute `window` or the severity budget — ② owns both, and recomputing would silently override the tier a P1 was given. ④ binds the LLM itself from an injected `LLMFactory`, because `SystemPrompt` travels through `Gateway.bind`, not through `LLM.call`, and flattening it to a string would throw away L3b's measured 94.9% prefix saving. Cost for ⑥'s numbers is read off the **trace** (`ledger.summary()` is already stamped on every `llm.call` span), so `core/` never imports the cost module.<br><br>**Must expose the event and span streams to an outside subscriber**, not swallow them — L6b consumes both over SSE, and discovering that after the harness is written means reopening it.<br><br>**Not this step:** the error invariant, `settle`, the stub sinks and the mutation set — L6a-2. Prompt *content* (L6d), report validation (L6e), integration resolution in ① (L5a). | `agent/core/harness.py`, `agent/sinks/{registry,stdout}.py`, `agent/tools/bundle.py`, `agent/prompts/assemble.py`, `agent/llm/protocol.py` (`LLMFactory`), `agent/triggers/{registry,alert,chat,patrol}.py` (`Trigger.sinks`); tests: `tests/agent/test_harness.py` (59), sharpened submodule resolution in `tests/test_architecture.py` | `[x]` |
| L6 | a-2 | **The harness, correctness.** The central invariant: **once an `Investigation` exists, every failure becomes an `Aborted` that still reaches ⑥** — so ③/⑤ raising, and a genuine bug, both deliver rather than vanish, and the bug's **traceback goes into the JSONL** rather than only to stderr (a per-investigation artefact that cannot explain its own failure is L4a's finding one layer up). **Retry lives in exactly one layer, the transport** — the loop, the harness and the sinks never retry, which is what makes "how many attempts" a knowable number. `Trigger.settle()` closes the dedup lifecycle, and **`mark_delivered` means a human can read it, not "we finished thinking"** — otherwise a broken Slack webhook becomes silent alert suppression via R2. An **abandoned generator** (a browser disconnecting, which is L6b's normal case) must still settle in a `finally`, or R1 joins a dead investigation forever. Stub `slack`/`jira` return `delivered=False`; a raising sink becomes `Delivery(delivered=False, detail=…)`, never `except: pass`.<br><br>**Not this step:** `tool_choice` and warning the model at a soft ceiling (W3 L8); crash-resume (L7b); concurrent writers on one investigation (L6b). | `agent/core/harness.py`, `agent/sinks/{slack,jira}.py`, `agent/triggers/{registry,alert,chat,patrol}.py` (`settle`); tests: failure paths + 6 mutations | `[ ]` |
| L6 | b | **Console backend — the event stream finally gets a consumer.** FastAPI app with `POST /trigger` (a golden case id → a background investigation), `GET /stream/{id}` (SSE: loop events *and* finished spans as they close), `GET /investigations`, `POST /chat`. Calls the harness directly, so the CLI in L6f and the ingress in L7 share one entry rather than three.<br><br>Why this is not decoration: `TurnStarted` / `TextDelta` / `ToolCalled` / `ToolReturned` are generated today and thrown away — `run_to_completion` collapses them to one terminal event. That is the same "produced and discarded" pattern the [§42](../TRADEOFFS.md#42-traceability-one-id-four-sinks--and-an-honest-audit-of-what-is-currently-wired) audit was written to shame, one layer up. The numbers: time-to-first-event after `POST`, and events/second sustained under a full run.<br><br>**Also records the span attributes we emit, as a table** — agent-execution tracing is a crowded space (Langfuse, LangSmith, Phoenix, Weave, Braintrust, Traceloop) converging on OTel's GenAI semantic conventions, and the console must be *a* consumer of a standard trace rather than the reason the trace has its shape ([§43](../TRADEOFFS.md#43-operator-console-hand-write-the-driving-surface-never-the-trace-viewer)). The convention could not be fetched from this machine, so **W3 L3 verifies the names against the published spec and renames ours to match** — no guessed `gen_ai.*` names go into code here.<br><br>**Not this step:** the console page itself (L6c); Langfuse as the after-the-fact trace viewer (W3 L3, [§43](../TRADEOFFS.md#43-operator-console-hand-write-the-driving-surface-never-the-trace-viewer)). | `agent/api/{entrypoint,stream}.py`, `pyproject.toml` (fastapi + uvicorn, earlier than L7) | `[ ]` |
| L6 | c | **Console page — one HTML file, zero build.** Live span tree growing as the run proceeds; the **dedup decision panel** (which of R0-R5 fired, on what key, with the reason); tiles for cost / elapsed / p90 / cache hit rate / budget consumed. Vanilla JS + `EventSource`, no npm — a node toolchain in a Python repo is operational cost with nothing to show for it.<br><br>**It is a deliverable, not a dev tool**: it must display *measured* values from the ledger and the trace, never placeholders. A page with no numbers is decoration, and this is [W7's cost-latency dashboard](#week-7--polish--demo) cashed in early. R0-R5 is the part with no other window: ordered rules whose order is the policy, currently visible only in test output.<br><br>**Not this step:** real tool data — every tile shows what a canned run actually produced (real queries land W3 L2); Slack/Jira delivery (W5/W7). | `agent/api/static/console.html`, `agent/api/static/console.js` | `[ ]` |
| L6 | d | **Minimum system prompt.** `assemble()` with the cache-prefix order `[A]` role+methodology → `[B]` output contract → `[C]` integration facet → `[D]` budget. Minimum content only — the D-rules are W3 L1. The number: measured prompt-cache hit rate on a second call, which is what the ordering exists for.<br><br>**Not this step:** the D-rules themselves (W3 L1) and the integration facet's text (L5a). **Does** include `PromptFragment.version`, since EVAL's `prompt_version` reproducibility key has nowhere to come from without it. | `agent/prompts/{assemble.py,methodology.md,output_contract.md}` | `[ ]` |
| L6 | e | **Report schema + a `parse` step that validates.** `submit_report` gets a real schema and the three fixed lines: `VERDICT` (origin / symptom-of / undetermined), `FIRST ACTION` (**exactly one**, or "none — investigate further"), `CONFIDENCE` (`unknown` is legal). Harness ⑤ reads a validated object, not prose. A rejected report comes back as `is_error` so the model can fix it.<br><br>**Not this step:** the body fields that need the D-rules to exist — ruled-out rules with reasons, the undo path, mitigation-vs-root-cause (W3 L7). | `agent/core/report.py`, `agent/tools/report.py` | `[ ]` |
| L6 | f | **`srectl trigger --scenario <case-id>` + the first live run.** Same harness entry as the console, printing the report and the span tree. Needed alongside the page because `eval/run.py` needs a programmatic path and the W2 exit criteria names this command. **First real LLM run of the project** — cost and latency published as-is.<br><br>**Not this step:** any claim about reasoning quality. Tools are still canned (W3 L2), so the numbers are harness-layer only. | `srectl/commands/trigger.py` | `[ ]` |
| L5 | a | **Integration config layer.** An integration is a YAML file, not a Python class: `match` rules, `runbook_namespace`, `prompt_fragment`, `notifier` list. The number that matters: Python lines to add integration #2 = **0**, enforced by a test.<br><br>**Not this step:** the MCP transport (L5b) and `inbound_mapping` (L5c). | `agent/integrations/{registry,config}.py`, `config/integrations/observability.yaml` | `[ ]` |
| L5 | b | **MCP over real stdio.** `observability-mcp` as an actual subprocess speaking MCP; returns stay canned. Plus `ToolMeta` (`side_effect` / `cost_hint` / `timeout`) reaching the bundle. Why two servers not one: deploy has WRITE side effects and belongs in its own permission scope (W6).<br><br>**Not this step:** real query bodies — returns stay canned until W3 L2; the deploy server's own WRITE permission scope (W6). | `mcp_servers/observability/`, `agent/tools/mcp_client.py` | `[ ]` |
| L5 | c | **Declarative `inbound_mapping`.** jsonpath field mapping covers the common webhook; an optional Python normalizer is the escape hatch. Proves the inbound schema is pluggable independently of the tool bundle — which is what W5's Grafana-inbound-only claim rests on.<br><br>**Not this step:** a second *live* integration (W5) — this proves the inbound schema is pluggable, not that two backends exist. | `agent/integrations/mapping.py` | `[ ]` |
| L4c | a | **Fixture first: a real cascade.** Reshape `GS-P-DEPENDENCY-DOWN-001/alert.json` into a real AlertManager webhook (`alerts[]`, `fingerprint`, `startsAt`, `status`) captured from the live stack, add the three sibling cascade alerts, and record the **expected grouping** rather than a count. Sequenced first: correlation with no case to grade against is an assertion.<br><br>**Not this step:** any scoring (L4c-c); periodicity detected from history rather than declared (W4). | `eval/golden/GS-P-DEPENDENCY-DOWN-001/*`, `config/topology.yaml` | `[ ]` |
| L4c | b | **Declared inhibition.** Runs **before** any score, because known causality (`up{service=X}==0` ⇒ every error-rate alert on X is a symptom) is more certain than inference. Edges carry `required` (a degradable dependency should not propagate correlation) and `sync` (an async hop needs a wider window).<br><br>**Not this step:** the score components (L4c-c) — inhibition deliberately runs before any of them. | `agent/core/correlate.py` | `[ ]` |
| L4c | c | **Scoring, for what inhibition does not cover.** Onset *ordering* (not closeness — the same topology means "payment broke checkout" or "checkout is hammering payment" depending on which onset came first, D3 vs D5), topology adjacency at **k=2 with per-hop decay** (the cascade spans payment→checkout→gateway), periodicity as a **negative** weight from config until W4 can detect it. Storm cap gains its correlation-layer meaning: past N alerts, stop scoring O(n²) pairs.<br><br>**Not this step:** the advisory `CorrelationDecision` record and its four destinations (L4c-d); learned co-occurrence (W4). | `agent/core/correlate.py`, `config/topology.yaml` | `[ ]` |
| L4c | d | **Every merge is advisory.** A `CorrelationDecision` — rule, score, components, path, onset delta, confidence — to **four destinations**: the investigation, `messages` (so the agent can disagree, [§39](../TRADEOFFS.md#39-merging-is-advisory-not-destructive)), the event stream, and eval. The exit-table row this closes: 4-alert cascade → **1** investigation.<br><br>**Not this step:** `correlation_overridden_rate` as a *measured* number — it needs a judge (W3 L9). | `agent/core/correlate.py`, harness ② wiring | `[ ]` |
| L7 | b | **FastAPI ingress.** Single entry dispatching by trigger type; idempotent on `alert_id` (AlertManager retries webhooks); returns 202 + `investigation_id`; in-process asyncio task + in-memory registry + the JSONL log L4a already writes. Temporal stays out of scope ([§32](../TRADEOFFS.md#32-temporal-is-out-of-scope-not-deferred)). Lands *after* `srectl trigger` because a CLI is the cheaper way to get the pipeline right.<br><br>**Not this step:** auth beyond a shared secret (W6); durable execution — Temporal is out of scope, not deferred ([§32](../TRADEOFFS.md#32-temporal-is-out-of-scope-not-deferred)). | `agent/api/entrypoint.py` | `[ ]` |
| L8 | a | **`eval/run.py` + metrics.** Harness-layer metrics are measurable under canned tools because they are properties of the harness and gateway, not of intelligence: termination rate, median turns, median tool calls, median cost, p90 latency, cache hit rate. Also aligns [EVAL.md](../EVAL.md) to Week 1's actual 3-file golden format.<br><br>**Not this step:** accuracy, or any judge-scored metric — those need W3 L9's anchor set and kappa gate first. | `eval/run.py`, `eval/metrics.py` | `[ ]` |
| L8 | b | **The baseline table, published as-is.** All 8 cases, real numbers, no targets moved. Plus a rewrite of [INCIDENT_WALKTHROUGH.md](../INCIDENT_WALKTHROUGH.md) — currently pre-pivot prose with illustrative numbers — against a real `GS-*` run. It ships here with ugly real numbers rather than in W7 with pretty invented ones: a project whose thesis is measurement cannot have its flagship narrative be fiction.<br><br>**Not this step:** the per-difficulty breakdown and the 30-day accuracy trend (W7). | first baseline table in `docs/`, rewritten `INCIDENT_WALKTHROUGH.md` | `[ ]` |

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
| Every abort carries a reason **and** a duration; a failed run leaves a JSONL trail | yes/no |
| Repeated identical tool call → contained on the 3rd, run still recoverable | yes/no |
| Suppressed / held / escalated alert counts | recorded, not silent |

The three "published as-is" rows are the point of this week. `median tool calls < 12` and `p99 cost < $0.40` were written before a loop had ever run; unassisted agentic RCA plausibly costs 30-100 tool calls. If the real figure is 40, we publish 40 and W3's precompute layer attacks it — we do not quietly move the target.

Plus: `srectl trigger --scenario GS-RES-001-redis-oom` runs end-to-end with a real LLM call, real MCP transport, canned tool returns, and a Langfuse trace showing every LLM and tool call. **No reasoning quality is claimed.**

---

## Week 3 — Loop intelligence

Goal: real reasoning inside the loop. Real queries against the Week-1 stack, a written methodology, a deterministic precompute layer, and the first accuracy number.

> **Re-planned twice.** The pre-pivot table (one prompt per node, routing by phase, `graph/nodes/*.py`) was void — those nodes do not exist. The 2026-08-01 capacity review then added three things and paid for them by cutting elsewhere (see **Scope cuts** in the pointer): a written [DIAGNOSIS.md](../DIAGNOSIS.md) replacing the one-line "elimination tree", a **precompute layer** because unassisted agentic RCA rediscovers topology and change timelines every incident, and **scoped tracing** for per-request causal chains. Nine lessons; this is the intelligence week and the depth went here.

| L | Concept | Deliverable | Status |
|---|---|---|---|
| L1 | **Methodology, written down + layered system prompt.** [DIAGNOSIS.md](../DIAGNOSIS.md) is authored first: numbered precompute rules (P1-P6), elimination rules with explicit activation *and* pruning conditions (D1-D7), and a named boundary for what stays in-model. Every rule maps to a golden case or a deterministic checker; rules without one are marked as gaps rather than assumed. The governing constraint is that DIAGNOSIS holds middleware-**agnostic** procedure only — "require a change to precede the symptom" is procedure, "Redis noeviction fails writes" is a runbook chunk ([§20](../TRADEOFFS.md#20-middleware-specific-knowledge-lives-in-rag-not-in-agent-code-or-cases)). Prompt assembly order is dictated by cache prefixing (most static first): `[A]` role + methodology → `[B]` output contract → `[C]` integration facet → `[D]` budget. Plain markdown + `assemble()`; no jinja2 for four fragments.<br><br>**Partially pulled forward to W2 L6b** — `assemble()`, the cache-prefix order and the minimum fragments ship there, because a pipeline that runs needs *a* system prompt. What stays here is the content that is the actual work: the numbered D-rules, their activation *and* pruning conditions, and the rule-to-case coverage table. | `DIAGNOSIS.md`, `agent/prompts/{methodology,output_contract}.md`, `agent/prompts/integrations/*.md`, `agent/prompts/assemble.py` | `[~]` |
| L2 | **Real observability tools.** PromQL against real Prometheus; SQL against real ClickHouse; topology derived from the client-side `downstream_requests_total{service,downstream,outcome}` metrics Week 1 L5 already emits. Every query inherits `Investigation.window`. Results shaped to a token budget — a raw ClickHouse page is megabytes. | `mcp_servers/observability/` real implementations | `[ ]` |
| L3 | **Traces, scoped and falsifiable.** OTel instrumentation (the `correlation_id` propagation from W1 L5 is the foundation) + a Tempo container + one `query_traces` tool returning **edge-level aggregates**, with a single request's span tree only on explicit `trace_id` drill-down. **Never raw span dumps** — that is the fastest way to destroy a context window. Framing matters: per-edge RED is already available from Prometheus, so what traces genuinely add is *where the time went inside one slow request*. Kept only if it earns a measured margin — see the exit table. [TRADEOFFS §29](../TRADEOFFS.md#29-trace-scope-per-request-causal-chains-not-a-third-pillar). | `mock/tempo/`, OTel wiring in `mock/services/_shared/`, `query_traces` in `mcp_servers/observability/` | `[ ]` |
| L4 | **Precompute layer in harness ②/③.** The deterministic joins the model should never be doing by eye: P1 merged timeline (alert firing, metric onset, log-error onset, deploys — with pairwise lead-lag in seconds), P2 topology graph, P3 blast radius including services that are sick without paging, P4 ranked candidate shortlist, P5 cascade root-vs-symptom. Onset *ordering* is arithmetic, and the entire elimination tree is built on it — leaving it to the model puts a silent error class under everything. **The hard constraint: a shortlist, never a conclusion.** If the final root cause is always the top candidate, the LLM is a narrator for a heuristic, which is why `precompute_override_rate` is in the exit table ([TRADEOFFS §28](../TRADEOFFS.md#28-precompute-produces-a-shortlist-never-a-conclusion)). | `agent/core/precompute.py`, shortlist logged into the trace so precompute bugs are separable from reasoning bugs | `[ ]` |
| L5 | **Parallel tool calls.** One assistant response can carry several `tool_use` blocks; `asyncio.gather` is already wired in L2's dispatch — this lesson measures it on `GS-P-DEPENDENCY-DOWN-001` (a real 4-alert cascade) and tunes concurrency. | wall-clock delta recorded | `[ ]` |
| L6 | **Context management.** What the phase graph gave for free and a loop must do explicitly: per-result token cap (the 20k-char valve in `tools/dispatch.py` is a stopgap, not a policy), oldest-result folding, compaction above a threshold. | `agent/core/context.py` | `[ ]` |
| L7 | **Report contract, not just a schema.** `submit_report` with a forced Pydantic schema, and the first three lines fixed: `VERDICT` (origin / symptom-of / undetermined), `FIRST ACTION` (**exactly one**, or "none — investigate further"), `CONFIDENCE`. A list of five actions is a way of not deciding, and it pushes the decision back onto someone already overloaded. Mitigation and root-cause fix labelled separately where they differ. Body: root cause, evidence, ruled-out D-rules with the reason each was pruned, open questions, assumptions, undo path. `confidence: "unknown"` is legal. Harness ⑤ reads a validated object instead of parsing prose.<br><br>**Partially pulled forward to W2 L6c** — the schema, the validating `parse` step and the three fixed lines ship there, because harness ⑤ has to read *something*. What stays here are the body fields that need the D-rules to exist first: ruled-out rules with the reason each was pruned, the undo path, and mitigation-vs-root-cause when they differ. | `agent/core/report.py`, contract section in [DIAGNOSIS.md](../DIAGNOSIS.md) | `[~]` |
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

Slack integration (the human-loop surface, so it is not cosmetic), ~~cost/latency dashboard~~ (**pulled forward to W2 L6b/L6c** as the operator console — the event stream needed a consumer four lessons earlier than this, and a dashboard is where the measured numbers become visible instead of quoted; what remains here is whatever the console still lacks after real tools land), 3-minute demo video, `INTERVIEW_CHECKLIST.md` filled with real numbers, repo hygiene. [INCIDENT_WALKTHROUGH.md](../INCIDENT_WALKTHROUGH.md) is *not* here — it moved to W2 L8, because a project whose thesis is measurement cannot leave its flagship narrative as fiction until the last week.

**Exit numbers**: `EVAL_REPORT.md` with per-difficulty breakdown; 30-day accuracy trend annotated with prompt and model changes; someone unfamiliar clones the repo, runs `make demo`, and understands what they saw in 60 seconds.

*Lesson table written at the end of Week 6.*

---

## Open gaps

Reviewed 2026-08-01. Items 1-6 were identified when the plan was re-scoped; 9-14 came from the capacity review the same day.

| # | Gap | Why it matters | Landing |
|---|---|---|---|
| 1 | **Alert-storm correlation** | The highest-value behaviour a real incident copilot has, and `GS-P-DEPENDENCY-DOWN-001` already reproduces it. | Dedup (same condition) ✅ W2 L4b; correlation (different conditions) W2 L4c; similarity W4 |
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
| 15 | **Maintenance windows / silences** — a deploy window should neither page nor spend budget. Standard in every alerting product; absent here, so an eval run during a declared maintenance would still burn money. | Cheap (a matcher plus a time range) but not what L4b was about, and **deliberately not smuggled into it** — it is a *suppression* rule, and L4b's whole discipline was that the ordered set stays the one in DIAGNOSIS. AlertManager's own `silences` API is the honest place for it. | W6 |
| 16 | **Semantic reuse for chat across sessions** — offer an existing report for a similar question, with a freshness bound. Needs embeddings. May **offer**, never suppress ([§41](../TRADEOFFS.md#41-semantic-similarity-may-inform-it-may-never-suppress)). | W4 |
| 17 | **Historical co-occurrence learning** — "these two alerts always fire together" is what BigPanda/Moogsoft learn; we hand-declare it. Also real periodicity detection, which L4c can only read from config. | W4 (needs episodic memory) |
| 18 | **Shared-dependency and co-location topology edges** — "A and B both call X" creates correlation without a direct edge; same-host/AZ matters in production and is meaningless in docker-compose. | Tier 2 |
| 19 | **Declared-vs-derived topology diff** — once W3 L2 derives the graph from `downstream_requests_total`, diffing it against `config/topology.yaml` turns a stale declaration into a *finding* rather than a silent miscorrelation. Free once both exist. | W3 L2 |
| 20 | **Flapping is dropped by R2** — a condition that resolves and re-fires within five minutes is suppressed, because a report for it went out four minutes ago. Reading that report *is* the right first move for on-call, so this is R2 working as designed; but a genuinely oscillating P1 gets one report and then silence. | Needs a case before it needs a rule — inventing an R2b on reasoning alone is how an ordered rule set rots. `resolved_at` is already recorded per key, so the rule is cheap once a case exists. | W6, with the golden-set expansion |
| 21 | **Langfuse is not wired at all** — the cross-week track says "from Week 2 L3 onward every LLM call is traced through the gateway — gaps are bugs", and this is that gap: `langfuse` appears in a `pyproject.toml` comment and nowhere else. The span model exists ([§42](../TRADEOFFS.md#42-traceability-one-id-four-sinks--and-an-honest-audit-of-what-is-currently-wired)) and a sink is a callable, so this is small. | W2 L6b/c give the *live* view; Langfuse is the after-the-fact one, and [§43](../TRADEOFFS.md#43-operator-console-hand-write-the-driving-surface-never-the-trace-viewer) says we do not hand-write that. | W3 L3, alongside OTel + Tempo, which need the same sink |
| 22 | **`tool_choice` is never used** — the strongest per-call constraint the providers expose, and it appears nowhere in the codebase. Today, hitting `max_turns` spends the entire budget and produces **nothing**; forcing the report tool on the final turn would make that a degraded report instead. Forcing `any` on turn 0 would stop the model answering in prose without querying anything. | Not blocked: `params` is already passed through by both adapters and is already in the cache key. What is missing is (a) a provider-agnostic translation, since the shape differs between Anthropic and OpenAI, and (b) the policy for *when* to force, which has to live in `core/loop.py`. | W3 L8, with the insufficient-evidence report — that lesson needs this mechanism anyway |
| 23 | **An LLM-call failure never reaches the model.** A *tool* failure becomes evidence it can act on (`ToolResultBlock(is_error=True)`, plus the repeat guard's nudge and previous result); an LLM failure is either retried invisibly or terminates the run. Two opposite policies in one codebase, and the tool one is the better of the two. | The channel already exists and needs no kernel change: `loop.run` yields `TurnStarted` *before* `await llm.call`, and an async generator suspends at `yield`, so a consumer that mutates `inv.messages` at that moment has it picked up by the next call. L6a-2 pins that ordering with a test, because W3 L6's compaction and W3 L8's soft-ceiling warning both stand on it. | W3 L8 |
| 24 | **`PromptFragment` carries no version** — [EVAL.md](../EVAL.md)'s reproducibility key is `(seed, model_version, prompt_version, golden_set_version)` and `prompt_version` has nowhere to come from. W7's "accuracy trend annotated with prompt and model changes" depends on it too. | One field plus a span stamp. Deferred to L6d rather than L6a because the first live LLM run is L6f, which comes *after* L6d, so nothing is lost — and versioning an empty fragment is noise. | L6d |
| 25 | **A crash mid-turn leaves an unresumable log.** The process dying between an assistant `tool_use` and its `tool_result` leaves a trailing unanswered `tool_use` in the JSONL; resuming from it would send an invalid `messages` array. L4a asserted reconstruction fidelity for *complete* runs only. | ~10 lines (drop a trailing assistant message whose `tool_use` blocks have no matching results), but nothing resumes yet, so building it now would be a fix with no consumer. | W2 L7b, with the ingress |
| 26 | **Two writers on one investigation.** L4b's `_absorb` mutates `inv.messages` while the loop may be mid-turn. Safe today because there is exactly one writer; the moment L6b's API lets `preprocess` run concurrently with a live `investigate` on the same investigation, it is a race. | Either a per-investigation lock, or queue the injection to the `TurnStarted` boundary — which gap #23's invariant already establishes as the natural serialization point. | W2 L6b |

---

## Cross-week tracks (run in parallel, small time slices)

- **Eval as a track, not a deliverable.** [EVAL.md](../EVAL.md) opens by calling the eval system "the load-bearing artifact of this project" — so `eval/run.py` exists from Week 2 L8 and the metric set grows weekly. Every week's exit criteria is a row from it.
- **Integration parity**: every new capability gets asked "does this work for all registered integrations, or only observability?" A capability that only works for one is a leak in the seam rule.
- **Interview checklist**: after every substantive change, add a data point / decision line to `INTERVIEW_CHECKLIST.md`.
- **Tradeoffs**: any non-obvious decision → a new entry in `TRADEOFFS.md` with Alternatives + Why + Cost + Reconsider-when.
- **Langfuse observability**: from Week 2 L3 onward every LLM call is traced through the gateway — gaps are bugs.

---

## Current pointer

**Session date**: 2026-08-04
**Last completed**: Week 2 L4b — trigger registry and layered dedup (363 tests green). See the L4b section at the end of this pointer.

The subsections below are in chronological order; **the last one is the live handoff.**

Key events on **2026-08-01** — **Week 2-7 re-planned**, no code changes:

- Named the five pillars (harness / loop / gateway / RAG / integrations) and the **seam rule**: adding a trigger, integration, sink, or provider must not touch `loop.py` or `harness.py`.
- **Harness recognized as a missing layer.** The system is not a bare agent loop; it is a deterministic 6-step pipeline *containing* one. Steps ①②⑥ vary by trigger; ③④⑤ are identical across alert / chat / patrol. New [TRADEOFFS §23](../TRADEOFFS.md#23-harness-deterministic-pipeline-around-the-agent-loop-refines-22).
- **Gateway recovered.** [§3](../TRADEOFFS.md#3-llm-gateway-in-process-wrapper-not-litellmportkey-service) promised routing / cache management / cost accounting / tracing; the pivot renamed it "LLM adapter layer" and dropped four of the five responsibilities behind "SDK-native" — which is true only for retry. [EVAL.md](../EVAL.md) lists the gateway as the sole source of `cost_usd`, and gateway-level response caching is what makes eval both affordable and reproducible. Scheduled W2 L3.
- **Multi-provider scoped down.** Two adapter classes total (`OpenAICompatLLM` covers DeepSeek / Qwen / Kimi by `base_url`; `AnthropicLLM` is the different-family class). Justification changed from cost to *requirement*: SECURITY L3 needs a different-family reviewer and EVAL needs a judge that differs from the agent. Quality parity across providers is explicitly **not** promised.
- **Integrations became configuration.** A YAML file plus an MCP server; zero Python per integration. Abstraction cost becomes "0 lines of Python" rather than "47 lines."
- **Trigger registry added.** Alert is one entry mode of three (alert / chat / patrol). Chat forces `messages` to belong to a persistable `Investigation` and forces the loop to yield events; patrol forces per-investigation budgets. Both are cheap now and invasive later, so they land in W2 L2.
- **Integration breadth cut from 5 real to 2 real.** The claim worth making is a measured abstraction cost, not a count. Freed budget went to gateway and eval.
- **Eval promoted from Week-6 deliverable to cross-week track**, with harness-layer baselines measurable in Week 2 under stub tools.
- Week 2 grew from 5 to 8 lessons and is now the heaviest week — all mechanical, no prompt tuning. (L4 later split into L4a/b/c and L3 into L3/L3b, taking it to 11.)
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

### Week 2 L3b closed — 2026-08-03

190 tests green. Everything except `srectl smoke` and `srectl prices` still runs offline.

**Cost accounting is now measured rather than asserted.** The rates came from the account's own billing export, and `srectl prices` self-checks by recomputing each billed day before it is trusted to judge anything — three days reproduced their totals exactly to ten decimal places. `prices_verified: true` now propagates through the ledger honestly, and it is **per provider**: Anthropic's rates are still from memory and still say so.

Four things were found by measuring instead of assuming, and each was wrong in a way that would not have surfaced on its own:

1. **`deepseek-chat` is an alias**, served by `deepseek-v4-flash`. The catalogue had been built on two names that are not models — breaking per-model pricing, and silently invalidating EVAL's `model_version` reproducibility key across any provider upgrade. Routing now refuses aliases at wiring time, and every response records `served_model` so an *uncatalogued* alias is flagged too.
2. **The prompt-cache discount is 98%, not ~90%**, so the real warm-call saving is 94.9% rather than the 85% first reported. The guess erred in the favourable direction — the direction that never gets questioned.
3. **DeepSeek has no cache-write premium**; the 1.25x default would have invented a charge that does not exist.
4. **Two cache-token fields**, not one: reading only `prompt_cache_hit_tokens` meant that if the provider ever dropped it, cached tokens would silently be billed at full input rate with no error.

**Currency conversion was removed entirely** at the same time. An exchange rate makes a wrong rate indistinguishable from a price change, which left reconciliation unable to answer its own question. Costs are now reported in the provider's own currency, per currency, never summed across — checked first that nothing needs a mixed total, and nothing does.

The "shared account confounds reconciliation" caveat was also **observed rather than hypothesised**: a second API key on this account moved the balance during the session, which is exactly why the invoice route (per model *and* per key) beats the balance endpoint.

### Week 2 L4 designed and split — 2026-08-03 (docs only, no code)

A design review of alert dedup/correlation against real production practice produced five decisions and one audit, all recorded in TRADEOFFS. L4 became **three lessons in dependency order**, because the later two cannot be built first:

- **[§38](../TRADEOFFS.md#38-alert-processing-alertmanager-owns-the-notification-layer-we-own-the-investigation-layer)** — we were about to reimplement part of AlertManager. It already computes the fingerprint, groups notifications, and suppresses declared causal pairs. The division: **AlertManager owns the notification layer (protecting a human's attention), we own the investigation layer (protecting a token budget)**. We consume its fingerprint instead of inventing a second definition of "the same alert", and declared inhibition runs before any score because known causality beats inference.
- **[§39](../TRADEOFFS.md#39-merging-is-advisory-not-destructive)** — merging is **advisory, not destructive**. Commercial correlation engines feed a human dashboard, so a merge is irreversible; our consumer is an LLM, so we can merge *and* hand over the reason and confidence and let the agent disagree. That lowers the cost of over-merging and therefore relaxes the threshold — but not to liberal, because budget is still shared.
- **[§40](../TRADEOFFS.md#40-three-cache-semantics-and-why-the-gateway-refuses-semantic-matching)** — three cache semantics, and the gateway's must stay **exact**. Semantic caching is a popular pattern and wrong here: two 0.97-similar prompts can need opposite answers, and a fuzzy hit would corrupt eval while every metric looked healthy. This also exposed a real gap — `ResponseCache` has no TTL and no eval/production mode.
- **[§41](../TRADEOFFS.md#41-semantic-similarity-may-inform-it-may-never-suppress)** — semantic similarity may **inform and offer, never suppress**. The costs are wildly asymmetric: a stale chat answer costs a re-read, a suppressed P1 costs an outage. And `GS-LOAD-001` exists partly to prove the precedent can itself be the liability.
- **[§42](../TRADEOFFS.md#42-traceability-one-id-four-sinks--and-an-honest-audit-of-what-is-currently-wired)** — an audit of what tracing actually exists found **four instrument sources and zero sinks**, three of them generated-then-discarded, and **no timing anywhere in `agent/`**. A failed run currently leaves nothing but an `Aborted` reason string. Traceability is therefore sequenced **first** as L4a, because §39's four-destination requirement and L4b's dedup decisions both need a sink that does not yet exist.

Two things worth carrying forward from the audit:

- **A fortunate interaction, not a designed one**: a model repeating an identical call hits our response cache, so `money_spent` stays flat — but `budget_charged` grows, because a hit replays the original cost. The budget gate reads `budget_charged`, so it catches the loop. Under the naive "cache hits are free" design the loop would have been *free*. A choice made for eval reproducibility bought runaway protection.
- **Runaways already stop; they just cannot be diagnosed.** Six ceilings guarantee termination. What is missing is that `tool_calls` counts by tool *name*, so twelve different queries and the same query twelve times are indistinguishable, and the reason says `max_turns` either way. Hence the model-side circuit breaker in L4a.

### Week 2 L4a — the traceability spine, closed 2026-08-03

**233 tests green** (190 → 233). Everything offline; no API key and no mock stack were needed.

Four span levels with durations (investigation → turn → llm.call/tool.call → attempt), a JSONL sink and a structured-log sink, `srectl replay`, and the model-side circuit breaker. The audit table in [§42](../TRADEOFFS.md#42-traceability-one-id-four-sinks--and-an-honest-audit-of-what-is-currently-wired) gained a third column; all seven rows moved.

**The numbers, measured rather than asserted** — the claim being checked was §42's own "negligible at this QPS". A 3-turn investigation with 4 tool calls, median of 400 runs:

| | |
|---|---|
| untraced | 0.245 ms |
| spans, in-memory sink | 0.263 ms — instrumentation costs **0.017 ms** |
| spans + events → JSONL | 1.409 ms — the durable log costs **1.147 ms** |
| one span site | 2.1 µs off / 3.5 µs on |
| log size | 6.3 KB; ~31 KB extrapolated to the 15-turn ceiling |
| **reconstruction** | **exact — 7/7 messages, 12/12 blocks** |

The split is the interesting part: instrumentation is free and **98% of the cost is the file**, because every record is one open-write-close so nothing is buffered in-process where a crash could swallow it. That is now a decision with a price (1.15 ms, and the fix if it ever matters is a held handle plus a per-line flush) rather than an unexamined default. The whole 1.4 ms is 0.0016% of the 90-second p90 target.

Three deltas from the design as written, each recorded in §42 with its reasoning:

1. **The trace id is ours; the alert's `correlation_id` is an attribute on every span.** The literal "adopt the correlation_id as the trace id" collides on a golden-case rerun (fixtures use a case slug) and on L4b's R3 rule, which deliberately mints a second investigation for one fingerprint. The grep-join is unaffected; Tempo will not show two runs as one trace.
2. **Sinks moved from the gateway to the `Trace`.** `Gateway.tracer` was a flat dict with no duration and no parent; keeping both shapes for one event would have meant writing the duration twice. Emission stays at the gateway, so coverage is still structural.
3. **Attempt spans arrive by callback, not from the return value** — `Transport.call(request, on_attempt=…)`. The returned list is lost with the exception on the failure path, which is the path most worth having evidence from.

Two things worth carrying forward:

- **A Python detail that would have silently corrupted the span tree.** The `investigation` and `turn` spans stay open across `yield`, and an async generator runs in its *caller's* context. Verified on CPython 3.14: step the stream from a Task and close it from the caller — what chat does on cancellation — and `ContextVar.reset(token)` raises `ValueError: <Token …> was created in a different Context`. Spans restore the previous parent by `set(previous)` instead. The first version of the test did **not** catch this: it drove the generator from one coroutine, so the mutation to `reset(token)` passed all 233 tests. The empirical check came first, then the test, then the mutation.
- **`from . import trace` exposed a false positive in the seam-rule test.** `_agent_imports` resolved a bare `from . import x` to `agent.core`, which is not in the allowed prefixes — so a legal sibling import failed the build while saying nothing about *what* was imported. Fixed by resolving each name individually. The guard was fail-safe, never permissive, but it was imprecise.

**Five guards mutation-tested**, so they are known to bite rather than assumed to: `reset(token)` for restore-by-set → 1 error; `args_hash` without `sort_keys` → 1 failure; `on_attempt` on the success path only → 2 failures; the repeat guard keyed on tool name alone → 7 errors; one loop exit path not stamping its outcome → 1 error.

### Week 2 L4b — trigger registry and layered dedup, closed 2026-08-04

**363 tests green** (233 → 363). Everything offline; no API key needed. One container *was* run, deliberately — see below.

Three triggers registered (`alert` real, `chat` / `patrol` stubs that genuinely normalise), the ordered rules R0-R5 in `agent/core/dedup.py`, thresholds and severity→budget tiers in `config/`, and [§40](../TRADEOFFS.md#40-three-cache-semantics-and-why-the-gateway-refuses-semantic-matching)'s cache-mode gap closed. `Investigation.from_alert` and `strip_fixture_metadata` moved out of `core/` into `triggers/alert.py`, as `investigation.py`'s own docstring said they would: the kernel could parse an AlertManager payload, which passed the seam test while leaking semantically.

**The design question was settled by measurement, not by reading.** P6a asked for a key that *comes from AlertManager* and *excludes `severity`*; no single field satisfies both. So a real `prom/alertmanager:v0.27.0` — the image the mock stack pins — was run with a webhook receiver and one condition posted at P2, then at P1:

| | measured |
|---|---|
| `alerts[].fingerprint`, P2 vs P1 | `327b605fce1b794f` vs **`3277605fce179078`** — differs |
| `groupKey` | `{}:{alertname="HighErrorRate", service="auth"}` — severity-free |
| grouping | both severities arrived in **one** webhook under that key |
| `commonLabels` in that webhook | **`severity` absent** |
| firing `endsAt` | `"0001-01-01T00:00:00Z"` — Go's zero time |

So the dedup key is **`groupKey`**, which is *more* faithful to §38's "consume, don't reinvent" than a hash of our own would have been; fingerprints are carried per member for the join to their log. Two traps came free with the payload: reading severity from `commonLabels` returns nothing exactly when an escalation happens, and Go's zero `endsAt` parses into a year-1 datetime that sorts ahead of every real timestamp. Recorded as [§38a](../TRADEOFFS.md#38a-the-dedup-key-is-groupkey-not-alertsfingerprint--measured-w2-l4b).

**The numbers**:

| | |
|---|---|
| dedup decision | 3.3 µs (R5) / 3.8 µs (R2) |
| storm cap with 200 keys in flight | 16.4 µs |
| full pre-process (parse + decide + build) | **36.8 µs**, p95 61 µs |
| 8-delivery sequence exercising R0-R5 | 4 investigations created, 2 joined, 2 dropped |
| 10 identical deliveries of one webhook | **1** investigation |
| 40 distinct conditions, storm cap 8 | **9** investigations (8 + one fleet aggregate); uncapped, 40 |
| 8 golden fixtures | 8 investigations; 6×P1 → 60 tool calls, 2×P2 → 40 |
| `<dedup>` block on an R3 reopen | +304 chars over the plain first message |

**Three bugs found by measuring, each of which passed every test that existed:**

1. **The storm cap capped nothing.** Returning `aggregate` for every excess condition changed the rule name in the record and still created 40 investigations for 40 conditions. Now the first excess condition opens one fleet-wide aggregate and the rest **join** it. Every rule was individually correct, which is why only a created-vs-delivered count caught it.
2. **R3's arrival counter never reset**, so it reported "5 arrivals" for the third arrival since a report. A lifetime counter sits permanently above the `≥ 3` threshold after the third arrival ever — every later recurrence would reopen escalated and the threshold would stop meaning anything. It now resets on delivery.
3. **A delivery timestamp in the future suppressed the alert**, with the reason "a report for this condition was delivered -180s ago". Both suppression rules now fail open on a negative elapsed time — the same direction as an unorderable severity.

A fourth was found while fixing the third: the fleet aggregate's per-condition notes repeated a whole paragraph each (5.9 KB over twenty) and **never named the affected condition** — the one thing a fleet-wide investigation exists to report. Now one line each, naming the key, bounded at 20 with the count continuing in the decision record.

**Two readings the rule table left implicit**, both taken from its own wording rather than invented:

- **R0's condition is "higher than what was already *delivered*"**, so an escalation arriving mid-investigation is R1's case: **join and raise that investigation's severity and budget in place**. Nothing is suppressed, so R0's guarantee holds, and one condition still produces one report instead of two budgets on one fault. `loop.run` re-reads `inv.budget` every iteration, so the bigger ceiling applies from the next turn — verified, not assumed.
- **`resolved` is a pre-rule, not a suppression rule.** In flight → join and hand the self-healing over as evidence; otherwise → drop, counted. It may never create an investigation, and it does not count as an arrival — otherwise a flapping condition would reach R3's threshold on its own recoveries.

**Deliberately not hidden:**

- **R4 (burst hold) has no alert source.** Every rule in `mock/prometheus/alerts.yml` sets `for: 1m`, so Prometheus has already done the aggregation; `config/alerting.yaml` opts in only `log_pattern`, which nothing emits yet. Implemented, unit-tested, mutation-tested, and **not exercised by anything the stack produces** — now a row in [DIAGNOSIS's coverage table](../DIAGNOSIS.md#rule-to-case-coverage) rather than an assumed capability. It stays in the middle of the ordered set because a hole in an ordered policy is worse than an unused branch.
- **No CLI surface.** L4b's artefact is consumed by tests and by the harness at L6; `srectl` gains nothing this lesson.
- **The dedup ledger is in memory**, so a restart forgets suppression state. That fails safe — an alert that would have been dropped creates an investigation instead — and L4a's JSONL log holds what a rebuild would need. Noted, not built: persistence with no consumer is the breadth this project avoids.
- **`pyyaml` arrived one lesson early** (scheduled L5). Thresholds that live in Python are thresholds nobody tunes. `agent/core/` still imports nothing from the loader — `DedupPolicy`'s defaults mirror the YAML, so the kernel runs with no config file at all, and `tests/triggers/test_policy.py` asserts that a missing, empty, or malformed file degrades to defaults rather than raising.

**Twelve guards mutation-tested**, all caught: R0 demoted below R2 → 4 failures; dedup keyed on `fingerprint` → 10; severity from `commonLabels` → 4; escalation ceiling removed → 2; hold flush silently dropped → 3; unknown severity ranked lowest → 2; storm cap aggregating instead of joining → 4; arrivals not reset on delivery → 4; `add_user_text` appending a second consecutive user message → 3; untimestamped cache entry served as fresh → 1; Go's zero `endsAt` accepted → 1; future delivery timestamp suppressing → 1.

One of those deserves its own line: **`add_user_text` now merges into a trailing user message.** Mid-investigation the last message carries that turn's `tool_result` blocks, and appending a second consecutive user message — which is exactly what absorbing a repeat alert or a resolution does — produces a `messages` array some providers reject outright. The L2 design named alert-storm absorption as the reason `messages` belongs to the `Investigation`; it would have failed on the first real absorption.

### Re-planned 2026-08-04 (same session, docs only)

An audit of the remaining W2 work, prompted by a fair complaint: each lesson had become one very long turn with no point at which a decision could be redirected, and nothing to look at afterwards.

**What the audit found**, counted rather than recalled: `harness.py` 7 lines, `prompts/assemble.py` 5, `core/report.py` 7, every sink a placeholder, `srectl trigger` a placeholder, tool returns canned. So the loop, the gateway, the trace and the trigger layer are each real and **nothing joins them** — there has never been a single command that takes an alert to a report.

Three changes:

1. **Steps replace lessons as the unit of work**, two exchanges each (design signed off, then one commit). Recorded in the handoff protocol above, because a rhythm that lives only in a chat log does not survive a session boundary.
2. **The remaining five lessons are split into 14 steps and reordered** — `L6 → L7a → L5 → L4c → L7b → L8`. Getting `srectl trigger` to run first buys a real system prompt, a validated report object and a destination for a report, all three of which L4c and L5 are better built against.
3. **Two W3 slices pulled forward and marked `[~]`** rather than silently double-counted: prompt assembly (W3 L1 → L6b) and the report schema (W3 L7 → L6c). In both cases the *shape* moves and the *content that is the real work* stays in W3.

### Operator console scheduled — 2026-08-04 (same session, docs only)

Decided after the re-plan above, and it changes L6a's design, so it is recorded before L6a starts rather than after.

**Why now**: an audit found the loop's **event stream has no consumer** — `TurnStarted` / `TextDelta` / `ToolCalled` / `ToolReturned` are generated on every run and `run_to_completion` collapses them into one terminal event. Same produced-then-discarded pattern [§42](../TRADEOFFS.md#42-traceability-one-id-four-sinks--and-an-honest-audit-of-what-is-currently-wired) documented for spans, one layer up, and the events were added in W2 L2 specifically so chat could stream and patrol could fan out. Separately, the cost/latency dashboard sat in W7 while the numbers it would show are produced from L3 onward — so for five weeks every cost and latency figure lives only in a commit message.

**What it is and is not** ([§43](../TRADEOFFS.md#43-operator-console-hand-write-the-driving-surface-never-the-trace-viewer)): we hand-write the surface that **drives** the system (trigger a case, chat, watch the span tree grow, watch which of R0-R5 fired, watch cost and budget move) and we do **not** hand-write a trace explorer — that is Langfuse's and Tempo's job, and a hand-rolled one would be a mock of a mature product. Scope fence: one HTML file, one JS file, `EventSource`, **no npm**.

**The condition that makes it a deliverable rather than decoration**: every tile shows a *measured* value from the ledger or the trace, or the tile does not ship. It is W7's dashboard cashed in early, so it is held to a deliverable's standard — tests, numbers, docs.

**Consequence for L6a, and the reason this is written first**: the harness must **expose the event and span streams to an outside subscriber**. If L6a swallows them the way `run_to_completion` does, L6b reopens the harness a step later.

### L6a designed, and split — 2026-08-08 (docs only)

The design was presented three times. The user's questions found **seven substantive omissions**, and roughly seven further items that turned out to belong to other steps — which is why every step row above now carries an explicit **`Not this step:`** clause. A row that lists deliverable files but not its boundary makes the boundary something both sides re-derive from scratch every time, and whoever derives it is the one who gets it wrong.

The seven that were genuinely missing from the design: `HarnessContext` named in a signature but never defined; `Trigger` missing the default sink binding that ARCHITECTURE §2 explicitly assigns it; the prompt fragment-stability table (deferring prompt *content* had quietly taken the *structure* with it); no named source for the cost figure ⑥ promises to print; a genuine bug's traceback going only to stderr rather than into the investigation's own JSONL; a raising sink being swallowed rather than becoming `Delivery(delivered=False)`; and an **abandoned async generator skipping the lifecycle close entirely** — a browser disconnect would have left R1 joining a dead investigation forever. The last was found by walking the design against "what if the consumer disappears", not by reading the code.

The cause was not step size — it was designing outward from *which files do I touch* rather than inward from *what data flows and what fails*. The transferable half of that is now an eight-item design checklist in the session skill, each item tied to the miss that produced it.

**L6a is split in two**, by *depth* rather than by layer: splitting by layer would leave one whole commit's worth of collaborators with no caller.

- **L6a-1, happy path** — three layers, six steps, `Route`/`Loadout`/`Result`/`HarnessContext`, sink registry + `stdout`, and the thin `bundle`/`assemble`/`LLMFactory` seams. Ships **a payload going ①→⑥ and printing a report** — the first end-to-end run of this project. Numbers: per-step timing, and `overhead 9.8 ms` split into named steps.
- **L6a-2, correctness** — the error invariant, the abandoned-generator `finally`, `Trigger.settle` and the dedup lifecycle, the `delivered=False` stubs, six mutations. Number: **a duplicate delivery through the full pipeline → 1 investigation**, which is the first time dedup is exercised by the real path rather than by a test calling `mark_delivered` itself.

Five new open gaps (#22-#26) came out of the same review: `tool_choice` unused, LLM-call failures never reaching the model, `PromptFragment` unversioned, a crash mid-turn leaving an unresumable log, and two writers on one investigation.

### Week 2 L6a-1 — the harness, happy path, closed 2026-08-08

**422 tests green** (363 → 422). Offline throughout. **An alert now becomes a printed report through one call path** — the first end-to-end run this project has had, replacing the by-hand wiring that produced L4b's span tree.

```
    2.09ms  harness.run
      0.70ms  harness.loadout
      0.98ms  investigation alert
        0.48ms  turn #0 tool_use
          0.04ms  tool.call query_metrics
        0.38ms  turn #1 tool_use
          0.02ms  tool.call submit_report
      0.01ms  harness.parse
      0.12ms  harness.fanout
```

**The numbers.** Per step, median of 400 runs, and the `overhead` lump [§42](../TRADEOFFS.md#42-traceability-one-id-four-sinks--and-an-honest-audit-of-what-is-currently-wired) promised to break down:

| | |
|---|---|
| ① route | 1.6 µs |
| ② preprocess (parse + dedup + tier + build) | 27.8 µs |
| ③ loadout (bundle + verify + prompt) | 28.7 µs |
| ⑤ parse | 3.9 µs |
| ⑥ fanout (context + one sink) | 19.0 µs |
| **harness overhead vs the hand-wired loop** | **78.6 µs** — 16.9% of a stub run, and **0.00009% of the 90 s p90 target** |
| the overhead lump, before → after | one number → `loadout 0.031` / `fanout 0.012` / `parse 0.002` / composition `0.216` ms |
| prompt | 3 fragments, 662 chars, **1 of 4** cache breakpoints used |
| 8 golden fixtures | 8 × `Done`, 3 turns, 3 tool calls each |

**Three bugs, all found by looking at the output rather than by a test:**

1. **The span tree was four roots, not one.** `loadout` / `parse` / `fanout` were *siblings* of the loop's `investigation` span, so `Trace.profile()` reported one step's duration as the whole run's `elapsed_ms` and `srectl replay` printed four trees. Fixed with a `harness.run` span wrapping ③-⑥. The test that was supposed to catch this **passed**, because it asserted the step names were *present* and not that they were *nested* — checklist item, earned.
2. **The prompt fragments were re-read from disk on every investigation** — 70 µs per loadout, most of the harness's whole overhead, for 285 bytes. Now cached per process, which makes the byte-identity guarantee *structural* rather than incidental: a file edited mid-run can no longer change the cache prefix between two investigations. Stated cost: editing a prompt needs a restart, which is correct — a prompt change is a deployment, and `prompt_version` (L6d) records which text served a run.
3. **`elapsed 0.0ms` on a real printed report.** ⑥ runs *inside* the root span by construction, so the root's duration is still unset when a sink asks how long the run took. The delivery context now reads elapsed from its own monotonic start, and recomputes the shares against it rather than leaving two numbers with different denominators in one dict.

**The seam test was sharpened, and re-proven.** `from ..triggers import registry` resolved to the bare package `agent.triggers`, so six legal registry imports failed while saying nothing about *what* was imported — the same imprecision L4a fixed for `from . import trace`, one level along. It now resolves submodules individually. Mutation-checked in both directions: `from ..triggers import alert` in `core/harness.py` → 1 failure; restored → green. Strictly more precise, never more permissive.

**Design decisions that survived contact**, all from the seven-exchange review: cost read off the trace rather than the `Ledger` (so `core/` never learns what money is, and sink / footer / console quote one set of numbers); the harness binding its own LLM through an injected `LLMFactory`, because `SystemPrompt` travels via `Gateway.bind` and flattening it would spend L3b's measured 94.9% prefix saving on a type annotation; ③ leaving `window` and the three tier ceilings untouched, asserted by a test; and untrusted alert text appearing only in `messages`, never in the system prompt — also asserted.

**Next up**: **L6a-2**, the correctness half — the error invariant (③/⑤ raising, and a real bug's traceback landing in the JSONL rather than only stderr), the abandoned-generator `finally`, `Trigger.settle` closing the dedup lifecycle, the `delivered=False` stubs, and six mutations. Number: a duplicate delivery through the full pipeline → **1** investigation, the first time dedup is exercised by the real path instead of by a test calling `mark_delivered` itself.
**Blockers**: none. ARCHITECTURE's two recorded deltas (two entry points; loadout's shrunk job) batch with L6a-2, per the rhythm's "docs may batch across a lesson's steps".

**Positioning reminder** (portfolio project for a mature-company SRE role, not a teaching project):
- Default reference frame: "how would Netflix / Airbnb / Coinbase SRE do this"; scope compromises are labelled as scope trade-offs, not teaching simplifications.
- Real off-the-shelf components always preferred over Python mocks; mocks reserved for cases where the real thing has irreducible ops burden (PagerDuty needs an account → `incident-tracker` mock stays).
- Three-layer architecture: agent code middleware-agnostic; cases pattern-based; middleware specifics in RAG. New middleware in prod = new runbook chunk + one YAML, 0 code changes.
- Alert design: only user-facing SLO violations page; infra signals stay in dashboards + query surface.
- **Every sophisticated component ships with a number or it doesn't ship.** Breadth of half-features is the failure mode this project is designed to avoid.

**Week 1 verified live** (unchanged): 13 real containers healthy; 8 golden cases pass via `python mock/scripts/case_runner.py <id>`; `GS-RES-001-redis-oom` drives a real Redis OOM → real alert → AlertManager webhook → tracked incident; `GS-P-DEPENDENCY-DOWN-001` produces a real 4-alert cascade; in-mock metric names match production verbatim.
