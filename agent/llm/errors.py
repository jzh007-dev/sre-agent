"""Provider error taxonomy — shared layer.

The circuit breaker cannot be built before this exists. "Open the breaker after
three failed retries" is wrong, because a malformed request fails three times
while the provider is perfectly healthy — that would trip the breaker and force
an unnecessary fallback. What the breaker actually needs is *which kind* of
failure happened, so only genuine unavailability counts toward it.

The division of labour: **detection is provider-specific and lives in the
adapter; policy is provider-agnostic and lives in transport.** Each class below
carries its own policy as class attributes, so `transport.py` reads them
declaratively instead of branching on error types.

See [TRADEOFFS §33](../../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators)
deltas 6 and 7.
"""
from __future__ import annotations


class ProviderError(Exception):
    """Base for every provider failure, carrying its own handling policy."""

    #: May the same request be sent again?
    retryable: bool = False
    #: Does exhausting retries on this class suggest the provider is unavailable?
    counts_toward_breaker: bool = False

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        #: Honour the provider's own backoff hint when it sends one.
        self.retry_after = retry_after

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.provider:
            parts.append(f"provider={self.provider}")
        if self.status is not None:
            parts.append(f"status={self.status}")
        return " ".join(parts)


class AuthError(ProviderError):
    """401 / 403. A configuration bug — retrying cannot fix a wrong key, and
    falling back to another provider would hide it."""

    retryable = False
    counts_toward_breaker = False


class InvalidRequest(ProviderError):
    """400. Our bug: a bad schema, an unsupported parameter, malformed messages.
    Retrying sends the same broken request; the breaker must not count it,
    because the provider is fine."""

    retryable = False
    counts_toward_breaker = False


class ContextLimit(InvalidRequest):
    """A 400 subtype worth separating: the request exceeded the context window.

    Not retryable as-is, but uniquely *recoverable* — compaction (W3 L6) shrinks
    the request and then one retry is legitimate. Transport does not act on this
    itself; it surfaces the class so the caller can compact and re-issue.
    """


class RateLimit(ProviderError):
    """429. Retryable, but deliberately **not** counted toward the breaker.

    Rate limiting is a quota condition, not an outage. Opening the breaker would
    fall back to another provider and thereby hide a quota misconfiguration —
    the failure should stay visible. Retries exhaust and the call fails loudly.
    """

    retryable = True
    counts_toward_breaker = False


class ServerError(ProviderError):
    """5xx. The provider is genuinely failing; this is what the breaker is for."""

    retryable = True
    counts_toward_breaker = True


class Overloaded(ServerError):
    """529 (Anthropic) and equivalents — capacity, not a bug. Same policy as 5xx."""


class Timeout(ProviderError):
    """No response within the deadline. Indistinguishable from a 5xx from here."""

    retryable = True
    counts_toward_breaker = True


class ContentFilter(ProviderError):
    """The provider refused on content grounds. Not a transport problem, so it is
    surfaced rather than retried — a refusal is a signal about the prompt."""

    retryable = False
    counts_toward_breaker = False


class MalformedResponse(ProviderError):
    """The response arrived but could not be parsed into domain types.

    Retryable once because sampling varies, and counted toward the breaker
    because a provider that consistently emits unparseable output is unusable
    even though it is technically up.
    """

    retryable = True
    counts_toward_breaker = True


#: Every class, for the taxonomy table in the interview checklist and for tests
#: that assert the policy matrix has not silently changed.
TAXONOMY: tuple[type[ProviderError], ...] = (
    AuthError,
    InvalidRequest,
    ContextLimit,
    RateLimit,
    ServerError,
    Overloaded,
    Timeout,
    ContentFilter,
    MalformedResponse,
)
