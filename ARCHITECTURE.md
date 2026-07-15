# Architecture — Tier 1.5

## Design principle

> Build the smallest system that carries the **thinking** of a Tier 2 production platform. Every "simple" choice must be defensible as an intentional tradeoff, not a capability gap.

Deployed shape stays monolithic and cheap; architectural seams stay Tier-2-shaped so scale-out is a mechanical migration, not a rewrite.

---

## High-level topology

```
                         ┌────────────────────────────┐
                         │ Alert Source (mock)        │
                         │  - fault injection script  │
                         │  - manual /trigger CLI     │
                         └──────────────┬─────────────┘
                                        │ (webhook)
                                        ▼
┌───────────────────────────────────────────────────────────────────┐
│  FastAPI ingress                                                  │
│    - auth (API key)                                               │
│    - dedupe / rate-limit                                          │
│    - enqueue → Temporal workflow                                  │
└──────────────────────────────┬────────────────────────────────────┘
                               │
                               ▼
┌───────────────────────────────────────────────────────────────────┐
│  Temporal Workflow (per incident, durable)                        │
│                                                                   │
│    ┌─────────────────────────────────────────────────────────┐    │
│    │  LangGraph state machine                                │    │
│    │                                                         │    │
│    │  [triage] ─▶ [collect] ─▶ [hypothesize] ─▶ [verify]     │    │
│    │      │           │             │              │        │    │
│    │      └───────────┴─────────────┴──────────────┘        │    │
│    │                        │                                │    │
│    │                        ▼                                │    │
│    │                    [report]                             │    │
│    │                                                         │    │
│    │  Each node = small agent loop with bounded tool budget  │    │
│    └─────────────────────────────────────────────────────────┘    │
└──────────────┬──────────────────────────┬─────────────────────────┘
               │                          │
               ▼                          ▼
┌────────────────────────────┐   ┌────────────────────────────────┐
│  LLM Gateway (in-process)  │   │  MCP Tool Layer                │
│   - model routing          │   │   - observability-mcp          │
│   - prompt cache mgmt      │   │       query_metrics            │
│   - retry / fallback       │   │       query_logs               │
│   - cost accounting        │   │       get_service_topology     │
│   - Langfuse tracing       │   │   - deploy-mcp                 │
│                            │   │       list_recent_deploys      │
│                            │   │       get_diff                 │
└──────────────┬─────────────┘   │       propose_rollback (WRITE) │
               │                 └────────────────┬───────────────┘
               ▼                                  │
        Anthropic API                             ▼
                                    ┌─────────────────────────────┐
                                    │  Mock Environment           │
                                    │   (docker-compose)          │
                                    │   - 4-5 fake microservices  │
                                    │   - Prometheus + Loki       │
                                    │   - Fault injection scripts │
                                    └─────────────────────────────┘

┌───────────────────────────────────────────────────────────────────┐
│  Data Layer                                                       │
│   Postgres    → workflow state (Temporal), audit log, tenant meta │
│   pgvector    → episodic memory (past incidents + embeddings)     │
│   Redis       → prompt cache, hot topology, LLM response cache    │
└───────────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────────┐
│  Ops                                                              │
│   Langfuse Cloud   → LLM tracing, prompt versioning, eval scores  │
│   Nightly eval     → regression suite over golden set             │
│   OTEL             → agent-level metrics (latency, cost per phase)│
└───────────────────────────────────────────────────────────────────┘
```

---

## Components

### 1. Ingress (FastAPI)

Thin layer. Deduplicates alerts (idempotency key = `alert_id`), enqueues a Temporal workflow, returns immediately.

- **Auth**: API key header, one shared secret for POC.
- **Dedup window**: 5 min per `alert_id`.
- **Rate limit**: 100 req/min per source (protects downstream).

### 2. Workflow layer (Temporal)

One workflow per incident. Durable — if the worker crashes, the workflow resumes from the last completed activity.

- **Why Temporal over plain async**: incidents can run 5-30 minutes; a crash mid-investigation must not lose the hypotheses already formed.
- **Activities**: each LangGraph node is wrapped as a Temporal activity for retry semantics.
- **Signals**: human approval events come in as Temporal signals (unblocks a paused workflow).

### 3. Agent state machine (LangGraph)

Five nodes, each a small bounded agent loop:

| Node | Goal | Tool budget | Model tier |
|---|---|---|---|
| `triage` | Classify severity, extract entities, prefetch similar incidents | ≤ 3 tool calls | Haiku 4.5 |
| `collect` | Pull metrics / logs / recent deploys per hypothesis leads | ≤ 8 tool calls | Sonnet 5 |
| `hypothesize` | Generate 2-4 candidate root causes with evidence | 0 tool calls (reasoning-only) | Sonnet 5 |
| `verify` | Adversarial check — try to refute each hypothesis | ≤ 4 tool calls per hypothesis | Opus 4.8 |
| `report` | Compose structured report + post to Slack/CLI | 1 tool call (post) | Sonnet 5 |

**State schema (structured, not free-text)**:

```python
class IncidentState(TypedDict):
    incident_id: str
    alert_payload: dict
    phase: Literal["triage", "collect", "hypothesize", "verify", "report", "done"]

    # Structured products
    entities: dict              # service, region, deploy_id, etc.
    hypotheses: list[Hypothesis]  # [{claim, evidence_refs, confidence, verdict}]
    collected_signals: dict     # {metrics: {...}, logs: [...], deploys: [...]}
    similar_incidents: list     # prefetched at triage

    # Meta
    tool_calls: list            # audit trail
    gate_decisions: list        # human approvals
    cost_tokens: dict           # per-phase breakdown
    transcript: list            # for replay
```

### 4. LLM Gateway (in-process module)

Not a separate service. A thin Python module that wraps `anthropic.Anthropic()`:

- **Model routing**: caller passes a semantic label (`triage` / `reason` / `verify`), gateway maps to model.
- **Prompt cache management**: stable-prefix construction, cache_control markers.
- **Fallback chain**: primary model → same model different region → smaller model.
- **Cost accounting**: every call logs input/output/cache tokens to Postgres.
- **Tracing**: forwards to Langfuse.

Wrapping this layer now costs a day; migrating to LiteLLM/Portkey later is a one-file change.

### 5. Tool layer (MCP)

Two MCP servers, each stdio-transport for local dev:

- **`observability-mcp`** — read-only tools against mock Prometheus + Loki.
- **`deploy-mcp`** — mostly read (deploy history, diffs); one write tool (`propose_rollback`) that requires human approval via Temporal signal.

**Tool metadata schema**:

```python
class ToolMeta:
    name: str
    description: str
    side_effect: Literal["READ", "WRITE", "DESTRUCTIVE"]
    cost_hint: Literal["cheap", "expensive"]  # affects caching policy
    timeout_seconds: int
    idempotency_key: Optional[str]            # required for WRITE
```

### 6. Data layer

- **Postgres**: primary store for state, audit, cost logs, tenant meta.
- **pgvector**: episodic memory (past incidents with embeddings + structured metadata).
- **Redis**: prompt cache (Anthropic cache is 5-min; Redis is our L2 for cross-session), hot topology data, LLM response cache for identical tool-output prompts.

### 7. Observability

- **Langfuse Cloud**: every LLM call, tool call, prompt version.
- **OTEL exporter → Grafana Cloud (free tier)**: agent-level metrics (per-phase latency, cost, tool call count, error rates).
- **Structured logs**: JSON logs with `incident_id` correlation.

---

## Data flow (one incident)

1. Alert POST → FastAPI → dedupe check → Temporal workflow started.
2. `triage` node runs:
   - Parses alert, extracts `service`, `severity`, `region`.
   - Prefetches top-3 similar past incidents from pgvector.
   - Emits `entities` and `similar_incidents` into state.
3. `collect` node runs:
   - LLM (Sonnet) plans which signals to pull.
   - Parallel tool calls: `query_metrics` (last 15 min) + `query_logs` (error patterns) + `list_recent_deploys` (last 2 hours).
   - Results normalized into `collected_signals`.
4. `hypothesize` node runs:
   - LLM produces 2-4 structured hypotheses (claim + evidence pointers + prior probability from similar_incidents).
5. `verify` node runs:
   - For each hypothesis in parallel, spawn a verify sub-agent with Opus.
   - Verify sub-agent is prompted to **refute** by default.
   - Emits verdict (`confirmed` / `refuted` / `inconclusive`) + refutation attempt log.
6. `report` node runs:
   - Assembles structured Markdown report.
   - If any hypothesis suggests write action, workflow pauses on signal for human approval.
   - Posts to Slack / CLI.

---

## What's real vs mocked in Tier 1.5

| Component | Real | Mocked / stubbed |
|---|---|---|
| FastAPI ingress | ✓ | — |
| Temporal workflow | ✓ (single worker, local) | multi-worker pool |
| LangGraph state machine | ✓ | — |
| LLM Gateway | ✓ (wrap `anthropic`) | LiteLLM/Portkey feature parity |
| MCP servers | ✓ (2 servers) | full MCP fleet |
| Model routing | ✓ (3-tier) | fine-tuned/local models |
| Postgres + pgvector | ✓ | dedicated vector DB cluster |
| Redis cache | ✓ | — |
| Langfuse | ✓ (cloud) | self-hosted |
| Slack integration | ✓ (webhook, single channel) | full Slack app w/ modal approvals |
| Kafka ingestion | ✗ | in-memory queue |
| Multi-tenancy | ✗ (schema has `tenant_id`, only 1 tenant runs) | row-level security enforced |
| K8s multi-replica | ✗ (docker-compose) | HPA + Temporal worker pool |
| A/B prompt framework | ✗ (versioning present, single active version) | full A/B routing |

The **mocked column is the interview-conversation column**: "here's the seam; here's the Tier 2 migration."

---

## Deployment shape

Single-machine or single-VM:

```
docker-compose.yaml
  ├── api          (FastAPI)
  ├── worker       (Temporal worker running LangGraph)
  ├── temporal     (Temporal server, dev mode)
  ├── postgres     (state + pgvector)
  ├── redis
  ├── mock-svc-a   ┐
  ├── mock-svc-b   │
  ├── mock-svc-c   │ mock target env
  ├── prometheus   │
  ├── loki         │
  └── grafana      ┘
```

Anthropic API + Langfuse Cloud are the only external dependencies.

Estimated running cost: **$10-30/month** (Anthropic API dominates; everything else is free tier).

---

## Evolution path

See [TRADEOFFS.md](./TRADEOFFS.md) for each seam's migration story. High-level:

- **Tier 1.5 → Tier 2**: in-memory queue → Kafka; single Temporal worker → worker pool; pgvector → Qdrant cluster; single tenant → RLS multi-tenant; LLM Gateway module → LiteLLM/Portkey service; docker-compose → K8s.
- **Tier 2 → Tier 3**: monolith agent → specialist sub-agent fleet; single-region → multi-region federation; API-only models → hybrid API + self-hosted fine-tuned; hardcoded gates → OPA policy engine; vector memory → graph + vector hybrid.
