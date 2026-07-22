"""
Deploy history — mock of a real deploy pipeline's audit surface.

In production this is ArgoCD's application history, Spinnaker's pipeline
records, or GitHub Actions run logs — a REST surface listing recent
deploys, their configs, and diffs. The Week 2 `deploy-mcp` tool will
query the equivalent real surface; here we surface a small JSON store
that case setup.yaml files pre-populate before running a scenario.

Shape kept close to what ArgoCD exposes:
  GET /deploys?service=X&limit=N   — recent deploys for a service
  GET /deploys/{deploy_id}         — single deploy detail with config diff
  POST /deploys                    — append a record (used by case runner)
  DELETE /deploys                  — clear all (used by case runner cleanup)

Storage is a JSON file mounted from the host so state survives container
restarts within a case run.
"""

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException

from _shared.fault import install_faults
from _shared.observability import get_logger, install_observability

SERVICE_TITLE = "deploy-history"
STORE_PATH = os.getenv("DEPLOY_STORE_PATH", "/data/deploys.json")

_deploys: list[dict[str, Any]] = []


def _load() -> None:
    global _deploys
    if os.path.exists(STORE_PATH):
        try:
            with open(STORE_PATH) as f:
                _deploys = json.load(f) or []
        except Exception:
            _deploys = []
    else:
        _deploys = []


def _save() -> None:
    os.makedirs(os.path.dirname(STORE_PATH) or ".", exist_ok=True)
    with open(STORE_PATH, "w") as f:
        json.dump(_deploys, f, indent=2, sort_keys=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load()
    logger.info(
        "deploy-history loaded",
        extra={"records": len(_deploys), "store_path": STORE_PATH},
    )
    yield


app = FastAPI(title=SERVICE_TITLE, lifespan=lifespan)
install_faults(app)
install_observability(app)
logger = get_logger()


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_TITLE, "records": len(_deploys)}


@app.get("/deploys")
async def list_deploys(service: str | None = None, limit: int = 20):
    items = _deploys
    if service:
        items = [d for d in items if d.get("service") == service]
    items = sorted(items, key=lambda d: d.get("timestamp", ""), reverse=True)[:limit]
    return {"count": len(items), "deploys": items}


@app.get("/deploys/{deploy_id}")
async def get_deploy(deploy_id: str):
    for d in _deploys:
        if d.get("id") == deploy_id:
            return d
    raise HTTPException(404, detail={"error": "deploy_not_found"})


@app.post("/deploys")
async def add_deploy(payload: dict):
    payload.setdefault("id", f"dep_{len(_deploys) + 1:04d}")
    payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    _deploys.append(payload)
    _save()
    logger.info(
        "deploy recorded",
        extra={"deploy_id": payload["id"], "service": payload.get("service")},
    )
    return payload


@app.delete("/deploys")
async def clear_deploys():
    n = len(_deploys)
    _deploys.clear()
    _save()
    return {"ok": True, "cleared": n}
