# CLAUDE.md

Guidance for Claude Code (and any coding agent) working in this repository. **These instructions encode the
safety posture — follow them exactly.**

## What this is

An autonomous **US-equities** trading agent that mirrors the Polymarket workspace discipline (`observe → paper →
live`, fail-closed, "nothing opens by default") but is otherwise live-like, so the path to live is the same
interfaces and code path — *not* a rebuild, though live is still a separately-validated step. Authoritative
design: `docs/superpowers/specs/2026-06-08-stocks-agent-design.md`. Build is staged M0→M8; see `PLAN.md`.

**State:** M0-M7 done. **M7 OFFLINE-COMPLETE on branch `codex/m7-backtest-gate`** (1683 tests green after
historical-review hardening): anti-lookahead backtest primitives, v2 artifact verifier/metrics, full-triple artifact cache
hardening, temp-only fixture builder, paper-phase criteria/runbook, and first real strategy
`directional.momentum_v1`. Production `artifacts/backtests/` still contains only `.gitkeep`; the reviewed
historical v2 artifact has not passed: both the AAPL-only and broader `EQUS.MINI:bbo-1m` historical attempts
failed M7 criteria; an earlier non-overlapping holdout over the same broader universe also failed. The broader
rerun hardened the historical input contract against impossible receive-before-event quote rows, and follow-up
hardening now requires future reviewed manifests to hash-bind the predeclared universe hypothesis. M1 tier-2 (2b
live-verified) stays deferred (no paid live realtime subscription).
M7b added `directional.momentum_v2` plus explicit historical `strategy_id` selection; valid holdout replay still
failed every symbol, and a diagnostic broader replay also failed. The bounded diagnostic closeout rejects an M7c
gap/adverse-selection gate and closes the current L1/BBO 1-minute long-only momentum family for M7 (1693 tests green).
**Next loop is a new predeclared strategy/universe family until a reviewed artifact verifies `ok`; only then can
full autonomous paper edge-validation start.** M8/live remains blocked until passing artifact + realized paper edge
satisfy the M7-pinned criteria plus the separate two-key/live runbook requirements. Merge-to-main decision still
open.

## Hard boundaries (do not cross without explicit instruction from Robin)

- No real-money orders. `config/risk_rules.json → live_trading.enabled = false` always until an explicit go.
- `config/agent_rules.json → enabled = false` and `paper_trading.enabled = false` stay `false` on the committed
  config; these are the run gates. Do not flip them.
- Live capital requires **two-key arming** (key A = committed config flag; key B = runtime secret in `.secrets/`,
  never committed) + the M8 go-live checklist. No single commit/process supplies both keys.
- The **broker is the position-of-record**. Reconcile the local journal against it; never silently mutate it; the
  modeled (Databento-depth) fill is a label/score and never overrides the broker ledger.
- `submit_order()` is reachable only with a valid preflight token: `OpenPreflightToken` (full gates, reject-all
  when the open run-gates are off) or `ReduceOnlyPreflightToken` (held position + position-decreasing only). The
  committed-config canary (S1) must show **no opening/increasing order is ever submitted**.
- Secrets in `.secrets/` (git-ignored), never committed. Tests use spy/no-op brokers and make **no** network
  calls or credential reads.

If a task implicitly requires breaching any of these, stop and ask.

## Commands

M7 is stacked on `m6-reconcile` on branch `codex/m7-backtest-gate`. The **offline acceptance suite** (currently
1683 tests) remains stdlib-only for normal development — no install needed to run it on a bare checkout:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -t .   # -t . is required (see note)
```

> `-t .` sets the top-level dir to the repo root so test modules import as
> `tests.agent.*`; without it, `discover -s tests` would treat `tests/agent/` as a
> top-level `agent` package and shadow the real `scripts/agent`, breaking imports.

The credentialed tier-2 (2a) verifier path imports `databento` lazily; that import is **not** exercised by the offline suite. `databento==0.79.0` is pinned in `requirements.txt` and installed in `.venv` (system Python is PEP-668 externally-managed) for use only when running the credentialed historical-verification path. M2's live calendar provider pin is documented as `exchange_calendars==4.13.2`, but the requirement stays commented and the import stays lazy/deferred; `alpaca-py` is still deferred to M5.

## Architecture (seven tiers — see spec §5)

1. **Data/ingest** — Databento behind a pluggable transport seam; recorder + dual-hash replay/reconcile; bar
   cache (ET-boundary resampler). 2. **Market-state** — session/halt/LULD/SSR + fail-closed corporate actions.
3. **Snapshot/signal** — quote-quality filters + feature engine. 4. **Strategy** — `Strategy` Protocol +
`Candidate`; calibration probe first. 5. **Risk/gates** — fail-closed run gates + `can_open()` chokepoint +
`IntradayMarginModel` (FINRA 26-10) + kill switch. 6. **Paper-exec (hybrid, broker-authoritative)** — Alpaca
paper drives the order lifecycle/ledger; Databento depth drives an execution-realism label via a second-quote
preflight. 7. **Journal/reconcile** — deterministic event-sourced JSONL + rehydrate + SOD/EOD broker reconcile.

## Conventions

- **Determinism:** `json.dumps(sort_keys=True, separators=(",", ":"))`, **Decimal-as-string**, one write per row,
  row hash, `run_id`/`decision_id`/`order_id` + per-stream monotonic `seq`, single writer lock per stream.
- **Money/PnL:** distinct newtypes `BrokerUSD` (ledger truth) vs `ModeledUSD` (strategy-evaluation); never
  conflate.
- **Time:** all market logic in **America/New_York (ET)**; timestamps persisted in **UTC** (`ts_utc`) + a
  monotonic clock for latency.
- **Imports:** no installed package — `tests/__init__.py` (for `python3 -m unittest`) and a repo-root
  `conftest.py` (pytest shim) each prepend `<repo>/scripts` to `sys.path` so test modules can `import agent...`
  (mirrors Polymarket's `ROOT/scripts` convention). Repo root itself is not added.
- **Subprocess** only via fixed command arrays, never `shell=True`.
- Keep authoring and review as separate passes; verify before claiming completion.

## Communication

Robin prefers short, direct **Danish**; evidence over vibes; facts / assumptions / opinions kept distinct.
