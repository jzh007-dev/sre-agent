"""LiteLLM swap-in — deliberately not implemented.

Evaluated and declined in [TRADEOFFS §34](../../TRADEOFFS.md#34-litellm-not-adopted-kept-as-a-documented-swap-in).
This file exists so the decision is visible in the code rather than only in a
document, and so the swap is a known shape rather than a research task.

**Why not now**

LiteLLM would replace roughly 40% of the transport layer — provider plumbing,
error normalisation, streaming normalisation — and none of the four gateway
responsibilities this project is built to demonstrate:

- `cache_control` breakpoint placement (`request.py` + `anthropic.py`)
- per-investigation budget with refuse-on-exceed (`gateway.py`)
- a response cache that replays cost so eval reproduces (`cache.py`)
- routing by task nature with a wiring-time family check (`routing.py`)

Its value also scales with provider count, and ours is two adapter classes:
`OpenAICompatAdapter` covers DeepSeek, Qwen and Kimi through `base_url` alone.
Its bundled price map is additionally approximate and lags provider changes, which
is a poor fit for a project whose stated thesis is measured cost.

**What is conceded**: normalising error taxonomies across providers is genuinely
fiddly and LiteLLM has done it. Here it is about a day of work — and that day
produces `errors.py`, which is itself an artifact worth having.

**When to switch**

- provider count passes ~6, or
- a non-standard endpoint is needed (Azure, Vertex), or
- load balancing across multiple API keys per provider is needed.

**How the swap looks**

Implement the `Adapter` protocol from `transport.py` — `provider` plus
`async send(request) -> (Response, Usage)` — delegating to
`litellm.acompletion(**payload)`, and map `litellm.exceptions` onto `errors.py`.
Nothing above transport changes: routing, construction, cache, budget, ledger and
tracing are all provider-agnostic by construction, and `core/loop.py` never learns
that anything moved.
"""
from __future__ import annotations


class LiteLLMAdapter:  # pragma: no cover — intentionally unimplemented
    """Placeholder. See the module docstring for the decision and the swap shape."""

    def __init__(self, *_: object, **__: object) -> None:
        raise NotImplementedError(
            "LiteLLM was evaluated and declined for Tier 1.5 — see TRADEOFFS §34. "
            "It replaces provider plumbing but none of the gateway responsibilities "
            "that carry this project's argument. Switch when provider count passes "
            "~6, a non-standard endpoint is needed, or multi-key load balancing is."
        )
