# Diagnosis Methodology

The reasoning procedure the agent follows, written down and numbered so that each rule can be pointed at a golden case or a deterministic checker. Before this document existed, [ROADMAP](./docs/ROADMAP.md) W3 L1 described the methodology as "an elimination tree" — one line, which is another way of saying the reasoning was outsourced to whatever the model happened to infer from context. That approach does not survive contact with cascades, and it cannot be regression-tested.

---

## The governing constraint

> **This document holds middleware-*agnostic* procedure. It never holds middleware knowledge.**

"Compare a change's timestamp against the symptom's onset and require the change to precede it" is procedure — true of Kubernetes, Kafka, Redis, and a mainframe alike. "Redis `maxmemory-policy noeviction` makes writes fail rather than evicting" is knowledge, and it belongs in a runbook chunk retrievable by `search_runbook`, per [TRADEOFFS §20](./TRADEOFFS.md#20-middleware-specific-knowledge-lives-in-rag-not-in-agent-code-or-cases).

The test for a proposed addition: *would this rule still be true if we swapped every piece of middleware in the stack?* If no, it is a runbook chunk. Without this constraint the document becomes a knowledge dump, the system prompt grows without bound, and adding a middleware stops being free.

## Three layers

| Layer | Who executes it | Where it lives |
|---|---|---|
| **1. Precompute** — deterministic joins over data we already have | code, before the model is invoked | harness ②/③ (`agent/core/harness.py`) |
| **2. Elimination rules** — which hypothesis classes are live, and when to prune one | the model, following numbered rules | `agent/prompts/methodology.md` |
| **3. In-model reasoning** — everything deliberately *not* proceduralised | the model, unconstrained | the loop |

The split follows [TRADEOFFS §23](./TRADEOFFS.md#23-harness-deterministic-pipeline-around-the-agent-loop-refines-22): enumerable work belongs in the shell. A topology join is enumerable. "Which of these three candidates actually explains these log lines" is not.

---

## Layer 1 — Precompute (P-rules)

Computed by the harness before the first LLM call. Each output goes into the prompt as a compact structured block, never as raw data.

### P1 — Merged timeline

Put four event streams on one axis, all inside `Investigation.window`:

| Stream | Source |
|---|---|
| alert firing time (and the rule's `for:` duration, so *breach* time is recoverable) | the alert payload |
| metric onset — first threshold breach per anomalous service | Prometheus range query |
| log-error onset — first error-level burst per anomalous service | ClickHouse |
| deploy / config events | deploy-history |

Output: an ordered list of `(t, stream, service, what)` plus pairwise lead-lag in seconds.

**Why this is not the model's job**: onset ordering is arithmetic. A model asked to eyeball timestamps across three tool results will sometimes get the order wrong, and the entire elimination tree below is built on ordering. Making it deterministic removes a whole class of silent reasoning error.

### P2 — Topology graph

Derived from the client-side downstream metrics Week 1 L5 already emits — `downstream_requests_total{service, downstream, outcome}`. Edges, per-edge RED, and each service's depth from the user-facing entry point.

**No tracing backend required for this.** See [TRADEOFFS §29](./TRADEOFFS.md#29-trace-scope-per-request-causal-chains-not-a-third-pillar) — traces add per-request causal chains, not the graph.

### P3 — Blast radius

Every service anomalous in the window (error rate or latency beyond its baseline), not just the ones that alerted. A service can be sick without breaching a paging threshold, and that service is frequently the cause of the one that did.

### P4 — Candidate ranking

A ranked shortlist of candidate root-cause services:

```
score(s) = w_change · change_freshness(s)
         + w_severity · anomaly_severity(s)
         + w_position · topology_position(s)
         + w_onset   · onset_earliness(s)
```

- `change_freshness` — decays over the window; a deploy 4 minutes before onset scores far above one 90 minutes before.
- `topology_position` — deeper (further from the user) scores higher, because a leaf failure explains its ancestors while the reverse requires an extra mechanism.
- `onset_earliness` — earlier onset scores higher within a correlated set.

**Weights are configuration, and their values are an eval question, not a taste question.** Initial values are uniform; W3 L9 reports accuracy against a weight sweep.

**The hard constraint**: this is a **shortlist, never a conclusion**. The prompt presents it as "candidates worth checking first, in this order" and the elimination rules below can discard all of them. `precompute_override_rate` ([EVAL.md](./EVAL.md)) measures whether the model actually does — if the final root cause is always the top candidate, the LLM is a narrator for a heuristic and we say so. See [TRADEOFFS §28](./TRADEOFFS.md#28-precompute-produces-a-shortlist-never-a-conclusion).

### P5 — Cascade root vs symptom

Within a correlated alert set: the service that is **downstream of the others** and has the **earliest onset** is the candidate root; upstream services with later onset are candidate symptoms.

Both conditions are required. Downstream-with-later-onset is not a root — that is a service being poisoned by its own caller's retry behaviour, which is a different mechanism (see D5).

- **Maps to**: `GS-P-DEPENDENCY-DOWN-001` — a 4-alert cascade whose correct output is one investigation naming `payment` as root and `checkout` / `gateway` as symptoms.
- **Checker**: deterministic. The expected root is in `expected.yaml`; scoring does not need a judge.

### P6 — Correlated-alert grouping

Alerts join an in-flight investigation instead of forking a new one when they fall inside a correlation window and their services are topologically adjacent. Rule-based, no model involved.

- **Maps to**: `GS-P-DEPENDENCY-DOWN-001`, via `expected_investigation_count: 1`.
- **Lands**: W2 L4 (rule version), W4 (similarity-based deduplication of *repeat* incidents, once episodic memory exists).

---

## Layer 2 — Elimination rules (D-rules)

The four hypothesis classes are Week 1's own golden-case taxonomy, which means every rule below already has cases behind it: **CHANGE / RES / DEP / DATA**.

Each rule states an **activation** condition (this class is live and worth spending tool calls on) and a **pruning** condition (this class is excluded, and the report must say so explicitly rather than silently dropping it).

### D1 — CHANGE

- **Activate** if any service in the blast radius has a deploy or config event in the window, preceding its own onset.
- **Prune** if there are no such events — **and state that explicitly in the report.**
- **Why the explicit statement**: absence of a deploy is the single most reliably hallucinated fact in incident diagnosis. A model pattern-matching "5xx spike" to "recent deploy" will invent one. `GS-LOAD-001` is built around this trap: there are no deploys in the last 24 hours, and the correct behaviour is to say so.
- **Maps to**: `GS-CHANGE-001-token-upgrade` (activation), `GS-LOAD-001` (pruning + hallucination guard).
- **Checker**: deterministic — any deploy identifier in the report that is absent from every tool result counts as a hallucination.

### D2 — RES

- **Activate** if a saturation signal on a blast-radius service breached in the window: memory, connection pool, thread pool, queue depth, disk, file descriptors.
- **Prune** if all saturation signals stayed within baseline.
- **Distinguish saturation from consequence**: a full thread pool downstream of an exhausted connection pool is a consequence. Order by onset (P1) and attribute to the earliest.
- **Maps to**: `GS-RES-001-redis-oom`.

### D3 — DEP

- **Activate** if a downstream's error or latency onset **precedes** the local onset.
- **Prune** if local onset precedes every downstream's — you are the origin, not a victim, and continuing down DEP wastes the budget.
- **Why direction is the whole rule**: co-occurrence is symmetric and therefore useless. Onset ordering is what separates "my dependency broke me" from "I broke my dependency by hammering it".
- **Maps to**: `GS-DEP-001-card-provider-block` (activation), `GS-P-DEPENDENCY-DOWN-001` (activation plus P5).

### D4 — DATA

- **Activate** if request rate, payload-size distribution, or key distribution shifted **before** onset.
- **Prune** if traffic shape is unchanged.
- **Maps to**: `GS-LOAD-001`, partially — the traffic shift is real there, but it is the *proximate* cause, not the root. Which is D5's job.

### D5 — Amplification check (runs whenever D4 activates)

- **Trigger**: internal request rate rises materially faster than ingress request rate.
- **Conclusion**: load is being amplified inside the system — retries without backoff, a fan-out loop, a thundering-herd cache miss. **The root cause is the amplification configuration, not the traffic.**
- **Why this is a separate rule**: without it, every traffic-driven incident is diagnosed as "traffic spike, scale up", which postpones the outage rather than fixing it. The distinction between mitigation and root-cause fix (see the report contract below) exists mostly because of this rule.
- **Maps to**: `GS-LOAD-001` — the case in the backlog specifically because it is the only one testing this, alongside memory anchoring and the no-deploy trap.

### D6 — Evidence grading

Metrics outrank logs; logs outrank runbook inference. A hypothesis is **correlational** until onset ordering supports it; the report must use the word that applies. Runbook text is never evidence *that* something happened — only a hypothesis about what to check.

- **Checker**: deterministic — a report claiming causation with no onset-ordering evidence in `report.evidence` is flagged.

### D7 — Stop rule

Submit when **one hypothesis explains every symptom in the blast radius** and **has survived one refutation attempt**.

If neither holds when the budget runs out, submit with `confidence: "unknown"`, the ruled-out list, and the open questions. A report that narrows 40 services to 2 is a useful shift handover; a report that guesses confidently is worse than nothing.

- **Enforced structurally**, not by instruction: `submit_report`'s handler returns `is_error` when no refutation is on record (W3 L7), so the model cannot skip verification by feeling confident.

---

## Layer 3 — Left to the model, deliberately

Named explicitly, because a methodology document's failure mode is creeping until the model is a template renderer:

- **Which query to run next**, and how to phrase it. Enumerating this would be rebuilding the phase graph the project already rejected.
- **Reading log semantics** — an unfamiliar error string, a stack trace, a vendor's status message.
- **Weighing conflicting evidence** when two hypothesis classes are both live and the signals disagree.
- **Deciding when the evidence is *enough*** — D7 gives the condition, not the threshold.
- **Writing the report**, including how to describe uncertainty honestly.

If a rule is ever proposed for Layer 2 that removes one of these, it belongs in Layer 3 and the proposal is wrong.

---

## Report contract

The methodology's output has a shape, because a correct diagnosis nobody acts on is worth nothing — the most common way an incident copilot dies is on-call ignoring it.

**The first three lines must answer two questions**: *is this mine?* and *what do I do right now?*

```
VERDICT      <service> is the origin | <service> is a symptom of <other> | undetermined
FIRST ACTION <exactly one action, or "none — investigate further">
CONFIDENCE   high | medium | low | unknown
```

Then the body: root cause, evidence, ruled-out classes (D1-D5 with the reason each was pruned), open questions, assumptions, undo path.

Two rules on `FIRST ACTION`:

1. **Exactly one.** A list of five is a way of not deciding, and it pushes the decision back onto the person who is already overloaded.
2. **Mitigation and root-cause fix are labelled separately** where they differ. "Scale up" as a mitigation with "fix the retry config" as the actual fix is a correct answer; presenting scale-up as *the* fix is D5 failing.

Measured by `human_first_action` alignment — `expected.yaml` records the action a competent on-call would take first, authored by hand, and the metric is whether the agent's single recommendation matches it. This is author ground truth rather than an LLM's opinion, which matters given that everything else in the eval is model-judged ([EVAL.md](./EVAL.md) self-certification risk).

---

## Rule-to-case coverage

Any rule without a case behind it is an assertion, not a method.

| Rule | Golden case | Grading |
|---|---|---|
| P1 timeline | all | deterministic (ordering present in evidence) |
| P2 topology | `GS-P-DEPENDENCY-DOWN-001` | deterministic |
| P5 cascade root vs symptom | `GS-P-DEPENDENCY-DOWN-001` | deterministic (expected root) |
| P6 alert grouping | `GS-P-DEPENDENCY-DOWN-001` | deterministic (`expected_investigation_count`) |
| D1 CHANGE activate | `GS-CHANGE-001-token-upgrade` | judge + deterministic |
| D1 CHANGE prune / no-deploy | `GS-LOAD-001` *(backlog)* | deterministic (hallucination checker) |
| D2 RES | `GS-RES-001-redis-oom` | judge |
| D3 DEP activate | `GS-DEP-001-card-provider-block` | judge |
| D3 DEP prune (local first) | **no case — gap** | — |
| D4 DATA | `GS-LOAD-001` *(backlog)* | judge |
| D5 amplification | `GS-LOAD-001` *(backlog)* | judge + deterministic |
| D6 evidence grading | all | deterministic |
| D7 stop rule | needs a genuinely-underdetermined case — **gap** | judge |
| Report contract | all | `human_first_action` alignment |

**Two known gaps**, recorded rather than glossed:

1. **D3 pruning** has no case — nothing in the set has a local onset preceding its downstream's. `GS-P-IO-LATENCY-001` is the closest and could be extended.
2. **D7** needs a case that is genuinely undetermined, where `confidence: "unknown"` is the *correct* answer. Every current case has a knowable answer, so the set cannot currently detect an agent that never admits uncertainty.

Also note that **three rules (D1-prune, D4, D5) all depend on `GS-LOAD-001`, which is in `eval/backlog/`** and not yet runnable. That is now the strongest argument for realising it earlier than Week 6.

---

## Status

Authored 2026-08-01 alongside the W3 re-plan. Layer 1 lands in W3 L3, Layer 2 in W3 L1, the report contract in W3 L6, and the coverage table becomes measurable in W3 L9. Until then this document is a design, and every number in it is a hypothesis — see [TRADEOFFS §30](./TRADEOFFS.md#30-unmeasured-targets-are-labelled-hypotheses).
