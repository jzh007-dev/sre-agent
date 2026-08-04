"""Cache mode and TTL — the gap [TRADEOFFS §40](../../TRADEOFFS.md) exposed.

§40's analysis of three cache semantics found that `ResponseCache` had **no TTL and
no eval/production distinction**, and that those two want opposite things: for eval an
unbounded cache *is* the reproducibility guarantee, while in production an identical
prompt a week later would hit a cache the world has moved past.

The clock is injected, as `transport.py` injects its sleeper. A TTL test that sleeps
is a test nobody runs.
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from agent.llm.cache import (
    DEFAULT_PRODUCTION_TTL,
    CacheEntry,
    FileStore,
    MemoryStore,
    ResponseCache,
)
from agent.llm.types import Response, StopReason, TextBlock
from agent.llm.usage import Cost, Usage


def entry(stored_at: float = 0.0) -> CacheEntry:
    return CacheEntry(
        response=Response(stop_reason=StopReason.END_TURN, content=[TextBlock(text="hi")]),
        usage=Usage(input_tokens=10, output_tokens=2),
        cost=Cost(native=0.001, currency="CNY"),
        model_id="deepseek-v4-flash",
        stored_at=stored_at,
    )


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class TestEvalMode(unittest.TestCase):
    def test_nothing_ever_expires(self):
        """EVAL.md principle 5 makes a run reproducible given
        `(seed, model_version, prompt_version, golden_set_version)`. An expiring cache
        would make it reproducible given those *and the date*."""
        clock = FakeClock()
        cache = ResponseCache.for_mode(MemoryStore(), "eval", clock=clock)
        cache.put("k", entry())
        clock.advance(86_400 * 30)
        self.assertIsNotNone(cache.get("k"))
        self.assertEqual(cache.expirations, 0)

    def test_eval_is_the_default(self):
        self.assertEqual(ResponseCache(store=MemoryStore()).mode, "eval")
        self.assertIsNone(ResponseCache(store=MemoryStore()).ttl)


class TestProductionMode(unittest.TestCase):
    def test_a_fresh_entry_still_hits(self):
        """Inside one investigation an identical prompt is a retry or a parallel
        duplicate, and reusing the answer is correct."""
        clock = FakeClock()
        cache = ResponseCache.for_mode(MemoryStore(), "production", clock=clock)
        cache.put("k", entry())
        clock.advance(60)
        self.assertIsNotNone(cache.get("k"))

    def test_an_aged_entry_is_a_miss(self):
        clock = FakeClock()
        cache = ResponseCache.for_mode(MemoryStore(), "production", clock=clock)
        cache.put("k", entry())
        clock.advance(DEFAULT_PRODUCTION_TTL + 1)
        self.assertIsNone(cache.get("k"))

    def test_expirations_are_counted_apart_from_misses(self):
        """Without the split, a TTL set too short looks exactly like a
        prefix-ordering bug: both show up only as a fallen hit rate."""
        clock = FakeClock()
        cache = ResponseCache.for_mode(MemoryStore(), "production", clock=clock)
        cache.put("k", entry())
        clock.advance(DEFAULT_PRODUCTION_TTL + 1)
        cache.get("k")
        cache.get("never-stored")

        self.assertEqual(cache.expirations, 1)
        self.assertEqual(cache.misses, 2, "an expiry is also a miss for the hit rate")
        self.assertEqual(cache.stats()["mode"], "production")

    def test_the_ttl_is_overridable_without_leaving_the_mode(self):
        clock = FakeClock()
        cache = ResponseCache.for_mode(MemoryStore(), "production", ttl=5, clock=clock)
        cache.put("k", entry())
        clock.advance(6)
        self.assertIsNone(cache.get("k"))


class TestDisabledMode(unittest.TestCase):
    def test_nothing_is_stored_or_served(self):
        cache = ResponseCache.for_mode(MemoryStore(), "disabled")
        cache.put("k", entry())
        self.assertIsNone(cache.get("k"))
        self.assertEqual(len(cache.store), 0)  # type: ignore[arg-type]


class TestTimestamping(unittest.TestCase):
    def test_put_stamps_an_entry_that_has_no_timestamp(self):
        """A cache whose TTL depends on each caller remembering to timestamp is a
        cache with no TTL."""
        clock = FakeClock(500.0)
        cache = ResponseCache.for_mode(MemoryStore(), "production", clock=clock)
        cache.put("k", entry())
        stored = cache.store.get("k")
        assert stored is not None
        self.assertEqual(stored.stored_at, 500.0)

    def test_an_explicit_timestamp_is_preserved(self):
        cache = ResponseCache.for_mode(MemoryStore(), "production", clock=FakeClock())
        cache.put("k", entry(stored_at=42.0))
        stored = cache.store.get("k")
        assert stored is not None
        self.assertEqual(stored.stored_at, 42.0)

    def test_a_timestamp_survives_the_file_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "cache.jsonl"
            clock = FakeClock(777.0)
            ResponseCache.for_mode(FileStore(path), "production", clock=clock).put(
                "k", entry()
            )
            reread = FileStore(path).get("k")
            assert reread is not None
            self.assertEqual(reread.stored_at, 777.0)


class TestLegacyEntries(unittest.TestCase):
    """Files written before entries were timestamped stay readable — the same
    tolerance `_cost_from_json` already applies to pre-bicurrency costs. Discarding
    them silently would look like a cache that never hits."""

    def _legacy_file(self, tmp: str) -> pathlib.Path:
        path = pathlib.Path(tmp) / "legacy.jsonl"
        record = {
            "key": "k",
            "entry": {
                "response": {"stop_reason": "end_turn", "content": [{"t": "text", "text": "hi"}]},
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "cost_usd": 0.002,
                "model_id": "deepseek-v4-flash",
            },
        }
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        return path

    def test_a_legacy_entry_reads_as_age_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            stored = FileStore(self._legacy_file(tmp)).get("k")
            assert stored is not None
            self.assertEqual(stored.stored_at, 0.0)
            self.assertEqual(stored.cost.native, 0.002)

    def test_it_is_kept_in_eval_and_refused_in_production(self):
        """Age-infinite, not brand-new: serve it where reuse is the point, refuse it
        where freshness is."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._legacy_file(tmp)
            self.assertIsNotNone(
                ResponseCache.for_mode(FileStore(path), "eval").get("k")
            )
            self.assertIsNone(
                ResponseCache.for_mode(FileStore(path), "production").get("k")
            )


if __name__ == "__main__":
    unittest.main()
