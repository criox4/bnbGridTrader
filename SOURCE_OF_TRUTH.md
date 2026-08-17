# SOURCE_OF_TRUTH.md

Canonical facts about this project. **Config files outrank this file** — where a
row names a file, that file is authoritative and this is a pointer, not a copy.
If they disagree, the file is right and this is stale; fix it here.
Reasoning behind these choices lives in [MEMORY.md](MEMORY.md).

## What this is

A bnbagent-studio **seller agent** (`bag init` scaffold): it sells work on BNB Chain
via ERC-8004 + ERC-8183 + x402, earning `$U`. It deploys to AWS Bedrock AgentCore.
It is not a plain AgentCore app — see `AGENTS.md` for the hard invariants.

## Authoritative locations

| Fact | Lives in |
| --- | --- |
| Framework, runtime, protocol, network, wallet, pricing, storage | `app/agent/studio.toml` |
| Runtime spec (build type, entrypoint, codeLocation, env vars) | `agentcore/agentcore.json` |
| Deploy target (account + region) | `agentcore/aws-targets.json` |
| Last deployed state | `agentcore/.cli/deployed-state.json` |
| Agent dependencies | `app/agent/pyproject.toml` |
| Secrets | `.studio/.env.local` (`WALLET_PASSWORD`, `OPENROUTER_API_KEY`, `SERVICE_API_KEY`), `agentcore/.env.local` — both gitignored, never read into chat |
| Wallet keystore | `.studio/wallets/` — workspace root, **never** under `app/agent/` |

## Current configuration

Mirrored from `app/agent/studio.toml` and `agentcore/agentcore.json`. Re-read those before relying on any row.

| | |
| --- | --- |
| Project / runtime name | `bnbGridTrader` |
| Framework · runtime · protocol | ADK · AgentCore · A2A |
| Network | `bsc-mainnet` (deployed). Testnet work needs `BNB_NETWORK=bsc-testnet` |
| Wallet | `evm-local` keystore, local signer — `0xFAf0ffd121947B9EE3920Fa0CfbF9EEEB0AcBF7f` (**throwaway**, imported key). Balances move; read them with `bag wallet balance --network <n>` rather than trusting a number here |
| LLM | OpenRouter, `openai/gpt-4o-mini` |
| `$U` token | `0xcE24439F2D9C6a2289F741120FE202248B666666` (mainnet). Testnet: `0xc70B8741…5565` |
| List price | `100000000000000000` wei (0.1 U) |
| Price clamp | floor `0`, ceiling `1000000000000000000` (1 U) |
| Quote TTL | 900 s (SDK cap) |
| Auto-settle | off |
| ERC-8004 id | mainnet `269233` ("BNB Grid Trader (test)"), testnet `1838` — per-network, both on the same address |
| Storage | `local` (file://), served back by `main.py`'s `/erc8183/job/{id}/response`. Dies with the host — IPFS is still the durable answer |
| Grid | 9 levels, ±10% around the activation price, geometric spacing |
| Sizing (mainnet) | 2 U/rung, 10 U cap, 0.5% slippage + 0.5% impact guards, 0.002 BNB gas reserve, 1 U daily-loss breaker |
| Sizing (testnet) | 0.05 U/rung, 0.5 U cap, 1% slippage + 2% impact guards, 0.02 BNB gas reserve, breaker off |
| Router | Smart Router on mainnet, V3 SwapRouter on testnet (chosen per network from the address book) |
| Build | CodeZip, entrypoint `main.py`, codeLocation `app/agent/`, PYTHON_3_14 |

## Invariants (full text in `AGENTS.md`)

1. The keystore never moves into a deploy `codeLocation`; key material is never printed or logged.
2. No secrets committed. `.env.local` files stay gitignored.
3. Signing is fixed entrypoint code — never an LLM-callable tool. MCP tools stay read-only.
4. The quote path is deterministic: fixed list price → clamp → sign. No LLM in it.
5. Deploy with `bag deploy`, never raw `agentcore deploy`. `agentcore dev`/`validate`/`status` are fine.
6. `[wallet.signing]` extra domains/types and `[payments.x402].allowed_hosts` are security boundaries — widen only on explicit request, and state the tradeoff.

## Lifecycle checklist

Order and command names taken from the `/bnbagent-studio` skill and from what the
`BNB_LP_Range_Balancer` sibling project actually ran. Update the marks as they change.

| | Step | State |
| --- | --- | --- |
| ✅ | `bag init` — scaffold `app/agent/`, `agentcore/`, venv | done |
| ✅ | `WALLET_PASSWORD` in `.studio/.env.local` | generated, 32 chars |
| ✅ | `bag wallet new --private-key -` — import throwaway key via stdin | `0xFAf0ff…BF7f` |
| ✅ | `OPENROUTER_API_KEY` via `bag env set … --file .studio/.env.local` | set |
| ✅ | `bag llm test` — LLM reachable | `pong` via OpenRouter |
| ✅ | `[payments.erc8183].max_price` — clamp ceiling | `1000000000000000000` (1 U, 10× list), matching the sibling |
| ✅ | `bag doctor` — scaffold gate | 3 WARNs, no FAILs |
| ⏸ | `[storage].kind = "ipfs"` + `STORAGE_API_URL` / `STORAGE_API_KEY` | **deferred** — needs a pinning service; `local` is fine until a buyer fetches |
| ✅ | Grid-trading strategy — `[strategy]` block + `chain.py` / `grid.py` / `grid_signing.py` / `strategy.py` | built; live on testnet |
| ✅ | Activate on testnet — seeded USDT, grid armed, first buy executed | 1 open lot at level 4 |
| ✅ | `bag dev` — local A2A, `negotiate` verified (signature recovers to our wallet) | done; funded path still untested |
| ✅ | `bag erc8004 register` — writes `[identity]` | mainnet `269233` "BNB Grid Trader (test)", testnet `1838`. Both point at the nip.io card |
| ✅ | Ship — Docker + nginx on `zd-instance`, mainnet | live at `bnb-grid.172-104-171-139.nip.io`, not trading |
| ⏸ | Rotate the wallet | **deferred to the real-domain deploy** — this is a test agent on nip.io, mainnet id is named "(test)". Rotate together with the setAgentURI on both ids |
| ⬜ | Fund mainnet + `GRID_MONITOR=1` | wallet has 0.0023 BNB, 0 USDT |
| ⬜ | Full funded job loop — createJob → fund → notify → deliver → settle | never run on either chain |

Notes:

- **`[storage].kind = "local"` now DOES deliver — because we serve it.** The
  scaffold's A2A app mounts no job-query route, so the
  `{ERC8183_AGENT_URL}/job/{id}/response` that `submit_result` publishes on-chain
  would 404 (`bag deploy prepare` raises this as W10, and the sibling deployment
  404s on both its ports to this day). `main.py`'s `_serve_deliverables` closes
  it by serving `$STORAGE_LOCAL_PATH/job-{id}.json`, which is exactly what
  `LocalStorageProvider` writes. Caveat: this storage dies with the host, and the
  on-chain URL cannot change while a job is unsettled — so IPFS remains the
  durable answer, now an upgrade rather than a blocker.
- **LLM runs through OpenRouter**, not Pieverse. `bag llm activate` is the Pieverse
  path (writes `PIEVERSE_LLM_API_KEY` + `[llm.pieverse].key_hash`); it is not
  required here, but note `bag deploy prepare` blocks on a missing `key_hash`
  *if* you ever switch `[llm].provider` to pieverse.
- **Use `app/agent/.venv/bin/bag`.** The global `bag` has no `litellm`, so
  `bag llm test` fails against it with `No module named 'litellm'`.
- **Signing policy needs no configuration.** EIP-3009 `ReceiveWithAuthorization` /
  `TransferWithAuthorization` against default domains on chains 56/97 is the
  zero-config path, and `bag wallet policy show` confirms both chains are already
  allowlisted with `Permit*` denied. Per invariant 6, leave it alone.

- **`bag deploy` is unproven on this stack.** `AGENTS.md` mandates it over raw
  `agentcore deploy`, and that stands — but the sibling project's
  `deployed-state.json` is `{"targets": {}}`: it shipped via Docker + nginx on a
  VPS instead, with the keystore bind-mounted read-only rather than baked in.
  Pick a path deliberately; AWS credentials are not configured here either way.
- **`bag erc8004 update-endpoint` can stop finding the agent.** The sibling's
  mainnet id `265375` aged out of the 8004scan indexer the CLI queries, so the
  endpoint had to be updated by token id through the library. Register with the
  final endpoint if you can.

## The strategy

Two products from one agent: an autonomous grid trades the wallet's own capital,
and buyers pay $U for status reports and computed grid plans.

| Module | Role |
| --- | --- |
| `app/agent/grid.py` | Pure math — levels, decisions, PnL, `plan_for()`. No chain, no I/O. `python test_grid.py` (20 checks). |
| `app/agent/chain.py` | Chain READS — pool price, balances, quoter. Address book only. |
| `app/agent/grid_signing.py` | Chain WRITES — wrap / exact approve / swap. Fixed code, never a tool. |
| `app/agent/strategy.py` | State, monitor loop, operator CLI, reports. |
| `app/service/main.py` | Service Layer (spec §2) — REST, marketplace payload, job visibility. Holds no key; owns the monitor. |

Grid shape: `levels` rungs geometrically spaced over ±`range_pct` around the
**activation** price. Equal ratios mean every rung earns the same percentage;
with arithmetic spacing the bottom rungs earn several times more than the top
ones. A lot bought at level *i* sells at level *i+1*. The grid is anchored at
activation and does NOT follow price — a grid that re-centres each poll buys
every dip at the new centre and never reaches a sell target.

Operator actions (never LLM tools — they move funds or control what does):

```
python strategy.py activate | pause | status | grid | plan | check
python strategy.py marketplace | orders | performance
python strategy.py seed 0.01      # swap BNB -> USDT so buys have quote currency
python strategy.py step [--force] # run exactly one decision
python strategy.py cancel         # cancelGrid — SELLS every open lot, then clears
python strategy.py update L U N   # updateGrid — re-shape in place, keeping lots
python strategy.py stop "reason"  # emergency stop; LATCHES (activate refuses)
python strategy.py resume         # release the emergency stop
python strategy.py reset          # forget grid + log WITHOUT selling
```

The monitor loop is opt-in via `GRID_MONITOR=1`, because exactly one process may
run it and the `flock` guard cannot see a process on another host.

**Pool depth is the binding constraint, not slippage.** Measured live on the
bsc-testnet BNB/USDT fee-500 pool, 2026-08-18: 0.01 USDT → 0.13% impact,
0.05 → 0.47%, 0.5 → 7.8%, 1.0 → **22.8%**. The slippage floor cannot catch this —
the quote already contains the impact, so a 22% impact trade fills "within
slippage" and still loses 22%. Hence the separate `max_price_impact_pct` guard
and the small `order_size_usdt`. Re-measure before raising either, and again on
mainnet (far deeper).

## Deployment

Live on **bsc-mainnet** at `https://bnb-grid.172-104-171-139.nip.io`
(agent card, `negotiate`, `notify_funded`, and `/erc8183/job/{id}/response`).

| | |
| --- | --- |
| Host | `zd-instance` — 172.104.171.139, Ubuntu, root |
| Path | `/root/BNBAgents/bnb-grid-trader/` (same layout as the sibling agents) |
| Containers | `bnb-grid-agent` (A2A, `127.0.0.1:9001`) + `bnb-grid-service` (REST, `127.0.0.1:8081`), one image |
| Service API | `https://bnb-grid-api.172-104-171-139.nip.io` — reads public, writes need `X-API-Key` |
| Ingress | nginx `bnb-grid.conf` + certbot TLS; loopback-only upstream |
| Trading | **`GRID_MONITOR=0` — not trading.** Serving quotes and plans only. |

Deploy / redeploy:

```bash
rsync -az --delete --exclude '.git/' --exclude '**/.venv/' --exclude '__pycache__/' \
      --exclude 'agentcore/cdk/node_modules/' --exclude 'app/agent/wheels/' \
      --exclude '.grid_state.*' ./ zd-instance:/root/BNBAgents/bnb-grid-trader/
ssh zd-instance 'cd /root/BNBAgents/bnb-grid-trader \
  && chown -R 10001:10001 .studio/wallets && chmod 700 .studio/wallets \
  && chmod 600 .studio/wallets/*.json \
  && set -a && . .studio/.env.local && set +a \
  && export PUBLIC_URL=https://bnb-grid.172-104-171-139.nip.io \
            BNB_NETWORK=bsc-mainnet GRID_MONITOR=0 \
            HOST_PORT=9001 SERVICE_HOST_PORT=8081 \
  && docker compose up -d --build'
```

`SERVICE_API_KEY` comes from `.studio/.env.local` via the `set -a` above. It gates
every fund-moving REST endpoint; when unset those endpoints return 503 rather than
running open, so a fresh deploy never briefly exposes `/cancel` to the internet.
`GRID_MONITOR` drives the SERVICE container's monitor — the agent container is
pinned to 0 so exactly one process ever polls.

```bash
```

Three things that fail silently if skipped — all three were hit on the first deploy:

1. **`chown 10001` the keystore.** rsync preserves the macOS uid, so it arrives
   `600 501:staff`. The container boots healthy and only fails at the FIRST
   SIGNATURE, as a `PermissionError` through the A2A error channel.
2. **`AGENTCORE_RUNTIME_URL` must be set.** `serve_a2a` takes the card's public
   `url` from it; AgentCore injects it, a VPS does not. Without it the card
   served `http://localhost:9000/` — pointing every buyer at their own machine.
3. **`PUBLIC_URL` must be the real hostname.** `submit_result` publishes
   `{ERC8183_AGENT_URL}/job/{id}/response` ON-CHAIN and it can never change while
   a submitted job is unsettled.

## Marketplace spec compliance

Built against `BNB Agent Studio Marketplace` §5 (Agent #2). Deviations that remain
deliberate:

- **Spacing defaults to geometric**, not the spec example's arithmetic. Both are
  supported via `[strategy].spacing`; geometric is the default because equal
  ratios make every rung earn the same percentage, where arithmetic makes the
  bottom rungs worth several times more than the top ones. Set `spacing =
  "arithmetic"` to match the spec example exactly.
- **Smart Router on mainnet only.** Spec §5.1 requires it; it is deployed on
  mainnet (selector `0x04e45aaf`, 7-field params, no deadline) and NOT on
  testnet, where `0x13f4EA83` is a different contract. Testnet falls back to the
  V3 SwapRouter (`0x414bf389`, 8 fields, with deadline). The ABI is chosen from
  the address book per network — sending one shape to the other router reverts.
- **`get_open_orders` returns open POSITIONS, not resting orders.** This agent
  trades spot swaps; there is no order book to have orders on.

## Environment

- `agentcore`, `uv`, `docker` and `bag` (0.0.5) are on PATH; `bag` is also installed
  into `app/agent/.venv` — prefer `app/agent/.venv/bin/bag` so deps match the agent.
- AWS credentials are **not** configured — required before any `bag deploy`.
- Agent venv: `uv pip install --python app/agent/.venv/bin/python -e ./app/agent`
  (that venv has no `pip`; it is uv-managed).
- The root `pyproject.toml` is a comment stub, not an installable package.
- Git root is this directory. Commit messages are gated by `.githooks/commit-msg`; enable with `git config core.hooksPath .githooks`.
