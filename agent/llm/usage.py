"""Token usage and cost — shared layer.

[EVAL.md](../../EVAL.md) names the gateway as the sole source of cost, so
this module is where a headline number in the project's own reporting comes from.
That imposes a discipline the rest of the codebase does not need: **a cost figure
must say which price table produced it.**

Provider price sheets change, and any table checked into a repository starts
going stale the day it is written. Rather than pretend otherwise, every `Price`
carries an `as_of` date and a `verified` flag, and every ledger entry records the
table version it was priced with. A cost reported from an unverified table is
labelled as such in eval output instead of being presented as measured fact.

**No currency conversion anywhere.** Costs are reported in whatever the provider
bills in — CNY for DeepSeek, USD for Anthropic. An exchange rate would add a second,
continuously-moving source of error on top of the rates themselves, and would make a
wrong rate indistinguishable from a price change. See `Cost`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

#: Bumped whenever any price below changes. Stamped onto every ledger entry and
#: every cache entry, so a cost number stays attributable after a price edit.
PRICE_TABLE_VERSION = "2026-08-02.unverified"


@dataclass(frozen=True)
class Usage:
    """Normalised token counts.

    Providers disagree on field names and on whether cached input tokens are
    included in the input count. Each adapter is responsible for normalising to
    this shape — `input_tokens` here always means *tokens billed at the input
    rate*, with cached reads and writes broken out separately.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    #: Tokens written into the provider's prompt cache (billed at a premium).
    cache_write_tokens: int = 0
    #: Tokens served from the provider's prompt cache (billed at a discount).
    cache_read_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_write_tokens
            + self.cache_read_tokens
        )


#: Days after which an unreconciled price table is treated as stale. Providers
#: change rates on their own schedule, so a table that is merely *old* is suspect
#: even when it was verified when written. Answers "what if they raise prices":
#: the system notices the table has aged and says so, rather than confidently
#: reporting figures derived from last quarter's rates.
PRICE_TABLE_MAX_AGE_DAYS = 90


@dataclass(frozen=True)
class Price:
    """Rates per 1M tokens, in `currency`.

    `cache_write` and `cache_read` default to the ratios the major providers
    converge on (writes at 1.25x input, reads at 0.1x input). A provider that
    differs states its own numbers; a provider with no prompt cache leaves them
    at their defaults and simply never reports cache tokens.

    **Currency is explicit, and never converted.** Rates are recorded in whatever
    the provider actually bills in, and costs are reported in that same currency.
    Exchange rates move continuously, so any conversion would make a cost figure
    depend on when it was computed — and a wrong rate would be indistinguishable
    from a rate change, which is precisely the thing `reconcile.py` exists to detect.
    Denominating in the billing currency removes the ambiguity instead of recording
    it.

    The consequence is that there is no single scalar "total cost" across providers
    billing in different currencies. That turns out not to be needed: the agent runs
    on one provider and the judge on another, and their costs are separate lines in
    `by_kind` rather than a sum.
    """

    input: float
    output: float
    cache_write: float | None = None
    cache_read: float | None = None
    as_of: str = PRICE_TABLE_VERSION
    #: False until checked against the provider's published pricing. Propagates
    #: into eval output so an unverified figure is never reported as measured.
    verified: bool = False
    #: What the rates above are denominated in — and what costs are reported in.
    currency: str = "USD"

    @property
    def stale_after(self) -> str:
        """The date past which this table should be re-checked."""
        return _add_days(_date_part(self.as_of), PRICE_TABLE_MAX_AGE_DAYS)

    def is_stale(self, today: str) -> bool:
        """Whether the table has aged past its window.

        `today` is passed in rather than read from the clock so that this is
        testable and so a cost report is a pure function of its inputs.
        """
        return today > self.stale_after

    @property
    def cache_write_rate(self) -> float:
        return self.cache_write if self.cache_write is not None else self.input * 1.25

    @property
    def cache_read_rate(self) -> float:
        return self.cache_read if self.cache_read is not None else self.input * 0.1


@dataclass(frozen=True)
class Cost:
    """An amount in the currency the provider bills in. No conversion, ever.

    This is exactly what will appear on the invoice, so it never needs restating —
    unlike a converted figure, which silently depends on the exchange rate that
    applied when it was written and cannot be corrected once the original amount is
    gone.

    Dropping conversion also makes `reconcile.py` strictly sharper: the provider's
    balance delta and our prediction are in the same currency, so **no exchange rate
    participates in the check that detects price drift**. With a conversion in the
    middle, a wrong rate and a real price change produce the same signal, leaving
    that check unable to answer the one question it exists for.
    """

    native: float
    currency: str = "USD"

    def __add__(self, other: Cost) -> Cost:
        """Only same-currency costs add.

        Summing CNY and USD yields a number that is a cost in neither, which is the
        silent wrongness this type exists to prevent — so it raises. Report per
        currency instead; nothing in this system actually needs a mixed-currency
        total.
        """
        if other.currency != self.currency:
            raise ValueError(
                f"cannot add {self.currency} and {other.currency} costs — report them "
                f"per currency instead. There is deliberately no exchange rate here."
            )
        return Cost(native=self.native + other.native, currency=self.currency)

    def __str__(self) -> str:
        return f"{self.native:.6f} {self.currency}"


def cost_of(usage: Usage, price: Price) -> Cost:
    """Cost for one call, in the currency the price table is denominated in."""
    per_token = 1_000_000.0
    native = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_write_tokens * price.cache_write_rate
        + usage.cache_read_tokens * price.cache_read_rate
    ) / per_token
    return Cost(native=native, currency=price.currency)


def cache_savings(usage: Usage, price: Price) -> Cost:
    """What the prompt cache saved on this call, versus paying full input rate.

    Reported because `cache_control` breakpoint placement is claimed to be the
    single highest-leverage cost decision in the gateway ([TRADEOFFS §3](../../TRADEOFFS.md#3-llm-gateway-in-process-wrapper-not-litellmportkey-service)).
    A claim like that needs a number attached, and this is the number.
    """
    if not usage.cache_read_tokens:
        return Cost(native=0.0, currency=price.currency)
    full = usage.cache_read_tokens * price.input
    discounted = usage.cache_read_tokens * price.cache_read_rate
    return Cost(native=(full - discounted) / 1_000_000.0, currency=price.currency)


def _date_part(version: str) -> str:
    """The ISO date prefix of a price-table version like "2026-08-02.unverified"."""
    return version.split(".", 1)[0].strip()


def _add_days(iso_date: str, days: int) -> str:
    try:
        return (date.fromisoformat(iso_date) + timedelta(days=days)).isoformat()
    except ValueError:
        # An unparseable version string is treated as immediately stale rather than
        # as never stale — failing toward "check this" is the safe direction.
        return "0001-01-01"
