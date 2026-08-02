"""Provider and model registry — construction layer.

Adding a provider is one `ProviderSpec` plus its models. That is the whole reason
[§3](../../TRADEOFFS.md#3-llm-gateway-in-process-wrapper-not-litellmportkey-service)
argues a hand-written gateway is affordable here: `OpenAICompatLLM` covers
DeepSeek, Qwen and Kimi through `base_url` alone, so "multi-provider" costs two
adapter classes and this table.

`family` is load-bearing rather than descriptive. [EVAL.md](../../EVAL.md) requires
the judge to differ from the agent and [SECURITY.md](../../SECURITY.md) L3 requires
a different-family reviewer, so routing validates family difference at wiring
time. Two models from the same lab share a family even when their names differ.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .usage import Price

AdapterKind = Literal["openai_compat", "anthropic"]
Tier = Literal["cheap", "workhorse", "strong"]


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    #: Shared-lab identity. The family-difference rule compares this, not `name`.
    family: str
    api_key_env: str
    kind: AdapterKind
    #: None for providers reached through their own SDK rather than an
    #: OpenAI-compatible endpoint.
    base_url: str | None = None


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    context_window: int
    price: Price
    tier: Tier
    supports_tools: bool = True
    #: Whether explicit `cache_control` markers are honoured. Providers that cache
    #: prefixes automatically set this False — the prompt layering still pays off,
    #: it just needs no annotation. See [§33](../../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators) delta 4.
    supports_explicit_cache: bool = False
    #: Set False for a model observed to emit malformed tool calls. A model the
    #: loop cannot rely on for `tool_use` is unusable as `main_loop` however well
    #: it writes prose.
    reliable_tool_use: bool = True
    #: Set when this id is a provider **alias** rather than a concrete model.
    #:
    #: Routing refuses to use one. Asking DeepSeek for `deepseek-chat` is served by
    #: `deepseek-v4-flash` today and by whatever replaces it tomorrow, which breaks
    #: two things at once: prices are per model, so pricing an alias prices nothing
    #: in particular; and [EVAL.md](../../EVAL.md) keys reproducibility on
    #: `model_version`, which a moving alias silently invalidates while looking
    #: perfectly stable. Aliases are catalogued so the error message can name the
    #: concrete model to use instead.
    alias_of: str | None = None


PROVIDERS: dict[str, ProviderSpec] = {
    "deepseek": ProviderSpec(
        name="deepseek",
        family="deepseek",
        api_key_env="DEEPSEEK_API_KEY",
        kind="openai_compat",
        base_url="https://api.deepseek.com/v1",
    ),
    "qwen": ProviderSpec(
        name="qwen",
        family="qwen",
        api_key_env="DASHSCOPE_API_KEY",
        kind="openai_compat",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    "kimi": ProviderSpec(
        name="kimi",
        family="moonshot",
        api_key_env="MOONSHOT_API_KEY",
        kind="openai_compat",
        base_url="https://api.moonshot.cn/v1",
    ),
    "anthropic": ProviderSpec(
        name="anthropic",
        family="anthropic",
        api_key_env="ANTHROPIC_API_KEY",
        kind="anthropic",
    ),
}

# Prices are placeholders pending verification against each provider's published
# pricing — see `usage.PRICE_TABLE_VERSION`. They are functional (accounting runs,
# relative comparisons hold) but any figure derived from them is reported as
# unverified rather than as measured fact.
#
# Two separate problems are flagged here, and the second is the worse one:
#   1. the rates were written from memory and are unverified;
#   2. DeepSeek's live `/models` returns `deepseek-v4-flash` and `deepseek-v4-pro`,
#      and a request for `deepseek-chat` is *served by* `deepseek-v4-flash`. So the
#      rates below were also attached to the wrong thing — an alias, not a model.
#      The concrete ids are catalogued and the aliases are marked; routing refuses
#      an alias so this cannot recur silently.
# DeepSeek bills in CNY, so its rates should be recorded in CNY once checked; the
# USD figures below are carried over from the alias entries and are certainly wrong
# for v4. `srectl smoke` reports `not_comparable` until that is fixed, by design.
MODELS: dict[str, ModelSpec] = {
    "deepseek-v4-flash": ModelSpec(
        id="deepseek-v4-flash",
        provider="deepseek",
        context_window=64_000,
        price=Price(input=0.28, output=1.10, cache_read=0.028),
        tier="workhorse",
        supports_explicit_cache=False,  # caches prefixes automatically
    ),
    "deepseek-v4-pro": ModelSpec(
        id="deepseek-v4-pro",
        provider="deepseek",
        context_window=64_000,
        price=Price(input=0.55, output=2.19),
        tier="strong",
        supports_explicit_cache=False,
    ),
    # Aliases — catalogued so routing can reject them by name with a useful message.
    "deepseek-chat": ModelSpec(
        id="deepseek-chat",
        provider="deepseek",
        context_window=64_000,
        price=Price(input=0.28, output=1.10, cache_read=0.028),
        tier="workhorse",
        alias_of="deepseek-v4-flash",
    ),
    "deepseek-reasoner": ModelSpec(
        id="deepseek-reasoner",
        provider="deepseek",
        context_window=64_000,
        price=Price(input=0.55, output=2.19),
        tier="strong",
        alias_of="deepseek-v4-pro",
    ),
    "qwen-plus": ModelSpec(
        id="qwen-plus",
        provider="qwen",
        context_window=128_000,
        price=Price(input=0.41, output=1.24),
        tier="workhorse",
    ),
    "qwen-turbo": ModelSpec(
        id="qwen-turbo",
        provider="qwen",
        context_window=128_000,
        price=Price(input=0.05, output=0.20),
        tier="cheap",
    ),
    "moonshot-v1-128k": ModelSpec(
        id="moonshot-v1-128k",
        provider="kimi",
        context_window=128_000,
        price=Price(input=8.40, output=8.40),
        tier="workhorse",
    ),
    "claude-sonnet-5": ModelSpec(
        id="claude-sonnet-5",
        provider="anthropic",
        context_window=200_000,
        price=Price(input=3.00, output=15.00),
        tier="workhorse",
        supports_explicit_cache=True,
    ),
    "claude-opus-5": ModelSpec(
        id="claude-opus-5",
        provider="anthropic",
        context_window=200_000,
        price=Price(input=15.00, output=75.00),
        tier="strong",
        supports_explicit_cache=True,
    ),
}


class UnknownModel(KeyError):
    pass


def model(model_id: str) -> ModelSpec:
    try:
        return MODELS[model_id]
    except KeyError as exc:
        raise UnknownModel(f"unknown model {model_id!r}; known: {sorted(MODELS)}") from exc


def provider_of(spec: ModelSpec) -> ProviderSpec:
    return PROVIDERS[spec.provider]


def family_of(model_id: str) -> str:
    return provider_of(model(model_id)).family
