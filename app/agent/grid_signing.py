"""Grid WRITE path — fixed code, NEVER an LLM tool.

Companion to ``signing.py``: that file signs the ERC-8183 money ops, this one
signs the trades (wrap / approve / swap). Both are fixed entrypoint code and
neither appears in ``tools.py``, so the LLM can never call them. The LLM decides
nothing here: ``grid.decide`` (pure arithmetic on an observed price) chooses the
action, and this module turns that choice into calldata.

## Where the guards live

The SDK's ``SigningPolicy`` gates ``sign_typed_data`` (EIP-712) only —
``sign_transaction`` is NOT policy-checked, and every call here is a plain
transaction. So the policy will not stop a bad one and these guards are the real
boundary:

* ``_require_allowed`` — no transaction is built to any address outside the
  verified shared address book.
* ``_require_gas_price`` — bounded gas price; gas limits bounded too.
* ``_min_out`` — every swap carries an output floor derived from a live quote.
* ``_require_impact`` — the quote itself is checked against spot, because the
  output floor bounds movement between quote and fill, NOT the price impact of
  the trade size. On the shallow testnet pool impact is the binding constraint:
  measured live, 0.05 USDT moves it 0.47% but 1.0 USDT moves it 22.8%.
* Approvals are EXACT-amount, never unlimited — an unlimited approve to a router
  is the classic way an agent wallet gets drained later — and swaps carry a
  deadline.

None of these read LLM output.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from bnbagent_studio_core.wallet import get_wallet
from web3 import Web3

import chain

log = logging.getLogger("seller-agent.grid")

DEADLINE_SECONDS = 600
MAX_GAS_PRICE_GWEI = 10.0
MAX_GAS_LIMIT = 1_000_000
GAS_ESTIMATE_BUFFER_PCT = 25.0

WBNB_ABI = [
    {"name": "deposit", "type": "function", "stateMutability": "payable",
     "inputs": [], "outputs": []},
    {"name": "withdraw", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256"}], "outputs": []},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "uint256"}]},
]
ERC20_WRITE_ABI = WBNB_ABI  # approve/allowance/balanceOf are the shared subset

ROUTER_ABI = [
    {"name": "exactInputSingle", "type": "function", "stateMutability": "payable",
     "inputs": [{"type": "tuple", "name": "params", "components": [
         {"type": "address", "name": "tokenIn"}, {"type": "address", "name": "tokenOut"},
         {"type": "uint24", "name": "fee"}, {"type": "address", "name": "recipient"},
         {"type": "uint256", "name": "deadline"}, {"type": "uint256", "name": "amountIn"},
         {"type": "uint256", "name": "amountOutMinimum"},
         {"type": "uint160", "name": "sqrtPriceLimitX96"}]}],
     "outputs": [{"type": "uint256", "name": "amountOut"}]},
]


# --- Guards ---------------------------------------------------------------------
def allowed_addresses(network: str) -> set[str]:
    """Every address this agent may transact with, from the shared address book."""
    a = chain.addresses(network)
    return {str(a[k]).lower() for k in ("quoter_v2", "swap_router", "wbnb", "usdt", "pool")}


def _require_allowed(network: str, address: str) -> str:
    """Refuse any address outside the verified address book; return it checksummed."""
    if str(address).lower() not in allowed_addresses(network):
        raise PermissionError(
            f"refusing to transact with {address} on {network}: not in the "
            f"verified address book ({chain.CONTRACTS_FILENAME})"
        )
    return Web3.to_checksum_address(address)


def _require_gas_price(w3) -> int:
    gas_price = int(w3.eth.gas_price)
    if gas_price > Web3.to_wei(MAX_GAS_PRICE_GWEI, "gwei"):
        raise RuntimeError(
            f"gas price {Web3.from_wei(gas_price, 'gwei')} gwei exceeds the "
            f"{MAX_GAS_PRICE_GWEI} gwei ceiling — refusing to send"
        )
    return gas_price


def _min_out(quoted: int, slippage_pct: float) -> int:
    """Output floor from a live quote. Bounds quote→fill drift, not trade impact."""
    if quoted <= 0:
        raise RuntimeError("quoter returned zero output — no liquidity for this size")
    return int(quoted * (1 - slippage_pct / 100.0))


def _require_impact(quote: dict[str, Any], max_impact_pct: float) -> None:
    """Refuse a trade whose own size moves the pool more than ``max_impact_pct``.

    This is the guard that matters on a thin pool. The output floor cannot catch
    it: the quote already includes the impact, so a 22% impact trade fills
    'within slippage' and still loses 22%.
    """
    impact = float(quote["price_impact_pct"])
    if impact > max_impact_pct:
        raise RuntimeError(
            f"refusing swap: price impact {impact:.3f}% exceeds the "
            f"{max_impact_pct:.3f}% ceiling (in {quote['amount_in']:.8f} → out "
            f"{quote['amount_out']:.8f}). Reduce [strategy].order_size_usdt."
        )


def _require_gas_reserve(network: str, spend_wei: int = 0) -> None:
    """Refuse to spend the wallet below the configured native-gas reserve.

    A wallet with inventory but no gas cannot sell — it is stuck holding the
    asset through whatever move follows.
    """
    cfg = chain.strategy_config(network)
    reserve_wei = int(cfg["min_gas_reserve_bnb"] * 1e18)
    native = chain.get_balances(network=network)["bnb_wei"]
    if native - spend_wei < reserve_wei:
        raise RuntimeError(
            f"refusing to send: native balance {native / 1e18:.6f} BNB minus "
            f"{spend_wei / 1e18:.6f} would fall below the "
            f"{cfg['min_gas_reserve_bnb']:.6f} BNB gas reserve"
        )


def _deadline() -> int:
    return int(time.time()) + DEADLINE_SECONDS


# --- Transaction plumbing -------------------------------------------------------
def _gas_limit(fn, sender: str, value: int, fallback: int) -> int:
    """Node estimate + headroom, else ``fallback``. An absurd estimate raises."""
    try:
        estimate = fn.estimate_gas({"from": sender, "value": value})
    except Exception as e:  # noqa: BLE001 — estimation is advisory, not a gate
        log.warning("gas estimation failed (%s); using fixed limit %s", e, fallback)
        return fallback
    limit = int(estimate * (1 + GAS_ESTIMATE_BUFFER_PCT / 100.0))
    if limit > MAX_GAS_LIMIT:
        raise RuntimeError(
            f"estimated gas {estimate} (+{GAS_ESTIMATE_BUFFER_PCT}% = {limit}) "
            f"exceeds the {MAX_GAS_LIMIT} ceiling — refusing to send"
        )
    return limit


def _send(network: str, fn, *, value: int = 0, gas: int = 400_000) -> dict[str, Any]:
    """Build → simulate → sign → broadcast → wait. Raises on revert."""
    w3 = chain._w3(network)
    wallet = get_wallet()
    sender = Web3.to_checksum_address(wallet.address)

    gas_price = _require_gas_price(w3)
    gas = _gas_limit(fn, sender, value, fallback=gas)
    _require_gas_reserve(network, spend_wei=value + gas * gas_price)

    tx = fn.build_transaction({
        "from": sender,
        # 'pending', not the default 'latest'. approve-then-swap runs back to
        # back, and a node that has not surfaced the approve in 'latest' hands
        # back a stale nonce — the swap then dies with "nonce too low" after the
        # approve has already landed.
        "nonce": w3.eth.get_transaction_count(sender, "pending"),
        "gas": gas,
        "gasPrice": gas_price,
        "value": value,
        "chainId": w3.eth.chain_id,
    })

    # Simulate first: a revert here costs nothing, a revert on-chain costs gas.
    try:
        w3.eth.call({k: v for k, v in tx.items() if k != "nonce"})
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"transaction would revert, not sending: {e}") from e

    signed = wallet.sign_transaction(tx)
    raw = signed["rawTransaction"] if isinstance(signed, dict) else signed.raw_transaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    log.info("sent %s -> %s", getattr(fn, "fn_name", "?"), tx_hash.hex())
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt["status"] != 1:
        raise RuntimeError(f"transaction reverted on-chain: {tx_hash.hex()}")
    return {
        "tx_hash": tx_hash.hex(),
        "gas_used": receipt["gasUsed"],
        "gas_cost_wei": receipt["gasUsed"] * gas_price,
        "block": receipt["blockNumber"],
    }


def _contract(network: str, address: str, abi: list):
    return chain._w3(network).eth.contract(
        address=_require_allowed(network, address), abi=abi
    )


# --- Operations -----------------------------------------------------------------
def wrap_bnb(amount_wei: int, network: str | None = None) -> dict[str, Any]:
    """Wrap native BNB into WBNB (the pool trades WBNB, not native)."""
    network = network or chain.default_network()
    if amount_wei <= 0:
        raise ValueError("amount_wei must be positive")
    wbnb = _contract(network, chain.addresses(network)["wbnb"], WBNB_ABI)
    return _send(network, wbnb.functions.deposit(), value=int(amount_wei), gas=120_000)


def unwrap_bnb(amount_wei: int, network: str | None = None) -> dict[str, Any]:
    """Unwrap WBNB back to native BNB (to restore the gas reserve)."""
    network = network or chain.default_network()
    if amount_wei <= 0:
        raise ValueError("amount_wei must be positive")
    wbnb = _contract(network, chain.addresses(network)["wbnb"], WBNB_ABI)
    return _send(network, wbnb.functions.withdraw(int(amount_wei)), gas=120_000)


def approve_exact(token: str, spender: str, amount_wei: int,
                  network: str | None = None) -> dict[str, Any] | None:
    """Approve EXACTLY ``amount_wei`` — never unlimited. None if already covered."""
    network = network or chain.default_network()
    if amount_wei <= 0:
        raise ValueError("amount_wei must be positive")
    owner = Web3.to_checksum_address(get_wallet().address)
    spender_addr = _require_allowed(network, spender)
    erc20 = _contract(network, token, ERC20_WRITE_ABI)

    current = int(erc20.functions.allowance(owner, spender_addr).call())
    if current >= amount_wei:
        return None
    # Some tokens refuse a non-zero -> non-zero approve; zero it first.
    if current > 0:
        _send(network, erc20.functions.approve(spender_addr, 0), gas=100_000)
    return _send(network, erc20.functions.approve(spender_addr, int(amount_wei)), gas=100_000)


def execute_swap(token_in: str, token_out: str, amount_in_wei: int,
                 network: str | None = None,
                 max_slippage_pct: float | None = None,
                 max_impact_pct: float | None = None) -> dict[str, Any]:
    """Exact-input single-pool swap with an output floor and an impact ceiling.

    Both guards are required and neither substitutes for the other: the floor
    stops a sandwich or a moving pool between quote and fill; the ceiling stops
    a trade that is simply too large for the pool's depth.
    """
    network = network or chain.default_network()
    cfg = chain.strategy_config(network)
    a = chain.addresses(network)
    if max_slippage_pct is None:
        max_slippage_pct = cfg["max_slippage_pct"]
    if max_impact_pct is None:
        max_impact_pct = cfg["max_price_impact_pct"]

    quote = chain.quote_swap(token_in, token_out, int(amount_in_wei), network)
    _require_impact(quote, max_impact_pct)
    floor = _min_out(int(quote["amount_out_wei"]), max_slippage_pct)

    approve_exact(token_in, a["swap_router"], int(amount_in_wei), network)
    router = _contract(network, a["swap_router"], ROUTER_ABI)
    recipient = Web3.to_checksum_address(get_wallet().address)
    result = _send(network, router.functions.exactInputSingle((
        _require_allowed(network, token_in),
        _require_allowed(network, token_out),
        int(a["fee"]), recipient, _deadline(),
        int(amount_in_wei), floor, 0,
    )), gas=400_000)
    result.update({
        "token_in": token_in,
        "token_out": token_out,
        "amount_in_wei": int(amount_in_wei),
        "quoted_out_wei": int(quote["amount_out_wei"]),
        "min_out_wei": floor,
        "price_impact_pct": quote["price_impact_pct"],
        "effective_price_usdt_per_bnb": quote["effective_price_usdt_per_bnb"],
    })
    return result
