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

**Currency.** Balance comes back in the provider's billing currency (CNY for
DeepSeek) while the ledger is USD, so the comparison needs an explicit FX rate. It
is passed in and recorded, never guessed — a wrong-but-hidden conversion would look
exactly like a price change.
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
    currency: str
    balance_before: float
    balance_after: float
    fx_to_usd: float
    predicted_usd: float
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
    def below_resolution(self) -> bool:
        """Whether the spend was too small for the balance endpoint to resolve."""
        return max(self.charged_usd, self.predicted_usd) < self.min_spend_usd

    @property
    def ratio(self) -> float | None:
        """Actual / predicted. >1 means we are under-reporting cost."""
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
            "charged_native": round(self.charged_native, 4),
            "currency": self.currency,
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
        if self.verdict == "consistent":
            return (
                f"consistent: provider charged ${self.charged_usd:.4f}, table "
                f"predicted ${self.predicted_usd:.4f} "
                f"(ratio {self.ratio:.3f}, within ±{self.tolerance:.0%})"
            )
        return (
            f"TABLE SUSPECT: provider charged ${self.charged_usd:.4f} but the table "
            f"predicted ${self.predicted_usd:.4f} (ratio {self.ratio:.3f}). Either "
            f"rates changed, the FX rate is wrong, or another workload shares this "
            f"account. Re-check the published rates before trusting any cost figure."
        )


def reader_for(provider: str) -> BalanceReader:
    try:
        return READERS[provider]()
    except KeyError as exc:
        raise ReconcileUnavailable(
            f"{provider} exposes no balance endpoint this project knows about; "
            f"reconcilable providers: {sorted(READERS)}"
        ) from exc
