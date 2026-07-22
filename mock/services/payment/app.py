"""
Payment — leaf service. Simulates charging a card.

Leaf = does not call anything downstream, so no httpx client here.
Baseline latency 30-200ms, baseline error rate 3%. L6 fault-injection will
be able to tune these knobs via /admin/faults.
"""

import asyncio
import random
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from _shared.fault import install_faults
from _shared.observability import get_logger, install_observability

SERVICE_TITLE = "payment"

BASELINE_LATENCY_MIN_MS = 30
BASELINE_LATENCY_MAX_MS = 200
BASELINE_ERROR_RATE = 0.03  # 3% — slightly higher than checkout so faults are visible

app = FastAPI(title=SERVICE_TITLE)
install_faults(app)
install_observability(app)
logger = get_logger()


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_TITLE}


@app.post("/charge")
async def charge():
    latency_ms = random.uniform(BASELINE_LATENCY_MIN_MS, BASELINE_LATENCY_MAX_MS)
    await asyncio.sleep(latency_ms / 1000)

    if random.random() < BASELINE_ERROR_RATE:
        logger.error("charge failed", extra={"reason": "simulated_baseline_error"})
        return JSONResponse(status_code=500, content={"error": "charge_declined"})

    txn_id = uuid.uuid4().hex
    logger.info("charge succeeded", extra={"txn_id": txn_id})
    return {"txn_id": txn_id, "status": "captured"}
