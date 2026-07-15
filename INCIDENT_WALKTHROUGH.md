# Incident Walkthrough

A single fictional incident traced end-to-end through every subsystem — the concrete narrative that makes "how does your diagnostic flow actually work" answerable in one story.

Scenario is loosely inspired by public postmortems (Cloudflare 2022 workers-KV, Datadog 2023 multi-day outage): a config change silently reduces DB connection pool size, causing gradual saturation until 5xx spikes trip an alert. Numbers below are for illustration; real fixture data lives in `eval/golden/GS-014-*/`.

---

## t=0.0s — Alert fires

A Prometheus alertmanager rule matches:

```yaml
alert: HighErrorRate
service: payment-svc
severity: P1
trigger: 5xx_rate > 5% for 3m
labels:
  region: us-east-1
  cluster: prod-1
annotations:
  runbook_url: https://wiki/runbooks/payment-svc-5xx
```

Alertmanager POSTs to the agent's FastAPI webhook.

**Ingress**:
- API key check → pass
- Dedupe check: no prior `alert_id=alt_29f...` in last 5min → new
- Enqueue Temporal workflow `IncidentWorkflow(incident_id="inc_2026_07_15_001")` → return 202 in 12ms

---

## t=0.4s — `triage` node

**Model**: Haiku 4.5 (fast classification).
**Budget**: ≤3 tool calls.

### LLM call 1 — entity extraction (schema-constrained)

Prompt (trimmed):
```
System: Extract entities from this alert. Return HypothesisSeed JSON.
User:
<alert>
  service: payment-svc
  severity: P1
  region: us-east-1
  trigger: 5xx_rate > 5% for 3m
</alert>
```

Output (Pydantic-validated):
```json
{
  "service": "payment-svc",
  "severity": "P1",
  "region": "us-east-1",
  "time_window": "2026-07-15T14:19-14:22Z",
  "signal_type": "error_rate_spike",
  "candidate_causes": ["recent_deploy", "downstream_degradation", "traffic_spike"]
}
```

### Tool call — `get_similar_incidents` (memory retrieval)

Runs against pgvector `past_incidents`:
```sql
SELECT incident_id, root_cause, resolution
FROM past_incidents
WHERE tenant_id = 'default'
  AND service = 'payment-svc'
ORDER BY embedding <=> $query_embedding
LIMIT 3;
```

Returns 3 hits; top-1 (cosine=0.87):
> `inc_2025_11_03_047`: "payment-svc 5xx after v1.9 deploy; root cause = SLOW_QUERY on orders table due to missing index; resolution = index added + forward-fix deploy"

### `triage` state emitted

```json
{
  "phase": "collect",
  "entities": {...as above},
  "similar_incidents": [
    {"id": "inc_2025_11_03_047", "similarity": 0.87, "cause_tag": "slow_query"},
    {"id": "inc_2025_09_22_012", "similarity": 0.71, "cause_tag": "downstream_timeout"},
    {"id": "inc_2025_08_14_005", "similarity": 0.63, "cause_tag": "config_change"}
  ]
}
```

**Elapsed**: 1.8s (Haiku is fast; 1 LLM call + 1 vector query).
**Cost so far**: $0.003.

---

## t=2.2s — `collect` node

**Model**: Sonnet 5.
**Budget**: ≤8 tool calls, ≤5 iterations.

### LLM call 2 — plan signals to pull

Prompt structure (with cached stable prefix):
```
System (cached, 4kb): ...you are the collect node...tool list...
User:
  Entities: {...}
  Similar incidents suggest: slow_query, downstream_timeout, config_change
  Alert time window: 14:19-14:22Z

  Plan the 3-5 highest-value tool calls to distinguish these causes.
```

Output:
```json
{
  "plan": [
    {"tool": "query_metrics", "args": {"queries": ["rate(http_requests_5xx{service='payment-svc'}[1m])", "db_pool_wait_time_p99{service='payment-svc'}", "downstream_rpc_latency_p99"], "window": "14:10-14:25"}},
    {"tool": "query_logs", "args": {"service": "payment-svc", "level": "error", "window": "14:15-14:25", "limit": 100}},
    {"tool": "list_recent_deploys", "args": {"service": "payment-svc", "window": "12:00-14:22"}},
    {"tool": "retrieve_runbook", "args": {"query": "payment-svc 5xx spike diagnostic checklist"}}
  ]
}
```

### Parallel tool execution

All 4 tools fire concurrently (Temporal activity fan-out):

**`query_metrics`** returns:
- 5xx rate: baseline 0.3% → 12% between 14:19-14:22 (climbing).
- `db_pool_wait_time_p99`: baseline 8ms → 4200ms between 14:18-14:22 ⚠️
- Downstream RPC latency: unchanged at 45ms.

**`query_logs`** returns (top patterns):
- `ERROR: acquiring connection from pool timed out after 30s` — 847 occurrences
- `WARN: pool utilization > 90%` — 1240 occurrences

**`list_recent_deploys`** returns:
- `payment-svc v2.3.0` deployed at 13:45Z (34 minutes before alert). Diff includes `config/db.yaml`.

**`retrieve_runbook`** (see [RAG.md](./RAG.md) retrieval) returns 3 chunks:
- Top chunk: "payment-svc DB pool sizing — default 50, must not drop below 30 under normal load..."

### Circuit breaker firing check

`same_tool_same_args` counter: no duplicates. Pass.

### `collect` state emitted

```json
{
  "phase": "hypothesize",
  "collected_signals": {
    "metrics": {"db_pool_wait_p99": [{"t": "14:19", "v": 4200}, ...], "5xx_rate": [...]},
    "logs": {"top_patterns": ["pool timeout", "pool util > 90%"], "counts": {...}},
    "deploys": [{"version": "v2.3.0", "time": "13:45Z", "config_files_changed": ["config/db.yaml"]}],
    "runbook_hits": ["runbook_payment_svc_5xx.md#db-pool"]
  }
}
```

**Elapsed cumulative**: 5.7s (parallel tool calls dominated by log query at 3.1s).
**Cost cumulative**: $0.021.

---

## t=5.7s — `hypothesize` node

**Model**: Sonnet 5.
**Budget**: 0 tool calls (reasoning only).

### LLM call 3 — generate hypotheses (schema-constrained)

Prompt (untrusted data wrapped in `<untrusted_data>` tags per Layer 1 security — see [SECURITY.md](./SECURITY.md)):

```
System: You are the hypothesize node. Generate 2-4 hypotheses per the schema.
User:
  Signals collected:
  <untrusted_data source="prometheus">
    metric: db_pool_wait_time_p99 climbed 8ms → 4200ms at 14:18
    metric: 5xx_rate climbed 0.3% → 12% at 14:19 (1min lag after pool wait)
  </untrusted_data>
  <untrusted_data source="loki">
    pattern: "pool timeout" 847× in 3min
    pattern: "pool util > 90%" 1240× in 3min
  </untrusted_data>
  <untrusted_data source="deploy_history">
    v2.3.0 at 13:45Z, changed config/db.yaml
  </untrusted_data>
  ...
```

Output:
```json
{
  "hypotheses": [
    {
      "id": "H1",
      "claim": "v2.3.0 config change reduced db_pool_size, causing exhaustion",
      "evidence_refs": ["deploy.v2.3.0.config/db.yaml", "metric.db_pool_wait_time_p99", "log.pool_timeout"],
      "confidence": 0.75,
      "prior_from_similar": 0.30
    },
    {
      "id": "H2",
      "claim": "Slow query saturating existing pool (like inc_2025_11_03_047)",
      "evidence_refs": ["metric.db_pool_wait_time_p99"],
      "confidence": 0.25,
      "prior_from_similar": 0.87
    },
    {
      "id": "H3",
      "claim": "Traffic spike beyond pool capacity",
      "evidence_refs": [],
      "confidence": 0.05,
      "prior_from_similar": 0.10
    }
  ],
  "proposed_actions": [
    {"action_type": "rollback", "target_service": "payment-svc", "target_version": "v2.2.9", "justification": "Config change in v2.3.0 is prime suspect; forward-fix requires config diff review."}
  ]
}
```

Note: `target_service` is enum-constrained to `KNOWN_SERVICES` (Layer 2). Malicious injection like `"target_service": "*"` would fail schema and force retry.

**Elapsed cumulative**: 9.3s.
**Cost cumulative**: $0.052.

---

## t=9.3s — `verify` node

**Model**: Opus 4.8 (adversarial reasoning).
**Budget**: ≤4 tool calls per hypothesis. Runs H1/H2/H3 in parallel.

### Sub-agent per hypothesis, prompted to REFUTE

**H1 verify sub-agent**:
- Fetches `get_diff("payment-svc", "v2.2.9", "v2.3.0")` → sees `db_pool_size: 50 → 10` in `config/db.yaml`. **Confirmed**.
- Verdict: `confirmed` with refutation log ("attempted to refute by checking if pool_size change was pre-existing; git blame shows it's new in v2.3.0").

**H2 verify sub-agent**:
- Fetches `query_metrics(db_query_p99)` → unchanged from baseline. **Refuted** (no slow queries).
- Verdict: `refuted`.

**H3 verify sub-agent**:
- Fetches `query_metrics(request_rate)` → baseline 800/s, current 810/s. **Refuted** (no traffic spike).
- Verdict: `refuted`.

### `verify` state emitted

```json
{
  "phase": "report",
  "hypotheses": [
    {"id": "H1", "verdict": "confirmed", "refutation_attempted": "checked pre-existing config", "final_confidence": 0.95},
    {"id": "H2", "verdict": "refuted"},
    {"id": "H3", "verdict": "refuted"}
  ]
}
```

**Elapsed cumulative**: 24.1s (Opus is slower; 3 parallel sub-agents each doing 1-2 tool calls).
**Cost cumulative**: $0.14 (Opus dominates).

---

## t=24.1s — `report` node

**Model**: Sonnet 5.

### Layer 3 second-model review (optional in Tier 1.5)

Before generating the report, the proposed rollback action is sent to a review LLM (different family; in POC could be Sonnet-thinking mode; Tier 2 would be a non-Anthropic model):

```
Reviewer: PROCEED
Reasons: [
  "Rollback target v2.2.9 is a real prior version",
  "Justification cites confirmed evidence H1",
  "No scope creep — action limited to payment-svc"
]
```

Verdict `PROCEED` → continue. If `BLOCK`, workflow would halt with `escalate_to_human` signal.

### Report assembly (Pydantic → Markdown by our code, not by LLM)

```markdown
# Incident inc_2026_07_15_001 — payment-svc 5xx spike

**Severity**: P1 | **Duration**: 3m | **Status**: root cause identified, action pending approval

## Root cause (confidence 0.95)
Config change in v2.3.0 reduced `db_pool_size` from 50 to 10.
Pool exhaustion under normal load caused connection acquisition timeouts,
which surfaced as 5xx responses.

## Evidence
- Deploy `payment-svc v2.3.0` at 13:45Z changed `config/db.yaml`.
  Diff: `db_pool_size: 50 → 10`.
- `db_pool_wait_time_p99` climbed 8ms → 4200ms at 14:18Z (1min lead of 5xx).
- 847× "pool timeout" errors in 3min.
- Refuted alternatives: slow-query (query_p99 unchanged), traffic spike (rate unchanged).

## Proposed action
**Rollback** `payment-svc` to `v2.2.9` (pending approval).

Dry-run preview:
```
kubectl rollout undo deployment/payment-svc --to-revision=<rev-of-v2.2.9>
Would revert: 1 replica set, 4 pods
```

## Undo path
If rollback causes new issues, the `forward_fix` path is: deploy a v2.3.1
that restores `db_pool_size: 50` in `config/db.yaml`.

**Awaiting human approval via #oncall-payments Slack.**
```

Egress filter (Layer 5) checks: no non-whitelisted URLs, no image tags, no secret patterns. Pass.

### Human approval gate

Report + rollback preview posted to Slack. Human clicks Approve within 90s. Temporal signal `approve_action(action_id="act_...")` fires. `propose_rollback` tool executes with idempotency_key.

Rollback succeeds. 5xx rate drops to baseline within 2 minutes.

### Post-incident write to episodic memory

```sql
INSERT INTO past_incidents (
  incident_id, service, root_cause_summary, resolution_summary, embedding,
  tenant_id, created_at, root_cause_tag
) VALUES (
  'inc_2026_07_15_001',
  'payment-svc',
  'v2.3.0 config change reduced db_pool_size 50→10, exhaustion under normal load',
  'rollback to v2.2.9; forward-fix v2.3.1 with correct pool_size planned',
  <embedding>,
  'default', now(), 'config_change'
);
```

Next time a similar incident hits `triage`, this row is in the top-3 candidates.

---

## Totals

| Metric | Value |
|---|---|
| Wall-clock (alert → approval-ready report) | **24.1s** |
| Wall-clock (including human approval + rollback exec) | ~2min |
| Total tool calls | 8 (1 memory + 4 collect + 3 verify) |
| Total LLM calls | 5 (Haiku ×1, Sonnet ×3, Opus ×3 parallel = 3 unique) |
| Cost | **~$0.14** |
| Hallucination count | 0 (all entities referenced exist in mock state) |
| Hypotheses generated | 3 (1 confirmed, 2 refuted) |
| Adversarial-eval checks passed | L1 (log lines wrapped), L2 (schema enforced), L4 (gate held for approval), L5 (report sanitized) |

---

## What this walkthrough demonstrates (interview map)

- **Multi-phase workflow**: not one big prompt, five bounded phases with distinct budgets and models.
- **Parallel tool calls** in `collect`.
- **Structured hypotheses** with priors from memory.
- **Adversarial verify**: sub-agent prompted to refute, catches the case where "similar incident said slow_query" would have misled a naive LLM.
- **Memory usage**: triage prefetch + memory-informed prior; new incident written back for future retrieval.
- **RAG usage**: runbook retrieved during collect, informs the LLM's plan.
- **Gate + preview**: rollback isn't auto-executed; dry-run diff shown to human.
- **Undo semantics**: report explicitly names the forward-fix path.
- **Cost engineering**: Haiku for triage classification, Opus reserved for verify's adversarial reasoning; median cost <$0.20 target met.
- **Security layers**: XML wrap, schema enum, reviewer verdict, gate, egress filter — all fire on this incident.

This is the story to tell when an interviewer asks "walk me through a real incident your agent handled." Every number is a fixture value you'd have in your golden set.
