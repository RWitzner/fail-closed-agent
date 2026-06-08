# CLAUDE.md

Guidance for Claude Code (and any coding agent) working in this repository. **These instructions encode the
safety posture — follow them exactly.**

## What this is

An autonomous **US-equities** trading agent that mirrors the Polymarket workspace discipline (`observe → paper →
live`, fail-closed, "nothing opens by default") but is otherwise live-like, so the path to live is the same
interfaces and code path — *not* a rebuild, though live is still a separately-validated step. Authoritative
design: `docs/superpowers/specs/2026-06-08-stocks-agent-design.md`. Build is staged M0→M8; see `PLAN.md`.

**State:** pre-M0. No agent code yet — only design + plans + this charter.

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

The repo is pre-M0; no runnable agent code yet. Once M0 lands, its acceptance command (bare checkout) is:

```bash
python3 -m pip install -r requirements.txt        # M0 is stdlib-only; deps pinned per milestone
python3 -m unittest discover -s tests -p 'test_*.py'
```

M0 is stdlib-only. Third-party deps are pinned in the milestone that first imports them: `databento` +
`exchange_calendars` in M1, `alpaca-py` in M5.

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
- **Imports:** no installed package — `tests/conftest.py` bootstraps `sys.path` to repo root + `scripts/`
  (mirrors Polymarket's `ROOT = parents[N]` convention).
- **Subprocess** only via fixed command arrays, never `shell=True`.
- Keep authoring and review as separate passes; verify before claiming completion.

## Communication

Robin prefers short, direct **Danish**; evidence over vibes; facts / assumptions / opinions kept distinct.
