# AGENTS.md — Stocks trading agent workspace

Root: `<repo>`. Sibling project the engineering spine ports from: `<sibling-workspace>`.

## What this is

An autonomous **US-equities** trading agent built with the same `observe → paper → live` discipline as the
Polymarket agent: it starts **paper-only with "nothing opens by default"** and is otherwise live-like (live data,
live order semantics, live-equivalent fill realism). The full design is `docs/superpowers/specs/2026-06-08-stocks-agent-design.md`.

**Current state:** pre-implementation. Design + M0/M1 implementation plans are written and reviewed; no agent code
exists yet. Next milestone is **M0** (skeleton + abstractions + safety tests).

## Scope

Use this workspace only for: the equities trading/research agent, its data/recorder/replay stack, strategy
plugins, risk gates, paper execution, journaling, dashboard, and the supporting docs/tests. Do **not** use it for
Polymarket, Rune/general-agent, family apps, or unrelated work.

## Hard boundaries (no real money — paper-first, live-like)

These are the committed defaults. Do not cross without an explicit, separately-approved instruction from Robin.

- `config/risk_rules.json → live_trading.enabled = false` **always** until an explicit go.
- `config/agent_rules.json → enabled = false` and `paper_trading.enabled = false` are the run gates; they stay
  `false` on the committed config.
- Live capital additionally requires **two-key arming** (key A = committed git-visible config flag; key B = a
  runtime secret in `.secrets/`, never committed) **+** the M8 go-live checklist (broker dry-run, kill-switch
  drill, caps, runbook). No single commit or process may supply both keys.
- The **broker is the position-of-record**; the local journal is reconciled against it and never silently
  mutated; the Databento-modeled fill never overrides the broker ledger.
- Config-canary tests must keep passing: on the committed config, **no opening/position-increasing order is ever
  submitted** (see invariant S1).
- Secrets live in `.secrets/` (git-ignored), never committed. Tests use spy/no-op brokers and make no network
  calls.
- Paper realism improves evidence quality but is **not** live-money proof.

If a task implicitly requires breaching any of these, stop and ask.

## Session startup

Before substantive work, read:
1. `CLAUDE.md` (boundaries + conventions)
2. `PLAN.md` (active status + roadmap)
3. `MEMORY.md` (stable facts + locked decisions)
4. `docs/superpowers/specs/2026-06-08-stocks-agent-design.md` (the design; §9 safety invariants, §12 boundaries)
5. the active milestone plan under `docs/superpowers/plans/`

## Layout (target — created milestone by milestone)

```
config/        risk_rules.json · agent_rules.json · data_retention.json
scripts/agent/ orchestrator · strategy · candidate · paper_book · portfolio · execution_preflight
               broker/ (alpaca) · marketdata/ (databento) · market_state · corporate_actions
               features · gates · config · risk (intraday_margin · pdt_compat · locate) · kill_switch
scripts/recorder/  recorder · event · book_state · book_hash · persistence · replay · reconcile · status
journal/   decisions · positions · fills · status · reconcile_alerts · data_quality_alerts (.jsonl)
data/      bars/ · snapshots/ · live/          # git-ignored
dashboard/ app.py                              # stdlib, 127.0.0.1
tests/     agent/ · recorder/ · lib/
.secrets/  (git-ignored)                       # Alpaca + Databento keys
docs/superpowers/specs + plans · runbooks
```

## Safety invariants

S1–S10 are defined in spec §9 and each must have an automated test. The committed-config canary (S1) and the
fail-closed posture are the load-bearing guarantees. See the per-milestone plans for the exact test names.

## Communication

Robin prefers short, direct **Danish** by default; evidence over vibes; a clear split of facts / assumptions /
opinions. Default to "no trade / watch" when edge is unclear.
