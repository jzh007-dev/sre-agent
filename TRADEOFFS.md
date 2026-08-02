# Tradeoffs

Every meaningful design decision recorded as: **what we chose**, **the alternatives considered**, **why we chose it**, and **when we'd change our mind**. This is the interview-conversation document.

Format for each entry:

- **Decision**: the chosen approach
- **Alternatives**: what else was on the table
- **Why**: reasoning, ideally with a scenario
- **Cost**: what this decision costs us
- **Reconsider when**: signals that would flip the decision

---

## 1. Agent paradigm: workflow-skeleton with agentic sub-nodes

> **Superseded by [§22](#22-agent-architecture-agent-loop-over-workflow-graph)** (2026-07-27). Original decision preserved as history; see §22 for the current architecture and why we flipped mid-Week-2-planning. The `experiment/langgraph` branch preserves a pre-pivot HEAD so this decision remains buildable for comparison.

- **Decision** (superseded): LangGraph 5-node state machine (`triage → collect → hypothesize → verify → report`); each node runs a bounded agent loop.
- **Alternatives**:
  - (A) Pure agentic loop (Claude-Code-style) — one big loop, LLM picks any tool at any time.
  - (B) Pure workflow — every step deterministic, LLM only for text generation.
- **Why**: incident response has known phases; the agentic freedom belongs *inside* a phase (which signal to pull, which hypothesis to check) not *across* phases (skipping verify because model felt confident). This is also the only shape that lets us do **phase-level eval assertions**.
- **Cost**: some incidents don't fit the 5-phase mold (e.g., "obvious rollback needed") and pay unnecessary overhead.
- **Reconsider when**: >30% of incidents short-circuit the graph, or eval reveals phase boundaries hurt accuracy.

## 2. Durable execution: Temporal, not plain async

> **Partial revision (2026-07-27)**: Temporal is deferred out of Week 2. See [§22](#22-agent-architecture-agent-loop-over-workflow-graph) "Related revisions" — with the loop-based agent architecture, messages + JSONL append log gives short-horizon durability for the 5-15 min incident window; Temporal returns Week 6-7 if production-grade durability becomes a shipping requirement. Original decision below is unchanged as the *eventual* target.

- **Decision**: Temporal workflow wrapping the LangGraph run.
- **Alternatives**:
  - (A) Plain `asyncio` with checkpoints to Postgres.
  - (B) Celery/RQ with periodic state pickling.
- **Why**: incidents run 5-30 minutes. Losing state mid-verify because a worker OOMs is unacceptable. Temporal gives durability + retry semantics + signal-based human approval for free. Rolling our own would be 2 weeks and worse.
- **Cost**: extra service to run, learning curve, harder local debugging than a plain script.
- **Reconsider when**: p99 incident duration falls under 60s (then plain async is fine).

## 3. LLM Gateway: in-process wrapper, not LiteLLM/Portkey service

> **Reaffirmed and scoped (2026-08-01)**. During the Week 2 pivot this module was renamed "LLM adapter layer" in [ARCHITECTURE.md](./ARCHITECTURE.md), and four of its five responsibilities were dropped behind the phrase *"SDK-native."* That phrase is true only for **retry and rate-limit backoff**. The SDK does not decide where `cache_control` breakpoints go, does not accumulate per-investigation cost, does not enforce a budget ceiling, and does not cache responses. Two documents depend on the parts that were dropped: [EVAL.md](./EVAL.md) names the gateway as the sole source of `cost_usd`, and gateway-level response caching is the only mechanism that makes EVAL.md's reproducibility principle (`deterministic given (seed, model_version, prompt_version, golden_set_version)`) physically achievable — LLM calls are otherwise non-deterministic. The gateway is restored as a first-class component in Week 2 L3, owning six responsibilities:
>
> 1. **Routing** by task nature (main loop / refute / judge) — see [§5 revision](#5-model-routing-3-tier-haiku--sonnet--opus).
> 2. **`cache_control` breakpoint placement** over the layered system prompt. Placed correctly this saves 60-70% of input tokens; placed wrongly it saves nothing. Pure code decision, unrelated to model quality.
> 3. **Cost accounting** — per-call → per-investigation accumulation, persisted.
> 4. **Budget enforcement** — a hard per-investigation ceiling that *refuses* the call so the harness can degrade gracefully. This is what turns `p99 incident cost < $0.40` from an aspiration into a mechanism.
> 5. **Response cache** keyed `(model, prompt_hash)` — makes nightly eval affordable *and* reproducible.
> 6. **Tracing** — Langfuse, tagged with `prompt_version` / `model_version` per [EVAL.md](./EVAL.md).

- **Decision**: hand-written `llm_gateway.py` — model routing, prompt cache mgmt, retry, cost accounting, Langfuse tracing.
- **Alternatives**:
  - (A) LiteLLM proxy — mature, feature-complete.
  - (B) Portkey — same.
  - (C) Direct `anthropic.Anthropic()` calls scattered in code.
- **Why**: we want the *seam* now for tradeoff conversation, but a proxy service is operational overhead we can't justify at QPS < 1. The hand-written module is 200 lines and demonstrates every concept the proxies do.
- **Cost**: features like model-provider fallback (Anthropic → Bedrock) require code we haven't written; a real gateway has them off-the-shelf.
- **Reconsider when**: adding a second LLM provider, or hitting the point where model config changes require a redeploy of the app rather than a config push.

## 4. Tool layer: MCP, not direct function tools

- **Decision**: two MCP servers (`observability-mcp`, `deploy-mcp`) over stdio.
- **Alternatives**:
  - (A) Direct Python functions passed as `tools=[...]` to the SDK.
  - (B) A single MCP server with all tools.
- **Why**: MCP is where the industry is going, and the isolation matters — the deploy tool has write side-effects and belongs in its own process with its own permission scope. Splitting by side-effect class is the right invariant.
- **Cost**: MCP adds subprocess boundary + serialization overhead vs direct calls (~5-10ms per call). For POC volume this is invisible.
- **Reconsider when**: profiling shows tool-boundary latency dominates end-to-end (won't happen at our scale, but noting for completeness).

## 5. Model routing: 3-tier (Haiku → Sonnet → Opus)

> **Revised (2026-08-01) — the routing key changed from *phase* to *task nature*, and the justification changed from *cost* to *requirement*.**
>
> Phases died with the graph pivot ([§22](#22-agent-architecture-agent-loop-over-workflow-graph)), which left routing with nothing to key on. It returns keyed on the nature of the call, which is a better predictor of required capability than position in a pipeline ever was:
>
> | Call | Tier | Why |
> |---|---|---|
> | Main ReAct loop | workhorse (primary provider) | carries all prompt tuning and the eval baseline |
> | Refute sub-loop | strongest | adversarial reasoning is where hallucinations get caught |
> | LLM judge / L3 reviewer | **different family, mandatory** | [EVAL.md](./EVAL.md) requires the judge to differ from the agent; [SECURITY.md](./SECURITY.md) L3 requires a different-family reviewer |
> | Alert noise classification | **deferred — rules first** | a cheap-model classifier is premature until measurement shows fingerprint + vector similarity is insufficient |
>
> **The cost argument is retired as the primary justification.** At an estimated $5-15/month, "Haiku is 10× cheaper" does not justify the burden of N prompt tunings and an N-way eval matrix. What *does* justify multi-model is that security and evaluation both **require** model diversity by design. Multi-provider therefore ships as a **seam, not a feature**: one primary provider carries all tuning; a second family exists to satisfy the judge/reviewer requirement; quality parity across providers is explicitly **not** promised. `OpenAICompatLLM` covers DeepSeek / Qwen / Kimi through `base_url` alone, so "multi-provider" costs two adapter classes and one catalog table — not a multi-LLM project.

- **Decision**: Haiku 4.5 for triage (extract entities, prefetch memory); Sonnet 5 for collect/hypothesize/report; Opus 4.8 for verify only.
- **Alternatives**:
  - (A) Sonnet everywhere — simpler.
  - (B) Opus everywhere — best quality.
  - (C) Haiku everywhere — cheapest.
- **Why**: triage is a classification task Haiku nails at 1/10 the cost. Verify is adversarial reasoning where Opus's extra depth pays for itself (verify is where hallucinations get caught). Sonnet is the workhorse in between.
- **Cost**: three models to test, three sets of prompt tuning, harder cross-model comparability in eval.
- **Reconsider when**: eval shows any tier underperforms one step up by <5%, collapse toward the cheaper model.

## 6. Memory: pgvector + Postgres, not a dedicated vector DB

- **Decision**: episodic memory stored in Postgres with `pgvector` extension; structured metadata as regular columns.
- **Alternatives**:
  - (A) Qdrant / Weaviate / Pinecone.
  - (B) Chroma (local file).
  - (C) No vector store — pure keyword search over incident summaries.
- **Why**: <10k historical incidents in POC. pgvector handles this fine, and colocation with Postgres lets us do `WHERE service = X AND severity = Y ORDER BY embedding <=> $query` in one hop. Adding a second datastore for POC scale is complexity theater.
- **Cost**: pgvector's HNSW index performance degrades past ~1M vectors; ANN quality is behind Qdrant.
- **Reconsider when**: >500k vectors, or query p99 > 100ms.

## 7. Retrieval: hybrid (dense + BM25 + metadata), not dense-only

- **Decision**: pgvector dense similarity + Postgres full-text (`tsvector`) BM25 + hard metadata filters (service, region, time_range).
- **Alternatives**:
  - (A) Dense-only.
  - (B) Sparse-only (BM25).
  - (C) Add a reranker layer (Cohere / BGE).
- **Why**: dense retrieval fails on **specific identifiers** (service names, deploy IDs, error codes) because the embedding smooths them out. Metadata filters compress candidate set before ranking. Reranker is Tier 2 territory — POC gets by without.
- **Cost**: three retrieval paths to maintain; hybrid scoring formula needs tuning.
- **Reconsider when**: recall@5 stays below 0.85 with tuning maxed out (add reranker); or when latency becomes an issue (drop BM25).

## 8. Ingestion: in-memory queue, not Kafka

- **Decision**: FastAPI accepts webhook, immediately starts Temporal workflow; no external queue.
- **Alternatives**:
  - (A) Kafka topic per tenant.
  - (B) Redis Streams.
  - (C) SQS.
- **Why**: peak load is <10 alerts/minute in POC. Kafka would be textbook resume-driven design. Temporal itself buffers workflow starts.
- **Cost**: burst tolerance is capped by FastAPI worker count; a real alert storm (>100/sec) would drop requests.
- **Reconsider when**: peak >50 req/sec, or when we need cross-consumer replay for eval.

## 9. Multi-tenancy: schema present, not enforced

- **Decision**: every table has `tenant_id` column; POC runs one tenant; RLS policies drafted but not enforced.
- **Alternatives**:
  - (A) No tenant model at all.
  - (B) Schema-per-tenant.
  - (C) Full row-level security enforcement now.
- **Why**: the schema seam is 30 minutes; enforcement + tenant onboarding + cost accounting is a week we don't have. Making the seam visible is the interview payoff.
- **Cost**: bugs where `tenant_id` is forgotten in a query can't surface until we actually run multi-tenant.
- **Reconsider when**: adding a second tenant (real or mocked with distinct data).

## 10a. Gate = approval workflow layer, NOT preview generator

- **Decision**: Gate handles who-approves-what-when + audit + blast-radius policy + timeout escalation. Gate does **not** generate previews of what an action will do.
- **Alternatives**:
  - (A) Gate internally simulates every action (would reinvent Terraform / kubectl semantics).
  - (B) No gate, LLM asked to "be careful" (unauditable).
- **Why**: preview semantics are domain-specific and belong in the tool that owns them. Gate is control flow.
- **Cost**: every WRITE tool must implement a `preview()` method (either wrap native dry-run or explicitly declare `preview_supported=False`).
- **Reconsider when**: never — this separation is invariant.

## 10b. Preview = native tool dry-run, wrapped by gate

- **Decision**: WRITE tools invoke native mechanisms (`kubectl --dry-run=server`, `terraform plan`, `pg_dry_run`) to produce a preview; the gate attaches the preview to the approval request.
- **Alternatives**:
  - (A) Custom preview engine per tool (huge surface, semantic drift).
  - (B) LLM writes the preview text (untrustworthy, injection-prone).
- **Why**: native dry-run is battle-tested, semantically correct, and free.
- **Cost**: tools without native dry-run support (e.g., send-notification) must declare `preview_supported=False`; gate then requires two-person approval instead of one.
- **Reconsider when**: never — always prefer native mechanisms.

## 10c. Blast radius: coarse tiering, not per-service policy

- **Decision**: three tiers — `single-service`, `multi-service`, `cross-cluster`. Tier determined by tool + arguments. Approval requirement scales with tier.
- **Alternatives**:
  - (A) OPA / Cedar policy engine per service.
  - (B) No blast-radius concept — all WRITE treated equally.
- **Why**: per-service policy is Tier 3 territory (needs organizational buy-in for policy authoring). Three-tier is enough for POC and demonstrates the concept.
- **Cost**: coarse tier occasionally over- or under-gates; edge cases handled by manual override with audit.
- **Reconsider when**: real deployment with heterogeneous services requiring per-service rules.

## 11. Prompt versioning: git + prompt IDs, not a prompt CMS

- **Decision**: prompts live in `prompts/*.jinja2` files, each with a `prompt_id` and `version` in front-matter. Langfuse tracks which version served each request.
- **Alternatives**:
  - (A) Langfuse Prompts (their managed prompt registry).
  - (B) PromptLayer / Humanloop.
  - (C) Prompts hardcoded in Python strings.
- **Why**: git is version control, and prompts are code. External prompt CMS makes rollback ambiguous (which git commit was live when?). Langfuse *tracks* which version was used, but authoritative source is git.
- **Cost**: prompt edits require a deploy, not a UI edit; PMs can't tweak prompts directly.
- **Reconsider when**: non-engineers need to edit prompts (real product), or A/B experiments need instant rollout without deploy.

## 12. Eval infrastructure: nightly regression + LLM judge, not on every commit

- **Decision**: nightly cron runs the full golden set (30-50 cases) via GitHub Actions; LLM judge (Opus judging Sonnet) scores each. PR-level eval runs only the "smoke set" (5 cases).
- **Alternatives**:
  - (A) Full eval on every commit.
  - (B) Eval only when manually triggered.
- **Why**: full eval takes 15-20 min and costs ~$2 per run. Nightly catches regressions before they compound; smoke set catches obvious breakage per PR.
- **Cost**: a regression can land in main and sit up to 24h before being caught by nightly.
- **Reconsider when**: prompt/model changes become >1/day.

## 13. Observability: Langfuse + Grafana Cloud, not self-hosted

- **Decision**: Langfuse Cloud for LLM traces + prompt/eval registry; Grafana Cloud free tier for agent metrics.
- **Alternatives**:
  - (A) Self-host Langfuse.
  - (B) Only structured logs, no dedicated LLM observability.
  - (C) Arize / Weights & Biases.
- **Why**: cloud free tiers cover POC volume. Self-hosting adds a datastore and a service we don't need until multi-tenant or data residency matters.
- **Cost**: prompt/trace data leaves our environment; not usable if we're targeting regulated industries as customers.
- **Reconsider when**: any real customer data flows through (must self-host for compliance).

## 14. Message construction: in-band XML-tag data isolation

- **Decision**: untrusted content (log lines, alert labels, metric labels, memory items) wrapped in `<untrusted_data source="...">` tags in the user message; system prompt tells the model to never follow instructions inside those tags.
- **Alternatives**:
  - (A) Out-of-band structured channel — like `tool_use` for data (does not exist in any SDK today).
  - (B) Markdown fences (`\`\`\``) or `---` separators.
  - (C) No delimiter, trust the LLM to sort it out.
- **Why**: (A) doesn't exist. (B) is easily broken by the same delimiters appearing in real logs. (C) is prompt injection paradise. XML tags are respected by Claude and GPT families, and the fake-closing-tag attack is defeated by escaping `<` in untrusted content.
- **Cost**: cannot fully eliminate semantic-level injection ("the correct root cause is X" wearing an innocent disguise); handled by Layer 3 (second-model review), not Layer 1.
- **Reconsider when**: an SDK-native untrusted-content channel becomes available (Anthropic or OpenAI ships one), or when we can afford the latency of two round-trips (one to extract facts, one to reason).

## 15. Second-model review: different-family reviewer

- **Decision**: a second, different-family LLM reviews the primary agent's proposed actions before execution. Reviewer sees only structured proposal + minimal summary, not raw untrusted data.
- **Alternatives**:
  - (A) Same-family reviewer (e.g., Claude Haiku reviewing Claude Sonnet).
  - (B) No reviewer — rely on gate alone.
  - (C) Rule-based classifier (no LLM in review).
- **Why**: different family defeats family-specific injection payloads. Rule-based misses semantic drift. Gate alone catches only unauthorized WRITE, not falsified reports that manipulate human approval.
- **Cost**: extra ~$0.005 per incident (small model, small context); latency +2-3s; adds a second LLM vendor to the dependency graph.
- **Reconsider when**: adversarial eval shows reviewer bypass rate > 5% (rotate reviewer model), or cross-family agreement is too low, causing false-block rate to hurt UX.

## 16. Sandbox: intentionally NOT implemented in Tier 1.5

- **Decision**: no tool-execution sandbox in the design. All tools have typed structured parameters; no LLM-generated code / query strings are ever executed by our system.
- **Alternatives**:
  - (A) E2B / Modal managed sandbox for a `run_query` tool.
  - (B) Docker container isolation for a `run_promql` tool.
  - (C) MicroVM (Firecracker) for a `run_python_snippet` tool.
- **Why**: sandbox defends against **untrusted code execution**. Our attack surface is **untrusted content interpretation**, handled by the five-layer defense in [SECURITY.md](./SECURITY.md). Adding sandbox without a code-executing tool is complexity without payoff.
- **Cost**: agent capability is bounded by the pre-defined tool set; cannot handle "run this arbitrary PromQL I just came up with" scenarios without a new tool + sandbox.
- **Reconsider when**: introducing any tool where an LLM-generated string is executed by an external engine — `run_promql`, `run_kubectl_read`, `run_python_snippet`. At that point, sandbox is required; the five-layer defense continues to apply on top.

## 17. Execution unit: incident, not conversation

- **Decision**: each agent run is a **single-shot execution** over one incident. No multi-turn dialogue with the operator during the run. Operator's only interaction point is the approval gate.
- **Alternatives**:
  - (A) Multi-turn chatbot: operator asks follow-up questions, agent replies, iterate.
  - (B) Notebook-style: agent shows partial results, operator directs next step.
- **Why**: incident response has a definite goal (find root cause, propose action) with strict latency pressure. Multi-turn adds decision points where a tired 3am operator is a worse driver than the LLM's own graph. Approval-gate is where the human belongs, not "should I fetch the logs now?".
- **Cost**: no "why did you do X?" mid-run — operator can only see the final report and the Langfuse trace. Interactive debugging happens post-incident.
- **Reconsider when**: use case shifts from incident response to interactive investigation (e.g., "help me understand this weird metric pattern"), or when operators consistently want to steer mid-run.

### Consequences for standard chatbot metrics

- **Multi-turn task completion rate**: not directly applicable — our unit is `incident_resolved_correctly` (LLM-judge score ≥ 4), not `conversation_completed`.
- **Sliding window / conversation memory**: not applicable — see [ARCHITECTURE.md](./ARCHITECTURE.md) §6 memory layering. The Temporal workflow state IS the "working memory" and has an event-history durability model, not a token-window one.
- **Turn-level satisfaction**: replaced by per-phase eval assertions (see [EVAL.md](./EVAL.md)). Each phase has its own quality signal, which is *finer-grained* than turn-level user rating.

## 18. Log storage: ClickHouse over Loki

- **Decision**: mock environment logs land in ClickHouse via Vector (docker log source → JSON parse → shape → HTTP JSONEachRow insert). The MCP `query_logs` tool will be a SQL query, not LogQL.
- **Alternatives**:
  - (A) Loki + Promtail — textbook Prometheus-ecosystem choice, label-based indexing.
  - (B) Elasticsearch — full-text inverted index, heavy on storage.
  - (C) Both Loki and ClickHouse in parallel — max flexibility, max complexity.
- **Why**: Loki's label-index + chunk-scan model degrades once service count grows past ~100; label cardinality explodes and chunk scans become linear. Personal production experience already saw Loki queries hang at that scale, with the fix being a migration to ClickHouse. ClickHouse is column-store + primary-key + skip-indexes; query p99 stays predictable as service count grows. Bonus: SQL is universal, agent tool logic transfers to any future employer's log pipeline; LogQL is Grafana-ecosystem-only.
- **Cost**: schema design work up-front (`init.sql` with `LowCardinality`, `PARTITION BY toDate(ts)`, `ORDER BY (service, level, ts)`, TTL); Vector pipeline more configured than Promtail; loses parity with Prometheus's label model, so cross-signal joins (metric → log) need a query, not a Grafana click.
- **Reconsider when**: service count stays under ~50 and team lacks CH ops experience — Loki's operational simplicity wins at small scale.

**Sub-decisions this drives**:
- **Timestamp normalization at ingestion, not at source**: services emit ISO 8601 with trailing `Z`; ClickHouse's DateTime64 parser rejects `Z`. Vector's shape transform strips `T` → space and drops `Z`. Reason: don't couple service log format to storage layer's parsing quirks.
- **`extra String` as JSON catch-all**: business-event fields (`order_id`, `reason`) whitelisted into `extra` as encoded JSON rather than adding columns per field. Reason: schema evolution stays free; can still `JSONExtractString(extra, 'order_id')` for queries.

## 19. Frontend: none for POC (Slack + CLI only)

- **Decision**: incident reports post to Slack via webhook and print to CLI. No web UI.
- **Alternatives**:
  - (A) Next.js dashboard showing incident list + trace replay.
  - (B) Streamlit for a quick internal UI.
- **Why**: on-call people live in Slack. Building a UI competes with building the agent. If a demo needs visualization, Langfuse's trace UI is already there for free.
- **Cost**: less impressive demo video; harder for a non-technical viewer to "see" the agent.
- **Reconsider when**: shipping to non-engineering users, or when trace replay UX becomes a differentiator.

## 20. Middleware-specific knowledge lives in RAG, not in agent code or cases

- **Decision**: Three strict layers of specificity:
  - **Agent code** — 100% middleware-agnostic. Tools are `query_metrics`, `query_logs`, `get_service_topology`, `list_recent_deploys`. No Redis-specific / Postgres-specific / Kafka-specific branches anywhere in the graph.
  - **Golden cases** — named by **diagnostic pattern**, not by middleware. Six categories: `LOAD`, `CHANGE`, `DEP`, `RES`, `DATA`, `ENV`. A specific incident (e.g., Redis bgsave OOM) is an *instance* of the `RES` pattern, not its own category.
  - **Runbooks (RAG)** — the only place middleware-specific knowledge lives. "How to triage a Redis OOM", "Kafka consumer lag playbook", "Postgres slow query checklist" — each a document, retrieved on demand at investigation time.
- **Alternatives**:
  - (A) Middleware-specific tools per vendor (e.g. `redis_check_memory`, `kafka_check_lag`). This is HolmesGPT's `toolsets/` shape.
  - (B) Middleware-specific cases (e.g. `GS-REDIS-*`, `GS-POSTGRES-*`).
  - (C) Hard-code middleware handling in the agent's prompt library.
- **Why**: A new middleware in production must not require a new case category, a new tool, or a code change. Adding Kafka to the fleet is "index the Kafka runbook + add exporter to Prometheus" — nothing else. This is how any team with a heterogeneous stack can adopt the agent without a per-vendor migration project. It's also why the incident taxonomy (six patterns) is stable: production incidents *are* one of these six shapes regardless of which technology is failing.
- **Cost**: Loses the immediate legibility of tech-specific case names ("Redis OOM" → "resource exhaustion in a downstream dep"). Users have to learn the six patterns. Also puts more pressure on RAG quality — bad runbooks mean the agent won't know how to interrogate a specific technology.
- **Reconsider when**: The agent needs to *take actions* on a middleware (not just diagnose) — writing to a Redis cache, restarting a Kafka broker, etc. Actions may warrant vendor-specific tools with proper auth/idempotency semantics.

## 21. Alert on SLO violations only; infra signals are query targets, not pages

- **Decision**: Alerting rules fire on **user-facing SLO breaches** (5xx rate, latency, downstream failure rate). Infra metrics (`redis_memory_used_bytes`, `pg_stat_activity_count`, `jvm_gc_pause_seconds`) are scraped, exposed in dashboards, and available for the agent to query — but do **not** page on-call.
- **Alternatives**:
  - (A) Layered alerting: both infra and business layers page, routed to different teams. "Redis Memory Pressure" (platform) + "Auth Login Failure" (auth team) both fire on the same underlying incident.
  - (B) Infra-only alerts (unusual, but seen in ops-heavy orgs).
- **Why**: At 100+ services with 30% using Redis (a normal mature-org shape), infra-layer alerts cause a page storm on every incident — Redis blips → 30 infra alerts fire → on-call is overwhelmed → real alerts get missed. Google SRE Book Chapter 6 recommends SLO-only alerting for the same reason. The infra signals still matter, but they are the *evidence the agent gathers during RCA*, not the trigger for human wake-up. Alert count should reflect "events requiring immediate human action", not "monitoring points instrumented".
- **Cost**: Loses the leading-indicator benefit of infra alerts (memory pressure warning *before* users see errors). Compensation: dashboards + prediction models in the observability layer can page pre-emptively when the risk model triggers — but that's a different decision point (risk-based paging) rather than metric-threshold paging.
- **Reconsider when**: The organization has separate platform vs product on-call rotations *and* platform owns latency SLOs for the middleware layer independently of business services. In that setup, layered alerts route to the right team without contributing to a single-team page storm.

## 22. Agent architecture: agent loop over workflow graph (supersedes §1)

- **Decision**: A single `while` loop where the LLM decides each next action (tool call or final report). No LangGraph, no fixed phase sequence, no orchestration framework beyond the Anthropic client SDK. `messages` array is the state; `stop_reason` drives loop termination.
- **Alternatives**:
  - (A) LangGraph 5-node state machine — the original §1 decision.
  - (B) Hand-written `Orchestrator` class with 5 phase methods — workflow-thinking minus the framework.
  - (C) Temporal-native — workflow body IS the orchestration; each activity a phase.
- **Why**:
  1. **Mainstream shape**: Claude Code, Cursor, Devin, and OpenAI Assistants all use a single tool-use loop. Anthropic's own "Building Effective Agents" post distinguishes *workflows* (deterministic paths, LangGraph territory) from *agents* (LLM-driven, loop territory). SRE root cause analysis is the latter — which tool to call next depends on evidence already gathered, and is not enumerable in advance.
  2. **Framework churn**: LangGraph 0.0.x → 0.1 → 0.2 → 0.3 have all been breaking; a 6-month upgrade toll on a portfolio project meant to survive is not worth the abstraction it buys us.
  3. **Double orchestration**: The original plan pairs LangGraph with Temporal. This duplicates state (Temporal workflow vars + LangGraph channels) and retry semantics (Temporal activity retry + LangGraph node retry). Loop pattern collapses this to one layer.
  4. **Streaming**: `while` naturally `yield`s events per LLM chunk / tool call. LangGraph's `.astream()` schema adds an abstraction layer between us and Anthropic's SSE.
  5. **State simplicity**: `messages` IS state. No `TypedDict` channel schema, no reducer functions, no `add_conditional_edges` for what is a ~30-line while.
- **Cost**:
  - Lose LangGraph's declarative visualization (`get_graph().draw_mermaid()`) — replaced with a hand-drawn diagram in ARCHITECTURE.md.
  - Lose `interrupt()` primitive for human-in-the-loop — Week 5 L4 gate implements approval via structured `tool_use` + external API instead.
  - Lose community pattern library (memory savers, checkpointers) — none of them apply to our shape anyway.
  - "Not using LangGraph" needs to be explained in interviews. This is actually a *signal* if articulated well (candidates who used LangGraph without evaluating it can't; candidates who chose to skip it need to defend the choice, which forces clarity).
- **Reconsider when**:
  - Adding ≥3 concurrent independent branches that need to merge state — that is where LangGraph's channel reducers earn their keep.
  - Tool-use loop primitives in the Anthropic SDK become inadequate (e.g., no way to express the branching we need).
  - Building a *workflow* product (deterministic steps with LLM-in-the-middle), not an *agent*.

**Related revisions**:
- **§1** (workflow-skeleton with agentic sub-nodes): **superseded**. Original entry preserved as history at the top of this file. `experiment/langgraph` branch preserves the pre-pivot HEAD.
- **§2** (Temporal for durable execution): **partial revision** — Temporal deferred out of Week 2. With loop-based agent, `messages` + JSONL append log provides sufficient short-horizon durability for the 5-15 min incident window; process death within that window is a manageable risk. Temporal returns Week 6-7 if production-grade durability becomes a shipping requirement.
- **Phase concept** (`triage → collect → hypothesize → verify → report`): dropped. Mature agent architecture is LLM-driven ordering; phases were workflow-thinking residue. Model routing (§5) is affected — no phases to route by; Week 2 uses single-model (Sonnet); cost optimization returns Week 3+ if measurement justifies it.

---

## 23. Harness: deterministic pipeline around the agent loop (refines §22)

- **Decision**: the system is a **linear pipeline containing a ReAct loop**. Six steps, fixed order, plain Python: `① route → ② preprocess → ③ loadout → ④ loop → ⑤ parse → ⑥ fanout`. Only step ④ is non-deterministic. Steps ①②⑥ vary by trigger type; ③④⑤ are identical across alert / chat / patrol.
- **Alternatives**:
  - (A) Pure loop — the alert goes straight into the loop and the LLM does everything, including dedup and notification, via tools.
  - (B) Steps ①-③ as tools the LLM calls.
  - (C) Back to a graph, with ④ as one node among six.
- **Why**:
  1. **§22 was half an answer.** It correctly argued that root cause analysis is agent-shaped, then implied the *whole system* is a bare loop. It isn't. Deduplication, severity mapping, tool loadout, and notification routing are all enumerable in advance — which is the exact definition of workflow territory in Anthropic's taxonomy. The correct description is *a workflow containing an agent*, which is also the standard production shape.
  2. **An LLM doing dedup is worse and more expensive** than a Redis key lookup. Handing a deterministic task to a model costs money and loses auditability.
  3. **The shell is where the cage is built.** The LLM decides *what to query next*; the code decides *what it may query, for how long, over which time window, in what output shape, and who receives the result*. Freedom of ordering without freedom over resources or contracts.
  4. **Testability.** ①②③⑤⑥ are ordinary functions — unit-testable, breakpoint-able, runnable without an LLM. Under (A) or (C) every one of those becomes an LLM call or a graph node.
- **Shape, precisely**: the shell is a *linear pipeline*, not a DAG. It is technically a degenerate DAG (a single chain), but calling it a DAG misleads — a DAG's value is expressing branches and joins, and there are none. The only real fan-out/fan-in is patrol's outer `asyncio.gather` over N investigations. The kernel is **structured ReAct**: Thought is an optional assistant text block, Action is a `tool_use` block, Observation is a `tool_result` block — the same idea as the 2022 ReAct paper, but with the API guaranteeing structure instead of a prompt convention being parsed. Plus recursive sub-agents (the refute loop). Full description: **linear pipeline shell + ReAct kernel + recursive sub-agents.**
- **Cost**: two places to look when debugging. Harness steps cannot adapt to evidence, by construction.
- **Reconsider when**: a shell step starts needing evidence from tool calls to decide what to do — that step belongs inside the loop, not in the shell.

## 24. Integrations are configuration, not code

- **Decision**: an integration is a **YAML file plus an MCP server**. Zero Python per integration. The file declares `match` rules, the `mcp` server command, `runbook_namespace`, `prompt_fragment`, `notifier` list, and a declarative `inbound_mapping` (jsonpath field mapping).
- **Alternatives**:
  - (A) An `Integration` Python Protocol with ~5 methods per integration (the first proposal).
  - (B) One flat tool registry — every tool always available to every incident.
  - (C) `if/elif` on alert source inside the harness.
  - (D) A separate agent deployment per integration.
- **Why**:
  - (B) breaks around 30 tools: schema bloat inflates every request, and tool-selection accuracy degrades. It also leaks a `jira` tool into a Kafka incident, where it is noise. A per-integration bundle keeps the schema list at roughly 6-10 tools per investigation.
  - (C) is the pattern that always rots.
  - (A) works but makes every integration a code review. Config makes the abstraction cost **zero lines of Python**, which is a far stronger claim and a directly measurable one (Week 5 L7).
  - This is [§20](#20-middleware-specific-knowledge-lives-in-rag-not-in-agent-code-or-cases) finally becoming code rather than a documented intention.
- **Cost**: inbound webhook normalization resists pure config, because every vendor's payload shape differs. Declarative jsonpath mapping covers the common flat-JSON webhook; an optional Python normalizer is the escape hatch for genuinely malformed sources. An investigation needing tools from two integrations requires an explicit compose step.
- **Reconsider when**: tools per integration exceeds ~10 (then intra-bundle selection or tool search is needed), or when a third integration needs the Python escape hatch (that's the signal the mapping DSL is too weak).

## 25. Trigger registry: alert is one entry mode of three

- **Decision**: a trigger registry with per-trigger pre-processors plugging into harness step ②. Three modes: `alert` (webhook, real), `chat` (interactive, stub in Week 2), `patrol` (scheduled, stub in Week 2). All three normalize to the same `Investigation`; the central noun is *investigation*, not *incident*.
- **Alternatives**:
  - (A) Alert-only now, generalize later.
  - (B) Three separate applications sharing a library.
- **Why**: (A) is the tempting one and it is a trap, because two consequences of chat and patrol are cheap now and invasive later:
  - **Chat requires multi-turn**, so `messages` must belong to a persistable `Investigation` rather than being a local variable discarded at loop exit. The same seam gives JSONL durability and a future Temporal resume for free.
  - **Chat requires streaming**, so the loop must yield events rather than return a string. [ARCHITECTURE.md](./ARCHITECTURE.md) already claims "`while` naturally yields events" as an advantage of loop over graph — an advantage the current string-returning code does not actually deliver.
  - **Patrol requires per-investigation budgets**, because it fans out over N targets that each need independent accounting and independent degradation.
  - Human-in-the-loop (see [§27](#27-human-in-the-loop-non-blocking-by-default)) also lands on this same seam at zero extra cost.
- **Cost**: `Investigation` is a real abstraction where `alert: dict` was free. Two stub triggers exist in Week 2 that do nothing useful.
- **Reconsider when**: nothing foreseeable. If chat and patrol were cancelled the abstraction would still be earned by durability and HITL alone.

## 26. k8s integration: a config-only seam, no k3d cluster

> **Reversed 2026-08-01, days after being decided.** The original entry (a k3d sidecar cluster with 2-3 deliberately broken pods) is below for the record. A capacity review put every proposed addition against a mandatory cut list, and k3d was the only item that was simultaneously expensive and weak as evidence.

- **Decision**: k8s is registered as a **configuration-only integration** — a YAML file with `match` rules and a declared MCP server, and no running cluster. The claim the integration layer needs to substantiate ("adding an integration costs zero lines of Python") is proven instead by a second *live* config-only integration on one container, not by a second infrastructure plane.
- **Alternatives**:
  - (A) k3d single-node sidecar cluster with broken pods — the previous decision.
  - (B) Migrate the 7 mock microservices onto k3d.
  - (C) Mock a kube-apiserver in FastAPI.
- **Why**:
  1. **The evidence per day is poor.** Three deliberately-broken pods do not demonstrate operating Kubernetes. An interviewer who cares about k8s will get past that in two questions; the same days spent on distributed-trace attribution, a written diagnostic methodology, and a deterministic precompute layer hold up much longer.
  2. **It buys the budget for the P0 items.** Traces ([§29](#29-trace-scope-per-request-causal-chains-not-a-third-pillar)), the methodology document, and the precompute layer ([§28](#28-precompute-produces-a-shortlist-never-a-conclusion)) total roughly the same number of days. Something had to pay for them, and every other candidate cut cost more evidence than this one.
  3. **The abstraction claim does not need k8s.** It needs a *second integration added without touching Python*, and any real backend serves — a Kafka broker is one container against k3d's whole control plane.
- **Cost**: no k8s-shaped signals in the golden set, so pod-lifecycle failure modes (OOMKilled, ImagePullBackOff, crashloop-with-no-deploy) go untested. Note that `GS-P-CRASHLOOP-001` already covers container restart at the docker level, which is the same *diagnostic pattern* under a different middleware — which is precisely the [§20](#20-middleware-specific-knowledge-lives-in-rag-not-in-agent-code-or-cases) thesis.
- **Reconsider when** — and this is unusually cheap to reverse, which is the payoff of [§24](#24-integrations-are-configuration-not-code):
  - the reversal is one `config/integrations/k8s.yaml`, one `mcp_servers/k8s/server.py`, and a k3d compose file. **Zero changes to `agent/core/`.** The expensive part is authoring golden cases for it, not the wiring.
  - flip it if: a target role's interview loop centres on Kubernetes; or eval shows the agent failing a class of failure that only pod-lifecycle signals explain; or the project extends past 7 weeks and the marginal day is cheap again.

<details>
<summary>Original decision (superseded the same week)</summary>

- **Decision**: keep Week 1's docker-compose untouched; add a k3d single-node cluster alongside it containing 2-3 deliberately broken pods, with `kube-state-metrics` scraped by the existing Prometheus.
- **Why**: migrating the mock fleet onto k3d would rewrite Week 1's fault injection and `case_runner.py` for zero diagnostic gain; the integration needs k8s-shaped signals, not microservices happening to run under k8s. Mocking a kube-apiserver would violate the real-components principle.
- **Cost**: two planes to stage; `case_runner.py` grows a second setup path.

</details>

## 27b. Golden set target: 30 cases, not 55

- **Decision**: ~30 cases — 20 ordinary (spread across easy / medium / hard / pathological) plus 10 adversarial.
- **Why**: the 10 adversarial cases are load-bearing because [EVAL.md](./EVAL.md)'s per-layer bypass rate needs roughly two per defence layer; they stay. The ordinary count was set at 45 before anyone had authored one against real fault injection, and authoring against a real stack costs several times what writing a fixture costs. Twenty is enough for a per-difficulty breakdown and for regression detection, which is what the set is *for*.
- **Cost**: weaker statistics per difficulty bucket — a single case flipping moves a bucket mean noticeably. Reported as a caveat rather than hidden.
- **Reconsider when**: bucket variance makes a regression signal unreadable, which is a measurable condition, not a guess.

## 27. Human-in-the-loop: non-blocking by default

- **Decision**: asking a human is a **tool** (`ask_human`), not a special loop state. Default semantics are **non-blocking**: the agent records the open question in its report and continues under an explicitly stated assumption. Blocking happens in exactly two cases — chat mode (a human is present by definition) and WRITE-tool approval (blocking *is* the point, see [SECURITY.md](./SECURITY.md) L4).
- **Alternatives**:
  - (A) Always block on clarification, as coding agents do.
  - (B) Never ask; force the model to guess silently.
- **Why**: **at 3am nobody answers.** An alert-triggered investigation that blocks indefinitely on a human produces nothing, which is strictly worse than a report that says *"I need to know X; assuming X=A, the conclusion is Y."* This is the single biggest behavioral difference from a coding agent, where the human is always present. The output framing is also more useful operationally: a report carrying explicit open questions hands off across a shift; a suspended investigation does not.
- **Implementation consequence**: **zero loop changes.** It is a tool whose result arrives from a different source depending on trigger — the next user message in chat, a Slack thread reply with a timeout in alert mode, never in patrol. This works only because `Investigation` is persistable and the loop yields events ([§25](#25-trigger-registry-alert-is-one-entry-mode-of-three)), which is independent confirmation that the Week 2 L2 refactor is correctly scoped.
- **Cost**: the report schema needs `open_questions` and `assumptions` fields, and eval needs a metric for whether stated assumptions were reasonable. A non-blocking agent can also proceed confidently down a wrong assumption — which is what the refute sub-loop exists to catch.
- **Reconsider when**: a WRITE tool exists whose blast radius makes "proceed under assumption" unacceptable even for reads. That's a gate policy question, not a loop question.

## 28. Precompute produces a shortlist, never a conclusion

- **Decision**: the harness computes a set of deterministic facts before the model is invoked — a merged timeline (alert firing, metric onset, log-error onset, deploys in window), a topology graph derived from existing client-side downstream metrics, the blast radius, and a **ranked shortlist of candidate root-cause services**. The model receives the shortlist and the timeline; it does not receive a proposed answer, and it is free to reject the ranking.
- **Alternatives**:
  - (A) No precompute — the model discovers topology and timelines through tool calls.
  - (B) Precompute a conclusion and have the model write it up.
- **Why (A) fails**: a real agentic RCA loop with no precomputation runs 30-100 tool calls, because the model rediscovers the service graph and the change timeline from scratch every incident. Those are deterministic joins over data we already have. Leaving them to the model is the reason unassisted agentic RCA does not scale, and it is also why this project's original "median tool calls < 12" target was fiction — see [§30](#30-unmeasured-targets-are-labelled-hypotheses).
- **Why (B) is the actual danger**: if the ranking is good enough, the model becomes an expensive narrator for a heuristic, and **accuracy metrics will look excellent while the LLM contributes nothing**. This failure is invisible to every metric in EVAL.md as originally written. Two guards:
  1. **`precompute_override_rate`** — the fraction of investigations whose final root cause is *not* the top-ranked candidate. Near zero means we built a ranking algorithm with a commentary track, and the honest response is to say so.
  2. The golden set must contain cases where the correct answer is **not** the top candidate. `GS-LOAD-001` is one by construction — the loudest signal is a 10x traffic rise and the real cause is a retry misconfiguration — which is a second reason to pull it out of `eval/backlog/`.
- **Cost**: a new deterministic component to maintain and test; a precompute bug now looks like a reasoning failure until you check the shortlist. Mitigated by logging the shortlist into the trace so the two are separable.
- **Reconsider when**: `precompute_override_rate` stays above ~40%, which would mean the ranking is noise and the model is doing the work anyway — then simplify to timeline plus topology and drop the scoring.
- **Consistent with [§23](#23-harness-deterministic-pipeline-around-the-agent-loop-refines-22)**: enumerable work belongs in the shell. A topology join is enumerable. "Which of these three candidates actually explains the log lines" is not.

## 29. Trace scope: per-request causal chains, not "a third pillar"

- **Decision**: add OTel tracing and a Tempo backend, expose one tool — `query_traces` — that returns **edge-level aggregates and, on explicit `trace_id` drill-down, a single request's span tree**. Never raw span dumps. Scoped initially to the latency and cascade cases, and kept only if it earns its keep by a measured margin.
- **Alternatives**:
  - (A) No traces (the Week 1 position).
  - (B) Full trace pillar with span-level querying available generally.
- **Why the framing matters**: the tempting argument is "the mock has metrics and logs, so a third of observability is missing." That overstates it. Week 1 L5 already emits client-side `downstream_requests_total{service,downstream,outcome}`, so **per-edge RED is already available from Prometheus** and the topology graph can be derived from it without any tracing backend. What traces genuinely add is narrower and real: *where the time went inside one slow request*, and edges nobody declared.
- **Why (B) fails**: a span dump is the single fastest way to destroy a context window, and an agent that pages through spans burns its budget on data it cannot summarise. Aggregates plus targeted drill-down is the only shape that fits inside a token budget.
- **Acceptance number** — this decision is falsifiable rather than assumed: accuracy delta on `GS-P-NETWORK-DELAY-001`, `GS-P-IO-LATENCY-001`, and `GS-P-DEPENDENCY-DOWN-001` with `query_traces` available versus withheld. Following [RAG.md](./RAG.md)'s own rule that an ablation row showing under 2% gain does not ship: **if the delta is negligible, we report that edge-level metrics were sufficient and remove the tool.** A negative result here is a finding, not a failure.
- **Cost**: OTel instrumentation across 7 services plus a Tempo container; one more thing that can be down during an incident (which the partial-observability work in W3 L8 then has to handle, so it is not purely cost).
- **Reconsider when**: covered by the acceptance number above.

## 30. Unmeasured targets are labelled hypotheses

- **Decision**: any number in the docs that has not been measured is written as a **hypothesis with the lesson that will test it**, not as a target. Specifically `median tool calls < 12`, `p99 cost < $0.40`, and `median investigation 60-90s` were all authored before a single loop had run.
- **Why**: the project's stated acceptance rule is "every component ships with a number." A target invented in advance and then quietly met by adjusting the target is the exact failure that rule exists to prevent. If the real figure is 40 tool calls, the honest move is to publish 40, explain why, and let the precompute layer ([§28](#28-precompute-produces-a-shortlist-never-a-conclusion)) attack it — not to relabel 40 as acceptable.
- **Cost**: the docs read less confidently. That is the correct amount of confidence for an unmeasured quantity.
- **Reconsider when**: never — this is a documentation discipline, not a design choice.

## 31. Patrol stays a stub until its value proposition is settled

- **Decision**: the `patrol` trigger keeps its seam and its stub; no real implementation until there is an answer to "what does patrol find that alerting does not?"
- **Why**: a scheduled run of the diagnostic loop is just slower alerting. The differentiator would be finding what never crosses a threshold — slow degradation, config drift, capacity trend — and that implies *different tools* (trend, diff-over-time) and a *different output shape* (a digest, not a root-cause report). Building the trigger before settling that produces a feature that duplicates alerting.
- **What the stub still buys**: it forced `Investigation` to carry a per-investigation budget and forced the loop to yield events, both of which landed in W2 L2 and are load-bearing for chat too. The seam earned its keep before the implementation existed.
- **Reconsider when**: the question above has a written answer with at least one golden case that alerting provably cannot catch.

## 32. Temporal is out of scope, not deferred

- **Decision**: Temporal leaves the plan. Per-investigation JSONL append logs are the Tier 1.5 durability answer, and Temporal is documented as a Tier 2 seam with a migration story.
- **Why**: it has been "deferred one more phase" three times (W2 → W5-6 → W6-7), which is how scope pretends to be planned. The honest position: for a 5-15 minute investigation window, a replayable append log is adequate, and `Investigation` owning `messages` ([§25](#25-trigger-registry-alert-is-one-entry-mode-of-three)) is what keeps the migration mechanical if it is ever needed.
- **Cost**: no crash-resume demo, and no signal-based human approval primitive — so W6's gate uses a structured tool call plus an external API instead, as [§22](#22-agent-architecture-agent-loop-over-workflow-graph) already anticipated.
- **Reconsider when**: p99 investigation duration exceeds ~30 minutes, or a human approval step needs to survive a process restart.

## 33. Gateway layering: four layers plus three cross-cutting decorators

- **Decision**: the gateway is decomposed into **routing → construction → transport**, with a **shared** codec layer used by all of them, and three cross-cutting concerns implemented as decorators rather than as a further layer: **response cache**, **budget gate**, **tracing**.

```
        ┌──────────── tracing (wraps everything) ─────────────┐
        │  ┌────────── response cache (wraps transport) ────┐ │
routing ─→ construction ─→ │ budget gate ─→ transport │ ─→ parse │
        │  └────────────────────────────────────────────────┘ │
        └── shared: domain types + per-provider codec ────────┘
             (used outbound by construction, inbound by parse)
```

- **Origin**: proposed as four layers — routing (pick the model by capability), construction (provider config, parameterised), transport (cost accounting, rate limiting, retry, error classification and the action each error implies), shared (message-format compatibility). That decomposition was adopted; the record below is the set of deltas applied to it, kept because each one is a decision someone will otherwise re-litigate.

| # | Delta from the original proposal | Reasoning |
|---|---|---|
| 1 | **Shared is cross-cutting, not the bottom layer.** | Message translation runs twice per call — outbound when construction builds the request, inbound when the response is parsed. It is a bidirectional codec, not a stage. Listing it fourth invites reading it as "beneath transport", which would put response parsing in the wrong place. |
| 2 | **Cache, budget gate and tracing are decorators, not a fifth layer.** | A cache *hit* means transport never runs, so the cache cannot be a stage inside transport; it wraps it. Same for tracing (spans the whole call) and the budget gate (a guard immediately before transport). |
| 3 | **The budget gate sits *after* the cache lookup, and a cache hit still charges the budget.** | A cached call costs no money, so gating before the cache would refuse free calls. But if cache hits were also free of *budget*, a run that degraded on budget exhaustion would stop degrading on rerun — breaking [EVAL.md](./EVAL.md) reproducibility. The resolution: the cache entry stores the original call's usage and cost, and a hit replays that charge. Money is genuinely saved; the ledger stays faithful; budget-driven degradation reproduces. Two numbers are therefore reported, `money_spent_usd` (excludes hits) and `budget_charged_usd` (includes them). |
| 4 | **`cache_control` breakpoint placement is construction's job.** | The original construction layer was scoped to "register provider config", which left the single highest-leverage cost item in [§3](#3-llm-gateway-in-process-wrapper-not-litellmportkey-service) — 60-70% of input tokens — with no owner. Construction owns *request assembly*, of which breakpoint placement is part. Note the provider asymmetry: Anthropic needs explicit markers, DeepSeek caches its prefix automatically, and **prompt-layer ordering pays off on both** — only one needs the annotation. |
| 5 | **Streaming belongs in transport, and it is not optional.** | Absent from the original layer list. L2 deliberately shaped `TextDelta` as a delta so the gateway could stream later; without it the two-stage output (W3 L8) and chat mode cannot exist. |
| 6 | **Error classification is the *precondition* for the circuit breaker, not a sibling feature.** | The proposal was "open the breaker after 3 failed retries". But a malformed request fails three times while the provider is perfectly healthy — that would open the breaker and force an unnecessary fallback. Only *retryable* classes count toward the breaker, and the threshold is **3 consecutive retryable failures**, not "3 retries of one call". Detection is provider-specific and lives in the adapter; policy is provider-agnostic and lives in transport. |
| 7 | **429 retries but never counts toward the breaker.** | Rate limiting is a quota condition, not an outage. Opening the breaker on 429 would fall back to another provider and thereby *hide* a quota misconfiguration. Retries exhaust, the call fails, and the failure is visible. |
| 8 | **SDK-level retries are switched off (`max_retries=0`).** | Both the `anthropic` and `openai` SDKs retry by default. Layering our 3 attempts on top of theirs is 9 requests to a dying provider, and it makes the retry count unreadable. Owning it entirely makes it one legible number. |
| 9 | **Cross-provider fallback is disabled in eval.** | The most consequential delta. If a provider 529s mid-investigation and the run continues on another, that run's accuracy is attributable to neither model, its cost mixes two price sheets, and the eval matrix's `model_version` dimension becomes a lie. Mid-run switching is additionally risky because `messages` was built under the first model's tool-use idiosyncrasies. So: **fallback is a production feature**, eval pins the provider, and any real run that fell back is tagged and excluded from model comparison. |
| 10 | **Routing keys on a concrete `CallKind`, and the family-difference rule is validated at wiring time.** | "Select by capability need" needed a definition or it becomes taste. `CallKind ∈ {main_loop, refute, judge, reviewer, classify}` maps to hard requirements (tool-use support, minimum context, family) and soft preferences (tier, cost). For `judge` and `reviewer`, *family must differ from the agent's* — that is a correctness requirement from [EVAL.md](./EVAL.md) and [SECURITY.md](./SECURITY.md) L3, not a preference, so a configuration that violates it fails at construction rather than at the first judged run. |
| 11 | **Rate limiting is justified by eval throughput, not production load.** | At under 1 QPS in production it is nearly moot; running 30 golden cases concurrently will certainly hit 429. That reframing determines the implementation: a per-provider semaphore plus 429 backoff is sufficient, and a token bucket would be theatre. |
| 12 | **The gateway binds the investigation at construction instead of changing the `LLM` protocol.** | Cost attribution and budget enforcement need to know which investigation a call belongs to, but the protocol is `call(messages, tools)`. Rather than growing the signature — which would leak accounting into the kernel — `gateway.bind(inv, kind)` returns an object implementing `LLM`. The loop keeps seeing a plain `LLM` and the seam rule is untouched. |
| 13 | **`BudgetExceeded` and `ProviderUnavailable` live in `llm/protocol.py`.** | They are part of the protocol's contract rather than provider details, so `core/loop.py` may import them under the seam rule, and the loop converts them into `Aborted` events. This preserves the L2 invariant that every run emits exactly one `Done` or `Aborted`. Provider-specific errors never reach the loop. |

- **Cost**: more files than a single `gateway.py`. Accepted because the layer boundaries are the interview content, and because each layer is separately testable — the transport policy paths (retry, breaker, classification) are all exercised offline with an injected send function and no network.
- **Reconsider when**: a fifth genuine stage appears. Candidates that would qualify: request batching, or a semantic (rather than exact-hash) cache.

## 34. LiteLLM: not adopted, kept as a documented swap-in

- **Decision**: write the gateway; do not take LiteLLM. `LiteLLMAdapter` is left as a legal implementation of the existing `LLM` protocol, with a written threshold for when to switch.
- **Alternatives**:
  - (A) Adopt LiteLLM for the transport layer — it ships retry, cross-provider fallback, error normalisation, streaming normalisation, and a price map.
  - (B) Adopt the LiteLLM proxy as a separate service.
- **Why not**:
  1. **It replaces the ~40% of the transport layer that is plumbing, and none of the four responsibilities that carry this project's argument.** `cache_control` breakpoint placement, per-investigation budget with refuse-on-exceed, a response cache that replays cost for reproducibility, and routing by task nature are all still ours to write. The saving is roughly 150 lines of provider plumbing.
  2. **Its value scales with provider count, and ours is two adapter classes.** `OpenAICompatLLM` covers DeepSeek / Qwen / Kimi through `base_url` alone; Anthropic needs one native class. Importing a large, fast-moving dependency to save 150 lines is a poor trade at that scale.
  3. **Its price map is approximate and lags provider changes.** For a project whose stated thesis is measured cost, an approximate price table undermines the claim — we would be reconciling against provider usage reporting regardless.
- **Conceded in its favour**: normalising error taxonomies across providers is genuinely fiddly and LiteLLM has already done it. With two adapters and seven error classes it is about a day of work here — and that day produces the error-classification table, which is itself the artifact worth having.
- **Cost**: we own the maintenance when a provider changes its error shapes or adds a caching mechanism.
- **Reconsider when**: provider count passes ~6; or a non-standard endpoint (Azure, Vertex) is needed; or load balancing across multiple API keys per provider is needed. Because the `LLM` protocol already exists, the swap is one new file and a wiring change.

## 35. Price drift: freeze history, flag age, reconcile against billing

- **Decision**: three defences against a checked-in price table going wrong, layered by how much they cost to build.
  1. **History is frozen.** Every `CostEntry` stores the cost computed at call time plus the `price_table_version` that produced it, and a cache replay charges the *original* cost rather than recomputing. A rate change can therefore never rewrite a past number.
  2. **Age is a signal.** `Price.as_of` plus a 90-day window; past it, `Ledger.prices_stale()` reports through the same channel as `prices_verified: false`. Verified-when-written is not verified-now.
  3. **Reconciliation is the automatic detector.** Snapshot the provider's account balance, run the work, snapshot again; compare what was actually charged against what the table predicted. Divergence beyond tolerance means the table is wrong, and the ratio says roughly by how much.
- **Alternatives**:
  - (A) Treat prices as a constant and update them by hand when someone remembers.
  - (B) Use a maintained third-party price map (LiteLLM ships one).
  - (C) Read cost from the provider's response — not available: none of DeepSeek, OpenAI or Anthropic return a cost field.
- **Why**: (A) is the default and it fails silently, which is the worst failure mode for a project whose thesis is measured cost — a stale table produces confident wrong numbers indefinitely, and nothing looks broken. (B) trades our staleness for someone else's, which is better but still unverified locally, and is one of the arguments [§34](#34-litellm-not-adopted-kept-as-a-documented-swap-in) already weighed. Reconciliation is the only option that closes the loop without a human reading a web page.
- **Cost and limits, stated plainly**:
  - **Resolution floor.** DeepSeek reports balance to two decimals of CNY, roughly $0.0014, while a single smoke call costs about $0.00007 — three orders of magnitude below it. Reconciliation is a **batch** instrument: an eval run over ~30 cases lands in the $0.1-1 range and moves the balance measurably. Below `min_spend_usd` the verdict is `inconclusive` rather than a ratio, because reporting rounding error as a finding is worse than reporting nothing.
  - **Currency is a second error source.** The balance arrives in CNY while the ledger is USD, so a USD figure carries an FX assumption stacked on the rate itself. `Price.currency` / `fx_to_usd` / `fx_as_of` record it, because **a hidden conversion error is indistinguishable from a price change** — and distinguishing them is the entire point of the check.
  - **Shared accounts confound it.** Other workloads on the same API key show up as divergence. Tolerance is deliberately generous (±25%) so the check does not cry wolf and get ignored.
  - Only providers exposing a balance endpoint can be reconciled at all. That is a real gap, reported rather than silently skipped.
- **Reconsider when**: a provider starts returning per-call cost (then reconciliation becomes a cross-check rather than the primary mechanism), or when spend grows enough that a monthly invoice is the better source of truth.

## 36. Environment note: the price table is unverified because the docs were unreachable

- **Situation**: the rates in `provider_catalog.MODELS` were written from memory and could not be checked, because this environment's fetch tooling is blocked by policy — not because the site was down. The provider *API* is reachable (real calls succeed, and the balance endpoint responds); only the documentation site is not.
- **Decision**: leave `verified=False` and let `prices_verified: false` propagate into every ledger summary, every trace and every future eval row, rather than flipping a flag on unchecked numbers.
- **Why this is recorded rather than quietly fixed later**: setting `verified=True` on rates nobody checked is exactly the failure [§30](#30-unmeasured-targets-are-labelled-hypotheses) exists to prevent — and it is worse than an invented target, because a target is visibly aspirational while a cost figure reads as a measurement. The arithmetic *is* verified: recomputing cost from real reported usage matches the ledger to 1e-9. Only the rates are open.
- **How to close it**: check the provider's published per-1M rates against `MODELS["deepseek-chat"].price` (currently input `0.28`, output `1.10`, cache-read `0.028`, denominated USD), set `verified=True`, and bump `PRICE_TABLE_VERSION`. Then run `srectl smoke` around a real eval batch and confirm reconciliation returns `consistent`.

---

## Meta-decisions

### Documentation-first, not code-first

Four docs get finalized before implementation starts. Reason: without a written tradeoff record, the eventual code is indistinguishable from a coincidence. Every commit references the decision it implements.

### Interview optimization ≠ resume-driven design

Every "sophisticated" component (Temporal, MCP, three-tier routing, eval pipeline) is either genuinely required by the incident-response problem or serves as a **thinking artifact** with a clear justification. Components that would only exist for resume padding (Kafka at 1 QPS, K8s for a single pod, a Rust rewrite, LLM fine-tuning) are explicitly excluded and documented as Tier 2/3 evolution.
