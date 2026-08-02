"""Request assembly — construction layer.

This module owns the provider-agnostic request and, with it, **`cache_control`
breakpoint placement** — the single highest-leverage cost decision in the gateway
(60-70% of input tokens on providers that honour explicit markers). The original
four-layer sketch scoped construction to "register provider config", which left
that item without an owner; see [TRADEOFFS §33](../../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators)
delta 4.

The layering is dictated by how prompt caching works: **a cache prefix must be a
literal prefix**, so fragments are ordered most-static-first and a breakpoint is
placed at the end of each stable run.

    [A] methodology      global, static          ┐ one cache entry
    [B] output contract  global, static          ┘ for the whole project
    [C] integration      per integration         → one entry per integration (~5)
    [D] budget/window    per investigation       → never cached

Provider asymmetry worth stating plainly: Anthropic needs explicit markers,
DeepSeek caches prefixes automatically. **The ordering pays off on both** — only
one of them needs the annotation. A design that treated caching as
Anthropic-specific would have got the ordering wrong for DeepSeek and silently
lost the discount.

W3 L1 authors the fragment *content*; this module defines the structure so that
lesson has somewhere to put it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from .types import Message

#: Anthropic honours at most four `cache_control` breakpoints per request. Two are
#: enough for the layering above (after [B], after [C]); the cap is recorded so a
#: future fifth fragment does not silently drop one.
MAX_CACHE_BREAKPOINTS = 4


@dataclass(frozen=True)
class PromptFragment:
    """One layer of the system prompt.

    `stable_across` documents *what* the fragment is constant with respect to, and
    is what determines breakpoint placement:

    - ``"project"`` — identical for every call ever made
    - ``"integration"`` — identical for every investigation of one integration
    - ``"investigation"`` — varies per run; never a cache prefix
    """

    name: str
    text: str
    stable_across: str = "investigation"


@dataclass(frozen=True)
class SystemPrompt:
    fragments: tuple[PromptFragment, ...] = ()

    @classmethod
    def of(cls, *fragments: PromptFragment) -> SystemPrompt:
        return cls(fragments=tuple(fragments))

    def ordered(self) -> tuple[PromptFragment, ...]:
        """Fragments most-static-first.

        A stable sort keyed on stability rank, so authored order is preserved
        inside a rank — `[A]` stays before `[B]` while both remain project-stable.
        """
        rank = {"project": 0, "integration": 1, "investigation": 2}
        return tuple(sorted(self.fragments, key=lambda f: rank.get(f.stable_across, 99)))

    def breakpoint_indices(self) -> tuple[int, ...]:
        """Indices (into `ordered()`) after which a cache breakpoint belongs.

        One at the end of each stable run, excluding the per-investigation tail —
        marking a fragment that changes every run would write a cache entry that
        is never read, which costs the write premium for nothing.
        """
        ordered = self.ordered()
        marks: list[int] = []
        for i, frag in enumerate(ordered):
            if frag.stable_across == "investigation":
                continue
            is_last_of_run = i + 1 == len(ordered) or ordered[i + 1].stable_across != frag.stable_across
            if is_last_of_run:
                marks.append(i)
        return tuple(marks[:MAX_CACHE_BREAKPOINTS])

    def text(self) -> str:
        return "\n\n".join(f.text for f in self.ordered() if f.text)


@dataclass(frozen=True)
class LLMRequest:
    """A provider-agnostic call, ready for an adapter to render.

    The cache key is computed from *this*, not from a rendered provider payload,
    so provider formatting differences do not fragment the cache — and so the same
    logical call made against two providers is recognisably the same call.
    """

    model_id: str
    messages: tuple[Message, ...]
    tools: tuple[dict[str, Any], ...] = ()
    system: SystemPrompt = field(default_factory=SystemPrompt)
    max_tokens: int = 4096
    temperature: float = 0.0
    #: Free-form extras an adapter may need (e.g. `thinking` budget). Included in
    #: the cache key, because anything that changes the output must.
    params: tuple[tuple[str, Any], ...] = ()

    def cache_key(self) -> str:
        """SHA-256 over everything that can change the response.

        Tool schemas are **included deliberately**. Omitting them would let a
        changed tool set hit a stale entry, and a wrong-but-cheap answer is the
        worst outcome for an evaluation system. The consequence — adding a tool
        invalidates the cache — is correct behaviour: adding a tool *should*
        require re-running eval.
        """
        payload = {
            "model": self.model_id,
            "system": [(f.name, f.text) for f in self.system.ordered()],
            "messages": [_message_repr(m) for m in self.messages],
            "tools": list(self.tools),
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "params": list(self.params),
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _message_repr(message: Message) -> dict[str, Any]:
    """Stable, content-complete representation for hashing.

    Deliberately explicit rather than `dataclasses.asdict`: a future field added
    to a block type should force a decision about whether it affects the response,
    instead of silently changing every cache key or silently being ignored.
    """
    blocks: list[dict[str, Any]] = []
    for block in message.content:
        kind = getattr(block, "type", block.__class__.__name__)
        if kind == "text":
            blocks.append({"t": "text", "text": block.text})
        elif kind == "tool_use":
            blocks.append(
                {"t": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
        elif kind == "tool_result":
            blocks.append(
                {
                    "t": "tool_result",
                    "id": block.tool_use_id,
                    "content": block.content,
                    "is_error": block.is_error,
                }
            )
        else:  # pragma: no cover — a new block type should fail loudly here
            raise TypeError(f"cannot hash unknown content block: {block!r}")
    return {"role": message.role, "content": blocks}


def build(
    model_id: str,
    messages: Sequence[Message],
    tools: Sequence[dict[str, Any]] = (),
    system: SystemPrompt | None = None,
    **params: Any,
) -> LLMRequest:
    known = {"max_tokens", "temperature"}
    extras = tuple(sorted((k, v) for k, v in params.items() if k not in known))
    return LLMRequest(
        model_id=model_id,
        messages=tuple(messages),
        tools=tuple(tools),
        system=system or SystemPrompt(),
        max_tokens=params.get("max_tokens", 4096),
        temperature=params.get("temperature", 0.0),
        params=extras,
    )
