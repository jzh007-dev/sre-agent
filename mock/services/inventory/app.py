"""
Inventory — leaf service. Simulates reserving stock.

Leaf, no httpx. Slightly faster than payment (in-memory-ish work).
Baseline error rate 1% — inventory is cheap and usually available.
"""

import asyncio
import random
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from _shared.fault import install_faults
from _shared.observability import get_logger, install_observability

SERVICE_TITLE = "inventory"

BASELINE_LATENCY_MIN_MS = 10
BASELINE_LATENCY_MAX_MS = 80
BASELINE_ERROR_RATE = 0.01

app = FastAPI(title=SERVICE_TITLE)
install_faults(app)
install_observability(app)
logger = get_logger()


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_TITLE}


@app.post("/reserve")
async def reserve():
    latency_ms = random.uniform(BASELINE_LATENCY_MIN_MS, BASELINE_LATENCY_MAX_MS)
    await asyncio.sleep(latency_ms / 1000)

    if random.random() < BASELINE_ERROR_RATE:
        logger.error("reserve failed", extra={"reason": "simulated_baseline_error"})
        return JSONResponse(status_code=500, content={"error": "out_of_stock"})

    hold_id = uuid.uuid4().hex
    logger.info("reserve succeeded", extra={"hold_id": hold_id})
    return {"hold_id": hold_id, "status": "reserved"}
