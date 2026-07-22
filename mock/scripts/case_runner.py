"""
Case runner — apply a golden case's setup.yaml against the running mock env,
poll AlertManager for expected alerts, dump the tracked incident.

Usage
-----
    python mock/scripts/case_runner.py --list
    python mock/scripts/case_runner.py <case-id>

The runner is the piece that connects a golden case fixture to the live mock
env. It reads `setup.yaml` and applies four kinds of state changes:

  1. `infra_commands`   — arbitrary commands run inside a compose container
                          (e.g. `redis-cli CONFIG SET maxmemory 5mb`). This is
                          how faults on infra dependencies are applied, using
                          the middleware's own admin surface.
  2. `deploy_records`   — HTTP POSTs to the deploy-history mock, seeding
                          "what deploys happened before this incident".
  3. `app_faults`       — POSTs to /admin/faults on our own application
                          services (fault types from _shared/fault.py).
  4. `load`             — concurrent HTTP calls to drive traffic through
                          the fault; without load, alert rules don't fire.

The runner **never reads** `expected.yaml`. That file is ground truth for
the LLM-judge in the Week 5+ eval pipeline; feeding it into the runner
would defeat the "agent gets zero context" principle.

Observed outputs (printed to stdout):
  - AlertManager active alerts at the end of the run
  - `incident-tracker` state (webhooks it received)

The runner is agnostic to which middleware or service the case exercises —
adding a Postgres OOM case is `target: postgres` in setup.yaml, zero runner
code changes.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CASE_ROOT = REPO_ROOT / "eval" / "golden"

# Host ports of services with /admin/faults endpoints. Adding a new app service
# means adding one line here; case setup.yaml then targets it by service name.
APP_SERVICE_PORTS = {
    "gateway": 18080,
    "checkout": 18081,
    "payment": 18082,
    "inventory": 18083,
    "incident-tracker": 18085,
    "auth": 18086,
    "deploy-history": 18087,
}

DEPLOY_HISTORY_URL = "http://localhost:18087"
ALERTMANAGER_URL = "http://localhost:19093"
INCIDENT_TRACKER_URL = "http://localhost:18085"


# ── Phase 1: reset state before each run ────────────────────────────────────
def clear_state() -> None:
    """Best-effort cleanup so runs don't leak state across cases."""
    with httpx.Client(timeout=5.0) as client:
        # Clear app-fault registries on every service that has one.
        for name, port in APP_SERVICE_PORTS.items():
            try:
                client.delete(f"http://localhost:{port}/admin/faults")
            except Exception:
                pass
        # Clear deploy history.
        try:
            client.delete(f"{DEPLOY_HISTORY_URL}/deploys")
        except Exception:
            pass

    # Reset Redis to a clean maxmemory=0 (unlimited) and empty keyspace.
    _docker_exec("mock-redis", ["redis-cli", "FLUSHALL"], quiet=True)
    _docker_exec("mock-redis", ["redis-cli", "CONFIG", "SET", "maxmemory", "0"], quiet=True)


# ── Phase 2: apply case setup ───────────────────────────────────────────────
def _docker_exec(container: str, cmd: list[str], quiet: bool = False) -> None:
    """Run a shell command inside a compose container."""
    if not quiet:
        print(f"  [infra] docker exec {container} {' '.join(cmd)}")
    try:
        r = subprocess.run(
            ["docker", "exec", container, *cmd],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0 and not quiet:
            print(f"    ! exit {r.returncode}: {r.stderr.strip()[:200]}")
        elif r.stdout.strip() and not quiet:
            print(f"    → {r.stdout.strip()[:200]}")
    except subprocess.TimeoutExpired:
        if not quiet:
            print(f"    ! timeout")


def apply_infra_commands(commands: list[dict[str, Any]]) -> None:
    for c in commands or []:
        _docker_exec(c["target"], c["cmd"])


def apply_deploy_records(records: list[dict[str, Any]]) -> None:
    if not records:
        return
    with httpx.Client(timeout=5.0) as client:
        for r in records:
            resp = client.post(f"{DEPLOY_HISTORY_URL}/deploys", json=r)
            body = resp.json()
            print(f"  [deploy] {body.get('id')} service={body.get('service')} version={body.get('version')}")


def apply_app_faults(faults: list[dict[str, Any]]) -> None:
    with httpx.Client(timeout=5.0) as client:
        for f in faults or []:
            service = f["service"]
            spec = f["spec"]
            port = APP_SERVICE_PORTS.get(service)
            if port is None:
                print(f"  [fault] WARN unknown service {service!r}, skipping")
                continue
            resp = client.post(f"http://localhost:{port}/admin/faults", json=spec)
            if resp.status_code < 300:
                print(f"  [fault] {service}: {spec.get('name')} ({spec.get('type')})")
            else:
                print(f"  [fault] FAILED {service}: HTTP {resp.status_code}: {resp.text[:200]}")


# ── Phase 3: generate load + poll alerts concurrently ───────────────────────
async def _one_burst(spec: dict[str, Any], end_ts: float) -> int:
    method = spec.get("method", "POST").upper()
    url = spec["url"]
    body = spec.get("body")
    rate = spec.get("rate_per_sec", 1)
    interval = 1.0 / max(rate, 0.1)
    n_sent = 0
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.time() < end_ts:
            try:
                if body is not None:
                    await client.request(method, url, json=body)
                else:
                    await client.request(method, url)
                n_sent += 1
            except Exception:
                pass
            await asyncio.sleep(interval)
    return n_sent


async def generate_load(load_specs: list[dict[str, Any]], duration_seconds: int) -> None:
    if not load_specs:
        print(f"  [load] (idle {duration_seconds}s — no load specs)")
        await asyncio.sleep(duration_seconds)
        return
    end_ts = time.time() + duration_seconds
    print(f"  [load] {len(load_specs)} burst(s) × {duration_seconds}s")
    counts = await asyncio.gather(*(_one_burst(s, end_ts) for s in load_specs))
    for spec, n in zip(load_specs, counts):
        print(f"    → {spec.get('method', 'POST').upper()} {spec['url']}: {n} requests sent")


async def poll_alerts(deadline: float) -> list[dict[str, Any]]:
    """Poll AlertManager until any alert is active, or deadline is reached.
    Returns the alerts list observed at end (may be empty)."""
    last_snapshot: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.time() < deadline:
            try:
                resp = await client.get(f"{ALERTMANAGER_URL}/api/v2/alerts")
                alerts = [a for a in resp.json() if a["status"]["state"] == "active"]
                last_snapshot = alerts
                # We keep polling until deadline so the report reflects the
                # steady-state after load has settled — not the first alert.
            except Exception:
                pass
            await asyncio.sleep(5)
    return last_snapshot


# ── Phase 4: report ─────────────────────────────────────────────────────────
def print_report(alerts: list[dict[str, Any]]) -> None:
    print(f"\nAlertManager: {len(alerts)} active alert(s)")
    for a in alerts:
        lbl = a["labels"]
        print(
            f"  {lbl.get('alertname', '?'):32s}"
            f" service={lbl.get('service', '-'):18s}"
            f" target={lbl.get('target_service', '-'):16s}"
            f" severity={lbl.get('severity', '-')}"
        )

    try:
        resp = httpx.get(f"{INCIDENT_TRACKER_URL}/incidents", timeout=5.0)
        incidents = resp.json()
    except Exception as exc:
        print(f"\n(incident-tracker unreachable: {exc})")
        return
    print(f"\nincident-tracker: {incidents['count']} incident(s) tracked")
    for inc in incidents["incidents"][:10]:
        print(
            f"  {inc['id'][:12]}  alert={inc.get('alert_name'):32s}"
            f" service={inc.get('service', '-'):18s}"
            f" state={inc.get('state')}"
        )


# ── Orchestration ───────────────────────────────────────────────────────────
def load_case(case_id: str) -> dict[str, Any]:
    case_dir = CASE_ROOT / case_id
    setup_path = case_dir / "setup.yaml"
    if not setup_path.exists():
        raise SystemExit(f"case not found: {setup_path}")
    with open(setup_path) as f:
        setup = yaml.safe_load(f)
    return {"id": case_id, "dir": case_dir, "setup": setup or {}}


async def run_case(case: dict[str, Any]) -> None:
    setup = case["setup"]
    print(f"=== Running case: {case['id']} ===")
    if desc := setup.get("description"):
        print(desc.strip())
        print()

    duration = int(setup.get("duration_seconds", 120))
    alert_deadline = time.time() + duration + 30  # +30s slack for `for: 1m`

    print("--- Phase 1: reset state ---")
    clear_state()
    time.sleep(2)

    print("\n--- Phase 2: apply setup ---")
    apply_infra_commands(setup.get("infra_commands", []))
    apply_deploy_records(setup.get("deploy_records", []))
    apply_app_faults(setup.get("app_faults", []))

    print("\n--- Phase 3: load + poll ---")
    load_task = asyncio.create_task(generate_load(setup.get("load", []), duration))
    alert_task = asyncio.create_task(poll_alerts(alert_deadline))
    await load_task
    alerts = await alert_task

    print("\n--- Phase 4: report ---")
    print_report(alerts)

    print("\n--- Phase 5: cleanup ---")
    clear_state()
    print("Done.")


def list_cases() -> None:
    for case_dir in sorted(CASE_ROOT.iterdir()):
        if case_dir.is_dir() and (case_dir / "setup.yaml").exists():
            title = ""
            try:
                with open(case_dir / "setup.yaml") as f:
                    setup = yaml.safe_load(f) or {}
                desc = (setup.get("description") or "").strip().splitlines()
                if desc:
                    title = f" — {desc[0]}"
            except Exception:
                pass
            print(f"{case_dir.name}{title}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Golden case runner")
    parser.add_argument("case_id", nargs="?", help="case directory name under eval/golden/")
    parser.add_argument("--list", action="store_true", help="list available cases")
    args = parser.parse_args()

    if args.list:
        list_cases()
        return
    if not args.case_id:
        parser.error("case_id required (or --list)")

    case = load_case(args.case_id)
    try:
        asyncio.run(run_case(case))
    except KeyboardInterrupt:
        print("\n(interrupted, running cleanup...)")
        clear_state()
        sys.exit(130)


if __name__ == "__main__":
    main()
