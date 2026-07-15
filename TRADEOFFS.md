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

## 10. Gate policy: hardcoded rules, not policy engine

- **Decision**: side_effect classification (`READ` / `WRITE` / `DESTRUCTIVE`) decides gate — read auto-pass, write requires human signal, destructive requires signal + dry-run diff shown.
- **Alternatives**:
  - (A) OPA / Cedar policy engine.
  - (B) LLM-decides-gate (asking a second LLM "is this safe").
  - (C) No gate — trust the primary LLM.
- **Why**: hardcoded rules are auditable and won't drift. Policy engine is right for Tier 3 where per-service blast-radius limits get involved.
- **Cost**: adding gate exceptions requires a code change and redeploy.
- **Reconsider when**: gate policies need per-tenant configuration.

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

## 14. Frontend: none for POC (Slack + CLI only)

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
