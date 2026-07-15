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

- [ ] System / phase / retrieved / scratchpad **prompt layering** documented — file: `prompts/README.md`
- [ ] **Prompt cache hit rate** measured and optimized — number: `_____ %`
- [ ] **Cache breakpoint ordering** decided (stable prefix first, dynamic content last) — evidence: `llm_gateway.py`
- [ ] **Context truncation policy** per phase (LRU / importance-scored / summary) — decision: `_____`
- [ ] **Tool result injection**: raw vs summary vs structured — decision: `_____`
- [ ] **Few-shot examples** dynamically selected per incident tag — evidence: `retrieval.py`
- [ ] Data point ready: "cache hit rate from X% → Y%, cost per incident from $Z → $W"

## 2. Tool schema design

- [ ] Every tool has: name, description, JSON Schema params, side_effect, cost_hint, timeout, idempotency_key (where WRITE)
- [ ] Tool descriptions start with verb, include one anti-example ("do NOT use for X")
- [ ] Enums used instead of free strings where possible
- [ ] **Tool selection accuracy** measured (which tool did the agent call for known-answer cases) — number: `_____ %`
- [ ] **Tool count vs accuracy** curve documented (add tool N+1, does accuracy go up or down?)
- [ ] **Parallel tool call groups** identified — evidence: `graph_definition.py`
- [ ] Tool error message design deliberate — errors are inputs to the next LLM decision

## 3. Agent loop control

- [ ] **Termination conditions** enumerated: task complete / max_iterations / cost budget / user cancel
- [ ] **Loop detection** implemented: 3× same tool + same args → forced reflection
- [ ] **Per-node tool call budget** enforced — see `ARCHITECTURE.md` §3
- [ ] **Per-node cost budget** enforced with degradation path
- [ ] **Reflection step** after each phase: "is the evidence sufficient?"
- [ ] Data point ready: "% of runs that hit reflection, % of those that recovered"

## 4. Memory / retrieval

- [ ] **Embedding model** chosen with justification — model: `_____`, alternatives compared: `_____`
- [ ] **Chunking strategy** decided — decision: `_____`
- [ ] **Hybrid retrieval**: dense + BM25 + metadata — evidence: `retrieval.py`
- [ ] **Reranker** decision (yes / no / deferred) — decision: `_____`
- [ ] **Memory update policy**: when does an incident enter memory, how deduplicated
- [ ] **Stale memory handling**: policy for entries >N months old
- [ ] Data point ready: "recall@5 with dense-only vs hybrid: X vs Y"

## 5. Evaluation

- [ ] **Golden set** ≥ 30 cases, difficulty distributed — see `eval/golden/`
- [ ] **LLM judge** designed with rubric — see `EVAL.md`
- [ ] **Judge validated** against manual scoring (kappa ≥ 0.7) — number: `_____`
- [ ] **Multi-dimensional metrics**: accuracy, tool efficiency, cost, latency, hallucination — all reported per run
- [ ] **Nightly regression** running via GitHub Actions
- [ ] **Smoke set on PR** running
- [ ] Data point ready: "accuracy on hard cases: X. Improved from Y when I made change Z."

## 6. Reliability

- [ ] **Model fallback chain** implemented — chain: `_____`
- [ ] **Tool retry with exponential backoff** — evidence: `mcp_client.py`
- [ ] **Idempotency keys** on all WRITE tools
- [ ] **Circuit breaker** per tool (N failures → open for M minutes)
- [ ] **Durable workflow via Temporal** — verified with kill-worker test
- [ ] **Kill test** performed: kill worker mid-verify, workflow resumes correctly
- [ ] Data point ready: "recovery time after worker crash: _____ seconds"

## 7. Cost engineering

- [ ] **Per-call cost logging** (input / output / cache tokens separated) — evidence: `llm_gateway.py`
- [ ] **Model routing** implemented and measured — table of `phase → model → avg_cost`
- [ ] **Prompt cache economics** understood (cache write 1.25× read; breakeven at 2 hits)
- [ ] **Batch API** used for eval runs on historical data (50% discount)
- [ ] **Tool result caching** within an incident (topology, deploy list)
- [ ] **Cost degradation path** when per-incident budget hit
- [ ] Data point ready: "median cost per incident: $X. Broken down: triage $A, collect $B, verify $C."

## 8. Latency

- [ ] **Streaming** used where user-facing (report to Slack streams as generated)
- [ ] **Parallel tool calls** in collect phase — evidence: `nodes/collect.py`
- [ ] **Speculative retrieval** during triage (prefetch similar_incidents async)
- [ ] **TTFT vs total latency** measured separately
- [ ] **Critical path profile** — where the 30 seconds go
- [ ] Data point ready: "p50 / p90 / p99 latency: _____ / _____ / _____ seconds"

## 9. Observability & debugging

- [ ] **Langfuse trace** on every LLM + tool call — verified
- [ ] **Decision-chain visualization** available (hypothesis → evidence → verdict)
- [ ] **Replay capability**: any past incident re-runnable from stored state (Temporal history)
- [ ] **Error taxonomy** defined: hallucination / tool_error / data_missing / prompt_bug
- [ ] **Error rate per class** tracked over time
- [ ] **Prompt version** stamped on every trace
- [ ] Data point ready: "top 3 error classes and their frequency"

## 10. Safety & permissions

- [ ] **Tool side-effect classification** — every tool tagged READ / WRITE / DESTRUCTIVE
- [ ] **Human-in-the-loop** for WRITE via Temporal signal
- [ ] **Dry-run mandatory** for DESTRUCTIVE — evidence: `_____`
- [ ] **Prompt injection defense** — structured parsing of log lines; second-model sanity check on suspicious tool inputs
- [ ] **Adversarial cases in golden set** — count: `_____`
- [ ] **Adversarial success rate** measured — target: 100%
- [ ] **Audit log** on every action: `(incident_id, tool, args, actor, timestamp)`

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

### N5. A safety story

- The most dangerous action the agent can take
- The gate design
- The failure mode you were most worried about
- How you verified it

### N6. An architecture decision you'd reverse

- One decision you're not sure about
- Under what evidence would you change it
- Signals genuine engineering humility

---

## Data-collection cadence

Every Friday, spend 30 minutes filling this file with the week's numbers. If a checkbox has been `[ ]` for 3 weeks, either scope it or drop it — an unfilled checklist is a lie.
