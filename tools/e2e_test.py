"""End-to-end scenario suite for the BNB Grid Trader.

    python tools/e2e_test.py            # everything EXCEPT live trading
    python tools/e2e_test.py --live     # also trades real capital on mainnet

Phases 1-4 are free: guards, negative paths, A2A rejections, the REST surface,
and the on-chain state of the commerce jobs. Phase 5 spends money (~4 cents of
fees + gas for a full buy/sell round trip) and only runs under --live.

Every check prints PASS / FAIL with what it actually observed, because a suite
that only prints "ok" cannot be audited. Negative checks are first-class: a
guard that never refuses anything has not been tested.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app" / "agent"))
os.environ.setdefault("BNB_NETWORK", "bsc-mainnet")

NET = os.environ["BNB_NETWORK"]
AGENT_URL = os.environ.get("SELLER_URL", "https://bnb-grid.172-104-171-139.nip.io")
API_URL = os.environ.get("SERVICE_URL", "https://bnb-grid-api.172-104-171-139.nip.io")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return cond


def refuses(name, fn, *exc):
    """A guard passes only when it RAISES. Silence is a failure, not a pass."""
    try:
        fn()
    except exc as e:
        return check(name, True, f"refused: {str(e)[:80]}")
    except Exception as e:  # noqa: BLE001
        return check(name, False, f"wrong error type {type(e).__name__}: {str(e)[:60]}")
    return check(name, False, "did NOT refuse")


def http(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def a2a(payload):
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
            "params": {"message": {"role": "user", "messageId": str(uuid.uuid4()),
                                   "parts": [payload]}}}
    _, raw = http(AGENT_URL, "POST", body)
    resp = json.loads(raw)
    result = resp.get("result") or {}
    for p in (result.get("parts") or []):
        if p.get("kind") == "data":
            return p["data"]
    return resp


# ---------------------------------------------------------------- phase 1
def phase_guards():
    print("\n[1] Guards and refusals (no chain writes)")
    import chain
    import grid
    import grid_signing as gs

    cfg = chain.strategy_config(NET)
    a = chain.addresses(NET)

    refuses("address allowlist refuses an unknown address",
            lambda: gs._require_allowed(NET, "0x000000000000000000000000000000000000dEaD"),
            PermissionError)
    check("address allowlist ACCEPTS a book address",
          gs._require_allowed(NET, a["usdt"]).lower() == a["usdt"].lower())

    refuses("impact guard refuses a pool-moving trade",
            lambda: gs._require_impact(
                {"price_impact_pct": 25.0, "amount_in": 1.0, "amount_out": 0.0},
                cfg["max_price_impact_pct"]), RuntimeError)
    gs._require_impact({"price_impact_pct": 0.01, "amount_in": 1.0, "amount_out": 1.0},
                       cfg["max_price_impact_pct"])
    check("impact guard ALLOWS a small trade", True, "0.01% under the ceiling")

    refuses("gas-reserve guard refuses spending the reserve",
            lambda: gs._require_gas_reserve(NET, spend_wei=int(1e18)), RuntimeError)

    tight = grid.build_grid(600.0, 610.0, 9)
    probs = grid.validate_grid(tight, chain.fee_bps(NET), cfg["max_slippage_pct"])
    check("validate_grid rejects sub-fee spacing", bool(probs),
          probs[0][:70] if probs else "accepted a losing grid")
    ok = grid.build_grid(540.0, 660.0, 9)
    check("validate_grid accepts a viable grid",
          not grid.validate_grid(ok, chain.fee_bps(NET), cfg["max_slippage_pct"]))

    refuses("per-network sizing refuses an unconfigured network",
            lambda: chain.strategy_config("bsc-unknown"), Exception)


# ---------------------------------------------------------------- phase 2
def phase_emergency():
    print("\n[2] Emergency stop latching")
    import strategy

    before = strategy.get_status(NET)["status"]
    strategy.emergency_stop("e2e test", NET)
    check("emergency_stop sets stopped", strategy.load_state(NET).get("emergency_stopped") is True)
    refuses("activate REFUSES while stopped", lambda: strategy.activate(NET), Exception)
    strategy.resume(NET)
    check("resume clears the latch",
          not strategy.load_state(NET).get("emergency_stopped"))
    check("status restored", strategy.get_status(NET)["status"] == before,
          f"{before} -> {strategy.get_status(NET)['status']}")


# ---------------------------------------------------------------- phase 3
def phase_a2a():
    print("\n[3] A2A surface (seller must refuse malformed work)")
    r = a2a({"kind": "text", "text": "hello, please do some work"})
    check("plain text without a DataPart is rejected",
          "error" in json.dumps(r).lower(), json.dumps(r)[:90])

    r = a2a({"kind": "data", "data": {"skill": "definitely_not_a_skill"}})
    check("unknown skill is rejected", "error" in json.dumps(r).lower(),
          json.dumps(r)[:90])

    r = a2a({"kind": "data", "data": {"skill": "negotiate", "task_description": "x",
                                      "terms": {"deliverables": "only this"}}})
    accepted = ((r.get("response") or {}).get("accepted"))
    check("negotiate rejects terms without quality_standards", accepted is False,
          str((r.get("response") or {}).get("reason"))[:70])

    r = a2a({"kind": "data", "data": {
        "skill": "negotiate", "task_description": "Grid plan for BNB/USDT",
        "terms": {"deliverables": "grid plan JSON", "quality_standards": "levels + sizing"}}})
    resp = r.get("response") or {}
    check("negotiate ACCEPTS a well-formed request", resp.get("accepted") is True)
    check("quote is the fixed list price",
          (resp.get("terms") or {}).get("price") == "100000000000000000",
          str((resp.get("terms") or {}).get("price")))

    r = a2a({"kind": "data", "data": {"skill": "notify_funded", "job_id": 999999999}})
    check("notify_funded rejects a nonexistent job",
          str(r.get("status")) != "accepted", json.dumps(r)[:90])


# ---------------------------------------------------------------- phase 4
def phase_service():
    print("\n[4] Service Layer REST")
    for path in ("/health", "/marketplace", "/status", "/grid", "/orders",
                 "/performance", "/plan"):
        code, body = http(API_URL + path)
        check(f"GET {path} public", code == 200, f"{code} {body[:60]}")

    code, _ = http(API_URL + "/pause", "POST")
    check("write WITHOUT api key is refused", code in (401, 503), f"HTTP {code}")

    code, _ = http(API_URL + "/pause", "POST", {}, {"X-API-Key": "wrong-key"})
    check("write with WRONG api key is refused", code == 401, f"HTTP {code}")

    key = os.environ.get("SERVICE_API_KEY")
    if key:
        code, body = http(API_URL + "/pause", "POST", {}, {"X-API-Key": key})
        check("write with correct api key is accepted", code == 200, f"HTTP {code}")


# ---------------------------------------------------------------- phase 5
def phase_commerce():
    print("\n[5] Commerce jobs on-chain")
    from bnbagent.erc8183 import ERC8183Client
    c = ERC8183Client(network="bsc-mainnet")
    for jid, expect in ((56610, "SUBMITTED"), (56609, "FUNDED"), (56608, "SUBMITTED")):
        j = c.get_job(jid)
        check(f"job {jid} is {expect}", j.status.name == expect, j.status.name)
        if jid == 56610:
            check("job 56610 client != provider",
                  j.client.lower() != j.provider.lower(),
                  f"{j.client[:10]}… vs {j.provider[:10]}…")
    code, body = http(f"{AGENT_URL}/erc8183/job/56610/response")
    check("delivered job's deliverable is fetchable", code == 200, f"HTTP {code}")
    code, _ = http(f"{AGENT_URL}/erc8183/job/999999/response")
    check("undelivered job 404s (route alive, no leak)", code == 404, f"HTTP {code}")


# ---------------------------------------------------------------- phase 6 (live)
def phase_live():
    print("\n[6] LIVE grid round trip on mainnet (spends real capital)")
    import chain
    import strategy

    strategy.reset(NET)
    st = strategy.activate(NET)
    check("activate builds a grid", st["levels"] == 9 and st["in_range"],
          f"{st['lower']:.2f}–{st['upper']:.2f} step {st['step_pct']:.2f}%")

    d = strategy.step(network=NET)
    check("decide() chose BUY and it executed", d.get("action") == "buy" and d.get("ok"),
          f"level {d.get('level')} tx {str(d.get('tx'))[:12]}…")

    orders = strategy.get_open_orders(NET)
    sells = [o for o in orders if o["side"] == "sell"]
    buys = [o for o in orders if o["side"] == "buy"]
    check("orders show both sides after the buy", bool(sells) and bool(buys),
          f"{len(buys)} buys, {len(sells)} sells")

    # Re-shape so the held lot's sell target sits BELOW spot: decide() must now
    # choose sell on its own. Rungs stay ~2.3%, well above the validate floor —
    # this forces the branch without weakening any guard.
    price = chain.get_price(NET)["price_usdt_per_bnb"]
    strategy.update_grid(500.0, 600.0, 9, network=NET)
    d2 = strategy.check_once(NET) if hasattr(strategy, "check_once") else None
    d3 = strategy.step(network=NET)
    check("decide() chose SELL on its own and it executed",
          d3.get("action") == "sell" and d3.get("ok"),
          f"target {d3.get('target')} vs spot {price:.2f} tx {str(d3.get('tx'))[:12]}…")

    perf = strategy.get_performance(NET)
    check("a round trip is recorded", perf["round_trips"] >= 1,
          f"round_trips={perf['round_trips']} grid_profit={perf['grid_profit']:.6f} USDT")
    check("inventory is flat after the sell", abs(perf["base_open"]) < 1e-12,
          f"base_open={perf['base_open']}")
    strategy.reset(NET)


if __name__ == "__main__":
    live = "--live" in sys.argv
    t0 = time.time()
    phase_guards()
    phase_emergency()
    phase_a2a()
    phase_service()
    phase_commerce()
    if live:
        phase_live()
    else:
        print("\n[6] LIVE grid round trip — SKIPPED (pass --live to spend real capital)")

    print(f"\n{'='*58}\n{len(PASS)} passed, {len(FAIL)} failed  ({time.time()-t0:.0f}s)")
    for f in FAIL:
        print(f"  FAILED: {f}")
    sys.exit(1 if FAIL else 0)
