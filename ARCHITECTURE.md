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
│  Agent loop (in-process asyncio; Temporal target Week 5+)         │
│                                                                   │
│    ┌─────────────────────────────────────────────────────────┐    │
│    │  run_incident(alert):                                   │    │
│    │                                                         │    │
│    │  while not done and turn < MAX_TURNS:                   │    │
│    │      response = await llm.call(messages, tools)         │    │
│    │      messages.append(assistant_msg(response))           │    │
│    │      if response.stop_reason == "tool_use":             │    │
│    │          for tc in response.tool_calls:                 │    │
│    │              result = await tools[tc.name](**tc.input)  │    │
│    │          messages.append(tool_results)                  │    │
│    │      elif response.stop_reason == "end_turn":           │    │
│    │          done = True                                    │    │
│    │                                                         │    │
│    │  LLM-driven ordering (no fixed phases)                  │    │
│    │  `messages` array = state; stop_reason drives exit      │    │
│    └─────────────────────────────────────────────────────────┘    │
└──────────────┬──────────────────────────┬─────────────────────────┘
               │                          │
               ▼                          ▼
┌────────────────────────────┐   ┌────────────────────────────────┐
│  LLM adapter layer         │   │  MCP Tool Layer                │
│   - AnthropicLLM           │   │   - observability-mcp          │
│   - OpenAICompatLLM        │   │       query_metrics            │
│     (DeepSeek/Qwen/Kimi)   │   │       query_logs               │
│   - provider_catalog       │   │       get_service_topology     │
│   - credentials resolver   │   │   - deploy-mcp (Week 5+)       │
│   - Langfuse tracing       │   │       list_recent_deploys      │
│                            │   │       get_diff                 │
└──────────────┬─────────────┘   │       propose_rollback (WRITE) │
               │                 └────────────────┬───────────────┘
               ▼                                  │
   Anthropic / DeepSeek /                         ▼
   Qwen / Kimi provider APIs      ┌─────────────────────────────────┐
                                  │  Mock Environment               │
                                  │   (docker-compose, 12 services) │
                                  │   - 7 microservices (Week 1)    │
                                  │   - Prometheus + AlertManager   │
                                  │   - ClickHouse + Vector         │
                                  │   - real Redis + redis-exporter │
                                  │   - fault-injection admin API   │
                                  └─────────────────────────────────┘

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
│   OTEL             → agent-level metrics (per-turn latency, cost) │
└───────────────────────────────────────────────────────────────────┘
```

---

## Components

### 1. Ingress (FastAPI)

Thin layer. Deduplicates alerts (idempotency key = `alert_id`), enqueues a Temporal workflow, returns immediately.

- **Auth**: API key header, one shared secret for POC.
- **Dedup window**: 5 min per `alert_id`.
- **Rate limit**: 100 req/min per source (protects downstream).

### 2. Durability layer

**Week 2 (current)**: none. FastAPI spawns an in-process asyncio task running `run_incident(alert)`. If the process dies mid-incident, that incident is lost — an acceptable risk for the 5-15 min per-incident window in POC.

**Week 5-6 target (Temporal)**: wrap the loop in a Temporal workflow; each LLM call and each tool call becomes a Temporal activity so crashes resume from the last completed activity. Human approval events arrive as Temporal signals. See [TRADEOFFS §2 revision](./TRADEOFFS.md#2-durable-execution-temporal-not-plain-async) for why this is deferred.

**Alternative to Temporal considered**: append-only JSONL log per incident is enough to reconstruct `messages` on restart. Whether we need Temporal-grade durability or JSONL-grade durability is a Week 5 measurement, not a Week 2 commitment.

### 3. Agent loop

A single async function `run_incident(alert, llm, tools)` drives a `while` loop. Each turn the LLM decides either to call one or more tools (`stop_reason == "tool_use"`) or to emit a final report (`stop_reason == "end_turn"`). Tool results are appended to `messages` (Anthropic-format content blocks); the LLM sees the full history each turn.

**Loop shape** (~30 lines, `agent/loop.py`):

```python
async def run_incident(alert, llm, tools) -> str:
    messages = [Message(role="user",
                        content=[TextBlock(f"<alert>{json.dumps(alert)}</alert>")])]
    turn, done = 0, False
    schemas = tool_schemas(tools)

    while not done and turn < MAX_TURNS:
        response = await llm.call(messages, tools=schemas)
        messages.append(Message(role="assistant", content=list(response.content)))
        if response.stop_reason == StopReason.END_TURN:
            done = True
        elif response.stop_reason == StopReason.TOOL_USE:
            results = []
            for tc in response.tool_calls:
                result = await tools[tc.name].run(**tc.input)
                results.append(ToolResultBlock(tool_use_id=tc.id, content=result))
            messages.append(Message(role="user", content=results))
        turn += 1
    return _extract_final_text(messages)
```

**Why loop over graph** (see [TRADEOFFS §22](./TRADEOFFS.md#22-agent-architecture-agent-loop-over-workflow-graph)):
- SRE root cause analysis is open-ended — which tool to call next depends on evidence gathered so far, and cannot be enumerated in advance.
- Mainstream agents (Claude Code, Cursor, Devin, OpenAI Assistants) all use a single tool-use loop.
- Anthropic's "Building Effective Agents" post distinguishes *workflows* (deterministic paths, LangGraph territory) from *agents* (LLM-driven, loop territory). SRE RCA is the latter.

**State**: no separate state class. `messages` is the state; `turn` and `done` are loop-local variables. Domain types (`Message` / `Response` / `ToolUseBlock` / etc.) in `agent/llm/types.py` are provider-agnostic and follow the Anthropic content-block shape (strictly more expressive than OpenAI's flat `content + tool_calls`).

**Tool budget** (Week 2 stub): global `MAX_TURNS = 15` runaway backstop. Per-tool budgets (allow 8 metric queries, 4 log queries, 2 runbook lookups) can be added at the tool layer when a real LLM makes over-budget behavior observable — deferred until real prompts land in Week 3.

**Model routing** (Week 3+): currently one model per session. Role-based routing (main agent vs. report generation vs. adversarial verify vs. LLM judge) will land when Week 3 real report generation or Week 5 LLM judge create the concrete requirement. See [TRADEOFFS §22](./TRADEOFFS.md#22-agent-architecture-agent-loop-over-workflow-graph) related revisions on §5.

### 4. LLM adapter layer

Not a separate service. A set of Python modules that wrap the `anthropic` and `openai` SDKs:

- **`agent/llm/types.py`** — provider-agnostic domain types (`Message`, `TextBlock`, `ToolUseBlock`, `ToolResultBlock`, `Response`).
- **`agent/llm/protocol.py`** — `LLM` Protocol: `async call(messages, tools) → Response`.
- **`agent/llm/anthropic.py`** — Anthropic Messages API adapter (`AnthropicLLM`).
- **`agent/llm/openai_compat.py`** — OpenAI-compatible adapter (`OpenAICompatLLM`) covering DeepSeek / Qwen (DashScope) / Moonshot (Kimi) / others via `base_url` + `api_key` config.
- **`agent/llm/provider_catalog.py`** — static registry: provider name → base_url + env var + default model.
- **`agent/llm/credentials.py`** — env-variable resolver with friendly error messages.

**Multi-provider rationale**: China-market context — DeepSeek and Alibaba's Qwen are the primary access-easy models; Kimi (Moonshot) as third provider for triangulation. Anthropic supported for cross-provider comparison and future when API credits are in place. Adding a new OpenAI-compat provider is one dict entry in the catalog.

**SDK, not httpx**: `anthropic` and `openai` SDKs handle SSE streaming, retry, rate-limit backoff, and prompt caching natively. Hand-writing HTTP would burn hundreds of lines re-implementing what the SDKs already ship correctly. This is the pattern OpenSRE follows too (native SDKs by default; LiteLLM opt-in via env var).

**Not using LiteLLM**: LiteLLM is a viable unified proxy but adds a layer of magic we don't need at 4 providers. OpenSRE's default path also skips LiteLLM (opt-in only via `OPENSRE_LLM_TRANSPORT=litellm`). Reconsider when the provider count crosses ~6 or we need Azure/Vertex-style non-standard endpoints.

**Cost / retry / streaming**: SDK-native. Langfuse trace wiring lands in Week 2 L5 end-to-end smoke.

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
    undo_tool: Optional[str]                  # name of the tool that reverses this one
    undo_semantics: Literal["exact", "forward_fix", "none"]  # what "undo" means for this tool
```

**Undo semantics per side-effect class**:

| Tool | `undo_tool` | `undo_semantics` | Meaning |
|---|---|---|---|
| `propose_rollback` | `propose_rollback` | `forward_fix` | "Undo" is another rollback (to the version we came from), not a state restore — the deploy history is append-only |
| `scale_service` | `scale_service` | `exact` | Idempotent reverse — call with the old replica count |
| `send_notification` | — | `none` | Notifications can't be un-sent; gate enforces two-person approval as compensating control |
| `query_metrics` / `query_logs` | — | — | READ tools don't need undo |

Undo is **not** automatic. The agent surfaces the undo path in its report; a human triggers it via the same gate (Temporal signal → approval). This matches production SRE reality — automated rollback of automated rollback is how you get into 3am compounded incidents.

### 6. Data layer

- **Postgres**: primary store for state, audit, cost logs, tenant meta.
- **pgvector**: episodic memory (past incidents with embeddings + structured metadata) + Runbook RAG chunks (see [RAG.md](./RAG.md)).
- **Redis**: prompt cache (Anthropic cache is 5-min; Redis is our L2 for cross-session), hot topology data, LLM response cache for identical tool-output prompts.

#### Memory layering

Three distinct memory tiers, deliberately kept separate:

| Tier | Storage | Lifetime | What it holds | Retrieved when |
|---|---|---|---|---|
| **Working state** (short-term) | `messages` array in the loop (in-process asyncio task) | Duration of the incident (minutes) | LLM turns, tool_use / tool_result blocks, final report text | Every LLM call — passed as function arg to `llm.call()` |
| **Episodic memory** (long-term, learned) | pgvector `past_incidents` (Week 4+) | Permanent (with 90-day staleness flag) | Prior incident summaries, root causes, resolutions | `search_similar_incidents` tool the LLM chooses to call |
| **Knowledge base** (long-term, authored) | pgvector `runbook_chunks` (Week 4+) | Permanent (wiki-sourced, weekly sync) | Human-written runbooks, postmortems, playbooks | `search_runbook` tool the LLM chooses to call |

**Why no "sliding window" like a chatbot**: incidents aren't multi-turn user conversations. Each incident is a single agent-loop execution over `messages`; the array only grows as tool calls accumulate, and is discarded when the loop terminates. Long-running compaction (summarizing older tool_results into a single message before proceeding) can be added when context window pressure appears — that's a per-turn concern, not a session-lifetime one.

**Long-term memory eviction / staleness**:
- Episodic memory entries older than 90 days get a `stale=true` flag; retrieval still finds them but the LLM is prompted to weight them lower.
- Runbook chunks tied to `doc_version`; when the doc updates, old chunks are hard-deleted (see [RAG.md](./RAG.md) incremental update).
- No LRU — storage is cheap, and old incidents remain diagnostically useful ("this bug pattern recurred from 2 years ago").

**Multi-tenant memory isolation (Tier 2 seam)**: every memory row has `tenant_id`; retrieval is `WHERE tenant_id = current_tenant() AND ...`. In Tier 1.5 there's one tenant, so filter is a no-op. Adding a second tenant is a config change, not a data migration — the seam is already in the schema.

**Preference / user-model memory (chatbot pattern)**: **not applicable to this system**. SRE agent operates on incidents (system events), not on users. If we ever added a "notify oncall based on their preference for Slack vs PagerDuty" feature, that'd be a `users` table with structured columns, not a memory retrieval problem.

### 7. Observability

- **Langfuse Cloud**: every LLM call, tool call, prompt version.
- **OTEL exporter → Grafana Cloud (free tier)**: agent-level metrics (per-incident wall-clock, per-turn LLM latency + cost, per-tool call count, error rates).
- **Structured logs**: JSON logs with `incident_id` correlation.

---

## Data flow (one incident)

1. Alert POST → FastAPI → dedupe check → spawn asyncio task running `run_incident(alert)`.
2. `messages` initialized with `<alert>{...}</alert>` wrapper (Week 5 L1 XML data isolation preludes this shape).
3. Loop begins. Each iteration:
   - **LLM turn** — LLM sees full `messages` + tool schemas; returns text + zero-or-more `tool_use` blocks.
   - **Tool dispatch** — for each `tool_use`, invoke registered tool (`query_metrics` / `query_logs` / `search_runbook` / `search_similar_incidents` / `list_recent_deploys` / ...); results appended as `tool_result` blocks in a `role=user` message.
   - **Termination check** — `stop_reason == "end_turn"` (LLM emitted final report) or `MAX_TURNS` fires (backstop).
4. **LLM-driven ordering** — a real trace on `GS-RES-001-redis-oom` might look like:  
   `alert` → LLM decides `search_similar_incidents(alert)` → LLM sees a Redis-OOM precedent → LLM decides `query_metrics(promql='redis_memory_used_bytes')` → LLM decides `query_logs(service='auth', level='error')` → LLM decides `search_runbook(service='auth', symptom='session write fail')` → LLM emits final report with `end_turn`.  
   No fixed sequence. The mix and order depend on evidence found.
5. **Optional verify** (Week 3+ decision): either a `verify_hypothesis` tool the LLM chooses to call, or an outer "refute" loop that runs a separate LLM over the final report before it ships. Design deferred until we see how the base loop reasons.
6. **Approval gate** (Week 5): write-classified tools (`propose_rollback`) trigger human approval before dispatch. Week 2 has no write tools; gate infrastructure lands later.
7. **Report** — last assistant message's text is the report; post to Slack + CLI + Langfuse trace URL.

---

## What's real vs mocked in Tier 1.5

| Component | Real | Mocked / stubbed |
|---|---|---|
| FastAPI ingress | ✓ (Week 2 L4) | — |
| Agent loop (`run_incident`) | ✓ | — |
| LLM adapter layer | ✓ (Anthropic + OpenAI-compat: DeepSeek/Qwen/Kimi) | LiteLLM unified proxy |
| Temporal durability | ✗ (deferred to Week 5-6) | Temporal workflow + activities |
| MCP servers | ✓ (2 servers: observability + deploy) | full MCP fleet |
| Model routing | ✗ (single-model per session) | role-based routing (agent/report/verify/judge) |
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
  ├── api           (FastAPI + agent loop, in-process asyncio)      # Week 2 L4
  ├── postgres      (audit log, pgvector for memory/RAG)             # Week 4+
  ├── redis         (real, also used by mock env's auth-svc)         # Week 1 + reused
  │
  │  Mock target env (Week 1, all real components):
  ├── gateway / checkout / payment / inventory / auth /
  │    deploy-history / incident-tracker
  ├── prometheus / alertmanager
  ├── clickhouse / vector
  └── redis-exporter
```

External dependencies: LLM provider APIs (DeepSeek / Qwen / Kimi / Anthropic) + Langfuse Cloud.

Temporal is intentionally not in the compose file — Week 2 uses in-process asyncio; Temporal is a Week 5-6 addition when production-grade durability becomes a shipping requirement (see [TRADEOFFS §2 revision](./TRADEOFFS.md#2-durable-execution-temporal-not-plain-async)).

Estimated running cost: **$5-15/month** (DeepSeek-priced tokens dominate at ¥1-2/M input; Qwen-plus and Kimi similar magnitude; other infra free tier or local).

---

## Target scale

| Dimension | Tier 1.5 target | Tier 2 (aspirational) |
|---|---|---|
| Alerts / day | ~50 (mostly during business hours, mock traffic + eval reruns) | 5k across a real fleet |
| Peak alerts / minute | <10 | 100+ during a fleet-wide event |
| Concurrent incidents | 1-3 | 20-50 |
| Median incident duration | 60-90s (agent wall-clock) | Same — agent parallelism doesn't reduce per-incident latency past a floor |
| p99 incident cost | <$0.40 | Same target with better model routing |
| Runbook corpus | ~100 docs, ~3k chunks | ~2k docs, ~50k chunks |
| Episodic memory | ~10k past incidents | ~1M |

**Where Tier 1.5 breaks first as load grows** (from [TRADEOFFS.md](./TRADEOFFS.md) evolution paths):
1. Single FastAPI worker at ~50 req/sec (add Kafka + horizontal scale)
2. Single Temporal worker at ~20 concurrent incidents (add worker pool)
3. pgvector past ~500k vectors (migrate to Qdrant/Milvus)
4. Anthropic native cache 5-min TTL doesn't cover cross-session (add Redis L2)

---

## Evolution path

See [TRADEOFFS.md](./TRADEOFFS.md) for each seam's migration story. High-level:

- **Tier 1.5 → Tier 2**: in-memory queue → Kafka; single Temporal worker → worker pool; pgvector → Qdrant cluster; single tenant → RLS multi-tenant; LLM Gateway module → LiteLLM/Portkey service; docker-compose → K8s.
- **Tier 2 → Tier 3**: monolith agent → specialist sub-agent fleet; single-region → multi-region federation; API-only models → hybrid API + self-hosted fine-tuned; hardcoded gates → OPA policy engine; vector memory → graph + vector hybrid.
