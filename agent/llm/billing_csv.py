"""Rate verification from the provider's own invoice.

This is the answer to "what if the provider raises prices", and it turned out to be
better than every mechanism designed before it.

**Why the invoice beats a pricing page.** A page states list prices; an invoice states
what *this account* was charged, including any tiering. The export carries a
per-token `price` column, so the rates can be read off directly rather than
transcribed — and then cross-checked by recomputing each day's charge and comparing
against the billed total. Three days reproduced their totals exactly to ten decimal
places, which is a far stronger claim than "the documentation said so".

**Why it beats the balance endpoint** (`reconcile.py`, still useful as a live crude
check):

- **Per model.** Balance is one number for the account, so a run mixing two models
  cannot be attributed.
- **Per API key.** This account has a second key doing unrelated work, and its spend
  moved the balance too — which is precisely the "shared account confounds
  reconciliation" caveat, observed rather than hypothesised. The CSV separates them.
- **Exact.** Balance is rounded to two decimals of CNY (~0.0014), three orders of
  magnitude above the cost of one call. The CSV has full precision.

Its one weakness is that it is a manual export rather than an endpoint — DeepSeek
exposes no usage API (`/usage`, `/dashboard/billing/usage`, `/user/info` all 404).
So this runs on demand, and the balance check remains the automatic one.

Nothing here reads the CSV from inside the repository: the export contains an account
id and key prefixes, so paths are supplied by the caller and the files stay outside
version control.
"""
from __future__ import annotations

import csv
import pathlib
from collections import defaultdict
from dataclasses import dataclass, field

from .provider_catalog import MODELS
from .usage import Price

#: Invoice row types → the `Price` field each one prices.
RATE_FIELDS: dict[str, str] = {
    "input_cache_miss_tokens": "input",
    "input_cache_hit_tokens": "cache_read",
    "output_tokens": "output",
}

#: Rows that carry a count but no rate, so they never contribute to a charge.
NON_BILLED_TYPES = frozenset({"request_count"})

#: Fractional disagreement tolerated between a derived rate and the catalogue.
#: Tight, unlike the balance check's tolerance: both sides here are exact numbers, so
#: any real difference means the table is wrong rather than that measurement is noisy.
RATE_TOLERANCE = 1e-9


@dataclass(frozen=True)
class UsageRow:
    date: str
    model: str
    key_name: str
    row_type: str
    price: float | None
    amount: int

    @property
    def charge(self) -> float:
        return (self.price or 0.0) * self.amount


@dataclass
class Invoice:
    """A parsed billing export."""

    usage: list[UsageRow] = field(default_factory=list)
    #: (date, model) → billed total, from the cost export.
    billed: dict[tuple[str, str], float] = field(default_factory=dict)
    currency: str = ""

    # ------------------------------------------------------------------ rates

    def derived_rates(self) -> dict[str, dict[str, float]]:
        """model → {`Price` field: rate per 1M tokens}.

        Rates come from the invoice's own `price` column, so this is transcription
        rather than inference. A model billed at two different rates for the same row
        type within the export is reported by `rate_conflicts()` — that would mean the
        provider changed the price mid-window, or applies time-of-day pricing that a
        single flat rate cannot represent.
        """
        seen: dict[str, dict[str, set[float]]] = defaultdict(lambda: defaultdict(set))
        for row in self.usage:
            field_name = RATE_FIELDS.get(row.row_type)
            if field_name is None or row.price is None:
                continue
            seen[row.model][field_name].add(row.price)

        return {
            model: {f: max(prices) * 1_000_000 for f, prices in fields.items()}
            for model, fields in seen.items()
        }

    def rate_conflicts(self) -> list[str]:
        """Row types billed at more than one rate for the same model.

        Worth surfacing loudly: it means a single `Price` cannot describe this model,
        and any cost figure computed from one would be an average masquerading as a
        rate.
        """
        seen: dict[tuple[str, str], set[float]] = defaultdict(set)
        for row in self.usage:
            if row.row_type in RATE_FIELDS and row.price is not None:
                seen[(row.model, row.row_type)].add(row.price)
        return [
            f"{model}/{row_type} billed at {sorted(prices)} — a single flat rate "
            f"cannot represent this (mid-window change, or time-of-day pricing?)"
            for (model, row_type), prices in seen.items()
            if len(prices) > 1
        ]

    # --------------------------------------------------------------- self-check

    def recomputed(self) -> dict[tuple[str, str], float]:
        """(date, model) → charge recomputed from rate × amount.

        Summed across API keys, because the cost export aggregates that way.
        """
        totals: dict[tuple[str, str], float] = defaultdict(float)
        for row in self.usage:
            totals[(row.date, row.model)] += row.charge
        return dict(totals)

    def self_check(self, tolerance: float = 1e-9) -> list[str]:
        """Confirm our reading of the invoice reproduces the invoice.

        This validates the *parser* before the parser is trusted to validate the price
        table. Without it, a misread column would look like price drift.
        """
        problems: list[str] = []
        recomputed = self.recomputed()
        for key, billed in sorted(self.billed.items()):
            got = recomputed.get(key)
            if got is None:
                problems.append(f"{key[0]} {key[1]}: billed {billed} but no usage rows found")
            elif abs(got - billed) > tolerance:
                problems.append(
                    f"{key[0]} {key[1]}: recomputed {got:.10f} vs billed {billed:.10f} "
                    f"(diff {got - billed:+.12f})"
                )
        return problems

    # -------------------------------------------------------- catalogue check

    def catalogue_drift(self) -> list[str]:
        """Where the catalogue disagrees with the invoice. **The drift detector.**"""
        problems: list[str] = []
        for model_id, rates in sorted(self.derived_rates().items()):
            spec = MODELS.get(model_id)
            if spec is None:
                problems.append(
                    f"{model_id}: billed for, but absent from the catalogue — costs for "
                    f"it cannot be computed at all"
                )
                continue
            if spec.price.currency != self.currency:
                problems.append(
                    f"{model_id}: catalogue is denominated in {spec.price.currency} but "
                    f"the invoice is in {self.currency}"
                )
            for field_name, invoiced in sorted(rates.items()):
                catalogued = getattr(spec.price, field_name, None)
                if catalogued is None:
                    problems.append(f"{model_id}.{field_name}: not set in the catalogue")
                elif abs(catalogued - invoiced) > RATE_TOLERANCE:
                    problems.append(
                        f"{model_id}.{field_name}: catalogue {catalogued} vs invoiced "
                        f"{invoiced} per 1M — the table is stale"
                    )
        return problems

    def spend_by_key(self) -> dict[str, float]:
        """Charge per API key.

        The reason this beats the balance endpoint: a second key on the same account
        moves the balance, so a balance delta cannot be attributed to this project's
        usage. Here it can.
        """
        totals: dict[str, float] = defaultdict(float)
        for row in self.usage:
            totals[row.key_name] += row.charge
        return dict(totals)

    def as_prices(self) -> dict[str, Price]:
        """Build `Price` objects straight from the invoice.

        What "verified" means concretely: rather than transcribing rates by hand and
        marking a flag, the rates *are* the invoice.
        """
        out: dict[str, Price] = {}
        for model_id, rates in self.derived_rates().items():
            if "input" not in rates or "output" not in rates:
                continue
            out[model_id] = Price(
                input=rates["input"],
                output=rates["output"],
                cache_read=rates.get("cache_read"),
                # DeepSeek has no write premium — a miss populates the cache at the
                # miss rate. Defaulting would invent Anthropic's 1.25x.
                cache_write=rates["input"],
                currency=self.currency,
                verified=True,
            )
        return out


def _to_float(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def load(amount_csv: str | pathlib.Path, cost_csv: str | pathlib.Path) -> Invoice:
    """Parse the two exports.

    `utf-8-sig` because the export carries a BOM; reading it as plain utf-8 corrupts
    the first header name and silently loses the first column.
    """
    invoice = Invoice()

    with pathlib.Path(amount_csv).open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            row_type = (row.get("type") or "").strip()
            if row_type in NON_BILLED_TYPES:
                continue
            amount = _to_float(row.get("amount", ""))
            invoice.usage.append(
                UsageRow(
                    date=(row.get("utc_date") or "").strip(),
                    model=(row.get("model") or "").strip(),
                    key_name=(row.get("api_key_name") or "").strip(),
                    row_type=row_type,
                    price=_to_float(row.get("price", "")),
                    amount=int(amount or 0),
                )
            )

    currencies: set[str] = set()
    with pathlib.Path(cost_csv).open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            cost = _to_float(row.get("cost", ""))
            if cost is None:
                continue
            key = ((row.get("utc_date") or "").strip(), (row.get("model") or "").strip())
            invoice.billed[key] = invoice.billed.get(key, 0.0) + cost
            currency = (row.get("currency") or "").strip()
            if currency:
                currencies.add(currency)

    if len(currencies) > 1:
        raise ValueError(f"invoice mixes currencies {sorted(currencies)}; cannot verify rates")
    invoice.currency = next(iter(currencies), "")
    return invoice
