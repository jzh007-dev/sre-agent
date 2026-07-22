"""
Session cache — Redis-shaped mock service.

Real production uses Redis with periodic bgsave forks for durability. Under
memory pressure the bgsave fork can fail with "Cannot allocate memory";
downstream services see cache SET/GET returning errors.

Not a real Redis. We synthesize the failure mode via the `memory_pressure`
fault: a gauge `session_cache_memory_bytes` linearly ramps to `target_bytes`
over `ramp_seconds`; past `threshold_bytes`, /set returns 500 with the
canonical Redis OOM log signature.

The gauge is updated by a 1 Hz background task rather than at scrape time
because real Redis exports memory continuously — the shape (gradual climb,
then failure) is what the SRE agent must recognize.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from prometheus_client import Gauge

from _shared.fault import get_memory_pressure_state, install_faults
from _shared.observability import get_logger, install_observability

SERVICE_TITLE = "session-cache"

# Simulated baseline memory usage — the fault ramps up from here.
_BASELINE_MEMORY_BYTES = 100_000_000  # 100 MB

# In-memory KV store — the "cache" itself. Not persisted anywhere.
_store: dict[str, str] = {}

memory_bytes = Gauge(
    "session_cache_memory_bytes",
    "Simulated memory usage of the session cache (mock).",
)
memory_bytes.set(_BASELINE_MEMORY_BYTES)


async def _memory_metric_updater():
    """Poll the memory_pressure fault state at 1 Hz and update the gauge.
    Real Redis exports this natively; we synthesize it from fault config."""
    logger.info("memory metric updater started")
    while True:
        try:
            state = get_memory_pressure_state()
            if state is not None:
                memory_bytes.set(state["current_bytes"])
            else:
                memory_bytes.set(_BASELINE_MEMORY_BYTES)
        except Exception as exc:
            logger.error("memory updater tick failed", extra={"reason": str(exc)})
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_memory_metric_updater())
    yield
    task.cancel()


app = FastAPI(title=SERVICE_TITLE, lifespan=lifespan)
install_faults(app)
install_observability(app)
logger = get_logger()


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_TITLE}


@app.post("/set")
async def set_key(payload: dict[str, Any]):
    key = payload.get("key")
    value = payload.get("value")
    if not key:
        raise HTTPException(400, "'key' required")

    # Consult memory_pressure state — this is the OOM path.
    # Failure surfaces both as the HTTP response AND as the canonical Redis
    # bgsave OOM log line, which the SRE agent grep should catch.
    state = get_memory_pressure_state()
    if state and state["should_fail"]:
        logger.error(
            state["failure_message"],
            extra={"key": key, "reason": "bgsave_oom"},
        )
        raise HTTPException(
            500,
            detail={"error": "bgsave_failed", "message": state["failure_message"]},
        )

    _store[key] = value
    return {"ok": True, "key": key}


@app.get("/get")
async def get_key(key: str):
    value = _store.get(key)
    if value is None:
        raise HTTPException(404, "key not found")
    return {"key": key, "value": value}


@app.get("/keys")
async def list_keys():
    return {"count": len(_store), "keys": list(_store.keys())[:100]}
