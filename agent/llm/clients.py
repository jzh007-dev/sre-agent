"""Real SDK clients, and the factory that assembles a live `Gateway` from env.

Kept separate from the adapters on purpose: the adapters take an injected client,
which is what lets every routing, construction and transport policy path be tested
offline. This module is the only place that imports a provider SDK, so it is also
the only place a missing dependency can bite — and it does so with a message that
names the package.

**`max_retries=0` on every client.** Both SDKs retry by default; layering the
transport's attempts on top of theirs is nine requests to a dying provider and
makes the retry count unreadable. Transport owns retry, so the SDK must not.
See [TRADEOFFS §33](../../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators)
delta 8.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ..core.trace import Trace
from .anthropic import AnthropicAdapter
from .cache import FileStore, MemoryStore, ResponseCache
from .credentials import api_key, configured_providers
from .gateway import Gateway
from .openai_compat import OpenAICompatAdapter
from .provider_catalog import MODELS, PROVIDERS, ProviderSpec, model
from .routing import CallKind, RoutingConfig, default_config
from .transport import CircuitBreaker, RetryPolicy, Transport

#: Wall-clock request deadline handed to the SDK. Distinct from the tool timeout in
#: `tools/protocol.py`: this bounds one provider call, that bounds one tool call.
REQUEST_TIMEOUT_SECONDS = 120.0


class MissingDependency(RuntimeError):
    pass


@dataclass
class OpenAIChatClient:
    """Adapts the `openai` SDK's nested call shape to the flat `create(**payload)`
    that `OpenAICompatAdapter` expects.

    The narrow Protocol in the adapter is what makes the fake in tests trivial; this
    class is the two-line bridge to the real thing.
    """

    inner: Any

    async def create(self, **payload: Any) -> Any:
        return await self.inner.chat.completions.create(**payload)


@dataclass
class AnthropicMessagesClient:
    inner: Any

    async def create(self, **payload: Any) -> Any:
        return await self.inner.messages.create(**payload)


def _openai_client(spec: ProviderSpec) -> OpenAIChatClient:
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover — environment, not logic
        raise MissingDependency(
            "the `openai` package is required for OpenAI-compatible providers "
            "(DeepSeek / Qwen / Kimi). Install it: pip install openai"
        ) from exc
    return OpenAIChatClient(
        AsyncOpenAI(
            api_key=api_key(spec),
            base_url=spec.base_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=0,  # transport owns retry — see the module docstring
        )
    )


def _anthropic_client(spec: ProviderSpec) -> AnthropicMessagesClient:
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:  # pragma: no cover
        raise MissingDependency(
            "the `anthropic` package is required for the different-family judge and "
            "reviewer. Install it: pip install anthropic"
        ) from exc
    return AnthropicMessagesClient(
        AsyncAnthropic(
            api_key=api_key(spec),
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=0,
        )
    )


def build_adapter(provider_name: str, model_id: str) -> Any:
    spec = PROVIDERS[provider_name]
    model_spec = model(model_id)
    if spec.kind == "openai_compat":
        return OpenAICompatAdapter(
            provider_spec=spec, model_spec=model_spec, client=_openai_client(spec)
        )
    return AnthropicAdapter(
        provider_spec=spec, model_spec=model_spec, client=_anthropic_client(spec)
    )


def _default_model_for(provider_name: str) -> str:
    """The workhorse model of a provider, preferring the cheaper tier.

    Only used to give a transport *a* model for client construction; the actual
    model per call comes from routing. Providers reached over an OpenAI-compatible
    endpoint do not bind a model at client level, so this is mostly bookkeeping.
    """
    candidates = [m for m in MODELS.values() if m.provider == provider_name]
    if not candidates:
        raise KeyError(f"no models catalogued for provider {provider_name!r}")
    order = {"workhorse": 0, "cheap": 1, "strong": 2}
    return sorted(candidates, key=lambda m: order.get(m.tier, 9))[0].id


def load_env(path: str = ".env") -> None:
    """Load `.env` if present.

    Best-effort and dependency-optional: a missing `python-dotenv` or a missing file
    is not an error, because exported environment variables are an equally valid way
    to configure this.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(path, override=False)


def live_gateway(
    routing: RoutingConfig | None = None,
    *,
    cache_path: str | None = None,
    trace: Trace | None = None,
    allow_fallback: bool = True,
    max_concurrency: int = 4,
) -> Gateway:
    """Assemble a `Gateway` with real transports for every credentialled provider.

    A provider with no key is **skipped rather than wired to fail**: a fallback
    candidate that always raises `MissingCredential` burns an attempt and shows up
    in the trace looking like a provider outage.

    `cache_path` turns on the file-backed response cache. That is the wiring eval
    wants — the expensive scenario is not one run, it is the same run repeated after
    a one-line prompt edit.

    `trace` is the fallback for callers with no loop above them; a loop installs its
    own and that one wins. See `Gateway.active_trace`.
    """
    load_env()
    available = set(configured_providers())
    if not available:
        raise RuntimeError(
            "no provider credentials found. Copy .env.example to .env and set one of "
            f"{sorted(spec.api_key_env for spec in PROVIDERS.values())}"
        )

    config = routing or default_config()
    wanted = {
        model(model_id).provider
        for kind in CallKind
        for model_id in config.candidates(kind)
    }

    transports: dict[str, Transport] = {}
    for provider_name in sorted(wanted & available):
        transports[provider_name] = Transport(
            adapter=build_adapter(provider_name, _default_model_for(provider_name)),
            policy=RetryPolicy(),
            breaker=CircuitBreaker(),
            max_concurrency=max_concurrency,
        )

    store = FileStore(cache_path) if cache_path else MemoryStore()
    return Gateway(
        routing=config,
        transports=transports,
        cache=ResponseCache(store=store),
        trace=trace,
        allow_fallback=allow_fallback,
    )


def single_provider_routing(model_id: str) -> RoutingConfig:
    """Route every call kind at one model.

    For smoke tests and for the common case of holding exactly one API key. It
    deliberately **fails `routing.validate()`** if used as-is, because judge and
    reviewer would share the agent's family — so callers that need those kinds must
    supply a second family. Wiring-time failure is the point.
    """
    return RoutingConfig(
        agent_model=model_id,
        assignments={kind: model_id for kind in CallKind},
    )


def smoke_routing(model_id: str) -> RoutingConfig:
    """A routing config valid with a single provider, by leaving judge and reviewer
    pointed at a different family that need not be credentialled.

    `live_gateway` only wires transports for providers that *have* keys, so the
    uncredentialled judge model is catalogued and validated but never called. That
    keeps the family invariant honest while letting one key run the loop.
    """
    other = "claude-sonnet-5" if model(model_id).provider != "anthropic" else "deepseek-chat"
    return RoutingConfig(
        agent_model=model_id,
        assignments={
            CallKind.MAIN_LOOP: model_id,
            CallKind.REFUTE: model_id,
            CallKind.CLASSIFY: model_id,
            CallKind.JUDGE: other,
            CallKind.REVIEWER: other,
        },
    )


def env_summary() -> dict[str, bool]:
    """Which providers have credentials. Printed by the smoke script so a failure is
    diagnosable without reading the process environment."""
    return {
        name: bool(os.environ.get(spec.api_key_env, "").strip())
        for name, spec in PROVIDERS.items()
    }
