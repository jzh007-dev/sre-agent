"""Price staleness and billing reconciliation.

These exist because a price table checked into a repository is wrong the moment the
provider changes its rates, and nothing else in the code would notice. Every cost
figure would stay confidently, silently incorrect — which is worse than an error,
because it looks like a measurement.
"""
from __future__ import annotations

import unittest

from agent.llm.cost import Ledger
from agent.llm.provider_catalog import model
from agent.llm.reconcile import (
    DEFAULT_MIN_SPEND_USD,
    ReconcileUnavailable,
    Reconciliation,
    reader_for,
)
from agent.llm.usage import PRICE_TABLE_MAX_AGE_DAYS, Price, Usage, cache_savings, cost_of


class TestPriceStaleness(unittest.TestCase):
    def test_stale_after_is_as_of_plus_the_window(self):
        price = Price(input=1.0, output=2.0, as_of="2026-01-01")
        self.assertEqual(price.stale_after, "2026-04-01")  # +90 days

    def test_a_fresh_table_is_not_stale(self):
        price = Price(input=1.0, output=2.0, as_of="2026-08-02")
        self.assertFalse(price.is_stale("2026-08-03"))

    def test_an_aged_table_is_stale_even_if_it_was_verified(self):
        """Verified-when-written is not verified-now. Providers change rates on
        their own schedule, so age alone is grounds for re-checking."""
        price = Price(input=1.0, output=2.0, as_of="2026-01-01", verified=True)
        self.assertTrue(price.is_stale("2026-06-01"))

    def test_a_version_suffix_is_tolerated(self):
        price = Price(input=1.0, output=2.0, as_of="2026-08-02.unverified")
        self.assertEqual(price.stale_after, "2026-10-31")

    def test_an_unparseable_version_is_treated_as_immediately_stale(self):
        """Failing toward 'go and check this' is the safe direction — the opposite
        default would make a typo silently disable the guard forever."""
        price = Price(input=1.0, output=2.0, as_of="whenever")
        self.assertTrue(price.is_stale("2020-01-01"))

    def test_currency_and_fx_are_recorded_not_assumed(self):
        """A hidden conversion error is indistinguishable from a price change, so
        the rate has to be part of the record."""
        price = Price(input=2.0, output=8.0, currency="CNY", fx_to_usd=0.14, fx_as_of="2026-08-02")
        self.assertEqual(price.currency, "CNY")
        self.assertEqual(price.fx_as_of, "2026-08-02")

    def test_catalogue_prices_are_currently_unverified(self):
        self.assertFalse(model("deepseek-chat").price.verified)
        self.assertGreater(PRICE_TABLE_MAX_AGE_DAYS, 0)


class TestLedgerProvenance(unittest.TestCase):
    def _ledger(self, price: Price) -> Ledger:
        ledger = Ledger(investigation_id="inv")
        ledger.record(
            kind="main_loop",
            model_id="deepseek-chat",
            provider="deepseek",
            usage=Usage(1000, 100),
            price=price,
        )
        return ledger

    def test_staleness_propagates_from_the_price_to_the_ledger(self):
        ledger = self._ledger(Price(input=1.0, output=2.0, as_of="2026-01-01"))
        self.assertTrue(ledger.prices_stale("2026-06-01"))
        self.assertFalse(ledger.prices_stale("2026-02-01"))

    def test_mixed_price_tables_are_flagged_not_summed_quietly(self):
        """A price edit landing mid-run means the total mixes rates. Silently adding
        them produces a number that is not a cost at any set of prices."""
        ledger = self._ledger(Price(input=1.0, output=2.0, as_of="2026-01-01"))
        ledger.record(
            kind="main_loop",
            model_id="deepseek-chat",
            provider="deepseek",
            usage=Usage(1000, 100),
            price=Price(input=2.0, output=4.0, as_of="2026-02-01"),
        )
        summary = ledger.summary()
        self.assertTrue(summary["mixed_price_tables"])
        self.assertEqual(len(summary["price_table_versions"]), 2)

    def test_a_single_table_is_not_flagged(self):
        summary = self._ledger(model("deepseek-chat").price).summary()
        self.assertFalse(summary["mixed_price_tables"])

    def test_historical_cost_is_frozen_not_recomputed(self):
        """The property that makes a price rise harmless to past numbers: a replayed
        cache entry charges what the original call cost, at the rates that applied
        then, rather than being repriced under today's table."""
        ledger = Ledger(investigation_id="inv")
        ledger.record(
            kind="main_loop",
            model_id="deepseek-chat",
            provider="deepseek",
            usage=Usage(1000, 100),
            price=Price(input=99.0, output=99.0),  # today's (absurd) table
            cached=True,
            cost_usd=0.000123,  # what it actually cost back then
        )
        self.assertAlmostEqual(ledger.entries[0].cost_usd, 0.000123)


class TestReconciliation(unittest.TestCase):
    def _rec(self, charged_cny: float, predicted_usd: float, **kwargs) -> Reconciliation:
        return Reconciliation(
            provider="deepseek",
            currency="CNY",
            balance_before=100.0,
            balance_after=100.0 - charged_cny,
            fx_to_usd=0.14,
            predicted_usd=predicted_usd,
            **kwargs,
        )

    def test_agreement_within_tolerance_is_consistent(self):
        # 1.00 CNY charged ≈ $0.14; table predicted $0.14
        rec = self._rec(charged_cny=1.0, predicted_usd=0.14)
        self.assertEqual(rec.verdict, "consistent")
        self.assertAlmostEqual(rec.ratio, 1.0, places=6)

    def test_a_doubled_charge_marks_the_table_suspect(self):
        """The signal a price rise produces."""
        rec = self._rec(charged_cny=2.0, predicted_usd=0.14)
        self.assertEqual(rec.verdict, "table_suspect")
        self.assertAlmostEqual(rec.ratio, 2.0, places=6)
        self.assertIn("TABLE SUSPECT", rec.explain())

    def test_small_spend_is_inconclusive_rather_than_noise(self):
        """Balance is reported to 2dp of CNY, about $0.0014, while a single call
        costs orders of magnitude less. Reporting a ratio there would be reporting
        rounding error as a finding."""
        rec = self._rec(charged_cny=0.0, predicted_usd=0.000133)
        self.assertTrue(rec.below_resolution)
        self.assertEqual(rec.verdict, "inconclusive")
        self.assertIn("below the", rec.explain())
        self.assertIn("eval run", rec.explain(), "the message must say what to do instead")

    def test_the_resolution_floor_is_configurable_and_documented(self):
        rec = self._rec(charged_cny=0.5, predicted_usd=0.07, min_spend_usd=0.0)
        self.assertFalse(rec.below_resolution)
        self.assertEqual(DEFAULT_MIN_SPEND_USD, 0.05)

    def test_tolerance_is_generous_on_purpose(self):
        """Rounding, FX drift and other workloads on the same account all
        contribute. A jumpy check trains people to ignore it."""
        rec = self._rec(charged_cny=1.2, predicted_usd=0.14)  # ratio ~1.2
        self.assertEqual(rec.verdict, "consistent")

    def test_zero_prediction_cannot_produce_a_ratio(self):
        rec = self._rec(charged_cny=1.0, predicted_usd=0.0)
        self.assertIsNone(rec.ratio)
        self.assertEqual(rec.verdict, "inconclusive")

    def test_summary_records_the_fx_rate_used(self):
        summary = self._rec(charged_cny=1.0, predicted_usd=0.14).summary()
        self.assertEqual(summary["fx_to_usd"], 0.14)
        self.assertEqual(summary["currency"], "CNY")

    def test_a_provider_without_a_balance_endpoint_says_so(self):
        with self.assertRaises(ReconcileUnavailable) as ctx:
            reader_for("anthropic")
        self.assertIn("no balance endpoint", str(ctx.exception))

    def test_deepseek_has_a_reader(self):
        self.assertEqual(reader_for("deepseek").provider, "deepseek")


class TestCacheSavingsMath(unittest.TestCase):
    def test_savings_is_the_gap_between_full_and_discounted_input(self):
        """The number behind the 85% figure the smoke run reported."""
        price = model("deepseek-chat").price
        usage = Usage(input_tokens=14, output_tokens=18, cache_read_tokens=1536)
        full_input_cost = 1536 * price.input / 1_000_000
        discounted = 1536 * price.cache_read_rate / 1_000_000
        self.assertAlmostEqual(cache_savings(usage, price), full_input_cost - discounted, places=12)

    def test_no_cached_tokens_means_no_savings(self):
        self.assertEqual(cache_savings(Usage(100, 10), model("deepseek-chat").price), 0.0)

    def test_cost_uses_the_discounted_rate_for_cached_reads(self):
        price = model("deepseek-chat").price
        cached = cost_of(Usage(input_tokens=0, cache_read_tokens=1000), price)
        uncached = cost_of(Usage(input_tokens=1000), price)
        self.assertLess(cached, uncached)


if __name__ == "__main__":
    unittest.main()
