"""Billing reconciliation — the mechanism that catches a price change.

The problem this solves: a price table checked into a repository is wrong the
moment the provider changes its rates, and nothing in the code would notice. Every
cost figure would stay confidently, silently incorrect. Reading a pricing page is
a manual step that will be skipped; reconciling against the provider's own billing
is automatic.

**How it works.** Snapshot the account balance, run some work, snapshot again. The
delta is what the provider actually charged. Compare it to `Ledger.money_spent_usd`,
which is what our table *predicted*. Divergence beyond tolerance means the table is
stale — and the ratio tells you roughly by how much.

**The resolution floor is real and worth stating.** DeepSeek reports balance to two
decimal places of CNY, about $0.0014. A single smoke call costs ~$0.00007, three
orders of magnitude below that, so per-call reconciliation is impossible. An eval
run over ~30 cases is comfortably above the floor. This is a *batch* instrument,
and `min_spend_usd` refuses to draw a conclusion below it rather than reporting
noise as a finding.

**Currency, and why the ledger now holds native amounts.** The balance arrives in
the provider's billing currency. If the ledger only held USD, this comparison would
need an FX conversion — and then a wrong exchange rate and a real price change would
produce *the same signal*, making the check unable to answer the question it exists
for. Because `Cost` keeps the native amount authoritative, the common case compares
**CNY against CNY and FX drops out entirely**. Conversion happens only when the
price table is denominated differently from the balance, and that case is flagged in
the output as a weaker result rather than reported identically.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from .credentials import api_key
from .provider_catalog import PROVIDERS

#: Below this, the balance endpoint's rounding dominates and no conclusion is drawn.
DEFAULT_MIN_SPEND_USD = 0.05

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
    #: Currency the *balance* is reported in.
    currency: str
    balance_before: float
    balance_after: float
    fx_to_usd: float
    predicted_usd: float
    #: What the ledger predicted in the price table's own currency, when that
    #: currency matches the balance's. Present means FX is not involved in the
    #: comparison at all, which is the stronger result.
    predicted_native: float | None = None
    #: Currency of `predicted_native`.
    predicted_currency: str = ""
    min_spend_usd: float = DEFAULT_MIN_SPEND_USD
    tolerance: float = DEFAULT_TOLERANCE
    notes: list[str] = field(default_factory=list)

    @property
    def charged_native(self) -> float:
        return self.balance_before - self.balance_after

    @property
    def charged_usd(self) -> float:
        return self.charged_native * self.fx_to_usd

    @property
    def compares_natively(self) -> bool:
        """True when the prediction and the charge are in the same currency.

        The stronger comparison: no exchange rate participates, so a divergence can
        only mean the rates are wrong (or another workload shares the account).
        """
        return self.predicted_native is not None and self.predicted_currency == self.currency

    @property
    def below_resolution(self) -> bool:
        """Whether the spend was too small for the balance endpoint to resolve."""
        return max(self.charged_usd, self.predicted_usd) < self.min_spend_usd

    @property
    def ratio(self) -> float | None:
        """Actual / predicted. >1 means we are under-reporting cost.

        Computed natively when possible so no FX error can masquerade as drift.
        """
        if self.compares_natively:
            assert self.predicted_native is not None
            if self.predicted_native <= 0:
                return None
            return self.charged_native / self.predicted_native
        if self.predicted_usd <= 0:
            return None
        return self.charged_usd / self.predicted_usd

    @property
    def verdict(self) -> str:
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
            "compares_natively": self.compares_natively,
            "charged_native": round(self.charged_native, 4),
            "currency": self.currency,
            "predicted_native": (
                None if self.predicted_native is None else round(self.predicted_native, 6)
            ),
            "predicted_currency": self.predicted_currency,
            "fx_to_usd": self.fx_to_usd,
            "charged_usd": round(self.charged_usd, 6),
            "predicted_usd": round(self.predicted_usd, 6),
            "ratio": None if self.ratio is None else round(self.ratio, 3),
            "tolerance": self.tolerance,
            "min_spend_usd": self.min_spend_usd,
            "notes": self.notes,
        }

    def explain(self) -> str:
        if self.verdict == "inconclusive":
            return (
                f"inconclusive: spent ~${self.predicted_usd:.6f}, below the "
                f"${self.min_spend_usd:.2f} floor set by {self.currency} balance "
                f"rounding. Reconcile around a full eval run, not a single call."
            )
        if self.compares_natively:
            assert self.predicted_native is not None
            charged = f"{self.charged_native:.4f} {self.currency}"
            predicted = f"{self.predicted_native:.4f} {self.currency}"
            basis = "native comparison, no FX involved"
            causes = "rates changed, or another workload shares this account"
        else:
            charged = f"${self.charged_usd:.4f}"
            predicted = f"${self.predicted_usd:.4f}"
            basis = f"converted at {self.fx_to_usd} — weaker, FX participates"
            causes = (
                "rates changed, the FX rate is wrong, or another workload shares "
                "this account"
            )

        if self.verdict == "consistent":
            return (
                f"consistent: provider charged {charged}, table predicted {predicted} "
                f"(ratio {self.ratio:.3f}, within ±{self.tolerance:.0%}; {basis})"
            )
        return (
            f"TABLE SUSPECT: provider charged {charged} but the table predicted "
            f"{predicted} (ratio {self.ratio:.3f}; {basis}). Either {causes}. "
            f"Re-check the published rates before trusting any cost figure."
        )


def reader_for(provider: str) -> BalanceReader:
    try:
        return READERS[provider]()
    except KeyError as exc:
        raise ReconcileUnavailable(
            f"{provider} exposes no balance endpoint this project knows about; "
            f"reconcilable providers: {sorted(READERS)}"
        ) from exc
