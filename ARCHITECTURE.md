# Architecture — Tier 1.5

## Design principle

> Build the smallest system that carries the **thinking** of a Tier 2 production platform. Every "simple" choice must be defensible as an intentional tradeoff, not a capability gap.

Deployed shape stays monolithic and cheap; architectural seams stay Tier-2-shaped so scale-out is a mechanical migration, not a rewrite.

**Shape in one line**: a **linear pipeline shell containing a ReAct kernel**, plus recursive sub-agents. Not a DAG; not a bare loop. See [TRADEOFFS §23](./TRADEOFFS.md#23-harness-deterministic-pipeline-around-the-agent-loop-refines-22).

---

## High-level topology

```
   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
   │ alert        │  │ chat         │  │ patrol       │   three trigger modes
   │ (webhook)    │  │ (interactive)│  │ (cron)       │   one Investigation type
   └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
          └─────────────────┼─────────────────┘
                            ▼
┌───────────────────────────────────────────────────────────────────┐
│  FastAPI ingress                                                  │
│    - auth (API key)                                               │
│    - dispatch by trigger type (trigger registry)                  │
│    - idempotent on alert_id (AlertManager retries webhooks)        │
│    - 202 + investigation_id; spawn in-process asyncio task         │
└──────────────────────────────┬────────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────────┐
│  HARNESS — deterministic pipeline (plain Python, fixed order)      │
│                                                                   │
│   ① route       trigger registry + integration registry lookup    │
│   ② preprocess  per-trigger: dedup+severity / intent / scope       │
│   ③ loadout     tool bundle · ToolBudget · window · system prompt  │
│        │                                                          │
│   ④ ┌──▼─── AGENT LOOP — ReAct kernel (LLM decides ordering) ───┐  │
│     │                                                          │  │
│     │   while turn < budget.max_turns:                          │  │
│     │       resp = await gateway.call(inv.messages, tools)      │  │
│     │       yield events                                        │  │
│     │       tool_use? → asyncio.gather(dispatch each)           │  │
│     │       submit_report? → done                               │  │
│     │                                                          │  │
│     │   ┌ refute sub-loop: own messages, own budget, top tier   │  │
│     │   └ triggered by LLM calling spawn_refute; code executes   │  │
│     └────────────────────────┬─────────────────────────────────┘  │
│   ⑤ parse       read a schema-validated Report object             │
│   ⑥ fanout      sink registry → stdout / slack / jira             │
└───────┬───────────────────────────────────┬───────────────────────┘
        │ every LLM call                    │ every tool call
        ▼                                   ▼
┌────────────────────────────┐   ┌────────────────────────────────┐
│  LLM GATEWAY (chokepoint)  │   │  MCP Tool Layer                │
│   - routing by task nature │   │   - observability-mcp          │
│   - cache_control placement│   │       query_metrics            │
│   - cost accounting        │   │       query_logs               │
│   - budget enforcement     │   │       get_service_topology     │
│   - response cache         │   │   - kafka-mcp (Week 5)         │
│   - Langfuse tracing       │   │   - deploy-mcp (Week 6, WRITE) │
│        │                   │   └────────────────┬───────────────┘
│   adapters:                │                    │
│   - OpenAICompatLLM        │                    │
│     (DeepSeek/Qwen/Kimi)   │   ┌────────────────▼───────────────┐
│   - AnthropicLLM           │   │  INTEGRATION CONFIG LAYER      │
│     (judge/reviewer family)│   │  config/integrations/*.yaml     │
└──────────┬─────────────────┘   │   - match rules                 │
           ▼                     │   - mcp server command          │
   provider APIs                 │   - runbook_namespace           │
                                 │   - prompt_fragment             │
                                 │   - notifier list               │
                                 │   - inbound_mapping (jsonpath)  │
                                 │  ZERO Python per integration    │
                                 └────────────────┬───────────────┘
                                                  ▼
                                 ┌────────────────────────────────┐
                                 │  Target environments            │
                                 │   docker-compose (13 svc, W1)   │
                                 │    - 7 microservices            │
                                 │    - Prometheus + AlertManager  │
                                 │    - ClickHouse + Vector        │
                                 │    - real Redis + exporter      │
                                 │    - Tempo (W3, scoped)         │
                                 └────────────────────────────────┘

┌───────────────────────────────────────────────────────────────────┐
│  Data Layer                                                       │
│   Postgres    → cost ledger, audit log, investigation index       │
│   pgvector    → episodic memory + runbook chunks (per-integration │
│                 namespace)                                        │
│   Redis       → alert fingerprints, gateway response cache, hot   │
│                 topology                                          │
└───────────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────────┐
│  Ops                                                              │
│   Langfuse Cloud   → LLM tracing, prompt versioning, eval scores  │
│   eval/run.py      → runs from Week 2; metric set grows weekly    │
│   OTEL             → agent-level metrics (per-turn latency, cost) │
└───────────────────────────────────────────────────────────────────┘
```

---

## Who decides what

This table is the architecture. Everything else is detail.

| Decision | Decided by | Where |
|---|---|---|
| **What to query next, how many times, in what order** | **LLM** | ④ |
| **When the investigation is finished** | **LLM** (calls `submit_report`) | ④ |
| Which tools are available at all | code (integration config) | ③ |
| Budget: max turns, max tokens, max cost | code (severity → tier) | ③ |
| Which time window every query covers | code (`Investigation.window`) | ③ |
| What the output must look like | code (Pydantic schema) | ③/⑤ |
| Who receives the report | code (sink registry) | ⑥ |
| Which model serves each call | code (gateway, by task nature) | gateway |
| Whether this alert is noise / joins an existing investigation | code (fingerprint + vector + correlation window) | ② |

> **The LLM has freedom of ordering, not freedom over resources or contracts.**

This is why the system is neither a DAG (ordering would be fixed at authoring time) nor a bare loop (the model would own the cage as well as the moves).

---

## Repository layout

The package is organized by **replaceability**, not by technical kind. `agent/core/` is the spine that must never be swapped; every sibling directory is a seam that can be replaced wholesale. This makes the layout itself explain the architecture, and it makes the seam rule mechanically checkable.

```
sre-agent/
├── pyproject.toml            agent-side deps, Python 3.11+ (mock/ has its own venv on 3.14
│                             deliberately — the observer and the observed must be able to
│                             fail independently)
├── Makefile · .env.example
│
├── agent/
│   ├── core/                 ── NOT pluggable: the spine ──
│   │   investigation.py · events.py · loop.py · harness.py
│   │   report.py · context.py · verify.py · degrade.py
│   │
│   ├── llm/                  seam: provider — gateway.py + adapters + cache + cost
│   ├── tools/                seam: tools — protocol · dispatch · bundle · mcp_client · stubs
│   ├── triggers/             seam: entry mode — registry · alert · chat · patrol
│   ├── integrations/         seam: middleware — registry · config · mapping (loader only)
│   ├── sinks/                seam: output — registry · stdout · slack · jira
│   │
│   ├── memory/               support: pgvector, runbook + episodic retrieval
│   ├── security/             support: sanitize · reviewer · gate · egress (1:1 with SECURITY.md)
│   ├── store/                support: jsonl append log · postgres
│   ├── prompts/              support: markdown fragments + assemble.py
│   └── api/                  support: FastAPI ingress
│
├── config/integrations/*.yaml   declarative; ops-editable; the whole of "adding an integration"
├── config/budgets.yaml          severity → budget tier
├── mcp_servers/{observability,k8s,deploy}/   separate processes — the agent is their client
├── srectl/                      CLI: trigger · chat · patrol · replay
├── eval/                        golden/ (3-file cases) · backlog/ · run.py · metrics · judge
├── mock/                        Week 1 target environment (+ runbooks/, tempo/ later)
└── tests/                       mirrors agent/, plus test_architecture.py
```

**Why no loose modules at the package top**: a directory with nine files and eight subpackages side by side gives a reader no way to tell which parts are the kernel and which are interchangeable. Grouping the spine under `core/` makes that distinction the first thing visible.

**The seam rule as a test** (`tests/test_architecture.py`, Week 2 L2) — two tiers:

| Module | May import | Fails on |
|---|---|---|
| `core/loop.py`, `core/investigation.py`, `core/events.py` | stdlib, `core/`, protocol modules | any concrete implementation — a provider adapter, a sink, an integration name |
| `core/harness.py` | the above plus each seam's `registry` | a concrete implementation behind a registry |

This is what makes the Week 5 L7 claim ("adding integration #3 cost zero lines of Python") an invariant enforced on every commit rather than a number measured once. A candidate architecture whose boundaries are only described is indistinguishable from one whose boundaries leak; a boundary with a failing test is not.

**`mcp_servers/` sits outside `agent/`** because MCP servers are separate processes and the agent is their *client*. Putting them inside the agent package would imply an import relationship that must not exist.

---

## Components

### 1. Ingress (FastAPI)

Thin layer. Authenticates, dispatches by trigger type, deduplicates, spawns an in-process asyncio task, returns immediately.

- **Auth**: API key header, one shared secret for POC.
- **Idempotency**: keyed on `alert_id` — AlertManager retries webhooks, and a retry must not create a second investigation.
- **Dedup window**: 5 min per `alert_id`.
- **Rate limit**: 100 req/min per source (protects downstream).
- Returns `202` + `investigation_id`.

### 2. Trigger registry

Alert is one entry mode of three. Each trigger contributes a pre-processor for harness step ② and a default sink binding for step ⑥; all three normalize to the same `Investigation`.

| Trigger | Step ② does | Step ⑥ does | Status |
|---|---|---|---|
| `alert` | fingerprint dedup, severity → budget tier, correlation window | Slack + jira | real (Week 2) |
| `chat` | intent recognition | synchronous streaming back to caller | stub (Week 2), real post-Week-5 |
| `patrol` | scope expansion → N investigations, fanned out with `asyncio.gather` | aggregated digest | stub (Week 2); value proposition undecided, see [ROADMAP open gaps](./docs/ROADMAP.md#open-gaps) |

The central noun is **`Investigation`**, not "incident" — an incident is one kind of investigation. See [TRADEOFFS §25](./TRADEOFFS.md#25-trigger-registry-alert-is-one-entry-mode-of-three).

### 3. Harness

Six steps, fixed order, plain Python (`agent/core/harness.py`). Steps ①②⑥ vary by trigger; **③④⑤ are identical across all three trigger modes** — which is what makes chat and patrol new triggers rather than new architectures.

The shell is a *linear pipeline*, not a DAG: no branches, no joins, no conditional edges. The only genuine fan-out/fan-in is patrol's outer `asyncio.gather`. See [TRADEOFFS §23](./TRADEOFFS.md#23-harness-deterministic-pipeline-around-the-agent-loop-refines-22) for why the shell is deterministic and why an LLM doing dedup would be both worse and more expensive.

### 4. Agent loop (ReAct kernel)

`agent/core/loop.py`. **Structured ReAct**: Thought is an optional assistant text block, Action is a `tool_use` block, Observation is a `tool_result` block. Same idea as the 2022 ReAct paper, but the API guarantees the structure instead of a prompt convention being parsed out of free text.

```python
async def run(
    inv: Investigation,          # messages live here → persistable, resumable
    gw: Gateway,                 # every LLM call goes through the gateway
    tools: ToolBundle,           # assembled by loadout from integration config
) -> AsyncIterator[Event]:       # TurnStarted/TextDelta/ToolCalled/ToolReturned/Done/Aborted
    while inv.turn < inv.budget.max_turns:
        resp = await gw.call(inv, tools.schemas())
        inv.messages.append(assistant_msg(resp))
        yield from events_for(resp)

        calls = resp.tool_uses()
        if not calls:
            break
        if submit := find(calls, "submit_report"):
            yield Done(report=validate(submit.input))
            return

        # parallel dispatch; a failing tool becomes an error result, not an exception
        results = await asyncio.gather(*(safe_dispatch(tools, c) for c in calls))
        inv.messages.append(Message(role="user", content=results))
        inv.turn += 1

async def run_to_completion(...) -> Report:   # thin wrapper for alert / patrol
    async for ev in run(...):
        if isinstance(ev, Done):
            return ev.report
```

Four properties that matter, each cheap to establish now and invasive to retrofit:

- **`messages` belongs to the `Investigation`**, not to the function. Chat needs multi-turn resume; alert storms need mid-flight absorption of new alerts; durability needs to serialize it. All three are the same seam.
- **Events, not a return value.** Chat needs streaming; patrol's 50-target fan-out needs progress. A string return blocks both.
- **Tool failure is a result, not an exception.** `safe_dispatch` converts any tool exception or timeout into `ToolResultBlock(is_error=True)` and lets the LLM route around it. This matters beyond robustness: **the observability stack is frequently part of the outage** — when Redis is OOM the logging path may also be broken — so reasoning under partial observability is a requirement, not an edge case.
- **Termination is `submit_report`, not `end_turn`.** A tool call is a deliberate delivery; `end_turn` merely means the model ran out of things to say. This also makes the report structurally validated and lets the handler reject a report that has no refute on record.

**State**: no separate state class beyond `Investigation`. `messages` is the conversation state; `turn` and `budget` are its accounting.

**Why loop over graph** (see [TRADEOFFS §22](./TRADEOFFS.md#22-agent-architecture-agent-loop-over-workflow-graph)):
- Root cause analysis is open-ended — which tool to call next depends on evidence gathered so far and cannot be enumerated in advance.
- Mainstream agents (Claude Code, Cursor, Devin, OpenAI Assistants) all use a single tool-use loop.
- Anthropic's "Building Effective Agents" post distinguishes *workflows* (deterministic paths) from *agents* (LLM-driven). SRE RCA is the latter — but the *shell around it* is the former, which is what §23 adds.

**Sub-agents**: the refute loop is a child `Investigation` with its own messages, its own budget, and the strongest model tier. The LLM triggers it by calling `spawn_refute`; the code executes it. `submit_report` returns `is_error` if no refute is on record, making verification structurally mandatory rather than prompt-suggested — recovering by contract what the graph enforced by topology.

**Human-in-the-loop**: `ask_human` is a tool, not a loop state, and is **non-blocking by default** — the agent records an open question and continues under a stated assumption. At 3am nobody answers a blocking prompt. See [TRADEOFFS §27](./TRADEOFFS.md#27-human-in-the-loop-non-blocking-by-default).

### 5. LLM Gateway + adapters

Not a separate service. `agent/llm/gateway.py` is the single chokepoint every LLM call passes through, and it owns six things the SDK does not do (see [TRADEOFFS §3](./TRADEOFFS.md#3-llm-gateway-in-process-wrapper-not-litellmportkey-service)):

| Responsibility | Why it can't be "SDK-native" |
|---|---|
| **Routing** by task nature (loop / refute / judge) | the SDK doesn't know what kind of call this is |
| **`cache_control` breakpoint placement** | the SDK never decides where the cache prefix ends; placed right this saves 60-70% of input tokens, placed wrong it saves nothing |
| **Cost accounting** per call → per investigation, **per currency, never converted** | [EVAL.md](./EVAL.md) names the gateway as the sole source of cost; an exchange rate would make a wrong rate indistinguishable from a price change ([§35](./TRADEOFFS.md#35-price-drift-freeze-history-flag-age-reconcile-against-billing)) |
| **Budget enforcement** — refuse the call when the ceiling is hit | turns `p99 cost < $0.40` from aspiration into mechanism; the harness then degrades instead of burning |
| **Response cache** keyed `(model, prompt_hash)` | the only way EVAL.md's reproducibility principle is physically achievable, and what makes nightly eval affordable |
| **Tracing** tagged `prompt_version` / `model_version` | required by EVAL.md's version matrix |

Retry and rate-limit backoff *are* SDK-native, and the gateway leaves them there.

**Adapters** (`agent/llm/`):

- **`types.py`** — provider-agnostic domain types (`Message`, `TextBlock`, `ToolUseBlock`, `ToolResultBlock`, `Response`).
- **`protocol.py`** — `LLM` Protocol: `async call(messages, tools) → Response`.
- **`openai_compat.py`** — `OpenAICompatLLM`, covering DeepSeek / Qwen (DashScope) / Moonshot (Kimi) through `base_url` + `api_key` alone.
- **`anthropic.py`** — `AnthropicLLM`, the different-family class.
- **`provider_catalog.py`** — static registry: provider → base_url + env var + default model.
- **`credentials.py`** — env-variable resolver with friendly errors.

**Multi-provider is a seam, not a feature.** One primary provider (China-market accessible: DeepSeek or Qwen) carries all prompt tuning and the eval baseline. A second *family* exists because [SECURITY.md](./SECURITY.md) L3 requires a different-family reviewer and [EVAL.md](./EVAL.md) requires a judge that differs from the agent — i.e. model diversity is a **design requirement**, not a cost optimization. Quality parity across providers is explicitly **not** promised. Total cost: two adapter classes and one catalog table.

**SDK, not httpx**: the `anthropic` and `openai` SDKs handle SSE streaming, retry, and rate-limit backoff correctly. Hand-writing HTTP would re-implement that badly.

**Not using LiteLLM**: a viable unified proxy, but magic we don't need at two adapter classes. Reconsider past ~6 providers or when Azure/Vertex-style non-standard endpoints appear.

### 6. Integration config layer

An integration is **a YAML file plus an MCP server — zero Python** (see [TRADEOFFS §24](./TRADEOFFS.md#24-integrations-are-configuration-not-code)):

```yaml
# config/integrations/k8s.yaml
name: k8s
match:
  alert.labels.source: kubernetes
mcp:
  command: ["python", "-m", "mcp_servers.k8s"]
runbook_namespace: k8s
prompt_fragment: prompts/integrations/k8s.md
notifier: [slack, jira]
inbound_mapping:
  service: $.labels.pod
  severity: $.labels.severity
```

This is [TRADEOFFS §20](./TRADEOFFS.md#20-middleware-specific-knowledge-lives-in-rag-not-in-agent-code-or-cases) becoming code: new middleware in production = one YAML + one runbook namespace + zero code changes. Week 5 L7 measures whether that held; the target is literally **0 lines of Python** to add integration #3.

Per-integration bundles also keep the tool schema list at ~6-10 tools per investigation instead of every tool always being present — which matters for both request size and tool-selection accuracy.

The one thing that resists pure config is inbound webhook normalization, since every vendor's payload differs. Declarative jsonpath mapping covers the common flat-JSON case; an optional Python normalizer is the escape hatch.

### 7. Tool layer (MCP)

MCP servers over stdio for local dev, one per side-effect scope:

- **`observability-mcp`** — read-only against real Prometheus + ClickHouse (Week 2 stubs, Week 3 real).
- **`k8s-mcp`** — declared in config as a seam; no cluster runs (see [TRADEOFFS §26](./TRADEOFFS.md#26-k8s-integration-a-config-only-seam-no-k3d-cluster)).
- **`deploy-mcp`** — mostly read (deploy history, diffs); one write tool (`propose_rollback`) requiring human approval (Week 6).

**Why split by side-effect class**: the write-capable server belongs in its own process with its own permission scope. That's the right invariant, and it's also the seam the Week 6 gate attaches to.

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
    undo_semantics: Literal["exact", "forward_fix", "none"]
```

**Undo semantics per side-effect class**:

| Tool | `undo_tool` | `undo_semantics` | Meaning |
|---|---|---|---|
| `propose_rollback` | `propose_rollback` | `forward_fix` | "Undo" is another rollback (to the version we came from), not a state restore — deploy history is append-only |
| `scale_service` | `scale_service` | `exact` | Idempotent reverse — call with the old replica count |
| `send_notification` | — | `none` | Notifications can't be un-sent; gate enforces two-person approval as compensating control |
| `query_metrics` / `query_logs` | — | — | READ tools don't need undo |

Undo is **not** automatic. The agent surfaces the undo path in its report; a human triggers it through the same gate. This matches production SRE reality — automated rollback of automated rollback is how 3am incidents compound.

### 8. Durability layer

**Week 2 (current)**: per-investigation JSONL append log. Every LLM call and tool result is appended, so `inv.messages` is reconstructable after a process restart. If the process dies mid-investigation, that investigation is recoverable from the log but not automatically resumed — acceptable for the 5-15 min window in POC.

**Tier 2 seam (Temporal)**: wrap the harness in a Temporal workflow; each LLM call and tool call becomes an activity so crashes resume from the last completed one, and human approval arrives as a signal. Deferred per [TRADEOFFS §2 revision](./TRADEOFFS.md#2-durable-execution-temporal-not-plain-async).

Whether we need Temporal-grade or JSONL-grade durability is a measurement, not a commitment. The `Investigation`-owns-`messages` design (§4) is what keeps either option open.

### 9. Data layer

- **Postgres**: cost ledger, audit log, investigation index, tenant meta.
- **pgvector**: episodic memory (past investigations + embeddings) + runbook chunks, both namespaced per integration (see [RAG.md](./RAG.md)).
- **Redis**: alert fingerprints for dedup/correlation, gateway response cache, hot topology.

#### Memory layering

Three distinct memory tiers, deliberately kept separate:

| Tier | Storage | Lifetime | What it holds | Retrieved when |
|---|---|---|---|---|
| **Working state** (short-term) | `inv.messages` | Duration of the investigation (minutes; longer for chat) | LLM turns, tool_use / tool_result blocks, final report | Every LLM call — passed through the gateway |
| **Episodic memory** (long-term, learned) | pgvector `past_investigations` (Week 4+) | Permanent (90-day staleness flag) | Prior summaries, root causes, resolutions | `search_similar_incidents` tool the LLM chooses to call, plus harness ② noise detection |
| **Knowledge base** (long-term, authored) | pgvector `runbook_chunks` (Week 4+) | Permanent (wiki-sourced, weekly sync) | Human-written runbooks, postmortems, playbooks | `search_runbook` tool, namespace-filtered by integration |

**Why no chatbot-style sliding window**: an alert investigation is a single loop execution; `messages` only grows as tool calls accumulate and is discarded at exit. Context pressure is handled by explicit compaction (Week 3 L4) rather than by a window — this is a per-turn concern, not a session-lifetime one. Chat mode does have session lifetime, which is exactly why `messages` lives on the `Investigation`.

**Long-term memory eviction / staleness**:
- Episodic entries older than 90 days get `stale=true`; retrieval still finds them but the LLM is prompted to weight them lower.
- Runbook chunks tied to `doc_version`; when the doc updates, old chunks are hard-deleted (see [RAG.md](./RAG.md)).
- No LRU — storage is cheap, and old incidents stay diagnostically useful ("this pattern recurred from 2 years ago").

**Multi-tenant isolation (Tier 2 seam)**: every memory row has `tenant_id`; retrieval filters on it. Tier 1.5 runs one tenant so the filter is a no-op. Adding a second tenant is config, not migration.

**Preference / user-model memory**: **not applicable**. This system operates on system events, not users.

### 10. Observability of the agent itself

- **Langfuse Cloud**: every LLM call, tool call, prompt version — wired at the gateway, so coverage is structural rather than remembered.
- **OTEL → Grafana Cloud (free tier)**: per-investigation wall-clock, per-turn LLM latency + cost, per-tool call counts, error rates.
- **Structured logs**: JSON with `investigation_id` correlation.

Note the coupling worth naming: the agent's own metrics flow into the same Prometheus it diagnoses. Fine at Tier 1.5, but in production the agent's telemetry belongs in a separate plane — otherwise the outage takes out the tooling that explains it.

---

## Data flow (one alert investigation)

1. **Ingress** — alert POST → auth → `alert_id` idempotency check → create `Investigation` → spawn asyncio task.
2. **① route** — trigger registry says `alert`; integration registry matches `observability.yaml` on the alert's labels.
3. **② preprocess** — fingerprint dedup against Redis; correlation window check (does this join an in-flight investigation instead of starting one?); severity → budget tier.
4. **③ loadout** — assemble the tool bundle from the integration's MCP server; compute `Investigation.window` (T0−30m → T0+5m); build `ToolBudget`; assemble the layered system prompt (`[A]` methodology → `[B]` output contract → `[C]` integration facet → `[D]` budget), ordered so `[A][B]` form a stable cache prefix.
5. **④ loop** — ReAct. Each iteration: gateway call → assistant response → zero-or-more `tool_use` blocks dispatched in parallel → `tool_result` blocks appended. A real trace on `GS-RES-001-redis-oom` might run: `search_similar_incidents` → sees a Redis-OOM precedent → `query_metrics(redis_memory_used_bytes)` → `query_logs(service=auth, level=error)` → `search_runbook(auth, "session write fail")` → `spawn_refute` → `submit_report`. **No fixed sequence** — the mix and order depend on evidence found. Every query inherits the window from step ③.
6. **⑤ parse** — read the schema-validated `Report` from the `submit_report` call: `root_cause`, `confidence`, `evidence`, `ruled_out`, `recommended_next_step`, `open_questions`, `assumptions`, `undo_path`.
7. **⑥ fanout** — sink registry per the integration's `notifier` list: stdout + Slack + jira, with the Langfuse trace URL attached.

**Degraded paths**, all of which produce a report rather than nothing:
- A tool fails or times out → `is_error` result, LLM routes around it, report states what could not be checked.
- Budget exhausted → gateway refuses the call, harness emits an "insufficient evidence" report with what was established.
- Information only a human has → `ask_human` records an open question, investigation continues under a stated assumption.

---

## What's real vs mocked in Tier 1.5

| Component | Real | Mocked / stubbed |
|---|---|---|
| FastAPI ingress | ✓ (Week 2 L7) | — |
| Trigger registry | ✓ `alert` | `chat` / `patrol` stubs |
| Harness (6 steps) | ✓ (Week 2 L6) | — |
| Agent loop (ReAct) | ✓ | — |
| LLM Gateway | ✓ (Week 2 L3) | LiteLLM/Portkey proxy service |
| LLM adapters | ✓ 2 classes (OpenAI-compat + Anthropic) | quality parity across providers |
| Integration config layer | ✓ (Week 2 L5) | — |
| Integrations with real tools | ✓ 2 (observability, kafka) | k8s / jira / grafana as config-only seams |
| MCP servers | ✓ (stdio) | full MCP fleet |
| Temporal durability | ✗ **out of scope**, not deferred ([§32](./TRADEOFFS.md#32-temporal-is-out-of-scope-not-deferred)) | Temporal workflow + activities |
| Model routing | ✓ 2 tiers (loop / judge-reviewer) | 3-tier with a cheap classifier |
| Postgres + pgvector | ✓ | dedicated vector DB cluster |
| Redis | ✓ | — |
| Langfuse | ✓ (cloud) | self-hosted |
| Slack integration | ✓ (webhook, single channel) | full Slack app w/ modal approvals |
| Kafka ingestion (of alerts) | ✗ | in-memory queue |
| Multi-tenancy | ✗ (schema has `tenant_id`, 1 tenant runs) | row-level security enforced |
| K8s | ✗ (config-only seam, no cluster — [§26](./TRADEOFFS.md#26-k8s-integration-a-config-only-seam-no-k3d-cluster)) | HPA + worker pool |
| A/B prompt framework | ✗ (versioning present, single active version) | full A/B routing |

The **mocked column is the interview-conversation column**: "here's the seam; here's the Tier 2 migration."

---

## Deployment shape

Single-machine or single-VM:

```
docker-compose.yaml
  ├── api           (FastAPI + harness + loop, in-process asyncio)   # Week 2 L7
  ├── postgres      (cost ledger, audit, pgvector)                   # Week 4+
  ├── redis         (real; also used by mock env's auth-svc)         # Week 1 + reused
  │
  │  Target env (Week 1, all real components):
  ├── gateway / checkout / payment / inventory / auth /
  │    deploy-history / incident-tracker
  ├── prometheus / alertmanager
  ├── clickhouse / vector
  ├── redis-exporter
  └── tempo                                                          # Week 3, scoped
```

External dependencies: LLM provider APIs (DeepSeek / Qwen / Kimi / Anthropic) + Langfuse Cloud.

Temporal is **out of scope**, not deferred — in-process asyncio plus a per-investigation JSONL append log is the Tier 1.5 answer, and `Investigation` owning `messages` is what keeps the migration mechanical if it is ever needed. See [TRADEOFFS §32](./TRADEOFFS.md#32-temporal-is-out-of-scope-not-deferred).

Estimated running cost: **$5-15/month** (DeepSeek-priced tokens dominate at ¥1-2/M input; Qwen-plus and Kimi similar magnitude; other infra free tier or local). The gateway's response cache is what keeps nightly eval inside this envelope.

---

## Target scale

| Dimension | Tier 1.5 target | Tier 2 (aspirational) |
|---|---|---|
| Alerts / day | ~50 (mock traffic + eval reruns) | 5k across a real fleet |
| Peak alerts / minute | <10 | 100+ during a fleet-wide event |
| Concurrent investigations | 1-3 (alert) / 50 (patrol fan-out) | 20-50 sustained |
| Median investigation duration | 60-90s agent wall-clock | Same — parallelism doesn't reduce per-investigation latency past a floor |
| p99 investigation cost | <$0.40, **enforced by the gateway** | Same target with better routing |
| Runbook corpus | ~100 docs, ~3k chunks | ~2k docs, ~50k chunks |
| Episodic memory | ~10k past investigations | ~1M |
| Registered integrations | 5 (2 with live tools) | 20+ |

**Where Tier 1.5 breaks first as load grows**:
1. Single FastAPI worker at ~50 req/sec (add Kafka + horizontal scale)
2. Patrol fan-out past ~50 concurrent investigations (add a worker pool)
3. pgvector past ~500k vectors (migrate to Qdrant/Milvus)
4. Provider-native prompt cache TTL doesn't cover cross-session (the gateway's Redis L2 already covers this seam)

---

## Evolution path

See [TRADEOFFS.md](./TRADEOFFS.md) for each seam's migration story. High-level:

- **Tier 1.5 → Tier 2**: in-memory queue → Kafka; asyncio tasks → Temporal + worker pool; pgvector → Qdrant cluster; single tenant → RLS multi-tenant; gateway module → LiteLLM/Portkey service; docker-compose → K8s.
- **Tier 2 → Tier 3**: monolith agent → specialist sub-agent fleet; single-region → multi-region federation; API-only models → hybrid API + self-hosted fine-tuned; hardcoded gates → OPA policy engine; vector memory → graph + vector hybrid.
