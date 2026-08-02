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
    DEFAULT_MIN_SPEND,
    ReconcileUnavailable,
    Reconciliation,
    reader_for,
)
from agent.llm.usage import (
    PRICE_TABLE_MAX_AGE_DAYS,
    Cost,
    Price,
    Usage,
    cache_savings,
    cost_of,
)


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

    def test_currency_is_recorded_and_never_converted(self):
        """A hidden conversion error is indistinguishable from a price change, so
        there is deliberately no exchange rate anywhere in this system."""
        price = Price(input=2.0, output=8.0, currency="CNY")
        self.assertEqual(price.currency, "CNY")
        self.assertFalse(hasattr(price, "fx_to_usd"))
        self.assertFalse(hasattr(Cost(native=1.0, currency="CNY"), "usd"))

    def test_catalogue_prices_are_currently_unverified(self):
        self.assertFalse(model("deepseek-v4-flash").price.verified)
        self.assertGreater(PRICE_TABLE_MAX_AGE_DAYS, 0)


class TestLedgerProvenance(unittest.TestCase):
    def _ledger(self, price: Price) -> Ledger:
        ledger = Ledger(investigation_id="inv")
        ledger.record(
            kind="main_loop",
            model_id="deepseek-v4-flash",
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
            model_id="deepseek-v4-flash",
            provider="deepseek",
            usage=Usage(1000, 100),
            price=Price(input=2.0, output=4.0, as_of="2026-02-01"),
        )
        summary = ledger.summary()
        self.assertTrue(summary["mixed_price_tables"])
        self.assertEqual(len(summary["price_table_versions"]), 2)

    def test_a_single_table_is_not_flagged(self):
        summary = self._ledger(model("deepseek-v4-flash").price).summary()
        self.assertFalse(summary["mixed_price_tables"])

    def test_historical_cost_is_frozen_not_recomputed(self):
        """The property that makes a price rise harmless to past numbers: a replayed
        cache entry charges what the original call cost, at the rates that applied
        then, rather than being repriced under today's table."""
        ledger = Ledger(investigation_id="inv")
        ledger.record(
            kind="main_loop",
            model_id="deepseek-v4-flash",
            provider="deepseek",
            usage=Usage(1000, 100),
            price=Price(input=99.0, output=99.0),  # today's (absurd) table
            cached=True,
            cost=Cost(native=0.000123, currency="USD"),  # what it cost back then
        )
        self.assertAlmostEqual(ledger.entries[0].cost.native, 0.000123)

    def test_a_replayed_cost_keeps_its_own_currency(self):
        ledger = Ledger(investigation_id="inv")
        ledger.record(
            kind="main_loop",
            model_id="deepseek-v4-flash",
            provider="deepseek",
            usage=Usage(1000, 100),
            price=Price(input=2.0, output=8.0, currency="CNY"),
            cached=True,
            cost=Cost(native=1.0, currency="CNY"),
        )
        entry = ledger.entries[0]
        self.assertEqual(entry.cost.native, 1.0)
        self.assertEqual(entry.currency, "CNY")


class TestReconciliation(unittest.TestCase):
    def _rec(self, charged_cny: float, predicted_cny: float, **kwargs) -> Reconciliation:
        kwargs.setdefault("predicted_currency", "CNY")
        return Reconciliation(
            provider="deepseek",
            currency="CNY",
            balance_before=100.0,
            balance_after=100.0 - charged_cny,
            predicted=predicted_cny,
            **kwargs,
        )

    def test_agreement_within_tolerance_is_consistent(self):
        rec = self._rec(charged_cny=1.0, predicted_cny=1.0)
        self.assertEqual(rec.verdict, "consistent")
        self.assertAlmostEqual(rec.ratio, 1.0, places=6)

    def test_a_doubled_charge_marks_the_table_suspect(self):
        """The signal a price rise produces."""
        rec = self._rec(charged_cny=2.0, predicted_cny=1.0)
        self.assertEqual(rec.verdict, "table_suspect")
        self.assertAlmostEqual(rec.ratio, 2.0, places=6)
        self.assertIn("TABLE SUSPECT", rec.explain())

    def test_no_exchange_rate_participates(self):
        """The comparison is CNY against CNY. With a conversion in the middle, a
        wrong rate and a real price change produce the same signal — leaving the
        check unable to answer the one question it exists for."""
        rec = self._rec(charged_cny=1.0, predicted_cny=1.0)
        self.assertTrue(rec.comparable)
        self.assertNotIn("fx", " ".join(rec.summary().keys()).lower())

    def test_a_mismatched_currency_refuses_to_guess(self):
        """A USD-denominated table against a CNY balance is not converted — it is
        reported as not comparable, with the fix named."""
        rec = self._rec(charged_cny=1.0, predicted_cny=0.14, predicted_currency="USD")
        self.assertFalse(rec.comparable)
        self.assertEqual(rec.verdict, "not_comparable")
        self.assertIsNone(rec.ratio)
        self.assertIn("Record the rates in CNY", rec.explain())

    def test_small_spend_is_inconclusive_rather_than_noise(self):
        """Balance is reported to 2dp of CNY while a single call costs orders of
        magnitude less. Reporting a ratio there would report rounding as a finding."""
        rec = self._rec(charged_cny=0.0, predicted_cny=0.001)
        self.assertTrue(rec.below_resolution)
        self.assertEqual(rec.verdict, "inconclusive")
        self.assertIn("below the", rec.explain())
        self.assertIn("eval run", rec.explain(), "the message must say what to do instead")

    def test_the_resolution_floor_is_configurable_and_documented(self):
        rec = self._rec(charged_cny=0.5, predicted_cny=0.5, min_spend=0.0)
        self.assertFalse(rec.below_resolution)
        self.assertEqual(DEFAULT_MIN_SPEND, 0.35)

    def test_tolerance_is_generous_on_purpose(self):
        """Rounding and other workloads on the same account both contribute. A jumpy
        check trains people to ignore it."""
        rec = self._rec(charged_cny=1.2, predicted_cny=1.0)  # ratio 1.2
        self.assertEqual(rec.verdict, "consistent")

    def test_zero_prediction_cannot_produce_a_ratio(self):
        rec = self._rec(charged_cny=1.0, predicted_cny=0.0)
        self.assertIsNone(rec.ratio)
        self.assertEqual(rec.verdict, "inconclusive")

    def test_summary_reports_one_currency_only(self):
        summary = self._rec(charged_cny=1.0, predicted_cny=1.0).summary()
        self.assertEqual(summary["currency"], "CNY")
        self.assertEqual(summary["predicted_currency"], "CNY")

    def test_a_provider_without_a_balance_endpoint_says_so(self):
        with self.assertRaises(ReconcileUnavailable) as ctx:
            reader_for("anthropic")
        self.assertIn("no balance endpoint", str(ctx.exception))

    def test_deepseek_has_a_reader(self):
        self.assertEqual(reader_for("deepseek").provider, "deepseek")


class TestCacheSavingsMath(unittest.TestCase):
    def test_savings_is_the_gap_between_full_and_discounted_input(self):
        """The number behind the 85% figure the smoke run reported."""
        price = model("deepseek-v4-flash").price
        usage = Usage(input_tokens=14, output_tokens=18, cache_read_tokens=1536)
        full_input_cost = 1536 * price.input / 1_000_000
        discounted = 1536 * price.cache_read_rate / 1_000_000
        saving = cache_savings(usage, price)
        self.assertAlmostEqual(saving.native, full_input_cost - discounted, places=12)
        self.assertEqual(saving.currency, price.currency)

    def test_no_cached_tokens_means_no_savings(self):
        self.assertEqual(cache_savings(Usage(100, 10), model("deepseek-v4-flash").price).native, 0.0)

    def test_cost_uses_the_discounted_rate_for_cached_reads(self):
        price = model("deepseek-v4-flash").price
        cached = cost_of(Usage(input_tokens=0, cache_read_tokens=1000), price)
        uncached = cost_of(Usage(input_tokens=1000), price)
        self.assertLess(cached.native, uncached.native)
        self.assertEqual(cached.currency, price.currency)

    def test_cost_is_denominated_in_the_price_table_currency(self):
        """Not silently USD: the amount is exactly what the provider will bill."""
        cny = Price(input=2.0, output=8.0, currency="CNY")
        cost = cost_of(Usage(input_tokens=1_000_000), cny)
        self.assertEqual(cost.currency, "CNY")
        self.assertAlmostEqual(cost.native, 2.0, places=9)

    def test_mixed_currency_costs_refuse_to_add(self):
        """Summing CNY and USD yields a number that is a cost in neither currency —
        precisely the silent wrongness this type exists to prevent."""
        with self.assertRaises(ValueError):
            Cost(native=1.0, currency="CNY") + Cost(native=1.0, currency="USD")

    def test_ledger_reports_totals_per_currency_and_never_sums_them(self):
        """The agent bills in one currency and the judge in another. They are separate
        lines, not a sum — which is why no exchange rate is needed anywhere."""
        ledger = Ledger(investigation_id="inv")
        ledger.record(
            kind="main_loop",
            model_id="deepseek-v4-flash",
            provider="deepseek",
            usage=Usage(input_tokens=1_000_000),
            price=Price(input=2.0, output=8.0, currency="CNY"),
        )
        ledger.record(
            kind="judge",
            model_id="claude-sonnet-5",
            provider="anthropic",
            usage=Usage(input_tokens=1_000_000),
            price=Price(input=3.0, output=15.0, currency="USD"),
        )
        summary = ledger.summary()
        self.assertEqual(
            {k: round(v, 4) for k, v in summary["money_spent"].items()},
            {"CNY": 2.0, "USD": 3.0},
        )
        self.assertTrue(summary["mixed_currencies"])
        self.assertEqual(summary["by_kind"]["main_loop"], {"CNY": 2.0})
        self.assertEqual(summary["by_kind"]["judge"], {"USD": 3.0})


if __name__ == "__main__":
    unittest.main()
