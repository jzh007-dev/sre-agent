"""Anthropic adapter — the different-family class.

It exists because [EVAL.md](../../EVAL.md) requires a judge from a different model
family than the agent and [SECURITY.md](../../SECURITY.md) L3 requires a
different-family reviewer. That is a **design requirement**, which is what makes
multi-provider a necessity here rather than a nice-to-have — see
[§5's revision](../../TRADEOFFS.md#5-model-routing-3-tier-haiku--sonnet--opus).
It is deliberately not prompt-tuned until W6; quality parity across providers is
explicitly not promised.

The codec is nearly an identity map, because `llm/types.py` was modelled on the
Messages API in the first place. Two things are genuinely this adapter's own:

- **`cache_control` breakpoints.** The one provider here that honours explicit
  markers, so this is where the 60-70% input-token saving is actually claimed.
- **Its own error shapes**, including 529 `overloaded_error`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from . import errors
from .provider_catalog import ModelSpec, ProviderSpec
from .request import LLMRequest
from .types import Message, Response, StopReason, TextBlock, ToolResultBlock, ToolUseBlock
from .usage import Usage


class MessagesClient(Protocol):
    """The slice of the `anthropic` SDK this adapter uses."""

    async def create(self, **payload: Any) -> Any: ...


@dataclass
class AnthropicAdapter:
    provider_spec: ProviderSpec
    model_spec: ModelSpec
    client: MessagesClient

    @property
    def provider(self) -> str:
        return self.provider_spec.name

    # ---------------------------------------------------------------- outbound

    def render(self, request: LLMRequest) -> dict[str, Any]:
        """Build the payload, placing cache breakpoints at stable-run boundaries.

        The system prompt goes out as a *list of blocks* rather than one string,
        because a breakpoint is an annotation on a block. One string would make the
        whole prompt a single cache unit, so any per-investigation content would
        invalidate the project-stable methodology on every call — which is exactly
        the mistake the layering in `request.py` exists to prevent.
        """
        fragments = request.system.ordered()
        breakpoints = set(request.system.breakpoint_indices())

        system_blocks: list[dict[str, Any]] = []
        for index, fragment in enumerate(fragments):
            if not fragment.text:
                continue
            block: dict[str, Any] = {"type": "text", "text": fragment.text}
            if index in breakpoints and self.model_spec.supports_explicit_cache:
                block["cache_control"] = {"type": "ephemeral"}
            system_blocks.append(block)

        payload: dict[str, Any] = {
            "model": request.model_id,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [_to_anthropic_message(m) for m in request.messages],
        }
        if system_blocks:
            payload["system"] = system_blocks
        if request.tools:
            payload["tools"] = [dict(tool) for tool in request.tools]
        payload.update(dict(request.params))
        return payload

    # ----------------------------------------------------------------- inbound

    async def send(self, request: LLMRequest) -> tuple[Response, Usage]:
        try:
            raw = await self.client.create(**self.render(request))
        except errors.ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 — deliberate: classify everything
            raise self.classify(exc) from exc
        return self.parse(raw)

    def parse(self, raw: Any) -> tuple[Response, Usage]:
        try:
            content: list[Any] = []
            for block in raw.content:
                kind = getattr(block, "type", "")
                if kind == "text":
                    content.append(TextBlock(text=block.text))
                elif kind == "tool_use":
                    content.append(
                        ToolUseBlock(id=block.id, name=block.name, input=dict(block.input))
                    )
                # `thinking` blocks and any future type are dropped rather than
                # raising: an unrecognised block is not a failure, and the loop only
                # keys on text and tool_use.
            stop = _STOP_REASONS.get(getattr(raw, "stop_reason", ""), StopReason.END_TURN)
            return Response(stop_reason=stop, content=content), _parse_usage(raw)
        except errors.ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise errors.MalformedResponse(
                f"could not parse response: {type(exc).__name__}: {exc}",
                provider=self.provider,
            ) from exc

    # -------------------------------------------------------- classification

    def classify(self, exc: Exception) -> errors.ProviderError:
        status = _status_of(exc)
        retry_after = _retry_after_of(exc)
        message = f"{type(exc).__name__}: {exc}"
        text = str(exc).lower()

        if status in (401, 403):
            return errors.AuthError(message, provider=self.provider, status=status)
        if status == 429:
            return errors.RateLimit(
                message, provider=self.provider, status=status, retry_after=retry_after
            )
        if status == 529 or "overloaded" in text:
            return errors.Overloaded(
                message, provider=self.provider, status=status, retry_after=retry_after
            )
        if status == 400:
            if "prompt is too long" in text or "max_tokens" in text and "context" in text:
                return errors.ContextLimit(message, provider=self.provider, status=status)
            return errors.InvalidRequest(message, provider=self.provider, status=status)
        if status is not None and 500 <= status < 600:
            return errors.ServerError(
                message, provider=self.provider, status=status, retry_after=retry_after
            )
        if _looks_like_timeout(exc):
            return errors.Timeout(message, provider=self.provider)
        return errors.ServerError(message, provider=self.provider, status=status)


_STOP_REASONS = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.END_TURN,
}


def _to_anthropic_message(message: Message) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            blocks.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            blocks.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
        elif isinstance(block, ToolResultBlock):
            entry: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": block.content,
            }
            if block.is_error:
                entry["is_error"] = True
            blocks.append(entry)
    return {"role": message.role, "content": blocks}


def _parse_usage(raw: Any) -> Usage:
    """Anthropic reports cache tokens separately from `input_tokens`, so unlike the
    OpenAI-compatible path there is nothing to subtract out."""
    usage = getattr(raw, "usage", None)
    if usage is None:
        return Usage()
    return Usage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    )


def _status_of(exc: Exception) -> int | None:
    for attr in ("status_code", "status"):
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
    return "timeout" in type(exc).__name__.lower() or "timeout" in str(exc).lower()
