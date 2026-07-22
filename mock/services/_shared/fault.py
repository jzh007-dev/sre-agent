"""
Fault injection framework.

Design (interview-relevant):
- **Per-process, module-level state** — the simplest storage that works for a
  single-instance mock env. Real prod would use a control-plane service
  (Toxiproxy, Chaos Mesh, LaunchDarkly-style feature flag) so faults can be
  set fleet-wide from one place. That trade-off is documented, not pretended-away.
- **Admin plane on the service itself** (`/admin/faults`) — POST to configure,
  GET to inspect, DELETE to clear. In prod this endpoint would live behind
  a separate port + network policy so external traffic can't reach it.
- **Middleware-level injection** — fault check runs BEFORE the endpoint logic
  in the middleware chain, so the endpoint stays business-only. Ordering
  matters: `install_faults(app)` must be called BEFORE `install_observability(app)`
  so the observability middleware wraps the fault middleware — that way
  short-circuited fault responses are recorded in `http_requests_total`
  (they ARE errors from the observer's perspective, not skipped).
- **Excluded paths** (`/metrics`, `/health`, `/admin*`) — control-plane traffic
  is never faulted; faulting `/metrics` breaks Prometheus scrapes, faulting
  `/admin` makes faults un-clearable, faulting `/health` fights liveness probes.

Fault schema (JSON):
    {
      "name": "checkout-slow",         # unique key, used for delete
      "type": "latency_ms",            # or "error_rate"
      "target_endpoint": "/checkout",  # exact match, or "*" for all business endpoints
      "config": {
        "delay_ms": 500,               # for latency_ms
        "rate": 1.0,                   # 0.0..1.0 — probability the fault fires per request
        "status_code": 500             # for error_rate; default 500
      }
    }
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# Per-process fault registry. Reset when the container restarts —
# scenarios/tests should always POST the faults they need at setup time.
_faults: dict[str, dict[str, Any]] = {}

# Paths where fault injection MUST be skipped. See design notes above.
_EXCLUDED_PREFIXES = ("/metrics", "/health", "/admin")

# Fault types this framework applies to *our own* application services
# (checkout, payment, inventory, gateway, auth, ...).
#
# Faults on infra dependencies (Redis, Postgres, Kafka, ...) are NOT injected
# through this framework — those use the middleware's own admin surface
# (redis-cli CONFIG SET, psql, kafka-configs.sh, ...). Rationale: match the
# real production shape where infra faults produce real vendor-authored
# signals (real metric names, real log lines) rather than our synthesized
# approximations. See TRADEOFFS.md §16.
#
# Two families of fault types on app services:
# - MIDDLEWARE-ACTIVE (checked in fault middleware, may short-circuit):
#     latency_ms         — sleep before endpoint
#     error_rate         — return 5xx short-circuit
#     log_pattern_emit   — emit a log line, request proceeds
#
# - SERVICE-CONSUMED (services read state, apply per their own semantics):
#     dependency_fail    — observability.call_downstream reads via check_downstream_fault()
_KNOWN_TYPES = {
    "latency_ms",
    "error_rate",
    "log_pattern_emit",
    "dependency_fail",
}


def _endpoint_excluded(endpoint: str) -> bool:
    return any(endpoint == p or endpoint.startswith(p + "/") or endpoint == p
               for p in _EXCLUDED_PREFIXES)


def _matches_target(endpoint: str, target: str) -> bool:
    return target == "*" or target == endpoint


async def _apply_one(fault: dict[str, Any], endpoint: str) -> JSONResponse | None:
    """Apply a single fault. Returns a short-circuit response if the fault
    should replace the endpoint's response; returns None otherwise (including
    for latency faults, which sleep in place then let the request proceed)."""
    if not _matches_target(endpoint, fault.get("target_endpoint", "*")):
        return None

    config = fault.get("config", {})
    if random.random() >= config.get("rate", 1.0):
        return None

    ftype = fault["type"]
    if ftype == "latency_ms":
        await asyncio.sleep(config.get("delay_ms", 0) / 1000)
        return None
    if ftype == "error_rate":
        return JSONResponse(
            status_code=config.get("status_code", 500),
            content={
                "error": "fault_injected",
                "fault_name": fault["name"],
                "fault_type": ftype,
            },
        )
    if ftype == "log_pattern_emit":
        # Emit a log line matching config.pattern; request proceeds unchanged.
        # Late import to avoid the fault↔observability circular at module load.
        from _shared.observability import logger

        logger.error(
            config.get("pattern", "simulated fault event"),
            extra={
                "fault_name": fault["name"],
                **config.get("extra", {}),
            },
        )
        return None
    # dependency_fail is service-consumed — no middleware action.
    return None


async def maybe_inject_fault(endpoint: str) -> JSONResponse | None:
    """Middleware entry point. Iterates configured faults; the first one that
    short-circuits wins. Latency faults sleep sequentially — this matches
    real-world "each fault takes its slice of the request budget"."""
    if _endpoint_excluded(endpoint):
        return None
    for fault in list(_faults.values()):
        response = await _apply_one(fault, endpoint)
        if response is not None:
            return response
    return None


# ── Service-consumed fault APIs (called from service code, not middleware) ─
#
# dependency_fail doesn't fit "inject at HTTP boundary" semantics — it
# represents *state* the service must consult and translate into its own
# failure mode (call raising ConnectError). Services call these helpers
# rather than the middleware.


def check_downstream_fault(target_service: str) -> Exception | None:
    """Called by observability.call_downstream BEFORE the actual httpx call.

    Returns an exception to raise (surfacing as outcome=conn_error in the
    client-side metric) or None to proceed with the real call. Used to
    simulate dependency-induced incidents without touching the target
    service — the caller sees the failure as if the network died."""
    for fault in list(_faults.values()):
        if fault["type"] != "dependency_fail":
            continue
        config = fault["config"]
        if config.get("target_service") != target_service:
            continue
        if random.random() >= config.get("rate", 1.0):
            continue
        return httpx.ConnectError(
            f"simulated dependency_fail on target_service={target_service}"
        )
    return None


def install_faults(app: FastAPI) -> None:
    """Register admin endpoints + fault-injection middleware on the app.

    CALL ORDER: install_faults(app) BEFORE install_observability(app).
    Reason (see design note above): FastAPI middleware runs in reverse-
    insertion order for requests, so the LAST-registered middleware is
    outermost. Registering observability last makes it the outermost wrapper,
    so it records the response's status code even when a fault short-circuits.
    """

    @app.get("/admin/faults")
    async def list_faults():
        return {"faults": list(_faults.values())}

    @app.post("/admin/faults")
    async def upsert_fault(fault: dict):
        if "name" not in fault or "type" not in fault:
            raise HTTPException(400, "'name' and 'type' are required")
        if fault["type"] not in _KNOWN_TYPES:
            raise HTTPException(
                400,
                f"unknown type {fault['type']!r}; supported: {sorted(_KNOWN_TYPES)}",
            )
        _faults[fault["name"]] = fault
        return {"ok": True, "fault": fault}

    @app.delete("/admin/faults")
    async def clear_all_faults():
        n = len(_faults)
        _faults.clear()
        return {"ok": True, "cleared": n}

    @app.delete("/admin/faults/{name}")
    async def clear_one_fault(name: str):
        removed = _faults.pop(name, None)
        if removed is None:
            raise HTTPException(404, f"no fault named {name!r}")
        return {"ok": True, "removed": removed}

    @app.middleware("http")
    async def _fault_middleware(request: Request, call_next):
        response = await maybe_inject_fault(request.url.path)
        if response is not None:
            return response
        return await call_next(request)
