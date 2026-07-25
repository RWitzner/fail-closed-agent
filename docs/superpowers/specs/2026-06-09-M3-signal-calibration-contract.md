# M3 (Signal + observe-only calibration probe) — FROZEN CONTRACT (READY-TO-BUILD, rev 2)

> **Status:** READY-TO-BUILD, 2026-06-09 (rev 2). Produced from the review-hardened design
> `2026-06-09-M3-signal-calibration-design.md` (decisions D1–D6 locked by Robin), then revised through a
> 4-lens adversarial critic pass (repo-facts / buildability / safety-invariants / calibration-math; 52
> findings — 5 blockers, 26 majors, 21 minors — ALL applied; see §P revision log). Mirrors the M2 contract
> (`2026-06-09-M2-market-state-contract.md`) in granularity: module-by-module APIs, code skeletons, frozen
> vocabularies, fixtures, and a test→invariant map. The build agent follows it without relitigating.
>
> **Branch:** `m3-signal` (off `main` @ `e19118f`). Baseline suite: 532 tests green.

## 0. Scope, ground rules, verified repo facts

**In scope (design §0):** `feature_engine.py`, `bar_series.py`, `quote_quality.py`, `signal_snapshot.py`,
`signal_config.py` (rev2: REPO-F7), `strategy.py`, `candidate.py`, `forecast.py`, `calibration.py`,
`calibration_report.py`, `strategies/calibration_probe.py`; one **additive** M2 edit
(`MarketCalendar.schedule_for`/`.calendar_pin` passthroughs — rev2: BUILD-F2; no M2 behavior change);
journal streams `decisions.jsonl` + `forecast_scored.jsonl`; config block
`agent_rules.signal`; calibration report artifact. **Out of scope:** order submission, preflight mint, risk
gates/`can_open()`, backtest gate, directional strategies, online learning, any network/credential call.

**Ground rules (unchanged from M0–M2):**

- Committed gates stay OFF: `agent_rules.enabled=false`, `paper_trading.enabled=false`,
  `live_trading.enabled=false`. M3 adds **no** gate and flips **no** gate.
- Offline suite is stdlib-only; no new dependency. Tests make no network calls and read no credentials.
- Determinism conventions: canonical `dumps()` (`serializer.py:50`), Decimal-as-string, per-row sha256 hash
  (`serializer.py:53-55`), per-stream monotonic `seq` under a shared per-path lock (`journal.py:62-81`),
  injected clocks only (no wall-clock reads in M3 modules).
- All market logic in ET; persisted timestamps UTC ISO-8601 (`ts_utc`); monotonic ms for staleness.
- **Timestamp comparison discipline (S3-load-bearing; critic SAFETY-F2):** the repo provably mixes two
  ISO-8601 surface forms for the same instant — recorder `ts_recv_utc` comes from
  `_ms_to_iso_utc` (`recorder.py:142-148`, `isoformat().replace("+00:00","Z")`, which OMITS fractional
  seconds at whole seconds), while bar-bucket strings always carry `.%f` (six digits,
  `bar_cache.py:137,155`). Lexicographic comparison across forms is WRONG in the eligible direction
  (`'.'(0x2E) < 'Z'(0x5A)` — the exact bug class bar_cache H2 fixed). Therefore: **every** M3 timestamp
  comparison (`watermark_utc <= as_of`, bucketing, horizon arithmetic, `resolve_bar_end_utc <= now_utc`,
  `<= rth_close_utc`) operates on **parsed UTC instants** via a shared `_parse_utc`-equivalent (mirror
  `bar_cache._parse_utc`), never on raw strings. Every M3-**minted** timestamp (`bucket_start_utc`,
  `bucket_end_utc`, `resolve_bar_end_utc`, bar keys, the golden `generated_ts_utc`) uses the ONE canonical
  surface form `strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"` so bar-key strings are byte-stable. §M.2/§M.3 carry
  mixed-form boundary cases (equal instants in different forms compare equal; a watermark
  `…00.500000Z` vs as_of `…00Z` is INELIGIBLE).

**Verified repo facts this contract builds on (file:line at `7902677`):**

| Fact | Source |
|---|---|
| `Bar` is OHLCV/VWAP only — **no bid/ask/mid**, no watermark field; `resample()` yields **non-empty** buckets only (skips empties; it has **no completion/watermark check** — the final bucket may be in-progress; completedness is exclusively the consumer's FD-2 as-of concern) | `scripts/recorder/bar_cache.py:65-79,158,224-252` |
| Persisted quote rows: `schema, dataset, instrument_id, symbol, vendor_seq, ts_event_utc, ts_recv_utc, reconnect_epoch` + `bid_px/bid_sz/ask_px/ask_sz` | `scripts/recorder/event_row.py:34-55,87-90` |
| `ts_recv_utc` derives from injected ms clock via `_ms_to_iso_utc` (deterministic offline) | `scripts/recorder/recorder.py:142-148` |
| Recorder streams: `events`, `data_quality_alerts`, `status`; `EventWriter.write_event/record`, `replay_stream` | `scripts/recorder/persistence.py:46-49,66-108` |
| Journal: `JournalWriter(path, run_id, clock).append(event_type, fields, *, decision_id, order_id)`; `_RESERVED={event_type,run_id,seq,hash,decision_id,order_id,ts_utc}`; `replay(path)` hash-verifies, drops only truncated tail | `scripts/agent/journal.py:21,28-58,91-130` |
| Canonical serializer: floats rejected, non-finite Decimal rejected, `row_hash()` = sha256 of canonical JSON | `scripts/agent/serializer.py:27-55` |
| Config: `load()`, `rules_hash()` over the assembled dict (`allow_nan=False`), `tighten_only_merge()` = bools AND / numerics min / dicts recurse / **anything else keeps base** / overlay-only keys dropped | `scripts/agent/config.py:13-43` |
| M2 config precedent: `agent_rules.market_state` holds **provenance strings only**; safety numbers are code constants | `config/agent_rules.json:11-14`, `tests/agent/test_config_market_state.py:30-43` |
| M2 verdict surface: `Verdict{symbol,instrument_id,session_state,tradability,halt,luld,ssr,two_sided_nbbo,short_allowed,reasons,ca_blackout,session_date_et}`; tighten-only `merge_severity` | `scripts/agent/market_state.py:86-89,184-209` |
| `MarketStateCache.get(symbol,instrument_id,session_date_et,*,now_ms)` → fresh verdict or `safe_default_verdict` with `reasons=("cache_stale_safe_default",)`; `DEFAULT_FRESHNESS_TTL_MS=2000`, strict `>` staleness | `scripts/agent/market_state_cache.py:34,52-125` |
| Calendar: `MarketCalendar.phase_at(ts_utc_iso)`, `session_date_for()`, `SessionSchedule{rth_open_utc,rth_close_utc,is_early_close,...}`; `FixtureScheduleProvider` raises `UnknownSessionDate` out-of-coverage; `calendar_pin()` | `scripts/agent/market_calendar.py:60-85,167-225,261-316` |
| `Nbbo{symbol,best_bid,best_ask,bid_sz,ask_sz,ts_utc}` + fail-closed `two_sided` | `scripts/agent/market_state.py:117-137` |
| S1 surface to forbid: see **FD-12, the authoritative closed set** (modules `agent.broker*`, `agent.execution_preflight`, `agent.kill_switch`, `agent.arming`; tokens incl. `require_token`/`consume`/`PreflightToken`) | `scripts/agent/execution_preflight.py:40-118`, `scripts/agent/broker/base.py:18-37` |
| Socket-block + sys.modules purity + AST module-scope-import test patterns | `tests/agent/test_no_network_no_creds.py:22,51-68,89-115` |
| Config canary pattern (real committed config; identity-False gates; overlay cannot loosen) | `tests/agent/test_config_canary.py:24-64` |
| Quantization precedent: `PRICE_QUANTUM=Decimal("0.0001")` ROUND_HALF_EVEN with round-trip check | `scripts/agent/status_ledger.py:93-105` |
| Ledger precedent: `StatusLedger(writer: EventWriter, *, rules_hash)` validating-writer pattern | `scripts/agent/status_ledger.py:217-326` |
| Test clock: `tests/lib/fakes.py:83-94 FakeClock(now_ms/advance)`; imports via `tests/__init__.py` `scripts/` shim | `tests/lib/fakes.py:83-94` |

## 1. Frozen decisions (FD-1 … FD-14) — resolves design §10

| # | Decision |
|---|---|
| FD-1 | **Label source = deterministic mid-bar resample of recorded quote rows** (`tbbo`/`bbo-*` schema rows from the M1 `events` stream), interval **"1m"**. **No OHLCV-close fallback in M3** — a missing mid bar makes the forecast `unresolved`, never silently relabeled. `MidBar.source_schema` records the **input** quote schema exclusively (e.g. `"tbbo"`); the derived label-series identity is carried by `interval` inside the `data_pin` — there is no `"mid-1m"` schema string anywhere (rev2: BUILD-F4). |
| FD-2 | **Watermark field is explicit:** `MidBar.watermark_utc` = `ts_recv_utc` of the **last contributing quote** of the bucket. A mid bar is *eligible at* `as_of` iff `bucket_end_utc <= as_of` **and** `watermark_utc <= as_of`. This is the S3 anti-lookahead predicate for both features and labels. |
| FD-3 | **Features are computed from the same mid-bar series** (`close := mid`). One provenance chain for features and labels; volume features deferred. |
| FD-4 | **Model = stable logistic over a frozen scale-free input vector** (§F), per-horizon coefficient sets (v1 values identical across horizons), coefficients/windows committed as Decimal-strings, `model_version="logit-mom-v1"`, `model_artifact_hash` = sha256 of the canonical JSON of `agent_rules.signal.model`. Standardization = identity (inputs are scale-free by construction); frozen explicitly. Coefficients are **untrained priors** — D4: M3 measures calibration, claims no edge. |
| FD-5 | **Quanta:** `MID_QUANTUM=Decimal("0.000001")` (exact for (bid+ask)/2 of 4dp prices), `PROB_QUANTUM=Decimal("0.000001")` with output clamp to `[1e-6, 1−1e-6]`, `FEATURE_QUANTUM=Decimal("0.00000001")` (8dp, boundary quantize without round-trip requirement — floats are approximate by nature), `BRIER_QUANTUM=Decimal("0.000000000001")` (1e-12, exact for squared 1e-6 probabilities), `BPS_QUANTUM=Decimal("0.01")`, `REPORT_QUANTUM=Decimal("0.00000001")` for report-level divisions. |
| FD-6 | **Reference forecaster (D5):** per `(symbol × horizon)` as-of climatology over **previously resolved** outcomes; below `min_reference_samples` (default "30") it degrades to constant `0.5` with `reference_forecaster_id="constant_0.5"`. Decision rows carry `reference_base_rate_asof_t0`, `reference_forecaster_id ∈ {"climatology_asof_v1","constant_0.5"}`, `reference_n`. Full-run climatology appears **only** in the report. |
| FD-7 | **Config posture (D6):** every value in `agent_rules.signal` is a **string, list of strings, or dict thereof** — `tighten_only_merge()` keeps base for non-bool/non-numeric/non-dict leaves, so overlays cannot alter any signal parameter at all. A changed model is a new commit ⇒ new `rules_hash`. |
| FD-8 | **Unresolved is terminal per `forecast_id`.** The resolver replays the `forecast_scored` stream first and skips any `forecast_id` already present (either event type). Late backfill ⇒ new run, never mutation. |
| FD-9 | **Tick granularity:** the probe ticks once per completed event-start bar per symbol. Pre-horizon gate failures emit **one** `do_nothing` row (`horizon=null`); the horizon gate and forecasts emit **one row per horizon**. `decision_id` and `forecast_id` are deterministic hashes (§I). |
| FD-10 | **Budgets (defaults, committed as strings):** `quote_staleness_ms_max="2000"` (mirrors `DEFAULT_FRESHNESS_TTL_MS`), `feature_staleness_ms_max="5000"`, `bar_lag_max_intervals="2"`, `refresh_cadence_ms="1000"`, `spread_bps_max="50"`. |
| FD-11 | **Binning:** `prob_bins="10"`; bins left-closed/right-open `[i/10,(i+1)/10)` with the last bin closed `[0.9,1.0]`; bin index = `min(int(p*10), 9)` computed in Decimal. Empty bin → `count=0`, `mean_forecast_p=null`, `observed_freq=null`. `0 < count < 30` → `thin=true`. |
| FD-12 | **The probe does NOT implement `Strategy`.** `strategy.py`/`candidate.py` are introduced as pure types for M5+; `CalibrationProbe` returns `ForecastDecision` rows only. S1 static guard (§M) — **this list is the authoritative closed set** (rev2: SAFETY-F9) — forbids M3 modules importing `agent.broker*`, `agent.execution_preflight`, `agent.kill_switch`, `agent.arming` (any scope), referencing the tokens `submit_order`, `mint_open_token`, `mint_reduce_only_token`, `OrderIntent`, `OpenPreflightToken`, `ReduceOnlyPreflightToken`, `PreflightToken`, `require_token`, `consume`, **and referencing `importlib` or `__import__` at all** (string-import bypass; M3 has no legitimate dynamic-import need — rev2: SAFETY-F6). A subprocess-isolated check (fixed argv array) additionally imports every M3 module fresh and asserts none of the forbidden modules land in `sys.modules`. |
| FD-13 | **Market-state gate for forecasting:** require `session_state == RTH` **and** `tradability == TRADABLE` (a `REDUCE_ONLY` or safe-default verdict ⇒ `do_nothing`). The probe never interprets `short_allowed` (no orders exist). |
| FD-14 | **Feature history requirement:** the model requires `max(feature_windows)+1 = 51` eligible completed mid bars in the current session-continuous lookback; fewer ⇒ `do_nothing(reason="features_unavailable")`. Within full history: zero std of returns ⇒ `z_ret=0` (defined); realized vol of a constant series ⇒ `0` (defined). |

**Frozen reason-code vocabulary** (closed set; `reasons` is a sorted tuple, mirroring M2):

```
identity_mismatch, feature_stale, feature_cutoff_mismatch, bar_lag_exceeded, features_unavailable,
quote_missing, quote_nonfinite, quote_nonpositive, quote_crossed, quote_locked,
quote_one_sided, quote_stale, spread_too_wide,
market_state_not_tradable, market_state_stale_default, market_state_not_rth,
calendar_unknown, session_horizon_crosses_close
```

**Gate order (frozen — determines funnel attribution):**
1. identity (symbol/instrument_id agree across feature snapshot, quote, market-state verdict)
2. features: availability, freshness (`feature_staleness_ms_max` monotonic), cutoff identity
   (`feature.feature_cutoff_bar_end_utc == event_start_bar_end_utc` ⇒ else `feature_cutoff_mismatch` —
   rev2: SAFETY-F5), and bar lag (`bar_lag_max_intervals` vs `decision_ts_utc`)
3. quote quality (§A)
4. market-state (FD-13; `cache_stale_safe_default` in verdict reasons ⇒ `market_state_stale_default`)
5. per-horizon session check (`resolve_bar_end_utc <= rth_close_utc` of the same `SessionSchedule`;
   schedule unavailable (`UnknownSessionDate` at fetch) ⇒ `calendar_unknown`, attributed HERE per horizon)

**Stop semantics (rev2: SAFETY-F4/BUILD-F13):** a failed gate **1–4** STOPs the tick with exactly ONE
journaled `do_nothing` row (`horizon=null`); later gates are not evaluated. Gate **5** is evaluated **per
horizon** and a failing horizon NEVER suppresses sibling horizons — each failing horizon emits its own
`do_nothing` row, each passing horizon emits its `forecast_only` row.

**Reason multiplicity (rev2: SAFETY-F7/REPO-F9):** within the failing stage, **ALL applicable** mapped
reasons are collected and sorted (mirroring §A's collect-all rule) — e.g. the safe-default market-state
verdict journals exactly `("market_state_not_rth","market_state_not_tradable","market_state_stale_default")`.

## A. `scripts/agent/quote_quality.py` — bps quote filters, pure, no I/O

```python
# scripts/agent/quote_quality.py
"""Quote-quality filters in bps. PURE function of a QuoteSnapshot + injected monotonic now_ms.
Warnings-as-data: returns a verdict, never raises on bad market data (raises only on
programming errors such as float inputs)."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Optional, Tuple

BPS_QUANTUM = Decimal("0.01")        # spread_bps reported at 2dp
MID_QUANTUM = Decimal("0.000001")    # FD-5; exact for (bid+ask)/2 of 4dp prices

@dataclass(frozen=True)
class QuoteSnapshot:
    """One NBBO observation with full provenance (event_row.py:34-55 field names)."""
    symbol: str
    instrument_id: int
    bid: Optional[Decimal]           # None == side missing
    ask: Optional[Decimal]
    bid_sz: Optional[Decimal]
    ask_sz: Optional[Decimal]
    ts_event_utc: str
    ts_recv_utc: str
    seen_at_ms: int                  # injected monotonic receipt stamp
    reconnect_epoch: int
    vendor_seq: Optional[int]
    dataset: str
    schema: str

@dataclass(frozen=True)
class QuoteVerdict:
    ok: bool
    reasons: Tuple[str, ...]         # sorted; subset of the frozen vocabulary (§1)
    mid: Optional[Decimal]           # quantized MID_QUANTUM; None unless both sides positive & finite
    spread_bps: Optional[Decimal]    # (ask-bid)/mid * 10_000, quantized BPS_QUANTUM; None if mid is None
    age_ms: int                      # now_ms - seen_at_ms

def evaluate(q: QuoteSnapshot, *, now_ms: int,
             spread_bps_max: Decimal, staleness_ms_max: int) -> QuoteVerdict: ...
```

Frozen semantics:

- Reject (each adds its reason; **all** applicable reasons are collected, then sorted):
  `quote_one_sided` (either side or size is None), `quote_nonfinite` (any non-finite Decimal),
  `quote_nonpositive` (`bid<=0 or ask<=0` or size `<=0`), `quote_crossed` (`bid>ask`), `quote_locked`
  (`bid==ask`), `quote_stale` (`age_ms > staleness_ms_max`, strict `>`, mirroring
  `market_state_cache.py:74-99`), `spread_too_wide` (strict `>`, compared on the **quantized
  `spread_bps` field** — the same value persisted on the verdict, never the raw quotient — rev2: MATH-Q8).
- `mid` and `spread_bps` are computed **iff both PRICE sides are present, positive, finite** (a size-derived
  `quote_one_sided`/`quote_nonpositive` does NOT suppress them — rev2: BUILD-F3) so rejected quotes stay
  inspectable; `mid=None` only when a price side is missing/non-positive/non-finite.
- Floats anywhere ⇒ `ValueError` (programming error; mirrors serializer float-reject posture).
- No wall clock, no I/O, no imports beyond stdlib.

## B. `scripts/agent/bar_series.py` — `MidBar` + deterministic resampler + `MidBarSeriesReader`

```python
# scripts/agent/bar_series.py
"""Explicit mid-bar label series (design §3.3). M1 OHLCV Bar is NOT a BBO label (bar_cache.py:65-79
has no bid/ask/mid) — this module owns the only label read used by calibration.resolve()."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple, Union

MID_QUANTUM = Decimal("0.000001")

@dataclass(frozen=True)
class MidBar:
    symbol: str
    instrument_id: int
    interval: str                  # "1m" (FD-1; only value built in M3)
    bucket_start_utc: str          # ET-boundary bucket open, UTC ISO (mirrors bar_cache.py:69)
    bucket_end_utc: str            # exclusive close — the t0/tH key
    session_date_et: str
    bid: Decimal                   # last valid quote of the bucket
    ask: Decimal
    mid: Decimal                   # (bid+ask)/2 quantized MID_QUANTUM; finite, > 0
    watermark_utc: str             # FD-2: ts_recv_utc of the contributing quote (S3 predicate)
    source_dataset: str            # e.g. "EQUS.MINI"
    source_schema: str             # input quote schema, e.g. "tbbo"
    data_pin: str                  # §I data-pin format
    quote_provenance: dict         # {ts_event_utc, ts_recv_utc, reconnect_epoch, vendor_seq}

@dataclass(frozen=True)
class MissingBar:
    symbol: str
    instrument_id: int
    interval: str
    bucket_end_utc: str
    reason: str                    # ∈ {"no_quotes_in_bucket","invalid_quotes_only","future_receipt","out_of_series"}

def resample_midbars(quote_rows: Iterable[dict], *, symbol: str, instrument_id: int,
                     interval: str = "1m", dataset: str, schema: str, data_pin: str
                     ) -> Tuple[List[MidBar], List[MissingBar]]:
    """rev2 (BUILD-F1): returns (bars, missing) — `missing` holds one
    MissingBar(reason="invalid_quotes_only") per bucket that had quotes but no VALID quote.
    The missing-bucket evidence must survive to the reader; it cannot be reconstructed from bars."""

class MidBarSeriesReader:
    def __init__(self, bars: Iterable[MidBar],
                 missing: Iterable[MissingBar] = ()) -> None: ...
    def get(self, symbol: str, instrument_id: int, bucket_end_utc: str,
            *, as_of_utc: Optional[str] = None) -> Union[MidBar, MissingBar]: ...
    def latest_eligible(self, symbol: str, instrument_id: int,
                        *, as_of_utc: str) -> Optional[MidBar]: ...
    def eligible_history(self, symbol: str, instrument_id: int,
                         *, as_of_utc: str, max_bars: int) -> Tuple[MidBar, ...]: ...
```

Frozen semantics:

- **Bucketing** mirrors `bar_cache.py` ET-boundary flooring exactly (ET wall-clock floor per interval, stored
  as UTC `[bucket_start, bucket_end)`); reuse the same flooring helper logic — **do not** import bar_cache
  internals if private; reimplement the documented [start,end) rule and pin it with shared-fixture tests.
  All minted bucket strings use the §0 canonical form; all comparisons are parse-then-compare (rev2: SAFETY-F2).
- **Input filtering (rev2: REPO-F10):** the M1 `events` stream mixes symbols and schemas. Rows whose
  `(schema, symbol, instrument_id)` do not match the kwargs are **deterministically skipped** (filtered),
  never an error. A matching row missing any of the four px/sz fields, or with an unparseable Decimal,
  raises `ValueError` (M1 persists all four as non-null — `event.py` `_build_quote`; such a row is a
  programming error, not market data — rev2: BUILD-F11).
- A quote row **contributes** to bucket B iff `bucket_start_utc <= ts_event_utc < bucket_end_utc` (event time
  buckets, parsed-instant comparison; receipt time only gates *eligibility* via `watermark_utc`).
- **Validity for the label series:** all four px/sz present (M1-guaranteed), prices finite, `> 0`, not
  crossed (`bid<=ask`). **Locked quotes are valid labels** (mid is well-defined) even though §A rejects them
  for *forecasting* — frozen asymmetry, documented here. (`quote_one_sided` — a `None` side — is reachable
  **only** via the live QuoteView/`QuoteSnapshot` path, never from persisted rows — rev2: REPO-F4.)
- The bar takes the **last valid quote** by (`ts_event_utc` parsed instant, then `vendor_seq`, then input
  order) within the bucket; `watermark_utc` = that quote's `ts_recv_utc` (verbatim string from the row; all
  consumers compare it as a parsed instant). Buckets with quotes but no *valid* quote yield no bar — they
  yield a `MissingBar(reason="invalid_quotes_only")` in the resampler's `missing` list. Truly empty buckets
  yield nothing at all (never fabricated, mirrors `bar_cache.py:224-252`).
- **`get()` resolution for an absent record (rev2: BUILD-F1):** per `(symbol, instrument_id)`, the series
  coverage is `[min, max]` over `bucket_end_utc` of ALL records (bars ∪ missing). A requested
  `bucket_end_utc` with no record: inside coverage ⇒ `MissingBar(reason="no_quotes_in_bucket")`; outside
  coverage (or an empty/unknown series key) ⇒ `MissingBar(reason="out_of_series")`. A bucket present in
  `missing` ⇒ `MissingBar(reason="invalid_quotes_only")`.
- `get(..., as_of_utc=T)` returns `MissingBar(reason="future_receipt")` if the bar exists but
  `watermark_utc > T` or `bucket_end_utc > T` (parsed-instant compares). **The resolver also passes an
  explicit `as_of_utc=now_utc` — it never skips the eligibility check** (rev2: SAFETY-F1; §G); `as_of_utc=None`
  remains only as "no eligibility filter" for offline report tooling and is used by no M3 production path.
- Out-of-order/duplicate `(symbol, instrument_id, bucket_end_utc)` records in the constructor ⇒ `ValueError`.
  Ordering is enforced **per `(symbol, instrument_id)`** (strictly increasing parsed `bucket_end_utc`);
  interleaving across keys is permitted (rev2: BUILD-F19). The resampler output is sorted and unique by
  construction.

## C. `scripts/agent/feature_engine.py` — rolling state outside `scan()`, anti-lookahead

```python
# scripts/agent/feature_engine.py
"""FeatureEngine: owns rolling per-(symbol,instrument_id) mid-bar history and computes the frozen
feature vector as-of an explicit UTC instant. scan() never touches this class — it reads the
immutable FeatureSnapshot the background refresh produced (design §3.2)."""

FEATURE_QUANTUM = Decimal("0.00000001")
FEATURE_NAMES = (                  # frozen order — also the model input order (§F)
    "z_ret_21", "momentum_9", "momentum_21", "rsi14_centered",
    "ema_gap_9_21", "sma_gap_21_50", "realized_vol_21",
)

@dataclass(frozen=True)
class FeatureSnapshot:
    symbol: str
    instrument_id: int
    interval: str
    feature_cutoff_bar_end_utc: Optional[str]  # latest eligible bar end; None iff n_bars == 0 (rev2: BUILD-F16)
    watermark_utc: Optional[str]               # that bar's watermark; None iff n_bars == 0
    features: Dict[str, str]           # name -> Decimal-string, quantized FEATURE_QUANTUM
    available: bool                    # False iff history < 51 bars (FD-14)
    n_bars: int
    data_pin: str
    rules_hash: str
    feature_snapshot_id: str           # row_hash(canonical §I input); deterministic
    refreshed_at_ms: int               # injected monotonic stamp (staleness gate input)

class FeatureEngine:
    # "SignalConfig" lives in its own dependency-free module scripts/agent/signal_config.py
    # (rev2: REPO-F7 — breaks the feature_engine <-> signal_snapshot import cycle).
    def __init__(self, *, reader: MidBarSeriesReader, config: "SignalConfig", clock) -> None: ...
    def compute(self, *, symbol: str, instrument_id: int, as_of_utc: str) -> FeatureSnapshot: ...

class FeatureView:                     # the probe-facing cached view (background refresh owner)
    def __init__(self, *, engine: FeatureEngine, clock) -> None: ...
    def refresh(self, *, symbol: str, instrument_id: int, as_of_utc: str) -> FeatureSnapshot: ...
    def latest(self, symbol: str, instrument_id: int) -> Optional[FeatureSnapshot]: ...
```

Frozen feature math (float internals, sanitized at the boundary):

- Inputs: the last ≤ 51 **eligible** mid bars at `as_of_utc` (`eligible_history`, FD-2 predicate), closes
  `c_i := mid_i` (FD-3). `n_bars < 51` ⇒ `available=False`, `features={}` (S2: nothing half-computed leaks).
  `n_bars == 0` ⇒ `feature_cutoff_bar_end_utc=None`, `watermark_utc=None` (serialized `null`; the values
  still enter the `feature_snapshot_id` hash as JSON null — rev2: BUILD-F16).
- `r_t = ln(c_t / c_{t-1})` (`math.log`).
- `SMA_w` = arithmetic mean of last `w` closes. `EMA_w`: seeded with `SMA_w` over the **oldest** `w` closes in
  the 51-bar window, then recursive with `alpha = 2/(w+1)` over the remainder (deterministic given the window).
- `momentum_w = c_t/c_{t-w} − 1`.
- `rsi14` = Wilder RSI(14) over the window (seed = simple means of first 14 gains/losses, then Wilder
  smoothing). Guards apply to the **final Wilder-smoothed averages** (rev2: MATH-Q3):
  `avg_gain==0 AND avg_loss==0 ⇒ rsi14=50` (constant series; `rsi14_centered=0`); else `avg_loss==0 ⇒ 100`;
  else `avg_gain==0 ⇒ 0`. `rsi14_centered = (rsi14 − 50)/50`.
- `z_ret_21`: population z-score (ddof=0) of `r_t` against the window of the **21 most recent returns
  `r_{t−20}..r_t` INCLUSIVE of `r_t`** (rev2: MATH-Q2); `std==0 ⇒ 0` (FD-14).
- `realized_vol_21`: population std (ddof=0) over the **same inclusive 21-return window** (constant series ⇒ `0`).
- `ema_gap_9_21 = (EMA_9 − EMA_21)/c_t`; `sma_gap_21_50 = (SMA_21 − SMA_50)/c_t`.
- Boundary: every value passes `math.isfinite` then `Decimal(repr(v)).quantize(FEATURE_QUANTUM,
  ROUND_HALF_EVEN)` → string. A non-finite value here raises `NonFiniteFeature(ValueError)` — guards above
  make it unreachable; the injection test (§M) asserts the raise.
- **Lookback does not cross sessions silently:** `eligible_history` is whatever the series holds; M3 fixtures
  keep one session. Multi-session feature semantics are deferred (§N) — the engine takes bars as given.

## D. `scripts/agent/signal_snapshot.py` — atomic provenance bundle + fail-closed assembly

```python
# scripts/agent/signal_snapshot.py
"""SignalSnapshot: frozen bundle of everything the probe knew at decision time (design §4).
assemble() is a pure function; it returns either a snapshot or the frozen gate-failure verdict.
Gate ORDER is frozen in §1 — funnel attribution depends on it."""

@dataclass(frozen=True)
class GateFail:
    stage: str                       # ∈ {"identity","features","quote","market_state","horizon"}
    reasons: Tuple[str, ...]         # sorted, frozen vocabulary (§1)
    horizon: Optional[str]           # set only for stage=="horizon"

@dataclass(frozen=True)
class SignalSnapshot:
    symbol: str; instrument_id: int
    decision_ts_utc: str; decision_seen_at_ms: int
    rules_hash: str
    feature: FeatureSnapshot                     # carries cutoff/watermark/id/pin
    quote: QuoteSnapshot; quote_verdict: QuoteVerdict
    market_state: "Verdict"                      # M2 verdict, embedded as-is
    calendar_pin: str; session_date_et: str
    event_start_bar_end_utc: str                 # == feature.feature_cutoff_bar_end_utc (frozen identity)
    horizons: Tuple[str, ...]                    # e.g. ("5m","30m")
    threshold_k: Decimal

def assemble(*, symbol, instrument_id, decision_ts_utc, decision_seen_at_ms,
             event_start_bar_end_utc: str,                       # the TICK's bar (rev2: SAFETY-F5)
             feature: Optional[FeatureSnapshot], quote: Optional[QuoteSnapshot],
             market_state: "Verdict", calendar_pin: str,
             config: "SignalConfig", now_ms: int) -> Union[SignalSnapshot, GateFail]: ...

def horizon_gate(snapshot: SignalSnapshot, horizon: str,
                 schedule: Optional["SessionSchedule"]) -> Union[str, GateFail]:
    """Returns resolve_bar_end_utc, or GateFail(stage='horizon', horizon=horizon).
    schedule is None (UnknownSessionDate at fetch) => GateFail reasons=('calendar_unknown',)
    (rev2: BUILD-F2 — the horizon gate owns the schedule; assemble does not take it)."""
```

Frozen semantics:

- **identity:** `feature`, `quote`, `market_state` must all carry the requested `(symbol, instrument_id)`
  (the M2 `Verdict` has both; `safe_default_verdict` echoes the request, so identity passes and the
  market-state stage catches it). `feature is None` or `quote is None` ⇒ `features_unavailable` /
  `quote_missing` at their stages.
- **features (collect-ALL within the stage, sorted — rev2: SAFETY-F7):** `available` is True (else
  `features_unavailable`); cutoff identity `feature.feature_cutoff_bar_end_utc ==
  event_start_bar_end_utc` (else `feature_cutoff_mismatch` — a lagging FeatureView can NEVER re-key a
  decision to an old bar, so duplicate decision/forecast ids are impossible by construction — rev2:
  SAFETY-F5); freshness `now_ms − feature.refreshed_at_ms <= feature_staleness_ms_max` (strict `>` ⇒
  `feature_stale`); bar lag `decision_ts_utc − feature_cutoff_bar_end_utc <= bar_lag_max_intervals ×
  interval` (parsed-instant arithmetic; reason `bar_lag_exceeded`).
- **quote:** §A verdict must be `ok`; its reasons map through unchanged.
- **market_state (collect-ALL within the stage, sorted):** FD-13. `"cache_stale_safe_default" in
  verdict.reasons` ⇒ `market_state_stale_default`; `session_state != RTH` ⇒ `market_state_not_rth`;
  `tradability != TRADABLE` ⇒ `market_state_not_tradable`. The safe-default verdict fires all three ⇒
  exact tuple `("market_state_not_rth","market_state_not_tradable","market_state_stale_default")`.
- **horizon** (per horizon, after snapshot assembly): `resolve_bar_end_utc = event_start_bar_end_utc + H`
  (exact minute arithmetic on the **parsed UTC instant**, re-minted in the §0 canonical form; H ∈ config
  horizons). `schedule is None` ⇒ `calendar_unknown` (attributed at this stage, per horizon — rev2:
  BUILD-F2/SAFETY-F3); else requires `resolve_bar_end_utc <= schedule.rth_close_utc` (parsed-instant
  compare; else `session_horizon_crosses_close`). Half-days come free via the M2 schedule.
- `event_start_bar_end_utc` **is** `feature.feature_cutoff_bar_end_utc` (frozen identity, design §3.1 "usually
  the same" tightened to "always" for M3) — now ENFORCED by the features-stage cutoff check, and the
  snapshot field is set from the tick parameter.
- Field sourcing (rev3 minor-5): `snapshot.session_date_et := market_state.session_date_et` (the verdict
  echoes the request, so it is the unique available source); `resolve_bar_key` is **null on ALL
  `do_nothing` rows**, including horizon-stage ones (GateFail carries no resolve_bar_end).

## E. `scripts/agent/strategy.py` + `scripts/agent/candidate.py` — pure types (NEW, not used by the probe)

```python
# scripts/agent/strategy.py
@dataclass(frozen=True)
class ScanContext:
    snapshot: SignalSnapshot
    rules_hash: str
    now_ms: int

@runtime_checkable
class Strategy(Protocol):
    strategy_id: str
    def scan(self, ctx: ScanContext) -> Sequence["Candidate"]: ...
```

```python
# scripts/agent/candidate.py
@dataclass(frozen=True)
class Leg:
    symbol: str; instrument_id: int
    side: str                        # ∈ {"buy","sell"}
    qty: Decimal                     # finite, > 0 (post-init validated)
    limit_price: Optional[Decimal]   # finite if present

@dataclass(frozen=True)
class Candidate:
    strategy_id: str
    legs: Tuple[Leg, ...]            # len >= 1; M3 single-leg N=1 is a convention, not a cap
    paper_eligible: bool
    score: Optional[Decimal]
```

Frozen: `__post_init__` validation mirrors `OrderIntent` (`broker/base.py:28-34`) — qty positive finite
Decimal, limit finite or None, side in closed vocabulary, legs non-empty. **No M3 module instantiates
`Candidate`** (S1 test asserts the probe's emitted types). These types carry no order authority — orders
remain preflight-token-gated (M5).

## F. `scripts/agent/forecast.py` — stable logistic, config-pinned coefficients

```python
# scripts/agent/forecast.py
PROB_QUANTUM = Decimal("0.000001")
P_MIN, P_MAX = Decimal("0.000001"), Decimal("0.999999")   # FD-5 clamp
Z_CLAMP = 30.0

@dataclass(frozen=True)
class ForecastEvent:
    horizon: str                     # "5m" | "30m"
    threshold_k: Decimal             # default 0
    event_start_bar_end_utc: str
    resolve_bar_end_utc: str

@dataclass(frozen=True)
class Forecast:
    event: ForecastEvent
    p: Decimal                       # quantized PROB_QUANTUM, clamped to [P_MIN, P_MAX] (closed)
    model_version: str
    model_artifact_hash: str

class NonFiniteFeature(ValueError): ...

def predict(features: Dict[str, str], *, coefficients: Dict[str, str],
            model_version: str, model_artifact_hash: str,
            event: ForecastEvent) -> Forecast: ...
```

Frozen semantics:

- `coefficients` keys = `("intercept",) + FEATURE_NAMES` exactly (missing/extra key ⇒ `ValueError`).
- `z = b0 + Σ b_i·x_i` in float; clamp `z` to `[−30, +30]`; branch-stable sigmoid (`z>=0`:
  `1/(1+exp(−z))`, else `exp(z)/(1+exp(z))`). Float→Decimal boundary (rev2: MATH-Q7, mirrors §C verbatim):
  `p_float` passes `math.isfinite`, then `Decimal(repr(p_float)).quantize(PROB_QUANTUM, ROUND_HALF_EVEN)`,
  then clamp to `[P_MIN, P_MAX]` — `0 < p < 1` always. Quantize-only (no round-trip check; p is float-born —
  rev2: MATH-Q6).
- Inputs re-validated: every `FEATURE_NAMES` value parses to a finite float, else `NonFiniteFeature`.
- v1 coefficients (committed in `agent_rules.signal.model.coefficients`, per horizon, identical values —
  conservative momentum priors, explicitly untrained):

```json
{"intercept": "0", "z_ret_21": "0.05", "momentum_9": "1.0", "momentum_21": "0.5",
 "rsi14_centered": "0.20", "ema_gap_9_21": "5.0", "sma_gap_21_50": "2.5", "realized_vol_21": "0"}
```

## G. `scripts/agent/calibration.py` + `calibration_report.py` — resolve, score, report

```python
# scripts/agent/calibration.py
BRIER_QUANTUM = Decimal("0.000000000001")
REPORT_QUANTUM = Decimal("0.00000001")          # rev2: BUILD-F15 — declared HERE, used for all means/divisions

UNRESOLVED_REASONS = frozenset({                 # rev2: BUILD-F12/SAFETY-F11 — closed set, ledger-validated
    "no_mid_bar_t0", "no_mid_bar_resolve", "label_source_mismatch",
})

# Sample shape crossing the calibration<->report boundary (rev2: BUILD-F15):
#   Sample = Tuple[Decimal p, int outcome]                      — brier / murphy / reliability_bins
#   RefSample = Tuple[Decimal p, int outcome, Decimal p_ref]    — BSS reference scoring

class AsOfClimatology:
    """D5/FD-6. Ingest order IS resolution order; rate() sees only previously-ingested outcomes.
    Ingestion is IDEMPOTENT per forecast_id (harden round 1, M3-01 BLOCKER): the resolver both
    re-seeds from the replayed stream on every resolve_due call AND ingests live after each new
    score — id-level dedupe HERE is the only shape that keeps a shared climatology correct for
    every resolver lifecycle. A repeated forecast_id is a silent no-op."""
    def __init__(self, *, min_samples: int) -> None: ...
    def ingest_resolved(self, *, symbol: str, horizon: str, outcome: int,
                        forecast_id: str) -> None: ...
    def rate(self, *, symbol: str, horizon: str) -> Tuple[Decimal, str, int]:
        """(p_ref quantized PROB_QUANTUM — quantize-only, division is float-free Decimal but generically
        off-grid (rev2: MATH-Q6), forecaster_id, n)."""

class ScoredLedger:
    """Validating writer for journal/forecast_scored.jsonl (StatusLedger pattern, status_ledger.py:217).
    Raises on: reason not in UNRESOLVED_REASONS, outcome not in (0, 1), missing_bar_reason not in
    the MissingBar vocabulary (when non-None), missing_bar_reason == "future_receipt" (a deferral
    must NEVER persist — structural SAFETY-F1 enforcement, rev3 minor-4),
    reserved-key collision (journal.py:21)."""
    EVT_SCORED = "forecast_scored"
    EVT_UNRESOLVED = "forecast_unresolved"
    def __init__(self, writer: EventWriter, *, rules_hash: str) -> None: ...
    def record_scored(self, *, decision_id, forecast_id, outcome: int, brier_i: Decimal,
                      mid_t0: Decimal, mid_th: Decimal, event_start_bar_key, resolve_bar_key,
                      resolved_as_of_utc: str, label_provenance: dict, data_pin: str) -> dict: ...
    def record_unresolved(self, *, decision_id, forecast_id, reason: str,
                          missing_bar_reason: Optional[str],
                          event_start_bar_key, resolve_bar_key,
                          resolved_as_of_utc: str, data_pin: str) -> dict: ...

@dataclass(frozen=True)
class ResolveStats:                              # rev2: BUILD-F7 — frozen shape, §M.7 asserts these
    considered: int                              # decision rows seen with action=="forecast_only"
    due: int                                     # resolve_bar_end_utc <= now_utc (parsed compare)
    scored: int
    unresolved: int                              # terminal unresolved rows written this call
    skipped_already_resolved: int                # forecast_id already in the scored stream (FD-8)
    deferred_not_eligible: int                   # rev2: SAFETY-F1 — bar future_receipt/not-yet-eligible: NO row, retry later

class ForecastResolver:
    def __init__(self, *, reader: MidBarSeriesReader, ledger: ScoredLedger,
                 scored_stream_path, climatology: Optional[AsOfClimatology] = None) -> None: ...
    def resolve_due(self, decision_rows: Iterable[dict], *, now_utc: str) -> ResolveStats: ...

def brier(samples) -> Decimal: ...
def murphy_decomposition(samples, *, bins: int = 10) -> dict: ...
def reliability_bins(samples, *, bins: int = 10) -> list: ...
def brier_skill_score(bs_model: Decimal, bs_ref: Decimal) -> Union[Decimal, str]: ...
```

Frozen semantics:

- **Idempotency (S4/FD-8):** `resolve_due` first `journal.replay(scored_stream_path)` → seen
  `forecast_id` set (both event types); skips seen ids. One row per `forecast_id`, ever. Rerun ⇒ no new rows.
- **Climatology resume-seeding (rev2: SAFETY-F14):** when a climatology is wired, `resolve_due` first
  ingests the outcomes of all replayed `forecast_scored` rows (that event type only) in stream `seq` order,
  BEFORE processing new due rows — resume-state is identical to uninterrupted-run state (§M.7 asserts).
  Seeding obtains `(symbol, horizon)` by joining the replayed scored row's `forecast_id` back to the
  `decision_rows` input (total within a run dir — `resolve_due` receives the replayed decision stream;
  rev3 minor-3); an unjoinable scored row raises (corrupt stream pair, fail-loud).
- A decision row is **due** iff `action=="forecast_only"` and `resolve_bar_end_utc <= now_utc`
  (parsed-instant compare).
- **Resolution reads bars AS-OF resolve time (rev2: SAFETY-F1 — the S3 wall for labels AND the
  climatology):** `reader.get(symbol, instrument_id, <bucket_end parsed from the frozen bar-key
  format>, as_of_utc=now_utc)` for both `event_start_bar_key` and `resolve_bar_key` (rev3 minor-2 —
  decision rows carry KEYS; the resolver splits `symbol|interval|bucket_end_utc`).
  Outcome routing by MissingBar reason:
  - `future_receipt` (either bar) ⇒ **deferred**: NO row is written, the forecast stays pending and is
    retried on a later `resolve_due` call (`deferred_not_eligible` counts it). Feed latency can therefore
    never permanently kill a resolvable forecast, and a future-received label can never leak into a score
    or into the as-of climatology.
  - `no_quotes_in_bucket` / `invalid_quotes_only` / `out_of_series` ⇒ **terminal**
    `record_unresolved(reason="no_mid_bar_t0"|"no_mid_bar_resolve", missing_bar_reason=<MissingBar reason>)`.
  - both bars present but mismatched `(source_dataset, source_schema, data_pin)` ⇒ terminal
    `label_source_mismatch` (`missing_bar_reason=None`). **Outcomes are never guessed.**
  - every scored/unresolved row records `resolved_as_of_utc = now_utc` (S3 provenance).
- `outcome = 1 if mid_tH >= mid_t0 × (1+k) else 0` — exact Decimal compare; `k` from the decision row.
- `brier_i = (p − outcome)²` exact Decimal, quantized `BRIER_QUANTUM`.
- On a successful score, `climatology.ingest_resolved(...)` (when wired) — resolution order defines as-of.
- **Scoring math (rev2: MATH-Q1/Q4/Q5 — every formula frozen):** with `samples = [(p_i, o_i)]`, `N = len`,
  bins per FD-11, `n_k`/`p̄_k`/`ō_k` the count and (unquantized exact-Decimal) means of bin k, `ō` the
  overall base rate:
  - `brier = (Σ brier_i)/N` — Decimal sum/division, quantized `REPORT_QUANTUM` ROUND_HALF_EVEN.
  - `REL = (1/N)·Σ_k n_k·(p̄_k − ō_k)²`, `RES = (1/N)·Σ_k n_k·(ō_k − ō)²`, `UNC = ō·(1−ō)` — all computed
    in exact Decimal from UNQUANTIZED bin means; each OUTPUT quantized `REPORT_QUANTUM` at the boundary.
    `murphy_decomposition` returns exactly the keys
    `{brier, reliability, resolution, uncertainty, base_rate, n}` (rev3 minor-6).
  - The 3-term identity `BS = REL − RES + UNC` is exact ONLY when every occupied bin's forecasts are
    constant (the within-bin variance/covariance residual is otherwise O(1e-3), NOT rounding — rev2:
    MATH-Q1). The §M.7 fixture is FROZEN to at most one distinct `p` per occupied bin, and the test asserts
    the identity within `Decimal("0.000001")` (boundary-rounding slack only). The docstring must state the
    identity does NOT hold for heterogeneous bins.
  - `brier_skill_score(bs_model, bs_ref) = 1 − bs_model/bs_ref` — Decimal division, quantized
    `REPORT_QUANTUM` ROUND_HALF_EVEN; returns the string `"unavailable:zero_reference_brier"` when
    `bs_ref == 0` (never NaN/Inf/exception).
  - Reference scoring (BSS inputs): per-sample reference brier `(p_ref − o)²` exact at `BRIER_QUANTUM`;
    `BS_ref` = mean quantized `REPORT_QUANTUM`. `bss_vs_climatology_asof` includes **ALL** scored samples,
    each against its own carried decision-time `reference_base_rate_asof_t0` — including rows whose
    reference degraded to `constant_0.5` (rev2: MATH-Q5).

```python
# scripts/agent/calibration_report.py
def build_report(*, decision_rows, scored_rows, run_id: str, rules_hash: str,
                 generated_ts_utc: str, bins: int = 10) -> dict: ...
def render_markdown(report: dict) -> str: ...
def write_report(report: dict, *, out_dir) -> "Path": ...     # reports/calibration/<run_id>.json (+ .md)
```

Frozen report shape (canonical `dumps()`; all numerics Decimal-strings quantized `REPORT_QUANTUM`):

```
{run_id, rules_hash, model_version, generated_ts_utc,
 funnel: {ticks, ticks_reaching_horizon,
          do_nothing_identity, do_nothing_features, do_nothing_quote,
          do_nothing_market_state, do_nothing_horizon, forecasts, unresolved, scored},
 dedupe: {decision_rows, unique_forecast_ids, duplicate_scored_dropped},
 aggregate: {n, brier, reliability, resolution, uncertainty, base_rate,
             bss_vs_climatology_asof, bss_vs_constant_half,
             brier_ref_climatology_asof, brier_ref_constant_half,
             full_run_base_rate},                       # report-only climatology (D5)
 bins: [{lo, hi, count, mean_forecast_p|null, observed_freq|null, thin}],
 per_cell: [{symbol, horizon, n, brier, base_rate, bss_vs_constant_half}],
 unresolved: {count, by_reason: {...}}}
```

- **Funnel arithmetic (rev2: BUILD-F9/SAFETY-F8 — frozen identities, §M.8 asserts BOTH):**
  `ticks` := count of **distinct `(symbol, instrument_id, event_start_bar_key)`** over replayed decision
  rows; `ticks_reaching_horizon` := ticks that produced NO pre-horizon do_nothing row. Identity 1:
  `do_nothing_identity + do_nothing_features + do_nothing_quote + do_nothing_market_state +
  ticks_reaching_horizon == ticks` (pre-horizon buckets count one row per tick). Identity 2:
  `do_nothing_horizon + forecasts == ticks_reaching_horizon × len(horizons)` (per-horizon rows, FD-9).
- Funnel counts come from replayed journal rows only (stage attribution via `GateFail.stage` recorded on
  do_nothing rows); calibration is never interpreted without the funnel (design §5).
- **Report-level formulas (rev2: MATH-Q4):** `mean_forecast_p` (per bin) = Decimal sum of `p` / count;
  `observed_freq` (per bin) = count(outcome==1)/count; `base_rate` = count(outcome==1)/n over scored
  samples; `full_run_base_rate` = same over ALL scored rows in the run (the report-only full-run
  climatology) — every division quantized `REPORT_QUANTUM` ROUND_HALF_EVEN.
- `BS_ref` for `bss_vs_climatology_asof` uses each sample's **decision-time**
  `reference_base_rate_asof_t0` (proper as-of, FD-6); constant-0.5 is the floor reference.
- Report dedupes scored rows by `forecast_id` deterministically (first by stream `seq`), counts drops.
- **Deterministic ordering (rev2: SAFETY-F13):** `per_cell` sorted by `(symbol, horizon)`; `bins` by bin
  index; `unresolved.by_reason` keys sort canonically via `dumps`.
- Rendering is stdlib-only: JSON + Markdown table; reliability diagram as a Markdown table + optional
  ASCII/SVG written by stdlib string formatting (no plotting dependency, design §9).

## H. `scripts/agent/strategies/calibration_probe.py` — the only stateful-per-tick unit

```python
# scripts/agent/strategies/calibration_probe.py
"""Observe-only calibration probe (design §2 data flow). Emits decision rows ONLY.
S1: this module never imports broker/execution_preflight/kill_switch/arming and never
constructs Candidate/OrderIntent. paper_eligible is hard-pinned False at the ledger."""

STRATEGY_ID = "calibration_probe_v1"
ACTIONS = frozenset({"do_nothing", "forecast_only"})     # would_open FORBIDDEN until M5/M7

class DecisionLedger:
    """Validating writer for journal/decisions.jsonl. Raises on: action not in ACTIONS,
    paper_eligible is not (identity) False, reason outside the frozen vocabulary,
    reserved-key collision (journal.py:21)."""
    EVT_DECISION = "decision"
    def __init__(self, writer: EventWriter, *, rules_hash: str) -> None: ...
    def record_decision(self, *, decision_id: str, fields: dict) -> dict: ...

@dataclass(frozen=True)
class ForecastDecision:
    action: str                       # ∈ ACTIONS
    symbol: str; instrument_id: int
    horizon: Optional[str]            # None on pre-horizon do_nothing (FD-9)
    reasons: Tuple[str, ...]
    forecast: Optional[Forecast]
    decision_id: str
    forecast_id: Optional[str]
    row: dict                         # the journaled row (as returned by the ledger)

class CalibrationProbe:
    # rev2 (BUILD-F2): the probe needs SessionSchedule + calendar_pin. FROZEN choice: an
    # ADDITIVE M2 edit — MarketCalendar gains two pure passthroughs delegating to its provider:
    #   def schedule_for(self, session_date_et: str) -> SessionSchedule   (propagates UnknownSessionDate)
    #   def calendar_pin(self) -> str
    # No M2 behavior changes; M2 tests untouched; the facade stays the probe's single calendar collaborator.
    def __init__(self, *, config: "SignalConfig", calendar: "MarketCalendar",
                 market_state_cache: "MarketStateCache", feature_view: FeatureView,
                 quote_view: "QuoteView", ledger: DecisionLedger,
                 climatology: AsOfClimatology, run_id: str, clock) -> None: ...
    def on_bar_complete(self, *, symbol: str, instrument_id: int,
                        event_start_bar_end_utc: str, decision_ts_utc: str
                        ) -> Tuple[ForecastDecision, ...]: ...

@runtime_checkable
class QuoteView(Protocol):
    def latest(self, symbol: str, instrument_id: int) -> Optional[QuoteSnapshot]: ...
```

Frozen tick algorithm (`on_bar_complete`):

1. `decision_seen_at_ms = clock.now_ms()`; `session_date_et = calendar.session_date_for(decision_ts_utc)`
   (pure date conversion — raises only `ValueError` on a malformed/naive timestamp, a PROGRAMMING error
   that propagates; it never raises `CalendarError` — rev2: REPO-F1). Then
   `schedule_or_none = calendar.schedule_for(session_date_et)` catching `UnknownSessionDate ⇒ None`
   (NO row is written here; `calendar_unknown` is attributed per-horizon at gate 5 — rev2: BUILD-F2).
   Pull `feature_view.latest()`, `quote_view.latest()`,
   `market_state_cache.get(symbol, instrument_id, session_date_et, now_ms=...)` —
   the verdict is ALWAYS fetched, so `market_state_provenance` is non-null on every row.
2. `assemble(..., event_start_bar_end_utc=event_start_bar_end_utc, ...)` (§D). `GateFail` ⇒ **one**
   `do_nothing` row (`horizon=null`, `gate_stage` field, reasons), STOP (FD-9; gates 1–4 only).
3. Per horizon in config order: `horizon_gate(snapshot, horizon, schedule_or_none)`. Fail ⇒ per-horizon
   `do_nothing` row (sibling horizons UNAFFECTED — rev2: SAFETY-F4). Pass ⇒
   `climatology.rate(...)` → `predict(...)` → `forecast_only` row with
   `edge_label = p − reference_base_rate_asof_t0` — the exact Decimal difference of two
   PROB_QUANTUM-quantized operands (exponent 1e-6 by construction, may be negative; NO further
   quantization — rev2: MATH-Q9).
4. Returns the tuple of `ForecastDecision`s; every emitted row already journaled by the ledger.

The probe holds **no** rolling numeric state (that lives in `FeatureEngine`); its only state is injected
collaborators + `run_id`. It performs no I/O beyond the ledger.

## I. Journal streams, deterministic IDs, data-pin format

**Streams** (file names under the run's journal dir, mirroring `status.jsonl`):

- `journal/decisions.jsonl` — event type `decision`. Field set (frozen; `decision_id` rides the journal
  kwarg, never `fields`, per `_RESERVED` — `journal.py:21`):
  `symbol, instrument_id, strategy, action, gate_stage|null, reasons[], horizon|null, forecast_id|null,
  forecast{event_type:"up_move", h, k, p}|null, reference_base_rate_asof_t0|null, reference_forecaster_id|null,
  reference_n|null, edge_label|null, signal_provenance{feature_snapshot_id, feature_cutoff_bar_end_utc,
  feature_watermark_utc, data_pin, model_version, model_artifact_hash}|null,
  quote_provenance{ts_event_utc, ts_recv_utc, seen_at_ms, reconnect_epoch, vendor_seq, dataset, schema}|null,
  market_state_provenance{tradability, session_state, reasons[], ca_blackout, stale_safe_default,
  calendar_pin, session_date_et}, event_start_bar_key, resolve_bar_key|null, decision_ts_utc,
  decision_seen_at_ms, data_pin, rules_hash, paper_eligible:false`
- `journal/forecast_scored.jsonl` — frozen field sets (rev2: BUILD-F8; `decision_id` kwarg links back to
  the decision row, S6):
  - `forecast_scored`: `forecast_id, outcome (0|1), brier_i, mid_t0, mid_th, event_start_bar_key,
    resolve_bar_key, resolved_as_of_utc, label_provenance, data_pin, rules_hash`
  - `forecast_unresolved`: `forecast_id, reason (∈ UNRESOLVED_REASONS), missing_bar_reason|null
    (∈ MissingBar vocabulary when non-null), event_start_bar_key, resolve_bar_key, resolved_as_of_utc,
    data_pin, rules_hash`
  - `label_provenance` frozen key set: `{t0: {watermark_utc, ts_event_utc, ts_recv_utc, reconnect_epoch,
    vendor_seq}, th: {…same…}, source_dataset, source_schema}` (the §B "scored row records the watermark"
    promise, both ends).
- The decision-row `forecast` subdict maps from the §F types as (rev2: BUILD-F18):
  `event_type = "up_move"` (M3 constant), `h = event.horizon`, `k = threshold_k` (Decimal),
  `p = forecast.p` (Decimal; serializer renders both as strings).
- M3 emits **no** separate `data_quality_alerts` rows (design §7 left it optional): unresolved rows carry the
  reason — one home, no duplication. (Frozen choice.)

**Bar key format (frozen):** `bar_key = f"{symbol}|{interval}|{bucket_end_utc}"` with `bucket_end_utc` in
the §0 canonical surface form (byte-stable keys — rev2: SAFETY-F2).

**Data-pin format (frozen):** `data_pin = f"{dataset}:{schema}:{interval}:{source_id}"` where `source_id` is
the fixture id (offline) or recorded-range id; e.g. `"EQUS.MINI:tbbo:1m:fixture:signal-aapl-v1"`.

**Deterministic IDs (S6)** — all via `serializer.row_hash` over a canonical dict with the exact key set:

- `decision_id = "d-" + row_hash({run_id, symbol, instrument_id, strategy, event_start_bar_key,
  horizon|null})` — per-horizon rows get distinct ids; the pre-horizon do_nothing row uses `horizon=null`.
- `forecast_id = "f-" + row_hash({run_id, decision_id, symbol, instrument_id, strategy, rules_hash,
  data_pin, model_version, feature_snapshot_id, event_start_bar_key, resolve_bar_key, h, k, p,
  reference_forecaster_id})` — the design-§7 field list, frozen verbatim.
- `feature_snapshot_id = "fs-" + row_hash({symbol, instrument_id, interval, feature_cutoff_bar_end_utc,
  watermark_utc, features, data_pin, rules_hash})`.

Replaying the same fixtures with the same `run_id` reproduces every id and row hash byte-for-byte
(S6 test). `p` inside `forecast_id` is the quantized Decimal-string.

## J. Config additions + `rules_hash` (D6/FD-7)

Committed `config/agent_rules.json` gains (run gates untouched):

```json
"signal": {
  "interval": "1m",
  "feature_windows": ["9", "21", "50"],
  "rsi_period": "14",
  "z_window": "21",
  "vol_window": "21",
  "horizons": ["5m", "30m"],
  "threshold_k": "0",
  "spread_bps_max": "50",
  "quote_staleness_ms_max": "2000",
  "feature_staleness_ms_max": "5000",
  "bar_lag_max_intervals": "2",
  "refresh_cadence_ms": "1000",
  "prob_bins": "10",
  "min_reference_samples": "30",
  "model": {
    "model_version": "logit-mom-v1",
    "standardization": "identity",
    "coefficients": {
      "5m":  {"intercept": "0", "z_ret_21": "0.05", "momentum_9": "1.0", "momentum_21": "0.5",
              "rsi14_centered": "0.20", "ema_gap_9_21": "5.0", "sma_gap_21_50": "2.5",
              "realized_vol_21": "0"},
      "30m": {"intercept": "0", "z_ret_21": "0.05", "momentum_9": "1.0", "momentum_21": "0.5",
              "rsi14_centered": "0.20", "ema_gap_9_21": "5.0", "sma_gap_21_50": "2.5",
              "realized_vol_21": "0"}
    }
  }
}
```

- **Every leaf is a string / list-of-strings** ⇒ `tighten_only_merge` keeps base for all of them
  (`config.py:43` else-branch); overlays cannot tighten, loosen, or reinterpret any signal parameter (FD-7,
  stricter than M2's numeric-tighten posture, per design §7).
- **Frozen (rev2: REPO-F7): a NEW dependency-free module `scripts/agent/signal_config.py` owns
  `SignalConfig.from_config(config: dict)`** (imports nothing from other M3 modules; both
  `feature_engine` and `signal_snapshot` import IT — no cycle). `FEATURE_NAMES` canonically lives in
  `signal_config.py`; `feature_engine` re-exports it so the §C surface holds (rev3 minor-1).
  `SignalConfig` also carries `rules_hash` (of the WHOLE assembled config, `config.py:17` semantics)
  so the engine/probe need no second config seam. It parses the strings once into typed
  values (`Decimal`/`int`/tuples), validating: known keys only, horizons parse as
  `<int>m`, windows positive ints, `prob_bins=="10"` in M3, coefficients keys exactly `("intercept",)+
  FEATURE_NAMES` per horizon. Unknown/missing keys ⇒ `ValueError` at startup (fail-loud, before any tick).
- `model_artifact_hash = sha256(canonical dumps(agent_rules.signal.model))` — computed by
  `SignalConfig`, carried on every forecast row; `rules_hash` (whole assembled config) is carried on every
  row as today. Changing any coefficient changes both.
- Gates canary (`test_config_canary.py`) keeps passing unchanged: `enabled`/`paper_trading.enabled`
  identity-False; M3 adds assertions, removes none.

## K. Fixtures (programmatic builders + committed goldens)

Builders live in `tests/lib/signal_fixtures.py` (pure, seeded, no wall clock, no randomness beyond an
explicit LCG seed so rows are reproducible):

| Fixture | Contents | Used by |
|---|---|---|
| `quotes_session_v1(symbol="AAPL", instrument_id=1001)` | 75 minutes of 1-per-minute valid tbbo-shaped persisted rows for session **`2026-06-15`** (a covered REGULAR day in the committed M2 calendar fixture — rev2: BUILD-F10; 2026-06-09 is NOT covered) starting 09:30 ET; prices follow a fixed deterministic path; plus: one crossed quote (10:05), one locked (10:06), one zero-bid (10:07). NO one-sided persisted row (M1 cannot persist one — rev2: REPO-F4); `quote_one_sided` is exercised in §M.1 via directly-constructed `QuoteSnapshot`s. At least two rows carry a `ts_recv_utc` WITHOUT fractional seconds (the recorder's whole-second form) to pin the §0 mixed-form discipline | bar_series, feature_engine, probe E2E |
| `future_receipt_quote()` | a quote with `ts_event_utc` inside bucket `10:14→10:15` but `ts_recv_utc=10:21:00Z` — the S3 leakage probe: features/labels as-of `10:20` must exclude the `10:15` bar; the RESOLVER at `now_utc=10:20` must DEFER (no row, no climatology ingest) and score exactly once at `10:22` (rev2: SAFETY-F1) | S3 tests (§M) |
| `gap_session_v1()` | same as quotes_session_v1 but minutes 10:30–10:39 have no quotes (halt analog) → resolver `no_mid_bar_resolve` with `missing_bar_reason="no_quotes_in_bucket"` | S4 tests |
| `half_day_calendar` | the M2 calendar fixture early-close date **2026-11-27** (RTH close 13:00 ET); horizon 30m forecast at 12:45 ET crosses 13:00 close ⇒ `session_horizon_crosses_close` | horizon gate |
| `zero_variance_session()` | constant-mid session ⇒ `z_ret=0`, `realized_vol=0`, `rsi14=50` / `rsi14_centered=0` (both-zero Wilder guard — rev2: MATH-Q3), all finite | S2 |
| `naninf_injection` | direct constructor/predict calls with NaN/Inf floats and non-finite Decimals | S2 |
| `zero_reference_brier()` | hand-built scored samples where the reference is perfect ⇒ `BS_ref=0` ⇒ BSS `"unavailable:zero_reference_brier"` | S2/report |
| `murphy_samples_v1()` | 40 hand-built `(p, outcome)` samples with **at most ONE distinct `p` per occupied bin** (rev2: MATH-Q1 — the 3-term identity is exact only then) | §M.7 Murphy identity |
| `tests/fixtures/signal/golden_report_v1.json` | committed expected report JSON for a fixed mini-run (deterministic end-to-end: quotes → bars → features → forecasts → resolve → report) | S6/report determinism |

The golden run uses `run_id="run-m3-golden-v1"`, the committed config,
`generated_ts_utc="2026-06-09T00:00:00.000000Z"` (the §0 canonical form — rev2: SAFETY-F13), and carries
the committed M2 calendar fixture's own `pin` string as `calendar_pin`; regenerating it must be
byte-identical (canonical dumps).

## L. Conventions-to-mirror table

| Convention | Source | M3 usage |
|---|---|---|
| Validating-ledger over `EventWriter` | `status_ledger.py:217-326` | `DecisionLedger`, `ScoredLedger` |
| Reserved journal keys — pass `decision_id` as kwarg | `journal.py:21,110-125` | both ledgers |
| Strict `>` staleness on injected monotonic ms | `market_state_cache.py:74-99` | quote age, feature staleness |
| Fail-closed safe default with explicit marker reason | `market_state_cache.py:101-125` | mapped to `market_state_stale_default` |
| Closed vocabularies as frozensets; out-of-vocab raises | `market_state.py:27-59` | `ACTIONS`, reasons, MissingBar reasons |
| Sorted machine-readable `reasons` tuple | `market_state.py:184-202` | GateFail / decision rows |
| ET-boundary bucketing `[start,end)`, no fabricated empties | `bar_cache.py:118-156,224-252` | `resample_midbars` |
| Decimal quantize + ROUND_HALF_EVEN (round-trip where exactness is owed) | `status_ledger.py:93-105` | **MID + BRIER round-trip enforced** (provably exact: 5dp mid ⊂ 1e-6 grid; (p−o)² of 6dp p is exactly 12dp); **PROB + FEATURE quantize-only** (float-born / off-grid division — rev2: MATH-Q6/BUILD-F6). Round-trip FOR PROB applies only where an already-quantized p is RE-ingested (ledger validation, forecast_id input) |
| `row_hash` for deterministic ids | `serializer.py:53-55` | decision/forecast/feature-snapshot ids |
| AST module-scope import guard | `test_no_network_no_creds.py:89-115` | S1 forbidden-imports test |
| Committed-config canary | `test_config_canary.py:24-64` | signal-block immutability canary |
| Injected `clock.now_ms()` everywhere | `market_state_cache.py:44,52` | probe/feature/quote staleness |

## M. Test list — each test file → cases → safety invariant

`tests/agent/` (offline, stdlib-only; extend `test_no_network_no_creds.py` rather than duplicating it):

1. **`test_quote_quality.py`** — accept path (mid/spread math exact vs hand-computed Decimal); each reject
   reason fires alone and in combination (sorted reasons); staleness strict-`>` boundary at exactly
   `staleness_ms_max`; locked vs crossed distinction; float input raises; non-finite Decimal ⇒
   `quote_nonfinite` not an exception. [S2]
2. **`test_bar_series.py`** — bucketing matches bar_cache ET-boundary rule incl. a DST date; last-valid-quote
   selection (ties broken by vendor_seq then input order); locked-quote bar valid; crossed/zero filtered;
   `invalid_quotes_only` vs `no_quotes_in_bucket` vs `out_of_series` (via the resampler `missing` seam —
   rev2: BUILD-F1); mixed-stream input filtering (other symbol/schema rows skipped — rev2: REPO-F10);
   **S3 core:** `get(as_of)` returns `future_receipt` when `watermark_utc > as_of` even though
   `bucket_end <= as_of`; **mixed ISO-form boundaries (rev2: SAFETY-F2):** a watermark `…00.500000Z` vs
   as_of `…00Z` (no fractional part) is INELIGIBLE; equal instants in different surface forms compare
   equal; duplicate/out-of-order per-(symbol,instrument_id) constructor raises while cross-key
   interleaving is permitted; `eligible_history` ordering. [S3]
3. **`test_feature_engine.py`** — hand-computed SMA/EMA/RSI/momentum/z/vol on a tiny fixed series (golden
   numbers in-test, the rev2-pinned inclusive z/vol window and both-zero RSI=50 rule); 50-bar history ⇒
   unavailable, 51 ⇒ available (FD-14 boundary); zero-bars ⇒ `available=False` with BOTH timestamp fields
   `None` and a deterministic `feature_snapshot_id` (rev2: BUILD-F16); zero-variance ⇒ z=0 and
   vol=0, rsi=50, all finite; **S3:** future-received bar excluded — feature values identical with and
   without the leaked bar present in the series; `feature_snapshot_id` deterministic; boundary
   quantization to 8dp; injection ⇒ `NonFiniteFeature`. [S2, S3]
4. **`test_signal_snapshot.py`** — gate order frozen (multi-fault input trips the EARLIEST stage only);
   identity mismatch; feature stale strict boundary; bar-lag boundary at exactly 2 intervals; **cutoff
   identity: a FeatureSnapshot whose cutoff != the tick's bar ⇒ `feature_cutoff_mismatch` (rev2:
   SAFETY-F5)**; **exact sorted reason tuples (rev2: SAFETY-F7):** safe-default verdict ⇒ exactly
   `("market_state_not_rth","market_state_not_tradable","market_state_stale_default")`; REDUCE_ONLY in RTH
   ⇒ exactly `("market_state_not_tradable",)`; PRE/POST ⇒ `market_state_not_rth` included; horizon:
   15:31+30m ⇒ crosses close; half-day 12:45+30m ⇒ crosses close; 12:25+5m on half-day ⇒ passes;
   `schedule=None` ⇒ `calendar_unknown` at stage `horizon`. [S2, fail-closed]
5. **`test_strategy_types.py`** — Candidate/Leg validation (qty<=0, non-finite, empty legs, bad side raise);
   frozen immutability; `CalibrationProbe` does **not** satisfy `Strategy` protocol (isinstance False);
   ForecastDecision is not a Candidate. [S1 typing edge]
6. **`test_forecast.py`** — golden p for hand-computed inputs; z-clamp at ±30 ⇒ p == P_MAX/P_MIN exactly
   (never 0/1); extreme coefficients ⇒ finite; missing/extra coefficient key raises; non-finite feature ⇒
   `NonFiniteFeature`; determinism (same input ⇒ same Decimal string). [S2]
7. **`test_calibration.py`** — outcome boundary `mid_tH == mid_t0·(1+k)` ⇒ 1 (>=, D2); brier_i exact;
   Murphy identity within `Decimal("0.000001")` on `murphy_samples_v1` (constant-p-per-bin fixture; the
   docstring caveat for heterogeneous bins asserted — rev2: MATH-Q1) with the frozen REL/RES/UNC formulas
   checked against hand-computed values; `BS_ref==0` ⇒ `"unavailable:zero_reference_brier"`;
   climatology as-of: rate before N-th ingest excludes it (no future leak — S3 for the reference); below
   min_samples ⇒ constant 0.5; **resolver S3 (rev2: SAFETY-F1):** future_receipt fixture at
   `now_utc=10:20` ⇒ NO row, NO climatology ingest, `deferred_not_eligible==1`; rerun at `10:22` scores
   exactly once with `resolved_as_of_utc` recorded; **resume-seeding (rev2: SAFETY-F14):** a resolver
   constructed over an existing scored stream reproduces the uninterrupted run's reference rates;
   **S4:** gap fixture ⇒ terminal unresolved with `reason`+`missing_bar_reason`; resolver rerun on same
   streams appends nothing (replay-dedupe; ResolveStats.skipped_already_resolved counts them); one score
   per forecast_id with duplicate decision rows input. [S3, S4]
8. **`test_calibration_report.py`** — the TWO frozen funnel identities hold on a mixed decision-row set
   (rev2: BUILD-F9: identity 1 over pre-horizon buckets + ticks_reaching_horizon; identity 2
   per-horizon × len(horizons)); bin edges (p=0.0 → bin 0 — only constructible directly, the clamp forbids
   it from predict; p=0.95 and p=1.0−quantum → bin 9); empty-bin nulls; thin flag; per-cell breakdown
   sorted (symbol, horizon); report dedupe by forecast_id; golden report byte-identical (canonical dumps,
   pinned generated_ts_utc); BSS vs as-of reference uses decision-time base rates incl. constant_0.5 rows. [S6]
9. **`test_calibration_probe.py`** — E2E on fixtures: valid tick ⇒ 2 forecast rows (5m, 30m) with
   paper_eligible False, deterministic ids, edge_label = p − ref; pre-horizon failure ⇒ exactly ONE
   do_nothing row with `horizon=null` and gate_stage; horizon failure ⇒ per-horizon row; **horizon
   independence (rev2: SAFETY-F4): with horizons `("30m","5m")` on the half-day 12:45 tick, the FIRST
   horizon fails and the second STILL forecasts (exactly one do_nothing(30m) + one forecast(5m))**;
   **duplicate-id impossibility (rev2: SAFETY-F5): two consecutive ticks against a lagging (un-refreshed)
   FeatureView yield two DISTINCT decision_ids, the second a do_nothing(feature_cutoff_mismatch)**;
   ledger rejects `action="would_open"`, `paper_eligible=True`, out-of-vocab gate_stage (raises); rows
   carry rules_hash/data_pin; rerun with same run_id reproduces identical ids and row hashes; **S1
   static:** AST walk over ALL M3 module sources — no import of
   `agent.broker*`/`agent.execution_preflight`/`agent.kill_switch`/`agent.arming` anywhere (module scope
   OR nested), no `Name`/`Attribute` reference to the FD-12 forbidden tokens, **and no `Name`/`Attribute`
   reference to `importlib`/`__import__` at all (rev2: SAFETY-F6)**; **S1 subprocess:** a fixed-argv
   subprocess imports every M3 module fresh and asserts none of the FD-12 forbidden modules land in
   `sys.modules`; **S1 behavioral:** full E2E with committed config produces only
   `decision`/`forecast_scored`/`forecast_unresolved` event types and zero Candidate instances. [S1, S6]
10. **`test_config_signal.py`** — committed config has the signal block, all leaves strings/lists/dicts
    (recursive type assertion); `tighten_only_merge` with a hostile overlay (changed coefficients, windows,
    horizons, added keys) returns the base block unchanged; changing any signal leaf changes `rules_hash`
    and an unchanged config reproduces it byte-for-byte (one direction only — rules_hash covers the WHOLE
    config, rev2: BUILD-F17); `model_artifact_hash` matches an independent sha256 of the canonical model
    block; run gates still identity-False (extends, never replaces, the M0 canary). [S1, D6]
11. **`test_no_network_no_creds.py` (extended)** — import every M3 module under
    `mock.patch("socket.socket", ...)`; assert no `alpaca`/`databento`/`exchange_calendars`/plotting/ML
    module in `sys.modules` after import; run a minimal feature → snapshot → forecast → score path under the
    socket patch (design §9). [S1, offline purity]

Every S-invariant (S1–S4, S6) has at least one named test above; the build is TDD per CLAUDE.md.

## N. Open items deliberately frozen / deferred

- Multi-session feature lookback semantics (overnight gaps in returns) — M3 fixtures are single-session;
  deferred to M7 backtest-gate design. The engine computes over whatever eligible bars exist; the session
  gate (RTH-only forecasting) bounds exposure to the ambiguity.
- `bbo-1s`/`bbo-1m` vendor schemas as alternative `resample_midbars` inputs — the reader API is already
  source-tagged; adding a vendor-bbo adapter is an additive change (no contract break). M3 builds the tbbo
  path only.
- Live wiring of `FeatureView.refresh` cadence (`refresh_cadence_ms`) to a real scheduler — M3 tests drive
  `refresh()` explicitly; the cadence value is committed config for forward-compatibility.
- Probe scheduling (who calls `on_bar_complete`) — M3 tests call it directly; the M5 runner owns scheduling.
- `data_quality_alerts` emission for signal-tier anomalies — unresolved rows carry reasons (§I); revisit if
  M5+ needs alerting symmetry.
- Post-hoc calibration (Platt/isotonic), model re-fitting, `would_open` vocabulary — M7.

## O. References

- Design: `docs/superpowers/specs/2026-06-09-M3-signal-calibration-design.md` (D1–D6, §2 module map, §6
  invariants table).
- Parent: `docs/superpowers/specs/2026-06-08-stocks-agent-design.md` §5 (Tier 3/4), §7 (data model), §8
  (safety table).
- M2 contract (structure + conventions precedent): `docs/superpowers/specs/2026-06-09-M2-market-state-contract.md`.
- Repo facts: §0 table (file:line verified at `7902677`).

## P. Revision log (rev 2, 2026-06-09 — 4-lens critic pass, 52 findings applied)

Blockers fixed: **BUILD-F1** (resampler→reader missing-bucket seam + `out_of_series` definition);
**BUILD-F2/REPO-F1/REPO-F2/SAFETY-F3** (calendar plumbing: additive `MarketCalendar.schedule_for`/
`.calendar_pin` passthroughs; `calendar_unknown` attributed per-horizon at gate 5; phantom
`session_date_for` error path deleted); **SAFETY-F1** (resolver reads labels as-of `now_utc`;
future_receipt ⇒ DEFER not unresolved; `resolved_as_of_utc` recorded; climatology cannot ingest
future-received outcomes); **SAFETY-F2** (parse-then-compare everywhere + ONE canonical minted timestamp
form; mixed-ISO-form boundary tests); **MATH-Q1** (Murphy 3-term identity is exact only for
constant-p bins — fixture constrained, formulas pinned).

Majors fixed: cutoff-identity gate `feature_cutoff_mismatch` kills duplicate-id corruption (SAFETY-F5/
BUILD-F5); per-horizon stop semantics + reversed-horizon test (SAFETY-F4/BUILD-F13); collect-ALL reason
rule per stage with exact tuples (SAFETY-F7/REPO-F9); funnel identities redefined (SAFETY-F8/BUILD-F9);
S1 guard extended to importlib/`__import__` + subprocess isolation, FD-12 authoritative incl.
`require_token`/`consume`/`PreflightToken` (SAFETY-F6/F9); fixtures moved to covered calendar dates
2026-06-15 / 2026-11-27 (REPO-F6/BUILD-F10); one-sided persisted row dropped (REPO-F4/BUILD-F11);
`SignalConfig` → own module `signal_config.py` (REPO-F7); PROB/FEATURE quantize-only vs MID/BRIER
round-trip (REPO-F5/BUILD-F6/MATH-Q6); REL/RES/UNC + BSS + report formulas pinned (MATH-Q4/Q5);
z-window inclusive + RSI both-zero ⇒ 50 (MATH-Q2/Q3); `Decimal(repr(.))` boundary for p (MATH-Q7);
ResolveStats + ScoredLedger row shapes + UNRESOLVED_REASONS frozen (BUILD-F7/F8/F12); climatology
resume-seeding (SAFETY-F14); mid/spread suppression rule (BUILD-F3); `"mid-1m"` phrase deleted
(BUILD-F4/SAFETY-F12); spread compare on quantized field (MATH-Q8); edge_label exactness (MATH-Q9);
golden `generated_ts_utc` + per_cell/bins ordering pinned (SAFETY-F13/BUILD-F10); input-row filtering
(REPO-F10); per-key ordering (BUILD-F19); zero-bars FeatureSnapshot nulls (BUILD-F16); `[P_MIN,P_MAX]`
closed-interval comment (BUILD-F14); REPORT_QUANTUM declared + sample shapes (BUILD-F15); forecast
subdict mapping (BUILD-F18); rules_hash one-direction wording (BUILD-F17); §0 resample fact reworded
(REPO-F8/SAFETY-F10).

## Q. Harden log (round 1, 2026-06-09 — 4-lens adversarial code review, repro-gated)

22-agent review (4 reviewers → independent skeptical verification per finding): 18 raw findings, 17
confirmed (deduped to 9 unique defects), 1 refuted. All fixed TDD; suite 689 → 700 tests.

1. **M3-01 (BLOCKER)** — shared `AsOfClimatology` double-ingested outcomes on repeated `resolve_due`
   calls (re-seed + live ingest with no dedupe), corrupting persisted FD-6 reference fields. Fix:
   id-level idempotent ingestion (`ingest_resolved` now REQUIRES `forecast_id`; repeated id = no-op).
2. **M3-02 (major)** — the probe journaled the caller's bar-boundary string verbatim; a whole-second
   recorder form minted a second key/id set for the same logical bar. Fix: `on_bar_complete` re-mints
   the §0 canonical form on entry.
3. **M3-EDGE-1 (major)** — DST fall-back fold: ET-wall-clock bucket keys collide (PEP 495 equality
   ignores `fold`). Fix: buckets keyed by the UTC instant of the ET-floored start.
4. **M3-03 (minor)** — report emitted an extra top-level `horizons` key outside the frozen shape.
   Fix: removed; top-level key set now test-asserted; golden regenerated.
5. **M3-EDGE-4 (minor)** — zero-sample report emitted `bins: []`. Fix: FD-11 ten-empty-bin array always.
6. **M3-05 (minor)** — `_sma`/`_ema`/`_wilder_rsi` silently mis-divided on short input (latent). Fix:
   defensive length guards (unreachable behind FD-14, fail-loud if ever reached).
7. **M3-R1-003 (minor)** — §L MID/BRIER round-trip enforcement was missing. Fix: `_quantize_checked`
   at the mid and brier_i quantize sites.
8. **M3-R1-004 (minor)** — config windows could drift from the frozen v1 FEATURE_NAMES silently. Fix:
   `SignalConfig` pins `feature_windows/rsi_period/z_window/vol_window` to the v1 values, fail-loud.
9. **M3-R4 (minor)** — persisted-value Decimal arithmetic ran under the caller-mutable ambient
   context. Fix: pinned `Context(prec=28, ROUND_HALF_EVEN)` via `localcontext` at every persisted-
   arithmetic site (quote_quality, bar_series, feature_engine, forecast, calibration, report);
   context-immunity tests added.
