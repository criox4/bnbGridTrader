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
| Secrets | `.studio/.env.local`, `agentcore/.env.local` — both gitignored, never read into chat |
| Wallet keystore | `.studio/wallets/` — workspace root, **never** under `app/agent/` |

## Current configuration

Mirrored from `app/agent/studio.toml` and `agentcore/agentcore.json`. Re-read those before relying on any row.

| | |
| --- | --- |
| Project / runtime name | `bnbGridTrader` |
| Framework · runtime · protocol | ADK · AgentCore · A2A |
| Network | `bsc-testnet` |
| Wallet | `evm-local` keystore, local signer — `0xFAf0ffd121947B9EE3920Fa0CfbF9EEEB0AcBF7f` (**throwaway**, imported key). Funded on bsc-testnet: ~0.30 BNB, 10 U |
| LLM | OpenRouter, `openai/gpt-4o-mini` |
| `$U` token | `0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565` |
| List price | `100000000000000000` wei (0.1 U) |
| Price clamp | floor `0` — **ceiling unset**, must be set before going live |
| Quote TTL | 900 s (SDK cap) |
| Auto-settle | off |
| Storage | `local` (file://) — offline dev only, does not survive deploy |
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
| ⬜ | `bag dev` — local A2A on `:9000`, exercise negotiate / notify_funded | never run |
| ⏸ | `bag erc8004 register` — writes `[identity]` | **deferred** until a real endpoint exists (gas-sponsored on testnet via MegaFuel, so no cost pressure to rush) |
| ⬜ | Ship — `bag deploy` (needs AWS creds) **or** Docker; see below | undecided |

Notes:

- **`[storage].kind = "local"` cannot deliver to a buyer.** It writes a `file://`
  deliverable to `~/.bag/deliverables/<project>/` and the agent mounts *no*
  job-query endpoint, by design. `submit_result` then either fails (no
  `ERC8183_AGENT_URL`) or publishes an unreachable URL on-chain — and this is true
  even against `bag dev`, not just after deploy. `bag deploy prepare` raises it as
  W10. `local` is correct only for offline dev where you read the file yourself.
  Any real buyer flow needs `ipfs` plus a pinning service's `STORAGE_API_URL` /
  `STORAGE_API_KEY`. **Deliberately not setting `ERC8183_AGENT_URL`** — with no
  job-query endpoint it would only publish a URL that 404s.
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
| `app/agent/grid.py` | Pure math — levels, decisions, PnL, `plan_for()`. No chain, no I/O. `python test_grid.py` (15 checks). |
| `app/agent/chain.py` | Chain READS — pool price, balances, quoter. Address book only. |
| `app/agent/grid_signing.py` | Chain WRITES — wrap / exact approve / swap. Fixed code, never a tool. |
| `app/agent/strategy.py` | State, monitor loop, operator CLI, reports. |

Grid shape: `levels` rungs geometrically spaced over ±`range_pct` around the
**activation** price. Equal ratios mean every rung earns the same percentage;
with arithmetic spacing the bottom rungs earn several times more than the top
ones. A lot bought at level *i* sells at level *i+1*. The grid is anchored at
activation and does NOT follow price — a grid that re-centres each poll buys
every dip at the new centre and never reaches a sell target.

Operator actions (never LLM tools — they move funds or control what does):

```
python strategy.py activate | pause | status | grid | plan | check
python strategy.py seed 0.01      # swap BNB -> USDT so buys have quote currency
python strategy.py step [--force] # run exactly one decision
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
| Container | `bnb-grid-agent`, image `bnb-grid-trader:latest`, `127.0.0.1:9001 -> 9000` |
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
            BNB_NETWORK=bsc-mainnet GRID_MONITOR=0 HOST_PORT=9001 \
  && docker compose up -d --build'
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

## Environment

- `agentcore`, `uv`, `docker` and `bag` (0.0.5) are on PATH; `bag` is also installed
  into `app/agent/.venv` — prefer `app/agent/.venv/bin/bag` so deps match the agent.
- AWS credentials are **not** configured — required before any `bag deploy`.
- Agent venv: `uv pip install --python app/agent/.venv/bin/python -e ./app/agent`
  (that venv has no `pip`; it is uv-managed).
- The root `pyproject.toml` is a comment stub, not an installable package.
- Git root is this directory. Commit messages are gated by `.githooks/commit-msg`; enable with `git config core.hooksPath .githooks`.
