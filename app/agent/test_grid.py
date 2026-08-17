"""Offline checks for the grid math. No chain, no network: `python test_grid.py`.

Covers the properties that cost money when they break — spacing, the buy/sell
decision, cost-aware edge, and PnL basis — not every branch.
"""
from __future__ import annotations

import grid


def test_geometric_spacing():
    g = grid.build_grid(100.0, 200.0, 5)
    assert len(g) == 5
    assert abs(g[0] - 100.0) < 1e-9
    assert abs(g[-1] - 200.0) < 1e-9
    ratios = [g[i + 1] / g[i] for i in range(len(g) - 1)]
    # Constant ratio is the whole point: equal percentage per rung.
    assert max(ratios) - min(ratios) < 1e-12, ratios


def test_degenerate_grids_raise():
    for args in [(100.0, 200.0, 1), (0.0, 200.0, 5), (200.0, 100.0, 5), (-1.0, 5.0, 3)]:
        try:
            grid.build_grid(*args)
        except ValueError:
            continue
        raise AssertionError(f"build_grid{args} should have raised")


def test_level_for_price():
    g = grid.build_grid(100.0, 200.0, 5)
    assert grid.level_for_price(g, 99.0) == -1        # below the floor
    assert grid.level_for_price(g, 100.0) == 0        # exactly on a rung
    assert grid.level_for_price(g, 250.0) == 4        # above the ceiling
    assert grid.level_for_price(g, g[2] + 1e-9) == 2


def test_buys_unfilled_level_below_center():
    g = grid.build_grid(100.0, 200.0, 5)
    d = grid.decide(g, g[1], {}, center_index=2)
    assert d["action"] == "buy" and d["level"] == 1, d


def test_does_not_rebuy_a_filled_level():
    g = grid.build_grid(100.0, 200.0, 5)
    d = grid.decide(g, g[1], {1: 0.5}, center_index=2)
    assert d["action"] == "hold", d


def test_does_not_buy_above_center():
    g = grid.build_grid(100.0, 200.0, 5)
    d = grid.decide(g, g[3], {}, center_index=2)
    assert d["action"] == "hold", d


def test_sells_one_step_up():
    g = grid.build_grid(100.0, 200.0, 5)
    d = grid.decide(g, g[2], {1: 0.5}, center_index=2)
    assert d["action"] == "sell" and d["level"] == 1, d
    assert abs(d["target"] - g[2]) < 1e-9


def test_sell_takes_priority_and_drains_deepest_first():
    g = grid.build_grid(100.0, 200.0, 5)
    # Two lots held, both past target, and level 0 is also an unfilled buy.
    d = grid.decide(g, g[3], {1: 0.5, 2: 0.5}, center_index=3)
    assert d["action"] == "sell" and d["level"] == 1, d


def test_holds_below_the_floor():
    g = grid.build_grid(100.0, 200.0, 5)
    d = grid.decide(g, 50.0, {}, center_index=2)
    assert d["action"] == "hold" and "below grid floor" in d["reason"], d


def test_net_edge_subtracts_both_legs():
    g = grid.build_grid(100.0, 200.0, 5)
    gross = grid.gross_edge_pct(g)
    net = grid.net_edge_pct(g, fee_bps=5, slippage_pct=1.0)
    # 0.05% fee twice + 1% slippage twice = 2.1%
    assert abs((gross - net) - 2.1) < 1e-9, (gross, net)


def test_tight_grid_is_flagged_not_silently_accepted():
    # 20 levels over a 2% span: rungs far closer together than the round trip costs.
    g = grid.build_grid(99.0, 101.0, 20)
    problems = grid.validate_grid(g, fee_bps=5, slippage_pct=1.0)
    assert problems and "loses" in problems[0], problems


def test_pnl_uses_cost_basis_not_cash_flow():
    trades = [
        {"side": "buy", "quote_amount": 100.0, "base_amount": 1.0},
        {"side": "buy", "quote_amount": 100.0, "base_amount": 2.0},
    ]
    r = grid.realised_pnl(trades)
    # Bought only: cash is -200 but nothing is realised yet.
    assert r["realised_quote"] == 0.0, r
    assert r["base_open"] == 3.0 and r["round_trips"] == 0

    trades.append({"side": "sell", "quote_amount": 80.0, "base_amount": 1.0})
    r = grid.realised_pnl(trades)
    # avg cost 200/3 = 66.67 for 1 base sold at 80 → +13.33
    assert abs(r["realised_quote"] - (80.0 - 200.0 / 3.0)) < 1e-9, r
    assert r["round_trips"] == 1


def test_plan_splits_capital_over_buy_levels_only():
    p = grid.plan_for(
        600.0, range_pct=10.0, levels=11, capital_quote=1100.0,
        fee_bps=5, slippage_pct=0.5,
    )
    assert p["lower"] == 540.0 and p["upper"] == 660.0
    assert p["buy_levels"] == p["center_index"] + 1
    assert abs(p["order_size_quote"] * p["buy_levels"] - 1100.0) < 1e-9
    assert p["net_edge_pct"] < p["gross_edge_pct"]
    assert p["warnings"] == [], p["warnings"]


def test_plan_rejects_nonsense_inputs():
    for kwargs in [
        {"capital_quote": 0.0, "range_pct": 10.0},
        {"capital_quote": 100.0, "range_pct": 0.0},
        {"capital_quote": 100.0, "range_pct": 150.0},
    ]:
        try:
            grid.plan_for(600.0, levels=5, fee_bps=5, slippage_pct=0.5, **kwargs)
        except ValueError:
            continue
        raise AssertionError(f"plan_for({kwargs}) should have raised")


def test_full_cycle_never_rebuys_or_oversells():
    """Walk a price path and assert inventory stays consistent."""
    g = grid.build_grid(100.0, 200.0, 6)
    filled: dict[int, float] = {}
    center = 3
    path = [g[3], g[2], g[1], g[0], g[1], g[2], g[3], g[2], g[1]]
    for price in path:
        for _ in range(len(g)):  # settle multi-level moves
            d = grid.decide(g, price, filled, center_index=center)
            if d["action"] == "buy":
                assert d["level"] not in filled or filled[d["level"]] == 0
                filled[d["level"]] = 1.0
            elif d["action"] == "sell":
                assert filled.get(d["level"], 0) > 0
                filled[d["level"]] = 0.0
            else:
                break
    assert all(v >= 0 for v in filled.values()), filled


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
