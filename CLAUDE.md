# CLAUDE.md

Instructions for Claude Code (and any other coding agent) working in this repository. **These instructions
encode the safety posture — follow them exactly.** For what the project is and what it found, read `README.md`;
for the architecture patterns, `docs/ARCHITECTURE.md`; for the measured results, `docs/RESULTS.md`; for the
build chronology, `PLAN.md`.

## What this is

An autonomous **US-equities** trading agent, built paper-first with a fail-closed posture: nothing opens by
default, live capital needs two-key arming, the broker is the position-of-record, every decision is journaled.
It is otherwise live-like — the path to live is the same interfaces and the same code path, not a rebuild,
though live remains a separately-validated step.

**It has never placed a trade.** Two strategy families were predeclared, tested, and nulled on their own
criteria; the line was then closed under a written stop rule. No reviewed backtest artifact passes, so the S9
gate blocks every real-strategy open. That is the standing posture, not a bug.

The authoritative design (frozen as authored, before the strategy work) is
`docs/superpowers/specs/2026-06-08-stocks-agent-design.md`.

## Hard boundaries (do not cross without an explicit, separately-approved instruction)

- **No real-money orders.** `config/risk_rules.json → live_trading.enabled` stays `false`.
- `config/agent_rules.json → enabled` and `paper_trading.enabled` are the run gates; they stay `false` on the
  committed config. Do not flip them.
- Live capital requires **two-key arming** — key A = the committed config flag, key B = a runtime secret under
  `.secrets/` that is never committed — plus the go-live checklist. No single commit or process supplies both.
- The **broker is the position-of-record.** Reconcile the local journal against it; never silently mutate it.
  The modeled fill is a label and never overrides the broker ledger.
- `submit_order()` is reachable only with a valid preflight token: `OpenPreflightToken` (full gates; rejects
  everything while the open run-gates are off) or `ReduceOnlyPreflightToken` (held position, position-decreasing
  only). The committed-config canary (S1) must keep showing that **no opening or increasing order is ever
  submitted**.
- Secrets live in `.secrets/` (git-ignored) and are never committed. Tests use spy/no-op brokers, read no
  credentials, and make no external network calls.
- Paper realism improves the quality of the evidence; it is **not** proof about live money.

If a task implicitly requires breaching any of these, stop and ask.

## Commands

The **offline acceptance suite** is stdlib-only and runs on a bare checkout with no install:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -t .   # -t . is required (see note)
```

> `-t .` sets the top-level directory to the repo root so test modules import as `tests.agent.*`. Without it,
> `discover -s tests` treats `tests/agent/` as a top-level `agent` package and shadows the real
> `scripts/agent`, breaking imports.

CLI entry points need `PYTHONPATH=scripts` (there is no installed package):

```bash
PYTHONPATH=scripts python3 -m agent.paper_session --help
PYTHONPATH=scripts python3 -m dashboard --journal-dir journal    # read-only, loopback only
```

`requirements.txt` pins the three third-party packages the **credentialed** paths use. Every one of them is
imported lazily inside a function, never at module scope, so the offline suite stays green without them.

## Architecture (seven tiers — see the design spec §5)

1. **Data/ingest** — vendor feeds behind a pluggable transport seam; recorder + dual-hash replay/reconcile; bar
   cache with an ET-boundary resampler. 2. **Market-state** — session/halt/LULD/SSR plus fail-closed corporate
   actions. 3. **Snapshot/signal** — quote-quality filters + feature engine. 4. **Strategy** — a `Strategy`
   protocol + `Candidate`; calibration probe first. 5. **Risk/gates** — fail-closed run gates, the `can_open()`
   chokepoint, an intraday margin model, and a kill switch. 6. **Paper execution** (hybrid, broker-authoritative)
   — the broker drives the order lifecycle and ledger; a second-quote preflight drives an execution-realism
   label. 7. **Journal/reconcile** — deterministic event-sourced JSONL, rehydrate, SOD/EOD broker reconcile.

## Conventions

- **Determinism:** `json.dumps(sort_keys=True, separators=(",", ":"))`, **Decimal-as-string**, one write per
  row, a per-row hash, `run_id`/`decision_id`/`order_id` plus a per-stream monotonic `seq`, one writer lock per
  stream.
- **Money:** the newtypes `BrokerUSD` (ledger truth) and `ModeledUSD` (strategy evaluation) are distinct and
  must never be conflated.
- **Time:** all market logic in **America/New_York**; timestamps persisted in **UTC** (`ts_utc`), plus a
  monotonic clock for latency.
- **Imports:** there is no installed package. `tests/__init__.py` (for `unittest`) and the repo-root
  `conftest.py` (a pytest shim) each prepend `<repo>/scripts` to `sys.path` so tests can `import agent...`.
  The repo root itself is not added.
- **Subprocess** only via fixed command arrays, never `shell=True`.
- Keep authoring and review as separate passes. Verify before claiming completion.
- **Never commit market data.** Recorded quotes, bars, journals and run reports are vendor-licensed and are
  git-ignored. Test fixtures must be synthesized — every committed fixture is reproducible from a committed
  generator or from the spec that authored it.
