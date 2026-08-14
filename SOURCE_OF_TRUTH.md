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
| Wallet | `evm-local` keystore, local signer — address not yet set (`bag wallet new`) |
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

## Environment

- `agentcore` and `uv` are on PATH. **`bag` is not installed** — install bnbagent-studio before running any `bag` command.
- Agent venv: `python -m venv app/agent/.venv && app/agent/.venv/bin/pip install -e ./app/agent`.
- The root `pyproject.toml` is a comment stub, not an installable package.
- Git root is this directory. Commit messages are gated by `.githooks/commit-msg`; enable with `git config core.hooksPath .githooks`.
