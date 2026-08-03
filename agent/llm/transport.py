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

Everything time-related is injected (`sleeper`, `now`, `clock`) so the retry,
breaker and duration paths are tested offline with no network and no real delays.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Awaitable, Callable, Protocol, Union

from . import errors
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
    a p90 outlier — but only with `duration_ms` alongside it. Until L4a the count
    was the only field the gateway read, so the distinction this type exists to make
    was recorded and then thrown away
    ([TRADEOFFS §42](../../TRADEOFFS.md#42-traceability-one-id-four-sinks--and-an-honest-audit-of-what-is-currently-wired)).
    """

    attempt: int
    provider: str
    model_id: str
    error: str | None = None
    error_class: str | None = None
    delay_before: float = 0.0
    #: Time in the adapter, excluding `delay_before`. The two are separate because
    #: backoff is our latency and the send is the provider's, and a total that mixed
    #: them would make our own retry curve look like a slow provider.
    duration_ms: float = 0.0


#: Called as each attempt completes, successful or not.
#:
#: A per-call parameter rather than a field on `Transport`: one transport is shared
#: by every investigation hitting that provider, so a field would race. It also
#: catches the failure path, where the attempt list is otherwise lost with the
#: raised exception — which is the path worth tracing.
AttemptSink = Callable[[Attempt], None]


class Adapter(Protocol):
    """What transport needs from a provider adapter."""

    provider: str

    async def send(self, request: LLMRequest) -> SendResult:
        """Issue the call, raising a `ProviderError` subclass on failure."""
        ...


class StreamingAdapter(Adapter, Protocol):
    """An adapter that can also stream.

    Optional by design: `Transport.call` works with any `Adapter`, and only
    `stream()` requires this. A provider without streaming support degrades to
    non-streaming rather than being unusable.
    """

    def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        """Yield chunks, ending with exactly one `StreamDone`."""
        ...


@dataclass(frozen=True)
class TextChunk:
    """A fragment of assistant text. Maps onto `core.events.TextDelta`."""

    text: str


@dataclass(frozen=True)
class StreamDone:
    """Terminal chunk: the accumulated response and its usage.

    Streaming still produces a complete `Response` at the end, because the loop
    needs the assembled `tool_use` blocks to dispatch and the cache needs something
    to store. Streaming is a *side channel* for latency, not a different result
    type — which is why the `LLM` protocol signature never changed.
    """

    response: Response
    usage: Usage


StreamChunk = Union[TextChunk, StreamDone]


@dataclass
class Transport:
    """Retry + breaker + concurrency limit around one provider adapter."""

    adapter: Adapter
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    max_concurrency: int = 4
    sleeper: Sleeper = asyncio.sleep
    rng: random.Random = field(default_factory=lambda: random.Random(0))
    #: Injected like `sleeper` and `breaker.now`, so an attempt's duration is exact
    #: in a test rather than whatever the machine was doing that second.
    clock: Clock = time.perf_counter

    _semaphore: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    def _sem(self) -> asyncio.Semaphore:
        # Created lazily: a Semaphore binds to the running loop, and the transport
        # is constructed at wiring time when there may not be one yet.
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
        return self._semaphore

    async def call(
        self,
        request: LLMRequest,
        on_attempt: AttemptSink | None = None,
    ) -> tuple[SendResult, list[Attempt]]:
        """Send with retries. Raises the last `ProviderError`, or
        `ProviderUnavailable` if the breaker is open before we even try.

        `on_attempt` fires as each attempt finishes, so the caller sees every one
        even when the call ultimately raises and the returned list never arrives.
        """
        attempts: list[Attempt] = []

        def finish(record: Attempt, started: float) -> None:
            record.duration_ms = (self.clock() - started) * 1000.0
            attempts.append(record)
            if on_attempt is not None:
                on_attempt(record)

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
            # Started after the backoff sleep: `delay_before` already carries that,
            # and double-counting it would attribute our own wait to the provider.
            started = self.clock()
            try:
                async with self._sem():
                    result = await self.adapter.send(request)
            except ProviderError as exc:
                record.error = str(exc)
                record.error_class = type(exc).__name__
                finish(record, started)
                self.breaker.record_failure(exc)
                last = exc
                if not exc.retryable:
                    # Nothing a second identical request can fix.
                    raise
                continue
            else:
                finish(record, started)
                self.breaker.record_success()
                return result, attempts

        assert last is not None
        raise last

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        """Stream one attempt. **No retries.**

        Deliberate: once a partial response has been shown to a human or appended to
        a digest, silently restarting would replay text the caller already saw. A
        stream that fails is surfaced so the caller decides — retry from scratch, or
        keep the partial and continue. The breaker is still updated, because a
        failed stream is evidence about the provider either way.
        """
        adapter = self.adapter
        if not hasattr(adapter, "stream"):
            raise errors.InvalidRequest(
                f"{adapter.provider} adapter does not support streaming",
                provider=adapter.provider,
            )

        if not self.breaker.allows():
            raise ProviderUnavailable(
                f"circuit breaker is {self.breaker.state().value} for {adapter.provider}",
                tried=[adapter.provider],
            )

        try:
            async with self._sem():
                async for chunk in adapter.stream(request):  # type: ignore[attr-defined]
                    yield chunk
        except ProviderError as exc:
            self.breaker.record_failure(exc)
            raise
        else:
            self.breaker.record_success()
