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
was broad (10 symbols traded, max 12.3% of legs / 16.8% of net-positive PnL — no concentration breach), so the
null is broad rather than a single-name artifact. This is **one tested configuration (L1 1-minute BBO, this 30-bar
horizon, this 10-name universe, long-only proxy) nulled clean; it does NOT causally isolate the substrate from the
horizon, universe, or strategy shape.** Per the predeclared Phase Gate + the two-family search-budget stop rule
(momentum = family 1 nulled; relative-strength = family 2 now nulled clean), the NO-GO/STOP and the routing toward
a substrate experiment follow from the rule, not from a proof that the substrate is the binding constraint — and it
is NOT the phase-2 short-side build and NOT a third same-substrate family.
**GPT adversarial review of the NULL/STOP returned 2026-06-26 and was independently re-verified (5-agent read-only
workflow + direct read): STOP HOLDS (no false-null — GPT's own RTH-leak falsification still failed hard). All four
findings confirmed; none blocked the offline merge. The four fixes are now APPLIED + TDD-tested (1773 tests green):
(B) the cross-sectional writer now also gates positive active PnL vs the `universe_equal_weight_long_v1` benchmark
(the packet requires BOTH benchmarks); (A) the RTH filter AND the manifest validator now require the `ts_event`
bucketing key — not just `ts_recv` — to fall inside the pinned session window (stale pre-open books dropped);
(C) the causal wording above softened; (D) credentialed manifests must now pin `sessions` from the market calendar
(`pin_sessions_from_provider`, half-day/DST-aware) and the regular-session deriver is offline-fixture-only behind
`allow_derived_sessions`.**
**Robin's call (2026-06-26): restart toward the sanctioned SUBSTRATE step = a longer/coarser decision/holding
horizon on the same L1 data (cheapest, reuses the harness, attacks both the edge and the realism-cap failures) —
NOT L2/MBP-10 (heaviest build) and NOT wider universe first.**
**M7d predeclaration GPT-reviewed rev 3 (rev 2 committed `013f9b6`, rev 3 applied 2026-07-02 after GPT review; suite
1846 tests green):** packet `docs/superpowers/specs/2026-06-26-M7d-longer-horizon-research-packet.md` + GPT handoff
`docs/superpowers/reviews/2026-06-26-M7d-predeclaration-gpt-review-handoff.md` — C2 = `1m`/`120m` sole decider on a
fresh fixed-20-session post-2026-06-13 holdout (complete ~2026-07-14); C1 = `1m`/`60m` descriptive on the snooped
window; pinned criteria unchanged; both benchmarks gated. STATUS = GPT-reviewed rev 3, not run-authorized; the
credentialed pull/run stays gated on Robin choosing M7d rather than routing straight to a realism-matched lever,
Robin's separate go, and the holdout. The 2026-07-02 full-project review (4
read-only deep-review agents + own-run reproduction; safety checklist 12/12 PASS in code; one pre-live MINOR for
the M8 checklist: the loss/drawdown auto-kill is SKIPPED on a non-"fresh" account read) applied three fixes TDD:
the cross-sectional runner now requires the DECISION bar to be FD-2-eligible at decision time (late-receipt bars
excluded from the ranked set as `decision_bar_*` exclusions — ranked set == tradable set); both reviewed-artifact
writers fail closed unless `rules_hash` == the config-DERIVED hash (the M7d horizon config edit changes it — an
expected, documented delta, not an `artifact_mismatch`); and `build_cross_sectional_input_manifest` requires an
explicit `horizon` (silent `30m` default removed). The review also measured what the packet had only hedged: the
staged clean-rs-v1 quotes predate fix A and no longer validate at HEAD — the fix-A-compliant baseline (manifest
`90866e90…`, rebuilt with the committed tools) is **1147 trades / net −$858.01 / active −$111.51 / p95 29.949 /
max 97.484** (NULL robust under the current contract; use THIS baseline for the C2 A/B) — and the
horizon-invariant entry-leg realism floor is **p95 ≈ 14.13 bps in the 120m-survivor population (94% of the 15-bps
cap; survivor combined gap p95 31.67)**, so a C2 GO is structurally improbable on the realism caps and the run's
decision value is the route-A-vs-B EDGE read. GPT review then forced rev 3: remove the undefined "comfortable
margin" gate, make route-B mechanism-classification only (no automatic second substrate spend), and reconcile
current HEAD prerequisites. A substrate-search budget is pinned in `PLAN.md` (M7d = substrate experiment 1 of at
most 2 on this family line; a second substrate null → documented program STOP absent a fresh mandate). Pre-run
operational prerequisites from the packet (calendar fixture/provider + committed pull→build→write driver) are now
closed by the paper-operational readiness build, but the credentialed M7d run remains gated as above.
No reviewed artifact verifies `ok`; production `artifacts/backtests/` still contains only `.gitkeep`; the staged run +
quotes live under gitignored `reports/m7_historical_runs/2026-03-10-clean-rs-v1/` (rows reproducible from the
committed backfill tool; NOTE: the staged manifest predates fix A and no longer validates at HEAD — the
fix-A-compliant rebuild `90866e90…` is the live baseline). Paper edge-validation + M8/live remain blocked. **Merge-to-main DONE 2026-06-26:
`codex/m7-backtest-gate` was fast-forward-merged into `main` (now `19786cf` = M7 tip; was M2 `a82be6d`; 49 commits,
0 conflicts, all safety gates verified closed first — `live_trading`/`agent_rules.enabled`/`paper_trading` all
`false`, `artifacts/backtests/` = `.gitkeep`). No remote, so locally reversible.**
**PAPER-OPERATIONAL READINESS BUILD DONE 2026-07-02 (on `main`, 1846 tests green; independently reviewed, all findings applied):** every buildable gap between
"spine done" and "an autonomous paper session runs" is closed, gates untouched. Built + TDD'ed: the
`account_blind_cap` bounded-blindness kill (the review MINOR — >120s non-fresh account reads with held positions
⇒ flatten-then-halt); the IMPLEMENTED `ExchangeCalendarsScheduleProvider` (lazy, version-pinned fail-closed) +
`agent.calendar_fixture` generator + the committed cross-checked `xnys_sessions_2026H2_v1.json` (holdout = 20
sessions ending 2026-07-14; the cross-check caught a real 2026-07-02 half-day error in the old margin fixture);
the committed M7d driver `agent.m7_run_driver` (packet §9+§10 prerequisites now BOTH done; staged-only, rules_hash
derived, full per-gate summary); the M1 tier-2b live seam `LiveQuoteFeed` + `databento_live_source` (bbo-1s
pinned, UNVERIFIED-fail-closed until the paid subscription; record→replay byte round-trip pinned; strict in-order
no-lookahead bar firing with vendor-skew guards); the day-runner `agent.paper_session` (SOD → tick loop →
idempotent EOD → daily report; exit codes; strategy registry — momentum wired, the cross-sectional RS proxy
REFUSED pending a predeclared scan-adapter) + `agent.paper_report`; the credentialed `agent.verify_alpaca_paper`
(+`alpaca-py==0.43.5` pinned; FD-M5-5 wall extended to exactly two `_build_real_client` sites); and the capstone
runbook `docs/runbooks/paper-go-live-checklist.md`. Replay rehearsal VERIFIED end-to-end (observe fixture: 74
ticks, report, exit 0, zero orders). **Remaining before autonomous paper = the runbook's five Robin-gated steps:
(A) Alpaca paper account + verifier, (B) the PAID Databento live realtime subscription + one-session tier-2b
verify, (C) an S9-passing reviewed artifact (M7d path; + the RS scan-adapter IF that family GOes), (D) the
reviewed paper-phase caps commit (overlays are tighten-only), (E) runtime arming `.secrets/run_gates.json`. No
unbuilt code stands in the path.**
**OPTIMIZATION PASS PARTIAL 2026-07-08 (on `main`, suite now 1860 tests green):** the 2026-07-07 full-project
optimization session (goal: optimize everything + finish the fully automated paper agent) died mid-run on API
errors; the 4 finished tasks were committed 2026-07-08 as `62f9557` (perf: `IncrementalJournalReader` — the tick
loop's per-bar-batch decisions read is now incremental+hash-verified instead of an O(day²) full re-read; the
scored-stream replay runs once per resolver instance; `eligible_history` served from a construction-time bisect
index — the M7d C2 dominant CPU term) + `7cfddbd` (paper ops: live-feed `source_exhausted_early` counting +
fired-bucket row pruning so bbo-1s memory/resample stays bounded; `paper_session` feed-truncation ⇒
`feed_truncated` + exit 1, and CLI exit codes 2=lock held / 3=journal corruption / 5=calendar coverage expired —
was a silent 0). **PAUSED by Robin; 5 tasks remain** (tracked in the memory backlog `optimization-backlog.md`):
`paper_report` completeness/restart-naming/`modeled_null_closes`/ET-date fixes; `agent.paper_autorun` (daily
scheduler wrapper + launchd template); `agent.paper_phase_report` (weekly criteria aggregator); safety fixes
F3–F6 (replay credential-inert, lock-reclaim race, blind-cap broker gate, drill hardening); step-D caps proposal
+ docs sync (runbook exit codes; `.gitignore`). Do not resume these without Robin's go.
**PAPER-LIVE-LOOP WAVE DONE 2026-07-10 (Robin's /goal: "vi venter ikke på M7d — live visning + i gang nu"; on
`main`, suite 1988 tests green, gates untouched, S1 canary green):** (1) the 2026-07-09 GPT correctness wave
(3 divergerende branches + 628 ucommittede linjer) was reviewed (7 agenter; safe-to-merge; task5's F5-version
BUGGY og droppet) and CONSOLIDATED to main — terminal-fill flatten gating, owner-safe run lock, replay
credential-inertness, semantic artifact re-verify, drill terminal evidence, journal integrity; (2) the two
remaining verifier bypasses CLOSED (v1 rejected outright; equal-weight second benchmark now re-checked at
verify — both were empirically forged pre-fix); (3) feed exceptions now write an `session_incomplete` daily
report + honest exit codes (mid-session JournalCorruption ⇒ 3); (4) journal `read_new()` delta API kills the
O(day²) deepcopy (tick loop accumulates deltas); (5) `paper_report`: session block, `modeled_null_closes`,
ET-date fallback, restart-safe naming (`<date>.json`, `<date>.1.json`, …); (6) **Track B status data plane
BUILT + WIRED (`agent.marketdata.status_plane`)** — closes P0-1: fail-closed halt/LULD/SSR provider (stream
stale/disconnect ⇒ UNKNOWN ⇒ opens blocked; reconnect ⇒ re-observation epoch; most-restrictive conflicts;
transitions journaled to `status_plane.jsonl`); the Alpaca `statuses`/`lulds` source is UNVERIFIED-fail-closed
until the Track D drill, so live composes DISCONNECTED (runs, journals, cannot open) with a loud ATTENTION note;
replay composes the rehearsal RTH-windows provider (nothing can open in replay by construction); (7) live-feed
**bounded reconnect with epoch bump** (`ReconnectingQuoteSource`) closes P0-3b — heartbeat-sliced backoff,
give-up ⇒ the existing truncation exit-1 path, stats folded into data-quality; (8) **`agent.paper_autorun`**
(in-process daily supervisor: append-only `autorun_log.jsonl`, retry ONLY truncation-exit-1, ATTENTION-file
escalation, launchd template in `docs/runbooks/`) + **`agent.paper_phase_report`** (weekly PINNED-criteria
aggregator; missing evidence — incl. the paper benchmark leg — stays `missing:`, never zero); (9) **the local
live view `python3 -m dashboard --journal-dir journal`** (stdlib, loopback-only, read-only, hash-verified
incremental reads): decisions/orders/fills/positions, BrokerUSD vs ModeledUSD split, kill state, status plane,
reports — verified END-TO-END against a replay-observe session (74 ticks, exit 0, zero orders) and a synthetic
session, including cross-process live updates while a session runs (demo artifacts under gitignored
`reports/demo_live_view/`). F2 (episode-scoped flatten client id) is a documented pre-arming runbook item.
Remaining before ARMED autonomous paper: runbook steps A–E (Robins eksterne trin) + Track D drills + the S9
decision — see `docs/superpowers/specs/2026-07-10-paper-canary-decision-memo.md` for the default-OFF proposal
awaiting Robin's explicit choice.
**SAME DAY (2026-07-10 formiddag): STEP A DONE** — Robin created the Alpaca paper account;
`agent.verify_alpaca_paper` green (account ACTIVE …REDACTED, $100k paper equity) AND the order drill green
(submitted→canceled, terminal_verified, filled 0); evidence `reports/alpaca_paper/verified_account.json`;
credentials in `.secrets/alpaca_paper.json` (600, git-ignored). **STEP B rerouted to $0 (Robins call — no $199
Databento while nothing is validated):** `agent.marketdata.alpaca_feed` built (`5925ed3`, suite 1997) — the free
IEX feed behind the same seam (`--live-source alpaca-iex`, provenance ALPACA.IEX/mbp-1, synthetic instrument
ids, FD-M5-5 wall consciously widened to a third `_build_real_client` site, realism-comparability break
predeclared), UNVERIFIED-fail-closed until `agent.verify_alpaca_feed` runs during RTH (≈15:30 DK) — that one
run pins the payload layout + record→replay round-trip AND measures statuses/lulds coverage on the free feed
(the Track B/D question). Green ⇒ flip the flag in a reviewed commit ⇒ first live observe-session via
`--live-source alpaca-iex` + dashboard. Databento $199 = deferred upgrade (buy month-by-month IF a strategy
validates). Strategic two-track note: intraday-sporet er drift/ops (edge-spørgsmålet afgøres af M7d/stop-reglen);
et LANGSIGTET fundamentals-spor (dagskadence, PIT-regnskabsdata, LLM-research, benchmark S&P500 TR) blev
anbefalet som ny forskningslinje — kræver Robins "fresh explicit mandate" per PLAN.md's substratregel + eget
predeclared packet. Stale worktrees (`autonomous-paper-hardening`, `task5-terminal-drill`,
`task6-incremental-replay` under `~/.config/superpowers/worktrees/Stocks/`) er superseded af main og kan
fjernes med `git worktree remove`.

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

M7 is stacked on `m6-reconcile` on branch `codex/m7-backtest-gate` (merged to `main` 2026-06-26). The **offline
acceptance suite** (currently 1860 tests) remains stdlib-only for normal development — no install needed to run
it on a bare checkout:

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
