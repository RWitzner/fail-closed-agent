# CLAUDE.md

Guidance for Claude Code (and any coding agent) working in this repository. **These instructions encode the
safety posture — follow them exactly.**

## What this is

An autonomous **US-equities** trading agent that mirrors the Polymarket workspace discipline (`observe → paper →
live`, fail-closed, "nothing opens by default") but is otherwise live-like, so the path to live is the same
interfaces and code path — *not* a rebuild, though live is still a separately-validated step. Authoritative
design: `docs/superpowers/specs/2026-06-08-stocks-agent-design.md`. Build is staged M0→M8; see `PLAN.md`.

**State:** M0–M5 done (M5 hardened, 1520 tests). **M6 IN PROGRESS on branch `m6-reconcile`** (stacked on
`m3-signal`→`m2-market-state`; nothing merged to `main`): contract frozen rev 6 READY-TO-BUILD (`1e96d5d`,
48-finding critic archive, 4 re-critique rounds to convergence); W1 built+committed (`2c2b6ed`, 1574 tests);
W2 pure diff core built+committed (`436f7b0`, 1590 tests); W3 PaperBook `position_adjust` fold built (1599 tests);
W4 orchestrator reconcile wiring built (1611 tests); W5 CLI/canary/purity/runbook built (1636 tests);
next is W6 = adversarial review. M1 tier-2 (2b live-verified) stays deferred (no paid
live realtime subscription). **Plan locked (2026-06-10): finish M6→M7, then a full autonomous paper
edge-validation phase (success criteria pinned in advance, in the M7 contract) before any M8/live step — see
PLAN.md "Edge before live". Merge-to-main decision still open.**

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

M4 has landed. The **offline acceptance suite** (896 tests) remains stdlib-only for normal development — no install needed to run it on a bare checkout:

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
