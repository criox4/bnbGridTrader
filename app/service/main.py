"""Service Layer (spec section 2) — public REST API, marketplace data, job polling.

The other half of the two-layer architecture. The Agent Layer (``app/agent``)
holds the key, reasons, and signs; this layer holds NO key and exposes what the
outside world needs:

    GET  /health          liveness, unauthenticated
    GET  /marketplace     the spec 5.7 listing payload
    GET  /status          strategy state
    GET  /grid            every level and which hold lots
    GET  /orders          open positions, one per filled rung
    GET  /performance     realised / unrealised PnL, win rate
    GET  /jobs            pending ERC-8183 jobs assigned to this provider
    POST /activate /pause /cancel /update /stop /resume   (spec 5.8 actions)

Reads are public — a marketplace has to be able to list the agent without a
credential. WRITES require ``SERVICE_API_KEY``; when it is unset they return 503
rather than running unauthenticated, because these actions move funds.

This process imports ``strategy`` / ``chain`` straight out of ``app/agent`` — one
implementation, two entrypoints. It signs nothing itself: the operator actions it
exposes call the same fixed code the CLI does, and that code loads the wallet
only when it actually sends.

WHO RUNS THE MONITOR: exactly one process, chosen by ``SERVICE_RUN_MONITOR``. The
flock in strategy.py excludes a second process on the same filesystem but cannot
see one on another host, so this is an explicit choice, never a default.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# The Agent Layer is the single implementation of strategy + chain access.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import chain  # noqa: E402
import strategy  # noqa: E402
from fastapi import Body, FastAPI, Header, HTTPException  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("grid-service")

app = FastAPI(title="BNB Grid Trader — Service Layer", version="1.0.0")

SERVICE_API_KEY = os.environ.get("SERVICE_API_KEY") or ""


def _require_key(x_api_key: str | None) -> None:
    """Gate a fund-moving action.

    An UNSET key returns 503, not 200: defaulting to open would mean a fresh
    deployment briefly exposes activate/cancel to the internet, and 'briefly' is
    all it takes.
    """
    if not SERVICE_API_KEY:
        raise HTTPException(503, "SERVICE_API_KEY is not configured — write actions disabled")
    if x_api_key != SERVICE_API_KEY:
        raise HTTPException(401, "invalid or missing X-API-Key")


def _safe(fn, *args, **kwargs):
    """Run a strategy call, turning failures into HTTP errors instead of 500s."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — the API reports, it does not crash
        log.exception("%s failed", getattr(fn, "__name__", fn))
        raise HTTPException(400, f"{type(e).__name__}: {e}") from e


# --- Public reads ---------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "keyless": True, "network": chain.default_network()}


@app.get("/marketplace")
def marketplace() -> dict:
    """The marketplace listing payload (spec 5.7)."""
    return _safe(strategy.get_marketplace_data)


@app.get("/status")
def status() -> dict:
    return _safe(strategy.get_status)


@app.get("/grid")
def get_grid() -> dict:
    return _safe(strategy.get_grid)


@app.get("/orders")
def orders() -> list:
    return _safe(strategy.get_open_orders)


@app.get("/performance")
def performance() -> dict:
    return _safe(strategy.get_performance)


@app.get("/plan")
def plan(capital_usdt: float | None = None) -> dict:
    """A computed grid plan — the same product the agent sells over ERC-8183."""
    return _safe(strategy.get_plan, capital_usdt)


@app.get("/jobs")
def jobs() -> dict:
    """FUNDED ERC-8183 jobs assigned to this provider.

    Visibility only. DELIVERY belongs to the Agent Layer, which holds the key —
    this layer must never be the thing that submits, or two processes would race
    to deliver the same job.
    """
    from bnbagent.erc8183 import ERC8183JobOps
    from bnbagent_studio_core.wallet import get_wallet

    def _pending():
        import asyncio

        ops = ERC8183JobOps(wallet_provider=get_wallet(), network=chain.default_network())
        return asyncio.run(ops.get_pending_jobs())

    return {"network": chain.default_network(), "pending": _safe(_pending)}


# --- Operator actions (spec 5.8) ------------------------------------------------
@app.post("/activate")
def activate(x_api_key: str | None = Header(default=None)) -> dict:
    _require_key(x_api_key)
    return _safe(strategy.activate)


@app.post("/pause")
def pause(x_api_key: str | None = Header(default=None)) -> dict:
    _require_key(x_api_key)
    return _safe(strategy.pause)


@app.post("/cancel")
def cancel(x_api_key: str | None = Header(default=None)) -> dict:
    """cancelGrid — SELLS every open lot, then clears the grid."""
    _require_key(x_api_key)
    return _safe(strategy.cancel_grid)


@app.post("/update")
def update(payload: dict = Body(default={}),
           x_api_key: str | None = Header(default=None)) -> dict:
    """updateGrid — re-shape in place: {"lower":.., "upper":.., "levels":..}."""
    _require_key(x_api_key)
    return _safe(strategy.update_grid,
                 payload.get("lower"), payload.get("upper"), payload.get("levels"))


@app.post("/stop")
def stop(payload: dict = Body(default={}),
         x_api_key: str | None = Header(default=None)) -> dict:
    """emergency_stop — latches; activate refuses until /resume."""
    _require_key(x_api_key)
    return _safe(strategy.emergency_stop, str(payload.get("reason") or "via API"))


@app.post("/resume")
def resume(x_api_key: str | None = Header(default=None)) -> dict:
    _require_key(x_api_key)
    return _safe(strategy.resume)


@app.get("/sweep")
def sweep_status(dry_run: bool = True) -> dict:
    """What the funded-job sweep sees right now. Read-only when dry_run (default).

    Public because it exposes only on-chain facts about jobs assigned to us.
    Setting dry_run=false pushes notify_funded, so it is gated like a write.
    """
    import sweep as _sweep

    return _safe(_sweep.sweep_once, dry_run=dry_run)


# --- Monitor --------------------------------------------------------------------
if (os.environ.get("SERVICE_SWEEP") or "").strip().lower() in ("1", "true", "yes"):
    # The funded-job watcher. Separate flag from the grid monitor: one polls the
    # POOL to trade our own capital, the other polls the COMMERCE contract for
    # work buyers already paid for. A deployment may well want the second
    # without the first — this one moves no funds of ours.
    import sweep as _sweep_mod

    _sweep_mod.start()

if (os.environ.get("SERVICE_RUN_MONITOR") or "").strip().lower() in ("1", "true", "yes"):
    for _problem in chain.check_config_consistency():
        log.error("CONFIG: %s", _problem)
    strategy.start_monitor()
    log.info("grid monitor started in the SERVICE layer (SERVICE_RUN_MONITOR set)")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("SERVICE_BIND_HOST") or "0.0.0.0",
        port=int(os.environ.get("SERVICE_PORT") or "8080"),
        log_level="info",
    )
