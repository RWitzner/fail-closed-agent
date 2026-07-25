# M6 (Reconcile hardening — SOD/EOD broker reconcile) — CONTRACT rev 6 — READY-TO-BUILD

**Status: rev 6 — READY-TO-BUILD.** Rev 1 synthesized 2026-06-10 from three independent
architect drafts (lens A: reconciliation correctness; lens B: fail-closed safety; lens C: integration
& runnability). Rev 2 (same day) applies the 5-lens critic pass: 48 raw findings deduplicated to 34
canonical (M6C-1…M6C-34), ALL applied, none rejected. Rev 3 applies the round-2 re-critique:
5 findings (RC-1…RC-5 — 2 major, 3 minor), ALL applied, none rejected. Rev 4 (2026-06-11) applies
the round-3 re-critique: 6 findings (RC-6…RC-11 — 4 major, 2 minor), ALL applied, none rejected.
Rev 5 (2026-06-11) applies the round-4 re-critique: 2 findings (RC-12…RC-13 — 1 major, 1 minor),
ALL applied, none rejected. Rev 6 (2026-06-11) applies the round-5 re-critique: **1 finding (RC-14 —
minor type-shape pin on `outstanding_cash_residue`), applied** — the round-5 re-critique otherwise
verified ALL 47 prior resolutions present and completely swept (`unverified=[]`) and confirmed the
cash-latch re-journal blocks BOTH clear paths and the PROBE_FAILED deferral blocks every adjust
path. All dispositions in §V (rev-1 log, V.2, V.3, V.4, V.5)
and in `docs/superpowers/reviews/2026-06-10-M6-contract-critic-findings.json`.
Disagreements between the lenses are resolved explicitly in §1; every load-bearing repo fact in §0.1
was re-verified against the source at the baseline commit by the synthesis pass itself.

Baseline: branch `m6-reconcile` @ `10532df`, 1520 tests green, gates OFF, committed caps 0.
House style mirrors the M5 frozen contract (`docs/superpowers/specs/2026-06-10-M5-paper-exec-contract.md`).

---

## 0. Scope, ground rules, verified facts

**M6 is reconcile hardening, not a feature milestone** (locked 2026-06-10 "Edge before live": spine
construction ends with M6/M7; bias LEAN). It delivers exactly one new capability — a deterministic
SOD/EOD/immediate broker-reconcile pass that:

- (a) replays local journal state and reads broker ground truth through the existing parse chokepoints,
- (b) diffs them under a frozen comparison policy (§2 FD-M6-4),
- (c) journals every divergence as an explicit `reconcile` row on a NEW stream
  `journal/reconcile_alerts.jsonl` (the spec-named stream, design spec :284/:445),
- (d) repairs the local book ONLY via journaled, fold-honored `position_adjust` rows where the broker
  wins (never a silent mutation; never a fabricated position),
- (e) latches drift fail-closed into `PortfolioRead.unreconciled_drift` (consumed by `can_open` as
  `portfolio_unreconciled` — already vocab-legal) until a completed clean pass,
- (f) maps drift to a non-zero process exit at the CLI (S5),
- (g) closes the M2 IOU: the `BrokerAdjustDetector` SOD baseline-seeding sequence + same-tick immediate
  reconcile on a `FreezeSignal` (spec :171-179), behind an injected `durable_ids` seam (inert in
  production until a security-master source exists).

**Ground rules (binding, inherited from M0–M5; restated where M6-load-bearing):**

- Committed gates stay OFF; `live_trading.enabled=false`; two-key arming untouched; M6 flips no gate
  and **adds no committed config key** (FD-M6-5) — `rules_hash` is byte-identical to M5's on every
  stream, so every committed golden stays valid un-regenerated (§1 row 16).
- The reconcile **pass** is **read-only toward the broker**: `account()` / `positions()` /
  `order_status(client_order_id)` only (all tokenless, broker/base.py:80-86). It mints no tokens,
  constructs no `OrderIntent`, submits nothing, **cancels nothing**. *(Scope: the PASS. The
  `_cmd_reconcile` **composition** inherits M5's startup step-10 dangling-order recovery, which may
  issue one journaled best-effort `restart_unknown_state` cancel on an adopted dangling order —
  reduce-direction-safe per FD-M5-23, owned explicitly in FD-M6-22 and pinned in §I file 5 —
  M6C-6.)* It is an observation/accounting job and runs with all run gates OFF — it is never gated by `opening_allowed` (the M5C-S3 posture:
  gates govern OPENS only; broker-touching order recovery already runs gates-OFF at
  orchestrator.py:626-632).
- Broker is position-of-record: on conflict the broker wins via an explicit journaled adjustment row;
  the journal is append-only and never edited; modeled fills never override the broker ledger (S5);
  `BrokerUSD` vs `ModeledUSD` never conflate.
- Non-zero exit on drift (S5): the standalone `agent reconcile` job exits **1** on drift found (even if
  fully adjusted — adjustment is remediation, not absolution); `agent paper` maps a SOD-pass drift or a
  rehydrated latch to exit 1. In-process passes never exit the process; they latch + journal.
- Offline suite stays stdlib-only / no network / no creds; injected clocks only — no M6 module reads a
  wall clock (the only wall reads stay in `__main__.py`, the `mint_run_id` precedent,
  orchestrator.py:213 / __main__.py:92-94).
- Determinism: canonical `dumps()`, Decimal-as-string, per-stream monotonic `seq`, row hash, `"v"`
  first payload key + `rules_hash` last on every ledger row, reserved-key set respected
  (journal.py:21). All market logic ET; persisted timestamps UTC.
- The reduce path, cancels, and kill-flatten gain **zero** new gates from anything in this contract
  (FD-M4-3 preserved).

### 0.1 Verified repo facts this contract builds on

All at `10532df`; **every row below was re-read from the source by the synthesis pass** (architect
claims that did not verify were dropped or corrected — see §1 rows 4, 12, 16 and the note below this
table).

| # | Fact | Where |
|---|------|-------|
| V1 | `PortfolioRead.unreconciled_drift: bool = False` — "M6 sets True on a KNOWN reconcile mismatch"; frozen dataclass (mutate via `dataclasses.replace`); `qty_for(symbol)` returns `Decimal("0")` for absent symbols (flat-is-absent) | `scripts/agent/risk/account_state.py:252-271` (field :258, qty_for :267-271) |
| V2 | `parse_positions_payload` has **no** drift kwarg (constructs default-False); `qty==0` rows silently dropped; malformed row → `ValueError`; `ACCOUNT_SOURCES = {alpaca_paper, alpaca_live, fixture, spy}` | `account_state.py:274-318` (drop :301-302); vocab :27 |
| V3 | `can_open` phase-1: `portfolio.unreconciled_drift is True` → terminal reason `"portfolio_unreconciled"`, stage `"portfolio"`, sorted with `portfolio_stale`; the kill rung precedes the portfolio rung in the frozen phase-1 order; `portfolio_unreconciled` already in `TERMINAL_REASONS` | `scripts/agent/risk/can_open.py:124-142` (drift :139-140); `scripts/agent/risk/reasons.py:16-19` |
| V4 | `rehydrate_exec_state`: an order leaves `open_orders` only on `_ORDER_CLOSING_EVENTS = {order_terminal, broker_reject, reject}`; `order_submit_unconfirmed` stays presumed-live (FD-M5-17); **any orders-row carrying an `order_id` becomes that order's "latest row"** | `scripts/agent/exec_ledger.py:1055-1107` (closing events :192-193, latest-row :1081-1088) |
| V5 | `PaperBook.rehydrate` **raises `ExecError` on any out-of-vocab positions-stream event_type**; sell fills never fold; buy fills cum-watermark-gated; `position_close` residuals verified by exact Decimal VALUE equality (`!=` on Decimals — value, not string/byte, compare; raise on mismatch — M6C-20); non-`broker_fill` fills-stream rows skipped; mark/pnl never fold | `scripts/agent/paper_book.py:618-709` (vocab raise :625-627, watermark :646-649, residual check :675-685) |
| V6 | Kill-flatten join exception: broker-side `client_order_id = "flatten-<symbol>"`, ledger-side ids separately minted (`o-` prefixed) — "M6's reconcile treats the kill actuator as the documented exception to FD-M5-7"; flatten booking is best-effort "(M6 reconciles)"; late fill on a cancelled close is "the M6 reconcile's job"; `close_superseded_position_closed` alert: "M6 folds any residual fill" | `scripts/agent/orchestrator.py:40-46, 1019-1021, 1076-1082, 1561-1572` |
| V7 | Startup step 10 `_recover_dangling_orders` runs iff `mode=="paper" and broker is not None`, gates-OFF included; step 6 exec rehydrate is pinned PURE (no broker call, M5C-S2); step 4 builds per-stream `EventWriter(path, run_id, **writer_kwargs)` with injectable `row_clock` | `orchestrator.py:626-632` (step 10), `:502-532` (step 6), `:450-479` (step 4) |
| V8 | `_refresh_account` rebuilds `self._portfolio` from scratch every refresh (`stale=False`, drift default False — a flag set on the instance is LOST at the next refresh); `_portfolio_read` re-stamps only `stale` via `replace()`; the positions branch catches **only `ValueError`** (a `BrokerHttpError` instance returned as data would NOT be caught — known trap) | `orchestrator.py:889-932` (rebuild :919-925, replace :927-932) |
| V9 | `_session_edge` (tick step 10): leaving-RTH edge over the recorded instant (`self._instant_utc`, never wall); best-effort cancel (`session_end`); once-per-`session_date_et` `close_of_day` guarded by `_closed_sessions`; **early return when `latest_unsafe()` is None** (the margin block can be starved on a truncated run) | `orchestrator.py:1912-1936` (early return :1930-1932) |
| V10 | `BrokerAdjustDetector`: `seed_baseline` explicit-only, Decimal-only; `observe_broker_qty` with no seeded baseline **RAISES** (S7-2 — never silently seeds); any qty≠baseline → `FreezeSignal(immediate_reconcile=True)` + frozen; blackout state only LABELS the `FreezeReason`; docstring: "The precise SOD seeding sequence lands in M6 reconcile (§M)" | `scripts/agent/corporate_actions.py:489-553` (raise :529-533, docstring :497-501, signal :542-549) |
| V11 | `record_order_terminal(*, terminal_state, filled_qty, cum_notional_usd, ts_broker_utc, decision_id, order_id)` and `record_order_state_alert(*, broker_order_id, raw_status, note, order_id)` exist with these exact signatures; both require `o-`/`synthetic-o-`-prefixed `order_id` | `exec_ledger.py:704-721, 690-702` (prefixes :178) |
| V12 | Journal reserved keys `{event_type, run_id, seq, hash, decision_id, order_id, ts_utc}` refuse payload collision; replay drops ONLY an unterminated tail, a newline-terminated bad line is fatal `JournalCorruption`; per-stream **path-keyed** seq+lock registry (multiple writers on one resolved path share one seq) | `scripts/agent/journal.py:21, 28-59, 70-81, 110-114` |
| V13 | M1 precedent `ReconcileReport(matched, mismatches, missing_in_recorded, missing_in_reference, ok)` — fail-closed `ok`, never mutates either side, "a CLI maps not-ok to a non-zero exit" | `scripts/recorder/reconcile.py:27-33` (docstring :1-20) |
| V14 | Spec pins: SOD/EOD job "replay local state, diff against Alpaca positions/account as ground truth, write status + alerts, non-zero exit on mismatch; on conflict the broker is truth and a `reconcile` adjustment row is emitted (never a silent mutation)" (:285-287); row shape `reconcile — symbol, local, broker, diff, action` (:347); S5 (:389); M6 milestone row (:406); `reconcile_alerts.jsonl` in the journal tree (:284, :445); CA-blackout detector "freezes the symbol and forces an **immediate (not EOD)** reconcile" (:177-178); `scripts/recorder/.../reconcile_runner` is the M1 depth-sense layout slot (:444) | `docs/superpowers/specs/2026-06-08-stocks-agent-design.md` |
| V15 | CLI: argparse subcommand pattern; `_cmd_paper` = committed config + tighten-only `--overlay` + `.secrets/alpaca_paper.json` + `.secrets/run_gates.json` + `_ZeroClock`/`_LatestQuoteView`, startup-only-then-exit, **exit 2 on mode mismatch**, `orch.close()` in finally; `__main__` import budget = orchestrator/config/secrets_runtime + stdlib; wall reads CLI-only (`_new_run_id`) | `scripts/agent/__main__.py:1-45, 92-94, 196-258` |
| V16 | `EXEC_LEDGER_VERSION = 1`; every ledger row is `{"v": VERSION, **body, "rules_hash": ...}`, validated kwarg-only BEFORE any write (raise leaves the stream untouched) | `exec_ledger.py:140, 446-468` |
| V17 | `Broker` Protocol reads are tokenless (`cancel_order`/`order_status`/`positions`/`account`); **no `list_open_orders` on the Protocol or on `FakeBroker`** — it exists only on the §F `OrderApi` Protocol and `_RealOrderApi` | `scripts/agent/broker/base.py:73-86`; `scripts/agent/broker/order_state.py:312-327`; `scripts/agent/broker/alpaca.py:236` |
| V18 | `FakeBroker.order_status` **advances at most one pending scripted fill slice per call** and raises `KeyError` on an unknown client_order_id ("composition bug → loud"); `FakeBroker.account()/positions()` derive from its own fill book (cost stands in for market value; equity = cash + Σ cost) — the fake cannot present drift | `scripts/agent/broker/fake.py:136-149, 164-200` |
| V19 | `tighten_only_merge` iterates **base keys only** — overlay-only keys are silently ignored (no new keys via overlay) | `scripts/agent/config.py:24-33` (docstring :3-5) |
| V20 | Polarity discipline: inverted-polarity knobs are CODE CONSTANTS (`LATENCY_BUDGET_MIN_MS` precedent); `SUBMIT_RECOVERY_ATTEMPTS = 3` (FD-M5-17) | `scripts/agent/execution_config.py:13-46` |
| V21 | `RiskKillSwitch`: `residual_symbols()` accessor; `_residual` latch rehydrates across restarts; `retry_residual` is HALTED-only + operator-attended | `scripts/agent/risk/risk_kill.py:175-182, 209-210, 214-231` |
| V22 | `RunLock` at `<journal_dir>/.lock`: `O_CREAT\|O_EXCL`; live pid ⇒ `RunLockHeld`; malformed ⇒ `RunLockHeld` (fail-closed); dead-pid reclaim reported via `acquire()` return + `self.reclaimed`; orchestrator acquires at startup step 1 and journals `run_lock_reclaimed` | `scripts/agent/run_lock.py:33-115`; `orchestrator.py:475-477` |
| V23 | Alpaca paper is fee-free broker-side (M5 A6); synthetic fee assumptions enter ONLY `execution_realistic_pnl` — "injecting synthetic fees into the broker side would manufacture permanent M6 reconcile drift" | `scripts/agent/fees.py:22-25` |
| V24 | `StatusLedger.record_broker_adjust_freeze(*, freeze_signal, instrument_id, ts_market_utc)` journals `prev_qty/curr_qty/immediate_reconcile/reason` to status.jsonl; the orchestrator today writes status rows via raw `_status_row` (no `StatusLedger` instance); `rehydrate_state` folds `broker_adjust_freeze` by durable_key | `scripts/agent/status_ledger.py:69, 304-326, 356-371`; `orchestrator.py:636-640` |
| V25 | Test seams: `FakeAccountProvider` scripts successive account/positions payloads (last repeats); `ExecPipeline` exposes `broker/account_provider/run_gates/credentials_path/row_clock` ctor seams; `ScriptedOrderApi` scripts `get_account`/`list_positions`/`get_by_client_order_id` (per-method FIFO; unscripted method ⇒ loud AssertionError); `AlpacaPaperBroker(order_api=...)` is the offline wire seam | `tests/lib/risk_fixtures.py:36-55`; `tests/lib/exec_fixtures.py:450-530`; `tests/lib/alpaca_fixtures.py:110-160`; `scripts/agent/broker/alpaca.py:96-107` |
| V26 | Prior-run journal seeding precedent: `_seed_dangling_order` builds a separate `ExecLedger` with `run_id="run-prior"` + fixed `_ROW_CLOCK` BEFORE constructing the orchestrator under test | `tests/agent/test_orchestrator.py:137-163` |
| V27 | The synthetic byte-golden compares **only** `("orders", "fills", "positions")` stream files — a new stream file participates in NO existing byte compare. (Risk-stream byte determinism is pinned only at the ledger level — `test_risk_ledger.py:301` compares replayed row lists, not e2e stream files — M6C-19.) | `tests/agent/test_synthetic_e2e.py:40, 65-78` |
| V28 | `SpyBroker.calls` records SUBMIT attempts only (cancel/status land in their own lists; `account()/positions()` reads are unrecorded and return `{}`) | `tests/lib/fakes.py:97-134` |
| V29 | `MarketCalendar.session_date_for` is total (pure ET-date arithmetic, no fixture consult, never raises); `phase_at` half-open windows ([rth_open, rth_close) is RTH), weekend → CLOSED structurally; `SessionSchedule.rth_close_utc` fixture-supplied, 13:00 ET on half-days, DST-correct | `scripts/agent/market_calendar.py:60-76, 268-273, 285-304` |
| V30 | `JournalWriter`/`EventWriter` construction does NOT create the stream file (tail-repair and replay both early-return on a missing path; the file appears on first append) — constructing an unused `ReconcileLedger` leaves no artifact | `journal.py:99-108, 28-32`; `scripts/recorder/persistence.py:52-100` |
| V31 | `_provider_payloads()` precedence: injected `account_provider` over broker; `_ACCOUNT_SOURCE_BY_KIND = {alpaca_paper→alpaca_paper, fake→fixture, else→spy}` | `orchestrator.py:881-887, 192` |
| V32 | M5 §T pins M6's bucket verbatim; FD-M5-7 pins `client_order_id = order_id` as "the M6 reconcile join key"; FD-M5-17 write-ahead + presumed-live; FD-M5-18 exact integrated notional (cum-watermark fills); FD-M5-21/22 defer retry/concurrency loosening to M6/M7 | M5 contract `:156, :166-171, :1871-1884` |
| V33 | `CLOSE_REASONS` already contains `"operator"`; `TERMINAL_STATES = {filled, canceled, expired, rejected, done_for_day}`; `ORDER_STATES` includes `unknown`/`pending_cancel` (non-terminal) | `scripts/agent/exec_reasons.py:79-91` |

**Dropped / corrected architect claims (verification deltas):** Draft A's "goldens are regenerated
once" is wrong (V27 — the new stream participates in no existing compare; see §1 row 16). Draft B's
"`_cmd_paper` returns 2 on mode mismatch" is correct; `RunLockHeld` has NO existing CLI mapping
today (uncaught in `__main__.py` — an unhandled traceback), so rev 2 pins `RunLockHeld ⇒ 2` as a
NEW family-consistent mapping with new handlers in both commands (§1 row 6, FD-M6-11, M6C-21). Draft C's "`session_date_for` never raises" verified TRUE (V29) — but only
for `session_date_for`; `schedule_for`/`phase_at` can raise `UnknownSessionDate`. The reconcile path
calls `schedule_for` exactly ONCE per not-found DAY-order probe (`_session_over`, §D) and maps
`UnknownSessionDate` to session-not-over (presumed-live, fail-closed toward keeping the order
tracked) — M6C-15; it never calls `phase_at`. Draft A's claim that the S1 canary would observe reconcile reads in
`broker.calls` is wrong (V28 — reads are unrecorded); the ctor-pass question is decided on other
grounds (§1 row 4).

### 0.2 The M5 §T bucket — every item ruled IN/OUT (rationale + owner)

| # | §T item | Ruling | Rationale (one line) | Owner of the OUT half |
|---|---------|--------|----------------------|-----------------------|
| 1 | SOD/EOD reconcile diff job (consumes §P streams + `client_order_id` join; sets `PortfolioRead.unreconciled_drift`) | **IN — the milestone** | This IS M6 (spec :406, S5 :389); everything below serves it. | — |
| 2 | `trade_updates` websocket fill stream | **OUT** | New always-on connectivity violates "Edge before live" (spine construction ends at M6/M7); REST polling + this reconcile bound the damage of a missed fill fail-closed (drift latches, opens stop). | M8-adjacent / edge-phase decision |
| 3 | Per-execution fill granularity (Alpaca activities endpoint) | **OUT** | Cum-watermark fill accounting (FD-M5-18) plus the position/cash lenses catch every net effect; a second wire surface buys observability, not safety. | M7+/live ops |
| 4 | Retry/replace policy + `max_open_orders > 1` | **OUT** | Throughput feature, not reconcile hardening; M5's single-in-flight invariant (FD-M5-21/22) simplifies the order join and stays. | M7, only if edge validation demands throughput |
| 5 | Dividend/CA position adjustment in `PaperBook` | **SPLIT** | IN: the journaled `position_adjust` row + `PaperBook.rehydrate` fold extension (the *mechanism*; broker-truth values only). OUT: modeled CA-factor math and any auto-adjust of a CA-frozen durable — frozen symbols stay operator-attended per S7 (FD-M6-8). | M7+ / operator runbook |
| 6 | Auto-retry cadence for kill residuals | **OUT** | Reconcile *reports* residual drift (it shows up in the position lens) and never actuates; the operator-attended runbook stands (FD-M4-19); reconcile never calls `trigger`/`retry_residual` (FD-M6-1). | M7 / ops |
| 7 | Journal rotation (M1 MINOR-9 mirror) | **OUT** | Orthogonal to reconcile correctness; rotation mid-M6 would complicate replay-based diffing; LEAN. | Pre-edge-validation ops hardening under M7 |

Also OUT (not in the bucket but adjacent): `CorporateActionFeed` blackout wiring into
`_refresh_market_state` (M6 passes `blacked_out=False` to the detector — label-only, never weakens
the freeze; no CA feed source exists); wiring `detector.is_frozen` into `TradabilityInputs.frozen`
(§1 row 15 — M7); dashboard renderers; anything `AlpacaLiveBroker` (M8); any change to two-key arming.

### 0.3 Explicit assumptions

- **A1.** Alpaca paper applies no cash effects outside fills (fee-free per V23; dividends not
  simulated per the parent spec :173) — the cash telescope is therefore **exact** by construction and
  any residue is unexplained drift that must latch. If real-world paper observation falsifies this,
  the remedy is the explicit operator rebaseline path (`agent reconcile --rebaseline-cash`, §E — the
  ONLY producer of action `rebaselined`, M6C-5) or a contract rev with evidence — never a silent
  tolerance widening (§1 row 10) and never an automatic re-anchor: a pass that finds a residue
  carries the previous baseline FORWARD unchanged, so the residue re-detects every pass until the
  operator acts (M6C-5) — literally every pass: cash-skipped passes re-journal the carried
  residue while it is outstanding (RC-8).
- **A2.** Alpaca paper positions payloads normally carry `avg_entry_price`; the cost lens degrades to
  note `cost_unverifiable` when it is absent (typed Optional, V1) — never a drift verdict.
- **A3.** Alpaca keeps terminal orders queryable by `client_order_id` over the EOD→next-SOD horizon
  (M5 §0.2 A4; not re-verified live here — offline tests do not depend on it).
- **A4.** No runnable mode has a live tick source until M1-2b ⇒ the in-process EOD edge fires only in
  replay-driven compositions (and only paper-mode ones — the hook is mode-gated, RC-2); the
  **operational** reconcile channel is the CLI job (`agent reconcile`
  nightly + the SOD pass `agent paper` runs at startup). Honest, not a gap; the next startup's SOD
  pass covers any truncated run.
- **A5.** No production symbol→`DurableId` mapping source exists (no CA feed; Alpaca positions carry
  no CUSIP/FIGI). The detector path is fully built + tested behind the injected `durable_ids` seam
  and is **inert** (byte-identical M5 behavior) when the mapping is empty — which it is in every
  production composition. Owner of provisioning: Robin / edge-phase ops.
- **A6.** A `reconcile_run` row with `completed=true, clean=true` on a `{sod, eod, cli}` phase is
  sufficient to clear the latch because every pass diffs the FULL surface (positions, cost, orders;
  cash whenever a baseline exists and the deferral set is empty) — partial passes do not exist in
  this contract; lens skips are journaled notes and any skip that hides a previously-latched
  dimension cannot produce `clean=true` against a still-drifted broker (the position facts re-diff
  every pass; a known cash residue RE-JOURNALS through cash-skipped passes — RC-8 — so the cash
  dimension cannot hide behind a skip either). The cash re-diff stays non-vacuous because an
  unexplained residue never refreshes the baseline automatically (M6C-5); immediate passes structurally never produce
  `clean=true` (M6C-1); the fold cross-checks every clean summary against the drift rows in its own
  window (M6C-22); and no symbol may be frozen at clear time (M6C-1).

---

## 1. Disagreement table (A = correctness, B = safety, C = integration; rev-1 resolutions, amended in place where rev 2 findings touched them — M6C ids inline; see §V)

| # | Topic | A | B | C | Resolution | Rationale (one line) |
|---|-------|---|---|---|------------|----------------------|
| 1 | Engine module name/count | `agent/reconcile_runner.py` (one module) | `agent/broker_reconcile.py` (one module) | `agent/broker_reconcile.py` + `agent/reconcile_ledger.py` (two) | **C: two modules** | Avoids the M1 `recorder/reconcile_runner` layout-slot collision (V14 :444) and keeps the diff engine strictly pure for the AST guard; the ledger module needs `recorder.persistence`. |
| 2 | Book adjustment mechanism | `position_adjust` positions-stream row + fold, auto-applied at SOD/EOD | adjust rows exist but ONLY an operator `--acknowledge` run writes them | no book mutation at all: ledger-side offsets replayed from the reconcile stream | **A's mechanism** | M5 code literally promises the fold ("M6 folds any residual fill", V6); C's offsets leave the book permanently diverging from the position-of-record (a later strategy close would oversell — broker rejects ⇒ more drift); B's operator-only path breaks the locked **autonomous** paper-validation goal for benign machine-explainable drift. |
| 3 | Latch clearing rule | clean `reconcile_run` row clears | ONLY operator acknowledge clears; clean pass never auto-clears | completed pass with `hard==0` clears | **A+C: a COMPLETED clean full pass clears; incomplete passes never change it** | Autonomy requires machine clearing of machine-resolved drift; the unresolvable kinds (broker-only, shorts, CA-frozen) stay un-clean until the operator acts at the broker, so they hold the latch anyway — B's safety intent is preserved without an acknowledge ceremony. |
| 4 | SOD pass placement | ctor step 11 (synthetic too) | ctor step 11 (paper-only) | explicit exported method; `_cmd_paper` invokes after construction; never in `__init__` | **C** | A ctor pass would silently add broker reads + journal rows to every existing paper composition (1520-test churn risk) and double-reconcile the CLI job (construction + explicit call); explicit invocation keeps ctor failure semantics clean and M5 pins byte-intact. |
| 5 | Synthetic-mode SOD pass | runs (clean-by-construction tripwire) | does not run | does not run | **does not run (B+C)** | FakeBroker reads derive from its own fill book (V18 — cannot drift); zero payload, plus golden/test churn for nothing; tests may still call `run_reconcile` explicitly against FakeBroker. (Rev 3: the EOD hook is mode-gated `mode == "paper"` too — synthetic compositions have a FakeBroker, so broker-presence alone would not exclude them at the session edge; RC-2.) |
| 6 | CLI exit codes | 0 clean / 1 drift / 2 usage+mode+RunLock / 3 broker-unreadable | 0/1/2 usage/3 aborted (RunLockHeld⇒3) | 0 ok / 2 not-completed (incl. RunLock, creds) / 3 drift / 1 unhandled | **A's vocabulary** | Drift=1 is 2-of-3 and the conventional "check failed" code; mode-mismatch=2 matches the existing CLI precedent (V15) and RunLockHeld=2 is a NEW family-consistent mapping (today it is uncaught — M6C-21, FD-M6-11); 3 = "could not reconcile ≠ reconciled" keeps broken/drifted distinguishable for cron. |
| 7 | `Broker.list_open_orders` Protocol extension | not used (probe only journaled ids) | IN — lockstep base/fake/alpaca/spy + `broker_order_untracked` kind | explicitly OUT (FD-M6-18) | **OUT (A+C)** | The position/cash lenses already catch the economic effect of any untracked order; only the agent submits to this paper account; no broker-surface growth in a reconcile milestone; kills the V17 hallucination risk and the FakeBroker purity trap. |
| 8 | Missed-fill catch-up | route through the live poll/booking pipeline (`caught_up` action) | journal-side resolution via existing `order_terminal` mechanics | order lens is alert-only; the economic effect is adopted by the position/cash lenses | **C (+B's journal-side terminal resolution)** | Re-entering the live booking machinery outside the tick loop fabricates task state and re-implements PaperBook open/close booking inside the pass; the position lens adopts broker truth in the SAME pass via `position_adjust`, and the order resolves journal-side with `record_order_terminal` (V11). No fill row is ever fabricated; the CASH effect is only DETECTED, never machine-resolved — when a baseline exists the residue latches and persists every pass until the operator acts (RC-5, FD-M6-21). |
| 9 | Cost-lens severity + tolerance | HARD drift beyond `qty × 0.005`/share; skip-note when `avg_entry_price` absent | HARD, exact (tolerance 0) | SOFT alert always | **A** | `avg_entry_price` is a derived per-share figure with no contractual precision pin, so exact compare (B) manufactures permanent false drift; but a beyond-half-cent-per-share divergence is a real missed fill/CA and silently corrupts broker-lineage cost basis, so soft-always (C) under-reports. |
| 10 | Cash tolerance | exact telescope | exact (all tolerances `Decimal("0")`) | `CASH_TOLERANCE_USD = 0.01` | **exact (A+B)** | Fee-free paper makes the telescope exact by construction (V23/A1); a cash tolerance is a loosen-polarity knob; falsification by live-paper observation is a contract rev with evidence, never a silent widening. |
| 11 | `AccountStore` interaction during a pass | EOD pass `put`s its fresh read into the store | (silent) | never touches the store, no F4 row | **C** | An `AccountStore.put` path writes an F4 `account_snapshot` row to risk.jsonl — perturbing `AccountStore` freshness state and adding unpinned rows to the risk stream — for zero safety; the pass journals its own provenance in `reconcile_baseline`. (Rationale corrected per M6C-19: no existing e2e test byte-compares risk.jsonl — V27.) |
| 12 | Order probe attempts per pass | `RECONCILE_ORDER_PROBE_ATTEMPTS = 3` | (unspecified) | 1 (the M5 ×3 recovery already ran at step 10) | **1** | `FakeBroker.order_status` advances a scripted fill slice per call (V18) — multi-probing has test side effects; a failed probe maps to an incomplete pass + exit 3 + rerun, which is the honest network-flake story. |
| 13 | Trigger/phase vocabulary | `{sod, eod, immediate}` | `{sod, eod, immediate, cli, acknowledge}` | `{sod, eod, freeze, cli}` | **`{sod, eod, immediate, cli}`** | `immediate` matches the spec's own language (:177-178); `cli` names the operator job; `acknowledge` dies with the acknowledge path (row 2/3). |
| 14 | Latch row mechanics | derived: drift row sets, clean run row clears (fold) | explicit `drift_latch_set`/`drift_latch_cleared` event types | derived: summary `latch_after` + trailing-rows-without-summary ⇒ latched | **derived fold (A+C), no extra event types** | The summary row is the commit marker; trailing drift rows after the last summary keep the latch (mid-pass crash, fail-closed); explicit latch events are a second source of truth to keep consistent for no payload. |
| 15 | `detector.is_frozen` → `TradabilityInputs.frozen` | not wired | not wired (M7) | wired for mapped symbols | **OUT (A+B) — M7** | A freeze now structurally holds the GLOBAL latch (the immediate pass synthesizes a `ca_silent_adjust` finding from the FreezeSignal and can never be clean; latch clears are phase-restricted AND blocked while any symbol is frozen — M6C-1), which blocks ALL opens — strictly stronger than per-symbol; touching `_refresh_market_state` (V8-adjacent) risks decider-downstream effects on the close path for zero added safety in M6. |
| 16 | Golden regeneration | regenerate once (SOD rows in synthetic runs) | extend golden set with the new stream | byte-stable, no regen (A4) | **byte-stable, no regen (C)** | Verified: goldens compare orders/fills/positions only (V27 — no e2e golden byte-compares risk.jsonl; RC-6) and no automatic pass runs in synthetic/observe — SOD per rows 4/5, EOD per the RC-2 `mode == "paper"` gate (structural, not an artifact of the golden scripts stopping short of 16:00) — and the unused writer creates no file (V30); the new stream gets its own pinned bytes in NEW tests only. |
| 17 | EOD placement + dedupe | inside the existing once-per block, before `close_of_day` | after `close_of_day`, separate set | after the best-effort cancel, before the margin block, separate set, independent of `latest_unsafe()` | **C** | The margin block early-returns when `latest_unsafe()` is None (V9) and must not starve the reconcile; a separate `_reconciled_eod_sessions` set keeps the two once-per-session guards independent; ordering vs `close_of_day` stops mattering once the store is untouched (row 11). |
| 18 | Status freeze-row writer | (unspecified) | `StatusLedger` over the existing step-4 status writer | same | **B+C** | `record_broker_adjust_freeze` is a `StatusLedger` method (V24); constructing a `StatusLedger` over the SAME resolved status.jsonl path shares lock+seq via the path-keyed registry (V12) — no second writer hazard. |
| 19 | Cash lens scheduling | EOD only; SOD baseline + rebaseline | telescope from previous snapshot, first-ever seeds | every pass with a baseline; baseline at end of every completed pass | **every completed pass with a baseline AND an empty deferral set (M6C-2); skip+note otherwise; the baseline's (cash, watermark) pair refreshes ONLY when the cash lens evaluated CLEAN this pass (M6C-5); a first-ever pass with a non-empty deferral set writes NO baseline row at all (RC-1)** | Cash moves only on fills in a fee-free paper account, so an intraday check is sound; an in-flight order makes the read torn-by-construction, so it skips honestly (`cash_skipped_inflight`) AND the prior baseline carries forward byte-identical — a torn read never anchors a telescope, and an unexplained residue is never silently absorbed (M6C-5; the telescope simply spans two passes — it stays exact because it is fill-driven). A skip while a residue is OUTSTANDING re-journals the carried residue as a drift row, so a skipped pass can never present clean over a known residue (RC-8). |
| 20 | Positions-stream event name | `position_adjust` | `reconcile_adjust` | (none — no book event) | **`position_adjust`** | Stream-voice consistency with `position_open`/`position_close`; the reconcile lineage rides in the row's `reconcile_id`/`adjust_id` fields. |

---

## 2. Frozen decisions (FD-M6-1 … FD-M6-22)

| ID | Decision | Rationale |
|----|----------|-----------|
| FD-M6-1 | **Reconcile is observation + journal, period.** The pass calls only `broker.account()` / `positions()` / `order_status(client_order_id)` (tokenless, V17) through the `_provider_payloads()` seam (V31 — test doubles keep working). It never imports `execution_preflight`, never constructs an `OrderIntent`, never submits, never cancels, never calls `RiskKillSwitch.trigger`/`retry_residual`, never mints or consumes a token. It runs with gates OFF and is **never gated by `opening_allowed`** — drift detection must work exactly when the system is locked down (M5C-S3 precedent, V7). Enforced by the AST pure-family guard + the S1 canary extension (§I tests 6, 7). | The S1 hard boundary; even cancels are excluded so the **PASS's** broker blast radius is exactly zero mutations. (The CLI *composition's* only possible broker mutation is the inherited, journaled M5 step-10 recovery cancel — `restart_unknown_state`, reduce-direction-safe per FD-M5-23 — owned explicitly in FD-M6-22 and pinned in §I file 5, never silently forked out: M6C-6.) |
| FD-M6-2 | **Adjustment algebra is diff-driven, hence a fixpoint.** Adjustments are computed from the *current* (local, broker) pair, never replayed as deltas. Running a pass twice is structurally idempotent: after the first pass's adjustments local == broker, so the second pass emits no adjustment. Double-run safety needs no dedup table. | Determinism + crash-safety without bookkeeping. |
| FD-M6-3 | **One new stream + one fold extension, nothing else.** Drift/note/baseline/summary rows go to NEW stream `journal/reconcile_alerts.jsonl` (the spec name, V14) via `ReconcileLedger`, a validating facade over `recorder.persistence.EventWriter` (no second writer/hash/serialization implementation). The position correction is a NEW positions-stream event `position_adjust` (via `ExecLedger`), and `PaperBook.rehydrate` is extended to fold it **in the same change** (V5 — otherwise every restart after an adjustment bricks). Nothing reconcile-shaped is ever written to `orders.jsonl` carrying an `order_id` kwarg except true lifecycle rows (V4 — a note row with an `order_id` kwarg would perturb `open_orders`; broker order ids ride payload fields). The status stream gains its first production `broker_adjust_freeze` writer (FD-M6-14) — no new status event types. | Avoids the two worst integration traps (V4, V5) and keeps the write surface auditable. |
| FD-M6-4 | **Comparison policy (the diff core), frozen.** All comparisons are **Decimal VALUE** comparisons (replayed strings re-wrapped via `Decimal(str(v))`; broker values arrive typed from the parse chokepoints), never serialized-string equality, never float; all derived sums under `Context(prec=28, ROUND_HALF_EVEN)`; no quantization of inputs anywhere in the diff (FD-M4-25). Per lens: **(a) position qty — exact** (`!=` ⇒ drift; shares are integers in M5's order matrix; no legitimate sub-share noise; broker-flat = absence = `Decimal("0")` via `qty_for`, V1/V2). **(b) position cost** — local fact = Σ open `PaperPosition.broker_cost_usd` per symbol (exact integrated notional, FD-M5-18); broker fact = `avg_entry_price × qty` under the pinned ctx; tolerance `\|local − broker\| ≤ qty × COST_TOLERANCE_PER_SHARE`, `COST_TOLERANCE_PER_SHARE = Decimal("0.005")` (§1 row 9 justification); `avg_entry_price is None` ⇒ lens skipped with note `cost_unverifiable` — a skip is not drift (FD-M6-7). **(c) cash — exact telescope**: `expected_cash = baseline_cash − Σ buy delta_cost_usd + Σ sell delta_cost_usd` over `broker_fill` rows with fills-stream `seq >` the baseline watermark, **deduped by first-occurrence `fill_id`** (duplicate polling re-reads journal the same fill_id deliberately), with rows whose `fill_id` already occurs at `seq ≤` the watermark excluded by the orchestrator pre-filter (boundary-straddling duplicates cannot double-count — M6C-26); fee-free paper makes the telescope exact (V23/A1); any residue latches with action `latched_operator` and the baseline carries forward unchanged (M6C-5). Evaluated only when a baseline exists and the deferral set is empty (§1 row 19, M6C-2). **(d) equity / buying_power — provenance-only lenses**, journaled in the baseline row, never a drift verdict (no independent local fact exists: we never recompute equity, marks ≠ MV by construction, FakeBroker reports cost as MV — V18). One broker-internal identity check, `\|equity − (cash + Σ market_value)\| > Decimal("0.01")` ⇒ note `broker_internal_inconsistency`, never drift. | Every drift dimension either latches exactly or is pinned provenance-only — no dishonest "tolerance" middle ground; the two non-zero tolerances are justified one-by-one and live as code constants (FD-M6-5). |
| FD-M6-5 | **No config block.** Every reconcile knob is a code constant (`COST_TOLERANCE_PER_SHARE`; the identity-check cent). No committed key, no overlay key. *Rationale:* any committed knob changes `rules_hash` on every row of every stream (golden churn) and creates polarity hazards — a tolerance knob loosens under growth, and an "enabled" knob would make reconcile switch-off-able (it must not be); overlay-only keys are silently ignored by `tighten_only_merge` anyway (V19), so there is no honest overlay path either. | The `latency_budget_ms` lesson (V20), applied wholesale. |
| FD-M6-6 | **The drift latch is global, journal-derived, and rehydrated.** It cannot live on a `PortfolioRead` (rebuilt every refresh, V8). It lives as `Orchestrator._drift_latch: bool`, derived by the PURE fold `rehydrate_reconcile_state` over `reconcile_alerts.jsonl` (§B): by ascending `seq`, any `reconcile` (drift) row sets it; a `reconcile_run` row with `completed=true AND clean=true AND phase ∈ {sod, eod, cli} AND zero drift rows observed since the previous summary` clears it (immediate-phase summaries NEVER clear — M6C-1; an inconsistent window — drift rows followed by a clean summary — keeps it set fail-closed — M6C-22); trailing drift rows after the last summary keep it set (mid-pass crash, fail-closed). In-process, `self._drift_latch = True` commits the MOMENT a pass's findings become non-empty, BEFORE the first journal write (a raise mid-write-sequence cannot leave a drift-aware journal with a drift-unaware gate — M6C-24); the clear decision stays at pass end and additionally requires NO symbol frozen (detector-frozen ∪ status-rehydrated frozen — M6C-1/M6C-4). Folded at startup (new PURE step 6.5 — file replay only, before any broker contact; `self._drift_latch = fold.latched or bool(self._frozen_durables)`) and re-stamped into every read in `_portfolio_read` via `dataclasses.replace(..., unreconciled_drift=self._drift_latch)`. A crash after drift detection therefore cannot clear a known drift; `can_open` needs **zero** changes (V3). The D6→D32 composition is closed by RC-8: a cash-skipped pass while a residue is OUTSTANDING re-journals the carried residue as a drift row (FD-M6-17), so its summary is never `clean=true` and its window is never drift-free — neither the in-process clear nor the fold can fire over a known unexplained residue. | V8's trap closed; fail-closed across restarts; the consumer already exists. |
| FD-M6-7 | **Drift means a KNOWN mismatch, never a failed read.** `AccountInvalid`, positions `ValueError`, a `BrokerHttpError` instance returned as data, or a missing broker produce note rows (`broker_read_failed` / `order_probe_failed` / `reconcile_skipped_no_broker`) + a `reconcile_run` summary with `completed=false` (clean is forced false) — and leave the latch UNCHANGED; the CLI maps it to exit 3. (`JournalCorruption` is NOT in this class: it is fatal with NO reconcile row written — FD-M6-19/M6C-14; the CLI maps it to exit 3 too.) Unknown-portfolio states stay covered fail-closed by `portfolio_missing`/`portfolio_stale` (V3). Both broker payloads are isinstance-guarded (`Mapping` / `list`) BEFORE parsing — explicitly NOT the ValueError-only pattern of `_refresh_account` (V8 trap). | "Couldn't check" ≠ "checked clean" ≠ "drifted" — three distinguishable outcomes (exit 3 / 0 / 1). |
| FD-M6-8 | **SOD/EOD/cli adjust; immediate never adjusts; frozen and unrepresentable never adjust.** The `FreezeSignal` path (`immediate` phase) runs detect-and-flag only: the orchestrator SYNTHESIZES the `ca_silent_adjust` finding UNCONDITIONALLY from the signal's own `prev_qty`/`curr_qty` (never from the re-read — a broker race that re-agrees by probe time must not yield a clean pass; an immediate pass therefore structurally always has ≥1 finding and can NEVER be clean — M6C-1), then journals the drift row (`kind=ca_silent_adjust`, `action=frozen_immediate`), the status freeze row, latch ON — absorbing a mid-CA quantity without ≥2-source provenance is exactly the silent adjustment S7 forbids. "Immediate never adjusts" is NORMATIVE, not a comment: the engine takes `adjusts_allowed=False` on immediate passes, coincident non-frozen drift is journaled with action `adjust_deferred` (zero plans — the §A.1 adjusted⇒≥1-plan pin holds), and the plan-write loop is gated `phase != "immediate"` in the §D pseudocode (M6C-1); it auto-adjusts at the next sod/eod/cli pass. Symbols frozen by the detector are excluded from SOD/EOD/cli auto-adjust too (`latched_operator`; the §0.2 item-5 OUT half). Non-frozen drift (missed fills, kill-flatten residuals, late fills on superseded closes) auto-adjusts at sod/eod/cli because the broker number is uncontested truth there. Non-representable states are NEVER synthesized: broker-only positions (`position_unknown_broker`) and broker shorts (`short_unrepresentable`) produce drift + `latched_operator`, no `position_adjust`, no fabricated `PaperPosition` (fabricating `opening_order_id`/fee provenance is worse than a held latch). | Squares broker-is-truth with S7's no-silent-absorption; the latch holds wherever truth needs a human. |
| FD-M6-9 | **Order join key.** `client_order_id == order_id` (FD-M5-7, V32) for all `o-`/`synthetic-o-` orders. Kill flattens are matched by probing `order_status(f"flatten-{symbol}")` for symbols in `self._risk_kill.residual_symbols()` (V21) — the documented FD-M5-7 exception (V6). The sweep probes ONLY ids it journaled or derived this way — never speculative ids (FakeBroker raises `KeyError` on unknown ids by design, V18; the probe wrapper maps `KeyError` and a 404-shaped `BrokerHttpError`-as-data uniformly to "not found"). **One probe per order per pass** (§1 row 12). Symbols flattened-and-booked by a kill need no flatten probe: their orders were already polled at booking (V6) and any unbooked effect surfaces in the position lens. | Probing unjournaled ids crashes the fake by design and is unverifiable against Alpaca. |
| FD-M6-10 | **PnL split integrity (S5).** Reconcile rows carry `BrokerUSD` and plain-Decimal qty ONLY; a `ModeledUSD` reaching any reconcile or adjust field raises `TypeError` with NO row written. The modeled lineage is never read, written, scaled, or flagged by reconcile; `position_adjust` touches `qty`/`broker_cost_usd` and leaves `modeled_cost_usd` byte-identical. `broker_account_pnl` and `execution_realistic_pnl` cannot collapse through M6. | The S5 lineage wall extends to the new stream. |
| FD-M6-11 | **Exit codes (the S5 surface), frozen:** `0` = pass completed, no drift; `1` = drift found this pass OR a pre-existing latch still set (even if fully adjusted — the operator must see it); `2` = usage / mode mismatch / `RunLockHeld` — `RunLockHeld ⇒ 2` is a **NEW mapping** chosen for family-consistency with the existing usage/mode-mismatch exit-2 precedent (V15); today `RunLockHeld` is UNCAUGHT in the CLI (unhandled traceback), so BOTH `_cmd_reconcile` AND `_cmd_paper` gain the `try/except RunLockHeld ⇒ stderr + exit 2` wrapper (M6C-21; never bypass the lock — the job writes, V22); `3` = could not reconcile (broker unreadable / parse failure / credentials-missing degrade / `JournalCorruption` — "no reconcile ≠ reconciled", fail-closed non-zero). **Precedence (RC-13): `completed=false` ⇒ exit 3 takes precedence over exit 1**, regardless of drift findings journaled this pass or any latch state — could-not-fully-check outranks drift-found (the drift is journaled + latched and re-surfaces as exit 1 on the next completed pass; both codes alert per M6C-25). This closes the literal overlap between the exit-1 clause ("drift found this pass OR a pre-existing latch still set") and the exit-3 clause on passes that both journal drift AND fail to complete (D35) — the same resolution D22/D31 already pin for the pre-existing-latch arm; the identical precedence governs `_cmd_paper`'s SOD mapping (3 over 1 on an incomplete SOD pass, M6C-23). In-process passes (sod-from-`_cmd_paper` aside) never exit the process; they latch + journal. `_cmd_paper` returns 1 when its SOD pass found drift or a rehydrated latch is set, and **3 when that pass returns `completed=false`** (could-not-check ≠ startup-ok — the FD-M6-7 three-outcome discipline holds on the startup path too, M6C-23). | Cron/ops must distinguish broken from drifted; S5 demands non-zero on drift, lens B adds non-zero on inability to reconcile. |
| FD-M6-12 | **Pass invocation is explicit, never in `__init__`** (§1 row 4). `Orchestrator.run_reconcile(*, phase, ts_utc, now_ms, trigger: Optional[FreezeSignal] = None) -> ReconcilePassResult` is an exported method (`trigger` carries the FreezeSignal on immediate passes — the durable key + prev/curr qty plumbing for the synthesized finding and the summary's `trigger_durable_key`, M6C-1; `None` on every other phase). Invocations: **sod** — `_cmd_paper` ONLY calls it after construction (paper mode with a broker; the degraded-observe path skips the call, §E); `_cmd_reconcile` runs NO sod pass — RC-3; **eod** — `_session_edge`, paper mode only (the RC-2 gate), once per `session_date_et` via a NEW `_reconciled_eod_sessions` set, after the best-effort cancel, independent of the margin block's `latest_unsafe()` early-return (V9, §1 row 17); **immediate** — same tick as a `FreezeSignal`, invoked with `trigger=<the signal>` (FD-M6-14, M6C-1); **cli** — `_cmd_reconcile`, exactly ONE pass per job (FD-M6-22/§E; degraded-observe ⇒ the pass notes `reconcile_skipped_no_broker` ⇒ exit 3). No wall clock anywhere in the orchestrator (`ts_utc` is supplied: CLI wall read per the `mint_run_id` precedent V15; the recorded `self._instant_utc` in-process; tests inject). Startup step 6 stays pinned PURE (V7). | Keeps ctor failure semantics clean, avoids double-reconcile in the CLI job, and leaves every existing test composition byte-identical. |
| FD-M6-13 | **Multi-position allocation, frozen.** The broker reports one row per symbol; the book may hold several open `PaperPosition`s (the key-collapse trap). A negative qty delta (broker < local Σ) is allocated **LIFO by `position_open` journal seq** (newest open position first), each position clamped flatten-never-flip, cascading to the next; a positive delta (broker > local Σ) accrues entirely to the newest open position. Cost adjustment sets the adjusted position's `broker_cost_usd` so the symbol-level Σ equals the broker-derived cost. One `position_adjust` per touched position per pass (structurally, via `adjust_id`). A symbol in the pass's **DEFERRAL SET** defers adjustment (drift still journaled with action `adjust_deferred` + note `adjust_deferred_inflight` + latched). The deferral set (M6C-2) = symbols of every order still open after this pass's sweep — i.e. in the V4 rehydrated/live `open_orders` fold, **including presumed-live unconfirmed orders (FD-M5-17)**, and NOT resolved terminal by this pass's probe — ∪ the live task's symbol ∪ `_open_deny` members ∪ symbols whose flatten probe returned a non-terminal result OR FAILED (§3 — a failed probe is LESS information than a live result, so it defers at least as hard; RC-9). Orders the probe resolved terminal THIS pass leave the set, so a D12 missed-fill adjusts in the SAME pass (FD-M6-21) while a still-live order defers (D23/D28) — the probe result IS the differentiator. Adjusts (and the cash lens, §1 row 19) run only with an empty/non-member deferral set, which keeps the rehydrate fold's ordering assumptions sound (§B.2): a fill booked later for a deferred symbol can never land after an adjust row, so the prev-value check cannot brick a restart. | Deterministic, reversible-on-paper allocation without inventing per-lot broker truth that does not exist. |
| FD-M6-14 | **Detector seeding sequence (closes the M2 IOU, V10).** New injected ctor seam `durable_ids: Optional[Mapping[str, DurableId]] = None` (default empty ⇒ detector exempt ⇒ byte-identical M5 behavior, A5). At the END of every COMPLETED `{sod, eod, cli}` pass, `seed_baseline(durable_id, broker_qty)` runs ONLY for held symbols with a mapped durable id whose post-pass local folded qty EQUALS the broker qty — an ACTUALLY reconciled point, the S7-2 requirement (M6C-9): frozen symbols, deferral-set symbols, and broker-only symbols are SKIPPED, and immediate-phase passes seed NOTHING (FD-M6-8 — they adjust nothing, so nothing is reconciled). A frozen durable's baseline is never re-seeded until the operator path (M7) unfreezes (M6C-9). Per `_refresh_account`, for each mapped+seeded+not-frozen symbol, `observe_broker_qty(durable, qty_for(symbol), blacked_out=False)` runs ONLY when (i) no order is in flight for that symbol and (ii) the local folded book qty still equals the seeded baseline (the agent has not itself traded since the seed — an agent-originated fill must not false-fire the detector); when local has moved, observation is suspended until the next consistent point re-seeds (broker qty == local folded qty, or the next completed pass). A `FreezeSignal` ⇒ `StatusLedger.record_broker_adjust_freeze` (a `StatusLedger` constructed over the existing step-4 status writer — same resolved path ⇒ shared lock/seq via the path-keyed registry, V12/V24, §1 row 18) + `run_reconcile(phase="immediate", ...)` **in the same tick** (spec :177-178 — immediate, not EOD). Held symbols without a durable id remain fully position-diffed but detector-exempt; note `durable_id_missing` once per symbol per run (state home: the per-run `self._noted_durables` set, seeded empty at startup step 6.5 — M6C-27). `blacked_out=False` is label-only (relabels the `FreezeReason`, never the freeze — S7-1); real CA-feed wiring is OUT (M7). | Seeds only at reconciled points (S7-2: observe-before-seed RAISES, by design); never freezes on the agent's own fills; immediate-not-EOD per the spec. |
| FD-M6-15 | **Scope fence.** No websocket, no activities endpoint, no retry/replace, no `max_open_orders` change, no journal rotation, no kill-retry cadence, no modeled CA math, no `Broker` Protocol growth (`list_open_orders` stays off the Protocol, §1 row 7), no `TradabilityInputs` change (§1 row 15), no `AccountStore` touch from the pass (§1 row 11). Any of these appearing in an M6 diff is a contract violation. | The locked LEAN bias, enforceable in review. |
| FD-M6-16 | **Module naming.** New modules `scripts/agent/broker_reconcile.py` (PURE diff engine + vocabularies) and `scripts/agent/reconcile_ledger.py` (validating facade + replay + latch fold). `scripts/recorder/reconcile.py` (M1 dual-hash depth reconcile) is untouched, never imported by, and never imports, the M6 modules; the spec layout's `recorder/.../reconcile_runner` slot (V14 :444) remains the M1 depth-sense slot and stays vacant. | One name = one concept (§1 row 1). |
| FD-M6-17 | **Baselines and watermarks.** Every COMPLETED pass ends by writing a `reconcile_baseline` row — with ONE pinned exception (RC-1): a completed pass with NO prior baseline AND a non-empty deferral set writes **NO baseline row at all** (there is no previous pair to carry forward, and the §B.1a non-nullable `cash_usd`/`fills_seq_watermark` fields must never be filled from a torn read — the first-ever seed defers in full; the first completed pass with an empty deferral set writes the seed). The row: broker truth post-adjustment — `cash_usd`, `equity_usd`, `buying_power_usd` (provenance), sorted `positions [{symbol, qty}]`, `fills_seq_watermark` (the current max fills-stream `seq`; a payload-legal name — bare `seq` is reserved, V12), sorted `durable_seeded` keys. **The `(cash_usd, fills_seq_watermark)` pair REFRESHES to broker truth / current max fills seq ONLY when the cash lens evaluated this pass AND found zero residue** (or on the first-ever seed, which itself requires an empty deferral set); when the lens was skipped (deferral set non-empty) **or found a residue**, the previous baseline's pair carries FORWARD byte-identical — a torn read never anchors a telescope, and an unexplained residue re-detects every subsequent pass and holds the latch until the operator acts (`agent reconcile --rebaseline-cash`, §E — the ONLY producer of action `rebaselined`; M6C-5). **"Every subsequent pass" is literal, skipped lenses included (RC-8):** while a residue is OUTSTANDING (the latest EVALUATED cash outcome was a residue, not yet `rebaselined` and not yet re-evaluated clean), a completed pass that SKIPS the cash lens RE-JOURNALS the carried residue as a `cash` drift row (action `latched_operator`; `local`/`broker`/`diff` byte-identical to the outstanding residue row — a carried KNOWN fact, never a fresh measurement; fresh `drift_id` via the new `reconcile_id`), so a cash-skipped pass can never produce `clean=true` over a known residue and neither the in-process clear nor the fold's drift-free window can fire (FD-M6-6). The cash telescope consumes `broker_fill` rows strictly above the latest baseline's watermark, first occurrence per `fill_id`; the orchestrator pre-filter additionally excludes any row whose `fill_id` already occurs at `seq ≤` the watermark, so a duplicate polling re-read straddling a baseline boundary cannot double-count (M6C-26). The first-ever pass has no baseline: with an EMPTY deferral set, the cash lens is skipped with note `baseline_seeded` and this pass's row seeds the pair; with a NON-EMPTY deferral set, note `cash_skipped_inflight` and NO baseline row (RC-1 — D33). | Short telescopes, replay-deterministic, and no machine self-absolution of unexplained cash (§1 row 19, A1); a torn read never seeds (RC-1). |
| FD-M6-18 | **Versions.** `EXEC_LEDGER_VERSION` stays 1 — `position_adjust` is additive vocabulary; no existing row shape changes (a bump would churn every golden for zero information). New `RECONCILE_LEDGER_VERSION = 1` governs the new stream. | Additive = no bump (the M5 precedent). |
| FD-M6-19 | **`JournalCorruption` is fatal to the job.** Replaying local streams never catches-and-continues past a newline-terminated corrupt line (V12). The CLI maps it to exit 3 with **NO reconcile row written** (writing into a possibly-corrupt journal tree is worse than silence; the exit code is the signal — the closed RECONCILE_NOTES set deliberately carries no corruption token, M6C-14); in-process it propagates (corruption-grade, same as today). | A reconcile that tolerates corruption certifies garbage as clean. |
| FD-M6-20 | **Kill interaction.** Reconcile never calls `trigger`/`retry_residual` (V21). Residual symbols are diffed like any other; expected divergences (broker filled the flatten, local close unbooked — V6) auto-adjust at sod/eod/cli like any non-frozen drift. Adjusting the book while HALTED is safe: the kill rung blocks opens before the portfolio rung in the frozen phase-1 order (V3), and the reduce path gains zero gates from any of this. The kill latch and the drift latch are independent fail-closed levers — neither clears the other. | Two levers, no coupling, no weakening in either direction. |
| FD-M6-21 | **Order-state resolution writes journal-side only** (§1 row 8). Resolutions per the §3 table: `record_order_terminal` (V11 signature, with the order's original journaled `decision_id`/`order_id`) closes the order in `open_orders` on the next rehydrate (V4); a missed-fill detection (`fills_missed`) is alert-only on the order lens — the QTY effect is ADOPTED by the position lens's `position_adjust` in the SAME pass (the terminal resolution removes the symbol from the deferral set, M6C-2). The CASH effect is only DETECTED, never machine-resolved (RC-5): the missed fill is never journaled as a `broker_fill` row (no fabrication), the telescope consumes ONLY `broker_fill` rows (FD-M6-4(c)), and a residue never refreshes the baseline (M6C-5) — so when a baseline exists, the same pass journals a `cash` residue (`latched_operator`) that re-detects on EVERY subsequent pass — cash-skipped passes included, via the RC-8 carried-residue re-journal (FD-M6-17) — and holds the latch until the operator `--rebaseline-cash`. Deliberate and strictly fail-closed; loosening the telescope to also consume terminal-resolution cum-notional deltas would weaken the no-fabrication line and is a contract-rev decision, not a default. A not-found ⇒ expired resolution (§3 row) carries PINNED kwargs (M6C-29): `filled_qty` = the order's journaled cum fill watermark (`Decimal("0")` when no fills), `cum_notional_usd` = the journaled cum notional after the last fill (`Decimal("0")` when no fills), `ts_broker_utc=None` (V11 allows None). No fill row, no booking-pipeline re-entry, no `order_state_alert` misuse for non-alert semantics. | No fabricated fills; the broker stays position-of-record through the position lens. |
| FD-M6-22 | **The CLI job IS an orchestrator composition.** `_cmd_reconcile` constructs the same paper composition as `_cmd_paper` (committed config, tighten-only overlay, `.secrets/` paths, `_ZeroClock`/`_LatestQuoteView`) and calls `run_reconcile(phase="cli", ts_utc=<CLI wall UTC ISO>, now_ms=0)`. The composition's startup step-10 dangling-order recovery (V7) is INHERITED unmodified — including its journaled best-effort `restart_unknown_state` cancel on an adopted dangling order: the contract owns this as the job's ONLY possible broker mutation (reduce-direction-safe, FD-M5-23; excluding step 10 would silently fork the composition and change sweep semantics — M6C-6). RunLock interplay is inherited: a live agent run ⇒ `RunLockHeld` ⇒ stderr + exit 2 (a NEW handler — see FD-M6-11/M6C-21; the same wrapper is added to `_cmd_paper`); a stale dead-pid lock reclaims + journals as today (V22). Cross-process journal append safety comes ONLY from the lock — "just reading" is false, the job writes. | One composition root; the `__main__` import budget holds (V15). |

---

## 3. Frozen vocabularies (closed sets; emitting out-of-vocab raises `ReconcileError(ExecError)` with NO row written)

```python
# scripts/agent/broker_reconcile.py
RECONCILE_PHASES = frozenset({"sod", "eod", "immediate", "cli"})

DRIFT_KINDS = frozenset({
    "position_qty",             # sum(local open qty) != broker qty (both sides held or zero)
    "position_avg_cost",        # cost beyond qty x COST_TOLERANCE_PER_SHARE, qty agreeing
    "position_unknown_broker",  # broker holds a symbol the book does not know
    "position_missing_broker",  # book holds, broker flat (absence == qty 0, V2)
    "short_unrepresentable",    # broker qty < 0 (PaperPosition is long-only)
    "cash",                     # telescope residue vs the latest baseline (FD-M6-17)
    "order_state",              # local-open vs broker-terminal/absent divergence (FD-M5-7 join)
    "fills_missed",             # broker filled_qty ahead of the journaled cum watermark (alert-only lens)
    "ca_silent_adjust",         # FreezeSignal-triggered (immediate phase only); SYNTHESIZED by the
                                # orchestrator from the signal's prev/curr qty, never the re-read (M6C-1)
})

RECONCILE_ACTIONS = frozenset({
    "adjusted",            # position_adjust emitted and folded (broker won) — sod/eod/cli only
    "adjust_deferred",     # drift journaled; adjustment deferred (deferral-set symbol, M6C-2,
                           # or immediate phase, M6C-1); next eligible sod/eod/cli pass adjusts
    "resolved_terminal",   # exec-ledger order_terminal row emitted (journal-side resolution)
    "alert_only",          # fills_missed: the economic effect is adopted by the position/cash rows
    "rebaselined",         # cash baseline re-anchored by the OPERATOR path ONLY
                           # (agent reconcile --rebaseline-cash, §E/M6C-5) — never automatic
    "latched_operator",    # no automatic remedy; the latch holds for the operator
    "frozen_immediate",    # CA path: freeze + latch, never adjust (FD-M6-8)
})

RECONCILE_NOTES = frozenset({
    "cost_unverifiable",             # avg_entry_price is None — lens skipped, not drift
    "durable_id_missing",            # symbol diffed but exempt from the M2 detector
    "broker_read_failed",            # AccountInvalid / ValueError / BrokerHttpError-as-data (FD-M6-7)
    "order_probe_failed",            # probe raised / returned error-as-data (pass incomplete)
    "order_probe_unknown",           # broker status maps to "unknown"/"pending_cancel" — non-terminal, kept live
    "broker_internal_inconsistency", # |equity - (cash + sum(MV))| > 0.01 (provenance check, never drift)
    "adjust_deferred_inflight",      # deferral-set symbol (M6C-2); adjust deferred
    "cash_skipped_inflight",         # deferral set non-empty => cash read torn by construction (§1 row 19)
    "baseline_seeded",               # first-ever pass: no baseline, cash lens skipped once
    "reconcile_skipped_no_broker",   # observe/degrade path — nothing to reconcile against
    "flatten_probe_result",          # flatten-<symbol> probe outcome (terminal/not-found); detail
                                     # "<symbol>:<state|not_found>" — D17 attribution observability;
                                     # NEVER a terminal resolution (M6C-3)
})
```

**Order-state resolution table (closed; consumed by the order lens; one probe per order, FD-M6-9/12;
the watermark comparisons consume the `cum_filled_watermark` input — a FILLS-stream fact the
orchestrator computes at the impure edge, M6C-16; `local = ∅` rows are the flatten probes,
`local_row=None` + `flatten_symbol` set, M6C-3):**

| local state | broker `order_status` result | verdict | action |
|---|---|---|---|
| open/unconfirmed | terminal `filled`, broker `filled_qty` > journaled cum watermark | `fills_missed` (alert-only) + `order_state` | `alert_only` + `resolved_terminal` (position/cash lenses adopt the economics in the same pass; the symbol LEAVES the deferral set — M6C-2) |
| open/unconfirmed | terminal `filled`, cum watermark already equal | `order_state` | `resolved_terminal` |
| open/unconfirmed | terminal `canceled`/`expired`/`rejected`/`done_for_day` | `order_state` | `resolved_terminal` |
| open/unconfirmed | live `accepted`, or `partially_filled` with broker `filled_qty` == cum watermark | no drift (agreement, order still working) | — no row; symbol joins the deferral set (M6C-3) |
| open/unconfirmed | live `partially_filled` with broker `filled_qty` > cum watermark | `fills_missed` (alert-only) | `alert_only`; NO `resolved_terminal` (order still live); symbol stays in the deferral set — position/cash lenses defer (M6C-3) |
| open (confirmed — a broker ACK was journaled) | not-found | corruption-grade `order_state` (a previously-ACKed id going 404 contradicts A3) | `latched_operator`; the order STAYS tracked — NEVER `resolved_terminal` from a 404 on an ACKed order (a transient 404 must not un-track a live order; M6C-3); symbol stays in the deferral set |
| unconfirmed | not-found (404-as-data / `KeyError`) AND the order's DAY-TIF session has ended (`_session_over`, §D) | `order_state` | `resolved_terminal` (terminal_state `"expired"`; pinned kwargs per FD-M6-21/M6C-29) |
| unconfirmed | not-found, session not ended (incl. `UnknownSessionDate` ⇒ not-over, M6C-15) | no drift (presumed-live, FD-M5-17) | — (symbol stays in open-deny AND in the deferral set, M6C-2) |
| open | `unknown` / `pending_cancel` (non-terminal per V33) | no drift | note `order_probe_unknown`; symbol stays in the deferral set |
| terminal locally, open at broker | *(resolver-unit-only row — unreachable through the sweep, which probes only open orders + flatten ids; kept as a defensive resolver case, M6C-3)* | `order_state` | `latched_operator` |
| any | probe raised / non-404 error-as-data | no verdict | note `order_probe_failed`; pass `completed=false`; symbol stays in the deferral set (journaled orders: structurally — the unresolved order remains in `open_orders`, step 4b; flatten probes: explicitly via the PROBE_FAILED branch's `defer.add` — RC-9) |
| ∅ (flatten probe) | terminal (any) OR not-found | no order-lens drift; note `flatten_probe_result` (detail `<symbol>:<state\|not_found>`) — the position lens owns the economics (D17) | — NEVER `resolved_terminal` (no journaled `o-` ids exist for flatten probes, V6 — M6C-3) |
| ∅ (flatten probe) | live / non-terminal | no drift | symbol joins the deferral set via `defer_symbols` (a mid-flight flatten must not be adjusted over — M6C-3) |
| ∅ (flatten probe) | probe raised / non-404 error-as-data | no verdict | note `order_probe_failed`; pass `completed=false`; **symbol joins the deferral set** (enforced at the orchestrator's PROBE_FAILED branch, §D step 4 — a failed probe is LESS information than a live result, and kill-residual symbols appear in NO other step-4b deferral source, so without this arm a possibly-live flatten would be adjusted over — RC-9; mirrors the live row above) |

---

## 4. Module map + import discipline

| Module | Status | Module-scope imports allowed | Forbidden at ANY scope (AST-guarded, §I test 7) |
|---|---|---|---|
| `scripts/agent/broker_reconcile.py` | **NEW, PURE** — vocabularies, `ReconcileError`, diff core, adjustment planner, order-resolution function | stdlib (`dataclasses`, `decimal`, `typing`), `agent.serializer`, `agent.exec_reasons` (ExecError base, TERMINAL_STATES) | `agent.broker.*`, `agent.execution_preflight`, `agent.kill_switch`, `agent.arming`, `submit_order`, `OrderIntent`, `mint_*`, `require_token`, `consume`, `importlib`, `__import__`, wall-clock tokens (`time`, `.now(`, `.utcnow`, `.sleep`, `.monotonic`) |
| `scripts/agent/reconcile_ledger.py` | **NEW** — `ReconcileLedger`, `replay_reconcile`, `rehydrate_reconcile_state` (latch fold) | stdlib, `agent.serializer`, `agent.journal` (exceptions), `agent.broker_reconcile` (vocab), `recorder.persistence` (EventWriter type / replay_stream) | same forbidden family |
| `scripts/agent/exec_ledger.py` | EXTENDED — `EVT_POSITION_ADJUST` + `record_position_adjust` | unchanged | unchanged |
| `scripts/agent/paper_book.py` | EXTENDED — `rehydrate` folds `position_adjust`; live twin `apply_position_adjust` | unchanged | unchanged |
| `scripts/agent/orchestrator.py` | EXTENDED — step 4+ writers, step 6.5 latch fold + detector construction, `run_reconcile`, `_session_edge` EOD hook, detector observe + immediate trigger, `_portfolio_read` stamping, `durable_ids` ctor seam | unchanged budget + `agent.broker_reconcile`, `agent.reconcile_ledger`, `agent.status_ledger` (StatusLedger ctor + `rehydrate_state` frozen fold), `agent.corporate_actions` (`BrokerAdjustDetector`, `DurableId` — M6C-4) | existing wall-clock/sleep AST scan re-applies to the grown file |
| `scripts/agent/__main__.py` | EXTENDED — `reconcile` subcommand; `_cmd_paper` SOD call + exit mapping | unchanged budget (orchestrator/config/secrets_runtime + stdlib, V15) | — |
| `scripts/recorder/reconcile.py` | **UNTOUCHED** (M1 depth sense, FD-M6-16) | — | — |
| `tests/lib/reconcile_fixtures.py` | NEW (tests-side) | — | — |

No edits to: `serializer.py`, `journal.py`, `persistence.py`, `account_state.py`, `can_open.py`,
`reasons.py`, `corporate_actions.py`, `status_ledger.py`, `broker/*`, `execution_preflight.py`,
`arming.py`, `kill_switch.py`, `risk_kill.py`, `run_lock.py`, `fees.py`, `config/*.json`.

---

## A. `scripts/agent/broker_reconcile.py` — the PURE diff engine (code skeleton)

```python
# scripts/agent/broker_reconcile.py
"""M6 broker reconcile: PURE diff core. NOT recorder/reconcile.py (M1 depth
dual-hash) — different concept, deliberately different module (FD-M6-16).
No broker, no I/O, no clock lives here; the orchestrator feeds parsed reads in
and takes planned actions out (FD-M6-1)."""

from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN
from typing import Mapping, Optional, Sequence, Tuple

from agent.exec_reasons import ExecError

class ReconcileError(ExecError): ...

COST_TOLERANCE_PER_SHARE = Decimal("0.005")     # FD-M6-4(b) — code constant, no config (FD-M6-5)
IDENTITY_TOLERANCE_USD   = Decimal("0.01")      # FD-M6-4(d) — note-only check, never a drift verdict
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)

@dataclass(frozen=True)
class DriftFinding:
    kind: str                       # DRIFT_KINDS member
    symbol: Optional[str]           # None only for kind == "cash"
    field: str                      # "qty" | "avg_cost" | "cash" | "order_state" | "fills"
    local: Optional[str]            # canonical Decimal string or closed state token; None = absent
    broker: Optional[str]
    diff: Optional[str]             # broker - local (Decimal string); None when non-numeric
    action: str                     # RECONCILE_ACTIONS member
    position_id: Optional[str] = None
    local_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None

def make_finding(*, kind, symbol, field, local, broker, action,
                 position_id=None, local_order_id=None, broker_order_id=None) -> DriftFinding:
    """THE one typed boundary (M6C-17, FD-M6-10): money values enter TYPED as
    BrokerUSD and are validated via as_broker_usd (a ModeledUSD / float / bool /
    NaN => TypeError, nothing constructed — test 1.23's home); qty values enter
    as plain finite Decimals. The canonical value string is rendered HERE, once
    (the serializer's Decimal rendering, never str(Decimal) ad hoc), and
    diff = broker - local is computed under _DECIMAL_CTX before stringification.
    Lineage is erased at stringification, so the lineage wall CANNOT live at the
    facade — the facade (§B.1a) validates strings structurally only."""

@dataclass(frozen=True)
class PlannedAdjust:
    position_id: str
    symbol: str
    prev_qty: Decimal
    adjusted_qty: Decimal                  # >= 0; 0 closes (status -> "closed")
    prev_broker_cost_usd: "BrokerUSD"
    adjusted_broker_cost_usd: "BrokerUSD"  # symbol-level sum == broker-derived cost

@dataclass(frozen=True)
class ReconcilePassResult:
    reconcile_id: str
    phase: str                            # RECONCILE_PHASES member
    session_date_et: str
    completed: bool                       # False => drift UNKNOWN; latch unchanged (FD-M6-7)
    findings: Tuple[DriftFinding, ...]    # sorted by the None-coerced total key
                                          # (kind, symbol or "", position_id or "", local_order_id or "")
                                          # — M6C-33: Optional[str] slots never reach None-vs-str ordering
    adjustments: Tuple[PlannedAdjust, ...]
    notes: Tuple[tuple, ...]              # (note, symbol_or_None, detail_str), sorted with the
                                          # symbol slot None-coerced to "" (M6C-33)
    clean: bool                           # completed AND findings == () — NECESSARY, not sufficient,
                                          # for a latch clear: the clear additionally requires
                                          # phase ∈ {sod, eod, cli}, a drift-free window, and NO
                                          # frozen symbol (FD-M6-6, §B.1, §D step 7 — RC-11)

def diff_positions(local_positions: Mapping[str, Tuple["PaperPosition", ...]],
                   portfolio: "PortfolioRead", *,
                   frozen_symbols: frozenset,
                   inflight_symbols: frozenset,
                   adjusts_allowed: bool,
                   reconcile_id: str) -> Tuple[Tuple[DriftFinding, ...],
                                               Tuple[PlannedAdjust, ...],
                                               Tuple[tuple, ...]]:
    """PURE. local_positions is keyed by SYMBOL; each value is the tuple of that
    symbol's open PaperPositions PRE-SORTED newest-first by position_open journal
    seq BY THE ORCHESTRATOR (which owns the replay — PaperPosition carries no seq
    field, so the ordering is an input, M6C-11). Aggregates sum(open qty) /
    sum(broker_cost_usd) per symbol (the key-collapse trap: many position_ids ->
    one broker row), compares per FD-M6-4(a)/(b), plans LIFO-cascade adjustments
    per FD-M6-13. Frozen symbols yield findings with action latched_operator;
    deferral-set symbols yield action adjust_deferred + note
    adjust_deferred_inflight; adjusts_allowed=False (immediate phase, M6C-1)
    yields action adjust_deferred for ALL otherwise-adjustable findings — in
    every one of these cases, ZERO plans (FD-M6-8/13)."""

def diff_cash(*, baseline_cash: "BrokerUSD",
              fill_rows_since_watermark: Sequence[Mapping],
              broker_cash: "BrokerUSD",
              reconcile_id: str) -> Optional[DriftFinding]:
    """PURE. fill_id first-occurrence dedupe; exact telescope (FD-M6-4(c)/17);
    buy => -delta_cost_usd, sell => +delta_cost_usd; replayed strings re-wrapped
    via Decimal(str(...)); arithmetic under _DECIMAL_CTX. The orchestrator
    PRE-FILTERS the row window: any row whose fill_id already occurs at
    seq <= the baseline watermark is excluded before this call, so a duplicate
    polling re-read straddling the boundary cannot double-count (M6C-26).
    A residue finding carries action "latched_operator" ("rebaselined" ONLY on
    the operator --rebaseline-cash path, M6C-5)."""

@dataclass(frozen=True)
class TerminalResolution:               # M6C-30 — the record_order_terminal inputs, pinned
    decision_id: str                    # the order's original journaled ids (FD-M6-21)
    order_id: str
    terminal_state: str                 # TERMINAL_STATES member
    filled_qty: Decimal                 # journaled cum watermark for expired-resolutions (M6C-29)
    cum_notional_usd: "BrokerUSD"       # journaled cum notional (Decimal("0") when no fills)
    ts_broker_utc: Optional[str]        # None for not-found resolutions (M6C-29)

@dataclass(frozen=True)
class ProbeResolution:                  # M6C-30 — resolve_order_probe's frozen return shape
    findings: Tuple[DriftFinding, ...]
    notes: Tuple[tuple, ...]
    terminal_resolutions: Tuple[TerminalResolution, ...]
    defer_symbols: Tuple[str, ...]      # symbols this probe adds to the deferral set (M6C-2/3)

def resolve_order_probe(local_row: Optional[Mapping], probe_result, *,
                        cum_filled_watermark: Decimal,
                        session_over: bool,
                        flatten_symbol: Optional[str],
                        reconcile_id: str) -> ProbeResolution:
    """PURE. Implements the §3 order-state resolution table. local_row is the
    order's latest orders-stream row, and is None EXACTLY for flatten probes
    (flatten_symbol then carries the symbol — M6C-3). cum_filled_watermark is
    the FILLS-stream cum fill watermark for this order, computed by the
    orchestrator at the impure edge (max cum_filled_qty per order_id over
    first-occurrence fill_ids) — the orders-stream latest row does NOT carry it
    (M6C-16). probe_result is a parsed BrokerOrder | the NOT_FOUND sentinel |
    the PROBE_FAILED sentinel — the orchestrator wraps the actual probe
    (KeyError / BrokerHttpError-as-data mapping happens THERE, at the impure
    edge). Never invents probes."""

def identity_note(*, equity, cash, market_values) -> Optional[tuple]:
    """FD-M6-4(d): broker-internal identity check; returns the
    broker_internal_inconsistency note tuple or None. Never a drift."""

# Probe-result sentinels (the impure edge maps KeyError / 404-as-data / raises to these;
# the PURE resolver consumes only these three shapes):
NOT_FOUND    = object()      # broker says the client_order_id does not exist
PROBE_FAILED = object()      # probe raised / non-404 error-as-data => pass completed=False
# (the third shape is a parsed BrokerOrder from parse_order_payload — the ONE parser)
```

### A.1 The LIFO adjustment planner (FD-M6-13), spelled out

For one symbol with broker qty `B` (≥ 0; `B < 0` short-circuits to `short_unrepresentable` /
`latched_operator` before planning) and open positions `p1..pn` sorted by `position_open` journal
seq DESCENDING (newest first), local Σ = `L`:

```
delta = B - L
if delta == 0 and cost within tolerance:        no finding, no plan
if symbol in frozen_symbols:                    finding(action=latched_operator); NO plan (FD-M6-8)
if symbol in inflight_symbols                   finding(action=adjust_deferred);
   (the deferral set, M6C-2):                   note adjust_deferred_inflight; NO plan (FD-M6-13)
if not adjusts_allowed                          finding(action=adjust_deferred); NO plan
   (immediate phase, M6C-1):                    (no note — the phase is on the summary row)
if delta > 0:                                   # broker holds MORE than the book
    plan one adjust on p1 (newest):  adjusted_qty = p1.qty + delta
if delta < 0:                                   # broker holds LESS
    remaining = -delta
    for p in (p1, p2, ...):                     # newest first
        take = min(p.qty, remaining)            # clamp: flatten, never flip (qty >= 0 always)
        plan adjust: adjusted_qty = p.qty - take
        remaining -= take
        if remaining == 0: break
    # remaining > 0 after flattening everything is impossible: B >= 0 and sum(takes) <= L
# Cost re-anchoring (M6C-8) rides the NEWEST position that remains OPEN post-plan — never a
# position flattened to 0: set that position's adjusted_broker_cost_usd so that
# sum(open broker_cost_usd over the symbol, post-plan) == avg_entry_price * B (pinned ctx).
# When the cascade exhausts exactly on flattened lots (no TOUCHED position remains open) but
# untouched open lots remain, emit ONE additional cost-only adjust on the newest open lot —
# otherwise cost drift would survive the pass and break the FD-M6-2 fixpoint (M6C-8).
# A sole cost-only adjust (qty already agreeing) targets the NEWEST open position
# (deterministic adjust_id — it keys position_id, M6C-8).
# Pinned values (M6C-28): every position adjusted to qty 0 carries
# adjusted_broker_cost_usd = Decimal("0"); intermediate reduced-but-open positions keep
# prev_broker_cost_usd; only the re-anchor target's cost moves.
# avg_entry_price is None => cost re-anchoring skipped; qty adjusts still apply (open
# survivors keep prev cost, flattened lots carry Decimal("0") — M6C-28, covers D10 where
# the broker row is absent entirely); note cost_unverifiable.
```

The planner is PURE and returns `(findings, plans, notes)`; the orchestrator journals findings,
writes `position_adjust` rows for plans (write-ahead), and commits book state after each row
(§B.3). A `position_qty` finding and its plan(s) share the same pass; a finding with
`action="adjusted"` and zero plans is a contract violation (ledger-validated at the facade is not
possible across rows, so it is pinned by test instead — §I file 1); deferred findings carry
`action="adjust_deferred"` and zero plans, so the pin stays total (M6C-1/2). An adjust that
closes a position deliberately records NO realized-PnL fields — the broker ledger owns the
economics and the drift row is the record (M6C-28).

### A.2 Engine determinism pins

- Every collection in `ReconcilePassResult` is sorted by a **None-coerced** total key —
  `_finding_key = (kind, symbol or "", position_id or "", local_order_id or "")`, note key
  `(note, symbol or "", detail)` — so Optional[str] slots never reach a None-vs-str comparison
  (which raises `TypeError` in Python; M6C-33). No sets reach serialization.
- The engine never sees raw payloads — the orchestrator parses at the chokepoints first (FD-M6-7) —
  so `local`/`broker` value strings are produced by ONE serialization path: the `make_finding`
  typed boundary (`serializer.dumps` rendering of Decimal), never `str(Decimal)` ad hoc (M6C-17).
- Hostile ambient Decimal contexts cannot perturb any output (pinned `_DECIMAL_CTX`, the M4-DET-1 /
  quote_quality precedent) — pinned by a hostile-context test cell.
- `diff` is always computed `broker − local` under the pinned ctx; `None` exactly when either side
  is a non-numeric state token (`order_state` rows).

---

## B. `scripts/agent/reconcile_ledger.py` + journal row shapes

### B.1 `journal/reconcile_alerts.jsonl` (all rows: `"v": 1` first, `rules_hash` last; written via `ReconcileLedger`; no reserved-key payloads — V12; kwarg-only exact field sets, raise-before-write — V16 conventions)

```python
# scripts/agent/reconcile_ledger.py
RECONCILE_LEDGER_VERSION = 1

class ReconcileLedger:
    """Validating facade over EventWriter on journal/reconcile_alerts.jsonl."""
    def __init__(self, writer: "EventWriter", *, rules_hash: str): ...
    def record_reconcile(self, *, reconcile_id, drift_id, kind, symbol, field,
                         local, broker, diff, action, position_id,
                         local_order_id, broker_order_id) -> dict: ...
    def record_reconcile_note(self, *, reconcile_id, note, symbol, detail) -> dict: ...
    def record_reconcile_baseline(self, *, reconcile_id, session_date_et,
                                  cash_usd, equity_usd, buying_power_usd,
                                  fills_seq_watermark, positions,
                                  durable_seeded) -> dict: ...
    def record_reconcile_run(self, *, reconcile_id, phase, session_date_et,
                             trigger_durable_key, broker_source,
                             checked_symbols, drift_count, adjusted_count,
                             note_count, completed, clean) -> dict: ...

def replay_reconcile(path) -> list:      # delegates to recorder.persistence.replay_stream (V12 semantics)

def rehydrate_reconcile_state(rows) -> dict:
    """PURE fold by ascending seq (FD-M6-6). Returns:
       {"latched": bool,           # drift row -> True; a reconcile_run row clears ONLY when
                                   # completed=true AND clean=true AND phase in {sod, eod, cli}
                                   # AND zero drift rows observed since the previous summary
                                   # (immediate summaries never clear, M6C-1; a drift-rows+clean-
                                   # summary window keeps True fail-closed, M6C-22); trailing
                                   # drift rows after the last summary keep True (fail-closed)
        "latest_baseline": Optional[dict],   # latest reconcile_baseline row content (cash telescope input)
        "outstanding_cash_residue": Optional[dict],  # latest kind="cash" drift-row content with
                                             # action "latched_operator" (RC-8); cleared by a
                                             # kind="cash" action="rebaselined" row or by a
                                             # completed=true summary whose window held ZERO
                                             # kind="cash" drift rows (sound: a cash-skipped pass
                                             # RE-JOURNALS an outstanding residue per FD-M6-17, so
                                             # a cash-row-free completed window means evaluated-
                                             # clean or no residue existed); seeds the in-process
                                             # re-journal state at step 6.5. The fold returns the
                                             # ROW CONTENT DICT; the orchestrator seam PROJECTS it
                                             # into a DriftFinding at step 6.5 (RC-14) — inside the
                                             # orchestrator the field is ALWAYS
                                             # Optional[DriftFinding], one shape on every path
        "pass_count": int}                   # ALL prior reconcile_run rows, completed or not
                                             # (occurrence input, §C/M6C-12)"""
```

Row shapes:

- `reconcile` — **the spec row (V14 :347), superset of `{symbol, local, broker, diff, action}`:**
  `{v, reconcile_id, drift_id, kind, symbol, field, local, broker, diff, action, position_id,
  local_order_id, broker_order_id, rules_hash}`. `local`/`broker`/`diff` are canonical Decimal
  strings (BrokerUSD lineage for money) or closed state tokens for `order_state`; nullable fields
  take explicit `None`. Broker order ids ride the payload field `broker_order_id`, NEVER the journal
  `order_id` kwarg (an arbitrary broker UUID as kwarg would fail prefix validation and perturb V4).
- `reconcile_note` — `{v, reconcile_id, note (RECONCILE_NOTES), symbol (nullable), detail (str),
  rules_hash}`.
- `reconcile_baseline` — end of every COMPLETED pass EXCEPT a first-ever pass with a non-empty
  deferral set, which writes none (FD-M6-17/RC-1):
  `{v, reconcile_id, session_date_et, cash_usd, equity_usd, buying_power_usd, fills_seq_watermark,
  positions (sorted [{symbol, qty}]), durable_seeded (sorted durable keys), rules_hash}`. Money
  exact/unquantized (rehydrate-bearing for the telescope — LD-R5 discipline).
- `reconcile_run` — pass summary, written LAST (the commit marker):
  `{v, reconcile_id, phase, session_date_et, trigger_durable_key (None|str), broker_source
  (ACCOUNT_SOURCES member), checked_symbols (sorted list), drift_count, adjusted_count, note_count,
  completed (bool), clean (bool), rules_hash}`. `trigger_durable_key` = the FreezeSignal's
  `durable_id.key()` on immediate passes (from `run_reconcile`'s `trigger` parameter, M6C-1); `None`
  on every other phase. Ledger-validated: `clean=true` requires `completed=true` AND
  `drift_count == 0` (M6C-22). A `completed=true AND clean=true` summary on a `{sod, eod, cli}`
  phase with a drift-free window is the ONLY latch-clearing row (FD-M6-6, M6C-1/22).

### B.1a Per-field validation rules (the `ExecLedger` discipline, V16, applied to the new facade)

| Field | Rule (validated BEFORE any write; violation ⇒ `ReconcileError`/`TypeError`, NO row) |
|---|---|
| `reconcile_id` / `drift_id` / `adjust_id` | non-empty str with the §C prefix (`rc-` / `rd-` / `adj-`) |
| `kind` / `action` / `note` / `phase` | member of the §3 closed set |
| `symbol` | non-empty str, or explicit `None` only where the shape allows (cash rows; non-symbol notes) |
| `field` | member of `{"qty", "avg_cost", "cash", "order_state", "fills"}` (closed) |
| `local` / `broker` / `diff` | canonical Decimal string, closed state token (order_state rows), or explicit `None`; **bool/float/non-finite anywhere ⇒ raise (S2)**. The facade validates STRUCTURALLY only — parseable finite Decimal string / closed token / `None` (M6C-17): lineage is erased at stringification, so the `BrokerUSD`-vs-`ModeledUSD` wall (FD-M6-10) is enforced at the engine's typed boundary `make_finding` (§A — a `ModeledUSD` ⇒ `TypeError` THERE, test 1.23), not here |
| `position_id` / `local_order_id` | `pos-` / `o-`/`synthetic-o-` prefixed when present (the §P.3 prefixes), else explicit `None` |
| `broker_order_id` | plain str or `None` — deliberately UN-prefixed (broker UUIDs / `flatten-<symbol>` ride here, never the journal kwarg) |
| `cash_usd` / `equity_usd` / `buying_power_usd` | `BrokerUSD` via `as_broker_usd`, exact/unquantized round-trip |
| `fills_seq_watermark` | non-negative int (bool rejected); the name is payload-legal — bare `seq` is reserved (V12) |
| `positions` | sorted list of `{"symbol": str, "qty": Decimal-string}`, no duplicates |
| `durable_seeded` / `checked_symbols` / counts | sorted lists / non-negative ints; `drift_count`/`adjusted_count`/`note_count` must equal the rows actually written this pass (pinned by test, not by the facade) |
| `completed` / `clean` | real bools; `clean and not completed` ⇒ raise; `clean and drift_count != 0` ⇒ raise (M6C-22 — a buggy clean summary cannot clear a latch its own window's rows set) |
| `detail` | bounded str (human-facing; never parsed back) |

### B.1b Replay / rehydrate edge semantics (inherited V12, pinned per-case in §I file 2)

- Truncated (unterminated) tail line: dropped on replay; the writer's tail-repair makes the next
  append land on a record boundary.
- Newline-terminated bad line: fatal `JournalCorruption` — the job maps it to exit 3 (FD-M6-19).
- Empty/missing stream: replays `[]`; `rehydrate_reconcile_state([])` ⇒
  `{"latched": False, "latest_baseline": None, "outstanding_cash_residue": None, "pass_count": 0}`.
- Multiple writers on the same resolved path (orchestrator + a prior seeding ledger in tests) share
  one seq via the path-keyed registry (V12) — the fold sorts by `seq`, so interleaving is total.
- Rows from PRIOR runs participate in the fold identically (the latch is cross-run state by design —
  D21).

### B.2 `journal/positions.jsonl` extension (via `ExecLedger`)

- `position_adjust` — `{v, adjust_id, reconcile_id, position_id, symbol, prev_qty, adjusted_qty,
  prev_broker_cost_usd, adjusted_broker_cost_usd, basis: "broker_truth", rules_hash}`.
  `position_id` is a plain payload field; `position_adjust` rows ride NO `order_id`/`decision_id`
  kwargs (they are not order lifecycle rows). **Validation (M6C-7, mirroring the close-fold
  precedent at the ledger):** `record_position_adjust` raises unless `prev_qty >= 0` AND
  `adjusted_qty >= 0` AND both are finite Decimals (bool/float rejected) AND both money fields pass
  `as_broker_usd` lineage — the planner's flatten-never-flip clamp is enforced at the WRITE layer
  too, so no caller/fixture bug can journal a flipping row.
- **Fold rule (`PaperBook.rehydrate`, same change as the event — V5):** `position_close` AND
  `position_adjust` rows are collected in positions-stream seq order and applied AFTER the fills pass
  (their relative order among themselves is the stream seq — total within one stream). For an adjust:
  verify `prev_qty`/`prev_broker_cost_usd` against the folded state by **exact Decimal VALUE
  equality** (the V5 precedent — `!=` on Decimals, value not string: `Decimal("30")` ==
  `Decimal("30.00")` must NOT brick a restart, M6C-20) and raise `ExecError` on mismatch (the same
  self-detecting telescope discipline as `position_close` residual verification, V5); additionally
  raise `ExecError` on (a) an UNKNOWN `position_id` — NEVER create-on-miss, an adjust row cannot
  manufacture a position; (b) `adjusted_qty < 0` — a seeded/buggy row cannot fold a flipped/short
  position; (c) an adjust targeting a position already folded `closed` (M6C-7). Then set
  `qty := adjusted_qty`, `broker_cost_usd := as_broker_usd(BrokerUSD(adjusted_broker_cost_usd))`
  exactly from the row; `modeled_cost_usd` untouched (FD-M6-10);
  `status := "closed" if adjusted_qty == 0 else "open"`; the fills cum watermark unchanged.
  Soundness: adjusts only run for symbols outside the deferral set — which includes presumed-live
  unconfirmed orders (FD-M6-13/M6C-2) — and per-position fills come from exactly one opening order
  (M5 single-in-flight), so every fill that can fold precedes every adjust/close that can fold —
  the prev-value check makes any violation brick loudly instead of rewriting history.
- **Live twin:** `PaperBook.apply_position_adjust(*, position_id, adjusted_qty,
  adjusted_broker_cost_usd, adjust_id, reconcile_id) -> PaperPosition` — write-ahead (ledger row
  first, state commit after), so fold == live stays byte-exact.

### B.3 Write-ahead ordering inside a pass (frozen)

per-finding `reconcile` rows → per-adjust `position_adjust` (exec ledger; book state commits after
its row) → order resolutions (`record_order_terminal`, V11, original journaled ids) →
`reconcile_note` rows → `reconcile_baseline` (COMPLETED passes only; OMITTED on a first-ever pass
with a non-empty deferral set — RC-1; post-adjustment broker truth) →
`reconcile_run` summary LAST. A crash anywhere mid-pass leaves trailing rows without a summary ⇒ the
latch fold reads them fail-closed (FD-M6-6).

---

## C. Deterministic id scheme (S6; all via `serializer.row_hash` over a canonical dict with the EXACT key set)

| id | form | operand dict |
|---|------|--------------|
| `reconcile_id` | `"rc-" + row_hash(...)` | `{"run_id", "phase", "session_date_et", "occurrence"}` — `occurrence` = count of ALL prior `reconcile_run` rows visible at pass start: the rehydrated `pass_count` (all summaries, completed or not) + every summary written this run, **completed or NOT** (M6C-12 — the live counter increments after EVERY summary write, so consecutive incomplete immediate passes mint distinct ids; immediate passes can repeat within a session) |
| `drift_id` | `"rd-" + row_hash(...)` | `{"reconcile_id", "kind", "symbol", "field", "position_id", "local_order_id"}` (None rendered as JSON null) |
| `adjust_id` | `"adj-" + row_hash(...)` | `{"reconcile_id", "position_id"}` — one adjust per position per pass, structurally |

Same inputs + same `run_id` + injected row clock ⇒ byte-identical streams (the existing ledger
determinism pin).

---

## D. Orchestrator wiring (deltas only; frozen step orders preserved)

**Startup (V7; additions are sub-steps):**

1. **Step 4+** — construct `EventWriter(journal_dir / "reconcile_alerts.jsonl", run_id,
   **writer_kwargs)` + `ReconcileLedger(rules_hash=self.rules_hash)` alongside the existing writers
   (orchestrator.py:450-479 pattern; no file is created until first write, V30), and
   `self._status_ledger = StatusLedger(<writer over the SAME resolved status.jsonl path>,
   ...)` for the freeze row (shared lock/seq via the path-keyed registry, V12 — §1 row 18).
2. **Step 6.5 (NEW, PURE — no broker call; M5C-S2 untouched)** —
   `state = rehydrate_reconcile_state(replay_reconcile(...))`; seed `self._latest_baseline`,
   `self._pass_count`, and `self._outstanding_cash_residue = _residue_from_row(state["outstanding_cash_residue"])`
   (the latest unadjudicated cash residue — drives the RC-8 skip-pass re-journal; rehydrated, so a
   restart cannot forget a known residue). `_residue_from_row(row: Optional[dict]) -> Optional[DriftFinding]`
   (RC-14) is a pure None-propagating projection: it constructs `DriftFinding` directly from the
   row's `kind/symbol/field/local/broker/diff/action` (+ `position_id`/`local_order_id`/
   `broker_order_id` when present) — direct dataclass construction, NOT `make_finding` (the row's
   money values are already-validated canonical Decimal STRINGS; no typed boundary is re-crossed).
   This pins ONE shape for the field on every path: step 5's in-process assignment stores the
   `DriftFinding` it just journaled, step 6.5 projects the rehydrated row into the same shape, and
   `_carried_residue_finding(residue: DriftFinding, reconcile_id) -> DriftFinding` (fresh
   `drift_id`, byte-identical `local/broker/diff`) consumes exactly that one type. The carried
   values are byte-identical under either representation — determinism-neutral (RC-14). Also `self._reconciled_eod_sessions: set = set()`,
   `self._durable_ids = dict(durable_ids or {})`, `self._observe_suspended: set = set()`,
   `self._noted_durables: set = set()` (per-run `durable_id_missing` dedupe — M6C-27), and
   `self._detector = BrokerAdjustDetector()` — constructed ONCE here, NEVER rebuilt (a rebuild
   would lose the in-memory frozen set and silently un-freeze CA-frozen symbols — M6C-4).
   Frozen state survives restarts: `self._frozen_durables` re-derives from the status-stream
   `broker_adjust_freeze` fold (`status_ledger.rehydrate_state` over the replayed status rows,
   V24 — a pure file replay), and `_frozen_symbols()` = the mapped symbols of (detector-frozen ∪
   `self._frozen_durables`) (M6C-4). `self._drift_latch = state["latched"] or
   bool(self._frozen_durables)` — a frozen durable holds the latch across restarts, fail-closed
   (M6C-1). A pre-existing latch therefore blocks opens from the FIRST tick, before any broker
   contact.
3. **Steps 9/10 unchanged. No reconcile pass runs in `__init__`** (FD-M6-12, §1 row 4).
4. **New ctor seam:** `durable_ids: Optional[Mapping[str, DurableId]] = None` (default empty ⇒
   detector exempt ⇒ byte-identical M5 behavior, A5).

**`run_reconcile(*, phase: str, ts_utc: str, now_ms: int, trigger: Optional[FreezeSignal] = None)
-> ReconcilePassResult`** (exported; the impure composer; `trigger` is the FreezeSignal on immediate
passes — M6C-1):

1. `phase ∈ RECONCILE_PHASES` (else `ReconcileError`); `session_date_et =
   self._calendar.session_date_for(ts_utc)` (total, V29); mint `reconcile_id` (§C).
2. No broker AND no account provider ⇒ note `reconcile_skipped_no_broker` + summary
   `completed=false` + return (latch untouched).
3. Fresh reads via `_provider_payloads()` (V31). isinstance-gate BOTH payloads (`Mapping` / `list`)
   BEFORE parsing (V8 trap); parse via `parse_account_payload` / `parse_positions_payload` with
   `source=self._account_source`. Any defect ⇒ note `broker_read_failed` ⇒ summary
   `completed=false` ⇒ return (FD-M6-7). **Never** `AccountStore.put`, never an F4 row, never
   `latest_unsafe()` (§1 row 11; spy-asserted).
4. Order sweep: probe `order_status(order_id)` once per rehydrated/live open order (incl.
   presumed-live unconfirmed, V4) + `order_status(f"flatten-{s}")` for
   `self._risk_kill.residual_symbols()` (FD-M6-9). Each probe wrapped: `KeyError` / 404-shaped
   `BrokerHttpError`-as-data ⇒ NOT_FOUND; any other raise/error-as-data ⇒ PROBE_FAILED (note
   `order_probe_failed`, pass `completed=false` at summary; a failed FLATTEN probe additionally
   adds its symbol to the deferral set — kill-residual symbols appear in NONE of step 4b's
   sources, so this arm is the only thing keeping a possibly-live flatten un-adjusted-over;
   failed journaled orders defer structurally via `open_orders` — RC-9). Resolve via `resolve_order_probe`
   (§3 table) with `cum_filled_watermark` computed from the replayed fills rows (M6C-16) and
   `flatten_symbol` set / `local_row=None` for flatten probes (M6C-3); `session_over` per the
   pinned M6C-15 algorithm below for journaled `o-`/`synthetic-o-` orders ONLY (the calendar
   fixture; no wall clock) and constant `False` on the flatten branch — flatten ids have no
   `order_submit_attempt` row to consult and the §3 flatten rows never read it (RC-4).
5. Compute the DEFERRAL SET (M6C-2): symbols of every order still in `open_orders` after the sweep
   (NOT resolved terminal this pass — incl. presumed-live unconfirmed) ∪ the live task's symbol ∪
   `_open_deny` members ∪ the probes' `defer_symbols` ∪ symbols of FAILED flatten probes (RC-9).
   Position + cost lenses (`diff_positions` —
   frozen symbols from `_frozen_symbols()` (detector ∪ rehydrated, M6C-4), `inflight_symbols` =
   the deferral set, `adjusts_allowed = (phase != "immediate")` — M6C-1); cash lens (`diff_cash`)
   when a baseline exists AND the deferral set is empty (notes `baseline_seeded` /
   `cash_skipped_inflight` otherwise), over the pre-filtered window (M6C-26); a cash-SKIPPED pass
   with an OUTSTANDING residue additionally RE-JOURNALS the carried residue finding (byte-identical
   `local`/`broker`/`diff`, FD-M6-17 — RC-8); identity note
   (FD-M6-4(d)). The moment findings become non-empty, `self._drift_latch = True` — BEFORE any
   journal write (M6C-24).
6. Write-ahead journal sequence per §B.3; book commits after its `position_adjust` rows; the
   plan-write loop is gated `phase != "immediate"` (NORMATIVE — FD-M6-8/M6C-1; immediate passes
   carry zero plans anyway via `adjusts_allowed=False`). The baseline row's (cash, watermark) pair
   refreshes only on a clean cash evaluation, else carries forward (M6C-5); a first-ever pass with a
   non-empty deferral set writes NO baseline row — there is no pair to carry forward (RC-1).
   `self._pass_count += 1` after EVERY summary write, completed or not (M6C-12).
7. State commits after the journal writes (clears only — sets already happened pre-write, M6C-24):
   the latch clears iff `completed AND not findings AND phase != "immediate" AND not
   _frozen_symbols()` (M6C-1); `self._latest_baseline` (completed passes); `seed_baseline` ONLY for
   mapped held symbols whose local folded qty equals the broker qty — skipping frozen, deferred and
   broker-only symbols, and skipped entirely on immediate passes (FD-M6-14/M6C-9); clear
   `self._observe_suspended` for re-seeded symbols.
8. Return the result. **No `submit_order`, no cancel, no token mint anywhere in this method**
   (test-asserted at the spy boundary).

Ordered pseudocode (the build target; numbers match the steps above):

```python
def run_reconcile(self, *, phase: str, ts_utc: str, now_ms: int,
                  trigger: Optional["FreezeSignal"] = None) -> ReconcilePassResult:
    if phase not in RECONCILE_PHASES:
        raise ReconcileError(f"out-of-vocab reconcile phase: {phase!r}")
    session_date_et = self._calendar.session_date_for(ts_utc)          # total, V29
    reconcile_id = _mint_reconcile_id(self.run_id, phase, session_date_et,
                                      self._pass_count)                # §C (counts ALL summaries, M6C-12)
    notes, findings, plans, resolutions = [], [], [], []
    defer: set = set()
    completed = True

    # (1b) immediate trigger: synthesize the finding from the SIGNAL, never the re-read (M6C-1)
    if phase == "immediate":
        findings.append(make_finding(kind="ca_silent_adjust", symbol=trigger.symbol,
                                     field="qty", local=trigger.prev_qty,    # typed Decimals in
                                     broker=trigger.curr_qty,
                                     action="frozen_immediate"))
        self._drift_latch = True       # latch commits BEFORE any journal write (M6C-24)

    # (2) read-source guard ---------------------------------------------------
    if self.broker is None and self._account_provider is None:
        notes.append(("reconcile_skipped_no_broker", None, self.mode))
        return self._finish_pass(..., completed=False)                 # summary; latch untouched

    # (3) fresh reads through the chokepoints (FD-M6-7/8) ---------------------
    account_payload, positions_payload = self._provider_payloads()
    if not isinstance(account_payload, Mapping) or not isinstance(positions_payload, list):
        notes.append(("broker_read_failed", None, "payload shape"))
        return self._finish_pass(..., completed=False)
    parsed_account = parse_account_payload(account_payload, source=self._account_source,
                                           seen_at_ms=now_ms, ts_read_utc=ts_utc)
    if isinstance(parsed_account, AccountInvalid):
        notes.append(("broker_read_failed", None, parsed_account.reason)); completed = False
    try:
        portfolio = parse_positions_payload(positions_payload, source=self._account_source,
                                            seen_at_ms=now_ms, stale=False)
    except ValueError as exc:
        notes.append(("broker_read_failed", None, str(exc))); completed = False
    if not completed:
        return self._finish_pass(..., completed=False)
    # NOTE: deliberately NO AccountStore.put / F4 row / latest_unsafe (§1 row 11).

    # (4) order sweep (FD-M6-9/21; one probe per id) ---------------------------
    open_orders = self._rehydrated_open_orders()        # V4 fold over orders.jsonl + live task
    cum_marks = self._fills_cum_watermarks()            # impure edge: max cum_filled_qty per
                                                        # order_id, first-occurrence fill_id (M6C-16)
    probe_ids = sorted(open_orders) + [f"flatten-{s}" for s
                                       in sorted(self._risk_kill.residual_symbols())]
    for probe_id in probe_ids:
        result = self._wrapped_probe(probe_id)          # BrokerOrder | NOT_FOUND | PROBE_FAILED
        if result is PROBE_FAILED:
            notes.append(("order_probe_failed", None, probe_id)); completed = False
            if probe_id not in open_orders:             # FAILED flatten probe: the symbol MUST join
                defer.add(probe_id.removeprefix("flatten-"))  # the deferral set — kill residuals
                                                        # reach no step-4b source, and a possibly-
                                                        # live flatten must not be adjusted over
                                                        # (RC-9; failed journaled orders defer
                                                        # structurally via open_orders below)
            continue
        is_flatten = probe_id not in open_orders        # flatten ids never appear there (V6, M6C-3)
        verdicts = resolve_order_probe(
            None if is_flatten else open_orders[probe_id], result,
            cum_filled_watermark=cum_marks.get(probe_id, Decimal("0")),
            session_over=(False if is_flatten                          # flatten ids have no
                          else self._session_over(probe_id, ts_utc)),  # order_submit_attempt row
                                                                       # (RC-4); M6C-15 algorithm
                                                                       # (below) for journaled ids
            flatten_symbol=probe_id.removeprefix("flatten-") if is_flatten else None,
            reconcile_id=reconcile_id)
        findings += verdicts.findings; notes += verdicts.notes
        resolutions += verdicts.terminal_resolutions    # TerminalResolution rows (M6C-29/30)
        defer.update(verdicts.defer_symbols)

    # (4b) the DEFERRAL SET (M6C-2): every order still open after the sweep ----
    resolved_ids = {r.order_id for r in resolutions}
    defer.update(self._order_symbol(row) for oid, row in open_orders.items()
                 if oid not in resolved_ids)            # incl. presumed-live unconfirmed (V4)
    defer.update(self._open_deny)
    if self._task is not None:
        defer.add(self._task.symbol)

    # (5) position / cost / cash / identity lenses ----------------------------
    f, p, n = diff_positions(self._open_positions_by_symbol(),   # symbol -> newest-first tuple (M6C-11)
                             portfolio,
                             frozen_symbols=self._frozen_symbols(),   # detector ∪ rehydrated (M6C-4)
                             inflight_symbols=frozenset(defer),
                             adjusts_allowed=(phase != "immediate"),  # M6C-1
                             reconcile_id=reconcile_id)
    findings += f; plans += p; notes += n
    cash_clean = False
    if self._latest_baseline is None:
        if defer: notes.append(("cash_skipped_inflight", None, ""))   # first seed needs quiet (M6C-5);
                                                                      # NO baseline row this pass (RC-1)
        else:     notes.append(("baseline_seeded", None, "")); cash_clean = True
    elif defer:
        notes.append(("cash_skipped_inflight", None, ""))             # pair carries forward (M6C-5)
        if self._outstanding_cash_residue is not None:                # a KNOWN residue holds through
            findings.append(_carried_residue_finding(                 # a skip: RE-JOURNAL it with
                self._outstanding_cash_residue, reconcile_id))        # byte-identical local/broker/
                                                                      # diff (fresh drift_id) — blocks
                                                                      # clean=true and both clear
                                                                      # paths (RC-8, FD-M6-17)
    else:
        rows = self._fills_since_watermark_prefiltered()   # drops fill_ids already seen at
                                                           # seq <= watermark (M6C-26)
        cash_finding = diff_cash(baseline_cash=..., fill_rows_since_watermark=rows,
                                 broker_cash=parsed_account.cash,     # M6C-18
                                 reconcile_id=reconcile_id)
        if cash_finding is None:
            cash_clean = True
            self._outstanding_cash_residue = None        # evaluated CLEAN — residue gone at the
                                                         # broker (RC-8)
        else:
            findings.append(cash_finding)
            self._outstanding_cash_residue = cash_finding   # commits pre-write with the latch
                                                            # (M6C-24/RC-8); on the operator
                                                            # --rebaseline-cash path the row carries
                                                            # action="rebaselined" and the residue
                                                            # clears instead (§E)
    idn = identity_note(equity=..., cash=..., market_values=...)
    if idn is not None: notes.append(idn)
    if findings:
        self._drift_latch = True       # in-process latch commits BEFORE the writes (M6C-24)

    # (6) write-ahead journal sequence (§B.3) ----------------------------------
    for finding in sorted(findings, key=_finding_key):   # None-coerced total key (M6C-33)
        self._reconcile_ledger.record_reconcile(...)
    if phase != "immediate":                             # NORMATIVE gate, not a comment (M6C-1)
        for plan in plans:                               # (zero plans on immediate anyway)
            self._exec_ledger.record_position_adjust(...)    # row FIRST
            self._book.apply_position_adjust(...)            # state AFTER (live twin)
    for res in resolutions:
        self._exec_ledger.record_order_terminal(...)     # pinned TerminalResolution fields (M6C-29)
    for note in sorted(notes, key=_note_key):            # None-coerced key (M6C-33)
        self._reconcile_ledger.record_reconcile_note(...)
    wrote_baseline = completed and (self._latest_baseline is not None or not defer)
    if wrote_baseline:
        # cash_usd / fills_seq_watermark = FRESH broker truth iff cash_clean, else the
        # previous baseline's pair byte-identical (carry-forward — M6C-5); the row shape
        # is unchanged, the VALUES encode the policy. A FIRST-EVER pass with a non-empty
        # deferral set writes NO baseline row — there is no previous pair to carry forward
        # and a torn fresh pair must never seed (RC-1); the first quiet completed pass seeds.
        self._reconcile_ledger.record_reconcile_baseline(...)
    summary = self._reconcile_ledger.record_reconcile_run(
        ..., trigger_durable_key=(trigger.durable_id.key() if trigger else None),  # M6C-1
        completed=completed, clean=completed and not findings)
    self._pass_count += 1               # EVERY summary consumes an occurrence (M6C-12)

    # (7) state commits (clears only — sets already happened pre-write, M6C-24) -
    if (completed and not findings and phase != "immediate"
            and not self._frozen_symbols()):             # phase-restricted, frozen-blocked (M6C-1)
        self._drift_latch = False
    if completed:
        if wrote_baseline:
            self._latest_baseline = ...                  # tracks the row actually written (RC-1)
        if phase != "immediate":                         # immediate seeds NOTHING (M6C-9)
            for symbol, durable in sorted(self._durable_ids.items()):
                if (portfolio.qty_for(symbol) != 0
                        and symbol not in self._frozen_symbols()   # frozen never re-seeded (M6C-9)
                        and symbol not in defer                    # deferred = unreconciled (M6C-9)
                        and self._local_folded_qty(symbol) == portfolio.qty_for(symbol)):
                    self._detector.seed_baseline(durable, portfolio.qty_for(symbol))
                    self._observe_suspended.discard(symbol)
    return ReconcilePassResult(...)
```

(The skeleton is normative for ORDER and for the forbidden-call set; naming of private helpers is
the builder's. `_wrapped_probe` is the ONLY place `KeyError` / `BrokerHttpError`-as-data mapping
happens. **`_session_over`, pinned (M6C-15):** `session_over = (the order's `order_submit_attempt`
session date < `session_date_et`) OR (equal AND the recorded instant ≥ that session's
`rth_close_utc`, via exactly ONE `schedule_for(order_session_date)` consult — the only schedule
consult on the reconcile path; `UnknownSessionDate` is caught and maps to session-NOT-over, i.e.
presumed-live, fail-closed toward keeping the order tracked rather than fabricating an expiry).
Scope (RC-4): `_session_over` is defined ONLY for journaled `o-`/`synthetic-o-` order ids — it
consults the order's `order_submit_attempt` row; flatten ids never appear in the orders stream as
journal ids (V6), so the orchestrator passes `session_over=False` on the flatten branch and the §3
flatten rows never consult it.
No wall clock. This supersedes rev 1's "never calls `schedule_for`" claim — see §0.1.)

**Tick loop (frozen §M.3 step order unchanged; additions inside existing steps):**

- **Step 3 (`_refresh_account`)** — after a successful positions parse: for each mapped + seeded +
  not-frozen + not-suspended symbol with no in-flight order, `observe_broker_qty(durable,
  portfolio.qty_for(symbol), blacked_out=False)`; if the local folded book qty no longer equals the
  seeded baseline (the agent traded), suspend observation for that symbol until the next reconciled
  point re-seeds (FD-M6-14). On a `FreezeSignal`:
  `ts = self._instant_utc or "1970-01-01T00:00:00.000000Z"` (the existing `_refresh_account`
  fallback precedent, orchestrator.py:900 — `self._instant_utc` initializes to `None` and a
  pre-instant freeze must not crash `session_date_for` mid-tick, M6C-34); then
  `self._status_ledger.record_broker_adjust_freeze(freeze_signal=sig,
  instrument_id=self._instrument_ids[symbol], ts_market_utc=ts)` then
  `self.run_reconcile(phase="immediate", ts_utc=ts, now_ms=now_ms, trigger=sig)` — same tick,
  spec :177-178; the `trigger` carries the signal's durable key + prev/curr qty into the
  synthesized finding and the summary (M6C-1).
- **`_portfolio_read`** — one line: `replace(self._portfolio, stale=...,
  unreconciled_drift=self._drift_latch)` (V1/V8 — the only stamping point; `can_open` unchanged, V3).
- **Step 10 (`_session_edge`)** — after the best-effort cancel, before the existing margin block
  (and independent of its `latest_unsafe()` early-return, V9):
  `if self.mode == "paper" and session_date_et not in self._reconciled_eod_sessions and
  (self.broker is not None or self._account_provider is not None):
  self._reconciled_eod_sessions.add(session_date_et);
  self.run_reconcile(phase="eod", ts_utc=self._instant_utc, now_ms=now_ms)`. The `mode == "paper"`
  gate mirrors the step-10 recovery precedent (V7) and is LOAD-BEARING (RC-2): synthetic
  compositions DO have a broker (FakeBroker), so a broker-presence check alone would not exclude
  them — an ungated hook would run an automatic pass in any synthetic/observe run crossing a
  leaving-RTH edge, contradicting §1 rows 5/16 and §I test 8, and advancing FakeBroker's scripted
  fill slices per probe (V18, the §1-row-12 side effect). Tests may still call `run_reconcile`
  explicitly against FakeBroker (§1 row 5). The hook DOES fire inside existing paper-mode
  edge-crossing tests — owned explicitly in the W4 gate (§J, RC-2). Half-days/DST correct
  for free (the edge derives from fixture-supplied `rth_close_utc`, V29).

**Gates interaction:** every pass runs regardless of run-gates (FD-M6-1); the latch only ever blocks
OPENS (`portfolio_unreconciled`); reduce/cancel/kill paths gain zero gates (FD-M6-20).

---

## E. CLI surface (`agent reconcile`), exit codes, ops story

```
agent reconcile [--journal-dir journal] [--calendar-fixture ...] [--overlay ...] [--rebaseline-cash]
```

`--rebaseline-cash` (M6C-5): the ONLY producer of action `rebaselined` — the operator path for an
adjudicated cash residue. The pass runs normally; when (and only when) the cash lens finds a
residue, the drift row carries `action="rebaselined"` instead of `latched_operator` and the
baseline's (cash, watermark) pair refreshes to broker truth; `self._outstanding_cash_residue`
clears (the residue is adjudicated — later cash-skipped passes re-journal nothing, RC-8); the
latch still requires the NEXT
clean pass to clear, and the exit code is still 1 (drift was found). Without the flag, an
unexplained residue carries the baseline forward unchanged and re-latches every pass — including
cash-skipped passes, via the RC-8 carried-residue re-journal (A1, FD-M6-17).

`_cmd_reconcile` (mirrors `_cmd_paper`, V15): committed config + tighten-only overlay;
`credentials_path=.secrets/alpaca_paper.json`, `run_gates_path=.secrets/run_gates.json` (read for
provenance only — the pass ignores gate values, FD-M6-1); construction wrapped in
`try/except RunLockHeld ⇒ stderr + return 2` (FD-M6-22); derived mode must be `"paper"` — the
credentials-missing degrade-to-observe ⇒ the pass notes `reconcile_skipped_no_broker` ⇒ return 3.
Then `result = orch.run_reconcile(phase="cli", ts_utc=<CLI wall UTC ISO>, now_ms=0)`; the summary
fields print to stdout; `orch.close()` in `finally`. Wall time enters only via `_new_run_id` and the
`ts_utc` argument, as today (V15).

Return mapping (FD-M6-11): `0` completed + clean + latch clear · `1` drift found this pass OR latch
still set (adjusted-is-still-1) · `2` usage / mode mismatch / `RunLockHeld` · `3` not completed
(broker unreadable / degrade / `JournalCorruption`). Precedence (RC-13): `completed=false` ⇒ 3
takes precedence over 1 regardless of findings or latch state — could-not-fully-check outranks
drift-found; the journaled+latched drift re-surfaces as exit 1 on the next completed pass; both
codes alert per M6C-25. The same rule governs `_cmd_paper`'s SOD mapping below.

`_cmd_paper` additionally calls `orch.run_reconcile(phase="sod", ts_utc=<CLI wall>, now_ms=0)` after
construction when `orch.mode == "paper"`; it returns 1 when that pass found drift or the rehydrated
latch is set, and **3 when that pass returns `completed=false`** (broker unreadable / parse failure
at SOD — could-not-check must not collapse into "startup ok"; the FD-M6-7 three-outcome discipline
holds on the startup path, M6C-23); the degraded-observe path skips the call (mode note already
journaled) and keeps today's return value. `_cmd_paper` gains the same
`try/except RunLockHeld ⇒ stderr + exit 2` wrapper as `_cmd_reconcile` (today `RunLockHeld` is
uncaught — an unhandled traceback; M6C-21).

**Ops story (runbook-only — no dashboard work in M6):** `docs/runbooks/m6-reconcile.md` — nightly
cron `python3 -m agent reconcile`; **alert on ANY non-zero exit (M6C-25)**: exit 1 (drift: read the
`reconcile_alerts.jsonl` tail), exit 3 (could not reconcile: lock-free retry / creds / broker), and
exit 2 with its own runbook line (a persistently held lock — hung agent process or stale-malformed
`.lock`, V22 reclaims only DEAD pids — means reconciliation has silently stopped happening;
remediation: check for a live/hung agent pid, inspect and clear a malformed `.lock` by hand);
exit-code table; the "EOD edge never fires on a truncated run" gap (V9) is closed operationally by
this job + the SOD pass at next startup (A4). The latch is visible as the `reconcile_run.clean/completed` pair and as
`portfolio_unreconciled` reject reasons in `risk.jsonl`.

---

## F. Config — no new block; the code-constants table (FD-M6-5)

| Constant | Value | Home | Polarity note |
|---|---|---|---|
| `COST_TOLERANCE_PER_SHARE` | `Decimal("0.005")` | `broker_reconcile.py` | larger = looser ⇒ must not be JSON (the FD-M5-29 discipline, V20) |
| `IDENTITY_TOLERANCE_USD` | `Decimal("0.01")` | `broker_reconcile.py` | note-only check; still a constant, same discipline |
| qty tolerance | none — exact Decimal compare | structural | the position-of-record dimension admits no tolerance |
| cash tolerance | none — exact telescope | structural | fee-free paper (V23/A1); §1 row 10 |
| order probes per pass | `1` (the M5 ×3 recovery already ran at step 10) | structural | §1 row 12 |

Committed `config/agent_rules.json` / `config/risk_rules.json`: **byte-unchanged.** `rules_hash`:
unchanged. Committed goldens: unchanged (§1 row 16; pinned by §I test 5).

---

## G. Fixture plan

- Reuse `FakeAccountProvider` (V25) for scripted drifted `positions_payloads` / `account_payloads`
  through the orchestrator's provider seam, and `AlpacaPaperBroker(order_api=ScriptedOrderApi)` +
  committed `tests/fixtures/alpaca/*.json` builders for the full wire path: drifted `list_positions`
  / `get_account`, terminal/404 `get_by_client_order_id` scripts, `BrokerTimeout` injection.
  FakeBroker itself cannot present drift (V18) — drift is injected ONLY at these two existing seams
  for orchestrator-level tests.
- **Third sanctioned seam — CLI-path tests ONLY (M6C-13):** `_cmd_paper`/`_cmd_reconcile` hardcode
  `credentials_path=_SECRETS / "alpaca_paper.json"` (`__main__.py:45,211`) and the orchestrator's
  paper branch constructs `AlpacaPaperBroker` internally from credentials, so neither existing seam
  is reachable from the CLI composition. File-5 tests therefore (a) patch `__main__._SECRETS` to a
  tmp secrets root holding a fake `alpaca_paper.json` (+ optional `run_gates.json`), and (b) patch
  **`agent.broker.alpaca.AlpacaPaperBroker`** — the SOURCE-module attribute (RC-12) — to a factory
  that **ACCEPTS the orchestrator's `credentials_loader` kwarg, DISCARDS it, and returns a real
  `AlpacaPaperBroker(order_api=ScriptedOrderApi(...))`** (the factory closes over the real class
  BEFORE patching) — `order_api` only, NEVER both kwargs: the real ctor raises `ValueError` on the
  pair (`broker/alpaca.py:96-107`, "order_api OR credentials_loader, not both"), and the paper
  branch calls with `credentials_loader=` (orchestrator.py:561-562), so the factory must absorb
  that kwarg (RC-7). The target module matters (RC-12): the orchestrator has **no module-scope
  `AlpacaPaperBroker` name** — its only occurrence is the function-local lazy import inside the
  paper ctor branch (`from agent.broker.alpaca import AlpacaPaperBroker  # lazy`,
  orchestrator.py:560), which re-resolves `agent.broker.alpaca.AlpacaPaperBroker` at CALL time;
  setting an `agent.orchestrator.AlpacaPaperBroker` attribute would be a dead binding the paper
  branch never reads, and the REAL ctor would then run `_build_real_client` →
  `from alpaca.common.exceptions import APIError` (`broker/alpaca.py:177-195`) — an `ImportError`
  in the stdlib-only offline suite. So
  `unittest.mock.patch("agent.broker.alpaca.AlpacaPaperBroker", factory)` (or equivalent) routes
  the full CLI composition onto scripted offline wire payloads (no real `.secrets` read, no
  network). This is the PINNED mechanism; the ONLY two sanctioned patch points are
  `__main__._SECRETS` and `agent.broker.alpaca.AlpacaPaperBroker` — no other monkeypatching of
  `__main__`/orchestrator/broker internals is sanctioned.
- Prior-run journal seeding via the `_seed_dangling_order` pattern (V26): separate
  `ExecLedger`/`ReconcileLedger` with `run_id="run-prior"` + fixed `_ROW_CLOCK` fabricate
  journal-vs-broker divergence, pre-existing baselines, and pre-existing latches before constructing
  the orchestrator under test.
- New `tests/lib/reconcile_fixtures.py`: `drifted_positions_payload(...)` /
  `drifted_account_payload(...)` builders (anchored on the committed
  `tests/fixtures/alpaca/positions_paper.json` field names); `seed_prior_run_book(...)`;
  `seed_prior_reconcile(...)`; `ReconcilePipeline` = `ExecPipeline` + `durable_ids=` passthrough +
  `run_reconcile(...)` convenience.
- One new committed fixture file: `tests/fixtures/alpaca/order_flatten_filled.json` (a terminal
  `flatten-AAPL` payload for the FD-M6-9 kill-flatten probe).
- Byte-goldens: existing committed goldens are NOT regenerated (§1 row 16). New byte pins for
  `reconcile_alerts.jsonl` live in the new test files under the frozen
  `GOLDEN_RUN_ID`/`FIXED_WRITER_TS` regime; regeneration by frozen helpers only, bytes copied, never
  hand-edited.

---

## H. Drift-injection test matrix (S5 — every row is at least one pinned test)

| # | Injection | Kind | Action | Latch after pass | CLI exit |
|---|---|---|---|---|---|
| D1 | broker qty 12 vs local Σ 10 (one open position) | `position_qty` | `adjusted` (+2 to newest) | set; clears on next clean pass | 1 |
| D2 | qty delta across two open positions (LIFO cascade + flatten-never-flip clamp) | `position_qty` | `adjusted` ×2 | set | 1 |
| D3 | avg cost off by > qty×0.005 | `position_avg_cost` | `adjusted` | set | 1 |
| D4 | avg cost off by ≤ qty×0.005 (boundary: exactly equal ⇒ match) | — none | — | unchanged | 0 |
| D5 | `avg_entry_price` absent | — note `cost_unverifiable` | — | unchanged | 0 |
| D6 | broker cash ≠ telescope (baseline seeded, no fills since) | `cash` | `latched_operator` (single token — M6C-10); the baseline (cash, watermark) pair carries FORWARD unchanged (M6C-5) ⇒ the residue re-detects every subsequent pass (cash-skipped passes re-journal the carried residue — RC-8, D34) | set, HOLDS until the operator `--rebaseline-cash` run (`rebaselined`) or the residue disappears at the broker | 1 |
| D7 | broker equity ≠ cash + ΣMV (cash itself consistent) | — note `broker_internal_inconsistency` | — | unchanged | 0 |
| D8 | equity/buying_power perturbed, cash + identity consistent | — nothing fires (provenance-only, FD-M6-4(d)) | — | unchanged | 0 |
| D9 | broker holds a symbol unknown to the book | `position_unknown_broker` | `latched_operator` (no fabricated position) | set, holds | 1 |
| D10 | book open, broker flat (absent row = qty 0, V1/V2) | `position_missing_broker` | `adjusted` to 0 (status closed; `adjusted_broker_cost_usd = Decimal("0")` — no broker row ⇒ no `avg_entry_price` ⇒ no re-anchor, M6C-28) | set | 1 |
| D11 | broker qty −5 (short) | `short_unrepresentable` | `latched_operator` | set, holds | 1 |
| D12 | local open order, broker `filled`, cum watermark behind | `fills_missed` + `order_state` (+ the position drift row that ADOPTS the qty; when a baseline exists, also a `cash` residue row — DETECTED, never machine-resolved, RC-5) | `alert_only` + `resolved_terminal` (+ `adjusted` — the terminal resolution removes the symbol from the deferral set THIS pass, so the adjust runs same-pass; M6C-2 vs D23/D28) + `latched_operator` on the cash row | set; the qty arm machine-resolves same-pass, but the cash residue persists per M6C-5 — it re-detects every subsequent pass against the unrefreshed baseline until the operator `--rebaseline-cash` (deliberate, fail-closed — RC-5; test 4.14) | 1 |
| D13 | local open order, broker `canceled` | `order_state` | `resolved_terminal` | set | 1 |
| D14 | unconfirmed order, broker 404, session over (incl. same-session post-`rth_close_utc`, the pinned `_session_over` consult — M6C-15) | `order_state` | `resolved_terminal("expired")` (pinned kwargs, M6C-29) | set | 1 |
| D15 | unconfirmed order, broker 404, session live | — none (presumed-live, FD-M5-17) | — | unchanged | 0 |
| D16 | broker status `replaced` → maps `unknown` | — note `order_probe_unknown` | — | unchanged | 0 |
| D17 | kill HALTED with residual; broker filled `flatten-AAPL`, close unbooked | `position_qty` via the position lens, attributed by the flatten probe (note `flatten_probe_result`, NEVER a terminal resolution — M6C-3; V6; an `"o-"`-prefix-filtered sweep would miss it — regression-pinned) | `adjusted` | set | 1 |
| D18 | mapped durable id; `FreezeSignal` mid-tick (qty ≠ seeded baseline, agent did not trade) | `ca_silent_adjust` SYNTHESIZED from the signal's `prev_qty`/`curr_qty` (M6C-1) | `frozen_immediate` (status freeze row + same-tick immediate pass; NO adjust; pass never clean; no detector seeding — M6C-9); next SOD still `latched_operator` (frozen) | set, holds | (in-process) |
| D19 | mapped durable id; the AGENT's own fill changes qty | — detector observation suspended (FD-M6-14); NO freeze | — | unchanged | — |
| D20 | double-run over the same drifted-then-adjusted state | — second pass empty (fixpoint, FD-M6-2) | — | first pass set; second pass clean clears | 1 then 0 |
| D21 | restart with unresolved drift rows (incl. trailing rows without a summary) | — latch rehydrated at step 6.5, opens rejected `portfolio_unreconciled` before any broker read | — | set pre-broker | — |
| D22 | `BrokerHttpError` positions payload / `AccountInvalid` / non-list payload | — note `broker_read_failed`, `completed=false`, NO drift verdict | — | unchanged (a pre-set latch survives) | 3 |
| D23 | in-flight watch order on the drifted symbol (probe returns a live state) | `position_qty` | `adjust_deferred` + note `adjust_deferred_inflight`, no adjust (deferral set — M6C-2) | set | 1 |
| D24 | first-ever pass (no baseline, deferral set empty) | — note `baseline_seeded`, cash skipped, baseline written (non-empty deferral set ⇒ D33) | — | unchanged | 0 |
| D25 | duplicated `fill_id` rows in fills.jsonl (polling re-read) | — telescope dedupes first-occurrence; duplicates straddling the baseline watermark excluded by the pre-filter (M6C-26); no false cash drift | — | unchanged | 0 |
| D26 | corrupt (newline-terminated bad line) local stream | — `JournalCorruption` fatal, NO reconcile row (FD-M6-19/M6C-14) | unchanged | 3 |
| D27 | `RunLockHeld` (live second composition) | — stderr only, no journal write (`_cmd_reconcile` AND `_cmd_paper` — M6C-21) | unchanged | 2 |
| D28 | presumed-live unconfirmed order (recovery 404×3 ⇒ no task, FD-M5-17) on the drifted symbol | `position_qty` | `adjust_deferred` + note `adjust_deferred_inflight`; cash skipped (`cash_skipped_inflight`); NO `position_adjust` — a fill booked by a LATER run's recovery therefore cannot brick rehydrate (regression-pinned, M6C-2) | set | 1 |
| D29 | `FreezeSignal` race: broker qty re-agrees with local by the immediate pass's fresh read | `ca_silent_adjust` (synthesized from the SIGNAL, not the re-read — M6C-1) | `frozen_immediate`; ≥1 finding structurally, pass never clean, latch NOT cleared | set, holds | (in-process) |
| D30 | held symbol with NO mapped durable id, two passes in one run | — note `durable_id_missing` exactly ONCE (per-run `self._noted_durables`, M6C-27); symbol fully position-diffed | — | unchanged | 0 |
| D31 | one order probe raises mid-sweep (other lenses fine) | — note `order_probe_failed`, `completed=false`, NO drift verdict for that order | — | unchanged (a pre-set latch survives — M6C-27) | 3 |
| D32 | order in flight at cash-lens time (baseline exists, NO residue outstanding — the residue-outstanding composition is D34, RC-8) | — note `cash_skipped_inflight`; baseline (cash, watermark) pair carried forward byte-identical (M6C-5/27) | — | unchanged | 0 |
| D33 | FIRST-EVER pass with an in-flight order (no prior baseline, deferral set non-empty) | — note `cash_skipped_inflight`; **NO `reconcile_baseline` row written** (no pair exists to carry forward and a torn read never seeds — RC-1); the next completed pass with an empty deferral set writes the first seed | — | unchanged | 0 |
| D34 | cash residue latched (D6) on pass N; pass N+1 carries an in-flight order ⇒ cash lens SKIPPED (the D6→D32 composition — RC-8) | `cash` — the carried residue RE-JOURNALED with `local`/`broker`/`diff` byte-identical to pass N's row (fresh `drift_id`; FD-M6-17) + note `cash_skipped_inflight` | `latched_operator` | set, HOLDS — summary not clean, fold window not drift-free: a skipped lens can never clear a known residue (in-process AND across a restart) | 1 |
| D35 | kill residual symbol with drifted broker qty; the `flatten-<symbol>` probe FAILS (`BrokerTimeout` / non-404 error-as-data) | `position_qty` | `adjust_deferred` + notes `order_probe_failed`, `adjust_deferred_inflight` — the FAILED flatten probe puts the symbol in the deferral set (RC-9); **NO `position_adjust` over a possibly-live flatten**; the next pass with a terminal flatten probe adjusts | set | 3 (drift WAS journaled this pass, but `completed=false` ⇒ 3-over-1 — the RC-13 precedence) |

---

## I. Test list — each file → cases → safety invariants

Harness note (the M4 §M / M5 §R shape): offline, stdlib-only; `FakeClock` / `SpyBroker` /
`ScriptedOrderApi` / `FakeAccountProvider`; **extend `test_config_canary.py` and
`test_no_network_no_creds.py` rather than duplicating**; run via
`python3 -m unittest discover -s tests -p 'test_*.py' -t .`.

1. **`tests/agent/test_broker_reconcile.py`** (~35) — enumerated cases:
   1. out-of-vocab kind/action/note/phase each raises `ReconcileError`, nothing constructed;
   2. `diff_positions` union semantics: book-only symbol ⇒ `position_missing_broker`; broker-only ⇒
      `position_unknown_broker` + `latched_operator`; both flat ⇒ nothing;
   3. absence-as-zero both directions (V1 `qty_for`, V2 zero-row drop modeled in fixtures);
   4. broker short (`qty=-5`) ⇒ `short_unrepresentable` + `latched_operator`, NO plan;
   5. multi-`position_id` aggregation: 3 open positions, broker one row, Σ compared (key-collapse);
   6. LIFO cascade: delta −12 over positions (newest 10, older 5) ⇒ newest→0 (closed), older→3;
   7. clamp flatten-never-flip: planner never emits `adjusted_qty < 0`;
   8. positive delta accrues entirely to newest;
   9. fixpoint: re-diff of the post-plan state is empty (FD-M6-2);
   10. frozen symbol ⇒ finding + `latched_operator`, no plan; deferral-set symbol ⇒ action
      `adjust_deferred` + note `adjust_deferred_inflight`, no plan (M6C-2);
   11. a `position_qty` finding with `action="adjusted"` always has ≥1 plan; deferred findings
      carry `adjust_deferred` + zero plans (§A.1 pin, M6C-1/2);
   12. Decimal-VALUE compare: `"1"` vs `"1.000000000"` ⇒ no drift;
   13. cost boundary: diff exactly `qty×0.005` ⇒ match; one ulp over ⇒ `position_avg_cost`;
   14. cost re-anchoring: Σ post-plan `broker_cost_usd` == `avg_entry_price × B` byte-exact;
   15. `avg_entry_price=None` ⇒ note `cost_unverifiable`, qty lens still runs;
   16. `diff_cash` exact telescope: buys subtract / sells add `delta_cost_usd`; zero residue ⇒ None;
   17. `fill_id` first-occurrence dedupe (duplicate polling re-reads, D25);
   18. watermark strictness: `seq ==` watermark excluded, `seq >` included;
   19. `resolve_order_probe` — one case per §3 table row (14 rows, incl. the broker-live,
      confirmed-open×404, and the three flatten rows — M6C-3), sentinel handling, and the
      `cum_filled_watermark` input driving the fills_missed split (M6C-16); returns a
      `ProbeResolution` with pinned `TerminalResolution` fields (M6C-29/30);
   20. `identity_note` boundary at `0.01` (≤ ⇒ None, > ⇒ note);
   21. hostile ambient Decimal context (prec=3, ROUND_DOWN) ⇒ byte-identical outputs;
   22. deterministic sort of findings/notes (shuffled input, sorted output) — INCLUDING
      None-bearing key slots mixed with str ones (no TypeError; None-coerced keys, M6C-33);
   23. ModeledUSD / float / bool / NaN into any money/qty slot of `make_finding` ⇒ TypeError,
      nothing constructed (the ONE typed boundary — M6C-17);
   24. LIFO boundary exhaustion: delta exhausts exactly on the newest lot (flattened to 0) ⇒ the
      re-anchor moves to the newest lot remaining OPEN (extra cost-only adjust when no touched lot
      stays open); re-diff of the post-plan state is empty (fixpoint preserved — M6C-8);
   25. pinned adjust costs: flatten-to-zero plans carry `adjusted_broker_cost_usd = Decimal("0")`,
      intermediate open survivors keep prev cost; `avg_entry_price=None` qty-only plans ditto
      (M6C-28);
   26. `adjusts_allowed=False`: otherwise-adjustable drift ⇒ action `adjust_deferred`, zero plans
      (M6C-1).
   **[S5, S2, DET]**
2. **`tests/agent/test_reconcile_ledger.py`** (~25) — enumerated cases:
   1. every `record_*` round-trips hash-verified via `replay_reconcile`;
   2. frozen field sets: missing kwarg ⇒ TypeError; extra kwarg ⇒ TypeError; positional ⇒ TypeError;
      a raise leaves the stream byte-untouched (read-back identical);
   3. `"v": 1` first payload key + `rules_hash` last on every row;
   4. reserved-key hygiene: no payload field named per V12; `fills_seq_watermark` accepted;
   5. each §B.1a validation rule has at least one negative cell (out-of-vocab kind; un-prefixed
      `position_id`; bool watermark; unsorted `positions`; `clean=true, completed=false`;
      `clean=true, drift_count!=0` ⇒ raise (M6C-22); float money; unparseable/non-finite value
      string ⇒ raise — the facade is structural-only, the lineage wall is test 1.23 (M6C-17));
   6. byte-identical replay under pinned run_id + row clock (the determinism pin);
   7. truncated-tail dropped on replay; newline-terminated bad line ⇒ `JournalCorruption` (§B.1b);
   8. `rehydrate_reconcile_state`: drift→set; completed-clean-{sod,eod,cli}-with-drift-free-window
      →clear; an immediate-phase clean summary NEVER clears (M6C-1); drift rows + a clean summary
      in ONE window keeps the latch set (fail-closed — M6C-22); trailing-rows-without-summary
      →set (fail-closed); set→clear→set sequences; baseline latest-wins; `pass_count` counts ALL
      summaries, completed or not (M6C-12); empty stream ⇒ the §B.1b zero state;
      `outstanding_cash_residue`: set by a `latched_operator` cash row, held through a
      skip-pass re-journal window, cleared by a `rebaselined` row and by a completed summary
      with a cash-drift-free window; an incomplete (`completed=false`) summary clears NOTHING
      (RC-8);
   9. cross-writer seq sharing on one resolved path (prior-run seeding ledger + fresh ledger);
   10. `cash_usd` exact-unquantized round-trip (LD-R5 discipline).
   **[S3, S2, S5, S6]**
3. **`tests/agent/test_paper_book.py` (extend) + `test_exec_ledger.py` (extend)** (~18) —
   enumerated cases:
   1. `record_position_adjust` field-set + lineage validation (the §B.2 shape; ModeledUSD ⇒
      TypeError; `order_id`/`decision_id` kwargs refused; `prev_qty < 0` / `adjusted_qty < 0` /
      bool/float qty ⇒ raise, nothing written — M6C-7);
   2. fold: qty-only adjust; cost-only adjust; combined; `adjusted_qty=0` ⇒ status "closed";
   3. prev-state telescope verification raises `ExecError` on qty OR cost mismatch — exact Decimal
      VALUE equality: a value-equal scale-different prev (`30` vs `30.00`) does NOT brick
      (the V5 precedent, M6C-20);
   4. fold ordering: adjust between two closes on one stream applies in seq order; an adjust row
      for a position from THIS change no longer bricks rehydrate (V5 regression);
   5. a `position_close` AFTER an adjustment verifies against the ADJUSTED basis on restart
      (the brick trap, proven un-bricked);
   6. rehydrate == live byte-exact via `dumps()` after `apply_position_adjust` (LD-R5);
   7. write-ahead: a ledger raise leaves stream AND book state untouched;
   8. modeled lineage byte-identical before/after any adjust (FD-M6-10);
   9. fills/orders folds unperturbed; the fills cum watermark unchanged by an adjust;
   10. `EXEC_LEDGER_VERSION` still 1; out-of-vocab positions row still raises;
   11. a seeded prior-run `position_adjust` row with negative `adjusted_qty` ⇒ rehydrate raises
      `ExecError` (a bad row cannot fold a flipped/short position — M6C-7);
   12. an adjust row referencing an UNKNOWN `position_id` ⇒ rehydrate raises (never
      create-on-miss — M6C-7);
   13. an adjust row targeting a position already folded `closed` ⇒ rehydrate raises (M6C-7).
   **[S5, S3, LD-R5]**
4. **`tests/agent/test_reconcile_orchestrator.py`** (~46) — enumerated cases:
   1.–13. the §H matrix rows needing composition (D1–D7, D9–D14) through
      `ReconcilePipeline`/`FakeAccountProvider`, asserting per row: the exact `reconcile` row fields
      (kind/local/broker/diff/action), the latch state, and the `position_adjust`/`order_terminal`
      rows where applicable;
   14. D12 fills-missed: order lens `alert_only` + `resolved_terminal`, the symbol leaves the
      deferral set THIS pass so the position lens's `position_adjust` adopts the qty in the SAME
      pass (M6C-2), NO fill row written (FD-M6-21); with a seeded baseline, the SAME pass also
      journals the `cash` residue (`latched_operator`) and pass N+1 re-detects CASH ONLY against
      the unrefreshed baseline (qty machine-resolved, no new position drift) — the latch holds
      across both passes until `--rebaseline-cash` (RC-5);
   15. D17 kill-flatten residual via `ScriptedOrderApi` + the committed `order_flatten_filled.json`
      fixture; note `flatten_probe_result`, NO terminal resolution (M6C-3); regression-pin that an
      `"o-"`-prefix-filtered sweep would miss it;
   16. D18 freeze: seeded baseline, drifted refresh ⇒ status `broker_adjust_freeze` row +
      same-tick immediate pass — ordering proven by a record-spy over
      `StatusLedger.record_broker_adjust_freeze` and `ReconcileLedger.record_*` CALL ORDER (the
      two streams have independent path-keyed seq counters, so a cross-stream seq compare is
      meaningless — M6C-31) — + `ca_silent_adjust` synthesized from the signal +
      `frozen_immediate` + `trigger_durable_key` on the summary + NO adjust + latch;
   17. D19 agent-fill suspension: agent's own booked fill changes qty ⇒ observation suspended, NO
      freeze; next completed pass re-seeds and observation resumes;
   18. D20 fixpoint at composition level: pass→adjust→pass ⇒ second pass clean, latch cleared;
   19. D21 restart: prior-run drift rows (incl. trailing-no-summary) ⇒ step 6.5 latch BEFORE any
      broker read; `can_open` rejects `portfolio_unreconciled`;
   20. D22 degraded reads: `BrokerHttpError`-as-data positions / `AccountInvalid` / non-list payload
      ⇒ note + `completed=false`; a PRE-SET latch survives (R14 mirror);
   21. D23 in-flight deferral; D24 first-baseline; D25 fill_id dedupe at composition level
      (incl. a duplicate straddling the baseline watermark ⇒ no false drift, M6C-26);
   22. latch threading: `_portfolio_read` stamps `unreconciled_drift=True`; an account refresh does
      NOT clear it (V8 regression); the REDUCE path and cancels still work while latched (FD-M4-3);
   23. **no `AccountStore.put` / no `latest_unsafe()` / no F4 risk row from any pass**
      (spy-asserted + risk.jsonl byte compare, §1 row 11);
   24. `_provider_payloads` precedence: injected provider wins over broker (V31);
   25. EOD edge: once per `session_date_et` via `_reconciled_eod_sessions`; fires on a 13:00-ET
      half-day fixture and across a DST-flip date; fires even when the margin block early-returns
      on `latest_unsafe() is None` (V9); does NOT fire twice when the margin block also runs;
   26. detector observe-before-seed unreachable by sequencing (S7-2 raise = a test of OUR ordering);
   27. detector inert with empty `durable_ids`: status/risk/orders/fills/positions streams
      byte-identical to an M5-shape run;
   28. kill HALTED + reconcile adjust legality (FD-M6-20): kill rung still terminal first in
      `can_open`; reconcile never calls `trigger`/`retry_residual` (spy);
   29. `run_reconcile` raises on out-of-vocab phase; no row written;
   30. immediate phase plans no adjustments even for non-frozen drift found in the same pass —
      those findings carry `adjust_deferred` (FD-M6-8/M6C-1 pinned at the composition level);
   31. D28 presumed-live deferral: recovery 404×3 (no task) + broker qty ahead ⇒ `adjust_deferred`,
      NO `position_adjust`, cash skipped; a fill booked by a later run's recovery then folds
      WITHOUT bricking rehydrate (the M6C-2 brick regression);
   32. D29 freeze race: broker re-read agrees with local ⇒ the synthesized `ca_silent_adjust`
      still journals, pass never clean, latch stays set (M6C-1);
   33. D30 `durable_id_missing` emitted once across two passes in one run (M6C-27); D31 mid-sweep
      probe failure ⇒ `completed=false`, latch unchanged (M6C-27); D32 cash skip carries the
      (cash, watermark) pair forward byte-identical (M6C-5);
   34. cash residue persistence: a D6 residue re-detects on the NEXT pass against the UNREFRESHED
      baseline; the latch holds across both passes (no machine self-absolution — M6C-5);
   35. latch-before-write: a ledger raise injected mid-step-6 ⇒ `self._drift_latch` already True
      in-process AND after restart (M6C-24);
   36. seeding discipline: frozen / deferred / broker-only symbols and immediate passes never
      `seed_baseline`; only local==broker symbols seed (M6C-9);
   37. detector identity: one `BrokerAdjustDetector` per orchestrator (never rebuilt across
      `_refresh_account` calls); a restart re-freezes from the status-stream fold and the latch
      rehydrates set while any durable is frozen (M6C-4/M6C-1);
   38. occurrence/ids: an incomplete pass followed by a second same-phase pass in one run mints
      DISTINCT `reconcile_id`s (every summary increments the counter — M6C-12);
   39. pre-instant freeze: a `FreezeSignal` before any quote (`_instant_utc is None`) uses the
      epoch-fallback instant — no crash, same-tick immediate pass (M6C-34);
   40. D33 first-ever × in-flight: a completed FIRST-EVER pass with a non-empty deferral set
      writes note `cash_skipped_inflight` + summary but NO `reconcile_baseline` row (the §B.1a
      non-nullable pair is never filled from a torn read); the next completed pass with an empty
      deferral set writes the first seed and the telescope anchors there (RC-1);
   41. EOD mode gate (RC-2): a paper-mode composition crossing a leaving-RTH edge fires exactly
      ONE eod pass (once per `session_date_et`); a synthetic composition (FakeBroker present —
      broker-presence alone would NOT exclude it) crossing the same edge fires NONE and leaves no
      `reconcile_alerts.jsonl` — the edge-crossing half of the test-8 structural proof;
   42. D34 skip-pass residue hold (RC-8): pass N latches a D6 cash residue; pass N+1 with an
      in-flight order SKIPS the cash lens but RE-JOURNALS the carried residue (`local`/`broker`/
      `diff` byte-identical to pass N's row, fresh `drift_id`), summary `clean=false`, the latch
      holds in-process AND through a restart's fold (no drift-free window); after
      `--rebaseline-cash`, a later cash-skipped pass re-journals NOTHING and a clean pass clears;
   43. D35 failed-flatten deferral (RC-9): kill residual + drifted broker qty + flatten probe
      PROBE_FAILED ⇒ the symbol joins the deferral set, the position drift journals
      `adjust_deferred` + note `adjust_deferred_inflight`, NO `position_adjust` row,
      `completed=false` (exit 3 at the CLI — the FD-M6-11/RC-13 3-over-1 precedence: the drift
      journaled this pass does NOT yield exit 1); the next pass with a terminal flatten probe
      adjusts.
   **[S5, S7, S6, R14-mirror]**
5. **`tests/agent/test_reconcile_cli.py`** (~17) — enumerated cases (drift/credentials enter via
   the §G third sanctioned seam: patched `__main__._SECRETS` tmp root + patched
   `agent.broker.alpaca.AlpacaPaperBroker` factory — the SOURCE-module attribute the paper branch's
   function-local lazy import resolves at call time (orchestrator.py:560; the orchestrator module
   holds no `AlpacaPaperBroker` attribute — RC-12) — that accepts-and-discards the
   `credentials_loader` kwarg and constructs the real class with `order_api=ScriptedOrderApi(...)`
   ONLY — never both kwargs, the real ctor raises on the pair — M6C-13/RC-7/RC-12):
   1. parser shape: `agent reconcile --journal-dir --calendar-fixture --overlay --rebaseline-cash`;
   2. clean pass ⇒ 0; summary fields printed to stdout;
   3. injected drift ⇒ 1; drift-fully-adjusted-still-1 (adjustment ≠ absolution);
   4. pre-existing rehydrated latch + clean broker ⇒ the pass adjudicates: a completed clean pass
      clears ⇒ 0; a still-drifted broker ⇒ 1;
   5. mode mismatch ⇒ 2 (the V15 precedent);
   6. `RunLockHeld` (live second composition) ⇒ 2, stderr only, journal tree byte-untouched —
      for `_cmd_reconcile` AND `_cmd_paper` (the NEW handler, M6C-21);
   7. credentials-missing degrade-to-observe ⇒ `reconcile_skipped_no_broker` note path ⇒ 3;
   8. broker-unreadable (`BrokerHttpError`-as-data) ⇒ 3; latch unchanged;
   9. `JournalCorruption` in a local stream ⇒ 3, NO reconcile row written (FD-M6-19/M6C-14);
   10. `_cmd_paper` returns 1 on SOD-pass drift and on a rehydrated latch; 0 on clean;
   11. degraded `_cmd_paper` skips the SOD call (today's return preserved);
   12. `orch.close()` releases the lock on every exit path (re-acquire succeeds);
   13. zero submits and zero token mints on every exit path; zero cancels EXCEPT the journaled
      `restart_unknown_state` recovery cancel when a dangling order is seeded (spy — the M6C-6
      carve-out, pinned not assumed);
   14. seeded dangling order + `agent reconcile`: step-10 recovery adopts + issues exactly ONE
      journaled `restart_unknown_state` cancel; the pass itself still submits/cancels/mints
      nothing (M6C-6);
   15. `_cmd_paper` SOD pass `completed=false` (broker unreadable at startup) ⇒ 3, NOT 0
      (M6C-23);
   16. `--rebaseline-cash` on a cash residue ⇒ drift row `action="rebaselined"`, baseline pair
      refreshed, exit 1; the NEXT clean pass clears (M6C-5);
   17. without the flag, the same residue ⇒ `latched_operator` + carried-forward baseline on
      every pass (M6C-5).
   **[S5, S1]**
6. **`tests/agent/test_config_canary.py` — NEW class `TestM6ReconcileCanary`** (~8) — REAL committed
   config, run-gates file ABSENT: a full `run_reconcile` over a drift-scripted provider + `SpyBroker`
   submits NOTHING (`calls == []`, `submitted == []`, `cancel_calls == []`), mints nothing (zero
   open-kind preflight authorizations), and STILL detects + journals drift + latches (gates govern
   opens only — detection works exactly when locked down); `can_open` still terminates at
   `run_gates` first on the committed config (drift observable only under the permissive in-memory
   config — pinned explicitly); committed config bytes + `rules_hash` equal the M5 values
   (golden-stability pin, §1 row 16). **[S1, S5]**
7. **`tests/agent/test_no_network_no_creds.py` — NEW class
   `TestM6ReconcileOfflinePurityAndImportGuard`** (~10) — module tables for `broker_reconcile.py` /
   `reconcile_ledger.py` (module-scope whitelist; pure-family any-scope forbidden prefixes/tokens
   per §4, own copied walker per convention); socket-blocked import; banned `sys.modules`;
   subprocess fresh-import via fixed argv; the orchestrator wall-clock/sleep AST scan re-run over
   the grown file; `_cmd_reconcile` reads `.secrets` only via explicit paths. **[purity, S1]**
8. **`tests/agent/test_synthetic_e2e.py` / `test_observe_e2e.py` (extend)** (~4) — committed goldens
   still byte-identical WITHOUT regeneration (§1 row 16 proof); `reconcile_alerts.jsonl` does not
   exist in synthetic/observe journal dirs (no automatic pass — SOD per §1 rows 4/5, EOD per the
   RC-2 mode gate; structural, not an artifact of the golden scripts stopping short of 16:00 —
   the edge-crossing proof is test 4.41; V30). **[S3, DET]**

Total new tests ≈ **163** (= the per-file estimates 35+25+18+46+17+8+10+4 — the sum and the total
must stay equal, M6C-32); suite target ≈ **1683** (1520 + 163). Each wave: author → full suite with `-t .` →
separate review pass (house rule); build subagents make no git mutations; reviewers are read-only.

**Invariant map**

| ID | Invariant | Primary tests |
|---|---|---|
| **S5** | Broker ledger never overwritten by a modeled fill; `broker_account_pnl` vs `execution_realistic_pnl` separate (lineage walls on every new row); reconciliation flags broker drift (journaled `reconcile` rows + latch + opens blocked) and exits non-zero; broker = truth via explicit journaled `position_adjust` rows, never silent mutation | 1, 2, 3, 4, 5, 6 |
| S1 | Committed config + absent gates file: reconcile makes ZERO broker mutations (no submit/cancel/mint) even under injected drift; the pure modules structurally cannot reach a mint/submit (AST) | 5, 6, 7 |
| S2 | No float/NaN in any reconcile row; exact Decimals end-to-end; pinned-context arithmetic | 1, 2 |
| S3 | New stream replays byte-identical; truncated-tail / corrupt-line semantics inherited; rehydrate == live byte-exact incl. `position_adjust`; restart after adjustment un-bricked; existing goldens untouched | 2, 3, 8 |
| S6 | `reconcile_id`/`drift_id`/`adjust_id` deterministic; rows correlate by run_id + ids + per-stream seq | 2, 4 |
| S7 | Silent broker adjust ⇒ freeze + IMMEDIATE (same-tick) reconcile + status row; baselines seeded only at ACTUALLY reconciled points (local==broker; frozen/deferred/broker-only skipped, immediate seeds nothing — M6C-9); observe-before-seed unreachable; immediate never adjusts and never cleans (M6C-1); frozen never auto-adjusts and blocks latch clears (M6C-1); freezes survive restarts via the status fold (M6C-4) | 4 |
| FD-M6-2 | Double-run fixpoint; no duplicate adjustments | 1, 4 |
| FD-M6-6/7 | Latch survives restart and account refreshes; failed reads never set or clear it; trailing rows fail-closed | 2, 4 |
| FD-M6-11 | clean / drift / cannot-reconcile are three distinguishable exits | 5 |

---

## J. TDD wave plan

| Wave | Builds | Tests (new) | Gate to next wave |
|---|---|---|---|
| W1 | `broker_reconcile.py` vocab + `ReconcileError` + dataclasses; `reconcile_ledger.py` row shapes + `replay_reconcile` + `rehydrate_reconcile_state` | files 1 (vocab/fold half), 2 | byte-identical replay + fail-closed fold green; suite stays green (no behavior change elsewhere) |
| W2 | PURE diff core: `diff_positions` (lenses + LIFO planner), `diff_cash`, `resolve_order_probe`, `identity_note`; tolerance boundaries; fixpoint | file 1 (core half) | engine green incl. hostile-context cells |
| W3 | Fold extension: `EVT_POSITION_ADJUST` + `record_position_adjust` + `PaperBook.rehydrate` fold + `apply_position_adjust` live twin + prev-state verification | file 3 | rehydrate == live byte-exact; brick-trap regression green |
| W4 | Orchestrator: step 4+ writers, step 6.5 latch fold, `run_reconcile`, `_portfolio_read` stamping, EOD hook (mode-gated, RC-2), detector seeding/observe/suspension + immediate trigger, `durable_ids` seam | file 4 (matrix) | full 1520-baseline + new green; M5 pinned tests green UN-EDITED, with one owned exception class (RC-2): W4 re-audits every existing PAPER-mode test that crosses a leaving-RTH edge — today exactly `test_orchestrator.py::test_session_edge_cancels_open_order_and_closes_day` — where the EOD hook now fires in-test: it consumes one extra scripted `get_by_client_order_id` entry (40 are scripted — headroom verified) and writes `reconcile_alerts.jsonl` notes + summary into that test's own journal dir (NO baseline row: first-ever pass, non-empty deferral set — RC-1); the test's assertions are untouched and synthetic/observe compositions are excluded structurally by the mode gate; goldens untouched |
| W5 | CLI: `reconcile` subcommand (+ `--rebaseline-cash`) + exit codes + `_cmd_paper` mapping + RunLockHeld wrappers; runbook page; canary + purity classes; golden-stability re-run; fixtures polish | files 5, 6, 7, 8 | full suite ≈ 1683 green |
| W6 | Adversarial review pass (separate authoring/review per house rule) — hostile payload shapes, lock contention, crash-mid-pass replay, repro-gated fixes only | — | review findings closed with repro tests |

---

## K. Conventions-to-mirror table

| Convention | Source (verified) | M6 usage |
|---|---|---|
| Validating ledger facade over `EventWriter`; `v`-first; `rules_hash` every row; kwarg-only exact field sets; raise-before-write | `exec_ledger.py:446-468` | `ReconcileLedger` (§B) |
| PURE fold by ascending seq; latest-row-wins; fold == live byte-exact | `status_ledger.py:356-371`, `exec_ledger.py:1055-1107` | `rehydrate_reconcile_state` (§B), `position_adjust` fold (§B.2) |
| Fail-closed report `ok`; diff carries both sides; never mutates inputs; CLI maps not-ok ⇒ non-zero | `recorder/reconcile.py:27-33` | `ReconcilePassResult` (§A), exit codes (§E) |
| Once-per-session edge dedupe | `orchestrator.py:1927-1933` | `_reconciled_eod_sessions` (§D) |
| `dataclasses.replace` freshness stamping | `orchestrator.py:927-932` | drift-latch stamping (§D) |
| Prior-run journal seeding for restart tests | `test_orchestrator.py:137-163` | drift fixtures (§G) |
| CLI subcommand asserting derived mode; wall reads CLI-only; exit-2 precedent | `__main__.py:196-258, 92-94` | `_cmd_reconcile` (§E) |
| Code-constant polarity discipline | `execution_config.py:13-46` | §F constants |
| Pinned Decimal context for derived values (ambient immunity) | `exec_ledger.py:197` | `_DECIMAL_CTX` (§A) |
| Self-verifying telescoped residuals (raise on mismatch; exact Decimal VALUE equality, not string — M6C-20) | `paper_book.py:675-685` | `position_adjust` prev-value check (§B.2) |

---

## Open items for the critique pass (rev 2 inputs)

1. The cash-telescope exactness assumption (A1) against real Alpaca-paper cent-rounding behavior is
   pinned but unverified live — the M6-2a credentialed runbook step should observe one real fill
   cycle before the edge-validation phase leans on exit codes.
2. ~~The `occurrence`/`pass_count` interaction across restarts~~ RESOLVED rev 2 (M6C-12):
   occurrence counts ALL summaries (rehydrated + this run, completed or not), incremented after
   every summary write — within-run incomplete passes mint distinct ids; the crash-mid-pass edge
   (rows but no summary) stays acceptable because `reconcile_id` also keys `run_id`, which differs
   after restart.
3. Whether `_cmd_paper`'s exit-1-on-latch breaks any operator script that treats `agent paper` exit
   0 as "startup ok" — flagged for Robin (ops contract change, deliberate S5 visibility). Rev 2
   adds `_cmd_paper` exit **3** on a `completed=false` SOD pass (M6C-23) and exit **2** on
   `RunLockHeld` (M6C-21) — the same flag to Robin covers all three new non-zero startup exits.

---

## V. Rev-1 critique resolution log (one line per canonical finding; 48 raw → 34 canonical, blockers first)

| ID | Disposition | Where applied |
|---|---|---|
| M6C-1 (blocker, safety+build-1; merges 3 raw) | APPLIED | FD-M6-6/8/12; §1 row 15; §3 actions (`adjust_deferred`); §A `diff_positions(adjusts_allowed)`; §B.1 fold + `reconcile_run` row; §D (1b) synthesized finding, trigger param, normative plan gate, phase-restricted frozen-blocked clear; §H D18/D29; §I 1.26, 2.8, 4.16/4.30/4.32, 4.37; A6 |
| M6C-2 (blocker, safety+correctness+build-1; merges 3 raw) | APPLIED | FD-M6-13 (deferral-set definition); FD-M6-4(c); §1 row 19; §B.2 soundness; §D step 5/(4b); §3 table deferral columns; §H D12/D23/D28; §I 1.10/1.11, 4.14, 4.31 |
| M6C-3 (blocker, safety+correctness+build-1+build-2; merges 4 raw) | APPLIED | §3 table closure (broker-live rows, confirmed-open×404 row, resolver-unit-only row 7 note, three flatten ∅ rows); new note `flatten_probe_result`; §A `resolve_order_probe(local_row=None, flatten_symbol)`; §H D17; §I 1.19, 4.15 |
| M6C-4 (major, repo-facts) | APPLIED | §4 orchestrator imports (+`agent.corporate_actions`); §D step 6.5 (detector constructed once, frozen state rehydrated from the status fold); §I 4.37 |
| M6C-5 (major, safety+correctness; merges 2 raw) | APPLIED | FD-M6-17 (conditional refresh / carry-forward); A1; A6; §1 row 19; §3 `rebaselined` operator-only; §E `--rebaseline-cash`; §H D6/D32; §I 4.33/4.34, 5.16/5.17 |
| M6C-6 (major, safety) | APPLIED | §0 ground rule scoped to the PASS; FD-M6-1 rationale; FD-M6-22 (inherited step-10 cancel owned); §I 5.13/5.14 |
| M6C-7 (major, safety) | APPLIED | §B.2 (`record_position_adjust` sign/lineage validation; fold raises on unknown id / negative qty / adjust-of-closed); §I 3.1, 3.11–3.13 |
| M6C-8 (major, correctness) | APPLIED | §A.1 (re-anchor on the newest OPEN survivor; extra cost-only adjust; newest-open pin for sole cost-only); §I 1.24 |
| M6C-9 (major, correctness) | APPLIED | FD-M6-14 (seed only local==broker; skip frozen/deferred/broker-only; immediate seeds nothing); §D step 7; invariant map S7; §I 4.36 |
| M6C-10 (major, correctness+build-1+build-2; merges 3 raw) | APPLIED toward fail-closed: D6 = `latched_operator`, `rebaselined` operator-gated (conflict between the safety fix and the build lenses' `rebaselined` pick resolved per the fail-closed rule) | §H D6; §3 vocab; §E |
| M6C-11 (major, build-1) | APPLIED | §A `diff_positions` input `Mapping[str, Tuple[PaperPosition, ...]]` newest-first, orchestrator-supplied ordering |
| M6C-12 (major, build-1+build-2; merges 2 raw) | APPLIED | §C occurrence (ALL summaries); §D pseudocode (`_pass_count += 1` after every summary); §B.1 fold doc; §I 2.8, 4.38; open item 2 |
| M6C-13 (major, build-1) | APPLIED | §G third sanctioned seam (patched `_SECRETS` + patched `AlpacaPaperBroker` factory); §I file-5 harness note |
| M6C-14 (major, build-1) | APPLIED (note clause dropped, no new vocab token — LEAN) | FD-M6-7 (JournalCorruption removed from the note class); FD-M6-19 (exit 3, NO row); §H D26; §I 5.9 |
| M6C-15 (major, build-1+build-2; merges 2 raw) | APPLIED (option b; the two merged fixes conflicted on UnknownSessionDate — resolved to presumed-live, the arm that fabricates nothing and keeps the order tracked + open-denied) | §0.1 note; §D `_session_over` pinned algorithm; §3 not-found rows; §H D14 |
| M6C-16 (major, build-2) | APPLIED | §A `resolve_order_probe(cum_filled_watermark=...)`; §3 table header; §D step 4; §I 1.19 |
| M6C-17 (major, build-2) | APPLIED | §A `make_finding` typed boundary; §B.1a structural-only rule; §I 1.23, 2.5 |
| M6C-18 (minor, repo-facts) | APPLIED | §D pseudocode `parsed_account.cash` |
| M6C-19 (minor, repo-facts) | APPLIED | §0.1 V27; §1 row 11 rationale |
| M6C-20 (minor, repo-facts) | APPLIED | V5; §B.2 fold rule; §K row; §I 3.3 (value-equal scale-different cell) |
| M6C-21 (minor, repo-facts) | APPLIED | FD-M6-11 ("NEW mapping" wording); FD-M6-22 + §E (`_cmd_paper` wrapper); §H D27; §I 5.6 |
| M6C-22 (minor, safety) | APPLIED | §B.1a (`clean ⇒ drift_count==0`); §B.1/FD-M6-6 fold window cross-check; §I 2.5/2.8 |
| M6C-23 (minor, safety+correctness; merges 2 raw) | APPLIED (exit 3, the job-consistent arm) | FD-M6-11; §E `_cmd_paper`; §I 5.15; open item 3 |
| M6C-24 (minor, safety) | APPLIED | FD-M6-6; §D step 5/(1b) + step-7 comment; §I 4.35 |
| M6C-25 (minor, safety) | APPLIED | §E ops story (alert on ANY non-zero; exit-2 runbook line) |
| M6C-26 (minor, correctness) | APPLIED (cross-boundary orchestrator pre-filter) | FD-M6-4(c)/FD-M6-17; §A `diff_cash` doc; §D step 5; §H D25; §I 4.21 |
| M6C-27 (minor, correctness) | APPLIED | FD-M6-14 (`self._noted_durables` state home); §D step 6.5; §H D30/D31/D32; §I 4.33 |
| M6C-28 (minor, correctness+build-1; merges 2 raw) | APPLIED | §A.1 pinned costs (flatten-to-zero = 0, survivors keep prev; no realized-PnL statement); §H D10; §I 1.25 |
| M6C-29 (minor, build-1) | APPLIED | FD-M6-21 pinned expired-resolution kwargs; §3 row; §H D14 |
| M6C-30 (minor, build-1) | APPLIED | §A `TerminalResolution`/`ProbeResolution` frozen dataclasses; §D step 4 |
| M6C-31 (minor, build-1) | APPLIED | §I 4.16 (call-order spy instead of cross-stream seq) |
| M6C-32 (minor, build-2) | APPLIED (re-summed after rev-2 cell additions: 161/1681; re-summed again at rev 4 after the RC-8/RC-9 cells: 163/1683) | §I totals; §J W5 gate |
| M6C-33 (minor, build-2) | APPLIED | §A/§A.2 None-coerced sort keys; §D pseudocode; §I 1.22 |
| M6C-34 (minor, build-2) | APPLIED (epoch-fallback, the existing :900 precedent — same-tick pass preserved) | §D tick step 3; §I 4.39 |

No findings rejected. One inter-finding conflict (M6C-10 vs M6C-5's safety arm) resolved toward
fail-closed; one intra-merge conflict (M6C-15's UnknownSessionDate arm) resolved toward the
non-fabricating outcome — both logged above.

### V.2 Round-2 re-critique resolution log (5 findings → RC-1…RC-5, majors first)

| ID | Disposition | Where applied |
|---|---|---|
| RC-1 (major, recritique — first-ever × non-empty-deferral baseline unwritable under M6C-5) | APPLIED | FD-M6-17 (the ONE pinned exception: NO baseline row — the first seed defers in full); §1 row 19; §B.1 `reconcile_baseline` shape note; §B.3 ordering (`OMITTED` arm); §D step 6 + pseudocode (`wrote_baseline` guard; `self._latest_baseline` tracks the row actually written); §H D24 amended + NEW row D33; §I NEW test 4.40; §J W4 gate (the in-test EOD pass writes NO baseline row) |
| RC-2 (major, recritique — EOD hook mode-ungated; fires in synthetic/FakeBroker compositions and inside an existing M5 paper-mode test) | APPLIED | §D tick step 10: `mode == "paper"` gate, LOAD-BEARING (mirrors the V7 step-10 precedent at orchestrator.py:628); §1 rows 5/16 amended (structural, not an artifact of golden scripts stopping short of 16:00); A4; FD-M6-12 eod bullet; §I test 8 + NEW test 4.41; §J W4 gate owns the one in-test firing (`test_session_edge_cancels_open_order_and_closes_day` — one extra scripted `get_by_client_order_id` consumed; 40 scripted, verified at tests/agent/test_orchestrator.py:507-508) |
| RC-3 (minor, recritique — FD-M6-12 listed `_cmd_reconcile` under the sod bullet ⇒ literal double-reconcile) | APPLIED | FD-M6-12 invocation list: sod = `_cmd_paper` ONLY; `_cmd_reconcile` runs NO sod pass; the degraded-observe/exit-3 parenthetical moved to the cli bullet (exactly ONE pass per CLI job — consistent with §E and FD-M6-22) |
| RC-4 (minor, recritique — `_session_over` computed for flatten ids that have no `order_submit_attempt` row) | APPLIED | §D step 4 + pseudocode: constant `session_over=False` on the flatten branch; the pinned `_session_over` algorithm scoped to journaled `o-`/`synthetic-o-` ids only (flatten ids never appear in the orders stream as journal ids, V6; the §3 flatten rows never consult it) |
| RC-5 (minor, recritique — D12 missed-fill cash residue is operator-latched under M6C-5 but described as adopted same-pass) | APPLIED — kept fail-closed: the alternative (telescope also consuming terminal-resolution cum-notional deltas) was explicitly NOT taken (it would loosen the no-fabrication line; contract-rev decision, not a default) | FD-M6-21 (cash DETECTED, never machine-resolved; persistence spelled out); §1 row 8 rationale; §H D12 (action + persistence columns); §I test 4.14 extended (pass-N+1 cash-only re-detection) |

No round-2 findings rejected. RC-5's optional loosening arm was resolved toward fail-closed per
the standing rule; every other finding strictly tightened or clarified existing behavior.

### V.3 Round-3 re-critique resolution log (6 findings → RC-6…RC-11, majors first)

| ID | Disposition | Where applied |
|---|---|---|
| RC-6 (major, recritique — M6C-19 resolution sweep incomplete: §1 row 16 still claimed goldens byte-compare risk.jsonl, contradicting the contract's own corrected V27 and the repo — `_STREAMS=(orders,fills,positions)` at test_synthetic_e2e.py:40) | APPLIED | §1 row 16 rationale: "(+risk)" dropped — "goldens compare orders/fills/positions only (V27)"; full-text sweep confirms no "(+risk)" claim remains anywhere; the same edit resolves RC-10 (the duplicate surface of the same defect) |
| RC-7 (major, recritique — §G pinned CLI seam un-constructible: the factory text passed BOTH `order_api` and `credentials_loader` to the real `AlpacaPaperBroker` ctor, which raises `ValueError` on the pair (alpaca.py:96-107) while the orchestrator's paper branch calls the patched symbol with `credentials_loader=` (orchestrator.py:561-562); M6C-13-introduced) | APPLIED | §G clause (b) reworded: the factory ACCEPTS the orchestrator's `credentials_loader` kwarg, DISCARDS it, and returns `AlpacaPaperBroker(order_api=ScriptedOrderApi(...))` — never both kwargs; §I file-5 harness note mirrored; the archive's M6C-13 resolution detail amended in place (original wording retained as history, amendment noted inside the entry). *[Patch TARGET as worded here superseded at rev 5 by RC-12: the orchestrator holds no module-scope `AlpacaPaperBroker` — :561-562 calls the name bound function-locally at :560 from `agent.broker.alpaca`, so the pinned target is `agent.broker.alpaca.AlpacaPaperBroker`; the accepts-and-discards semantics stand unchanged — see §V.4.]* |
| RC-8 (major, recritique — cash-residue latch fail-open window, the D6→D32 composition: a completed pass whose cash lens was SKIPPED (in-flight order) had findings==() and cleared the latch — in-process AND via the fold's drift-free window — over a KNOWN, journaled, unadjudicated cash residue, re-enabling opens for at least one pass interval; contradicted D6's latch column, FD-M6-17's "re-detects every subsequent pass", and A6) | APPLIED — the fail-closed RE-JOURNAL arm chosen (zero new row fields): while a residue is OUTSTANDING, a cash-skipped completed pass re-journals the carried residue as a `cash` drift row byte-identical in `local`/`broker`/`diff`, so the pass is never clean and neither clear path can fire; the alternative arm (an unconditional `cash_evaluated` conjunct on every clear) was NOT taken — it would also block clears unrelated to any known residue, while the chosen arm is equally fail-closed against the defect and makes FD-M6-17's claim literally true | FD-M6-17 (the normative re-journal rule + outstanding definition); FD-M6-6 (composition closed); FD-M6-21; A1; A6; §1 row 19; §B.1 fold (+`outstanding_cash_residue` output key, derivation pinned); §B.1b zero state; §D step 6.5 (rehydrated state home) + step 5 + pseudocode (skip-branch re-journal; evaluated-clean/residue set-clear; `--rebaseline-cash` clears); §E; §H D6/D32 amended + NEW row D34; §I 2.8 extended + NEW test 4.42 |
| RC-9 (major, recritique — flatten-probe PROBE_FAILED never reached the deferral set: the pseudocode's PROBE_FAILED branch bypasses `resolve_order_probe` (so `defer_symbols` is unreachable) and kill-residual symbols appear in NO step-4b source (`_residual` is independent state, risk_kill.py:209-210; every `_open_deny.add` is a submit-recovery path), so a kill-residual symbol with a failed flatten probe was adjustable over a possibly-live flatten — the §3 generic-failed row also contradicted the flatten-failed row; M6C-3-introduced) | APPLIED | §3 flatten-failed row: symbol JOINS the deferral set (mirrors the live row; enforced at the orchestrator PROBE_FAILED branch); §3 generic-failed row clarified (journaled orders defer structurally via `open_orders`, flatten probes explicitly); FD-M6-13 deferral-set definition (+"or FAILED"); §D step 4 + pseudocode (`defer.add(probe_id.removeprefix("flatten-"))` on the failed-flatten arm); §H NEW row D35; §I NEW test 4.43. The archive's M6C-3 resolution detail describes the rev-2 arm this supersedes — noted in the RC-9 archive entry, history untouched |
| RC-10 (minor, recritique — §1 row 16 "(+risk)" golden falsehood; the same defect surface as RC-6, retained through two revision passes) | APPLIED (via the RC-6 edit — one edit, both findings logged) | §1 row 16 |
| RC-11 (minor, recritique — §A `ReconcilePassResult.clean` comment ("the latch-clearing condition") stale against the rev-2 clear rule: the clear additionally requires phase ∈ {sod,eod,cli}, a drift-free window, and no frozen symbol) | APPLIED | §A skeleton comment: clean is NECESSARY, not sufficient, for a latch clear, with the three extra conjuncts named (FD-M6-6, §B.1, §D step 7) |

No round-3 findings rejected. RC-8's two proposed arms were both fail-closed for the identified
defect; the re-journal arm was chosen as the leaner one that also makes the contract's existing
"re-detects every pass" claims literally true — no clear that was previously legitimate (no known
residue outstanding) is blocked, and no known residue can ever be skipped over.

### V.4 Round-4 re-critique resolution log (2 findings → RC-12…RC-13, majors first)

| ID | Disposition | Where applied |
|---|---|---|
| RC-12 (major, recritique — §G pinned CLI seam STILL un-constructible after RC-7: the patch target "the orchestrator module's `AlpacaPaperBroker` symbol" does not exist — `scripts/agent/orchestrator.py` has NO module-scope `AlpacaPaperBroker` name; the only occurrence is the function-local lazy import inside the paper ctor branch (`from agent.broker.alpaca import AlpacaPaperBroker  # lazy`, orchestrator.py:560, branch :557-562), which re-resolves `agent.broker.alpaca.AlpacaPaperBroker` at call time, so patching `agent.orchestrator.AlpacaPaperBroker` is a dead attribute and the paper branch constructs the REAL broker, whose `_build_real_client` reaches `from alpaca.common.exceptions import APIError` (alpaca.py:177-195) — an `ImportError` in the stdlib-only offline suite; the §G evidence line "the orchestrator's paper branch CALLS the patched symbol (orchestrator.py:561-562)" was subtly false — :561-562 calls the name bound at :560, not any orchestrator-module attribute; the exact defect class RC-7 was meant to close, fail-safe in direction but pinning an unimplementable mechanism for the whole file-5 surface) | APPLIED | §G clause (b) re-pinned to the SOURCE-module attribute: `unittest.mock.patch("agent.broker.alpaca.AlpacaPaperBroker", factory)`, factory closing over the real class BEFORE patching; RC-7 accepts-and-discards semantics unchanged; the "no other monkeypatching" sentence now names exactly the two sanctioned patch points (`__main__._SECRETS`, `agent.broker.alpaca.AlpacaPaperBroker`); §I file-5 header mirrored (M6C-13/RC-7/RC-12); §V.3 RC-7 row carries a supersession note; the archive's RC-7 resolution detail AND M6C-13's rev-4 amendment amended in place again (history retained inside both entries) |
| RC-13 (minor, recritique — D35 makes the FD-M6-11/§E exit clauses literally overlap with no pinned precedence: "drift found this pass" (exit 1) and "not completed" (exit 3) BOTH match the failed-flatten row — the first composition where drift is found AND journaled in a `completed=false` pass — and only the matrix row, not the normative text, resolved it (D22 pinned 3 only over the pre-existing-latch arm); two faithful builders could pin different exits and byte-pin different file-5 goldens; not fail-open — both codes are non-zero and alert per M6C-25, and the latch re-surfaces as exit 1 on the next completed pass) | APPLIED | FD-M6-11 gains the normative precedence sentence: `completed=false` ⇒ exit 3 takes precedence over exit 1 regardless of findings or latch state (could-not-fully-check outranks drift-found); §E return mapping mirrored; §H D35 exit cell cites the precedence; §I test 4.43 pins it explicitly; the same precedence extended to `_cmd_paper`'s SOD mapping (consistent with D22/D31/D35 and M6C-23 as already pinned) |

No round-4 findings rejected. Both findings strictly tightened determinism/buildability without
touching any fail-closed behavior: RC-12 swaps an unreachable patch target for the one the lazy
import actually consults (the loud-ImportError failure mode it replaces was safe but
unimplementable); RC-13 pins the already-fail-closed 3-over-1 arm (both exits alert, the drift
stays journaled + latched).

### V.5 Round-5 re-critique resolution log (1 finding → RC-14)

| ID | Disposition | Where applied |
|---|---|---|
| RC-14 (minor, recritique — `outstanding_cash_residue` type contradiction: §B.1/§B.1b/§D step 6.5 typed the field `Optional[dict]` (journal-row content) while §D step 5 assigns `diff_cash`'s `Optional[DriftFinding]` return to it, so `_carried_residue_finding` received two incompatible shapes (dict subscripts vs dataclass attributes) depending on whether the residue arrived via restart-rehydrate or same-run detection; values byte-identical either way — no determinism/safety impact, but a frozen-contract type contradiction a W4 builder copies verbatim) | APPLIED | ONE shape pinned: inside the orchestrator the field is ALWAYS `Optional[DriftFinding]`. §D step 6.5 now projects via the new pure helper `_residue_from_row(row: Optional[dict]) -> Optional[DriftFinding]` (None-propagating; direct dataclass construction from the row's `kind/symbol/field/local/broker/diff/action` + optional ids; NOT `make_finding` — row values are already-validated canonical Decimal strings, no typed boundary re-crossed); §B.1 fold comment notes the fold returns the ROW DICT and the orchestrator seam projects (fold contract unchanged); `_carried_residue_finding(residue: DriftFinding, reconcile_id) -> DriftFinding` signature pinned at the same spot; determinism-neutrality of the two representations stated explicitly |

The round-5 re-critique otherwise returned `unverified=[]` (all 47 prior resolutions verified
present and completely swept) and zero further findings: the cash-latch re-journal was traced to
block BOTH clear paths (in-process line-level `completed and not findings` AND the journal-fold
drift-free-window rule) in every composition, and the PROBE_FAILED deferral was traced to block
every adjust-writing path. RC-14 is the convergence point — the contract is READY-TO-BUILD.
