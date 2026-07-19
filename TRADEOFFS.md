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

- **Decision**: LangGraph 5-node state machine (`triage → collect → hypothesize → verify → report`); each node runs a bounded agent loop.
- **Alternatives**:
  - (A) Pure agentic loop (Claude-Code-style) — one big loop, LLM picks any tool at any time.
  - (B) Pure workflow — every step deterministic, LLM only for text generation.
- **Why**: incident response has known phases; the agentic freedom belongs *inside* a phase (which signal to pull, which hypothesis to check) not *across* phases (skipping verify because model felt confident). This is also the only shape that lets us do **phase-level eval assertions**.
- **Cost**: some incidents don't fit the 5-phase mold (e.g., "obvious rollback needed") and pay unnecessary overhead.
- **Reconsider when**: >30% of incidents short-circuit the graph, or eval reveals phase boundaries hurt accuracy.

## 2. Durable execution: Temporal, not plain async

- **Decision**: Temporal workflow wrapping the LangGraph run.
- **Alternatives**:
  - (A) Plain `asyncio` with checkpoints to Postgres.
  - (B) Celery/RQ with periodic state pickling.
- **Why**: incidents run 5-30 minutes. Losing state mid-verify because a worker OOMs is unacceptable. Temporal gives durability + retry semantics + signal-based human approval for free. Rolling our own would be 2 weeks and worse.
- **Cost**: extra service to run, learning curve, harder local debugging than a plain script.
- **Reconsider when**: p99 incident duration falls under 60s (then plain async is fine).

## 3. LLM Gateway: in-process wrapper, not LiteLLM/Portkey service

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

---

## Meta-decisions

### Documentation-first, not code-first

Four docs get finalized before implementation starts. Reason: without a written tradeoff record, the eventual code is indistinguishable from a coincidence. Every commit references the decision it implements.

### Interview optimization ≠ resume-driven design

Every "sophisticated" component (Temporal, MCP, three-tier routing, eval pipeline) is either genuinely required by the incident-response problem or serves as a **thinking artifact** with a clear justification. Components that would only exist for resume padding (Kafka at 1 QPS, K8s for a single pod, a Rust rewrite, LLM fine-tuning) are explicitly excluded and documented as Tier 2/3 evolution.
