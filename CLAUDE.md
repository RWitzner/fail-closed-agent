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
gap/adverse-selection gate and closes the current L1/BBO 1-minute long-only momentum family for M7 (1693 tests).
**M7c is the next predeclared family, proxy-first**
(`docs/superpowers/specs/2026-06-13-M7c-relative-strength-research-packet.md`, rev 2, reviewed APPROVE): phase 1 is
`relative_strength.long_only_proxy_v1`, a long-only cross-sectional gating probe; phase 2 (true market-neutral,
multi-leg/short-side) is conditional on a predeclared Phase Gate. A two-family search-budget stop rule in `PLAN.md`
forces a substrate decision (longer horizon / L2 depth / wider universe) over a third same-substrate family if M7c
also nulls. **The phase-1 strategy unit, the multi-symbol harness, AND the step-5 artifact plumbing are BUILT** (committed
`b9a8756` on `codex/m7-backtest-gate`, **1740 tests green**): the pure ranking + proxy +
`universe_equal_weight_long_v1` benchmark id + contract + TDD tests (reviewed APPROVE); the multi-symbol
same-timestamp cross-sectional decision harness `run_historical_cross_sectional_backtest`
(`backtest_historical.py` — timestamp alignment across the universe, top-2 long, 30-bar horizon, M7 quote-B
latency, no-overlap per symbol, both legs aggregated under one artifact, exposure-matched
`universe_equal_weight_long_v1` attribution, reusing `_simulate_historical_long_trade` unchanged); and now
(packet step 5) `validate_historical_cross_sectional_manifest` + `write_m7_historical_cross_sectional_artifact` +
the `m7-historical-cross-sectional-artifact` CLI. One hash-bound multi-symbol manifest binds the predeclared
universe block + a per-symbol data binding (`{instrument_id,row_count,quote_rows_sha256}`); the per-symbol
`data_pin` is derived from `manifest_hash:symbol` (no circular hash) and `horizon` is hash-bound + validated (not
silently defaulted). Both long legs aggregate into one `(strategy_id,rules_hash,data_pin)` v2 artifact (FD-P1-10)
with the equal-weight-long attribution + a canonical-JSON breadth/leg diagnostics string in provenance (FD-P1-9);
`verify_artifact` was widened on its OPTIONAL provenance allow-set only (pinned benchmark, floors, required keys,
metric schema unchanged). Same fail-closed write guard as single-symbol; only `relative_strength.long_only_proxy_v1`
accepted. The data-production half is also BUILT + COMMITTED (`e45c2c0`): `scripts/agent/historical_backfill.py`
— `build_cross_sectional_input_manifest` (+ `normalize_quote_event`, `derive_session_windows`,
`instrument_ids_from_rows`, `cross_sectional_data_pin`), whose build→validate→write→run round-trip is pinned against
the real harness, plus a tier-2b live-pull seam (`pull_normalized_window`) that is offline-tested via an injected
source and **fails closed**; the real `databento` `get_range` + DBN `bbo-1m` decode is lazily imported and flagged
tier-2b-UNVERIFIED (raises) until verified against the live API. Two adversarial reviews (5- and 4-dimension, each
finding independently verified) returned `changes_required`; all must-fixes were applied (**1761 tests green**).
The live `bbo-1m` pull is wired + verified against the real `EQUS.MINI` `BBOMsg` record, and **the credentialed
clean-window run is DONE: M7c phase-1 NULLED on the packet's preferred clean window `2026-03-10→2026-04-08` (21
sessions, 1144 trades, staged-only, no production write).** Broad NO-GO: net `-$839.68`, active `-$120.65` vs the
pinned `exposure_matched_midbar_v1` (the `+$405.64` active vs `universe_equal_weight_long_v1` only means the basket
lost more), profit factor `0.55`, and the realism gaps blow the caps (p95 `29.8` > 15, max `97.5` > 50). Breadth
was broad (10 symbols traded, max 12.3% of legs / 16.8% of net-positive PnL — no concentration breach), so this is
**decisive evidence the binding constraint is the SUBSTRATE (L1 1-minute BBO), not the long-only strategy shape.**
**Per the predeclared Phase Gate + the two-family search-budget stop rule (momentum = family 1 nulled;
relative-strength = family 2 now nulled clean), the next step is a SUBSTRATE decision (longer decision/holding
horizon, L2/MBP-10 depth-aware fill tier, or wider liquidity-screened universe) — NOT the phase-2 short-side build
and NOT a third same-substrate family. **Robin's call (2026-06-26): an explicit STOP on the autonomous edge search
(the packet's sanctioned outcome after two same-substrate families nulled) — no substrate family is in flight; the
strategy-search loop is paused pending a scope/ambition reassessment and the long-pending merge-to-main decision.**
No reviewed artifact verifies `ok`; production `artifacts/backtests/` still contains only `.gitkeep`; the staged run +
quotes live under gitignored `reports/m7_historical_runs/2026-03-10-clean-rs-v1/` (reproducible from the committed
backfill tool). Paper edge-validation + M8/live remain blocked. Merge-to-main decision still open.

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
1761 tests) remains stdlib-only for normal development — no install needed to run it on a bare checkout:

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
