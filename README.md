# BNB Grid Trader

An autonomous **grid trading agent** for BNB/USDT on BNB Chain, built with
[bnbagent-studio](https://github.com/bnb-chain/bnbagent-studio). It does two
things from one wallet:

1. **Trades a price grid** on PancakeSwap with its own capital — buying rungs as
   price falls, selling them one rung up as it rises.
2. **Sells its work** over ERC-8183 for `$U` — computed grid plans and live
   strategy status reports, quoted and delivered agent-to-agent.

Built against the *BNB Agent Studio Marketplace* spec, Agent #2.

## Live

| | |
| --- | --- |
| Agent (A2A) | `https://bnb-grid.172-104-171-139.nip.io` |
| Service (REST) | `https://bnb-grid-api.172-104-171-139.nip.io` |
| ERC-8004 | mainnet `269233`, testnet `1838` |

```bash
curl https://bnb-grid-api.172-104-171-139.nip.io/marketplace
curl https://bnb-grid.172-104-171-139.nip.io/.well-known/agent-card.json
```

> This is a **test deployment** on a nip.io host, with a throwaway wallet. The
> mainnet registration is named "BNB Grid Trader (test)" for that reason.

## Architecture

Two layers, two processes, one image — the split the spec asks for:

```
app/agent/     Agent Layer    A2A seller. Holds the key. Signs. :9000
  grid.py        pure math — levels, decisions, PnL. No chain, no I/O.
  chain.py       chain READS — pool price, balances, quoter.
  grid_signing.py chain WRITES — wrap / approve / swap. Never an LLM tool.
  strategy.py    state, monitor loop, operator actions, reports.
  signing.py     ERC-8183 money ops (quote / submit / settle).

app/service/   Service Layer  REST + marketplace. Holds no key. :8080
```

**The LLM never touches money.** It can read — price, balances, grid state, PnL —
and it writes the prose of a deliverable. It cannot price a quote, choose a trade,
build calldata, or sign: pricing is a fixed list price clamped before signing, the
trade decision is arithmetic in `grid.py`, and every write lives in
`grid_signing.py`, which is deliberately absent from the tool list.

## Strategy

`levels` rungs spanning ±`range_pct` around the **activation** price, spaced
geometrically so every rung earns the same percentage. A lot bought at level *i*
sells at level *i+1*; that one-step offset is the profit, less fees and slippage.
The grid is anchored at activation and does not follow price — a grid that
re-centres each poll buys every dip at the new centre and never reaches a target.

### Risk controls

| Guard | What it stops |
| --- | --- |
| `max_price_impact_pct` | A trade too large for the pool's depth |
| `max_slippage_pct` | Drift between quote and fill (sandwiches, moving pool) |
| `max_daily_loss` | A losing day compounding |
| `min_gas_reserve_bnb` | Ending up with inventory you cannot sell |
| `max_capital_usdt` | Total exposure |
| gas price / limit ceilings | A misbehaving node's absurd estimate |
| address allowlist | Any transaction to an address outside the verified book |
| emergency stop | Restarting before a human has looked |

Impact and slippage are **separate on purpose**: the quote already contains the
impact, so a 22%-impact swap fills "within slippage" and still loses 22%.

## Operating

```bash
cd app/agent
python strategy.py status                 # human-readable report
python strategy.py activate                # build the grid, arm the loop
python strategy.py step                    # run exactly one decision
python strategy.py cancel                  # SELL every open lot, clear the grid
python strategy.py stop "reason"           # emergency stop (latches)
```

Trading only happens while state is `active` **and** a monitor is running
(`GRID_MONITOR=1`, in exactly one process). Importing the code never moves funds.

## Configuration

Everything lives in `app/agent/studio.toml` under `[strategy]`. Trade sizing and
guards are **per-network** inline tables, because sizing does not port between
chains — measured live, 1 USDT moves the testnet pool 22.8% and mainnet 0.048%.
`chain.py` refuses to run without an entry for the active network rather than
guessing a trade size.

## Development

```bash
uv pip install --python app/agent/.venv/bin/python -e ./app/agent
cd app/agent && python test_grid.py        # 20 offline checks, no network
app/agent/.venv/bin/bag doctor             # scaffold + wallet + config gate
```

Deployment, the lifecycle checklist, and every non-obvious fact:
[`SOURCE_OF_TRUTH.md`](SOURCE_OF_TRUTH.md). Why things are the way they are:
[`MEMORY.md`](MEMORY.md). Hard invariants: [`AGENTS.md`](AGENTS.md).
