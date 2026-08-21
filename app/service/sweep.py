"""Periodic funded-job sweep — the Service Layer's watcher.

    python sweep.py --once --dry-run    # scan and print, touch nothing
    python sweep.py --once              # scan and notify
    python sweep.py                     # loop forever

WHY THIS EXISTS. The agent is push-driven: a buyer calls ``notify_funded`` and
delivery starts immediately. A buyer who funds on-chain and never notifies is
served only if some OTHER notify happens to arrive and trigger the agent's
opportunistic sweep. With no traffic, that job sits FUNDED until its deadline
and the buyer reclaims — escrowed money, work never done, nothing logging an
error because nothing was watching. ``seller_core`` names this gap itself: "a
periodic Lambda poller ... is the v2 robust path".

WHY IN THE SERVICE LAYER. Delivery requires signing, and only the Agent Layer
holds the key. So this process never delivers: it SCANS (read-only, no wallet
loaded — the provider address comes from studio.toml, not from a keystore) and
then PUSHES ``notify_funded`` to the agent over A2A, which is the same entry
point a buyer uses. Watcher here, signer there, one implementation of delivery.

EXACTLY ONE PROCESS may run this, for the same reason as the grid monitor: two
sweepers would both notify, and while the agent dedupes in-flight jobs, that
dedupe is per-process and cannot span hosts. Opt in with ``SERVICE_SWEEP=1``.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import chain  # noqa: E402

log = logging.getLogger("grid-service.sweep")

# How far back to scan. Jobs are global across every agent on the chain, so the
# counter climbs regardless of our traffic; anything older than this window is
# past its deadline for us in practice and is the operator's problem, not the
# poller's. Kept bounded because get_jobs_batch multicalls the whole range.
SCAN_DEPTH = int(os.environ.get("SWEEP_SCAN_DEPTH") or 400)
# Re-notify the same job only this often. The agent dedupes in-flight work, but
# a restart clears that, so we must be willing to tell it again — just not every
# 30 seconds, which would spawn a background task per tick for a job that is
# failing for a permanent reason.
RENOTIFY_AFTER_SECONDS = int(os.environ.get("SWEEP_RENOTIFY_SECONDS") or 900)
# Don't bother the agent with a job that cannot be submitted in time.
MIN_SECONDS_LEFT = int(os.environ.get("SWEEP_MIN_SECONDS_LEFT") or 300)

AGENT_URL = os.environ.get("AGENT_A2A_URL") or "http://agent:9000/"

_last_notified: dict[int, float] = {}
# Jobs the agent refused for a PERMANENT reason (bad description, not ours,
# budget below price). notify_funded only answers "rejected" when the verdict is
# permanent, so re-notifying can never change the answer — it would just spawn
# work every interval forever. Job 56609 is exactly this case.
_rejected: dict[int, str] = {}


def _provider_address() -> str:
    """Our provider address from studio.toml — NOT from the keystore.

    Reading it here would mean unlocking the wallet in the keyless layer just to
    learn a public string.
    """
    from bnbagent_studio_core import config

    cfg = config.load_studio_toml()
    addr = (cfg.get("wallet") or {}).get("address") or (cfg.get("provider") or {}).get("address")
    if not addr:
        raise RuntimeError("no [wallet].address in studio.toml — cannot tell which jobs are ours")
    return str(addr).lower()


def _list_price_wei() -> int:
    from bnbagent_studio_core import config

    cfg = config.load_studio_toml()
    # The key is `price`, not `list_price` — same one signing.list_price() reads.
    p = str((((cfg.get("payments") or {}).get("erc8183")) or {}).get("price", "")).strip()
    return int(p) if p else 0


def _notify(job_id: int) -> dict:
    """Push notify_funded to the Agent Layer — the same door a buyer uses."""
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "role": "user", "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "data", "data": {"skill": "notify_funded", "job_id": job_id}}],
        }},
    }
    req = urllib.request.Request(
        AGENT_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    result = resp.get("result") or {}
    for part in (result.get("parts") or []):
        if part.get("kind") == "data":
            return part.get("data") or {}
    return resp


def sweep_once(*, dry_run: bool = False, network: str | None = None) -> dict:
    """One scan. Returns what was found and what was pushed."""
    from bnbagent.erc8183 import ERC8183Client, JobStatus

    network = network or chain.default_network()
    provider = _provider_address()
    min_budget = _list_price_wei()
    now = int(time.time())

    client = ERC8183Client(network=network)  # read-only: no wallet_provider
    counter = int(client.commerce.job_counter())
    lo = max(1, counter - SCAN_DEPTH + 1)
    jobs = client.commerce.get_jobs_batch(list(range(lo, counter + 1)))

    found, notified, skipped = [], [], []
    for job in jobs:
        if job is None or str(job.provider).lower() != provider:
            continue
        if job.status != JobStatus.FUNDED:
            continue
        found.append(job.id)
        left = int(job.expired_at) - now
        if left <= MIN_SECONDS_LEFT:
            skipped.append({"job": job.id, "why": f"only {left}s before expiry"})
            continue
        if min_budget and int(job.budget) < min_budget:
            skipped.append({"job": job.id, "why": f"budget {job.budget} below list price {min_budget}"})
            continue
        if job.id in _rejected:
            skipped.append({"job": job.id, "why": f"permanently rejected: {_rejected[job.id]}"})
            continue
        last = _last_notified.get(job.id, 0)
        if now - last < RENOTIFY_AFTER_SECONDS:
            skipped.append({"job": job.id, "why": f"notified {now - int(last)}s ago"})
            continue
        if dry_run:
            notified.append({"job": job.id, "dry_run": True})
            continue
        try:
            ack = _notify(job.id)
            _last_notified[job.id] = now
            if str(ack.get("status")) == "rejected":
                _rejected[job.id] = str(ack.get("reason") or "rejected")[:120]
            notified.append({"job": job.id, "ack": ack.get("status"), "reason": ack.get("reason")})
            log.info("swept job %s -> %s", job.id, ack.get("status"))
        except Exception as e:  # noqa: BLE001 — one bad job must not stop the sweep
            skipped.append({"job": job.id, "why": f"notify failed: {type(e).__name__}: {e}"})
            log.warning("notify for job %s failed: %s", job.id, e)

    return {"network": network, "provider": provider, "scanned": f"{lo}-{counter}",
            "funded_for_us": found, "notified": notified, "skipped": skipped}


def start(network: str | None = None) -> None:
    """Background loop. Interval from [payments.erc8183].poll_interval_seconds."""
    import threading

    from bnbagent_studio_core import config

    cfg = config.load_studio_toml()
    interval = int(((cfg.get("payments") or {}).get("erc8183") or {}).get(
        "poll_interval_seconds") or 30)

    def _loop():
        log.info("funded-job sweep started (every %ss, agent at %s)", interval, AGENT_URL)
        while True:
            try:
                r = sweep_once(network=network)
                if r["notified"]:
                    log.info("sweep: %s", json.dumps(r["notified"]))
            except Exception as e:  # noqa: BLE001 — a sweep failure must not kill the loop
                log.warning("sweep failed: %s: %s", type(e).__name__, e)
            time.sleep(interval)

    threading.Thread(target=_loop, name="funded-job-sweep", daemon=True).start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if "--once" in sys.argv:
        print(json.dumps(sweep_once(dry_run="--dry-run" in sys.argv), indent=2, default=str))
    else:
        start()
        while True:
            time.sleep(3600)
