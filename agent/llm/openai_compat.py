"""OpenAI-compatible adapter — DeepSeek, Qwen and Kimi through `base_url` alone.

One class for three providers is the reason "multi-provider" costs almost nothing
here, and the reason [§34](../../TRADEOFFS.md#34-litellm-not-adopted-kept-as-a-documented-swap-in)
concludes LiteLLM is not worth its weight at this scale.

The adapter owns two provider-specific jobs and no policy:

1. **Codec** — translate our Anthropic-shaped domain types to and from the flat
   OpenAI shape. Anthropic's content-block model is strictly more expressive, so
   this direction loses nothing; the reverse would.
2. **Error classification** — map SDK exceptions onto the taxonomy in `errors.py`.
   Policy (retry, breaker, fallback) is transport's, keyed on the class this
   returns.

The SDK client is injected rather than constructed here, which is what lets every
transport policy path be tested offline with no network and no API key.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from . import errors
from .provider_catalog import ModelSpec, ProviderSpec
from .request import LLMRequest
from .types import Message, Response, StopReason, TextBlock, ToolResultBlock, ToolUseBlock
from .usage import Usage


class ChatClient(Protocol):
    """The slice of the OpenAI SDK this adapter uses.

    Narrow on purpose: a Protocol this small is trivial to fake in tests, and it
    documents exactly how much of the SDK surface we depend on.
    """

    async def create(self, **payload: Any) -> Any: ...


@dataclass
class OpenAICompatAdapter:
    provider_spec: ProviderSpec
    model_spec: ModelSpec
    client: ChatClient

    @property
    def provider(self) -> str:
        return self.provider_spec.name

    # ---------------------------------------------------------------- outbound

    def render(self, request: LLMRequest) -> dict[str, Any]:
        """Build the wire payload.

        Note there is no `cache_control` here: these providers cache their prompt
        prefix automatically. The prompt *layering* still pays off — a stable
        prefix is what makes automatic caching hit — it just needs no annotation.
        That asymmetry is [§33](../../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators)
        delta 4: treating caching as an Anthropic-only concern would have got the
        ordering wrong here and silently lost the discount.
        """
        messages: list[dict[str, Any]] = []
        system_text = request.system.text()
        if system_text:
            messages.append({"role": "system", "content": system_text})
        for message in request.messages:
            messages.extend(_to_openai_messages(message))

        payload: dict[str, Any] = {
            "model": request.model_id,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool["input_schema"],
                    },
                }
                for tool in request.tools
            ]
        payload.update(dict(request.params))
        return payload

    # ----------------------------------------------------------------- inbound

    async def send(self, request: LLMRequest) -> tuple[Response, Usage]:
        try:
            raw = await self.client.create(**self.render(request))
        except errors.ProviderError:
            # Already classified (a fake client in tests, or a nested adapter).
            raise
        except Exception as exc:  # noqa: BLE001 — deliberate: classify everything
            raise self.classify(exc) from exc
        return self.parse(raw)

    def parse(self, raw: Any) -> tuple[Response, Usage]:
        try:
            choice = raw.choices[0]
            message = choice.message
            content: list[Any] = []

            text = getattr(message, "content", None)
            if text:
                content.append(TextBlock(text=text))

            for call in getattr(message, "tool_calls", None) or []:
                content.append(
                    ToolUseBlock(
                        id=call.id,
                        name=call.function.name,
                        input=_parse_arguments(call.function.arguments),
                    )
                )

            stop = _STOP_REASONS.get(getattr(choice, "finish_reason", ""), StopReason.END_TURN)
            # A response carrying tool calls is a tool_use turn regardless of what
            # the provider labelled it; providers are inconsistent here and the
            # loop keys on the blocks, not the label.
            if any(isinstance(block, ToolUseBlock) for block in content):
                stop = StopReason.TOOL_USE

            return (
                Response(
                    stop_reason=stop,
                    content=content,
                    served_model=str(getattr(raw, "model", "") or ""),
                ),
                _parse_usage(raw),
            )
        except errors.ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise errors.MalformedResponse(
                f"could not parse response: {type(exc).__name__}: {exc}",
                provider=self.provider,
            ) from exc

    # -------------------------------------------------------- classification

    def classify(self, exc: Exception) -> errors.ProviderError:
        """SDK exception → taxonomy class.

        Keyed on HTTP status where available, because status is the one thing every
        OpenAI-compatible provider agrees on. Message-sniffing is used only for the
        context-limit case, which is a 400 that needs distinguishing from other
        400s so the caller can compact and retry.
        """
        status = _status_of(exc)
        retry_after = _retry_after_of(exc)
        message = f"{type(exc).__name__}: {exc}"

        if status in (401, 403):
            return errors.AuthError(message, provider=self.provider, status=status)
        if status == 429:
            return errors.RateLimit(
                message, provider=self.provider, status=status, retry_after=retry_after
            )
        if status == 400:
            text = str(exc).lower()
            if any(hint in text for hint in _CONTEXT_LIMIT_HINTS):
                return errors.ContextLimit(message, provider=self.provider, status=status)
            return errors.InvalidRequest(message, provider=self.provider, status=status)
        if status == 422:
            return errors.InvalidRequest(message, provider=self.provider, status=status)
        if status is not None and 500 <= status < 600:
            return errors.ServerError(
                message, provider=self.provider, status=status, retry_after=retry_after
            )
        if _looks_like_timeout(exc):
            return errors.Timeout(message, provider=self.provider)
        if "content" in str(exc).lower() and "filter" in str(exc).lower():
            return errors.ContentFilter(message, provider=self.provider, status=status)
        # Unknown failures are treated as retryable server errors: a transient
        # network fault is far more likely than a novel permanent one, and the
        # breaker bounds the damage if that guess is wrong.
        return errors.ServerError(message, provider=self.provider, status=status)


_STOP_REASONS = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
}

_CONTEXT_LIMIT_HINTS = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "reduce the length",
)


def _to_openai_messages(message: Message) -> list[dict[str, Any]]:
    """One domain message may become several OpenAI messages.

    Anthropic nests `tool_result` blocks inside a `role=user` message; OpenAI wants
    one `role=tool` message per result. That structural difference is the main
    reason the codec is not a field rename.
    """
    tool_results = [b for b in message.content if isinstance(b, ToolResultBlock)]
    if tool_results:
        return [
            {"role": "tool", "tool_call_id": block.tool_use_id, "content": block.content}
            for block in tool_results
        ]

    text = "\n".join(b.text for b in message.content if isinstance(b, TextBlock) and b.text)
    tool_uses = [b for b in message.content if isinstance(b, ToolUseBlock)]

    if message.role == "assistant" and tool_uses:
        import json

        return [
            {
                "role": "assistant",
                "content": text or None,
                "tool_calls": [
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input, ensure_ascii=False),
                        },
                    }
                    for block in tool_uses
                ],
            }
        ]
    return [{"role": message.role, "content": text}]


def _parse_arguments(arguments: Any) -> dict[str, Any]:
    import json

    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise errors.MalformedResponse(f"tool call arguments were not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise errors.MalformedResponse(f"tool call arguments were not an object: {parsed!r}")
    return parsed


def _parse_usage(raw: Any) -> Usage:
    """Normalise usage. Field names and cached-token conventions differ per provider.

    DeepSeek reports cached tokens *inside* `prompt_tokens`, so the cached portion is
    subtracted out — otherwise cached tokens would be billed at the full input rate
    and the cache would appear to save nothing.

    Two representations of the same number are read, because DeepSeek returns both:
    its own `prompt_cache_hit_tokens` and the OpenAI-style
    `prompt_tokens_details.cached_tokens`. Reading only one means that if the provider
    ever drops that field, this silently returns zero cached tokens and starts billing
    them at full rate — a costing error with no error message.
    """
    usage = getattr(raw, "usage", None)
    if usage is None:
        return Usage()
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)

    cache_hit = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
    if not cache_hit:
        details = getattr(usage, "prompt_tokens_details", None)
        cache_hit = int(getattr(details, "cached_tokens", 0) or 0)

    details = getattr(usage, "prompt_tokens_details", None)
    cache_write = int(getattr(details, "cache_write_tokens", 0) or 0)

    return Usage(
        input_tokens=max(prompt - cache_hit, 0),
        output_tokens=completion,
        cache_read_tokens=cache_hit,
        cache_write_tokens=cache_write,
    )


def _status_of(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _retry_after_of(exc: Exception) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _looks_like_timeout(exc: Exception) -> bool:
    import asyncio

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    name = type(exc).__name__.lower()
    return "timeout" in name or "timeout" in str(exc).lower()


# --------------------------------------------------------------------------- #
# Streaming. A side channel for latency, not a different result type: the stream
# still ends with a complete Response, because the loop needs assembled tool_use
# blocks to dispatch and the cache needs something to store. This is why the LLM
# protocol signature never changed to accommodate it.
# --------------------------------------------------------------------------- #


class _ToolCallAccumulator:
    """Reassembles tool calls from deltas.

    Streamed tool calls arrive as fragments — the name in one chunk, the JSON
    arguments split across several — so nothing can be parsed until the stream ends.
    That is precisely why `StreamDone` carries the assembled `Response` rather than
    the caller being expected to stitch chunks together.
    """

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, Any]] = {}

    def add(self, delta: Any) -> None:
        for call in getattr(delta, "tool_calls", None) or []:
            index = getattr(call, "index", 0) or 0
            slot = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if getattr(call, "id", None):
                slot["id"] = call.id
            function = getattr(call, "function", None)
            if function is not None:
                if getattr(function, "name", None):
                    slot["name"] = function.name
                if getattr(function, "arguments", None):
                    slot["arguments"] += function.arguments

    def blocks(self) -> list[ToolUseBlock]:
        return [
            ToolUseBlock(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                input=_parse_arguments(slot["arguments"]),
            )
            for index, slot in sorted(self._calls.items())
        ]

    def __bool__(self) -> bool:
        return bool(self._calls)


class StreamingOpenAICompatAdapter(OpenAICompatAdapter):
    """Adds `stream()`. Separate class so the non-streaming path stays the simple
    one and `Transport.call` keeps working with any adapter."""

    async def stream(self, request: LLMRequest):
        from .transport import StreamDone, TextChunk

        payload = self.render(request)
        payload["stream"] = True
        # Ask for usage in the final chunk; providers that ignore this option simply
        # report nothing, and the fallback below keeps accounting from silently
        # becoming zero.
        payload["stream_options"] = {"include_usage": True}

        text_parts: list[str] = []
        tool_calls = _ToolCallAccumulator()
        finish_reason = ""
        usage = Usage()

        try:
            stream = await self.client.create(**payload)
            async for event in stream:
                reported = _parse_usage(event)
                if reported.total:
                    usage = reported
                for choice in getattr(event, "choices", None) or []:
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    piece = getattr(delta, "content", None)
                    if piece:
                        text_parts.append(piece)
                        yield TextChunk(text=piece)
                    tool_calls.add(delta)
                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason
        except errors.ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self.classify(exc) from exc

        content: list[Any] = []
        if text_parts:
            content.append(TextBlock(text="".join(text_parts)))
        content.extend(tool_calls.blocks())

        stop = _STOP_REASONS.get(finish_reason, StopReason.END_TURN)
        if tool_calls:
            stop = StopReason.TOOL_USE

        yield StreamDone(
            response=Response(stop_reason=stop, content=content), usage=usage
        )
