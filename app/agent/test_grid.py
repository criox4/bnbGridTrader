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


def test_arithmetic_spacing_matches_the_spec_example():
    # The marketplace spec shows 600..800 with grid_count 10 -> 11 levels of 20.
    g = grid.build_grid(600.0, 800.0, 11, spacing="arithmetic")
    assert g == [600.0 + 20 * i for i in range(11)], g
    diffs = [g[i + 1] - g[i] for i in range(len(g) - 1)]
    assert max(diffs) - min(diffs) < 1e-9


def test_arithmetic_step_ratio_reports_the_worst_rung():
    g = grid.build_grid(600.0, 800.0, 11, spacing="arithmetic")
    # Bottom rung gains 20/600 = 3.33%, top rung 20/780 = 2.56%. The honest
    # figure is the SMALLEST, or a validate_grid check passes on the best rung
    # while the top ones lose money.
    assert abs(grid.gross_edge_pct(g) - (800.0 / 780.0 - 1) * 100) < 1e-9


def test_bad_spacing_name_raises():
    try:
        grid.build_grid(100.0, 200.0, 5, spacing="linear")
    except ValueError:
        return
    raise AssertionError("unknown spacing should raise")


def test_round_trips_pair_by_level():
    trades = [
        {"side": "buy",  "level": 1, "quote_amount": 10.0, "base_amount": 1.0},
        {"side": "buy",  "level": 2, "quote_amount": 10.0, "base_amount": 1.0},
        {"side": "sell", "level": 1, "quote_amount": 11.0, "base_amount": 1.0},   # +1 win
        {"side": "sell", "level": 2, "quote_amount": 9.5,  "base_amount": 1.0},   # -0.5 loss
    ]
    rts = grid.round_trips(trades)
    assert len(rts) == 2 and abs(rts[0] - 1.0) < 1e-9 and abs(rts[1] + 0.5) < 1e-9, rts
    r = grid.realised_pnl(trades)
    assert r["completed_grids"] == 2
    assert abs(r["win_rate"] - 0.5) < 1e-9, r["win_rate"]
    assert abs(r["grid_profit"] - 0.5) < 1e-9, r["grid_profit"]


def test_round_trips_ignores_unclosed_and_unlabelled():
    trades = [
        {"side": "buy", "level": 1, "quote_amount": 10.0, "base_amount": 1.0},  # still open
        {"side": "buy", "quote_amount": 10.0, "base_amount": 1.0},              # no level
    ]
    assert grid.round_trips(trades) == []
    assert grid.realised_pnl(trades)["win_rate"] == 0.0


def test_pending_orders_matches_decide():
    """Every listed order must be one decide() would actually take."""
    g = grid.build_grid(80.0, 120.0, 9)
    filled = {2: 1.0, 5: 1.0}
    orders = grid.pending_orders(g, filled, center_index=4)

    # An armed grid is never empty: rungs 0,1,3,4 are unfilled and <= centre.
    buys = [o for o in orders if o["side"] == "buy"]
    assert [o["level"] for o in buys] == [0, 1, 3, 4], buys
    # Nothing above centre is a buy — decide() would refuse it.
    assert all(o["level"] <= 4 for o in buys)

    sells = [o for o in orders if o["side"] == "sell"]
    assert [o["level"] for o in sells] == [2, 5], sells
    assert all(abs(o["trigger"] - g[o["level"] + 1]) < 1e-12 for o in sells)

    # Each buy trigger really does make decide() buy that level — checked with no
    # lots held, because a rung's buy price is also the sell target of the rung
    # below it, and decide() sells first. Both orders are pending at that price;
    # which one fires on a given poll is decide()'s business, not this list's.
    for o in buys:
        d = grid.decide(g, o["trigger"], {}, center_index=4)
        assert d["action"] == "buy" and d["level"] == o["level"], (o, d)


def test_pending_orders_top_lot_has_no_sell_target():
    """A lot on the top rung cannot sell — listed with trigger None, not dropped."""
    g = grid.build_grid(80.0, 120.0, 5)
    orders = grid.pending_orders(g, {4: 2.0}, center_index=2)
    top = [o for o in orders if o["level"] == 4]
    assert len(top) == 1 and top[0]["side"] == "sell" and top[0]["trigger"] is None, top


def test_round_trips_need_the_buy_and_sell_on_the_SAME_level():
    """The pairing rule that update_grid must preserve.

    A lot re-indexed by updateGrid sells at its NEW level. If the buy stays
    logged at the old one the pair is unmatchable and grid_profit reads 0 while
    real money moved — and that figure is published to the marketplace.
    """
    mismatched = [
        {"side": "buy",  "level": 4, "quote_amount": 2.0, "base_amount": 0.00331906},
        {"side": "sell", "level": 7, "quote_amount": 1.9980005, "base_amount": 0.00331906},
    ]
    assert grid.round_trips(mismatched) == [], "levels differ -> no pair"
    assert grid.realised_pnl(mismatched)["grid_profit"] == 0

    matched = [dict(t) for t in mismatched]
    matched[1]["level"] = 4
    r = grid.realised_pnl(matched)
    assert abs(r["grid_profit"] + 0.0019995) < 1e-9, r["grid_profit"]
    assert r["completed_grids"] == 1
    # Cash accounting is level-independent and must agree either way.
    assert abs(grid.realised_pnl(mismatched)["realised_quote"] - r["realised_quote"]) < 1e-12


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
