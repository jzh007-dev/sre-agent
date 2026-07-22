"""
Auth service — real prod-shape login + session verification.

Session storage: real Redis (see docker-compose). Key pattern
`session:<token>` → JSON blob, TTL 24h. Uses `redis` (redis-py, industry
standard client) — this is exactly how production auth services connect
to Redis.

Two natural failure modes surface here (not fault types — endpoint-native):
1. Redis SETEX/GET fails when maxmemory is exhausted → 500 back to caller.
   Log line contains Redis's own error string ("OOM command not allowed...").
2. /verify rejects tokens with the wrong version prefix, emitting log
   "token signature mismatch: expected v2, got v1". This surfaces the
   change-induced incident shape where a deploy changes token algo and
   pre-existing cached sessions no longer verify.

Fault framework stays generic (see _shared/fault.py) — no auth-specific
fault types added.
"""

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException

from _shared.fault import install_faults
from _shared.observability import get_logger, install_observability

SERVICE_TITLE = "auth"

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
# TOKEN_VERSION is the current signature version this instance expects.
# During a rolling deploy of a token-algo change, old instances still say v1
# while new instances say v2 — cached v1 tokens fail against v2 verifiers.
TOKEN_VERSION = os.getenv("TOKEN_VERSION", "v1")
SESSION_TTL_SECONDS = 24 * 3600


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await app.state.redis.ping()
        logger.info(
            "connected to redis",
            extra={"redis_url": REDIS_URL, "token_version": TOKEN_VERSION},
        )
    except Exception as exc:
        logger.error("initial redis ping failed", extra={"reason": str(exc)})
    yield
    await app.state.redis.close()


app = FastAPI(title=SERVICE_TITLE, lifespan=lifespan)
install_faults(app)
install_observability(app)
logger = get_logger()


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_TITLE, "token_version": TOKEN_VERSION}


@app.post("/login")
async def login(payload: dict):
    username = payload.get("username")
    if not username:
        raise HTTPException(400, detail={"error": "username_required"})

    token = f"{TOKEN_VERSION}:{uuid.uuid4().hex}"
    user_id = f"u_{uuid.uuid4().hex[:8]}"
    session = {
        "user_id": user_id,
        "username": username,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "token_version": TOKEN_VERSION,
    }

    try:
        # Real Redis SETEX. On OOM, Redis returns
        # "OOM command not allowed when used memory > 'maxmemory'." —
        # this raises redis.ResponseError. On network partition,
        # redis.ConnectionError is raised. Both are surfaced as 500 to
        # the caller, with the underlying Redis error in the log.
        await app.state.redis.setex(
            f"session:{token}", SESSION_TTL_SECONDS, json.dumps(session)
        )
    except aioredis.ResponseError as exc:
        logger.error(
            f"session write failed: {exc}",
            extra={"reason": "cache_write_failed", "user_id": user_id},
        )
        raise HTTPException(500, detail={"error": "cache_unavailable", "message": str(exc)})
    except aioredis.ConnectionError as exc:
        logger.error(
            f"redis unreachable: {exc}",
            extra={"reason": "cache_conn_error"},
        )
        raise HTTPException(500, detail={"error": "cache_unavailable"})

    logger.info("login succeeded", extra={"user_id": user_id})
    return {"token": token, "user_id": user_id, "expires_in": SESSION_TTL_SECONDS}


@app.get("/verify")
async def verify(token: str):
    # Token version check — the change-induced failure mode.
    version_prefix, _, _ = token.partition(":")
    if version_prefix != TOKEN_VERSION:
        logger.error(
            f"token signature mismatch: expected {TOKEN_VERSION}, got {version_prefix}",
            extra={
                "reason": "token_version_mismatch",
                "expected_version": TOKEN_VERSION,
                "got_version": version_prefix,
            },
        )
        raise HTTPException(
            401,
            detail={"error": "invalid_token", "message": "token version mismatch"},
        )

    try:
        raw = await app.state.redis.get(f"session:{token}")
    except (aioredis.ResponseError, aioredis.ConnectionError) as exc:
        logger.error(
            f"session read failed: {exc}",
            extra={"reason": "cache_read_failed"},
        )
        raise HTTPException(500, detail={"error": "cache_unavailable"})

    if raw is None:
        raise HTTPException(401, detail={"error": "invalid_token", "message": "session not found"})

    return {"valid": True, "session": json.loads(raw)}


@app.post("/logout")
async def logout(payload: dict):
    token = payload.get("token")
    if not token:
        raise HTTPException(400, detail={"error": "token_required"})
    try:
        deleted = await app.state.redis.delete(f"session:{token}")
    except (aioredis.ResponseError, aioredis.ConnectionError):
        # Logout is best-effort; leaked sessions expire via TTL.
        deleted = 0
    return {"ok": True, "deleted": deleted}
