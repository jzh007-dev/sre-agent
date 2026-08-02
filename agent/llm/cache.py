"""Response cache — a cross-cutting decorator around transport.

Two jobs, and the second is the one that is easy to get wrong.

**Affordability.** Re-running ~30 golden cases nightly, times a configuration
sweep, is the dominant cost of this project. Only the calls a change actually
affects should be recomputed.

**Reproducibility.** [EVAL.md](../../EVAL.md) principle 5 requires a run to be
deterministic given `(seed, model_version, prompt_version, golden_set_version)`.
LLM calls are not deterministic, so without this cache that principle is
decorative rather than achievable.

The subtle part: **a cache hit still charges the investigation's budget.** A hit
costs no money, so gating budget before the cache would refuse free calls — but if
hits were also free of *budget*, a run that degraded on budget exhaustion would
stop degrading on rerun, and the run would no longer reproduce. So each entry
stores the original call's usage and cost, and a hit replays that charge. Money is
genuinely saved; the ledger stays faithful; degradation reproduces. See
[TRADEOFFS §33](../../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators)
delta 3.

The key comes from `LLMRequest.cache_key()`, which includes the tool schemas —
omitting them would let a changed tool set hit a stale entry, and wrong-but-cheap
is the worst possible outcome for an evaluation system.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from .types import Message, Response, StopReason, TextBlock, ToolResultBlock, ToolUseBlock
from .usage import PRICE_TABLE_VERSION, Cost, Usage


@dataclass(frozen=True)
class CacheEntry:
    """A stored response plus what it originally cost.

    `cost` and `usage` are the replay payload — without them a hit could not charge
    the budget, and the reproducibility guarantee above would not hold. The cost is
    stored in the provider's native currency plus the FX rate that applied, so a
    replay is exact rather than re-converted at today's rate.
    `price_table_version` is recorded so an entry priced under an older table is
    identifiable rather than silently mixed into a cost total.
    """

    response: Response
    usage: Usage
    #: The original call's cost, in both currencies. Replaying the native amount is
    #: what keeps a rerun's accounting identical even if FX has since moved.
    cost: Cost
    model_id: str
    price_table_version: str = PRICE_TABLE_VERSION


class CacheStore(Protocol):
    def get(self, key: str) -> CacheEntry | None: ...
    def put(self, key: str, entry: CacheEntry) -> None: ...


class MemoryStore:
    """Process-lifetime store. The default for tests and for a single eval run."""

    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}

    def get(self, key: str) -> CacheEntry | None:
        return self._entries.get(key)

    def put(self, key: str, entry: CacheEntry) -> None:
        self._entries[key] = entry

    def __len__(self) -> int:
        return len(self._entries)


class FileStore:
    """JSONL-backed store, so cache reuse survives across processes.

    Cross-session reuse is the point: the expensive scenario is not one eval run,
    it is the same run repeated after a one-line prompt edit. Redis as an L2 is a
    W4 concern; a file is adequate at this scale and has no operational cost.

    Append-only with a last-wins in-memory index — an append log is safe against a
    crash mid-write, and rewriting a large JSON blob on every put is not.
    """

    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        self._index: dict[str, CacheEntry] = {}
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                self._index[record["key"]] = _entry_from_json(record["entry"])
            except (json.JSONDecodeError, KeyError, TypeError):
                # A truncated final line from an interrupted write is expected;
                # skipping it is correct and a corrupt cache must never be fatal.
                continue

    def get(self, key: str) -> CacheEntry | None:
        return self._index.get(key)

    def put(self, key: str, entry: CacheEntry) -> None:
        self._index[key] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"key": key, "entry": _entry_to_json(entry)}, ensure_ascii=False) + "\n")

    def __len__(self) -> int:
        return len(self._index)


@dataclass
class ResponseCache:
    """Thin policy wrapper over a store, with hit/miss counters for the exit table."""

    store: CacheStore
    enabled: bool = True
    hits: int = 0
    misses: int = 0

    def get(self, key: str) -> CacheEntry | None:
        if not self.enabled:
            return None
        entry = self.store.get(key)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def put(self, key: str, entry: CacheEntry) -> None:
        if self.enabled:
            self.store.put(key, entry)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


# --------------------------------------------------------------------------- #
# Serialization. Hand-written rather than pickled: the cache is a file a human
# should be able to read when a result looks wrong, and a pickle would couple the
# on-disk format to the exact class layout.
# --------------------------------------------------------------------------- #


def _entry_to_json(entry: CacheEntry) -> dict[str, Any]:
    return {
        "response": {
            "stop_reason": entry.response.stop_reason.value,
            "content": [_block_to_json(b) for b in entry.response.content],
        },
        "usage": asdict(entry.usage),
        "cost": asdict(entry.cost),
        "model_id": entry.model_id,
        "price_table_version": entry.price_table_version,
    }


def _entry_from_json(raw: dict[str, Any]) -> CacheEntry:
    return CacheEntry(
        response=Response(
            stop_reason=StopReason(raw["response"]["stop_reason"]),
            content=[_block_from_json(b) for b in raw["response"]["content"]],
        ),
        usage=Usage(**raw["usage"]),
        cost=_cost_from_json(raw),
        model_id=raw["model_id"],
        price_table_version=raw.get("price_table_version", "unknown"),
    )


def _cost_from_json(raw: dict[str, Any]) -> Cost:
    """Read a stored cost, tolerating entries written before costs were bicurrency.

    An older entry holds a bare `cost_usd`; treating it as USD-native with fx 1.0 is
    exactly what it meant at the time, so old cache files stay usable instead of
    being silently discarded (which would look like a cache that never hits).
    """
    if "cost" in raw:
        return Cost(**raw["cost"])
    return Cost(native=raw.get("cost_usd", 0.0), currency="USD", fx_to_usd=1.0)


def _block_to_json(block: Any) -> dict[str, Any]:
    kind = getattr(block, "type", "")
    if kind == "text":
        return {"t": "text", "text": block.text}
    if kind == "tool_use":
        return {"t": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if kind == "tool_result":
        return {
            "t": "tool_result",
            "id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    raise TypeError(f"cannot serialize content block: {block!r}")


def _block_from_json(raw: dict[str, Any]) -> Any:
    kind = raw["t"]
    if kind == "text":
        return TextBlock(text=raw["text"])
    if kind == "tool_use":
        return ToolUseBlock(id=raw["id"], name=raw["name"], input=raw["input"])
    if kind == "tool_result":
        return ToolResultBlock(
            tool_use_id=raw["id"], content=raw["content"], is_error=raw.get("is_error", False)
        )
    raise TypeError(f"cannot deserialize content block: {raw!r}")


__all__ = [
    "CacheEntry",
    "CacheStore",
    "FileStore",
    "MemoryStore",
    "ResponseCache",
    "Message",
]
