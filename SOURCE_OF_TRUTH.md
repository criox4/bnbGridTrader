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
| ⬜ | Write the grid-trading strategy — no `[strategy]` block, no strategy code | not started |
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

## Environment

- `agentcore`, `uv`, `docker` and `bag` (0.0.5) are on PATH; `bag` is also installed
  into `app/agent/.venv` — prefer `app/agent/.venv/bin/bag` so deps match the agent.
- AWS credentials are **not** configured — required before any `bag deploy`.
- Agent venv: `uv pip install --python app/agent/.venv/bin/python -e ./app/agent`
  (that venv has no `pip`; it is uv-managed).
- The root `pyproject.toml` is a comment stub, not an installable package.
- Git root is this directory. Commit messages are gated by `.githooks/commit-msg`; enable with `git config core.hooksPath .githooks`.
