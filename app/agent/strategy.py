"""Grid strategy — state, the autonomous loop, and the operator actions.

Layering: ``grid.py`` decides (pure math), ``chain.py`` reads, ``grid_signing.py``
writes, and this module sequences the three and owns the persistent state.

The loop only acts while state is ``active``, so importing this module never
starts moving funds. Activation is an explicit operator action:

    python strategy.py activate      # start trading (arms the loop)
    python strategy.py pause         # stop trading; inventory is left as-is
    python strategy.py status        # human-readable report
    python strategy.py seed 0.02     # swap 0.02 BNB into USDT to fund buys
    python strategy.py step          # run exactly one decision, then stop
    python strategy.py reset         # clear grid + trade log (keeps inventory)

``activate`` / ``pause`` / ``seed`` / ``step`` are NOT exposed as LLM tools — see
``tools.py``. They move funds or control the thing that moves funds, so they stay
operator-only and no prompt injection can reach them.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chain
import grid

log = logging.getLogger("seller-agent.strategy")

STATE_DIR = Path(os.environ.get("GRID_STATE_DIR") or Path(__file__).resolve().parent)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_path(network: str | None = None) -> Path:
    """State file, namespaced BY NETWORK.

    A grid is a set of price levels and the lots held at them; both are
    per-chain. Sharing one file across networks would apply testnet's levels to
    mainnet inventory the first time someone exported ``$BNB_NETWORK``.
    """
    return STATE_DIR / f".grid_state.{network or chain.default_network()}.json"


_EMPTY: dict[str, Any] = {
    "status": "paused",
    "grid": [],
    "center_index": 0,
    "center_price": 0.0,
    "filled": {},        # level index (as str, JSON) -> base amount in wei
    "trades": [],
    "activated_at": None,
    "updated_at": None,
    "last_error": None,
}


def load_state(network: str | None = None) -> dict[str, Any]:
    p = state_path(network)
    if not p.is_file():
        return dict(_EMPTY)
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.error("state file %s unreadable (%s); treating as empty", p, e)
        return dict(_EMPTY)
    merged = dict(_EMPTY)
    merged.update(data)
    return merged


def save_state(state: dict[str, Any], network: str | None = None) -> None:
    """Write state atomically — a torn state file loses the inventory ledger."""
    p = state_path(network)
    state["updated_at"] = _now()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(p)


def _update(network: str | None = None, **fields) -> dict[str, Any]:
    state = load_state(network)
    state.update(fields)
    save_state(state, network)
    return state


def _filled_map(state: dict[str, Any]) -> dict[int, float]:
    """JSON keys are strings; the decision logic indexes levels by int."""
    return {int(k): float(v) for k, v in (state.get("filled") or {}).items() if float(v) > 0}


@contextlib.contextmanager
def _trade_lock(network: str | None = None):
    """Exclusive lock so two processes never trade the same grid at once.

    flock excludes another process on the SAME filesystem but cannot see one on
    another host — see the monitor-start note in ``main.py``.
    """
    lock = state_path(network).with_suffix(".lock")
    lock.touch(exist_ok=True)
    with open(lock, "r+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                f"another process holds the grid lock ({lock}); refusing to trade"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# --- Operator actions -----------------------------------------------------------
def activate(network: str | None = None) -> dict[str, Any]:
    """Build the grid around the CURRENT price and arm the loop.

    The grid is anchored at activation time and does not follow price. A grid
    that re-centres on every poll is not a grid — it buys every dip at the new
    centre and never reaches a sell target.
    """
    network = network or chain.default_network()
    problems = chain.check_config_consistency(network)
    if problems:
        raise RuntimeError("refusing to activate with config problems: " + "; ".join(problems))

    cfg = chain.strategy_config(network)
    price = chain.get_price(network)["price_usdt_per_bnb"]
    r = cfg["range_pct"] / 100.0
    levels = grid.build_grid(price * (1 - r), price * (1 + r), cfg["levels"])
    warnings = grid.validate_grid(levels, chain.fee_bps(network), cfg["max_slippage_pct"])
    if warnings:
        raise RuntimeError("refusing to activate: " + "; ".join(warnings))

    state = load_state(network)
    # Re-activating keeps inventory and the trade log; only the levels move.
    state.update({
        "status": "active",
        "grid": levels,
        "center_index": grid.level_for_price(levels, price),
        "center_price": price,
        "activated_at": _now(),
        "last_error": None,
    })
    save_state(state, network)
    log.info("activated grid on %s around %.6f USDT/BNB", network, price)
    return get_status(network)


def pause(network: str | None = None) -> dict[str, Any]:
    """Stop trading. Inventory and the trade log are left untouched."""
    _update(network, status="paused")
    log.info("paused grid on %s", network or chain.default_network())
    return get_status(network)


def reset(network: str | None = None) -> dict[str, Any]:
    """Clear the grid, inventory ledger and trade log. Does NOT sell anything.

    The tokens stay in the wallet; only this agent's memory of them is dropped.
    """
    save_state(dict(_EMPTY), network)
    return get_status(network)


def seed(bnb_amount: float, network: str | None = None) -> dict[str, Any]:
    """Swap ``bnb_amount`` native BNB into USDT so the grid has quote currency.

    A grid buys the base asset with quote currency; a wallet holding only BNB
    has nothing to buy with and every rung below centre is unreachable. Wraps to
    WBNB first because the pool trades WBNB, not native.
    """
    import grid_signing as gs

    network = network or chain.default_network()
    a = chain.addresses(network)
    wei = int(bnb_amount * 1e18)
    if wei <= 0:
        raise ValueError(f"bnb_amount must be positive, got {bnb_amount}")

    with _trade_lock(network):
        bal = chain.get_balances(network=network)
        if bal["wbnb_wei"] < wei:
            gs.wrap_bnb(wei - bal["wbnb_wei"], network)
        result = gs.execute_swap(a["wbnb"], a["usdt"], wei, network)
    log.info("seeded %.6f BNB -> USDT (tx %s)", bnb_amount, result["tx_hash"])
    return result


def check(network: str | None = None) -> dict[str, Any]:
    """The decision for right now. Read-only — never trades."""
    network = network or chain.default_network()
    state = load_state(network)
    if not state["grid"]:
        return {"action": "hold", "reason": "no grid — run `python strategy.py activate`"}
    price = chain.get_price(network)["price_usdt_per_bnb"]
    return grid.decide(
        [float(x) for x in state["grid"]], price, _filled_map(state),
        center_index=int(state["center_index"]),
    )


def step(network: str | None = None, *, force: bool = False) -> dict[str, Any]:
    """Run exactly ONE decision and execute it if it is a trade.

    ``force`` bypasses the paused check (for a manual single step); it does NOT
    bypass the impact, slippage or gas-reserve guards, which are in the write
    path and apply to every send.
    """
    import grid_signing as gs

    network = network or chain.default_network()
    state = load_state(network)
    if state["status"] != "active" and not force:
        return {"action": "hold", "reason": f"strategy is {state['status']}"}
    if not state["grid"]:
        return {"action": "hold", "reason": "no grid — activate first"}

    cfg = chain.strategy_config(network)
    a = chain.addresses(network)
    decision = check(network)
    if decision["action"] == "hold":
        return decision

    with _trade_lock(network):
        # Re-read inside the lock: the decision above was made outside it, and a
        # concurrent step could have filled the same level in between.
        state = load_state(network)
        filled = _filled_map(state)
        price = chain.get_price(network)["price_usdt_per_bnb"]
        decision = grid.decide([float(x) for x in state["grid"]], price, filled,
                               center_index=int(state["center_index"]))
        if decision["action"] == "hold":
            return decision

        level = int(decision["level"])
        try:
            if decision["action"] == "buy":
                deployed = sum(
                    t["quote_amount"] for t in state["trades"] if t["side"] == "buy"
                ) - sum(t["quote_amount"] for t in state["trades"] if t["side"] == "sell")
                if deployed + cfg["order_size_usdt"] > cfg["max_capital_usdt"]:
                    return {"action": "hold", "level": level,
                            "reason": f"capital ceiling: {deployed:.6f} USDT deployed, "
                                      f"limit {cfg['max_capital_usdt']:.6f}"}
                amount_wei = int(cfg["order_size_usdt"] * 10 ** chain.decimals(network, a["usdt"]))
                res = gs.execute_swap(a["usdt"], a["wbnb"], amount_wei, network)
                base_out = res["quoted_out_wei"]
                state.setdefault("filled", {})[str(level)] = base_out
                state["trades"].append({
                    "ts": _now(), "side": "buy", "level": level,
                    "quote_amount": cfg["order_size_usdt"],
                    "base_amount": base_out / 1e18,
                    "price": decision["price"], "tx": res["tx_hash"],
                    "price_impact_pct": res["price_impact_pct"],
                })
            else:  # sell
                amount_wei = int(decision["amount_base"])
                res = gs.execute_swap(a["wbnb"], a["usdt"], amount_wei, network)
                quote_out = res["quoted_out_wei"] / 10 ** chain.decimals(network, a["usdt"])
                state.setdefault("filled", {})[str(level)] = 0
                state["trades"].append({
                    "ts": _now(), "side": "sell", "level": level,
                    "quote_amount": quote_out,
                    "base_amount": amount_wei / 1e18,
                    "price": decision["price"], "tx": res["tx_hash"],
                    "price_impact_pct": res["price_impact_pct"],
                })
        except Exception as e:  # noqa: BLE001 — record and stop, never half-update
            state["last_error"] = f"{_now()} {type(e).__name__}: {e}"
            save_state(state, network)
            log.error("trade failed: %s", e)
            return {"action": decision["action"], "level": level, "ok": False, "error": str(e)}

        state["last_error"] = None
        save_state(state, network)

    return {**decision, "ok": True, "tx": res["tx_hash"]}


# --- Reports (figures formatted by CODE, never by the model) --------------------
def get_status(network: str | None = None) -> dict[str, Any]:
    """Machine-readable strategy state + live price and balances."""
    network = network or chain.default_network()
    state = load_state(network)
    out: dict[str, Any] = {
        "network": network,
        "status": state["status"],
        "activated_at": state["activated_at"],
        "updated_at": state["updated_at"],
        "last_error": state["last_error"],
        "levels": len(state["grid"]),
        "center_price": state["center_price"],
        "open_lots": len(_filled_map(state)),
        "trade_count": len(state["trades"]),
    }
    if state["grid"]:
        levels = [float(x) for x in state["grid"]]
        out.update({"lower": levels[0], "upper": levels[-1],
                    "step_pct": grid.gross_edge_pct(levels)})
    try:
        price = chain.get_price(network)["price_usdt_per_bnb"]
        out["price_usdt_per_bnb"] = price
        if state["grid"]:
            levels = [float(x) for x in state["grid"]]
            out["in_range"] = levels[0] <= price <= levels[-1]
            out["current_level"] = grid.level_for_price(levels, price)
    except Exception as e:  # noqa: BLE001 — a status call must not fail on RPC
        out["price_usdt_per_bnb"] = f"unavailable ({type(e).__name__}) — do not estimate it"
    try:
        b = chain.get_balances(network=network)
        out["balances"] = {"bnb": b["bnb"], "wbnb": b["wbnb"], "usdt": b["usdt"]}
    except Exception as e:  # noqa: BLE001
        out["balances"] = f"unavailable ({type(e).__name__}) — do not estimate it"
    return out


def get_performance(network: str | None = None) -> dict[str, Any]:
    """Realised PnL and open inventory. Realised and unrealised stay SEPARATE.

    Folding an open lot's mark-to-market into "profit" is how a losing grid
    reads as a winning one right up until it is closed.
    """
    network = network or chain.default_network()
    state = load_state(network)
    pnl = grid.realised_pnl(state["trades"])
    out = dict(pnl)
    out["network"] = network
    out["gas_note"] = "realised PnL excludes gas; see per-trade tx hashes"
    try:
        price = chain.get_price(network)["price_usdt_per_bnb"]
        open_base = pnl["base_open"]
        out["open_base_value_usdt"] = open_base * price
        out["unrealised_quote"] = (
            (price - pnl["avg_buy_price"]) * open_base if pnl["avg_buy_price"] > 0 else 0.0
        )
    except Exception as e:  # noqa: BLE001
        out["unrealised_quote"] = f"unavailable ({type(e).__name__}) — do not estimate it"
    return out


def get_grid(network: str | None = None) -> dict[str, Any]:
    """The active grid: every level, which hold lots, and where price sits."""
    network = network or chain.default_network()
    state = load_state(network)
    if not state["grid"]:
        return {"network": network, "levels": [], "note": "no grid — not activated"}
    levels = [float(x) for x in state["grid"]]
    filled = _filled_map(state)
    try:
        price = chain.get_price(network)["price_usdt_per_bnb"]
        current = grid.level_for_price(levels, price)
    except Exception:  # noqa: BLE001
        price, current = None, None
    return {
        "network": network,
        "center_price": state["center_price"],
        "center_index": state["center_index"],
        "price_usdt_per_bnb": price,
        "current_level": current,
        "levels": [
            {"index": i, "price": lvl, "lot_base": filled.get(i, 0.0) / 1e18,
             "side": "buy" if i <= int(state["center_index"]) else "sell"}
            for i, lvl in enumerate(levels)
        ],
    }


def get_status_report(network: str | None = None) -> str:
    """Human-readable status. Every figure is formatted HERE, by code.

    Prefer this over the raw dicts when answering a paid question: for a status
    report the numbers ARE the product, and a model that reformats them will
    eventually reformat one wrongly.
    """
    s = get_status(network)
    p = get_performance(network)
    price = s.get("price_usdt_per_bnb")
    price_s = f"{price:.6f}" if isinstance(price, (int, float)) else str(price)

    lines = [
        f"Grid trader — {s['network']}",
        f"  status:        {s['status']}"
        + (f"  (since {s['activated_at']})" if s["activated_at"] else ""),
        f"  price:         {price_s} USDT/BNB",
    ]
    if s["levels"]:
        lines += [
            f"  grid:          {s['levels']} levels, "
            f"{s['lower']:.6f} – {s['upper']:.6f} USDT/BNB "
            f"({s['step_pct']:.4f}% per step)",
            f"  centre:        {s['center_price']:.6f} USDT/BNB",
            f"  in range:      {s.get('in_range')}",
            f"  open lots:     {s['open_lots']}",
        ]
    else:
        lines.append("  grid:          not activated")

    realised = p.get("realised_quote")
    lines += [
        f"  trades:        {s['trade_count']} ({p['round_trips']} completed round trips)",
        f"  realised PnL:  {realised:+.6f} USDT" if isinstance(realised, (int, float))
        else f"  realised PnL:  {realised}",
    ]
    unreal = p.get("unrealised_quote")
    lines.append(
        f"  unrealised:    {unreal:+.6f} USDT on {p['base_open']:.8f} BNB held"
        if isinstance(unreal, (int, float))
        else f"  unrealised:    {unreal}"
    )
    if isinstance(s.get("balances"), dict):
        b = s["balances"]
        lines.append(
            f"  balances:      {b['bnb']:.6f} BNB, {b['wbnb']:.6f} WBNB, {b['usdt']:.6f} USDT"
        )
    if s["last_error"]:
        lines.append(f"  last error:    {s['last_error']}")
    return "\n".join(lines)


def get_plan(capital_usdt: float | None = None, network: str | None = None) -> dict[str, Any]:
    """A grid plan around the CURRENT price — the product buyers pay for.

    Deterministic: same spot price and parameters always produce the same plan,
    so a buyer can re-derive it.
    """
    network = network or chain.default_network()
    cfg = chain.strategy_config(network)
    price = chain.get_price(network)["price_usdt_per_bnb"]
    plan = grid.plan_for(
        price,
        range_pct=cfg["range_pct"],
        levels=cfg["levels"],
        capital_quote=capital_usdt if capital_usdt else cfg["max_capital_usdt"],
        fee_bps=chain.fee_bps(network),
        slippage_pct=cfg["max_slippage_pct"],
    )
    plan["network"] = network
    plan["pair"] = cfg["pair"]
    plan["pool_fee_tier"] = cfg["fee"]
    return plan


# --- Monitor loop ---------------------------------------------------------------
_monitor: threading.Thread | None = None
_stop = threading.Event()


def _loop() -> None:
    network = chain.default_network()
    interval = chain.strategy_config(network)["poll_interval_seconds"]
    log.info("grid monitor started on %s (every %ss)", network, interval)
    while not _stop.is_set():
        try:
            if load_state(network)["status"] == "active":
                result = step(network)
                if result.get("action") != "hold":
                    log.info("monitor: %s", result)
        except Exception:  # noqa: BLE001 — the loop must never die on one bad poll
            log.exception("monitor poll failed")
        _stop.wait(interval)
    log.info("grid monitor stopped")


def start_monitor() -> None:
    global _monitor
    if _monitor and _monitor.is_alive():
        return
    _stop.clear()
    _monitor = threading.Thread(target=_loop, name="grid-monitor", daemon=True)
    _monitor.start()


def stop_monitor() -> None:
    _stop.set()


def is_monitor_running() -> bool:
    return bool(_monitor and _monitor.is_alive())


# --- CLI ------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if action == "activate":
            print(json.dumps(activate(), indent=2, default=str))
        elif action == "pause":
            print(json.dumps(pause(), indent=2, default=str))
        elif action == "reset":
            print(json.dumps(reset(), indent=2, default=str))
        elif action == "seed":
            amount = float(sys.argv[2]) if len(sys.argv) > 2 else 0.02
            print(json.dumps(seed(amount), indent=2, default=str))
        elif action == "step":
            print(json.dumps(step(force="--force" in sys.argv), indent=2, default=str))
        elif action == "check":
            print(json.dumps(check(), indent=2, default=str))
        elif action == "grid":
            print(json.dumps(get_grid(), indent=2, default=str))
        elif action == "plan":
            print(json.dumps(get_plan(), indent=2, default=str))
        elif action == "performance":
            print(json.dumps(get_performance(), indent=2, default=str))
        elif action == "status":
            print(get_status_report())
        else:
            print(f"unknown action {action!r}\n\n{__doc__}")
            sys.exit(2)
    except Exception as e:  # noqa: BLE001 — CLI reports, never tracebacks at the user
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
