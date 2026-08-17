"""Grid math — PURE functions, no chain access, no I/O, no LLM.

Everything here is deterministic and testable offline (`python test_grid.py`).
The chain reads live in ``chain.py``; the writes live in ``grid_signing.py``.
Keeping the arithmetic here is not tidiness: the numbers ARE the product for
both halves of this agent (the autonomous trades and the sold plans), so they
must be reproducible without a network.

Model
-----
A grid is ``levels`` price points spanning ``[lower, upper]``. Spacing is
GEOMETRIC (constant *ratio* between neighbours), not arithmetic: a grid is a
percentage-return machine, and equal ratios make every rung earn the same
percentage. With arithmetic spacing the bottom rungs earn several times more per
fill than the top ones, which quietly makes low prices the only profitable
region.

Each level holds at most one lot. Below the center the agent buys; a lot bought
at level *i* is sold when price reaches level *i+1*. That one-step offset is
where the profit comes from — ``step_ratio - 1`` per round trip, minus fees and
slippage.
"""
from __future__ import annotations

from typing import Any

# A grid whose rungs are closer together than the round-trip cost loses money on
# every fill while looking busy. 2 * fee is the floor (buy leg + sell leg); the
# guard demands strictly more so a filled round trip cannot be exactly break-even.
MIN_EDGE_BPS = 5.0  # 0.05% of headroom over costs, on top of fees


def build_grid(lower: float, upper: float, levels: int,
               spacing: str = "geometric") -> list[float]:
    """``levels`` prices from ``lower`` to ``upper`` inclusive.

    ``spacing="geometric"`` (default) keeps a constant RATIO between rungs, so
    every rung earns the same percentage. ``spacing="arithmetic"`` keeps a
    constant DIFFERENCE, which is what the marketplace spec's example shows
    (600, 620, 640 … 800) — offered because the spec asks for it, but note the
    tradeoff: with arithmetic spacing the bottom rungs earn several times more
    per fill than the top ones, which quietly makes low prices the only
    profitable region.

    Raises ValueError on a degenerate grid rather than returning something
    plausible — a silently collapsed grid would trade against itself.
    """
    if levels < 2:
        raise ValueError(f"a grid needs at least 2 levels, got {levels}")
    if lower <= 0 or upper <= 0:
        raise ValueError(f"prices must be positive, got lower={lower} upper={upper}")
    if upper <= lower:
        raise ValueError(f"upper ({upper}) must exceed lower ({lower})")
    if spacing == "arithmetic":
        step = (upper - lower) / (levels - 1)
        return [lower + step * i for i in range(levels)]
    if spacing != "geometric":
        raise ValueError(f"spacing must be 'geometric' or 'arithmetic', got {spacing!r}")
    ratio = (upper / lower) ** (1.0 / (levels - 1))
    return [lower * (ratio ** i) for i in range(levels)]


def step_ratio(grid: list[float]) -> float:
    """The SMALLEST ratio between neighbouring levels.

    Constant for a geometric grid. For an arithmetic one the ratio shrinks as
    price rises, so the smallest (top) step is the one that decides whether the
    grid is profitable everywhere — reporting grid[1]/grid[0] there would quote
    the best rung and hide the losing ones.
    """
    if len(grid) < 2:
        raise ValueError("grid must have at least 2 levels")
    return min(grid[i + 1] / grid[i] for i in range(len(grid) - 1))


def gross_edge_pct(grid: list[float]) -> float:
    """Gross return per completed round trip, in percent, before costs."""
    return (step_ratio(grid) - 1.0) * 100.0


def net_edge_pct(grid: list[float], fee_bps: float, slippage_pct: float) -> float:
    """Round-trip return after pool fees (both legs) and worst-case slippage.

    Fees are charged on the buy AND the sell, so the fee tier counts twice. The
    same is true of slippage: each leg can fill at the edge of its guard.
    """
    costs_pct = 2.0 * (fee_bps / 100.0) + 2.0 * slippage_pct
    return gross_edge_pct(grid) - costs_pct


def validate_grid(grid: list[float], fee_bps: float, slippage_pct: float) -> list[str]:
    """Problems that make this grid unprofitable by construction. Empty == fine."""
    problems: list[str] = []
    net = net_edge_pct(grid, fee_bps, slippage_pct)
    if net <= 0:
        problems.append(
            f"every round trip loses {abs(net):.4f}% — level spacing "
            f"({gross_edge_pct(grid):.4f}%) is below the {2 * fee_bps / 100.0:.4f}% "
            f"fee cost plus {2 * slippage_pct:.4f}% slippage. Widen the range or "
            f"use fewer levels."
        )
    elif net * 100.0 < MIN_EDGE_BPS:
        problems.append(
            f"round-trip edge is only {net:.4f}% after costs — below the "
            f"{MIN_EDGE_BPS / 100.0:.4f}% minimum. A fill this thin is noise."
        )
    return problems


def level_for_price(grid: list[float], price: float) -> int:
    """Index of the highest level at or below ``price``; -1 when below the grid.

    ``len(grid) - 1`` means at or above the top rung.
    """
    idx = -1
    for i, level in enumerate(grid):
        if price >= level:
            idx = i
        else:
            break
    return idx


def decide(
    grid: list[float],
    price: float,
    filled: dict[int, float],
    *,
    center_index: int,
) -> dict[str, Any]:
    """The trade decision. Deterministic; the LLM never calls this to act.

    ``filled`` maps level index -> base-token amount held from that level's buy.
    Returns ``{"action": "buy"|"sell"|"hold", ...}``.

    Buy when price has fallen to an unfilled level below center. Sell a lot when
    price has risen to one level above where it was bought. Only ONE action per
    poll — a multi-level move fills the rest on subsequent polls, which keeps
    each decision independently verifiable against a single observed price.
    """
    if price <= 0:
        return {"action": "hold", "reason": f"invalid price {price}"}

    idx = level_for_price(grid, price)
    if idx < 0:
        return {"action": "hold", "reason": "price below grid floor — outside the range"}
    if idx >= len(grid) - 1 and price > grid[-1]:
        # At/above the ceiling: nothing left to buy, but held lots still sell below.
        pass

    # Sell first: realising profit frees capital and is never the wrong direction.
    # The deepest lot whose one-step target has been reached wins, so inventory
    # drains bottom-up and the remaining lots stay the cheapest ones held.
    sellable = sorted(
        (i for i, amt in filled.items() if amt > 0 and i + 1 < len(grid) and price >= grid[i + 1]),
    )
    if sellable:
        i = sellable[0]
        return {
            "action": "sell",
            "level": i,
            "amount_base": filled[i],
            "bought_at": grid[i],
            "target": grid[i + 1],
            "price": price,
            "reason": f"price {price:.6f} reached sell target {grid[i + 1]:.6f} for the lot bought at {grid[i]:.6f}",
        }

    # Buy: the level we just crossed down into, if it is below center and empty.
    if idx <= center_index and not filled.get(idx):
        return {
            "action": "buy",
            "level": idx,
            "level_price": grid[idx],
            "price": price,
            "reason": f"price {price:.6f} is at unfilled level {idx} ({grid[idx]:.6f})",
        }

    return {
        "action": "hold",
        "level": idx,
        "price": price,
        "reason": "no unfilled level below center and no lot at its sell target",
    }


def realised_pnl(trades: list[dict[str, Any]]) -> dict[str, float]:
    """Realised PnL in quote currency from a completed trade log.

    Only matched round trips count. Open lots are unrealised by definition and
    are reported separately by ``strategy.get_performance`` — folding an open
    lot's mark-to-market into "profit" is how a losing grid reads as a winning
    one right up until it is closed.
    """
    spent = sum(t["quote_amount"] for t in trades if t["side"] == "buy")
    earned = sum(t["quote_amount"] for t in trades if t["side"] == "sell")
    base_bought = sum(t["base_amount"] for t in trades if t["side"] == "buy")
    base_sold = sum(t["base_amount"] for t in trades if t["side"] == "sell")
    # Cost basis of what was actually sold, at the average buy price. Without
    # this the figure is just cash-in-minus-cash-out, which reports a fresh grid
    # that has only bought as a large loss.
    avg_cost = (spent / base_bought) if base_bought > 0 else 0.0
    cost_of_sold = avg_cost * base_sold
    pairs = round_trips(trades)
    wins = [r for r in pairs if r > 0]
    return {
        "grid_profit": sum(pairs),
        "win_rate": (len(wins) / len(pairs)) if pairs else 0.0,
        "completed_grids": len(pairs),
        "realised_quote": earned - cost_of_sold,
        "quote_spent": spent,
        "quote_earned": earned,
        "base_open": base_bought - base_sold,
        "avg_buy_price": avg_cost,
        "round_trips": min(
            sum(1 for t in trades if t["side"] == "buy"),
            sum(1 for t in trades if t["side"] == "sell"),
        ),
    }


def round_trips(trades: list[dict[str, Any]]) -> list[float]:
    """Profit of each COMPLETED round trip, in quote currency, in close order.

    Pairs each sell with the buy at the SAME grid level, which is what a round
    trip is here — a lot bought at level i and sold at level i+1. Average-cost
    accounting (``realised_pnl``) answers "what did the book make"; this answers
    "did each individual grid close green", which is what ``win_rate`` and the
    marketplace's ``grid_profit`` mean.

    Trades without a ``level`` (older logs, or hand-written test data) are
    skipped rather than guessed at.
    """
    open_lots: dict[int, float] = {}
    closed: list[float] = []
    for t in trades:
        level = t.get("level")
        if level is None:
            continue
        if t["side"] == "buy":
            open_lots[int(level)] = float(t["quote_amount"])
        elif t["side"] == "sell":
            cost = open_lots.pop(int(level), None)
            if cost is not None:
                closed.append(float(t["quote_amount"]) - cost)
    return closed


def plan_for(
    price: float,
    *,
    range_pct: float,
    levels: int,
    capital_quote: float,
    fee_bps: float,
    slippage_pct: float,
) -> dict[str, Any]:
    """Compute a grid plan around ``price`` — the deliverable buyers pay for.

    Pure arithmetic on a caller-supplied spot price, so the same inputs always
    produce the same plan and it can be re-derived by the buyer.
    """
    if capital_quote <= 0:
        raise ValueError(f"capital must be positive, got {capital_quote}")
    if not 0 < range_pct < 100:
        raise ValueError(f"range_pct must be in (0, 100), got {range_pct}")

    lower = price * (1.0 - range_pct / 100.0)
    upper = price * (1.0 + range_pct / 100.0)
    grid = build_grid(lower, upper, levels)
    center = level_for_price(grid, price)
    # Only rungs at/below center are ever bought, so capital splits across those.
    buy_levels = max(center + 1, 1)
    per_level = capital_quote / buy_levels
    net = net_edge_pct(grid, fee_bps, slippage_pct)

    return {
        "spot_price": price,
        "lower": lower,
        "upper": upper,
        "range_pct": range_pct,
        "levels": levels,
        "grid": grid,
        "center_index": center,
        "buy_levels": buy_levels,
        "capital_quote": capital_quote,
        "order_size_quote": per_level,
        "step_pct": gross_edge_pct(grid),
        "gross_edge_pct": gross_edge_pct(grid),
        "net_edge_pct": net,
        "profit_per_round_trip_quote": per_level * net / 100.0,
        "max_round_trips_if_full_sweep": buy_levels,
        "profit_full_sweep_quote": per_level * net / 100.0 * buy_levels,
        "fee_bps": fee_bps,
        "slippage_pct": slippage_pct,
        "warnings": validate_grid(grid, fee_bps, slippage_pct),
        "assumptions": [
            "Geometric spacing — every rung earns the same percentage.",
            "One lot per level; a lot bought at level i is sold at level i+1.",
            f"Costs modelled as {2 * fee_bps / 100.0:.4f}% pool fees (both legs) "
            f"plus {2 * slippage_pct:.4f}% worst-case slippage.",
            "Price impact is NOT modelled — on a shallow pool a real fill can be "
            "far worse than the slippage guard suggests. Size against pool depth.",
            "A price that leaves the range stops the grid: below the floor it "
            "holds inventory at a loss, above the ceiling it holds only quote.",
        ],
    }
