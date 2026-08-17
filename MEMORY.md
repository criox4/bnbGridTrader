# MEMORY.md — decision log

Append-only. Newest entry at the top. One entry per decision that a future reader
would otherwise have to reverse-engineer from the diff. Facts belong in
[SOURCE_OF_TRUTH.md](SOURCE_OF_TRUTH.md); this file holds *why*.

Entry format:

```
## YYYY-MM-DD — <what changed, one line>
**Why:** the problem or pressure that forced it.
**Approach:** what we picked.
**Rejected:** what we didn't pick, and the reason.
**Revisit when:** the condition that would make this decision wrong.
```

---

## 2026-08-18 — Closed the marketplace-spec gaps without weakening the LLM boundary

**Why:** a cross-check against the marketplace spec found eight gaps — no Service Layer, no marketplace payload, missing `max_daily_loss` / `emergency_stop` / `cancelGrid` / `updateGrid`, spec tool names absent, and the wrong router.
**Approach:** added them all. `app/service/` is the Service Layer (reads public, writes behind `SERVICE_API_KEY`, and an UNSET key 503s rather than running open). Spec tool names exist as thin aliases over the single implementation. `emergency_stop` LATCHES — `activate` refuses until `resume` — because a stop that auto-clears is just a pause.
**Rejected:** exposing the spec's `execute_buy` / `execute_sell` / `record_trade` as LLM tools. The spec lists them alongside the read tools, but a name from a spec does not make a fund-moving function safe to hand to a model; they live in the write path instead. Also rejected making `get_open_orders` pretend to be an order book — this agent trades spot swaps, so it reports open POSITIONS and says so.
**Revisit when:** the spec adds a limit-order venue, which would make real open orders meaningful.

## 2026-08-18 — Smart Router is mainnet-only; verified by selector, not by presence

**Why:** spec §5.1 mandates the PancakeSwap Smart Router. `0x13f4EA83` has code on BOTH chains, so a presence check would have said "deployed, use it everywhere" — and the testnet bytecode is a third the size, i.e. a different contract. That is the same trap the address book documents for `quoter_v2`.
**Approach:** probed each router's runtime bytecode for the `exactInputSingle` SELECTOR. Mainnet Smart Router exposes the 7-field form `0x04e45aaf` (no deadline); the V3 SwapRouter exposes the 8-field `0x414bf389` (with deadline). Router AND ABI are now chosen per network from the address book, and the approve targets whichever router will actually pull the tokens.
**Rejected:** assuming one address works on both chains; assuming the two routers share a parameter tuple. They do not — the Smart Router moved `deadline` into multicall.
**Revisit when:** PancakeSwap deploys a Smart Router to testnet, or a newer router version changes the tuple again.

## 2026-08-18 — Serve deliverables ourselves rather than wait for IPFS

**Why:** `submit_result` publishes `{ERC8183_AGENT_URL}/job/{id}/response` on-chain, but the A2A app mounts no such route — so a buyer can pay and never fetch. Checked the sibling deployment expecting to copy its fix: **it 404s too**, on both its ports. The gap is unsolved there, not solved.
**Approach:** a ~30-line read-only ASGI route in `main.py` serving `$STORAGE_LOCAL_PATH/job-{id}.json`, which is exactly what `LocalStorageProvider` writes. `int()` on the path segment is the traversal guard; verified 200 / 404 / traversal-blocked.
**Rejected:** deploying with the gap and calling it a known issue — that ships an agent that sells work it cannot deliver. Also rejected blocking on IPFS: it needs a paid pinning service, and IPFS remains the better answer for durability (this dies with the host), just not a reason to ship broken now.
**Revisit when:** wiring IPFS storage, which supersedes this for anything that must outlive the VPS.

## 2026-08-18 — Mainnet on the burned throwaway wallet, by explicit decision

**Why:** deploying to mainnet with the key that was pasted into chat. Flagged it; the user chose to reuse it rather than rotate.
**Approach:** deployed with `GRID_MONITOR=0` and the mainnet wallet essentially unfunded (0.0023 BNB, 0 USDT) — it serves signed quotes and plans but the gas-reserve guard refuses every write, so there is nothing to steal yet.
**Rejected:** a fresh `bag wallet new` for mainnet, which is what the earlier entry recommended. Overridden deliberately, not forgotten.
**Revisit when:** funding it for real. Anyone with this transcript can drain that address, and an ERC-8004 registration would bind it to the agent's identity permanently — so rotate BEFORE funding or registering, not after.

## 2026-08-18 — Grid sizing set from measured pool depth, not from a round number

**Why:** the first `[strategy]` defaults used `order_size_usdt = 1.0`. Quoting that size against the live testnet pool returned **22.8% price impact** — the impact guard would have refused every single trade and the grid would have sat active and idle, looking healthy.
**Approach:** measured the impact curve with the quoter before committing to numbers (0.01 → 0.13%, 0.05 → 0.47%, 0.5 → 7.8%, 1.0 → 22.8%) and set `order_size_usdt = 0.05` / `max_capital_usdt = 0.5`. The curve is recorded in `studio.toml` next to the values so the next person sees why they are small.
**Rejected:** relying on `max_slippage_pct` alone — it bounds quote→fill drift, not the impact of the trade's own size, so a 22% impact swap fills "within slippage" and still loses 22%. That is why `max_price_impact_pct` exists as a separate guard.
**Revisit when:** trading mainnet (far deeper — re-measure, don't assume) or if the testnet pool's liquidity changes.

## 2026-08-18 — Geometric grid spacing, anchored at activation

**Why:** two design choices that are invisible in code review but decide whether the strategy makes money.
**Approach:** geometric spacing (constant ratio between rungs) so every rung earns the same percentage; and the grid is anchored at the activation price, never re-centred.
**Rejected:** arithmetic spacing — the bottom rungs would earn several times more per fill than the top ones, quietly making low prices the only profitable region. Re-centring on each poll — that is not a grid: it buys every dip at the new centre and never reaches a sell target.
**Revisit when:** adding a deliberate trend-following mode, which would re-anchor on an explicit rule rather than every poll.

## 2026-08-14 — ERC-8004 registration and IPFS storage both deferred

**Why:** "configure everything" ran out of things that could be set without a decision. Both remaining items commit to something external — an on-chain agentURI, and a paid pinning service.
**Approach:** deferred both. `max_price` set to 1 U (10× list, sibling convention); signing policy left untouched (the SDK's decision tree confirms EIP-3009 on chains 56/97 is the zero-config path).
**Rejected:** registering now with a `localhost` agentURI — that is precisely what the sibling project did and then had to repoint; its mainnet id later aged out of the 8004scan indexer the CLI queries, stranding `update-endpoint`. Also rejected setting `ERC8183_AGENT_URL`: the agent mounts no job-query endpoint, so it would publish a URL that 404s rather than fixing anything.
**Revisit when:** there is a real serving endpoint (→ register), or a buyer needs to fetch a deliverable (→ ipfs, required even against `bag dev`).

## 2026-08-14 — Throwaway wallet imported; own OpenRouter key instead of `bag llm activate`

**Why:** the scaffold had an empty keystore and empty env placeholders, so nothing downstream of `bag init` could run. (An earlier read of `.env.local` reported both vars as "set" — that was a `sed` artifact; they were empty.)
**Approach:** generated a 32-char `WALLET_PASSWORD` into the gitignored `.studio/.env.local`, imported the user-supplied throwaway key over **stdin** (`--private-key -`; `bag` refuses inline keys), and set `OPENROUTER_API_KEY` directly rather than running `bag llm activate`. Also installed `bag` into `app/agent/.venv` so its deps match the agent's.
**Rejected:** `bag env set` for the password — it deliberately refuses unlock passwords as argv. `bag llm activate` — that provisions a Pieverse gateway key; the user has their own OpenRouter key.
**Revisit when:** this stops being a throwaway. The key was pasted in chat, so it must never hold real value — rotate to a fresh wallet before mainnet.

## 2026-08-14 — One instruction file for both Claude Code and Codex, via symlink

**Why:** Codex reads `AGENTS.md`, Claude Code reads `CLAUDE.md`. Two files covering the same project drift within a week.
**Approach:** `BNB_Grid_Trader/AGENTS.md` is a symlink to `CLAUDE.md` — one file to edit. The repo's own `AGENTS.md` (already read by both) gained pointers to `SOURCE_OF_TRUTH.md`, `MEMORY.md`, and the commit convention.
**Rejected:** duplicating the content into a real second file — guaranteed drift. Making `CLAUDE.md` a stub that imports `AGENTS.md` — Codex has no import syntax, so the stub direction only works one way.
**Revisit when:** the two tools need genuinely different instructions, or a Windows clone can't follow the symlink.

## 2026-08-14 — Conventional-commit gate as a committed `.githooks/commit-msg`

**Why:** commit history needed to be machine-readable, and this repo had no VCS at all until today.
**Approach:** a 6-line POSIX `sh` hook in `.githooks/`, enabled per clone with `git config core.hooksPath .githooks`.
**Rejected:** husky and the `pre-commit` framework — both add a dependency and a bootstrap step to enforce one regex. `.git/hooks/` directly — not committable, so it can't be shared.
**Revisit when:** more than one gate is needed (lint, secret scan, tests) and ordering/staging starts to matter.

## 2026-08-14 — Git root is `bnbGridTrader/`, not the parent directory

**Why:** `.gitignore` here is the only thing keeping `.studio/` (the wallet keystore) out of a commit. Initing one level up would have left it tracked-by-default.
**Approach:** `git init` inside `bnbGridTrader/`. `CLAUDE.md` stays at the parent level so it loads at session start, and is therefore outside the repo.
**Rejected:** repo at `BNB_Grid_Trader/` — secret-leak risk for no gain.
**Revisit when:** anything outside `bnbGridTrader/` needs version control.
