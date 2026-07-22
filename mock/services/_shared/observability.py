"""
Shared observability for all mock services.

Every service imports from this module so the four services stay consistent on:
  - correlation_id propagation (in/out via header, in-process via ContextVar)
  - JSON-line logs that always carry service + correlation_id
  - RED metrics on the server side (http_requests_total, http_request_duration_seconds)
  - Client-side metrics on downstream calls (downstream_requests_total, downstream_duration_seconds)
  - httpx AsyncClient factory that auto-injects the correlation header

Design notes (interview-relevant):
- ContextVar, not threading.local: async-safe, propagates across `await`.
- Client-side and server-side metrics coexist on purpose. The delta
  `client.calls - server.receives` is the ONLY way to observe network/pool/DNS
  failures — the server-side counter never fires for requests that never arrive.
- `target_endpoint` MUST be a route pattern ("/orders/{id}"), not a raw URL,
  or every order_id becomes a new time series and cardinality explodes.
- One AsyncClient per process, shared via app.state.http. Per-request `new` would
  bypass the connection pool and blow up latency.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextvars import ContextVar

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)

# ── Service identity (read from env at import time) ───────────────────
SERVICE_NAME = os.getenv("SERVICE_NAME", "unknown")
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "v0.0.0")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── Correlation ID (Python's MDC) ─────────────────────────────────────
correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


# ── Structured JSON logging ───────────────────────────────────────────
class _JsonFormatter(logging.Formatter):
    """One-line JSON per record. Always stamps service + correlation_id."""

    _RESERVED = {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "msg": record.getMessage(),
            "correlation_id": correlation_id.get(),
        }
        for k, v in record.__dict__.items():
            if k not in self._RESERVED and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger() -> logging.Logger:
    logger = logging.getLogger(SERVICE_NAME)
    if not logger.handlers:
        logger.setLevel(LOG_LEVEL)
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logger.handlers = [handler]
        logger.propagate = False
    return logger


logger = get_logger()


# ── Metrics: server side (this service, inbound) ──────────────────────
requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests received.",
    ["method", "endpoint", "status"],
)
request_duration = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# ── Metrics: client side (this service, outbound) ─────────────────────
downstream_requests_total = Counter(
    "downstream_requests_total",
    "Outbound HTTP requests from this service to downstream services.",
    ["target_service", "target_endpoint", "method", "outcome", "status_code"],
)
downstream_duration_seconds = Histogram(
    "downstream_request_duration_seconds",
    "Latency of outbound requests, measured client-side (includes network + queueing).",
    ["target_service", "target_endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


# ── FastAPI wiring ────────────────────────────────────────────────────
def install_observability(app: FastAPI) -> None:
    """Register /metrics endpoint + observe-all middleware on the given app."""

    @app.get("/metrics")
    async def _metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def _observe(request: Request, call_next):
        # Skip control-plane paths:
        # - /metrics: Prometheus self-scrapes ~17k/day, would dominate counters
        #   and dilute the latency histogram (scrapes are <1ms).
        # - /admin/*: fault-injection control plane, not user traffic.
        # - /health: liveness probes, ditto.
        path = request.url.path
        if (path == "/metrics"
                or path == "/health"
                or path == "/admin"
                or path.startswith("/admin/")):
            return await call_next(request)

        # Correlation: accept caller header, else mint one.
        corr = request.headers.get("x-correlation-id") or uuid.uuid4().hex[:12]
        correlation_id.set(corr)

        endpoint = request.url.path
        method = request.method
        start = time.perf_counter()

        response = await call_next(request)
        duration = time.perf_counter() - start

        requests_total.labels(method, endpoint, response.status_code).inc()
        request_duration.labels(endpoint).observe(duration)

        response.headers["x-correlation-id"] = corr
        logger.info(
            f"{method} {endpoint} -> {response.status_code}",
            extra={
                "endpoint": endpoint,
                "method": method,
                "status": response.status_code,
                "duration_ms": round(duration * 1000, 2),
            },
        )
        return response


# ── httpx: outbound client with correlation auto-injection ────────────
async def _inject_correlation(request: httpx.Request) -> None:
    corr = correlation_id.get()
    if corr and "x-correlation-id" not in request.headers:
        request.headers["x-correlation-id"] = corr


def make_http_client(
    *,
    timeout_seconds: float = 3.0,
    max_connections: int = 100,
    max_keepalive: int = 20,
) -> httpx.AsyncClient:
    """
    Build an AsyncClient with sane pool limits + auto correlation-header injection.
    Hold ONE per process on app.state.http; do NOT construct per-request.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive,
        ),
        event_hooks={"request": [_inject_correlation]},
    )


async def call_downstream(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    target_service: str,
    target_endpoint: str,  # ROUTE PATTERN, not raw URL
    **kwargs,
) -> httpx.Response:
    """
    Wrap an outbound call with client-side metrics + a structured log line.

    Records the metric on ANY outcome (success, HTTP error, timeout, conn error).
    Re-raises network errors so the caller decides fallback vs propagate.
    """
    start = time.perf_counter()
    status_code = "none"
    outcome = "ok"
    try:
        response = await client.request(method, url, **kwargs)
        status_code = str(response.status_code)
        if 500 <= response.status_code < 600:
            outcome = "http_5xx"
        elif 400 <= response.status_code < 500:
            outcome = "http_4xx"
        return response
    # Timeout branches — ordered specific-to-general because PoolTimeout /
    # ConnectTimeout / ReadTimeout all inherit from httpx.TimeoutException.
    # Each maps to a different root-cause hypothesis (see TRADEOFFS if written).
    except httpx.PoolTimeout:
        outcome = "pool_timeout"      # local pool exhausted; slot never freed
        raise
    except httpx.ConnectTimeout:
        outcome = "connect_timeout"   # remote didn't complete TCP/TLS in time
        raise
    except httpx.ReadTimeout:
        outcome = "read_timeout"      # remote alive but slow producing response
        raise
    except httpx.TimeoutException:
        outcome = "timeout"           # write timeout / catch-all
        raise
    except httpx.ConnectError:
        outcome = "conn_error"        # DNS / refused / unreachable — remote not there
        raise
    except httpx.HTTPError:
        outcome = "http_error"        # anything else httpx knows about
        raise
    finally:
        duration = time.perf_counter() - start
        downstream_requests_total.labels(
            target_service=target_service,
            target_endpoint=target_endpoint,
            method=method.upper(),
            outcome=outcome,
            status_code=status_code,
        ).inc()
        downstream_duration_seconds.labels(
            target_service=target_service,
            target_endpoint=target_endpoint,
        ).observe(duration)
        logger.info(
            f"downstream {method.upper()} {target_service}{target_endpoint} -> {outcome} {status_code}",
            extra={
                "target_service": target_service,
                "target_endpoint": target_endpoint,
                "method": method.upper(),
                "outcome": outcome,
                "status_code": status_code,
                "duration_ms": round(duration * 1000, 2),
            },
        )
