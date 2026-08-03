# Call Walkthrough — one LLM call, eight ways

What actually happens between an alert arriving and a provider answering, and which
file decides what. Every scenario below is covered by a test, named at the end of
each one, so this document describes behaviour rather than intent.

Numbers marked **(live)** are from a real DeepSeek run on 2026-08-02; the rest are
from the test suite.

---

## The files, and what each one decides

| File | Layer | Decides |
|---|---|---|
| `core/investigation.py` | — | The time window every query covers; the budget ceiling; owns `messages` |
| `core/loop.py` | ④ ReAct kernel | Nothing about *how* to call — only *whether to keep going*. Converts contract errors into `Aborted` events |
| `llm/routing.py` | routing | Which model serves this `CallKind`, and the fallback order. Rejects a judge sharing the agent's family at **wiring** time |
| `llm/provider_catalog.py` | construction | Model facts: context window, price, tier, cache-marker support, and **whether the id is a provider alias** (routing refuses those — see [§37](../TRADEOFFS.md#37-route-to-concrete-models-never-to-a-provider-alias)) |
| `llm/credentials.py` | construction | Whether a provider is usable at all |
| `llm/request.py` | construction | Prompt fragment **order** (cache prefix), where breakpoints go, the cache key, the context pre-check |
| `llm/gateway.py` | assembly | The order of operations, the budget refusal, fallback, tracing |
| `llm/cache.py` | decorator | Whether this call happens at all; replays the original cost on a hit |
| `llm/cost.py` | decorator | Two totals **per currency**: what was paid and what the budget was charged |
| `llm/transport.py` | transport | Retry, backoff, circuit breaker, concurrency limit. Knows one provider |
| `llm/errors.py` | shared | Which failures are retryable, and which count toward the breaker |
| `llm/openai_compat.py` | adapter | DeepSeek/Qwen/Kimi codec + error classification + streaming |
| `llm/anthropic.py` | adapter | Anthropic codec, `cache_control` markers, 529 handling |
| `llm/usage.py` | shared | Token normalisation; `Cost` in the provider's billing currency, never converted |
| `llm/reconcile.py` | — | Whether the price table still matches reality, by same-currency comparison against the provider's own balance |

The rule the layout enforces: **`core/` never imports a concrete implementation.**
`tests/test_architecture.py` fails the build otherwise.

---

## 1. Happy path — cache miss

**预设**: `deepseek-v4-flash` routed for `main_loop` (a concrete model — routing refuses the `deepseek-chat` alias); budget `{"CNY": 3.00}`; response cache empty; breaker closed.

**情景**: `GS-RES-001-redis-oom` fires. First LLM turn.

**过程**

| # | File | Action |
|---|---|---|
| 1 | `investigation.py` | `from_alert()` — **strips `_meta`** (it states the root cause; leaking it would invalidate every accuracy number), wraps the rest in `<alert>`, pins `window = T0−30m … T0+5m` |
| 2 | `loop.py` | `tool_schemas(tools)` → raises if any tool declared `window`. Yields `TurnStarted(0)`, calls `llm.call()` |
| 3 | `routing.py` | `CallKind.MAIN_LOOP` → `[deepseek-chat]`. Fallback candidates appended only if `allow_fallback` |
| 4 | `request.py` | Orders fragments `[A]methodology [B]contract [C]integration [D]budget`; breakpoints after `[B]` and `[C]`; `cache_key()` = SHA-256 over model + system + messages + **tool schemas** + params |
| 5 | `cache.py` | Miss. `misses += 1` |
| 6 | `request.py` | `check_context()` — estimate ≤ 85% of 64 000. Passes |
| 7 | `gateway.py` | Budget gate in **this provider's currency**: `0 < 3.00 CNY`. A currency with no ceiling is refused, not treated as unbounded |
| 8 | `transport.py` | Breaker closed → acquire semaphore (max 4) → `adapter.send()` |
| 9 | `openai_compat.py` | Renders OpenAI payload — **no `cache_control`**, DeepSeek caches prefixes automatically. Parses reply; a response carrying tool calls becomes `TOOL_USE` regardless of `finish_reason` |
| 10 | `usage.py` | `prompt_cache_hit_tokens` **subtracted from** `prompt_tokens`, or cached tokens would be billed at full rate |
| 11 | `cache.py` / `cost.py` | Store response + `Cost`; ledger records `attempts=1` |

**结果 (live)**: `in=1550 out=18 cache_read=0 cost=0.001586 CNY`, one attempt, `TOOL_USE`.
The loop dispatches the tool calls with `asyncio.gather`.

*Tests*: `test_second_identical_call_is_served_from_cache`, `test_the_answer_never_reaches_the_first_message`

---

## 2. Same prefix, second question — the provider's prompt cache

**预设**: identical system prompt, different user question. Our response cache **misses** (different key), so the call really goes out.

**情景**: turn 2 of the same investigation.

**过程**: steps 1-11 as above, except step 10 sees `prompt_cache_hit_tokens: 1536`.

**结果 (live)**:

```
call 1: in=1550 out=18 cache_read=0     cost=0.001586 CNY
call 2: in=14   out=18 cache_read=1536  cost=0.000081 CNY   ← 94.9% less
```

A cache hit costs **2% of a miss** on `deepseek-v4-flash` — 1.00 vs 0.02 CNY per 1M,
[verified from the invoice](../TRADEOFFS.md#36-how-the-price-table-got-verified--and-what-verified-is-worth).
On a warm call the cache saves several times what the call itself costs.

This is the payoff of `request.py` ordering fragments most-static-first, and it
happened on DeepSeek **with no explicit markers at all**. A design that treated
prompt caching as an Anthropic feature would have got the ordering wrong here and
lost the discount silently.

The first figure reported for this was 85%, computed from a price table written from
memory. The invoice put it at 94.9% — the guess was wrong in the *favourable*
direction, which is the direction nobody questions.

*Test*: `test_deepseek_cached_tokens_are_subtracted_from_input`

---

## 3. Identical call repeated — our response cache

**预设**: same model, same system, same messages, same tool schemas.

**情景**: an eval rerun after a one-line change elsewhere.

**过程**: step 5 **hits**. Transport is never reached. The ledger records the entry
with `cached=True` and **replays the original `Cost`** rather than recomputing it.

**结果**

```
calls: 2   cached_calls: 1
money_spent:    {"CNY": 0.000081}   ← what was paid
budget_charged: {"CNY": 0.000162}   ← what the ceiling saw
```

Two totals, because they answer different questions. If a hit were also free of
*budget*, a run that degraded on exhaustion would stop degrading on rerun — and the
run would no longer reproduce. Money is genuinely saved; accounting stays faithful.

*Tests*: `test_cache_hit_still_charges_the_budget`, `test_file_store_survives_a_new_process`

---

## 4. Provider 500, then recovery

**预设**: `RetryPolicy(max_attempts=3, base_delay=0.5)`; breaker threshold 3.

**情景**: DeepSeek returns 500, then succeeds.

**过程**

| Attempt | Delay before | Outcome | Breaker |
|---|---|---|---|
| 1 | 0 | `ServerError` (retryable, counts) | `consecutive_failures = 1` |
| 2 | **0.5s** = `base_delay × 2⁰` | success | reset to 0 |

The exponent is `attempt − 2`, not `attempt − 1`. That off-by-one shipped once and
doubled every delay — invisible in production except as unexplained latency, and
caught only because a test asserted the exact delay list.

**结果**: `attempts=2` on the ledger entry and `retried: true` in the trace. "The
provider was slow" and "we retried twice" look identical in a latency number
otherwise, and retries are one of the few things that explain a p90 outlier.

*Tests*: `test_retryable_error_then_success`, `test_backoff_is_exponential_and_capped`

---

## 5. A malformed request — why the breaker must not open

**预设**: a tool schema the provider rejects with 400.

**情景**: our bug, not an outage.

**过程**: `openai_compat.classify()` → `InvalidRequest`, whose class attributes are
`retryable=False, counts_toward_breaker=False`. Transport raises immediately without
a second attempt; the breaker counter does not move. The gateway *does* try the next
provider — a schema quirk one provider rejects, another may accept.

**结果**: breaker stays `CLOSED` after repeated 400s.

This is the reason error classification had to exist before the breaker. "Open after
three failed retries" would trip here and force a pointless fallback while the
provider was perfectly healthy.

**429 is the same story for a different reason**: retryable, but never counted. Rate
limiting is a quota condition, and breaking on it would fall back to another provider
and thereby *hide* a quota misconfiguration.

*Tests*: `test_three_retries_of_one_bad_request_do_not_open_the_breaker`, `test_rate_limit_never_opens_the_breaker`

---

## 6. Provider genuinely down — breaker opens

**预设**: threshold 3, `open_seconds=30`.

**过程**

1. Three consecutive `ServerError`/`Timeout`/`Overloaded` → `opened_at = now`
2. Next call: `breaker.allows()` is `False` → `ProviderUnavailable` **without touching the provider**
3. After 30s → `HALF_OPEN`, one probe allowed. Success closes; failure re-opens

**结果, production** (`allow_fallback=True`): gateway tries `qwen-plus`, ledger marks
`fell_back: true` — and that run is **excluded from model comparison** in eval,
because its accuracy belongs to neither model and its cost mixes two price sheets.

**结果, eval** (`allow_fallback=False`): `ProviderUnavailable` → loop yields
`Aborted("provider_unavailable")`. Eval pins the provider on purpose.

*Tests*: `test_open_breaker_refuses_without_calling_the_provider`, `test_fallback_is_disabled_when_configured_off`

---

## 7. Budget exhausted

**预设**: `ToolBudget(max_cost={"CNY": 1.00})`; already spent `1.00 CNY`.

**过程**: cache miss → context check passes → **budget gate refuses** →
`BudgetExceeded` → gateway does *not* try fallback (a ceiling is ours, not the
provider's; every candidate would refuse identically) → loop's single
`except LLMContractError` yields `Aborted("budget", …)`.

**结果**: the provider is never contacted. The harness emits an "insufficient
evidence" report naming the ceiling that stopped it. Refusing rather than truncating
is what turns a cost *target* into a cost *mechanism*.

*Tests*: `test_budget_exhaustion_refuses_before_sending`, `test_budget_exceeded_becomes_an_aborted_event`

---

## 8. Context overflow

**预设**: 400 000 characters of accumulated tool results; `deepseek-chat` at 64 000 tokens.

**过程**: `check_context()` estimates ~118 000 tokens against 54 400 usable (85%
headroom reserves room for the response) → `ContextOverflow` carrying
`excess_tokens`.

**结果**: refused before transport — a 400 costs a round trip, and some providers do
not distinguish "too long" from other bad requests, so the caller would have to guess
whether compaction is the fix.

**And deliberately no fallback**, even though `qwen-plus` has a 128 000 window:
falling back to a bigger window postpones compaction until *nothing* fits. That is
fallback used to defer a problem. `ContextOverflow` is therefore a **contract** error
in `llm/protocol.py`, not a provider failure — W3 L6 catches it and compacts.

*Test*: `test_oversized_request_is_refused_before_transport`

---

## What the ledger says at the end

```json
{
  "calls": 2, "cached_calls": 0, "total_attempts": 2,
  "money_spent":    {"CNY": 0.000623},
  "budget_charged": {"CNY": 0.000623},
  "cache_savings":  {"CNY": 0.003011},
  "by_kind": {"main_loop": {"CNY": 0.000623}},
  "fell_back": false,
  "currencies": ["CNY"],
  "mixed_currencies": false,
  "prices_verified": true,
  "price_table_versions": ["2026-08-03.invoice-verified"],
  "mixed_price_tables": false
}
```

Note `cache_savings` exceeds `money_spent` by roughly 5x: on a cache-warm call the
discount is worth several times the call itself.

Four of these fields exist to keep the number honest rather than flattering:

- **Every amount is per currency and never converted** — exactly what the providers
  will invoice, in the currency they invoice in. There is deliberately no single
  scalar total: the agent bills in one currency and the judge in another, and those
  are separate lines rather than a sum. A conversion would add a continuously-moving
  second error source, and would make a wrong rate look identical to a price change.
- **`prices_verified: true`** — but per provider, not globally. DeepSeek's rates were
  read off its own invoice and cross-checked by recomputing three days of charges to
  ten decimal places; Anthropic's are still from memory and still say so. The flag was
  `false` for a day, and the ledger said so on every line rather than rounding it off.
- **`mixed_price_tables`** — a run spanning a price edit would be summing two rate
  sets into a number that is a cost at neither.
- **`mixed_currencies`** — a run spanning two billing currencies (only possible via
  fallback, which is already tagged) cannot be summed, and says so.

And two mechanisms close the loop on price drift:

**`srectl prices`** reads the provider's billing export, derives the rates from its
per-token `price` column, self-checks by recomputing each billed day, and then reports
where the catalogue disagrees. This is the authority — it measures what this account is
actually charged, per model *and per API key*. The last part matters: a second key on
the same account moved the balance during this work, which is why a balance delta
cannot be attributed to one project and an invoice line can.

**`srectl smoke`** does the crude live version against the balance endpoint. Below
`0.35 CNY` of spend it returns `inconclusive` rather than a ratio — the balance has
2-decimal resolution and one call costs a tiny fraction of that. It is a batch
instrument, sized for an eval run.

---

## What this document cannot yet show you

Every trace above is reconstructed from the test suite, not from a recorded run —
because **there is nowhere for a run to be recorded**. An audit against the code found
four instrument sources and zero sinks:

| Instrument | State |
|---|---|
| `Gateway.tracer` | defaults to a no-op |
| `transport.Attempt` (`error`, `delay_before`) | written, never read — only `len(attempts)` is used |
| Loop event stream | fully generated, then discarded by `run_to_completion` |
| Timing | **absent entirely** — no durations anywhere in `agent/` |

So today a failed investigation leaves behind an `Aborted` reason string and nothing
else. Scenario 4's retry, for instance, is *known* to have happened because a test
asserts the delay list — not because a trace recorded it.

**W2 L4a fixes this** and is deliberately sequenced before the dedup and correlation
work, because those decisions have to be recorded somewhere. Once it lands, this
document gets a ninth scenario it cannot honestly have now: **a repetition loop** —
the model calling the same tool with identical arguments until a ceiling fires. Worth
noting what already happens there, because it was luck rather than design: an identical
repeated call hits the response cache, so `money_spent` stays flat, but `budget_charged`
still grows (a hit replays the original cost), and the budget gate reads
`budget_charged` — so the loop *is* caught. Under the naive "cache hits are free"
design it would have run for free until `max_turns`.

See [TRADEOFFS §42](../TRADEOFFS.md#42-traceability-one-id-four-sinks--and-an-honest-audit-of-what-is-currently-wired).
