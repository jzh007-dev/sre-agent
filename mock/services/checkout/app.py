"""
Checkout — mock microservice for sre-agent.

Exposes:
  GET  /health     — liveness probe
  POST /checkout   — business endpoint, simulates work + baseline error rate
  GET  /metrics    — Prometheus scrape endpoint

Design notes:
- All observability lives in middleware (metrics + access log). Business code
  stays focused on business.
- Logs are JSON, one line per event, always carry correlation_id.
- Blocking calls in async endpoints would freeze the event loop. Use asyncio.sleep,
  not time.sleep, to simulate latency.
"""

import asyncio
import json
import logging
import os
import random
import time
import uuid
from contextvars import ContextVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# ── Config ─────────────────────────────────────────────────────────
SERVICE_NAME = os.getenv("SERVICE_NAME", "checkout")
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "v1.0.0")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Baseline behavior — knobs L6 (fault injection) will hijack.
BASELINE_LATENCY_MIN_MS = 20
BASELINE_LATENCY_MAX_MS = 150
BASELINE_ERROR_RATE = 0.02  # 2%

# ── Correlation ID (Python's MDC) ──────────────────────────────────
# ContextVar propagates cleanly across asyncio tasks. Do NOT use threading.local
# in async code — coroutine switches would leak IDs between requests.
correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


# ── Structured JSON logging ────────────────────────────────────────
class JsonFormatter(logging.Formatter):
    """One-line JSON per log record. Every record carries service + correlation_id."""

    RESERVED = {
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
        # Merge extras passed via `logger.info(msg, extra={...})`.
        for k, v in record.__dict__.items():
            if k not in self.RESERVED and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


logger = logging.getLogger(SERVICE_NAME)
logger.setLevel(LOG_LEVEL)
_handler = logging.StreamHandler()
_handler.setFormatter(JsonFormatter())
logger.handlers = [_handler]
logger.propagate = False


# ── Metrics ────────────────────────────────────────────────────────
# RED: Rate + Errors + Duration. This handles all three from labels alone.
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


# ── App + middleware ───────────────────────────────────────────────
app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION)


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.middleware("http")
async def observe(request: Request, call_next):
    """
    Owns ALL request observability:
      1. correlation_id in/out
      2. counter on every completed request
      3. histogram on every completed request
      4. one access-log line per request
    """
    # 1. correlation_id — accept caller-supplied, else generate.
    corr = request.headers.get("x-correlation-id") or uuid.uuid4().hex[:12]
    correlation_id.set(corr)

    endpoint = request.url.path
    method = request.method
    start = time.perf_counter()

    response = await call_next(request)
    duration = time.perf_counter() - start

    # 2 & 3. Metrics.
    requests_total.labels(method, endpoint, response.status_code).inc()
    request_duration.labels(endpoint).observe(duration)

    # 4. Access log.
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


# ── Endpoints ──────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_NAME, "version": SERVICE_VERSION}


@app.post("/checkout")
async def checkout():
    # Simulate business work (async sleep — never time.sleep in async code).
    latency_ms = random.uniform(BASELINE_LATENCY_MIN_MS, BASELINE_LATENCY_MAX_MS)
    await asyncio.sleep(latency_ms / 1000)

    # Baseline error injection.
    if random.random() < BASELINE_ERROR_RATE:
        logger.error(
            "checkout failed",
            extra={"reason": "simulated_baseline_error"},
        )
        return JSONResponse(status_code=500, content={"error": "internal"})

    order_id = uuid.uuid4().hex
    logger.info("checkout succeeded", extra={"order_id": order_id})
    return {"order_id": order_id, "status": "confirmed"}
