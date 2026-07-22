"""
Incident tracker — PagerDuty-shaped mock service.

Purpose: close the alert → incident loop. AlertManager POSTs webhook payloads
here; the service stores them as incidents with the PagerDuty state machine
(triggered → acked → resolved). The SRE agent will later consume the incident
list via a small MCP tool and update state as it works.

Not a real PagerDuty — no on-call schedules, no escalation policies, no
notifications. Just the state machine + a REST shape close enough to PD's
Incidents API that a production migration is a base-URL change.

Endpoints:
  POST /webhook/alertmanager   — AlertManager posts here; ingests/updates incidents
  GET  /incidents              — list incidents (query ?state=... to filter)
  GET  /incidents/{id}         — get one
  PATCH /incidents/{id}        — update state (triggered → acked → resolved)
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
import uuid

from fastapi import FastAPI, HTTPException

from _shared.fault import install_faults
from _shared.observability import get_logger, install_observability

SERVICE_TITLE = "incident-tracker"

# Per-process incident store. Fine for the mock — real PD persists to Postgres.
# Keyed by AlertManager fingerprint so re-fired alerts update the same incident.
_incidents: dict[str, dict[str, Any]] = {}

_VALID_STATES = {"triggered", "acked", "resolved"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("incident-tracker up")
    yield


app = FastAPI(title=SERVICE_TITLE, lifespan=lifespan)
install_faults(app)
install_observability(app)
logger = get_logger()


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_TITLE}


@app.post("/webhook/alertmanager")
async def alertmanager_webhook(payload: dict[str, Any]):
    """
    Ingests AlertManager webhook payloads.
    Payload shape: https://prometheus.io/docs/alerting/latest/configuration/#webhook_config

    Behavior:
    - New fingerprint → create incident in state=triggered
    - Existing fingerprint + status=firing → refresh updated_at
    - Existing fingerprint + status=resolved → set state=resolved (unless human already acked)
    """
    alerts = payload.get("alerts", [])
    n_new = 0
    n_updated = 0
    n_resolved = 0
    for alert in alerts:
        fingerprint = alert.get("fingerprint") or uuid.uuid4().hex
        am_status = alert.get("status", "firing")
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})

        existing = _incidents.get(fingerprint)
        if existing is None:
            _incidents[fingerprint] = {
                "id": fingerprint,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "alertmanager_status": am_status,
                "state": "triggered",
                "alert_name": labels.get("alertname"),
                "service": labels.get("service"),
                "target_service": labels.get("target_service"),
                "severity": labels.get("severity"),
                "labels": labels,
                "annotations": annotations,
                "starts_at": alert.get("startsAt"),
                "ends_at": alert.get("endsAt"),
            }
            n_new += 1
        else:
            existing["alertmanager_status"] = am_status
            existing["updated_at"] = _now_iso()
            existing["ends_at"] = alert.get("endsAt")
            if am_status == "resolved" and existing["state"] != "resolved":
                # AlertManager says resolved. Human may still want to close manually.
                # For MVP: auto-resolve on AM resolve.
                existing["state"] = "resolved"
                n_resolved += 1
            else:
                n_updated += 1

    logger.info(
        "webhook ingested",
        extra={"n_new": n_new, "n_updated": n_updated, "n_resolved": n_resolved},
    )
    return {"ok": True, "new": n_new, "updated": n_updated, "resolved": n_resolved}


@app.get("/incidents")
async def list_incidents(state: str | None = None):
    items = list(_incidents.values())
    if state:
        items = [i for i in items if i["state"] == state]
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return {"count": len(items), "incidents": items}


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: str):
    incident = _incidents.get(incident_id)
    if incident is None:
        raise HTTPException(404, f"incident {incident_id!r} not found")
    return incident


@app.patch("/incidents/{incident_id}")
async def update_incident(incident_id: str, patch: dict[str, Any]):
    incident = _incidents.get(incident_id)
    if incident is None:
        raise HTTPException(404, f"incident {incident_id!r} not found")
    if "state" in patch:
        state = patch["state"]
        if state not in _VALID_STATES:
            raise HTTPException(
                400,
                f"invalid state {state!r}; must be one of {sorted(_VALID_STATES)}",
            )
        incident["state"] = state
    if "notes" in patch:
        incident.setdefault("notes", []).append(
            {"ts": _now_iso(), "text": patch["notes"]}
        )
    incident["updated_at"] = _now_iso()
    return incident
