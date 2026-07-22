"""
Gateway — edge service. External traffic first-hop.

Responsibility (education-scale):
  1. Mint correlation_id if the caller didn't provide one (middleware does this)
  2. Route POST /orders to checkout POST /checkout, propagate response
  3. (Later, if we don't outsource to Kong) rate-limit, auth, request logging

Owns an httpx AsyncClient because it makes outbound calls. Client is
created in the FastAPI lifespan and shared for the whole process.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from _shared.fault import install_faults
from _shared.observability import (
    call_downstream,
    get_logger,
    install_observability,
    make_http_client,
)

SERVICE_TITLE = "gateway"

CHECKOUT_URL = os.getenv("CHECKOUT_URL", "http://checkout:8000")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Single AsyncClient per process — mounted on app.state.
    # Explicit lifespan gives us a clean shutdown (waits for in-flight requests
    # then closes the connection pool). Analogous to a Spring @Bean with a
    # @PreDestroy hook, or a WebClient held in a singleton bean.
    app.state.http = make_http_client()
    yield
    await app.state.http.aclose()


app = FastAPI(title=SERVICE_TITLE, lifespan=lifespan)
install_faults(app)
install_observability(app)
logger = get_logger()


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_TITLE}


@app.post("/orders")
async def create_order(request: Request):
    """Thin edge endpoint. Forwards to checkout, returns whatever checkout returns."""
    client = request.app.state.http
    try:
        resp = await call_downstream(
            client,
            "POST",
            f"{CHECKOUT_URL}/checkout",
            target_service="checkout",
            target_endpoint="/checkout",
        )
    except Exception as exc:
        # Any network-level failure: emit a 502 (Bad Gateway) — canonical for
        # "downstream did not answer". Not 500: that would suggest gateway itself
        # broke. Distinguishing helps operators triage.
        logger.error("order forward failed", extra={"reason": exc.__class__.__name__})
        return JSONResponse(status_code=502, content={"error": "upstream_unreachable"})

    return JSONResponse(status_code=resp.status_code, content=resp.json())
