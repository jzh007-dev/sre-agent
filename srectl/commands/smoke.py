"""`srectl smoke` — the L3b live check.

One real call per credentialled provider, then report what the gateway measured.
Three things are being verified, and only the first is about "does it work":

1. **The wiring**: routing → construction → transport → parse produces a usable
   `Response`.
2. **The usage fields**: each provider reports tokens under its own field names, and
   the adapter's normalisation is guesswork until seen against a real payload. This
   is what lets `cost_usd` stop being labelled unverified.
3. **The prompt cache**: the second identical call should report cached input
   tokens. That is the number behind the claim that breakpoint ordering is the
   highest-leverage cost decision in the gateway — and if it comes back zero, the
   claim is wrong and we say so.

Run: `.venv/bin/python -m srectl smoke`
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone

from agent.core.investigation import Investigation, ToolBudget, Window
from agent.llm.clients import env_summary, live_gateway, load_env, smoke_routing
from agent.llm.cost import Ledger
from agent.llm.provider_catalog import MODELS, model
from agent.llm.request import PromptFragment, SystemPrompt
from agent.llm.routing import CallKind
from agent.llm.types import Message, TextBlock
from agent.llm.usage import cost_of

#: Long enough that a prompt cache has something to hit on the second call —
#: providers set a minimum cacheable prefix (commonly ~1k tokens), so a short
#: prompt would report zero cached tokens and look like a broken cache.
_FILLER = (
    "You are diagnosing production incidents. Establish a timeline before forming "
    "a hypothesis. Prefer metrics over logs, and logs over runbook inference. "
    "State plainly when evidence is correlational rather than causal. "
)


def _system() -> SystemPrompt:
    return SystemPrompt.of(
        PromptFragment("methodology", _FILLER * 40, stable_across="project"),
        PromptFragment("contract", "Answer in one short sentence.", stable_across="project"),
        PromptFragment("integration", "Signals: Prometheus, ClickHouse.", stable_across="integration"),
    )


def _investigation() -> Investigation:
    return Investigation(
        id="inv_smoke",
        trigger="alert",
        window=Window.around(datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)),
        budget=ToolBudget(max_cost_usd=0.50),
    )


async def _one_provider(model_id: str, stream: bool) -> dict:
    traces: list[dict] = []
    gateway = live_gateway(
        routing=smoke_routing(model_id),
        tracer=traces.append,
        # Fallback off: this is a check of one provider, and a silent switch would
        # make the report describe a provider we did not intend to test.
        allow_fallback=False,
    )
    spec = model(model_id)
    if spec.provider not in gateway.transports:
        return {"model": model_id, "skipped": "no credentials"}

    inv = _investigation()
    ledger = Ledger(investigation_id=inv.id)
    llm = gateway.bind(inv, CallKind.MAIN_LOOP, system=_system(), ledger=ledger)
    question = [Message(role="user", content=[TextBlock(text="Name one cause of a 5xx spike.")])]

    first = await llm.call(question, [])

    # Second call, identical prompt but a different question, so the *prefix* is
    # reused while the response cache is bypassed. That isolates the provider's
    # prompt cache from ours — otherwise a hit in our own cache would look like a
    # provider cache hit and prove nothing.
    second_question = [
        Message(role="user", content=[TextBlock(text="Name another cause of a 5xx spike.")])
    ]
    second = await llm.call(second_question, [])

    streamed_chunks = 0
    if stream:
        streamed_chunks = await _stream_once(gateway, spec, question)

    entries = [e for e in ledger.entries if not e.cached]
    return {
        "model": model_id,
        "provider": spec.provider,
        "first_answer": _first_text(first)[:120],
        "second_answer": _first_text(second)[:120],
        "calls": ledger.calls,
        "usage": [
            {
                "input": e.usage.input_tokens,
                "output": e.usage.output_tokens,
                "cache_read": e.usage.cache_read_tokens,
                "cache_write": e.usage.cache_write_tokens,
                "cost_usd": round(e.cost_usd, 6),
                "attempts": e.attempts,
            }
            for e in entries
        ],
        "prompt_cache_hit_on_second_call": bool(
            len(entries) > 1 and entries[1].usage.cache_read_tokens > 0
        ),
        "ledger": ledger.summary(),
        "traces": len(traces),
        "streamed_chunks": streamed_chunks,
    }


async def _stream_once(gateway, spec, messages) -> int:
    """Exercise the streaming path if the wired adapter supports it."""
    from agent.llm.openai_compat import OpenAICompatAdapter, StreamingOpenAICompatAdapter
    from agent.llm.request import build
    from agent.llm.transport import StreamDone, TextChunk

    transport = gateway.transports[spec.provider]
    adapter = transport.adapter
    if isinstance(adapter, OpenAICompatAdapter) and not hasattr(adapter, "stream"):
        adapter = StreamingOpenAICompatAdapter(
            provider_spec=adapter.provider_spec,
            model_spec=adapter.model_spec,
            client=adapter.client,
        )
        transport.adapter = adapter

    if not hasattr(transport.adapter, "stream"):
        return 0

    request = build(spec.id, messages, system=_system(), max_tokens=200)
    chunks = 0
    async for chunk in transport.stream(request):
        if isinstance(chunk, TextChunk):
            chunks += 1
        elif isinstance(chunk, StreamDone):
            break
    return chunks


def _first_text(response) -> str:
    for block in response.content:
        text = getattr(block, "text", None)
        if text:
            return text
    return "(no text)"


def _price_check(result: dict) -> list[str]:
    """Recompute cost from reported usage and compare against the ledger.

    A mismatch means the catalogue price and the accounting disagree — which is the
    one bug in a cost pipeline that produces a confident wrong number rather than an
    error.
    """
    notes: list[str] = []
    spec = model(result["model"])
    from agent.llm.usage import Usage

    for entry in result["usage"]:
        usage = Usage(
            input_tokens=entry["input"],
            output_tokens=entry["output"],
            cache_read_tokens=entry["cache_read"],
            cache_write_tokens=entry["cache_write"],
        )
        expected = round(cost_of(usage, spec.price), 6)
        if abs(expected - entry["cost_usd"]) > 1e-9:
            notes.append(f"cost mismatch: ledger {entry['cost_usd']} vs recomputed {expected}")
    if not spec.price.verified:
        notes.append(
            f"price table {spec.price.as_of} is UNVERIFIED — compare the token counts "
            f"above against {spec.provider}'s published pricing, then set verified=True"
        )
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="srectl smoke", description=__doc__)
    parser.add_argument(
        "--model",
        action="append",
        default=None,
        help="model id to smoke (repeatable). Defaults to every credentialled provider.",
    )
    parser.add_argument("--stream", action="store_true", help="also exercise the streaming path")
    args = parser.parse_args(argv)

    # Load .env before reporting, or the summary describes the process environment
    # rather than the configuration actually about to be used — which reads as "no
    # credentials" immediately above a successful call.
    load_env()
    print("credentials:", json.dumps(env_summary()))

    if args.model:
        targets = args.model
    else:
        # One model per provider — the workhorse tier, since that is what the loop
        # actually routes to.
        seen: dict[str, str] = {}
        for model_id, spec in MODELS.items():
            if spec.tier == "workhorse":
                seen.setdefault(spec.provider, model_id)
        targets = list(seen.values())

    failures = 0
    skipped = 0
    for model_id in targets:
        print(f"\n=== {model_id}")
        try:
            result = asyncio.run(_one_provider(model_id, args.stream))
        except Exception as exc:  # noqa: BLE001 — a smoke script reports, it does not raise
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            failures += 1
            continue

        if result.get("skipped"):
            print(f"  skipped: {result['skipped']}")
            skipped += 1
            continue

        print(f"  answer 1: {result['first_answer']}")
        print(f"  answer 2: {result['second_answer']}")
        for i, entry in enumerate(result["usage"], 1):
            print(
                f"  call {i}: in={entry['input']} out={entry['output']} "
                f"cache_read={entry['cache_read']} cache_write={entry['cache_write']} "
                f"cost=${entry['cost_usd']} attempts={entry['attempts']}"
            )
        print(f"  provider prompt cache hit on 2nd call: {result['prompt_cache_hit_on_second_call']}")
        if result["streamed_chunks"]:
            print(f"  streamed text chunks: {result['streamed_chunks']}")
        print(f"  ledger: {json.dumps(result['ledger'])}")
        for note in _price_check(result):
            print(f"  NOTE: {note}")

    attempted = len(targets) - skipped
    reached = attempted - failures
    print(f"\n{reached}/{attempted} credentialled providers reached ({skipped} skipped, no key)")
    # A run where every credentialled provider failed is a failure; a run where
    # everything was skipped is a configuration problem, and both should be visible
    # in the exit code rather than only in the text above.
    if attempted == 0:
        print("no credentialled providers — set a key in .env")
        return 2
    return 1 if reached == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
