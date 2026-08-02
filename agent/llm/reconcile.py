"""Billing reconciliation — the mechanism that catches a price change.

The problem this solves: a price table checked into a repository is wrong the
moment the provider changes its rates, and nothing in the code would notice. Every
cost figure would stay confidently, silently incorrect. Reading a pricing page is
a manual step that will be skipped; reconciling against the provider's own billing
is automatic.

**How it works.** Snapshot the account balance, run some work, snapshot again. The
delta is what the provider actually charged. Compare it to `Ledger.money_spent` in
the same currency, which is what our table *predicted*. Divergence beyond tolerance means the table is
stale — and the ratio tells you roughly by how much.

**The resolution floor is real and worth stating.** DeepSeek reports balance to two
decimal places of CNY, about $0.0014. A single smoke call costs ~$0.00007, three
orders of magnitude below that, so per-call reconciliation is impossible. An eval
run over ~30 cases is comfortably above the floor. This is a *batch* instrument,
and `min_spend_usd` refuses to draw a conclusion below it rather than reporting
noise as a finding.

**Currency: same-currency only, by construction.** The balance arrives in the
provider's billing currency and the ledger records costs in that same currency, so
this compares CNY against CNY and **no exchange rate participates at all**. That is
what makes the check able to answer its own question: with a conversion in the
middle, a wrong rate and a real price change produce the same signal.

If the price table is denominated differently from the balance, this does **not**
convert and guess — it returns `not_comparable` and says to denominate the table in
the billing currency. A weaker answer dressed up as a real one would be worse than
no answer.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from .credentials import api_key
from .provider_catalog import PROVIDERS

#: Below this (in the billing currency), the balance endpoint's rounding dominates
#: and no conclusion is drawn. DeepSeek reports 2dp of CNY, so ~0.35 CNY is the point
#: at which a delta is more signal than rounding.
DEFAULT_MIN_SPEND = 0.35

#: Fractional disagreement tolerated before the table is called into question.
#: Generous on purpose: rounding, FX drift, and concurrent use of the same account
#: all contribute, and a false "prices changed" alarm would train people to ignore
#: this check.
DEFAULT_TOLERANCE = 0.25


class BalanceReader(Protocol):
    provider: str

    def balance(self) -> tuple[float, str]:
        """Return `(amount, currency)` for the account."""
        ...


@dataclass
class DeepSeekBalanceReader:
    """Reads `GET /user/balance`.

    Uses `urllib` rather than the OpenAI SDK because this is not a model call and
    does not belong on the transport path — it has no retry policy, no budget, and
    no place in the cost ledger it is checking.
    """

    provider: str = "deepseek"
    url: str = "https://api.deepseek.com/user/balance"
    timeout: float = 20.0

    def balance(self) -> tuple[float, str]:
        spec = PROVIDERS[self.provider]
        request = urllib.request.Request(
            self.url,
            headers={"Accept": "application/json", "Authorization": f"Bearer {api_key(spec)}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ReconcileUnavailable(f"could not read {self.provider} balance: {exc}") from exc

        infos = payload.get("balance_infos") or []
        if not infos:
            raise ReconcileUnavailable(f"{self.provider} returned no balance_infos: {payload}")
        info = infos[0]
        return float(info["total_balance"]), str(info["currency"])


class ReconcileUnavailable(RuntimeError):
    """The provider's billing could not be read. Not a price-table verdict."""


#: Providers exposing a balance endpoint. A provider absent here cannot be
#: reconciled, which is itself worth reporting rather than silently skipping.
READERS: dict[str, type[BalanceReader]] = {"deepseek": DeepSeekBalanceReader}


@dataclass
class Reconciliation:
    """The verdict of one before/after comparison."""

    provider: str
    #: Currency the *balance* is reported in — and the only currency compared.
    currency: str
    balance_before: float
    balance_after: float
    #: What the ledger predicted, in `predicted_currency`.
    predicted: float
    predicted_currency: str
    min_spend: float = DEFAULT_MIN_SPEND
    tolerance: float = DEFAULT_TOLERANCE
    notes: list[str] = field(default_factory=list)

    @property
    def charged(self) -> float:
        return self.balance_before - self.balance_after

    @property
    def comparable(self) -> bool:
        """Whether prediction and charge are in the same currency.

        No conversion is attempted when they are not: an exchange rate would make a
        wrong rate look exactly like a price change.
        """
        return self.predicted_currency == self.currency

    @property
    def below_resolution(self) -> bool:
        """Whether the spend was too small for the balance endpoint to resolve."""
        return max(self.charged, self.predicted) < self.min_spend

    @property
    def ratio(self) -> float | None:
        """Actual / predicted. >1 means we are under-reporting cost."""
        if not self.comparable or self.predicted <= 0:
            return None
        return self.charged / self.predicted

    @property
    def verdict(self) -> str:
        if not self.comparable:
            return "not_comparable"
        if self.below_resolution:
            return "inconclusive"
        ratio = self.ratio
        if ratio is None:
            return "inconclusive"
        if abs(ratio - 1.0) <= self.tolerance:
            return "consistent"
        return "table_suspect"

    def summary(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "verdict": self.verdict,
            "currency": self.currency,
            "charged": round(self.charged, 4),
            "predicted": round(self.predicted, 6),
            "predicted_currency": self.predicted_currency,
            "ratio": None if self.ratio is None else round(self.ratio, 3),
            "tolerance": self.tolerance,
            "min_spend": self.min_spend,
            "notes": self.notes,
        }

    def explain(self) -> str:
        if self.verdict == "not_comparable":
            return (
                f"not comparable: {self.provider} bills in {self.currency} but the "
                f"price table is denominated in {self.predicted_currency or 'nothing'}. "
                f"Deliberately not converted — an exchange rate would make a wrong "
                f"rate look identical to a price change. Record the rates in "
                f"{self.currency}."
            )
        if self.verdict == "inconclusive":
            return (
                f"inconclusive: spent ~{self.predicted:.6f} {self.currency}, below the "
                f"{self.min_spend:.2f} {self.currency} floor set by balance rounding. "
                f"Reconcile around a full eval run, not a single call."
            )
        charged = f"{self.charged:.4f} {self.currency}"
        predicted = f"{self.predicted:.4f} {self.currency}"

        if self.verdict == "consistent":
            return (
                f"consistent: provider charged {charged}, table predicted {predicted} "
                f"(ratio {self.ratio:.3f}, within ±{self.tolerance:.0%})"
            )
        return (
            f"TABLE SUSPECT: provider charged {charged} but the table predicted "
            f"{predicted} (ratio {self.ratio:.3f}). Either the rates changed or "
            f"another workload shares this account. Re-check the published rates "
            f"before trusting any cost figure."
        )


def reader_for(provider: str) -> BalanceReader:
    try:
        return READERS[provider]()
    except KeyError as exc:
        raise ReconcileUnavailable(
            f"{provider} exposes no balance endpoint this project knows about; "
            f"reconcilable providers: {sorted(READERS)}"
        ) from exc
