"""Read-only chain tools exposed to this agent's LLM (ADK FunctionTool wrap).

Each entry in ``LLM_READ_TOOLS`` is a function from
``bnbagent_studio_core.tools.chain_readonly`` wrapped as an ADK ``FunctionTool``. The
LLM may call any tool in this list while producing the deliverable (the
``notify_funded`` work step); each function's docstring becomes the description
the LLM sees.

You own this file — edit ``LLM_READ_TOOLS`` to control exactly what your
agent can read on-chain. Lines for features your project doesn't use are
commented out by default; uncomment after you've added the dependency to
``studio.toml``.

**All tools are read-only** by the studio definition: no
on-chain state change, no transferable authority, no transaction signing, no
EIP-712 typed-data signing. The agent IS the sole on-chain signer,
but ALL of its signing — quote-sign, submit_result, settle, plus the
automatic budget-gated Pieverse LLM-credit auto-renew inside ``load_model()`` —
lives in ``signing.py`` as FIXED entrypoint code and is NEVER a tool the LLM
can invoke. The LLM only produces work text after a job is verified funded; it
can never price, sign, spend, or mutate chain state. Keep this list read-only.

(``pieverse_usage`` is the one exception in the underlying module: it does a
SIWE EIP-191 personal_sign, domain-locked to llm.pieverse.io, no on-chain
effect. It is commented out below.)
"""
from __future__ import annotations

from google.adk.tools import FunctionTool

from bnbagent_studio_core.tools import chain_readonly as cr

import chain as _chain
import strategy as _strat


# --- Grid tools the LLM may call ------------------------------------------------
# `network` is NOT exposed on any of these. It is a config fact, not a decision:
# left visible, a model fills it in and eventually invents a value (`'bsc'`),
# which either errors or — worse — reads the wrong chain. Deterministic code
# decides what the model operates on.
#
# Docstrings are what the model sees, so they say what the tool returns.
def get_price() -> dict:
    """Current BNB price in USDT, read from the PancakeSwap V3 pool tick."""
    return _chain.get_price()


def get_balances() -> dict:
    """This agent's BNB, WBNB and USDT balances."""
    return _chain.get_balances()


def get_status_report() -> str:
    """Finished status report for the grid strategy — status, grid, PnL, balances.

    PREFER THIS for any question about how the strategy is doing: every figure in
    it is formatted by code. Quote it verbatim.
    """
    return _strat.get_status_report()


def get_status() -> dict:
    """Raw strategy state: status, grid bounds, open lots, trade count, balances."""
    return _strat.get_status()


def get_grid() -> dict:
    """Every grid level, which levels hold a lot, and where price sits now."""
    return _strat.get_grid()


def get_performance() -> dict:
    """Realised PnL, round trips, and open inventory. Realised and unrealised
    are reported SEPARATELY — never add them together."""
    return _strat.get_performance()


def check_decision() -> dict:
    """What the strategy would do at the current price. READ-ONLY — describing a
    decision never executes it."""
    return _strat.check()


def get_plan(capital_usdt: float | None = None) -> dict:
    """Compute a grid trading plan around the current price for ``capital_usdt``.

    This is the sellable product: levels, spacing, order size, per-round-trip
    edge after fees and slippage, and the assumptions behind it. All figures are
    computed by code — quote them, do not recompute them.
    """
    return _strat.get_plan(capital_usdt)


def quote_trade(amount_usdt: float) -> dict:
    """Simulate buying BNB with ``amount_usdt`` — output and PRICE IMPACT.

    Use this to answer "how big a trade can this pool take". Read-only
    (``eth_call``); it never sends a transaction.
    """
    a = _chain.addresses()
    wei = int(amount_usdt * 10 ** _chain.decimals(_chain.default_network(), a["usdt"]))
    return _chain.quote_swap(a["usdt"], a["wbnb"], wei)


# --- Spec 5.5 names (read-only half) -------------------------------------------
# The marketplace spec names its tools differently from ours. These are thin
# aliases so anything looking for the spec's vocabulary finds it, without a
# second implementation to drift. The spec's `execute_buy` / `execute_sell` /
# `record_trade` are NOT here: they move funds or mutate the ledger, and a name
# from a spec does not make them safe for an LLM. They live in grid_signing.py
# and strategy.py as fixed code.
def get_balance() -> dict:
    """This agent's BNB, WBNB and USDT balances (alias of get_balances)."""
    return _chain.get_balances()


def calculate_grid(capital_usdt: float | None = None) -> dict:
    """Compute the grid levels and order sizing for the current price."""
    return _strat.get_plan(capital_usdt)


def get_grid_status() -> dict:
    """The active grid: levels, which hold lots, and where price sits."""
    return _strat.get_grid()


def get_open_orders() -> list:
    """Open positions, one per filled rung, each with its sell target."""
    return _strat.get_open_orders()


def calculate_order_size(capital_usdt: float | None = None) -> dict:
    """USDT committed per buy rung, and how many rungs the capital covers."""
    plan = _strat.get_plan(capital_usdt)
    return {k: plan[k] for k in ("order_size_quote", "buy_levels", "capital_quote")}


def should_buy() -> dict:
    """Whether the strategy would BUY at the current price, and why."""
    d = _strat.check()
    return {"should_buy": d["action"] == "buy", "decision": d}


def should_sell() -> dict:
    """Whether the strategy would SELL at the current price, and why."""
    d = _strat.check()
    return {"should_sell": d["action"] == "sell", "decision": d}


def calculate_pnl() -> dict:
    """Realised and unrealised PnL. They are SEPARATE — never add them."""
    return _strat.get_performance()


def calculate_grid_profit() -> dict:
    """Profit from CLOSED round trips only, with the win rate."""
    p = _strat.get_performance()
    return {k: p.get(k) for k in ("grid_profit", "completed_grids", "win_rate", "round_trips")}


def get_marketplace_data() -> dict:
    """The marketplace listing payload for this agent (spec 5.7 shape)."""
    return _strat.get_marketplace_data()


# Read-only grid + strategy tools. The WRITE path (approve / swap / wrap) lives in
# grid_signing.py and the loop controls (activate / pause / seed / step) live in
# strategy.py as operator-only CLI actions. NONE of them belong here: activate and
# pause control the thing that moves funds, and seed and step move funds directly,
# so keeping them out means no prompt injection can start, stop, or trigger a trade.
GRID_READ_TOOLS = [
    FunctionTool(get_price),
    FunctionTool(get_balances),
    FunctionTool(get_status_report),   # prefer this — figures formatted by code
    FunctionTool(get_status),
    FunctionTool(get_grid),
    FunctionTool(get_performance),
    FunctionTool(check_decision),
    FunctionTool(get_plan),
    FunctionTool(quote_trade),
    # Spec 5.5 vocabulary — read-only half only.
    FunctionTool(get_balance),
    FunctionTool(calculate_grid),
    FunctionTool(get_grid_status),
    FunctionTool(get_open_orders),
    FunctionTool(calculate_order_size),
    FunctionTool(should_buy),
    FunctionTool(should_sell),
    FunctionTool(calculate_pnl),
    FunctionTool(calculate_grid_profit),
    FunctionTool(get_marketplace_data),
]

LLM_READ_TOOLS = [
    *GRID_READ_TOOLS,

    # --- Wallet & chain basics ---
    FunctionTool(cr.wallet_info),
    FunctionTool(cr.balance_native),
    FunctionTool(cr.balance_u),         # requires [u_token] in studio.toml
    FunctionTool(cr.network_info),
    FunctionTool(cr.tx_status),

    # --- LLM provider ---
    # FunctionTool(cr.pieverse_usage),  # SIWE personal_sign; requires [llm.provider=pieverse-llm]

    # --- ERC-8004 identity (read-only lookups the LLM may want for context) ---
    FunctionTool(cr.agent_info),        # requires [erc8004] in studio.toml
    FunctionTool(cr.agent_by_address),  # requires [erc8004] in studio.toml

    # --- ERC-8183 jobs (READ-ONLY status/list — writes live in signing.py) ---
    FunctionTool(cr.job_status),        # requires [erc8183] in studio.toml
    FunctionTool(cr.job_list),          # requires [erc8183] in studio.toml
    # FunctionTool(cr.job_count),       # network-wide stat — usually noise

    # --- Advanced / footguns (commented by default) ---
    # FunctionTool(cr.contract_call_view),  # accepts any ABI — LLM-callable footgun
    # FunctionTool(cr.block_info),
    # FunctionTool(cr.wallet_list),         # multi-wallet management — dev concern
    # FunctionTool(cr.wallet_address),      # alias of wallet_info
]
