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
