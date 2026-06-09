# M3 — Signal + observe-only calibration probe (DESIGN)

> **Status:** DESIGN (pre-contract). This document fixes the M3 scope, the load-bearing decisions, the module
> decomposition, and the safety invariants. The **build-ready frozen contract** (module-by-module APIs, code
> skeletons, fixtures, test→invariant map) is produced from this design via the architect-panel → critic →
> revision workflow, mirroring `2026-06-09-M2-market-state-contract.md`.
>
> **Branch:** `m3-signal` (off `main` @ `a82be6d`, M2 merged). Authoritative parent design:
> `docs/superpowers/specs/2026-06-08-stocks-agent-design.md` (§5 Tier 3/4, §7 data model, §8 safety table).

## 0. Scope

M3 delivers **Tier 3 (snapshot/signal)** and the **observe-only slice of Tier 4 (the calibration probe)**:

1. A **feature/indicator engine** fed by the M1 bar cache, holding rolling state outside the stateless `scan()`
   hot path, anti-lookahead by construction.
2. **Quote-quality filters** re-expressed in **bps** (reusing the Polymarket `summarize_book()` depth/spread math),
   warnings-as-data.
3. The `Strategy` Protocol + frozen `ScanContext` and `Candidate`/`Leg` types — **introduced in this repo for the
   first time** (no `strategy.py`/`candidate.py` exists today).
4. An **observe-only calibration probe** that emits **probabilistic event/threshold forecasts**, opens nothing
   (`paper_eligible=False`), and journals `decision` rows only.
5. A **realized-move scorer** (forecast vs realized outcome) + a **calibration report** (Brier decomposition,
   reliability diagram, skill vs a reference forecaster).

**Out of scope (deferred):** any order submission, preflight-token mint, paper/live execution (M5); risk gates /
`can_open()` / margin / locate (M4); the anti-lookahead historical **backtest gate** that proves realized edge
(M7); directional momentum/mean-reversion strategies (post-M7); online learning / model re-fitting.

## 1. Load-bearing decisions (DECIDED — Robin, 2026-06-09)

| # | Decision | Rationale |
|---|---|---|
| D1 | **Forecast form = probabilistic event/threshold.** The probe emits `P(event) ∈ [0,1]`. | Calibration is *defined* for probability forecasts; Brier + reliability diagram is the canonical, unambiguous metric. S1 is trivially safe — a probability carries no order semantics. |
| D2 | **Event = `mid(t0+H) ≥ mid(t0)·(1+k)`**, horizons `H ∈ {5m, 30m}` (configured set), threshold `k` configurable (**default 0** → pure up-move). Realized purely from the **bar cache** at the ET-boundary-aligned `t0+H` close. | Many scoring samples per symbol per day → the reliability diagram populates fast; no new data source beyond M1's bar cache. |
| D3 | **Genuine first predictive model** using the full feature set (MAs/RSI/z-scores/realized-vol) → probabilities, **not** a trivial baseline. | Robin's call. Constrained by D4 below. |
| D4 | **M3 produces calibration *evidence*, never an edge *claim*.** "Has edge → paper-eligible" remains **M7's** anti-lookahead backtest gate. The probe stays observe-only (`paper_eligible=False`), opens nothing (S1). The feature engine is **anti-lookahead by construction**. | Reconciles D3 with the design's load-bearing rule that edge is not "proven" without the M7 gate. Building the model now forces anti-lookahead discipline into the feature layer from day one. |
| D5 | A **null/reference forecaster** (climatology base-rate + constant 0.5) is scored alongside the model **as a control in the report**, not as a competing probe output. | A reliability diagram / Brier score is uninterpretable without a baseline (is Brier 0.21 good?). Brier Skill Score vs climatology makes the number legible. Standard scoring practice, cheap. |

## 2. Architecture — decomposed modules (one purpose each)

```
scripts/agent/
  feature_engine.py        FeatureEngine — rolling state OUTSIDE scan(), background refresh, anti-lookahead
  quote_quality.py         QuoteQuality — bps spread/cross/lock/staleness/one-sided filters, warnings-as-data
  signal_snapshot.py       SignalSnapshot — atomic bundle {NBBO, market-state, feature vector, provenance}
  strategy.py              Strategy Protocol + frozen ScanContext            (NEW to repo)
  candidate.py             Candidate / Leg (single-leg = N=1)                (NEW to repo)
  forecast.py              Forecast / ForecastEvent + the predictive model (fixed config-pinned coefficients)
  calibration.py           realized-move resolver + scoring (Brier, Murphy decomposition, BSS, reference)
  calibration_report.py    renders reliability diagram + decomposition + per-(symbol×horizon) breakdown
  strategies/
    calibration_probe.py   observe-only probe: ScanContext → Forecast → decision row; opens nothing (S1)
```

**Why decomposed (not a consolidated `signal.py` + `calibration_probe.py`):** the design mandates separating the
stateless `scan()` hot path from the rolling feature state; each unit is independently unit-testable; mirrors M2's
granularity. Consolidation produces large files that tangle rolling state with the hot path — rejected.

### Data flow (one probe tick, stateless `scan()`)
```
bar cache ──(background refresh)──► FeatureEngine.snapshot()  ──┐
NBBO ───────────────────────────────────────────────────────────┤
market_state.TradabilityDecider ────────────────────────────────┴─► SignalSnapshot (atomic, provenance-stamped)
                                                                         │
                                          QuoteQuality gate ── fail ─────► decision{action=do_nothing, reason}   (STOP)
                                                                         │ pass
                                          MarketState gate  ── not RTH-tradable / horizon leaves session ──► do_nothing (STOP)
                                                                         │ tradable & horizon in-session
                                          forecast.predict(features) ──► P(event) ∈ (0,1)
                                                                         │
                                          decision{action ∈ {do_nothing, would_open}, forecast, edge,
                                                   signal_provenance, quote_provenance, paper_eligible=FALSE}
                                                                         │  (would_open is a LABELED HYPOTHETICAL — never submitted)
                                          schedule resolve @ t0+H ──► calibration.resolve() reads bar cache,
                                                   emits forecast_scored{forecast_id, outcome∈{0,1}, brier_i}
```

## 3. Feature engine (anti-lookahead)

Computed at decision time `t0` using **only** bars whose end-time `≤ t0` (no in-progress / future bar leaks).
Rolling state lives in `FeatureEngine`, refreshed on a background cadence; `scan()` reads an immutable snapshot.

- **Returns:** log returns over the base bar interval.
- **Trend:** `SMA(w)`, `EMA(w)` for `w ∈ feature_windows` (default `{9, 21, 50}` bars); `momentum_w = close_t/close_{t−w} − 1`.
- **RSI:** Wilder RSI, period `rsi_period` (default 14).
- **Rolling z-score of returns:** `z = (r_t − mean_w(r)) / std_w(r)`. **S2 guard:** `std_w == 0` (or window < min
  samples) ⇒ `z = 0`, never NaN/Inf.
- **Realized vol:** rolling std of returns over `vol_window`; **S2 guard:** empty / single-sample window ⇒ defined
  sentinel (0 or `unavailable` flag), never NaN/Inf.

Every feature value is `Decimal`-as-string on the wire; floats are confined to the engine's internal numerics and
sanitized at the boundary (the S2 re-verification point for M3 — see §5).

## 4. Forecast model + scoring

**Model (`forecast.predict`):** a deterministic parametric map `feature_vector → P(event)` (logistic form), with
**config-pinned coefficients** hashed into `rules_hash`. **No online learning / no re-fitting in M3** — keeps the
probe reproducible and anti-lookahead-clean. A poorly-calibrated model is a *measurement result*, not a failure;
post-hoc calibration (Platt/isotonic) is an M7 concern.

**Event resolution (`calibration.resolve`):** both `mid(t0)` and `mid(t0+H)` are the **NBBO mid from the `bbo` bar**
at the ET-boundary (same series on both ends — apples-to-apples; the `ohlcv` close is a documented fallback only if
the `bbo` bar is unavailable, decided in the contract). Read the bar cache at the ET-boundary-aligned `t0+H`;
`outcome = 1 if mid(t0+H) ≥ mid(t0)·(1+k) else 0`. If **no bar exists at `t0+H`** (gap/halt/session end), the
forecast is **unresolved** → excluded from scoring with a logged reason. Outcomes are never guessed.

**Scoring (`calibration`):**
- **Brier score:** `BS = mean((p_i − o_i)²)`.
- **Murphy decomposition** over `K` probability bins (default 10): `BS = Reliability − Resolution + Uncertainty`,
  where `Uncertainty = base_rate·(1 − base_rate)`. Reliability ↓ better, Resolution ↑ better.
- **Reliability diagram:** per-bin `(mean_forecast_p, observed_freq, count)`.
- **Reference / skill:** Brier Skill Score `BSS = 1 − BS_model / BS_ref` vs the climatology base-rate forecaster
  (D5); the constant-0.5 forecaster is also reported as a floor.
- Reported **per `(symbol × horizon)`** and aggregate, with sample counts (so thin cells are visible, never hidden).

**Quote-quality (`quote_quality`, bps):** `spread_bps = (ask − bid)/mid · 1e4`. Reject if `spread_bps >
spread_bps_max`, crossed (`bid > ask`), locked (`bid == ask`), stale (`age_ms > staleness_ms_max`), or one-sided /
missing side. A failing quote ⇒ `do_nothing` with reason; **no forecast is scored on a rejected quote**.

## 5. Safety invariants

| ID | Invariant (M3) | Test |
|---|---|---|
| **S1** | The probe mints **no** preflight token, calls **no** `submit_order`, flips **no** gate, and emits only `decision` / `forecast_scored` rows. `would_open` is a labeled hypothetical that never reaches a broker/preflight seam. `paper_eligible == False` on every row. | Static + behavioral assertion that no probe path imports/reaches the broker or preflight modules; canary that committed-config produces zero opening intents. |
| **S2** | NaN/Inf never reaches a price/size/feature field. **Re-verified where floats are born in M3:** zero-variance z-score (÷0) and empty-window realized-vol. | NaN/Inf-injection tests on the feature engine boundary + serializer round-trip. |
| **S3** | Feature engine is **anti-lookahead**: a feature at `t0` depends only on bars with end-time `≤ t0`. | A fixture replaying bars where a future bar, if leaked, would change the feature — asserts no change. |
| **S4** | Forecast resolution is **deterministic and gap-honest**: missing `t0+H` bar ⇒ unresolved + logged, never a guessed outcome. | Gap/halt fixtures assert exclusion + reason, not a fabricated 0/1. |

## 6. Journal + config

**Streams** (event-sourced JSONL, `sort_keys`, `Decimal`-as-string, row hash, per-stream monotonic `seq`, single
writer lock — §7 conventions):
- `decisions.jsonl` — `decision` rows: `symbol, strategy, action ∈ {do_nothing, would_open}, forecast {event, H,
  k, p}, edge, signal_provenance, quote_provenance, paper_eligible=false`. For M3's probabilistic probe `edge` is a
  **pure label** = `p − reference_base_rate` (the model's deviation from the climatology reference, D5); it carries
  **no** sizing/order semantics and is never consumed by a gate.
- `forecast_scored.jsonl` — resolved outcomes: `forecast_id, outcome ∈ {0,1}, brier_i, resolved_ts_utc` (idempotent
  by `forecast_id`; links back to the decision row).
- **Calibration report artifact:** `reports/calibration/<run_id>.json` (+ rendered) — git-ignored / reproducible.

**Config additions** (hashed into `rules_hash`, tighten-only where a threshold has a safe direction): `feature_windows`,
`rsi_period`, `vol_window`, `horizons` `{5m,30m}`, `threshold_k` (default 0), `quote_quality {spread_bps_max,
staleness_ms_max}`, `model_coefficients`, `scoring_bins`, `refresh_cadence_ms`. Run/live gates stay **OFF**.

## 7. Module boundaries & dependencies (what each unit needs)

- `feature_engine` → reads `recorder/bar_cache`; depends on nothing in Tier 4. Owns rolling state.
- `quote_quality` → pure function of an NBBO snapshot; no I/O.
- `signal_snapshot` → composes `feature_engine` snapshot + NBBO + `market_state` decider output; pure assembly.
- `strategy`/`candidate` → pure type/protocol definitions; no dependencies.
- `forecast` → pure function of a feature vector + pinned coefficients; no I/O.
- `calibration` / `calibration_report` → read bar cache (resolve) + `forecast_scored` stream; pure scoring math.
- `strategies/calibration_probe` → orchestrates the above; the ONLY stateful-per-tick unit; **never** imports the
  broker/preflight modules (enforced by S1 test).

## 8. Open items deliberately left to the frozen contract

- Exact `model_coefficients` values and the logistic feature standardization constants.
- Background-refresh cadence default (`refresh_cadence_ms`) and the staleness budget calibrated to the feed.
- Whether `would_open` is emitted at all in M3 or deferred until M5 (it is *defined* now; emission is a probe-policy
  knob — default may be `do_nothing`-only to keep M3 forecasts pure observation).
- Fixture roster (bar-cache replay fixtures per horizon, gap/halt fixtures, NaN/Inf-injection fixtures) and the
  test→invariant map — produced in the contract, mirroring M2 §H/§J.
