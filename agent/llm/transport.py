"""Transport — send, retry, break, limit.

Policy lives here and is provider-agnostic; **detection lives in the adapters**,
which translate their SDK's exceptions into the taxonomy in `errors.py`. That
split is what makes the circuit breaker correct: it acts on error *class*, not on
attempt count, so a malformed request that fails three times does not trip a
breaker on a healthy provider.

Two things are deliberately owned here rather than delegated to the provider SDKs:

- **Retry.** Both the `anthropic` and `openai` SDKs retry by default. Layering
  three attempts on top of theirs is nine requests to a dying provider and makes
  the retry count unreadable, so adapters construct their clients with
  `max_retries=0` and this module is the only thing that retries.
- **Concurrency.** A semaphore per provider. Its justification is *eval
  throughput*, not production load — at under 1 QPS in production it is nearly
  moot, but running 30 golden cases concurrently will certainly hit 429. That
  framing is why a semaphore is sufficient and a token bucket would be theatre.

Everything time-related is injected (`sleeper`, `now`) so the retry and breaker
paths are tested offline with no network and no real delays.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Protocol

from .errors import ProviderError
from .protocol import ProviderUnavailable
from .request import LLMRequest
from .types import Response
from .usage import Usage

#: (Response, Usage) — what a successful send returns.
SendResult = tuple[Response, Usage]
SendFn = Callable[[LLMRequest], Awaitable[SendResult]]
Sleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with jitter.

    `max_attempts` counts *total* attempts, not retries after the first — so 3
    means one call and two retries. Naming it attempts avoids the off-by-one that
    "3 retries" invites.
    """

    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: bool = True

    def delay_for(self, attempt: int, retry_after: float | None, rng: random.Random) -> float:
        """Backoff before `attempt`, where `attempt` is the 1-based index of the
        attempt about to run — so the first *retry* is `attempt == 2`.

        The exponent is therefore `attempt - 2`, not `attempt - 1`: the first retry
        waits `base_delay`, the second `2 × base_delay`, and so on. Getting this
        wrong doubles every delay, which is invisible in production and shows up
        only as unexplained latency.

        A provider's own `Retry-After` always wins — it knows its recovery window
        better than our curve does.
        """
        if retry_after is not None:
            return min(retry_after, self.max_delay)
        if attempt < 2:
            return 0.0
        raw = min(self.base_delay * (2 ** (attempt - 2)), self.max_delay)
        if not self.jitter:
            return raw
        # Full jitter: uniform in [0, raw]. Avoids synchronised retries across the
        # concurrent investigations an eval run creates.
        return rng.uniform(0.0, raw)


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Opens after N *consecutive retryable* failures against one provider.

    "Consecutive retryable" rather than "three retries of one call" is the whole
    point — see [TRADEOFFS §33](../../TRADEOFFS.md#33-gateway-layering-four-layers-plus-three-cross-cutting-decorators)
    delta 6. A 400 or a 401 never moves this counter, and neither does a 429
    (delta 7: rate limiting is a quota condition, and breaking on it would fall
    back to another provider and hide the misconfiguration).

    Half-open lets exactly one probe through; success closes, failure re-opens.
    """

    threshold: int = 3
    open_seconds: float = 30.0
    now: Clock = field(default=lambda: 0.0)

    consecutive_failures: int = 0
    opened_at: float | None = None
    _probing: bool = False

    def state(self) -> BreakerState:
        if self.opened_at is None:
            return BreakerState.CLOSED
        if self.now() - self.opened_at >= self.open_seconds:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allows(self) -> bool:
        state = self.state()
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.OPEN:
            return False
        # Half-open: one probe at a time.
        if self._probing:
            return False
        self._probing = True
        return True

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None
        self._probing = False

    def record_failure(self, error: ProviderError) -> None:
        self._probing = False
        if not error.counts_toward_breaker:
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            self.opened_at = self.now()

    def force_open(self) -> None:  # for tests and for manual provider drain
        self.consecutive_failures = self.threshold
        self.opened_at = self.now()


@dataclass
class Attempt:
    """One transport attempt, recorded for the trace.

    Kept because "the provider was slow" and "we retried four times" look identical
    in a latency number, and the retry count is one of the few things that explains
    a p90 outlier.
    """

    attempt: int
    provider: str
    model_id: str
    error: str | None = None
    error_class: str | None = None
    delay_before: float = 0.0


class Adapter(Protocol):
    """What transport needs from a provider adapter."""

    provider: str

    async def send(self, request: LLMRequest) -> SendResult:
        """Issue the call, raising a `ProviderError` subclass on failure."""
        ...


@dataclass
class Transport:
    """Retry + breaker + concurrency limit around one provider adapter."""

    adapter: Adapter
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    max_concurrency: int = 4
    sleeper: Sleeper = asyncio.sleep
    rng: random.Random = field(default_factory=lambda: random.Random(0))

    _semaphore: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    def _sem(self) -> asyncio.Semaphore:
        # Created lazily: a Semaphore binds to the running loop, and the transport
        # is constructed at wiring time when there may not be one yet.
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
        return self._semaphore

    async def call(self, request: LLMRequest) -> tuple[SendResult, list[Attempt]]:
        """Send with retries. Raises the last `ProviderError`, or
        `ProviderUnavailable` if the breaker is open before we even try."""
        attempts: list[Attempt] = []

        if not self.breaker.allows():
            raise ProviderUnavailable(
                f"circuit breaker is {self.breaker.state().value} for {self.adapter.provider} "
                f"after {self.breaker.consecutive_failures} consecutive retryable failures",
                tried=[self.adapter.provider],
            )

        last: ProviderError | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            delay = 0.0
            if attempt > 1:
                assert last is not None
                delay = self.policy.delay_for(attempt, last.retry_after, self.rng)
                await self.sleeper(delay)

            record = Attempt(
                attempt=attempt,
                provider=self.adapter.provider,
                model_id=request.model_id,
                delay_before=delay,
            )
            try:
                async with self._sem():
                    result = await self.adapter.send(request)
            except ProviderError as exc:
                record.error = str(exc)
                record.error_class = type(exc).__name__
                attempts.append(record)
                self.breaker.record_failure(exc)
                last = exc
                if not exc.retryable:
                    # Nothing a second identical request can fix.
                    raise
                continue
            else:
                attempts.append(record)
                self.breaker.record_success()
                return result, attempts

        assert last is not None
        raise last
