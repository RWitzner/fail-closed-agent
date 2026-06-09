# M3 — Signal + observe-only calibration probe (DESIGN)

> **Status:** DESIGN (pre-contract), review-hardened 2026-06-09. This document fixes the M3 scope, the
> load-bearing decisions, the module decomposition, and the safety invariants. The **build-ready frozen contract**
> (module-by-module APIs, code skeletons, fixtures, test→invariant map) is produced from this design via the
> architect-panel → critic → revision workflow, mirroring `2026-06-09-M2-market-state-contract.md`.
>
> **Branch:** `m3-signal` (off `main` @ `a82be6d`, M2 merged). Authoritative parent design:
> `docs/superpowers/specs/2026-06-08-stocks-agent-design.md` (§5 Tier 3/4, §7 data model, §8 safety table).
>
> **Review hardening applied:** the design now explicitly resolves the bbo/mid-bar gap, as-of anti-lookahead
> semantics, snapshot provenance, S1 observe-only isolation, `rules_hash`/config home, deterministic
> `forecast_id`/score idempotency, climatology cutoff rules, quote-quality numeric guards, and offline purity.

## 0. Scope

M3 delivers **Tier 3 (snapshot/signal)** and the **observe-only slice of Tier 4 (the calibration probe)**:

1. A **feature/indicator engine** fed by completed M1/M3 bar series, holding rolling state outside the stateless
   `scan()` hot path, anti-lookahead by construction.
2. A **mid-bar label series** (`MidBar` / `MidBarSeriesReader`) for event resolution. This is explicit because the
   existing M1 `recorder.bar_cache.Bar` is OHLCV/VWAP and does **not** contain bid/ask/mid fields.
3. **Quote-quality filters** re-expressed in **bps** (reusing the Polymarket `summarize_book()` depth/spread math),
   warnings-as-data.
4. The `Strategy` Protocol + frozen `ScanContext` and `Candidate`/`Leg` types — **introduced in this repo for the
   first time** (no `strategy.py`/`candidate.py` exists today). M3's calibration probe does **not** return an
   order-capable `Candidate`; it returns an observe-only `ForecastDecision`.
5. An **observe-only calibration probe** that emits **probabilistic event/threshold forecasts**, opens nothing
   (`paper_eligible=False`), and journals `decision` rows only. M3 action vocabulary is `forecast_only` /
   `do_nothing`; `would_open` is deferred until M5/M7 and is forbidden in M3 rows.
6. A **realized-move scorer** (forecast vs realized outcome) + a **calibration report** (Brier decomposition,
   reliability diagram, skill vs a reference forecaster, and full funnel counts).

**Out of scope (deferred):** any order submission, preflight-token mint, paper/live execution (M5); risk gates /
`can_open()` / margin / locate (M4); the anti-lookahead historical **backtest gate** that proves realized edge
(M7); directional momentum/mean-reversion strategies (post-M7); online learning / model re-fitting; any live or
credentialed data call in the offline suite.

## 1. Load-bearing decisions (DECIDED — Robin, 2026-06-09)

| # | Decision | Rationale |
|---|---|---|
| D1 | **Forecast form = probabilistic event/threshold.** The probe emits `P(event) ∈ [0,1]`. | Calibration is *defined* for probability forecasts; Brier + reliability diagram is the canonical, unambiguous metric. S1 is trivially safe — a probability carries no order semantics. |
| D2 | **Event = `mid(t0+H) ≥ mid(t0)·(1+k)`**, horizons `H ∈ {5m, 30m}` (configured set), threshold `k` configurable (**default 0** → pure up-move). Realized from the explicit **mid-bar series** at ET-boundary-aligned completed bars, not from an implicit OHLCV fallback. | Many scoring samples per symbol per day → the reliability diagram populates fast; the label API is now concrete and provenance-stamped. |
| D3 | **Genuine first predictive model** using the full feature set (MAs/RSI/z-scores/realized-vol) → probabilities, **not** a trivial baseline. | Robin's call. Constrained by D4 below and by the no-edge-claim rule. |
| D4 | **M3 produces calibration *evidence*, never an edge *claim*.** "Has edge → paper-eligible" remains **M7's** anti-lookahead backtest gate. The probe stays observe-only (`paper_eligible=False`), opens nothing (S1). The feature/label layer is **anti-lookahead by construction**. | Reconciles D3 with the design's load-bearing rule that edge is not "proven" without the M7 gate. Building the model now forces anti-lookahead discipline into the feature layer from day one. |
| D5 | A **null/reference forecaster** (as-of trailing climatology + constant 0.5) is scored alongside the model **as report control**, not as a competing probe output. Decision-time `reference_base_rate_asof_t0` may use only prior resolved samples or a fixed committed training fixture. | A reliability diagram / Brier score is uninterpretable without a baseline, but full-run climatology must not leak future outcomes into decision rows. |
| D6 | **All model/config provenance is commit-pinned.** M3 signal config lives in a committed base config/artifact and is hashed into `rules_hash`; local overlays cannot change model coefficients or windows. | Existing `tighten_only_merge()` is safe only where smaller means safer. Coefficients/windows do not have that ordering, so M3 treats them as immutable provenance, not runtime-tunable gates. |

## 2. Architecture — decomposed modules (one purpose each)

```
scripts/agent/
  feature_engine.py        FeatureEngine — rolling state OUTSIDE scan(), background refresh, anti-lookahead
  bar_series.py            MidBar / MidBarSeriesReader — explicit completed mid-bar label series + provenance
  quote_quality.py         QuoteQuality — bps spread/cross/lock/staleness/one-sided/non-positive filters
  signal_snapshot.py       SignalSnapshot — atomic bundle {NBBO, market-state, feature vector, provenance}
  strategy.py              Strategy Protocol + frozen ScanContext            (NEW to repo)
  candidate.py             Candidate / Leg (single-leg = N=1)                (NEW to repo; not emitted by M3 probe)
  forecast.py              Forecast / ForecastEvent + predictive model (fixed config-pinned coefficients)
  calibration.py           realized-move resolver + scoring (Brier, Murphy decomposition, BSS, reference)
  calibration_report.py    renders reliability diagram + decomposition + funnel + per-(symbol×horizon) breakdown
  strategies/
    calibration_probe.py   observe-only probe: ScanContext → ForecastDecision → decision row; opens nothing (S1)
```

**Why decomposed (not a consolidated `signal.py` + `calibration_probe.py`):** the design mandates separating the
stateless `scan()` hot path from the rolling feature state and from the label/scoring surface. Each unit is
independently unit-testable; mirrors M2's granularity. Consolidation produces large files that tangle rolling
state, quote freshness, labels, and scoring — rejected.

### Data flow (one probe tick, stateless `scan()`)
```
bar/mid series ──(background refresh)──► FeatureEngine.snapshot(as_of=decision_ts_utc) ──┐
NBBO ────────────────────────────────────────────────────────────────────────────────────┤
market_state.TradabilityDecider / MarketStateCache ───────────────────────────────────────┴─► SignalSnapshot
                                                                                                  │
                                        Snapshot freshness / identity gate ── fail ───────► decision{action=do_nothing, reason} (STOP)
                                                                                                  │ pass
                                        QuoteQuality gate ── fail ───────────────────────► decision{action=do_nothing, reason} (STOP)
                                                                                                  │ pass
                                        MarketState gate ── not RTH-tradable / horizon leaves session ──► do_nothing (STOP)
                                                                                                  │ tradable & horizon in same RTH session
                                        forecast.predict(features) ──────────────────────► P(event) ∈ (0,1)
                                                                                                  │
                                        decision{action=forecast_only, forecast_id, forecast, edge_label,
                                                 signal_provenance, quote_provenance,
                                                 paper_eligible=FALSE}
                                                                                                  │
                                        schedule resolve @ resolve_bar_end_utc ──────────► calibration.resolve() reads MidBarSeriesReader,
                                                 emits forecast_scored{forecast_id, outcome∈{0,1}, brier_i}
```

## 3. Feature engine + label series (anti-lookahead)

### 3.1 As-of and horizon semantics (frozen)

Every probe tick carries explicit times:

- `decision_ts_utc` — the UTC instant the decision row is created.
- `decision_seen_at_ms` — injected monotonic clock for staleness calculations (same pattern as M2 cache clocks).
- `feature_cutoff_bar_end_utc` — latest completed feature bar whose `bucket_end_utc <= decision_ts_utc` **and** whose
  receipt/replay watermark is `<= decision_ts_utc`.
- `event_start_bar_end_utc` — completed mid-bar boundary used for `mid(t0)`. It must be `<= decision_ts_utc` and is
  usually the same as `feature_cutoff_bar_end_utc` for the chosen interval.
- `resolve_bar_end_utc` — `event_start_bar_end_utc + H`, aligned to the same ET bar boundary.

**No in-progress bars. No future receipts.** A bar with an end-time before `decision_ts_utc` is still not usable if
its event/receipt watermark or replay cursor says the agent would not have seen it by `decision_ts_utc`. The frozen
contract must specify the exact watermark field/API and include a fixture where a future-received bar would change a
feature if leaked.

**Session policy:** `event_start_bar_end_utc` and `resolve_bar_end_utc` must both be inside the same continuous RTH
session according to M2 `MarketCalendar` / `SessionSchedule`. Half-days use the M2 schedule; the M1 resampler's
ET-date bucketing is not sufficient by itself. If the horizon crosses RTH close, a halt/auction, a missing calendar
coverage date, or a session boundary, the decision is `do_nothing` or the forecast is later `unresolved` with a
logged reason — never guessed.

### 3.2 Feature engine

Computed at decision time `t0` using **only** completed bars at or before `feature_cutoff_bar_end_utc`. Rolling state
lives in `FeatureEngine`, refreshed on a background cadence; `scan()` reads an immutable snapshot.

- **Returns:** log returns over the base bar interval.
- **Trend:** `SMA(w)`, `EMA(w)` for `w ∈ feature_windows` (default `{9, 21, 50}` bars); `momentum_w = close_t/close_{t−w} − 1`.
- **RSI:** Wilder RSI, period `rsi_period` (default 14).
- **Rolling z-score of returns:** `z = (r_t − mean_w(r)) / std_w(r)`. **S2 guard:** `std_w == 0` (or window < min
  samples) ⇒ `z = 0`, never NaN/Inf.
- **Realized vol:** rolling std of returns over `vol_window`; **S2 guard:** empty / single-sample window ⇒ defined
  sentinel (`0` plus `available=false`), never NaN/Inf.

Every feature value is `Decimal`-as-string on the wire; floats are confined to the engine's internal numerics and
sanitized at the boundary (the S2 re-verification point for M3 — see §6).

### 3.3 Mid-bar label series (`bar_series.py`)

M3 may not silently treat the existing M1 OHLCV `Bar` as a BBO label. The frozen contract must choose and implement
one explicit API:

```python
@dataclass(frozen=True)
class MidBar:
    symbol: str
    instrument_id: int
    interval: str                  # "1s" | "1m"; contract chooses the default
    bucket_start_utc: str
    bucket_end_utc: str            # completed bar boundary used for t0/tH
    session_date_et: str
    bid: Decimal
    ask: Decimal
    mid: Decimal                   # (bid + ask) / 2, Decimal, finite, > 0
    source_dataset: str            # e.g. "EQUS.MINI"
    source_schema: str             # "bbo-1s" / "bbo-1m" / explicit tbbo-resample adapter
    data_pin: str                  # dataset/schema/range/fixture pin
    quote_provenance: dict         # ts_event_utc/ts_recv_utc/reconnect_epoch/vendor_seq range
```

`MidBarSeriesReader.get(symbol, instrument_id, bucket_end_utc) -> MidBar | MissingBar` is the sole label read used
by `calibration.resolve()`. If the contract chooses OHLCV `close` as a documented fallback, that fallback must be a
separate `source_schema="ohlcv-*"` row with `fallback_reason`, and reports must break out primary-vs-fallback
samples. Outcomes are never mixed without provenance.

## 4. SignalSnapshot provenance (atomic means explicit)

`SignalSnapshot` is frozen and must include enough provenance to replay exactly what the probe knew at decision time:

- `symbol`, `instrument_id`, `decision_id`, `decision_ts_utc`, `decision_seen_at_ms`, `rules_hash`.
- `feature_cutoff_bar_end_utc`, `feature_snapshot_id`, `feature_data_pin`, `feature_watermark_utc`.
- `quote_ts_event_utc`, `quote_ts_recv_utc`, `quote_seen_at_ms`, `reconnect_epoch`, `vendor_seq`, `quote_dataset`,
  `quote_schema`.
- NBBO `bid`, `ask`, `bid_sz`, `ask_sz`, `mid` as finite Decimal values/strings.
- M2 market-state verdict: `tradability`, `session_state`, `reasons`, `ca_blackout`, `refreshed_at_ms` or explicit
  safe-default marker, plus `calendar_pin` / session date.
- `event_start_bar_end_utc`, `resolve_bar_end_utc`, horizon, threshold `k`.

Snapshot construction fails closed (`do_nothing`) if identities mismatch (`symbol`/`instrument_id`), the market-state
cache is missing/stale and returns safe default, NBBO is stale/one-sided/crossed/locked/non-positive, feature state is
stale, or the horizon cannot be proven to remain in the same RTH session.

## 5. Forecast model + scoring

**Model (`forecast.predict`):** a deterministic parametric map `feature_vector → P(event)` (logistic form), with
**config-pinned coefficients** hashed into `rules_hash`. **No online learning / no re-fitting in M3** — keeps the
probe reproducible and anti-lookahead-clean. A poorly-calibrated model is a *measurement result*, not a failure;
post-hoc calibration (Platt/isotonic) is an M7 concern.

**Numerical guard:** logistic evaluation must use a stable implementation (`z` clamp or branch-stable sigmoid) so
extreme coefficients/features produce `0 < p < 1` without NaN/Inf. Output probability is serialized as a Decimal
string at the boundary. Non-finite input feature values are rejected before prediction.

**Event resolution (`calibration.resolve`):** both `mid(t0)` and `mid(t0+H)` come from `MidBarSeriesReader` using the
same source series (`source_dataset`, `source_schema`, `data_pin`) unless a separately-provenanced fallback policy is
explicitly enabled in the frozen contract. `outcome = 1 if mid(t0+H) >= mid(t0)·(1+k) else 0`. If **no mid bar exists
at `resolve_bar_end_utc`** (gap/halt/session end), the forecast is **unresolved** → excluded from scoring with a
logged reason. Outcomes are never guessed.

**Scoring (`calibration`):**
- **Brier score:** `BS = mean((p_i − o_i)^2)`.
- **Murphy decomposition** over `K` probability bins (default 10): `BS = Reliability − Resolution + Uncertainty`,
  where `Uncertainty = base_rate·(1 − base_rate)`. Reliability ↓ better, Resolution ↑ better.
- **Reliability diagram:** per-bin `(mean_forecast_p, observed_freq, count)`.
- **Reference / skill:** Brier Skill Score `BSS = 1 − BS_model / BS_ref` vs the as-of/training climatology forecaster
  (D5); the constant-0.5 forecaster is also reported as a floor. If `BS_ref == 0`, BSS is `unavailable` with reason
  `zero_reference_brier`, never NaN/Inf.
- Reported **per `(symbol × horizon)`** and aggregate, with sample counts (so thin cells are visible, never hidden).
- Reported with **funnel counts:** snapshots seen → quote rejected → market-state/session rejected → horizon rejected
  → forecasted → unresolved → scored. Calibration quality is not interpreted without the funnel.

**Reference base-rate / edge label:** decision rows may carry `reference_base_rate_asof_t0` only if it is computed
from prior resolved samples (`resolved_ts_utc < decision_ts_utc`) or a committed fixed training fixture. Full-run
climatology is report-only and must never be copied into decision-time `edge_label`. For M3, `edge_label` is a pure
observation label (`p - reference_base_rate_asof_t0`) and carries **no** sizing/order semantics.

**Quote-quality (`quote_quality`, bps):** `spread_bps = (ask − bid)/mid · 1e4`. Reject if `bid <= 0`, `ask <= 0`,
`mid <= 0`, `spread_bps > spread_bps_max`, crossed (`bid > ask`), locked (`bid == ask`), stale (`age_ms >
staleness_ms_max` using injected monotonic ms), or one-sided / missing side. A failing quote ⇒ `do_nothing` with
reason; **no forecast is scored on a rejected quote**.

## 6. Safety invariants

| ID | Invariant (M3) | Test |
|---|---|---|
| **S1** | The probe mints **no** preflight token, calls **no** `submit_order`, flips **no** gate, imports no broker/preflight module, and emits only `decision` / `forecast_scored` rows. `ForecastDecision` is not an `OrderIntent` / `Candidate`; `action="forecast_only"` is a labeled observation, not an order. `paper_eligible == False` on every row. | Static AST + import assertion that probe/M3 modules do not import `agent.broker`, `agent.execution_preflight`, `OrderIntent`, `submit_order`, or `mint_*token`; behavioral canary that committed config produces zero broker calls and zero order intents. |
| **S2** | NaN/Inf never reaches a price/size/feature/probability field. **Re-verified where floats are born in M3:** zero-variance z-score, empty-window realized-vol, stable logistic, BSS `BS_ref==0`, quote-quality `mid<=0`. | NaN/Inf-injection tests on feature engine / forecast / scoring / quote-quality boundaries + serializer round-trip. |
| **S3** | Feature engine and label resolver are **anti-lookahead**: a feature/label at `t0` depends only on bars with completed end-time and observed/replay watermark `<= decision_ts_utc`; future-received bars cannot affect decisions. | Fixture where a future bar or future-received bar would change a feature/outcome if leaked — asserts no change. |
| **S4** | Forecast resolution is **deterministic, idempotent, and gap-honest**: missing `t0+H` mid bar ⇒ unresolved + logged, never a guessed outcome; resolver rerun does not duplicate scores. | Gap/halt/session-end fixtures assert exclusion + reason; rerun resolver asserts one score per `forecast_id` after replay/dedupe. |
| **S6** | Cross-stream rows correlate by `run_id`/`decision_id`/`forecast_id` with monotonic per-stream `seq`; every decision/scored row carries `rules_hash` and `data_pin`. | Journal correlation/idempotency tests; replay verifies deterministic `forecast_id` and row hashes. |

## 7. Journal + config

**Streams** (event-sourced JSONL, `sort_keys`, `Decimal`-as-string, row hash, per-stream monotonic `seq`, single
writer lock — parent §7 conventions):

- `decisions.jsonl` — `decision` rows: `symbol, instrument_id, strategy, action ∈ {do_nothing, forecast_only},
  decision_id, forecast_id, forecast {event, H, k, p}, reference_base_rate_asof_t0, edge_label,
  signal_provenance, quote_provenance, market_state_provenance, feature_snapshot_id, event_start_bar_key,
  resolve_bar_key, data_pin, rules_hash, paper_eligible=false`.
- `forecast_scored.jsonl` — resolved outcomes: `forecast_id, decision_id, outcome ∈ {0,1}, brier_i,
  resolved_ts_utc, event_start_bar_key, resolve_bar_key, data_pin, rules_hash` (idempotent by `forecast_id`; links
  back to the decision row).
- `data_quality_alerts.jsonl` — optional M3 alerts for unresolved/gap/future-watermark/invalid-mid cases if the
  frozen contract chooses to emit them separately from `forecast_scored` unresolved rows.
- **Calibration report artifact:** `reports/calibration/<run_id>.json` (+ rendered Markdown/SVG if desired) —
  git-ignored / reproducible; report rendering is stdlib-only in M3 unless a dependency is separately approved and
  lazily imported.

**Deterministic identifiers:**

`forecast_id = row_hash(canonical_forecast_input)` where the canonical input includes: `run_id`, `decision_id`,
`symbol`, `instrument_id`, `strategy`, `rules_hash`, `data_pin`, `model_version`, `feature_snapshot_id`,
`event_start_bar_key`, `resolve_bar_key`, `H`, `k`, `p`, and `reference_forecaster_id`. The exact field list is
frozen in the contract. The resolver must replay existing `forecast_scored` rows before appending; reports must also
dedupe by `forecast_id` deterministically.

**Config additions / `rules_hash`:**

- Add a committed base config block such as `agent_rules.signal` for M3 provenance while keeping run/live gates OFF.
- Encode model coefficients, feature windows, horizons, thresholds, and scoring bins as immutable committed values
  (prefer Decimal strings / string lists / named model artifact hash) so `tighten_only_merge()` cannot reinterpret
  coefficients via numeric `min()`.
- Runtime/local overlays may only tighten true safety thresholds if the frozen contract implements an explicit
  direction-aware merge. In M3, default posture is stricter: overlays cannot modify model coefficients/windows; a
  changed model is a new commit with a new `rules_hash`.
- Every decision, score, and report carries the assembled `rules_hash` plus `model_version` / `model_artifact_hash`.
- `config/agent_rules.json → enabled=false` and `paper_trading.enabled=false`; `config/risk_rules.json →
  live_trading.enabled=false` stay unchanged.

## 8. Module boundaries & dependencies (what each unit needs)

- `feature_engine` → reads completed bar/mid series via injected readers; depends on nothing in Tier 4. Owns rolling
  state and watermarks.
- `bar_series` → owns `MidBar` / `MidBarSeriesReader`; adapts persisted quote/bbo/ohlcv rows into an explicit label
  series with provenance. No silent fallback.
- `quote_quality` → pure function of an NBBO snapshot + injected monotonic clock; no I/O.
- `signal_snapshot` → composes feature snapshot + NBBO + M2 market-state verdict; pure assembly + fail-closed
  identity/freshness validation.
- `strategy`/`candidate` → pure type/protocol definitions; no dependencies. M3 probe does not emit `Candidate`.
- `forecast` → pure function of a feature vector + pinned coefficients; no I/O.
- `calibration` / `calibration_report` → read `MidBarSeriesReader` (resolve) + `forecast_scored` stream; pure
  scoring math, no network, no heavy plotting dependency.
- `strategies/calibration_probe` → orchestrates the above; the ONLY stateful-per-tick unit; **never** imports the
  broker/preflight modules (enforced by S1 test).

## 9. Offline purity / dependency rule

M3 remains stdlib-only for the offline suite unless Robin explicitly approves a new dependency. The frozen contract
must extend `tests/agent/test_no_network_no_creds.py` to import every new M3 module and assert no `alpaca`,
`databento`, `exchange_calendars`, plotting, ML, or stats SDK is pulled into `sys.modules` at module scope. The
socket-patched no-network test must exercise a minimal feature → snapshot → forecast → score path.

Report rendering should default to reproducible JSON + Markdown/SVG generated with stdlib. If a plotting dependency
is ever added, it must be pinned, lazy-imported, and excluded from the bare offline path.

## 10. Open items deliberately left to the frozen contract

- Exact `model_coefficients` values, `model_version`, `model_artifact_hash`, and logistic feature standardization
  constants.
- The exact `MidBarSeriesReader` source policy: vendor `bbo-1m`/`bbo-1s` vs deterministic `tbbo` resample, and
  whether OHLCV close fallback is allowed at all. If allowed, fallback reporting must be separate.
- Background-refresh cadence default (`refresh_cadence_ms`) and the staleness budget calibrated to the feed.
- Exact probability binning policy (closed/open edges, empty-bin representation) and minimum sample warnings.
- Fixture roster (mid-bar fixtures per horizon, future-received-bar leakage fixtures, gap/halt/session-end fixtures,
  NaN/Inf-injection fixtures, zero-reference-Brier fixtures) and the test→invariant map — produced in the contract,
  mirroring M2 §H/§J.
