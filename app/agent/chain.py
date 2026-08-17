"""Chain READS for the grid trader. No writes, no signing, no LLM.

Every on-chain write lives in ``grid_signing.py``; the pure arithmetic lives in
``grid.py``. This module is the only place that talks to an RPC for reads.

Addresses come from the shared ``config/bsc-contracts.json`` address book and are
never hardcoded here. Every address in that file was verified by CALLING it on
the live chain — its own ``_readme`` documents two traps that cost the sibling
project real time: PancakeSwap's docs list a Factory/Router V3 with NO CODE on
mainnet, and ``quoter_v2`` is effectively swapped between the networks (each
address answers on exactly one chain and reverts with a bare
``execution reverted: 0x`` on the other).
"""
from __future__ import annotations

import functools
import json
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from bnbagent_studio_core.networks import get_network
from web3 import Web3

CONTRACTS_FILENAME = "config/bsc-contracts.json"
RPC_RETRIES = 3
RPC_RETRY_SLEEP = 0.4

AGENT_STUDIO_TOML = Path(__file__).resolve().parent / "studio.toml"


# --- Address book ---------------------------------------------------------------
def _contracts_path() -> Path:
    """$BNB_CONTRACTS_CONFIG, else the nearest ``config/`` walking up from here.

    Walking up means this works from the agent dir, the workspace root, or a
    deployed bundle that ships ``config/`` alongside ``app/``.
    """
    override = os.environ.get("BNB_CONTRACTS_CONFIG")
    if override:
        return Path(override)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / CONTRACTS_FILENAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"shared address book {CONTRACTS_FILENAME} not found above {__file__}; "
        "set $BNB_CONTRACTS_CONFIG or restore the file"
    )


@lru_cache(maxsize=1)
def _contracts() -> dict[str, Any]:
    return json.loads(_contracts_path().read_text())


@lru_cache(maxsize=8)
def _addresses(network: str, fee: int, pair: str = "BNB/USDT") -> dict[str, Any]:
    """Flat address lookup for ``network`` at the traded fee tier.

    The pool is selected BY FEE TIER, not fixed: mainnet carries both a fee-500
    and a fee-100 BNB/USDT pool, and the fee-100 one is ~2x deeper but earns a
    fifth the rate. Which one this agent trades is a strategy decision
    (``[strategy].fee``), not an address-book fact.
    """
    try:
        net = _contracts()["networks"][network]
    except KeyError:
        raise ValueError(
            f"network {network!r} is not in {CONTRACTS_FILENAME}; "
            f"known: {sorted(_contracts()['networks'])}"
        ) from None

    dex = net["pancakeswap_v3"]
    pools = dex["pools"].get(pair) or []
    match = next((p for p in pools if int(p["fee"]) == int(fee)), None)
    if match is None:
        raise ValueError(
            f"no {pair} fee-{fee} pool listed for {network}; "
            f"available fees: {sorted(int(p['fee']) for p in pools)}"
        )
    return {
        "chain_id": int(net["chain_id"]),
        "quoter_v2": dex["quoter_v2"],
        "swap_router": dex["swap_router"],
        # Present on mainnet only — see the address book's _readme. None means
        # "fall back to the V3 SwapRouter", which is a different ABI, not just a
        # different address.
        "smart_router": dex.get("smart_router"),
        "wbnb": net["tokens"]["wbnb"],
        "usdt": net["tokens"]["usdt"],
        "pool": match["address"],
        "fee": int(match["fee"]),
    }


def supported_networks() -> list[str]:
    return sorted(_contracts()["networks"])


# --- Config ---------------------------------------------------------------------
def _studio_toml() -> dict[str, Any]:
    from bnbagent_studio_core import config as _config

    return _config.load_studio_toml(AGENT_STUDIO_TOML) or {}


def default_network() -> str:
    """The active network: ``$BNB_NETWORK`` wins, else ``[network].default``.

    ONE resolver for this fact, used by the strategy AND by the seller runtime.
    The scaffold's own version ignored ``$BNB_NETWORK``, so exporting mainnet
    moved the strategy while leaving the seller polling testnet jobs — a second
    resolver for one fact always drifts from the first.
    """
    env = (os.environ.get("BNB_NETWORK") or "").strip()
    if env:
        return env
    return str((_studio_toml().get("network") or {}).get("default") or "bsc-testnet")


def strategy_config(network: str | None = None) -> dict[str, Any]:
    """``[strategy]`` from studio.toml, with defaults and per-network resolution."""
    raw = dict(_studio_toml().get("strategy") or {})
    network = network or default_network()

    def _per_network(key: str, default: float) -> float:
        """Read a key that may be a scalar OR a per-network inline table.

        Trade sizing is NOT portable between chains: measured live, 1 USDT moves
        the testnet pool 22.8% but 5000 USDT moves mainnet 0.065% — a 100x+
        difference in what is safe. A single scalar means exporting
        ``$BNB_NETWORK`` silently carries one chain's sizes onto the other, so
        these keys accept ``{ bsc-mainnet = X, bsc-testnet = Y }``.
        """
        value = raw.get(key, default)
        if isinstance(value, dict):
            if network not in value:
                raise ValueError(
                    f"[strategy].{key} has no entry for {network!r} "
                    f"(has: {sorted(value)}) — refusing to guess a trade size"
                )
            return float(value[network])
        return float(value)

    # `grid_count` is the marketplace spec's name and counts INTERVALS, not
    # rungs: its example has grid_count 10 and lists 11 prices (600..800 by 20).
    # Off by one in that direction means every grid is one rung short.
    if "grid_count" in raw:
        levels = int(raw["grid_count"]) + 1
    else:
        levels = int(raw.get("levels", 9))

    cfg = {
        "pair": str(raw.get("pair", "BNB/USDT")),
        "fee": int(raw.get("fee", 500)),
        "levels": levels,
        "grid_count": levels - 1,
        "spacing": str(raw.get("spacing", "geometric")),
        "range_pct": float(raw.get("range_pct", 10.0)),
        # Absolute bounds (spec shape). When both are set they WIN over
        # range_pct: an operator who names 600/800 means those prices, not a
        # band around whatever the price happens to be at activation.
        "lower_price": float(raw.get("lower_price") or 0.0),
        "upper_price": float(raw.get("upper_price") or 0.0),
        # Loss ceiling per UTC day, in quote currency. 0 disables it.
        "max_daily_loss": _per_network("max_daily_loss", 0.0),
        "order_size_usdt": _per_network("order_size_usdt", 1.0),
        "max_capital_usdt": _per_network("max_capital_usdt", 10.0),
        "max_slippage_pct": _per_network("max_slippage_pct", 1.0),
        "max_price_impact_pct": _per_network("max_price_impact_pct", 2.0),
        "min_gas_reserve_bnb": _per_network("min_gas_reserve_bnb", 0.02),
        "poll_interval_seconds": int(raw.get("poll_interval_seconds", 60)),
    }
    # `investment` is the spec's name for total deployed capital.
    if "investment" in raw:
        cfg["max_capital_usdt"] = _per_network("investment", cfg["max_capital_usdt"])
    cfg["investment"] = cfg["max_capital_usdt"]
    cfg["network"] = network
    return cfg


def grid_bounds(price: float, network: str | None = None) -> tuple[float, float]:
    """The grid's (lower, upper) for a given spot price.

    Absolute ``lower_price``/``upper_price`` win when both are set; otherwise the
    band is +/-``range_pct`` around ``price``.
    """
    cfg = strategy_config(network)
    lo, hi = cfg["lower_price"], cfg["upper_price"]
    if lo > 0 and hi > 0:
        return lo, hi
    r = cfg["range_pct"] / 100.0
    return price * (1 - r), price * (1 + r)


def fee_bps(network: str | None = None) -> float:
    """The traded pool's fee in BASIS POINTS.

    PancakeSwap fee tiers are in hundredths of a bip, so 500 is 5 bps (0.05%),
    not 500 bps. One helper for the conversion because getting it wrong by 10x
    silently turns a profitable grid into a losing one — which is exactly what
    the first run of ``check_config_consistency`` reported.
    """
    return float(addresses(network)["fee"]) / 100.0


def check_config_consistency(network: str | None = None, *,
                             scope: str = "all") -> list[str]:
    """Cross-field config errors that per-field validation cannot see.

    These are the ones that surface only as a bad trade: an $U currency from the
    other chain, a fee tier with no pool, a grid too tight to cover its own
    costs. Returned rather than raised so a typo cannot take the A2A surface down.

    ``scope="trading"`` drops the checks that only affect SELLING. The $U
    currency is a payments fact: a mismatch makes quotes worthless but has no
    bearing on whether the grid can trade. Without this split, pointing the
    config at mainnet made ``strategy.activate`` refuse to run on testnet — a
    selling misconfiguration blocking an unrelated trading action.
    """
    problems: list[str] = []
    network = network or default_network()
    toml = _studio_toml()

    try:
        addrs = _addresses(network, int(strategy_config(network)["fee"]),
                           str(strategy_config(network)["pair"]))
    except (ValueError, FileNotFoundError) as e:
        return [f"address book: {e}"]

    # The $U currency is per-network and switching [network].default does NOT
    # update it — a stale value signs quotes denominated in a token that does not
    # exist on the chain being traded.
    currency = str(((toml.get("payments") or {}).get("erc8183") or {}).get("currency") or "")
    known = {
        "bsc-mainnet": "0xcE24439F2D9C6a2289F741120FE202248B666666",
        "bsc-testnet": "0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565",
    }
    expected = known.get(network)
    if scope != "trading" and expected and currency and currency.lower() != expected.lower():
        problems.append(
            f"[payments.erc8183].currency {currency} is not the $U token for "
            f"{network} ({expected}) — quotes would be denominated in a token "
            f"that does not exist on the chain being traded"
        )

    cfg = strategy_config(network)

    if cfg["spacing"] not in ("geometric", "arithmetic"):
        problems.append(
            f"[strategy].spacing is {cfg['spacing']!r} — must be 'geometric' or 'arithmetic'"
        )
    lo, hi = cfg["lower_price"], cfg["upper_price"]
    if (lo > 0) != (hi > 0):
        problems.append(
            "[strategy] lower_price and upper_price must be set together "
            f"(got lower={lo}, upper={hi}) — one alone is silently ignored"
        )
    elif lo > 0 and hi <= lo:
        problems.append(f"[strategy].upper_price ({hi}) must exceed lower_price ({lo})")

    import grid as _grid

    try:
        r = cfg["range_pct"] / 100.0
        probe = _grid.build_grid(100.0 * (1 - r), 100.0 * (1 + r), cfg["levels"],
                                 spacing=cfg["spacing"])
        # Only rungs at or BELOW centre are ever bought, so a full sweep costs
        # order_size x buy_rungs — not x levels. Counting all levels overstates
        # the requirement by ~2x and rejects a capital ceiling that is exactly
        # right (it flagged 18 USDT needed for a grid whose real sweep is 10).
        buy_rungs = _grid.level_for_price(probe, 100.0) + 1
        if cfg["order_size_usdt"] * buy_rungs > cfg["max_capital_usdt"]:
            problems.append(
                f"a full grid sweep needs "
                f"{cfg['order_size_usdt'] * buy_rungs:.4f} USDT ({buy_rungs} buy rungs "
                f"x {cfg['order_size_usdt']:.4f}) but [strategy].max_capital_usdt is "
                f"{cfg['max_capital_usdt']:.4f} — the lowest rungs would never fill"
            )
        problems.extend(
            f"grid: {p}" for p in _grid.validate_grid(
                probe, float(addrs["fee"]) / 100.0, cfg["max_slippage_pct"]
            )
        )
    except ValueError as e:
        problems.append(f"grid: {e}")

    return problems


# --- RPC ------------------------------------------------------------------------
def _retry_rpc(fn):
    """Retry a chain read a few times before giving up.

    Public BSC endpoints are load balanced, and a node that has not caught up
    answers a perfectly valid call with a spurious revert. That matters more than
    normal flakiness here: the caller is deciding whether to move real money, and
    a false read mid-sequence would trade on a price that was never true. A
    permanent failure still raises after the retries.
    """

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        last: Exception | None = None
        for attempt in range(RPC_RETRIES):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 — retry any RPC-layer failure
                last = e
                if attempt < RPC_RETRIES - 1:
                    time.sleep(RPC_RETRY_SLEEP * (attempt + 1))
        raise last  # type: ignore[misc]

    return wrapped


@lru_cache(maxsize=4)
def _w3(network: str) -> Web3:
    return Web3(Web3.HTTPProvider(get_network(network).rpc_url))


ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint8"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "uint256"}]},
]

POOL_ABI = [
    {"name": "slot0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [
        {"type": "uint160", "name": "sqrtPriceX96"}, {"type": "int24", "name": "tick"},
        {"type": "uint16", "name": "observationIndex"},
        {"type": "uint16", "name": "observationCardinality"},
        {"type": "uint16", "name": "observationCardinalityNext"},
        {"type": "uint32", "name": "feeProtocol"}, {"type": "bool", "name": "unlocked"}]},
    {"name": "liquidity", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint128"}]},
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "address"}]},
]

QUOTER_ABI = [
    {"name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "tuple", "name": "params", "components": [
         {"type": "address", "name": "tokenIn"}, {"type": "address", "name": "tokenOut"},
         {"type": "uint256", "name": "amountIn"}, {"type": "uint24", "name": "fee"},
         {"type": "uint160", "name": "sqrtPriceLimitX96"}]}],
     "outputs": [{"type": "uint256", "name": "amountOut"},
                 {"type": "uint160", "name": "sqrtPriceX96After"},
                 {"type": "uint32", "name": "initializedTicksCrossed"},
                 {"type": "uint256", "name": "gasEstimate"}]},
]


def addresses(network: str | None = None) -> dict[str, Any]:
    network = network or default_network()
    cfg = strategy_config(network)
    return _addresses(network, int(cfg["fee"]), str(cfg["pair"]))


@lru_cache(maxsize=16)
def decimals(network: str, token: str) -> int:
    c = _w3(network).eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
    return int(c.functions.decimals().call())


def _pool(network: str):
    return _w3(network).eth.contract(
        address=Web3.to_checksum_address(addresses(network)["pool"]), abi=POOL_ABI
    )


# --- Price ----------------------------------------------------------------------
# A tick prices token0 in token1: price_raw = 1.0001**tick in raw base units, so
# the human price of token0 in token1 is price_raw * 10**(dec0 - dec1). token0 is
# USDT on both chains (it sorts below WBNB on each), so that is WBNB-per-USDT and
# the BNB price is its reciprocal.
def price_from_tick(tick: int, dec0: int = 18, dec1: int = 18) -> float:
    """USDT per BNB at ``tick``."""
    bnb_per_usdt = (1.0001 ** tick) * (10 ** (dec0 - dec1))
    if bnb_per_usdt <= 0:
        raise ValueError(f"degenerate tick price at tick={tick}")
    return 1.0 / bnb_per_usdt


@_retry_rpc
def get_price(network: str | None = None) -> dict[str, Any]:
    """Live BNB price in USDT from the PancakeSwap V3 pool's ``slot0`` tick.

    On BSC testnet this pool is NOT arbitraged against real markets, so the value
    will not track the real BNB price. That is expected, not a bug.
    """
    network = network or default_network()
    a = addresses(network)
    slot0 = _pool(network).functions.slot0().call()
    tick = int(slot0[1])
    d0 = decimals(network, a["usdt"])
    d1 = decimals(network, a["wbnb"])
    return {
        "network": network,
        "pair": strategy_config(network)["pair"],
        "pool": a["pool"],
        "fee": a["fee"],
        "tick": tick,
        "price_usdt_per_bnb": price_from_tick(tick, d0, d1),
    }


@_retry_rpc
def get_balances(address: str | None = None, network: str | None = None) -> dict[str, Any]:
    """Native BNB, WBNB and USDT balances for ``address`` (default: this agent)."""
    network = network or default_network()
    a = addresses(network)
    w3 = _w3(network)
    if address is None:
        from bnbagent_studio_core.wallet import get_wallet

        address = get_wallet().address
    owner = Web3.to_checksum_address(address)

    def _erc20(token: str) -> int:
        c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        return int(c.functions.balanceOf(owner).call())

    native = int(w3.eth.get_balance(owner))
    wbnb, usdt = _erc20(a["wbnb"]), _erc20(a["usdt"])
    return {
        "network": network,
        "address": owner,
        "bnb": native / 1e18,
        "wbnb": wbnb / 10 ** decimals(network, a["wbnb"]),
        "usdt": usdt / 10 ** decimals(network, a["usdt"]),
        "bnb_wei": native,
        "wbnb_wei": wbnb,
        "usdt_wei": usdt,
    }


@_retry_rpc
def quote_swap(token_in: str, token_out: str, amount_in_wei: int,
               network: str | None = None) -> dict[str, Any]:
    """Simulated swap output via QuoterV2 — an ``eth_call``, never a transaction.

    Also reports ``price_impact_pct`` against the pool's current spot. On a
    shallow pool that number, not the slippage guard, is the real constraint:
    the guard only limits how far the fill may drift from the QUOTE, and the
    quote already includes the impact.
    """
    network = network or default_network()
    a = addresses(network)
    q = _w3(network).eth.contract(
        address=Web3.to_checksum_address(a["quoter_v2"]), abi=QUOTER_ABI
    )
    params = (
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
        int(amount_in_wei),
        int(a["fee"]),
        0,
    )
    out = q.functions.quoteExactInputSingle(params).call()
    amount_out = int(out[0])

    d_in = decimals(network, token_in)
    d_out = decimals(network, token_out)
    human_in = amount_in_wei / 10 ** d_in
    human_out = amount_out / 10 ** d_out

    spot = get_price(network)["price_usdt_per_bnb"]
    usdt = a["usdt"].lower()
    if token_in.lower() == usdt:      # USDT in, BNB out
        effective = human_in / human_out if human_out else 0.0
    else:                              # BNB in, USDT out
        effective = human_out / human_in if human_in else 0.0
    impact = abs(effective - spot) / spot * 100.0 if spot > 0 else float("inf")

    return {
        "network": network,
        "amount_in_wei": int(amount_in_wei),
        "amount_out_wei": amount_out,
        "amount_in": human_in,
        "amount_out": human_out,
        "effective_price_usdt_per_bnb": effective,
        "spot_price_usdt_per_bnb": spot,
        "price_impact_pct": impact,
    }
