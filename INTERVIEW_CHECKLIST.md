# Interview Prep Checklist

Ten depth dimensions that big-tech AI/agent interviews consistently probe. For each dimension, this project must produce:

1. **A decision** — what I chose
2. **A tradeoff** — the alternative and why I rejected it
3. **A data point** — a number that makes the decision concrete
4. **A reflection** — what I'd do differently

Fill this file as the project evolves. Every checkbox is a piece of interview evidence.

---

## Legend

- [ ] = not started
- [~] = in progress
- [x] = complete, with note where the evidence lives

---

## 1. Context construction

- [ ] **Prompt layering** documented — `[A]` methodology → `[B]` output contract → `[C]` integration facet → `[D]` budget, ordered by cache prefix — file: `agent/prompts/assemble.py`
- [ ] **Prompt cache hit rate** measured and optimized — number: `_____ %`
- [ ] **Cache breakpoint ordering** decided (stable prefix first, dynamic last) — evidence: `agent/llm/gateway.py`
- [ ] **Context truncation policy** (per-result cap / oldest-result folding / compaction threshold) — evidence: `agent/core/context.py`
- [ ] **Tool result injection**: raw vs summary vs structured — decision: `_____`
- [ ] **Few-shot examples** dynamically selected per incident tag — evidence: `retrieval.py`
- [ ] Data point ready: "cache hit rate from X% → Y%, cost per incident from $Z → $W"

## 2. Tool schema design

- [ ] Every tool has: name, description, JSON Schema params, side_effect, cost_hint, timeout, idempotency_key (where WRITE)
- [ ] Tool descriptions start with verb, include one anti-example ("do NOT use for X")
- [ ] Enums used instead of free strings where possible
- [ ] **Tool selection accuracy** measured (which tool did the agent call for known-answer cases) — number: `_____ %`
- [ ] **Tool count vs accuracy** curve documented (add tool N+1, does accuracy go up or down?)
- [ ] **Parallel tool calls** — several `tool_use` blocks per response via `asyncio.gather` — evidence: `agent/core/loop.py`
- [ ] Tool error message design deliberate — errors are inputs to the next LLM decision

## 3. Agent loop control

- [ ] **Termination conditions** enumerated: task complete / max_iterations / cost budget / user cancel
- [ ] **Loop detection** implemented: 3× same tool + same args → forced reflection
- [ ] **Tool call budget** enforced — global `MAX_TURNS` backstop now; per-tool budgets when observable abuse appears. See `ARCHITECTURE.md` §3
- [ ] **Per-investigation cost budget** enforced at the gateway with a degradation path (refuse the call, emit an insufficient-evidence report)
- [ ] **Refute sub-loop** mandatory before delivery — `submit_report` returns `is_error` with no refutation on record
- [ ] Data point ready: "% of hypotheses killed by refute; `precompute_override_rate`"

## 4. Memory / retrieval

- [ ] **Embedding model** chosen with justification — model: `_____`, alternatives compared: `_____`
- [ ] **Chunking strategy** decided — decision: `_____`
- [ ] **Hybrid retrieval**: dense + BM25 + metadata — evidence: `retrieval.py`
- [ ] **Reranker** decision (yes / no / deferred) — decision: `_____`
- [ ] **Memory update policy**: when does an incident enter memory, how deduplicated
- [ ] **Stale memory handling**: policy for entries >N months old
- [ ] Data point ready: "recall@5 with dense-only vs hybrid: X vs Y"

## 5. Evaluation

- [ ] **Golden set** ~30 cases (20 ordinary + 10 adversarial), difficulty distributed — see `eval/golden/`
- [ ] **LLM judge** designed with rubric — see `EVAL.md`
- [ ] **Judge validated** against a hand-authored 24-example anchor set *before* it scores real runs (kappa ≥ 0.7 as a gate) — number: `_____`
- [ ] **Multi-dimensional metrics**: accuracy, tool efficiency, cost, latency, hallucination — all reported per run
- [ ] **Nightly regression** running via GitHub Actions
- [ ] **Smoke set on PR** running
- [ ] Data point ready: "accuracy on hard cases: X. Improved from Y when I made change Z."

## 6. Reliability

- [ ] **Model fallback chain** implemented — chain: `_____`
- [ ] **Tool retry with exponential backoff** — evidence: `mcp_client.py`
- [ ] **Idempotency keys** on all WRITE tools
- [ ] **Circuit breaker** per tool (N failures → open for M minutes)
- [ ] **Durability**: per-investigation JSONL append log; Temporal deliberately out of scope with a written Tier 2 migration ([§32](./TRADEOFFS.md#32-temporal-is-out-of-scope-not-deferred))
- [ ] **Replay test** performed: `srectl replay <investigation-id>` reconstructs `messages` from the log
- [ ] Data point ready: "investigations reconstructable from the JSONL log: _____ %"

## 7. Cost engineering

- [ ] **Per-call cost logging** (input / output / cache tokens separated) — evidence: `agent/llm/cost.py`
- [ ] **Model routing by task nature** (main loop / refute / judge) measured — table of `call kind → model → avg_cost`
- [ ] **Prompt cache economics** understood (cache write 1.25× read; breakeven at 2 hits)
- [ ] **Batch API** used for eval runs on historical data (50% discount)
- [ ] **Tool result caching** within an incident (topology, deploy list)
- [ ] **Cost degradation path** when per-investigation budget hit — gateway refuses the call, harness emits an "insufficient evidence" report
- [ ] Data point ready: "median cost per investigation: $X. Broken down: main loop $A, refute sub-loop $B, judge $C."
- [ ] Data point ready: "gateway cache hit rate $X% — nightly eval over ~30 cases costs $Y instead of $Z"

## 8. Latency

- [ ] **Streaming** used where user-facing (report to Slack streams as generated)
- [ ] **Parallel tool calls** — several `tool_use` blocks in one response dispatched with `asyncio.gather`; evidence: wall-clock delta on the 4-alert cascade case
- [ ] **Two-stage output** — preliminary verdict streamed within seconds, full report after
- [ ] **Speculative retrieval** in harness ② (prefetch similar investigations async, off the critical path)
- [ ] **TTFT vs total latency** measured separately
- [~] **Critical path profile** — where the 30 seconds go. Span durations and `Trace.profile()` land in W2 L4a; the offline split is 0.017 ms instrumentation / 1.147 ms durable log per investigation. A real provider-vs-tool split needs live tools (W3 L2)
- [ ] Data point ready: "p50 / p90 / p99 latency: _____ / _____ / _____ seconds"

## 9. Observability & debugging

- [ ] **Langfuse trace** on every LLM + tool call — verified
- [ ] **Decision-chain visualization** available (hypothesis → evidence → verdict)
- [x] **Replay capability**: `srectl replay <id>` — span tree with durations, timing profile, and `messages` rebuilt from the event stream (reconstruction asserted exact against a live run)
- [ ] **Error taxonomy** defined: hallucination / tool_error / data_missing / prompt_bug
- [ ] **Error rate per class** tracked over time
- [ ] **Prompt version** stamped on every trace
- [ ] Data point ready: "top 3 error classes and their frequency"

## 10. Safety & prompt injection defense

See [SECURITY.md](./SECURITY.md) for the full threat model and five-layer defense stack. This checklist tracks implementation.

### 10.1 Layer 1 — Message construction (data isolation)

- [ ] **Untrusted-content XML wrapping** — every log/metric/label/memory item wrapped in `<untrusted_data source="...">` — evidence: `agent/security/sanitize.py`
- [ ] **System prompt anti-injection clause** — explicit instruction to never follow content inside untrusted tags
- [ ] **Sanitization**: strip C0/C1 control chars, zero-width, homoglyphs; escape fake closing tags; normalize NFC
- [ ] **Per-item length cap** (4KB); truncation marker on overflow
- [ ] Data point: cases where fake `</untrusted_data>` closing was attempted → 100% escaped

### 10.2 Layer 2 — Structured output constraints

- [ ] **The report is a forced tool call with a validated schema** — it is the only exit from the loop, so structured output is not skippable
- [ ] **Enum constraints** on high-risk fields (`target_service` ∈ known_services)
- [ ] **Bounded list sizes** (max 4 hypotheses, max 3 proposed actions)
- [ ] **Retry-on-schema-fail** with error fed back to LLM
- [ ] Data point: schema-fail rate over 100 runs — number: `_____ %`

### 10.3 Layer 3 — Second-model review

- [ ] **Reviewer LLM** implemented, different family from primary — pair: `_____ / _____`
- [ ] **Reviewer prompt** with 5-point rubric — file: `agent/prompts/reviewer.md`
- [ ] **PROCEED / FLAG / BLOCK verdict** enforced in workflow
- [ ] **BLOCK escalates to human**, does not proceed silently
- [ ] Data point: FLAG rate; BLOCK rate; false-block rate — numbers: `_____`

### 10.4 Layer 4 — Gate (approval workflow)

- [ ] **Tool side-effect classification** — every tool tagged READ / WRITE / DESTRUCTIVE
- [ ] **`preview_supported` flag** per WRITE tool — every WRITE either has native dry-run or requires two-person approval
- [ ] **Native dry-run integration**: `kubectl --dry-run=server`, `terraform plan`, etc. — dry-run output attached to approval request
- [ ] **Human-in-the-loop via Temporal signal** — kill test: workflow resumes cleanly after signal-wait
- [ ] **Blast radius tier** computed per action (single-service / multi-service / cross-cluster)
- [ ] **Audit chain** on every action: `(incident_id, tool, args, actor, timestamp, dry_run_hash, verdict)`

### 10.5 Layer 5 — Egress filter

- [ ] **Report is serialized from Pydantic**, not LLM-generated Markdown
- [ ] **URL whitelist** enforced (runbook host, Grafana host, Slack) — regex evidence: `egress.py`
- [ ] **Image tag stripping**
- [ ] **Secret regex sanitize** (AWS keys, JWT, PEM blocks)
- [ ] **Length cap** on report content

### 10.6 Cross-cutting caps

- [ ] **Per-incident cost cap** enforced with degradation path
- [ ] **Max tool calls** enforced (20 across phases)
- [ ] **Max iterations per node** enforced (5)
- [x] **Same-tool-same-args circuit breaker** implemented — W2 L4a, keyed on `(tool_name, args_hash)`; the 3rd identical call returns the previous result plus a nudge rather than aborting
- [ ] Data point: cap-trip rate; false-trip rate on normal cases

### 10.7 Adversarial eval coverage

- [ ] **10 adversarial cases** in golden set (see [EVAL.md](./EVAL.md) adversarial matrix)
- [ ] **Per-layer bypass rate** reported — target: 0% at L1-L5
- [ ] **New injection payload class → new adversarial case** (workflow enforced)

### 10.8 Documented gaps

- [ ] **Known unguarded surfaces** listed in SECURITY.md and understood — able to speak to each in interview:
  - Memory poisoning
  - Chained multi-step attacks
  - Prompt bloat DoS
  - Reviewer-model bypass (shared training data)
  - Supply chain

### 10.9 Why NOT sandbox — able to articulate

- [ ] Able to explain: tools have typed params; no LLM-generated string is executed by our system; sandbox is for code-execution scenarios; the five-layer stack targets *content interpretation* which is our actual attack surface
- [ ] Able to state the trigger condition: adding a tool that executes an LLM-generated string (e.g., `run_promql(query: str)`) is when sandbox becomes necessary

---

## Cross-cutting narratives to prepare

These are the multi-dimensional stories that show up in senior interviews. Prepare one 10-minute story for each.

### N1. Scaling from Tier 1.5 to Tier 2

- What breaks first?
- What changes: queue, worker pool, vector DB, LLM gateway service, multi-tenant enforcement
- What order do you migrate?

### N2. A regression story

- Prompt / model change → eval showed a drop
- How did you localize it? (phase-level metrics)
- How did you fix it? What did you learn?

### N3. A cost-optimization story

- Baseline cost per incident
- Two or three optimizations applied (caching, routing, tool result cache)
- Delta with numbers

### N4. A hallucination story

- A specific hallucination the agent produced
- Why it happened
- What defense you added
- How the eval verifies the defense

### N5. A safety / prompt-injection story

- The realization that gate ≠ full defense
- The five-layer stack and why each layer alone is insufficient
- Concrete adversarial case that made you add a layer
- The gap you *haven't* closed and why that's an honest choice, not a blind spot

### N6. An architecture decision you'd reverse

- One decision you're not sure about
- Under what evidence would you change it
- Signals genuine engineering humility

---

## Data-collection cadence

Every Friday, spend 30 minutes filling this file with the week's numbers. If a checkbox has been `[ ]` for 3 weeks, either scope it or drop it — an unfilled checklist is a lie.
