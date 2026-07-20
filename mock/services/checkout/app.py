"""
Checkout — orchestrator.

Now calls payment (/charge) and inventory (/reserve) IN PARALLEL, because
neither depends on the other's response. Latency = max(payment, inventory)
rather than sum.

Failure policy:
  - Both must succeed → 200 with order_id
  - Either fails (HTTP 5xx OR network error) → 500 with reason
  - No compensation logic here (SAGA / dead-letter is a later lesson)

Emits both server-side metrics (from middleware) and client-side metrics
(from call_downstream, per downstream call).
"""

import asyncio
import os
import random
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from _shared.observability import (
    call_downstream,
    get_logger,
    install_observability,
    make_http_client,
)

SERVICE_TITLE = "checkout"

PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment:8000")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory:8000")

BASELINE_LATENCY_MIN_MS = 20
BASELINE_LATENCY_MAX_MS = 60
BASELINE_ERROR_RATE = 0.01  # checkout's own baseline; downstream faults are separate


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = make_http_client()
    yield
    await app.state.http.aclose()


app = FastAPI(title=SERVICE_TITLE, lifespan=lifespan)
install_observability(app)
logger = get_logger()


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_TITLE}


async def _charge(client):
    return await call_downstream(
        client,
        "POST",
        f"{PAYMENT_URL}/charge",
        target_service="payment",
        target_endpoint="/charge",
    )


async def _reserve(client):
    return await call_downstream(
        client,
        "POST",
        f"{INVENTORY_URL}/reserve",
        target_service="inventory",
        target_endpoint="/reserve",
    )


@app.post("/checkout")
async def checkout(request: Request):
    # Own baseline latency (represents checkout's local work: validation, etc.)
    local_ms = random.uniform(BASELINE_LATENCY_MIN_MS, BASELINE_LATENCY_MAX_MS)
    await asyncio.sleep(local_ms / 1000)

    if random.random() < BASELINE_ERROR_RATE:
        logger.error("checkout local failure", extra={"reason": "simulated_baseline_error"})
        return JSONResponse(status_code=500, content={"error": "internal"})

    client = request.app.state.http
    # asyncio.gather: both coroutines run concurrently on the event loop.
    # return_exceptions=True — a raised exception becomes a value in the result
    # list, so one failing does not cancel the other.
    charge_res, reserve_res = await asyncio.gather(
        _charge(client),
        _reserve(client),
        return_exceptions=True,
    )

    # Classify each downstream outcome.
    charge_ok = not isinstance(charge_res, Exception) and charge_res.status_code < 500
    reserve_ok = not isinstance(reserve_res, Exception) and reserve_res.status_code < 500

    if not (charge_ok and reserve_ok):
        reason = []
        if not charge_ok:
            reason.append(
                f"payment:{type(charge_res).__name__ if isinstance(charge_res, Exception) else charge_res.status_code}"
            )
        if not reserve_ok:
            reason.append(
                f"inventory:{type(reserve_res).__name__ if isinstance(reserve_res, Exception) else reserve_res.status_code}"
            )
        logger.error("checkout aborted", extra={"reason": ";".join(reason)})
        return JSONResponse(status_code=500, content={"error": "downstream_failed", "detail": reason})

    order_id = uuid.uuid4().hex
    logger.info(
        "checkout succeeded",
        extra={
            "order_id": order_id,
            "txn_id": charge_res.json().get("txn_id"),
            "hold_id": reserve_res.json().get("hold_id"),
        },
    )
    return {"order_id": order_id, "status": "confirmed"}
