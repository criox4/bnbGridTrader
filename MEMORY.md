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

## 2026-08-18 — End-to-end seller lifecycle reached SUBMITTED on mainnet and COMPLETED on a fork
**Why:** the agent needed a real test of the full seller path, not only quote signing and isolated grid trades.
**Approach:** ran a temporary local A2A seller against BSC mainnet with a 0.02 U smoke price: negotiate → quote anchoring → create/register/set budget/fund → `notify_funded` → OpenRouter work → signed submit. Mainnet job 56608 reached `SUBMITTED`; a disposable fork of the same state advanced seven days and verified `router.settle` reaches `COMPLETED` without waiting or spending another mainnet transaction.
**Rejected:** changing the production price/wallet or pretending the mainnet job was already settled. The real job remains `SUBMITTED` until its seven-day dispute window expires.
**Revisit when:** the mainnet job's dispute window expires; settle it on mainnet and verify the final U transfer. The same run exposed that the local deliverable route expects `job-{id}.json` while the SDK writes `erc8183-job-{id}.json`, so fetch is not production-ready until that mismatch is fixed.

## 2026-08-18 — Cross-wallet mainnet smoke test stayed isolated from the deployed signer
**Why:** verify that a second throwaway wallet can execute the grid write path without silently replacing the wallet configured for the live seller agent.
**Approach:** used a temporary workspace-level keystore and state directory, seeded only a small USDT balance, activated the mainnet grid, and completed one guarded buy. Receipt inspection confirmed the seed swap, approval, and grid swap all came from the second wallet and mined successfully. The repository config, deployed wallet, and live monitor were left unchanged.
**Rejected:** switching `app/agent/studio.toml` or redeploying the live seller for this smoke test — that would turn an isolated wallet-selection test into a production signer rotation.
**Revisit when:** the second wallet is explicitly promoted to the live signer; rotate the pasted key first and update the deployment secrets/config together.

## 2026-08-18 — Why settlement waits, and the one way to make it not wait

**Why:** "if the buyer approves, why wait 7 days?" Verified against the ABIs, the local SDK docs, the EIP text and free `eth_call` probes rather than reasoning from the SDK alone.

**What is actually true:**
- **There is no on-chain approve.** `OptimisticPolicy` exposes only `dispute()` and `voteReject()`. The SDK docstring and the BNB docs both state it: "silence past the dispute window is implicit approval… there is no `voteApprove` on-chain". `settle --action approve` just calls `router.settle()`, which READS the verdict — it reverts `NotDecided()` until the window elapses. Approval is the absence of a dispute, and absence can only be proven by time.
- **The window is NOT part of ERC-8183.** The EIP says "no dispute resolution or arbitration" at the kernel, and that the evaluator "MAY be the client… so the client can complete or reject the job without a third party". `complete(jobId, reason, optParams)` is evaluator-only with NO waiting at kernel level. The 7 days come from the v1 deployment pattern where `evaluator = router = OptimisticPolicy`, not from the standard.
- **The router-hook does NOT gate completion.** Probed `beforeAction(jobId, complete_selector)` from the kernel address on real registered jobs: **ALLOWED**. The verdict gate lives in `router.settle()`, not in the hook. So `evaluator = our EOA` + `hook = router` (policy registered to satisfy the hook) settles the moment the job is SUBMITTED — no window. This is the same-day path on mainnet.

**Two ways the BNB kernel DEVIATES from the EIP** (both verified by eth_call, both the reason testnet cannot be worked around): the EIP calls the hook optional and `address(0)` "fully compliant", but this kernel raises `HookRequired()` for a zero hook — unconditionally, for EOA and router evaluators alike. And it ERC-165-checks the hook: an EOA or a random contract raises `HookMissingInterface()`. The router is the only compliant hook deployed, and it refuses to act on a job with no registered policy (`PolicyNotSet()`), which needs a whitelisted policy, which testnet no longer has. The chain is closed at every link.

**Tradeoff if the EOA-evaluator path is ever used for real buyers:** buyer-as-evaluator means the buyer can also reject and refund AFTER delivery. The SDK flags exactly this as `CLIENT_AS_EVALUATOR`. Fine when we are both sides of a smoke test; wrong for customers — which is what the optimistic policy exists to prevent.

**Revisit when:** running the loop on mainnet (use evaluator = our own address to skip the 7 days), or if a testnet policy is re-whitelisted.

## 2026-08-18 — The funded job loop is BLOCKED on testnet by an external de-whitelist

**Why:** attempted the full `createJob → fund → notify → deliver → settle` loop on testnet (free, 10 $U held). `bag erc8183 buy` reverted at register with `0xc94463e3` = `PolicyNotWhitelisted()`.
**Approach:** decoded the selector against the shipped ABIs rather than guessing, then read the router directly. `EvaluatorRouter.policyWhitelist(0x4f4678d4…)` is **False on testnet and True on mainnet** — same SDK, same pinned addresses. Jobs 7 and 9 carry that exact policy, so it worked once and was switched off by the stack's owner. Not our bug and not fixable from here.
**Rejected:** three routes around it, each closed by the chain itself — (1) `hook = 0x0` → `HookRequired()`; (2) `hook = router, evaluator = us` (unrouted job, completed via `commerce.complete()` instead of `router.settle()`, which the SDK explicitly tolerates — it warns `CLIENT_AS_EVALUATOR` but returns `valid: True`) → `PolicyNotSet()` at fund, because the router-as-hook refuses to act on a job with no registered policy; (3) finding a replacement whitelisted policy → every public testnet RPC prunes logs past ~49k blocks, so the whitelist history cannot be read without an archive provider.
**Revisit when:** the studio re-whitelists a testnet policy (re-check `policyWhitelist` before assuming), or the loop runs on mainnet instead — see the constraints below.

**Two facts that decide the mainnet alternative:** the wallet holds **0.0224 U against a 0.1 U list price**, so it cannot pay itself; and mainnet's OptimisticPolicy `dispute_window` is **7 days** (testnet's is 1), so settlement cannot complete same-day even once funded.

**One testnet write vanished; cause NOT established.** A `createJob` broadcast returned a hash, then appeared in neither mempool nor block and the nonce never advanced (`TransactionPendingError` after 300s). Disabling the MegaFuel paymaster — `dataclasses.replace(resolve_network(net), use_paymaster=False)`, which makes `_build_paymaster` return None — and retrying the SAME call mined it. That is suggestive but NOT conclusive: jobs 528/529 were created earlier through `bag erc8183 buy` with the paymaster ON and mined fine, so it is one drop against two successes on the same path, and transient testnet congestion explains it equally well. Self-paying is the reliable workaround; do not treat sponsorship as known-broken on the strength of one sample.

**Left behind:** testnet jobs 528/529/530 sit OPEN with no escrow (530 has a budget set but was never funded — balance is still 10.0 U). They expire on their own; rejecting them would hit the same `PolicyNotSet()` hook.

## 2026-08-18 — `get_open_orders` reports both sides, not just the fills

**Why:** the earlier entry justified reporting positions instead of resting orders, and that half was right — but it shipped only the SELL side. An armed grid with no fills yet returned `[]`, which reads as "nothing happening" when in fact four buy rungs are committed. Live testnet check: 4 real pending buys were invisible.
**Approach:** `grid.pending_orders()` derives both sides from `decide()`'s own rules — a sell per open lot (trigger = level above its buy), a buy per unfilled rung at or below centre — so anything listed is what the next qualifying poll actually does. Each entry carries `resting: false`. Pure math in `grid.py`, so it is testable offline; `strategy.py` only attaches amounts. Two tests added (22 total).
**Rejected:** faking a resting order book (the earlier call, still right — nothing rests anywhere). Also rejected asserting one decide() action per trigger price in the test: a rung's buy price IS the sell target of the rung below, and decide() sells first. Both orders are genuinely pending; which fires is decide()'s business, not this list's. The test checks the buy rule against an empty lot map instead.
**Revisit when:** `decide()`'s buy or sell rule changes — `pending_orders()` mirrors it by hand and would silently start lying.

## 2026-08-18 — Closed the marketplace-spec gaps without weakening the LLM boundary

**Why:** a cross-check against the marketplace spec found eight gaps — no Service Layer, no marketplace payload, missing `max_daily_loss` / `emergency_stop` / `cancelGrid` / `updateGrid`, spec tool names absent, and the wrong router.
**Approach:** added them all. `app/service/` is the Service Layer (reads public, writes behind `SERVICE_API_KEY`, and an UNSET key 503s rather than running open). Spec tool names exist as thin aliases over the single implementation. `emergency_stop` LATCHES — `activate` refuses until `resume` — because a stop that auto-clears is just a pause.
**Rejected:** exposing the spec's `execute_buy` / `execute_sell` / `record_trade` as LLM tools. The spec lists them alongside the read tools, but a name from a spec does not make a fund-moving function safe to hand to a model; they live in the write path instead. Also rejected making `get_open_orders` pretend to be an order book — this agent trades spot swaps, so it says so. (Superseded in part: it now reports BOTH sides, still `resting: false` — see the entry above.)
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
