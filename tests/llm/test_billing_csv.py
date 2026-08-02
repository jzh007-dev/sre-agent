"""Rate verification from a provider invoice.

The fixture is a real DeepSeek billing export with the account id, key values and key
*names* replaced — this repository is public, and a key name can name another project.
The volumes and rates are kept because they are the evidence. It is kept because its numbers are load-bearing: recomputing each day's
charge from the per-token rates reproduces the billed total exactly, which is what
makes the catalogue's `verified=True` a claim rather than an assertion.
"""
from __future__ import annotations

import pathlib
import tempfile
import unittest

from agent.llm import billing_csv
from agent.llm.provider_catalog import model
from agent.llm.usage import Usage, cost_of

AMOUNT_CSV = """user_id,utc_date,model,api_key_name,api_key,type,price,amount
U,20260727,deepseek-v4-pro,key-a,sk-x,input_cache_hit_tokens,0.000000025,24448
U,20260727,deepseek-v4-pro,key-a,sk-x,input_cache_miss_tokens,0.000003,24423
U,20260727,deepseek-v4-pro,key-a,sk-x,request_count,,12
U,20260727,deepseek-v4-pro,key-a,sk-x,output_tokens,0.000006,14044
U,20260801,deepseek-v4-flash,key-a,sk-x,input_cache_hit_tokens,0.00000002,867328
U,20260801,deepseek-v4-flash,key-a,sk-x,input_cache_miss_tokens,0.000001,98221
U,20260801,deepseek-v4-flash,key-a,sk-x,request_count,,18
U,20260801,deepseek-v4-flash,key-a,sk-x,output_tokens,0.000002,22695
U,20260802,deepseek-v4-flash,key-a,sk-x,input_cache_hit_tokens,0.00000002,415232
U,20260802,deepseek-v4-flash,key-a,sk-x,input_cache_miss_tokens,0.000001,75005
U,20260802,deepseek-v4-flash,key-a,sk-x,request_count,,14
U,20260802,deepseek-v4-flash,key-a,sk-x,output_tokens,0.000002,21837
U,20260802,deepseek-v4-flash,key-b,sk-y,input_cache_hit_tokens,0.00000002,23040
U,20260802,deepseek-v4-flash,key-b,sk-y,input_cache_miss_tokens,0.000001,3557
U,20260802,deepseek-v4-flash,key-b,sk-y,request_count,,19
U,20260802,deepseek-v4-flash,key-b,sk-y,output_tokens,0.000002,536
"""

COST_CSV = """user_id,utc_date,model,wallet_type,cost,currency
U,20260727,deepseek-v4-pro,Paid,0.1581442000000000,CNY
U,20260801,deepseek-v4-flash,Paid,0.1609575600000000,CNY
U,20260802,deepseek-v4-flash,Paid,0.1320734400000000,CNY
"""


class InvoiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        base = pathlib.Path(self._dir.name)
        self.amount = base / "amount.csv"
        self.cost = base / "cost.csv"
        # utf-8-sig: the real export carries a BOM, and reading it as plain utf-8
        # corrupts the first header and silently drops the first column.
        self.amount.write_text(AMOUNT_CSV, encoding="utf-8-sig")
        self.cost.write_text(COST_CSV, encoding="utf-8-sig")
        self.invoice = billing_csv.load(self.amount, self.cost)

    def tearDown(self) -> None:
        self._dir.cleanup()


class TestSelfCheck(InvoiceCase):
    def test_recomputing_from_rates_reproduces_every_billed_total(self):
        """This validates the parser before the parser is trusted to judge the price
        table. Without it, a misread column would look exactly like price drift."""
        self.assertEqual(self.invoice.self_check(), [])

    def test_the_match_is_exact_not_approximate(self):
        recomputed = self.invoice.recomputed()
        for key, billed in self.invoice.billed.items():
            with self.subTest(day=key):
                self.assertAlmostEqual(recomputed[key], billed, places=10)

    def test_a_days_total_sums_across_api_keys(self):
        """The cost export aggregates per day and model; the usage export splits by
        key. 2026-08-02 has two keys, and only their sum matches the billed figure."""
        recomputed = self.invoice.recomputed()[("20260802", "deepseek-v4-flash")]
        self.assertAlmostEqual(recomputed, 0.13207344, places=10)

    def test_request_count_rows_do_not_contribute_a_charge(self):
        """They carry a count and no rate; treating them as billable would inflate
        every total by the request count."""
        self.assertTrue(all(r.row_type != "request_count" for r in self.invoice.usage))

    def test_a_wrong_billed_total_is_reported(self):
        self.cost.write_text(
            COST_CSV.replace("0.1581442000000000", "0.9999999999999999"), encoding="utf-8-sig"
        )
        broken = billing_csv.load(self.amount, self.cost)
        problems = broken.self_check()
        self.assertEqual(len(problems), 1)
        self.assertIn("deepseek-v4-pro", problems[0])


class TestDerivedRates(InvoiceCase):
    def test_rates_come_out_per_million_tokens(self):
        rates = self.invoice.derived_rates()
        expected = {
            "deepseek-v4-flash": {"input": 1.0, "cache_read": 0.02, "output": 2.0},
            "deepseek-v4-pro": {"input": 3.0, "cache_read": 0.025, "output": 6.0},
        }
        for model_id, fields in expected.items():
            self.assertEqual(set(rates[model_id]), set(fields))
            for name, value in fields.items():
                with self.subTest(model=model_id, field=name):
                    # Almost-equal, not equal: the invoice quotes per-token rates, and
                    # scaling 2.5e-8 by 1e6 is not exactly 0.025 in binary floating
                    # point. RATE_TOLERANCE is what the drift check uses for the same
                    # reason.
                    self.assertAlmostEqual(rates[model_id][name], value, places=12)

    def test_the_cache_discount_is_enormous_and_measured(self):
        """98% on flash. Our earlier estimate of ~90% came from a table written from
        memory, which is exactly why the invoice is the better source."""
        rates = self.invoice.derived_rates()["deepseek-v4-flash"]
        self.assertAlmostEqual(1 - rates["cache_read"] / rates["input"], 0.98, places=6)

    def test_no_rate_conflicts_so_a_flat_price_can_describe_it(self):
        """Two rates for one row type would mean a mid-window price change or
        time-of-day pricing, and a single `Price` would then be an average
        masquerading as a rate."""
        self.assertEqual(self.invoice.rate_conflicts(), [])

    def test_a_rate_conflict_is_reported(self):
        self.amount.write_text(
            AMOUNT_CSV
            + "U,20260803,deepseek-v4-flash,key-b,sk-y,output_tokens,0.000004,100\n",
            encoding="utf-8-sig",
        )
        conflicted = billing_csv.load(self.amount, self.cost)
        problems = conflicted.rate_conflicts()
        self.assertEqual(len(problems), 1)
        self.assertIn("time-of-day", problems[0])

    def test_currency_comes_from_the_invoice(self):
        self.assertEqual(self.invoice.currency, "CNY")

    def test_mixed_currencies_are_refused(self):
        self.cost.write_text(COST_CSV + "U,20260803,x,Paid,1.0,USD\n", encoding="utf-8-sig")
        with self.assertRaises(ValueError):
            billing_csv.load(self.amount, self.cost)


class TestCatalogueDrift(InvoiceCase):
    def test_the_catalogue_currently_matches_the_invoice(self):
        """The verification behind `verified=True` on the DeepSeek entries."""
        self.assertEqual(self.invoice.catalogue_drift(), [])

    def test_a_stale_catalogue_rate_is_detected(self):
        """The drift detector doing its job — a price rise reaching the invoice while
        the table still holds the old number."""
        self.amount.write_text(
            AMOUNT_CSV.replace(
                "U,20260802,deepseek-v4-flash,key-b,sk-y,output_tokens,0.000002,536",
                "U,20260802,deepseek-v4-flash,key-b,sk-y,output_tokens,0.000004,536",
            ),
            encoding="utf-8-sig",
        )
        drifted = billing_csv.load(self.amount, self.cost)
        problems = drifted.catalogue_drift()
        self.assertTrue(any("output" in p and "stale" in p for p in problems), problems)

    def test_a_model_billed_but_absent_from_the_catalogue_is_reported(self):
        self.amount.write_text(
            AMOUNT_CSV
            + "U,20260803,deepseek-v5-turbo,key-b,sk-y,input_cache_miss_tokens,0.000001,100\n"
            + "U,20260803,deepseek-v5-turbo,key-b,sk-y,output_tokens,0.000002,100\n",
            encoding="utf-8-sig",
        )
        unknown = billing_csv.load(self.amount, self.cost)
        problems = unknown.catalogue_drift()
        self.assertTrue(any("deepseek-v5-turbo" in p for p in problems), problems)


class TestSpendAttribution(InvoiceCase):
    def test_spend_is_split_per_api_key(self):
        """Why this beats the balance endpoint: a second key on the same account moves
        the balance, so a balance delta cannot be attributed to this project."""
        spend = self.invoice.spend_by_key()
        self.assertAlmostEqual(spend["key-b"], 0.0050898, places=10)
        self.assertGreater(spend["key-a"], spend["key-b"] * 50)

    def test_as_prices_builds_verified_price_objects(self):
        prices = self.invoice.as_prices()
        flash = prices["deepseek-v4-flash"]
        self.assertTrue(flash.verified)
        self.assertEqual(flash.currency, "CNY")
        self.assertEqual(flash.input, 1.0)
        # No write premium: a miss populates the cache at the miss rate, so pinning
        # this avoids inventing Anthropic's 1.25x convention.
        self.assertEqual(flash.cache_write, flash.input)


class TestCostAgreesWithTheInvoice(unittest.TestCase):
    def test_our_cost_function_reproduces_an_invoiced_charge(self):
        """End to end: the catalogue's Price, fed our own Usage shape, produces the
        number the provider actually billed for that key and day."""
        spec = model("deepseek-v4-flash")
        usage = Usage(input_tokens=3557, output_tokens=536, cache_read_tokens=23040)
        cost = cost_of(usage, spec.price)
        self.assertEqual(cost.currency, "CNY")
        self.assertAlmostEqual(cost.native, 0.0050898, places=10)


if __name__ == "__main__":
    unittest.main()
