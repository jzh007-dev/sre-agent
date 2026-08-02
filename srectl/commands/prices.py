"""`srectl prices` — verify the price table against the provider's own invoice.

Run after exporting the billing CSVs from the provider console:

    .venv/bin/python -m srectl prices --amount ~/dl/amount.csv --cost ~/dl/cost.csv

Three checks, in the order that makes a failure interpretable:

1. **Self-check** — recompute each day's charge from the invoice's own per-token rates
   and compare against its billed total. This validates the *parser* before the parser
   is trusted to judge the price table; without it, a misread column would look
   exactly like price drift.
2. **Rate conflicts** — the same row type billed at two different rates would mean a
   single flat `Price` cannot describe the model (a mid-window change, or time-of-day
   pricing), and any cost computed from one would be an average pretending to be a
   rate.
3. **Catalogue drift** — the actual detector: does `provider_catalog` still match what
   the provider charges?

The CSV paths are arguments rather than a location in the repository, because the
export carries an account id and API-key prefixes.
"""
from __future__ import annotations

import argparse
import json

from agent.llm import billing_csv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="srectl prices", description=__doc__)
    parser.add_argument("--amount", required=True, help="path to the amount/usage CSV export")
    parser.add_argument("--cost", required=True, help="path to the cost CSV export")
    parser.add_argument(
        "--json", action="store_true", help="emit the derived rates as JSON and nothing else"
    )
    args = parser.parse_args(argv)

    invoice = billing_csv.load(args.amount, args.cost)
    rates = invoice.derived_rates()

    if args.json:
        print(json.dumps({"currency": invoice.currency, "rates": rates}, indent=2, sort_keys=True))
        return 0

    print(f"currency: {invoice.currency}")
    print(f"rows: {len(invoice.usage)} usage, {len(invoice.billed)} billed day/model pairs")

    print("\n1. self-check — does our reading reproduce the invoice?")
    problems = invoice.self_check()
    if problems:
        for problem in problems:
            print(f"   MISMATCH {problem}")
        print("   → the parser is wrong; do not trust the rates below")
        return 1
    for (date, model_id), billed in sorted(invoice.billed.items()):
        print(f"   {date} {model_id:20s} {billed:.10f} {invoice.currency}  exact")

    print("\n2. rate conflicts")
    conflicts = invoice.rate_conflicts()
    if conflicts:
        for conflict in conflicts:
            print(f"   WARNING {conflict}")
    else:
        print("   none — one flat rate per row type, so a single Price can describe it")

    print(f"\n3. derived rates (per 1M tokens, {invoice.currency})")
    for model_id, fields in sorted(rates.items()):
        parts = "  ".join(f"{name}={value:g}" for name, value in sorted(fields.items()))
        print(f"   {model_id:20s} {parts}")
        if "cache_read" in fields and fields.get("input"):
            discount = 1 - fields["cache_read"] / fields["input"]
            print(f"   {'':20s} cache hit is {discount:.1%} cheaper than a miss")

    print("\n4. catalogue drift")
    drift = invoice.catalogue_drift()
    if drift:
        for problem in drift:
            print(f"   DRIFT {problem}")
        print("   → update provider_catalog.MODELS and bump the price table version")
    else:
        print("   none — the catalogue matches what the provider charged")

    print("\n5. spend by API key")
    for key_name, spend in sorted(invoice.spend_by_key().items(), key=lambda kv: -kv[1]):
        print(f"   {key_name:20s} {spend:.6f} {invoice.currency}")
    if len(invoice.spend_by_key()) > 1:
        print(
            "   note: more than one key on this account, which is why a balance delta\n"
            "   cannot be attributed to this project. This breakdown can."
        )

    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
