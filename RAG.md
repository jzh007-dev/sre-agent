# Runbook RAG Subsystem

A retrieval subsystem for SRE runbooks and postmortems. **Not** the agent's episodic memory (that's past-investigation recall in pgvector) — this is the **knowledge base** the agent searches via the `search_runbook` tool when it wants an applicable playbook.

**Namespaced per integration.** Every chunk carries the `runbook_namespace` declared in its integration's YAML, and retrieval filters on it. This is the mechanism behind [TRADEOFFS §20](./TRADEOFFS.md#20-middleware-specific-knowledge-lives-in-rag-not-in-agent-code-or-cases): middleware-specific operational knowledge lives here as *content*, never in the agent code and never in the system prompt. Adding Kafka to the fleet means adding a namespace of runbook chunks — not a prompt edit, not a code change.

> Same Tier 1.5 philosophy: smallest system that works; every sophisticated feature listed with a **trigger condition** for when it becomes worth adding.

---

## Scope reality check

Before designing anything, honest numbers:

| | Value | Implication |
|---|---|---|
| Runbook corpus | ~100 docs | Small |
| Chunks after splitting | ~3k | Trivial for any modern index |
| Query volume | <1 QPS during incidents | Latency budget generous |
| Query type diversity | ~2 (symptom lookup, procedure lookup) | Single retrieval config fine |
| Doc edit rate | <5 docs/week | Batch reindex is fine |

At this scale, a lot of RAG "best practices" are overkill. The design below is intentionally simple, and every simplification has a documented trigger for when we'd revisit.

---

## Tier 1.5 implementation

### Data flow

```
   Wiki (source of truth)
       │
       │  weekly sync
       ▼
   ┌────────────────────────────────┐
   │ Ingestion                      │
   │  1. HTML/PDF → Markdown        │
   │  2. Split at H2 boundaries     │
   │  3. Embed (BGE-M3, self-host)  │
   │  4. Upsert to pgvector + tsv   │
   └────────────────┬───────────────┘
                    ▼
   ┌────────────────────────────────┐
   │ Retrieval                      │
   │  namespace filter (integration)│
   │  dense (pgvector) + BM25 (tsv) │
   │  simple weighted sum → top-5   │
   └────────────────┬───────────────┘
                    ▼
   search_runbook tool → agent loop
```

### Chunking

- Split by H2 heading. One H2 section = one chunk.
- H1 title prepended as breadcrumb: `"# Chapter 3 System Architecture\n## 3.1 Core Components\n\n<body>"`.
- Chunks that exceed 1500 tokens get split at paragraph boundary; a rare event with our doc style.
- Tables and code blocks kept intact inside their host chunk (never split mid-block).

**No parent-child hierarchy in Tier 1.5.** Docs are short enough (avg ~5 H2s per doc) that H2-granularity chunks are already the right size for both embedding and LLM context. Parent-child would double the storage and query complexity for marginal recall gain at this corpus size.

### Embedding

- **BGE-M3**, self-hosted via `sentence-transformers` (CPU ONNX is fine at our QPS).
- Not fine-tuned. Not from an API. 1024-dim.
- Rationale: at 3k chunks, embedding quality plateaus fast; the difference between BGE-M3 and a fine-tuned model is <5% recall@5 and doesn't justify the training pipeline.

### Retrieval

- Dense: pgvector, cosine similarity, HNSW index (`m=16, ef_search=40`).
- BM25: Postgres `tsvector` + `ts_rank_cd`.
- Fusion: **simple normalized weighted sum**, `score = 0.7 * dense_norm + 0.3 * bm25_norm`. Both scores min-max normalized within the returned set.
- **No RRF, no reranker** in Tier 1.5. Reason below.
- Metadata filter (`service_tags`, `updated_at`) applied as SQL `WHERE` before ranking.
- Return: top 5 chunks.

### Incremental update

Doc-level content hash. If unchanged: skip. If changed: **re-embed all chunks of that doc** (typical doc is ~5 chunks, ~2¢ of BGE compute; not worth per-chunk diff logic).

Delete-old + insert-new is transactional per doc, so concurrent readers never see a partial state.

### Handling misses

- Reranker's absent, so we use a **dense-score threshold** (`cos_sim > 0.55`) as the "did we find anything" signal.
- If top-1 below threshold: the tool returns "no matching runbook" and the agent falls back to first-principles reasoning.
- These "no-hit" events are logged; weekly report surfaces topics that need runbook coverage.
- **Feedback loop (Week 4)**: on a no-hit case that the agent nonetheless diagnoses, it proposes a draft runbook chunk in its report. Otherwise the no-hit log is a list nobody consumes. See [ROADMAP open gap #6](./docs/ROADMAP.md#open-gaps).

### Metrics tracked

Small golden set: 50 `(query, expected_chunk_id)` pairs, hand-labeled from real oncall Slack questions.

| Metric | Target | Current |
|---|---|---|
| recall@5 | ≥ 0.85 | (TBD) |
| hit@3 | ≥ 0.75 | (TBD) |
| p95 latency | < 300ms | (TBD) |

`hit@1` tracked as diagnostic only, not a target — top-5 goes to the LLM anyway.

**That's it for what we actually build.** Everything below is Tier 2 evolution with explicit triggers.

---

## Tier 2 evolution paths

Each feature below is **not built** but **thought through**. If a specific signal fires, we add it. The trigger conditions are the interview substance — every "why not X now" has an answer, and every "when would you add X" has a threshold.

### Parent-child chunking

- **What it is**: embed small H3 chunks (200-400 tok) but return the containing H2 to the LLM. Embedding sharpness + generous context, both.
- **Add when**: docs grow past ~10 H2s each (chunks get too big for tight embedding) OR recall@5 stalls below 0.85 with everything else tuned.
- **Interview hook**: header breadcrumb stitched into child chunk prefix; parent expansion at query time via `parent_id` foreign key.

### Cross-encoder reranker

- **What it is**: BGE-reranker-v2-m3 re-scores top-20 candidates. Cross-attends `(query, chunk)` for a stronger relevance signal than bi-encoder cosine.
- **Add when**: recall@20 is fine but hit@3 lags (retrieval finds it but ranks it wrong) OR corpus grows past ~50k chunks (bi-encoder discrimination degrades).
- **Interview hook**: candidate window is ~20 not 50 because reranker is ~30x slower per pair; top-5 output because LLM position bias favors <10 chunks.
- **Why chunk order matters**: position bias — early context gets more attention; 20 mediocre chunks beat 5 relevant ones for recall but lose to them for accuracy. Reranker's job is to put the best chunk first.

### RRF instead of weighted sum

- **What it is**: rank-based fusion `sum(1/(k+rank))` with `k=60`. Independent of raw score scales; robust when adding a third retrieval path.
- **Add when**: adding a third retrieval channel (e.g., entity-graph, MMR-diversified) — weighted sum breaks with heterogeneous score distributions.
- **Interview hook**: `k=60` standard, weights per ranker adjustable; documents present in only one ranker's top-N still score.

### Query intent routing

- **What it is**: light classifier tags query as `symptom_lookup` / `identifier_lookup` / `procedure_lookup` / `time_ranged`, picks a retrieval config per tag.
- **Add when**: query type diversity grows past ~4 (right now we have 2 and one config serves both).
- **Why not TinyBERT?**: 4 classes with strong keyword signals → logistic regression hits ~96%; TinyBERT's ~1% gain isn't worth the training/serving stack. Would use TinyBERT past ~8 classes or when contextual disambiguation matters.

### Time-info injection

- **What it is**: parse query time semantics ("last hour", "yesterday 8pm"), extract `time_window`, apply as metadata filter, stitch into prompt as `<time_context>` tag above retrieved chunks.
- **Add when**: incident queries start referencing specific timestamps often (we mostly get "right now" queries — not a pain point yet).
- **Interview hook**: never embed the time expression itself into the query vector; time is a structured filter, not a semantic signal.

### Per-chunk incremental update

- **What it is**: content-hash each chunk; on doc edit, diff old/new chunk sets, re-embed only modified chunks.
- **Add when**: doc edit rate goes past ~50/week OR corpus grows to where full-doc re-embed costs matter.
- **Interview hook**: `(doc_id, chunk_id)` composite PK; hard-delete removed chunks; audit table keeps 30-day rollback.

### API-based / larger embedding

- **What it is**: swap BGE-M3 for OpenAI `text-embedding-3-large` (3072-dim) or Cohere.
- **Add when**: recall stalls AND we can rule out chunking/ranker (embedding quality is the actual bottleneck).
- **Interview hook**: also compare with a fine-tuned domain embedding — worth the training pipeline only past ~1M chunks AND a stable query distribution.

### Milvus / Qdrant instead of pgvector

- **Add when**: >1M vectors OR p95 retrieval >200ms with pgvector HNSW tuned.
- **Interview hook**: HNSW beats IVF at our scale; IVF becomes competitive on memory past ~5M vectors. If forced onto IVF: `nlist ≈ 4·√N`, sweep `nprobe` 1→32 until recall plateaus.

### GraphRAG

- **What it is**: entity + relation extraction at index time, graph storage, multi-hop query resolution.
- **Add when**: queries need cross-doc synthesis (real signal: incident reports repeatedly cite "needed knowledge from 3 runbooks").
- **Interview hook**: 3-4 weeks of pipeline work; useful for "what services depend on X and had incidents last month" — not our current query shape.

### Full Agentic RAG at retrieval layer

- **What it is**: retrieval agent decomposes complex query → sub-queries → per-sub-query retrieve → synthesize.
- **Our position**: the *outer* system is already agentic — the ReAct loop decides when to call `search_runbook`, with what query, and whether to call it again after seeing the result. That is agentic-RAG behavior at the agent level, and it comes free from the loop. Keeping the *retrieval* subsystem procedural makes it cacheable and testable.
- **Add when**: retrieval starts needing planning (queries with multiple entities that must be resolved separately).

### Multimodal RAG

- **What it is**: architecture diagrams, dashboard screenshots as retrieval targets; VLM caption generation at index time.
- **Add when**: not planned. SRE runbooks are text-first. If a diagram carries load-bearing info, we treat it as a documentation bug — the doc should have text.

### Airgapped deployment

- **What it is**: fully offline stack for regulated environments (medical device, defense).
- **Path**: BGE-M3 already self-hosted; swap Anthropic API for local vllm (Qwen-2.5-32B); ship as one `docker-compose` bundle with pre-loaded runbooks.
- **What degrades**: LLM judge for eval must also be local; absolute scores drop, relative regression detection still works.

---

## Metrics — computing recall without labels

Question that comes up: "How is `recall@5 = 85%` computed?"

Two modes:

1. **Golden set (offline)**: `recall@k = |retrieved ∩ ground_truth| / |ground_truth|` against the 50 labeled pairs.
2. **Online proxy (no labels)**: LLM-as-judge scores each `(query, retrieved_chunk)` on a 0-2 rubric. Aggregate to **soft recall** = `sum(scores) / (2·k)`. Runs on a 5% traffic sample nightly. Detects drift, not a substitute for labeled eval.

`hit@3 > hit@1` on purpose: SRE queries are broad, 2-3 chunks usually jointly answer; strict top-1 is a punishing signal that doesn't correlate with agent accuracy. `hit@1` kept as a diagnostic (if it tanks but `hit@3` holds, the retriever is fine and the ranker miscalibrated).

---

## Ablation matrix (planned)

Numbers to fill after implementation. Interview point is the **shape** of the table, not the values.

| Config | recall@5 | hit@3 | p50 latency |
|---|---|---|---|
| dense only | | | |
| BM25 only | | | |
| weighted sum (Tier 1.5) | | | |
| + RRF | | | |
| + reranker | | | |
| + parent-child | | | |

Each row = one Tier-2 upgrade. The delta is what justifies (or doesn't justify) adding it. If a row shows <2% gain, we don't ship it.

---

## Design principle

Same as the rest of this project: **build the smallest system that carries the thinking**. Every sophisticated RAG feature has a real-world use case; almost none of them exists in *this project's real-world use case yet*. The value of writing them down is knowing exactly which signal would flip the decision — that's the conversation, not the implementation.
