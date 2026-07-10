# Paper Go-Live Checklist

- **Date:** 2026-07-02 (updated 2026-07-10 — live view, autorun, status plane, reconnect)
- **Scope:** everything between "the code is done" and "the agent paper-trades autonomously every
  session". This runbook flips nothing: every arming step is an explicit Robin action, and the
  committed repo stays fail-closed (S1: committed config ⇒ zero opening submits — pinned by canary
  tests).
- **Sibling runbooks:** `docs/runbooks/m7-paper-edge-validation.md` (the formal edge-validation
  criteria the paper phase is measured against), `docs/runbooks/m6-reconcile.md`.

## Start TODAY (no decisions, no credentials, nothing armed)

Watch a full session in the live view right now — replay rehearsal with real order lifecycle
evidence in the browser:

```bash
PYTHONPATH=scripts python3 -m agent.paper_session \
  --journal-dir reports/demo/journal --replay tests/fixtures/execution/observe_session_tbbo.jsonl \
  --symbols AAPL --session-date 2026-07-06 --report-dir reports/demo/reports
PYTHONPATH=scripts python3 -m dashboard --journal-dir reports/demo/journal \
  --report-dir reports/demo/reports        # → http://127.0.0.1:8788/
```

**Robins tre eksterne trin** (the ONLY things code cannot do — each is a single action):

1. **Alpaca paper-konto** → step A below (≈15 min, gratis) — unlocks the account verifier + drill.
2. **Betalt Databento live-realtime-abonnement** → step B below — unlocks OBSERVE-mode (live data,
   nul ordrer) same-day, and the tier-2b verification session.
3. **Arming-beslutningerne** → steps C/D/E below — and the S9 question (paper uden valideret edge?)
   which is deliberately NOT coded around: read
   `docs/superpowers/specs/2026-07-10-paper-canary-decision-memo.md` and choose.

## Phase map (observe → paper → live) and today's status

| Phase | What runs | Blocking prerequisites | Status today |
|---|---|---|---|
| **Rehearsal (replay)** | `python3 -m agent.paper_session --journal-dir <dir> --replay <events.jsonl> --symbols ...` — the full SOD→loop→EOD→report path over recorded data | none | **RUNNABLE NOW** |
| **Observe (live data, zero orders)** | same command with `--live` and NO `--strategy-id`: the M3 calibration probe journals forecasts; nothing can open | B (live data subscription + tier-2b verify) | blocked on **B** only |
| **Paper (live data, paper orders)** | same command with `--strategy-id <id>`; opens require S9 artifact + arming | A + B + C + D + E | blocked on A/B/C/D/E |
| **Live (real money)** | M8 — out of scope here | M8 checklist + two-key arming + realized paper edge | far gated |

The steps below are ordered. A, B and C are independent and can run in parallel; D and E come last.

## Step A — Alpaca paper account (Robin, ~15 min, free)

1. Create the account at https://app.alpaca.markets (paper trading is free; no funding needed).
2. Generate PAPER API keys (the dashboard's paper-trading section — NOT live keys).
3. Write `.secrets/alpaca_paper.json` (git-ignored; the loader requires exactly these keys):

   ```json
   {"key_id": "PK...", "secret_key": "...", "base_url": "https://paper-api.alpaca.markets"}
   ```

4. Verify (read-only; writes a REDACTED artifact under `reports/alpaca_paper/`):

   ```bash
   PYTHONPATH=scripts .venv/bin/python3 -m agent.verify_alpaca_paper
   ```

   Expect `ok: true`. Then the submit→cancel round-trip drill (paper account, a $1.00 limit that
   can never fill):

   ```bash
   PYTHONPATH=scripts .venv/bin/python3 -m agent.verify_alpaca_paper --allow-order-drill
   ```

   Both must pass before any armed session. `alpaca-py==0.43.5` is already pinned and installed in
   `.venv`; the offline suite never needs it. Precondition if you override `--drill-symbol`: the
   symbol must trade ABOVE $1.00 (the drill is a buy at a fixed $1.00 limit — on a sub-$1 symbol
   it becomes marketable and can fill; a fill is still a hard failure with best-effort cancel, but
   it leaves a 1-share paper position to flatten by hand). The drill requires terminal proof:
   `canceled`/`rejected`/`expired` with `filled_qty` exactly 0 — a bare cancel request is not ok.

## Step B — live quote data (2026-07-10: the $0 route is DEFAULT, paid Databento is the upgrade)

**Route B-0 (chosen 2026-07-10, $0): the Alpaca IEX feed** — the free market-data plan on the
step-A paper account. The adapter is built (`agent.marketdata.alpaca_feed`; provenance pinned
`ALPACA.IEX`/`mbp-1`, synthetic instrument ids, realism-gap numbers NOT comparable with the
Databento-based backtest caps — predeclared) and is UNVERIFIED-fail-closed until one verification
session during market hours:

```bash
PYTHONPATH=scripts .venv/bin/python3 -m agent.verify_alpaca_feed --symbols AAPL,MSFT --seconds 60
```

Green ⇒ flip the seam's `allow_unverified_live` default in a reviewed commit (one line, exists for
exactly this), review the printed quote samples for size-unit semantics, and read the report's
`statuses`/`lulds` counts — the same run measures whether the FREE feed carries the halt/LULD
channels the status plane needs. Live sessions then use `--live-source alpaca-iex`. Caveats: IEX ≈
2% of consolidated volume (NBBO approximation — fine for mega-caps, thin for small-caps); if the
drill shows no LULD coverage, opens stay fail-closed-blocked until the $99 Alpaca SIP upgrade
(full consolidated feed incl. LULD) or route B-1.

**Route B-1 (upgrade, paid): Databento live realtime `EQUS.MINI`** — full research↔live data
symmetry (same dataset as every backtest); worth buying when a strategy actually validates (can be
bought month-by-month for the edge-validation phase). Pinned live schema: **`bbo-1s`** (shares the
`BBOMsg` layout the 2026-06-26 credentialed historical pull verified; `tbbo` stays unverified).

1. Provision the live realtime subscription for `EQUS.MINI` on the Databento account; confirm the
   existing `.secrets/databento.json` key carries the live entitlement (or write the new key there).
2. Run the tier-2b verification session (one attended session, mirrors how the historical pull was
   verified):

   ```bash
   PYTHONPATH=scripts .venv/bin/python3 -m agent.paper_session \
     --journal-dir journal-verify --live --allow-unverified-live \
     --symbols AAPL --record-events reports/live_verify/events.jsonl
   ```

   during RTH, for a few minutes (Ctrl-C is safe: the kill/flatten path is not armed and nothing
   can open). Then verify the recording replays byte-consistently:

   ```bash
   PYTHONPATH=scripts python3 -m agent.paper_session \
     --journal-dir journal-verify-replay --replay reports/live_verify/events.jsonl --symbols AAPL
   ```

3. On success: flip the `allow_unverified_live` default in a reviewed commit (the flag exists
   precisely so this is a one-line, reviewable change), noting the verified record layout.

Fallback if B is deferred: the **observe phase can run on replay** (recorded historical data) and
every offline rehearsal works — but genuinely live observe/paper needs the subscription. There is
no sanctioned delayed-data paper mode (decisions on stale quotes would corrupt the realism
evidence, which is the whole point of the paper phase).

## Step C — a strategy with a passing reviewed artifact (S9 — the edge gate)

No paper-eligible strategy can open without a reviewed v2 artifact verifying `ok` for the exact
`(strategy_id, rules_hash, data_pin)` in `artifacts/backtests/` — enforced per-open at preflight
stage 5, not just by convention. Today `artifacts/backtests/` holds only `.gitkeep`.

- The active path is the **M7d C2 run** (`1m`/`120m` on the fresh 20-session holdout, complete
  ~2026-07-14): packet review (GPT) → Robin's separate go → the committed driver
  (`agent.m7_run_driver`) stages the run → on a (provisional) GO: the predeclared confirmation
  holdout → only then the production artifact write + paper entry. All four M7d operational
  prerequisites are DONE (calendar provider, cross-checked session fixture, committed driver, fix-A
  baseline).
- **Known adapter gap (only if the GO'ed strategy is the RS proxy):**
  `relative_strength.long_only_proxy_v1` is cross-sectional (`decide(snapshots, ...)`) and does not
  implement the per-symbol `scan(ctx)` Protocol the live loop drives. Before IT can paper-trade, a
  scan-adapter (per-tick cross-sectional assembly inside the orchestrator) must be predeclared,
  built and reviewed. `--strategy-id` refuses the id with exactly this message until then. The
  momentum strategies are wired (`directional.momentum_v1/v2`) but their family is nulled + closed.
- If M7d nulls per its stop rules, there is no paper phase to start — that is the predeclared
  outcome, not a runbook failure.

## Step D — the paper-phase config commit (Robin-reviewed, git-visible)

Committed caps are 0 (the second wall) and overlays are **tighten-only** — they can never raise a
cap. Arming paper therefore requires a REVIEWED COMMIT that sets the paper-phase risk envelope.
This is deliberate key-A-style discipline: the commit is code-reviewable and enables nothing by
itself (gates still require the runtime file in step E, opens still require the S9 artifact).

Suggested starting envelope (strategy notional is $1,000/leg — `PAPER_NOTIONAL_USD`):

```
config/risk_rules.json caps:
  max_position_usd:          2000
  max_gross_exposure_usd:   10000
  max_net_exposure_usd:     10000
  max_daily_loss_usd:         200
  max_drawdown_usd:           400
  max_sector_exposure_usd:   6000
  max_abs_beta_notional_usd 12000
risk.universe: sector/beta metadata for every traded symbol (else sector_unknown/beta_unknown rejects)
config/agent_rules.json universe.symbols: the predeclared universe
```

`live_trading.enabled` stays `false`. `agent_rules.enabled`/`paper_trading.enabled` STAY `false`
in the committed file — the runtime file is the run key. Note: this commit changes `rules_hash`,
so it must land BEFORE the artifact-producing run it will trade under (the artifact binds
`rules_hash`), or the artifact will correctly refuse to verify.

## Step E — arm the run gates (Robin, the last key)

```bash
cat > .secrets/run_gates.json <<'EOF'
{"enabled": true, "paper_trading": {"enabled": true}}
EOF
```

- Absent/malformed/non-identity-`true` ⇒ both gates read `false` (fail-closed).
- **Disarm at any time by deleting the file.** The next tick's opens reject at `run_gates`;
  held positions can still be reduced/flattened (reduce-only is never gate-blocked).

### Pre-flight drill (same day, before the first armed session)

1. Full suite green on the checkout: `python3 -m unittest discover -s tests -p 'test_*.py' -t .`
2. S1 canary in situ: run one session UNARMED (no `run_gates.json`) with the strategy flag —
   `submit_calls` must be zero and every open must reject at `run_gates` (check the daily report's
   reject reasons).
3. Kill drill on the armed composition (paper account, flat book): trigger
   `orch.trigger_kill("drill")` via a python one-liner against a scratch journal dir, or rely on
   the suite's kill-flatten E2E; verify the journal shows `flattening → halted` and (with a held
   fixture position) a reduce-only sell.
4. OPEN pre-arming item (F2, 2026-07-10 review): the kill-flatten client id is
   `flatten-<symbol>` — deterministic but NOT episode-scoped. Across session days on the same
   Alpaca paper account, a retained prior `flatten-AAPL` order can make a fresh session's
   residual-retry probe read a STALE terminal fill as "already flattened" (or bounce a first
   submit on a duplicate client id). Before the first ARMED multi-session week, scope the id per
   kill episode (e.g. `flatten-<kill_generation>-<session_date_et>-<symbol>`, rehydrate-stable
   within an episode) end-to-end through the M0 actuator, the probe, `confirm_residual_flat`,
   and `_book_flatten_closes` — a TDD pass of its own, not an edit-and-run.

## Daily operation (once A–E hold)

One command per session day — either the supervisor (recommended for unattended days):

```bash
PYTHONPATH=scripts .venv/bin/python3 -m agent.paper_autorun \
  --journal-dir journal --symbols AAPL,MSFT,... --strategy-id <GOED_STRATEGY>
```

(bounded retry ONLY on a truncated feed; append-only `autorun_log.jsonl`; loud
`ATTENTION-<date>.txt` on any unclean day; launchd template:
`docs/runbooks/com.stocks-agent.paper-autorun.plist.template`) — or the runner directly:

```bash
PYTHONPATH=scripts .venv/bin/python3 -m agent.paper_session \
  --journal-dir journal --live --symbols AAPL,MSFT,... --strategy-id <GOED_STRATEGY> \
  --record-events data/live/$(date +%F).events.jsonl
```

**Follow it live** (read-only, loopback-only; works during the session AND afterwards):

```bash
PYTHONPATH=scripts python3 -m dashboard --journal-dir journal
# → http://127.0.0.1:8788/ — decisions, orders, fills, positions,
#   Broker-vs-Modeled PnL (never conflated), kill state, status plane, reports
```

**Weekly**, aggregate the evidence against the pinned criteria (missing evidence stays
missing — never silently zero):

```bash
PYTHONPATH=scripts python3 -m agent.paper_phase_report \
  --report-dir reports/paper_sessions --journal-dir journal \
  --allocated-notional-usd <ROBINS_ALLOKERING>
```

What it does autonomously: skips non-trading days (calendar fixture `xnys_sessions_2026H2_v1`,
cross-checked; regenerate with `agent.calendar_fixture` for 2027); SOD broker reconcile; the
10-step tick loop (probe always journals; strategy opens only through preflight + S9 + risk
ladder; marks; exits); at the RTH close: cancel-open-orders, margin close-of-day, EOD reconcile
(in-loop, with an idempotent fallback); daily report to `reports/paper_sessions/<date>.json`;
records the live stream so every session is replayable.

Exit codes: `0` clean · `1` unclean (reconcile drift, a TRUNCATED live feed —
`source_exhausted_early`, the day is not full-session evidence — or a mid-session crash; a
crashed session still best-effort writes its daily report with `session_incomplete: true`) ·
`2` run lock held/usage · `3` journal corruption (startup OR mid-session) · `4` kill-switch
HALTED (operator attention required; re-arm = a deliberate NEW run after review) · `5` calendar
fixture coverage expired (regenerate with `agent.calendar_fixture` + review — a silent 0 here
would end the paper program unnoticed).

Lock hygiene: each journal tree holds a `.lock` (owner PID) and a persistent `.lock.guard`
sidecar. **Never delete `.lock.guard` by hand** — recreating it gives concurrent processes a
new inode to flock and silently breaks the one-session-per-journal-tree serialization. A stale
`.lock` from a dead PID is reclaimed automatically; only remove `.lock` manually if it is
malformed AND the owning PID is confirmed dead.

Bounded-blindness note: if broker account reads go non-fresh for >120s while positions are held,
the agent flattens-then-halts on its own (`account_blind_cap`) — expect exit 4 + a journaled
cause, not a silently blind session.

Weekly review (Robin): the daily reports + `evaluate_paper_phase_criteria` against the pinned
matrix in `m7-paper-edge-validation.md` (≥20 sessions, ≥30 trades, PF ≥ 1.10, realism caps, zero
safety breaches). That matrix — not vibes — decides whether the paper phase is producing the
realized-edge evidence M8 would need.

## Standing hard boundaries (unchanged by this runbook)

- `live_trading.enabled` stays `false`; live needs two-key arming + the M8 checklist.
- The broker is position-of-record; the journal reconciles against it, never overwrites it.
- No committed secrets; `.secrets/` only. Tests stay offline (no network, no creds).
- Threshold/criteria changes, universe changes, and any strategy change require a predeclared
  packet + review, not an edit-and-run.
