# Stocks Trading Agent — Design Spec

- **Date:** 2026-06-08
- **Status:** **Active — M0 implemented & hardened** (129 tests; design reviewed twice externally + a 5-lens adversarial workflow + a code review; M1 next; later milestones require their own plans/reviews)
- **Owner:** Robin
- **Sibling project:** `<sibling-workspace>` (the engineering spine this design ports)

> **TL;DR (DK):** Vi bygger en autonom aktie-handelsagent med samme `observe → paper → live`-disciplin som
> Polymarket-agenten. ~60–70 % af Polymarket-rygraden genbruges (engine, plugin-model, deterministisk journal,
> fail-closed gates, recorder/replay, dashboard, eksekverings-realisme). Det aktie-specifikke (broker, real-time
> data, markedskalender/halts, corporate actions, ordretyper, settlement/margin, kontinuerlig edge) bygges nyt.
> Stack: **Alpaca** (broker, paper og live deler API) + **Databento** (data, live≡historik), **hybrid fill-model**,
> kurateret single-name US large-cap, kalibrerings-probe først, live-penge-gate OFF bag two-key arming.
> **Live er ikke "bare et flip":** samme interfaces/kode-sti, men live kræver separat validering, broker dry-run,
> kill-switch-drill, caps og runbook.

---

## 1. Executive summary

This project builds an autonomous **stocks/equities** trading agent that mirrors the discipline of Robin's
Polymarket workspace: it starts **paper-only with "nothing opens by default"** and is otherwise **live-like**
(live data, live order semantics, live-equivalent fill realism), so that the path to live is **the same
interfaces and code path** rather than a rebuild. Going live is still an explicit, separately-validated step
(broker dry-run, kill-switch drill, caps, runbook) — not merely a config flip.

A deep review of the Polymarket codebase (9 subsystems mapped) found that roughly **60–70 % transfers** — and
the transferable portion is exactly the hard-won engineering spine, not the strategy alpha. What must be rebuilt
is everything prediction-market-shaped (binary 0/1 payoffs, arbitrage edge math, the UMA/Gamma resolution
oracle, the `condition_id`/token identity model) and the **entire live-data tier**.

The design is a **seven-tier layered architecture** that keeps the proven spine at the top (engine, Strategy
plugin model, deterministic journal, risk gates, dashboard) and concentrates the rebuild in the bottom (data)
and the settlement/risk middle. We start as an **observe-only calibration probe**, then require a **historical
replay/backtest gate (anti-lookahead)** before any paper-eligible strategy, and well before any live canary.

The single biggest risk is **data quality**: the data tier is a from-scratch rebuild, and getting it right
(streaming NBBO + depth + bars, replay/reconcile, sequence-gap detection) is the precondition for the
execution-realism discipline to mean anything.

## 2. Goals and non-goals

### Goals
1. A long-running autonomous agent that ingests live equity market data, runs pluggable strategies, and opens /
   marks / closes **paper** positions with **live-equivalent fill realism**.
2. **Same interfaces and code path for paper and live.** Every seam (broker, data, fill model) is built so the
   live implementation is the same code behind a flag — while keeping that live is still a separately-validated
   step (dry-run, kill-switch drill, caps, runbook), not a silent flip.
3. Reuse the Polymarket spine wherever it is asset-agnostic; rebuild only what is genuinely equity-specific.
4. **Safety-first posture:** observe → paper → live, fail-closed, "nothing opens" by default, enforced by
   config-canary tests against the real order-submission seam.
5. Evidence-grounded edge: an observe-only calibration loop **and** a historical anti-lookahead backtest prove a
   forecasting model before any capital (even paper) is risked on it.

### Non-goals (initial scope)
- Options, futures, FX, crypto, international markets. (Single-name **US equities** only at start.)
- High-frequency / sub-millisecond latency. (We target seconds-scale decision cadence.)
- Portfolio optimization across thousands of names. (Bounded curated universe.)
- Real money, until the calibration loop + backtest demonstrate realized edge **and** a separately-approved live
  gate is armed. Paper-only is the committed default.

## 3. Locked decisions

| # | Decision | Choice | Why |
|---|----------|--------|-----|
| 1 | Broker / execution seam | **Alpaca** | Paper and live share one API surface (base-URL + key differ) → live reuses the same code path. Simple REST/WS, fractional shares. Going live still requires separate validation (below). |
| 2 | Market-data vendor | **Databento** (primary) | Live and historical share one normalized schema → recorder/replay/backtest/live run identical code. Depth (L2 MBP-10 / L3 MBO) makes depth-aware fills honest. `MarketDataTransport` abstraction keeps **Polygon** as an alternate provider candidate, not a guaranteed drop-in replacement, because depth/status/replay semantics can differ. Exact datasets pinned in §5.1 / M1. |
| 3 | Fill model | **Hybrid (broker-authoritative)** | Alpaca (paper, later live) is the **position-of-record**; Databento depth produces an independent **execution-realism label/score**, journaled separately — it never overrides the broker ledger. |
| 4 | Asset universe | **Curated single-name US large-cap (~20–50)** | Keeps the data tier tractable; JSONL persistence holds; depth is affordable + high-fidelity on a focused set. Going broad later is a throughput change, not an architecture change. |
| 5 | First strategy | **Observe-only calibration probe** → backtest gate → directional | Validates the model-vs-market edge, the data/execution pipeline, and a historical anti-lookahead backtest before risking even paper capital. |
| 6 | Live posture | **Live-capable from day one, live-money gate OFF** | Build the broker live-seam, flatten-then-halt kill switch, and the margin/locate gates as first-class from the start; flip to a tightly-capped live canary only after realized edge is shown, behind two-key arming + a documented go-live checklist. |

**Considered and rejected:** an off-the-shelf framework (QuantConnect Lean, backtrader, zipline). Optimized for
backtesting, not the second-quote execution-realism discipline; larger conceptual rebuild for live.

## 4. Architectural principles (carried from Polymarket)

Asset-agnostic, adopted verbatim:

1. **Plugin protocol + per-tick read-only context.** `Strategy` is a `runtime_checkable` Protocol
   (`name`, `paper_eligible`, `async scan(feed, ctx) -> list[Candidate]`); a frozen `ScanContext` is built once
   per tick. Adding a strategy = drop a class in the list.
2. **Structural price-object gate.** The paper book refuses to fill against anything but a typed, fully-fillable,
   fresh, provenance-stamped quote object. A bare float / discovery / REST price is structurally rejected.
3. **Exact-integrated-notional cost basis, never `size * vwap`.** `Decimal` throughout; float only at one
   documented seam.
4. **Separation of mark source vs settlement source.**
5. **Fail-safe / fail-closed defaults everywhere.** Missing status → blocks new opens but never force-closes a
   held one; ambiguous → no action.
6. **Append-only deterministic JSONL + rehydrate.** `json.dumps(sort_keys=True, separators=(",", ":"))`,
   Decimal-as-string, one write per row; restart rebuilds from OPEN row (immutable facts) + LATEST row.
7. **Fail-closed run gates + tighten-only config merge + `rules_hash` + a single `can_open()` chokepoint.**
8. **Pluggable-transport + dual-hash replay/reconcile.** `FakeTransport` / `FlakyTransport` drive the pipeline
   deterministically offline; `raw_event_hash` + derived-state hash + replay re-derivation + external reconcile.
9. **Config-canary "nothing opens" tests** against the real order-submission seam.
10. **`plan → adversarial multi-lens review → harden → verify → live-validate`** per increment.

### Paper-realism principles (the crux of "live-like")
1. **Never fill on the quote you decided on.** Re-quote a **second** fresh NBBO after a real awaited latency
   budget (the await + run-loop integration lives in `orchestrator.py`, not `execution_realism.py`) whose
   timestamp strictly post-dates the decision stamp by a configured minimum; a **missing decision timestamp is a
   hard reject** (`missing_decision_stamp`), never a skipped check.
2. **Model fillability against depth, not top-of-NBBO.** Walk available size and compute a depth-integrated VWAP
   with explicit `worst_price = slippage`. (Depth granularity is dataset-dependent — see §5.1; L2 MBP-10 gives
   **conservative/probabilistic** fillability, **not** true queue position, which requires L3 MBO.)
3. **Fail-closed on unknown.** Stale/un-timestamped quote → unfillable; unknown tick/lot size → reject in strict
   mode; malformed config → enables-but-rejects-all.
4. **Provenance-gated truth source.** Marks / modeled fills accepted only from a typed quote carrying provenance
   + `book_hash` + timestamps (e.g. `databento_mbp10`).
5. **Two-class evidence separation, never collapsed.** *Optimistic* paper (filled at decision-time NBBO) vs
   *execution-realistic* paper (passed the second-quote re-validation), journaled and dashboarded distinctly.
6. **Enumerate machine-readable reject reasons** (cheap/structural → size/tick → freshness/epoch → economic).
7. **Model real order semantics, not just FOK** — but start with a **narrow supported-order matrix** (Tier 6)
   and expand only as broker behavior is verified.
8. **The broker fill is authoritative; the modeled fill is a label.** Validate the broker's reported fill
   against an independent fresh Databento NBBO and **flag** divergence (e.g. fill beats NBBO → optimistic); never
   overwrite the broker ledger with the modeled price.
9. **Reconnect/epoch + session-stability gates.** A pre-submit `execution_preflight` must pass after the latency wait and second quote: if `reconnect_epoch` changed or the symbol entered halt/auction/LULD before submit, **do not submit the broker order**. If an already-accepted broker order later reports fills, those `broker_fill` rows remain authoritative, while the modeled assessment can be rejected/flagged.
10. **Calibrate latency/freshness budgets to the actual feed/broker**, not Polymarket's 500 ms.
11. **Paper realism is not live-money proof.** A separate real-feed live-validation + an approved real-money
    canary precede any live capital.

## 5. System architecture — seven tiers

`reuse` = port with low change; `adapt` = reuse shape, rebuild body; `new` = net-new.

### Tier 1 — Data / ingest
**Responsibility:** subscribe to streaming NBBO quotes, trade prints, and depth; maintain per-symbol
`EquityBookState` + `TradeTape`; persist hash-stamped append-only events; maintain a bar cache (1s/1m/daily)
with resampling.

- **reuse:** `clob_recorder/recorder.py` single-writer loop + reconnect/backoff; `persistence.py` EventWriter
  (append-only, daily rotation, gzip, retention from config); `replay.py` + `reconcile.py` dual-hash harness +
  `book_hash.py` (the derived-state hash `replay.py` imports — **rewrite for L2 MBP-10 depth ladders**, not a
  verbatim port); `lock.py` PID lock; `status.py` heartbeat; `FakeTransport`/`FlakyTransport` tests.
- **adapt:** `event.py` → parse Databento schemas; `token_ids.py` → `instrument_id` ↔ `symbol`+MIC identity;
  reconcile ground-truth → Databento historical + Alpaca positions.
- **new:** **sequence-gap detection keyed to the dataset's actual sequencing semantics** (some composite feeds do
  not provide meaningful per-venue sequence numbers — verified per dataset in M1); the **bar cache** + resampler
  (**ET session-boundary bucketing**, see §11 — tested across a DST transition); **always-on reconnect** — the
  ported recorder caps reconnect at 5 attempts (fine for a batch recorder, unsafe for a 24/5 feed), so make it
  unbounded-with-backoff-cap and raise a `data_quality_alert` on prolonged disconnect rather than terminating
  silently; entitlement/auth + heartbeat handling.
- **Key types:** `MarketDataTransport` (Protocol: `async stream(symbols) -> AsyncIterator[bytes]`),
  `EquityBookState`, `TradeTape`, `BarCache`, `Quote` (typed; provenance + `book_hash` + monotonic & wall ts +
  `reconnect_epoch`).

#### 5.1 Databento dataset / schema / entitlement matrix
Datasets are **pinned per milestone** in the implementation plan; this design fixes the *levels* and the
verification requirement. (Verified 2026-06-08: EQUS.MINI is a **top-of-book L1 composite**, not L2.)

| Need | Level | Dataset class (exact code pinned in M1) | Schemas | Notes |
|------|-------|------------------------------------------|---------|-------|
| Signals + top-of-book NBBO | L1 | composite (EQUS.MINI-class) | `tbbo` (primary NBBO) / `bbo-1s` / `bbo-1m` / `trades` / `ohlcv-1s` / `ohlcv-1m` / `definitions` | Cheap; **no MBP-10**; **no `status` schema** → halt/LULD from broker + `exchange_calendars`; `definitions` supplies `instrument_id`↔`symbol`/MIC; pick one of `tbbo`/`bbo-1s`/`mbp-1` as NBBO source (redundant cost otherwise); sequence semantics verified in M1. |
| Depth-aware fills (depth-VWAP) | L2 | depth-capable US-equities dataset | `mbp-10` | Conservative/probabilistic fillability; **not** queue position. |
| True queue position (optional upgrade) | L3 | venue-native (e.g. `XNAS.ITCH`-class) | `mbo` | Only if/when resting-order queue modeling is required; higher cost/volume. |

**M1 deliverable:** a pinned matrix with exact dataset codes, entitlements, per-schema sequence-number behavior,
and a verification command that asserts the chosen dataset actually serves the required schema.

### Tier 2 — Market-state
**Responsibility:** decide per-symbol tradability + apply corporate actions.

- **reuse:** `market_status.py` pure-decider behind an injectable fetcher seam; `market_status_cache.py`
  freshness-gated non-blocking cache (degrades to safe default when stale); tighten-only severity merge; union
  open-position symbols into the refresh set.
- **adapt:** decider body → session state machine `{pre, rth, post, closed, auction, halted}` + `halt/LULD/SSR`
  flags, driven by a market calendar (`exchange_calendars`) and vendor/broker status messages; replace the
  boolean orderbook flag with a live two-sided-NBBO check. A **status/halt ledger** (`journal/status.jsonl`)
  records state transitions explicitly (not only implicitly via alerts).
- **new — corporate actions, fail-closed:** consume Alpaca's CA feed **plus** the data-vendor CA feed and
  **cross-validate**; treat ex-date/event windows as a **trading blackout** for the affected symbol; reconcile
  against broker account activity (Alpaca paper does **not** simulate dividends and CA data may be delayed with
  no creation-time guarantee, so the agent must not assume an adjustment happened); emit `corporate_action`
  rows with **explicit provenance** modeled as a **tri-state** (`confirmed_by_N_sources` /
  `single_source_blackout` / `conflicting_blackout`) — **≥2 independent sources required to clear blackout**, a
  lone source stays blacked-out; a **broker-adjusted-while-blacked-out detector** freezes the symbol and forces
  an immediate (not EOD) reconcile if broker position qty changes during a CA blackout; durable CUSIP/FIGI
  identity under unstable tickers.
- **Key types:** `SessionState`, `TradabilityDecider` (pure), `MarketStateCache`, `CorporateActionFeed`,
  `AdjustmentEvent` (with `provenance_set`, tri-state `validation_status`).

### Tier 3 — Snapshot / signal
- **reuse:** `pm_snapshot.summarize_book()` depth/spread math + warnings-as-data → quote-quality filters
  (re-expressed in **bps**); atomic-snapshot pattern; public-API-with-fallback fetch.
- **new:** a **feature/indicator engine** fed by the bar cache, holding rolling state **outside** the stateless
  `scan()` hot path, refreshed on a background cadence: MAs, RSI, z-scores, realized-vol, forecast features.
- **Key types:** `FeatureEngine`, `SignalSnapshot`, `QuoteQuality`.

### Tier 4 — Strategy
- **reuse directly:** `strategy.py` Protocol + frozen `ScanContext`; `candidate.py` `Candidate`/`Leg` (single-leg
  = N=1); observe-only probe template (`paper_eligible=False`); two-tier edge discipline + `journal_deduped`
  staging.
- **new:** the **continuous edge/sizing function** (expected-return / z-score / forecast-vs-price gap instead of
  `net_surplus_pp`); vol-target / Kelly sizing; a continuous-PnL calibration loop (forecast vs realized move).
  First strategies: a **calibration probe**, then (after the backtest gate) a directional momentum/mean-reversion
  strategy; an event/threshold probe ("will AAPL close above X") as an easy early calibration target.
- **Do NOT port:** `arb_strategy.evaluate_bundle` / `net_surplus_pp`.

### Tier 5 — Risk / gates
**Responsibility:** fail-closed run gates + a single pre-trade `can_open()` chokepoint enforcing exposure caps.

- **reuse (gates) / adapt (can_open):** `gates.py` identity-strict run-gate ladder + `config.py` tighten-only
  merge + `rules_hash` port directly. `portfolio.can_open()` **adapts**: the cap-ladder structure transfers
  (renamed `market_id→symbol`, `theme→sector/factor`) but the **signature is rebuilt** to
  `can_open(candidate, portfolio, account) -> Verdict` reconciling broker buying power (no account param today).
- **new sub-gates:**
  - **`IntradayMarginModel` (canonical).** Per FINRA Regulatory Notice **26-10** (verified 2026-06-08:
    published Apr 20 2026, effective **Jun 4 2026**, phase-in to Oct 20 2027), the amended Rule 4210 intraday
    margin standards **replace** the legacy day-trade-count "pattern day trader" designation and the $25k
    minimum. The canonical model monitors **intraday margin exposure / actual market exposure**, not a day-trade
    count. **The broker's reported buying power / margin is ground truth** — the model reconciles against it
    rather than re-deriving a hardcoded rule.
  - **`LegacyPdtCompatMode` (transition only).** Because firms may phase in until Oct 2027, the broker may still
    enforce the old PDT/$25k regime during transition. This mode mirrors **whatever Alpaca actually enforces**
    (detected from broker account fields / rejections), and is **not** the canonical rule.
  - Short-sale locate/borrow availability + borrow cost + SSR uptick handling; gross & net leverage; per-sector /
    per-factor / beta exposure caps; a **max-drawdown / daily-loss kill switch** as a first-class halt condition;
    SEC/TAF/FINRA regulatory fees (in FeeModel).
- **Behavior:** on a kill-switch / live-gate flip, **flatten-then-halt** (a frozen halt leaves live exposure
  unmanaged). **Flatten submits closing / reduce-only (position-decreasing) orders only** — S1 "nothing opens"
  constrains *opens*, not risk-reducing closes; a kill switch can **never** emit an opening/position-increasing
  order (asserted by test).
- **Key types:** `RunGates`, `RiskRules` (hashed), `can_open(candidate, portfolio, account) -> Verdict`,
  `KillSwitch`, `IntradayMarginModel`, `LegacyPdtCompatMode`, `LocateCheck`.

### Tier 6 — Paper execution (hybrid, broker-authoritative)
**Responsibility:** the open → mark → close lifecycle with live-equivalent fill realism, **without** drifting
from the broker ledger.

- **reuse:** `paper_book.py` lifecycle shape; the typed-price-object fill loop; exact-integrated-notional Decimal
  cost basis; per-position fee assumption stamped on rows; `execution_realism.py` second-quote re-validation;
  `fees.py` FeeModel.
- **adapt:** binary 0/1 payoff → **continuous close-at-price**; add a **short path** (sell-to-open, mark at
  best_ask, inverted sign); `resolution.py` **replaced** by a settlement / corporate-action / exit engine
  (terminal-state *pattern* survives); FeeModel += SEC/TAF/FINRA + maker/taker + borrow.
- **new:**
  - **`Broker` abstraction** (`AlpacaPaperBroker` / `AlpacaLiveBroker`) with a hard kill switch. The broker is
    the **position-of-record**: positions, cash, buying power, and fills come from the broker and are
    authoritative.
  - **`broker_fill` vs `modeled_execution_fill` are journaled as separate records.** The broker fill drives the
    account ledger and PnL; the Databento-depth modeled fill is an **execution-realism label/score** (how
    optimistic was the broker fill vs an independent depth-aware estimate?) and **never** overrides the broker ledger.
    This removes the by-design drift between the local journal and broker reconciliation. PnL is split into
    `broker_account_pnl` (reconciliation/accounting truth) and `execution_realistic_pnl` (strategy-evaluation
    truth), held as **distinct newtypes** (`BrokerUSD` vs `ModeledUSD`) so a modeled price is type-incompatible
    with a broker-ledger field and cannot be silently written into it.
  - **Pre-submit execution preflight (structural chokepoint, fail-closed by type).** After the awaited latency
    budget and second quote, re-check feed epoch, quote freshness, session state, halt/LULD/auction/SSR state,
    tick/lot validity, and `can_open()`. `Broker.submit_order()` accepts a **non-forgeable preflight token** that
    only a *passing* preflight can mint, so the broker seam is structurally unreachable without one — **regardless
    of any enable flag** (disabled ≠ `ok=True`; disabled means reject-all). Tokens are **split by intent** so the
    fail-closed posture never blocks risk reduction: an **`OpenPreflightToken`** (opening / position-increasing
    orders) runs the full gates and is **reject-all when the open run-gates are off**; a
    **`ReduceOnlyPreflightToken`** is mintable **only for an existing held position + a position-decreasing order
    on the kill-switch / halt / close path**, and is *not* gated off by the open run-gates (so flatten can always
    reduce exposure). Every submit path routes through one of the two; failure writes a `reject` row and submits nothing.
  - **Post-submit state-change control.** If feed epoch changes or the symbol halts/LULD/auctions *after* submit
    but before terminal, issue a best-effort **cancel** and journal a `post_submit_cancel_attempt`; the order is a
    **marketable-limit with a worst-price cap** derived from the preflight quote (never a bare market order, which
    Alpaca rejects outside RTH), so any late fill stays price-bounded. DAY TIF is unsafe until this exists.
  - **Narrow supported-order matrix first:** start with **marketable-limit + DAY**; explicitly test and journal
    broker **rejection paths**; expand order types / TIFs only as Alpaca support is verified.
  - Partial-fill modeling from the broker's actual fills; conservative depth-based fillability estimate from L2
    (true queue position deferred to an optional L3/MBO upgrade); Reg-NMS price-dependent sub-penny ticks.
- **Hybrid wiring:** decision → await latency budget → re-quote fresh Databento depth → run `execution_preflight`
  (epoch/session/halt/LULD/freshness/tick/lot/`can_open()`); only if it passes: (a) submit the order to Alpaca
  (paper/live) and record the **authoritative** `broker_fill`(s); (b) independently compute a
  `modeled_execution_fill` from the re-quoted depth + execution-realism gate; (c) journal both, label the realism
  class, alert on divergence, and update both `broker_account_pnl` and `execution_realistic_pnl`. Our own
  `can_open()` sub-gates (margin/locate/exposure) are authoritative for *whether to open*; the broker is
  authoritative for *what actually filled*.
- **Key types:** `Broker` (Protocol; `submit_order` requires a preflight token), `PaperBook`, `OrderIntent`,
  `OpenPreflightToken`, `ReduceOnlyPreflightToken`, `BrokerFill`, `ModeledFill`, `SettlementEngine`.

### Tier 7 — Journal / reconcile
- **reuse:** `paper_positions.py` append-only deterministic JSONL writer + `rehydrate.py` (verbatim; Decimal-as-
  string; field vocabulary changes; two-stream event-sourced shape + rehydrate contract unchanged); idempotent-
  by-foreign-key; first-class `do_nothing` rows.
- **new — cross-stream atomicity & correlation:** every row carries `run_id`, `decision_id`, and (where
  applicable) `order_id`, plus a **per-stream monotonic `seq`**. A **single writer lock** guards each stream; a
  documented **partial-write replay rule** (last line truncated/invalid → drop on replay) keeps streams
  recoverable. Streams: `decisions.jsonl`, `positions.jsonl`, `fills.jsonl` (broker + modeled, tagged),
  `status.jsonl` (session/halt ledger), `reconcile_alerts.jsonl`, `data_quality_alerts.jsonl`.
- **new — broker reconciliation:** a **SOD/EOD** job on the `reconcile_runner.py` structure (replay local state,
  diff against Alpaca positions/account as ground truth, write status + alerts, non-zero exit on mismatch); on
  conflict the broker is truth and a `reconcile` adjustment row is emitted (never a silent mutation).

### Dashboard / observability
- **reuse:** `dashboard/app.py` `ThreadingHTTPServer` on 127.0.0.1; `_safe_workspace_path()` sandbox;
  `html.escape` + CSP + `MAX_FILE_BYTES`; pure `_capital_summary()` money roll-up (unit-tested without a server).
- **adapt:** the ~40 renderer bodies → tickers / shares / broker-vs-modeled fills / NBBO / sector exposure;
  `caps_used` → equity risk dimensions (gross/net/sector/beta/drawdown); show the optimistic-vs-realistic split.
- **guard:** if it ever exposes live order actions or leaves localhost, add auth/CSRF/TLS.

## 6. The hybrid execution-realism fill model (broker-authoritative)

```
decision (t0, Databento quote A, signal+forecast)  ──► await latency_budget_ms  ──► RE-QUOTE (t1>t0, fresh NBBO+depth)
        │                                                                                   │
        ▼                                                                                   ▼
[execution_preflight]  epoch/session/halt/LULD/freshness/tick/lot/can_open checks; FAIL ⇒ reject row, NO BROKER SUBMIT
        │ pass
        ├──────────────────────────────────────────┬──────────────────────────────────────┐
        ▼ submit order                             ▼ modeled (independent)                ▼ already-accepted order handling
[Alpaca paper/live order]                 [execution-realism gate]              if broker later reports fills after acceptance,
 authoritative broker_fill(s):             depth-VWAP at quote B size            keep broker_fill authoritative; modeled side
 fills / avg_price / qty / venue           Reg-NMS tick · enumerated reject      may be rejected/flagged, never back-voided
 → broker ledger + broker_account_pnl      reasons → modeled_execution_fill
        └──────────────────────────────────────────┬──────────────────────────────────────┘
                                                    │
        journal broker_fill (authoritative) AND modeled_execution_fill (label) as SEPARATE rows;
        maintain broker_account_pnl AND execution_realistic_pnl; evaluate strategy on execution_realistic_pnl
```

Account ledger = broker. Realism evidence = modeled. They are never collapsed, so reconciliation against the
broker cannot drift. Strategy quality and paper-readiness gates use `execution_realistic_pnl`; broker/account
operations use `broker_account_pnl`. Going live: `AlpacaPaperBroker` → `AlpacaLiveBroker` (same interface, same gate) — plus the
separate go-live validation (§12).

## 7. Data model — event-sourced JSONL

All rows: `json.dumps(sort_keys=True, separators=(",", ":"))`, Decimal-as-string, one write per line, row hash,
plus `run_id`, `decision_id`, per-stream monotonic `seq`, and `ts_utc` (UTC persisted; see §11 timezone policy).
Single writer lock per stream; partial-write replay rule drops a truncated trailing line.

Event types (illustrative fields):
- `decision` — `symbol`, `strategy`, `action ∈ {do_nothing, would_open}`, `forecast`, `edge`,
  `signal_provenance`, `quote_provenance`.
- `order_submitted` — `position_id`, `order_id`, `symbol`, `side ∈ {long, short}`, `qty`,
  `order_intent {order_type, tif, limit_price}`, `token_kind ∈ {open, reduce_only}`. (Submission ≠ fill; fills
  and `realism_class` are async and land on the rows below, not here.)
- `broker_fill` — `order_id`, `position_id`, `filled_qty`, `avg_price`, `liquidity_flag`, `venue`,
  `cost_usd` (authoritative), `fee` — **drives the broker ledger and broker_account_pnl**.
- `modeled_execution_fill` — `order_id`, `modeled_price`, `modeled_vwap`, `slippage`, `quote {provenance,
  book_hash, seen_at_ms, reconnect_epoch}`, `divergence_vs_broker`, `realism_class` — **label/scoring only**.
- `pnl_snapshot` — `position_id`, `broker_account_pnl`, `execution_realistic_pnl`, `realism_class`,
  `basis {broker, modeled}`, `used_for_strategy_evaluation: execution_realistic_pnl`.
- `mark` — `position_id`, `mark_price`, `mark_source ∈ {best_bid, best_ask}`, `unrealized_pnl`.
- `corporate_action` — `symbol`, `type`, `factor`, `adjusted_qty`, `adjusted_cost_basis`, `provenance`,
  `cross_validated`.
- `close` — `position_id`, `exit_price`, `realized_pnl`, `fees`, `reason`.
- `reject` — `symbol`, `reason` (enumerated: `missing_decision_stamp` / `stale_quote` / `epoch_changed` /
  `halt_luld_ssr` / `invalid_tick_lot` / `can_open_denied` / `latency_lost_edge` / ...), `stage`.
- `post_submit_cancel_attempt` — `order_id`, `cause`, `outcome`.
- `status` — `symbol`, `from_state`, `to_state`, `cause` (session/halt/LULD/SSR/auction).
- `reconcile` — `symbol`, `local`, `broker`, `diff`, `action`.

(Type↔row mapping: the `BrokerFill` / `ModeledFill` types in §5 Tier 6 serialize to the `broker_fill` /
`modeled_execution_fill` rows here.)

Rehydrate: OPEN row = immutable facts; LATEST row per `position_id` = evolving state; `close`/terminal = skip.
The **broker ledger** is the source of account economics; `execution_realistic_pnl` is the source for strategy
evaluation and paper-readiness claims.

## 8. Stock-specific subsystems (the must-builds)

1. **Broker execution abstraction** (Alpaca paper + live) — position-of-record; async fills; submit/cancel;
   positions/account queries; hard live kill switch; config-canary tests target this seam.
2. **Real-time market-data feed** (Databento) — streaming NBBO + trades + depth + bars; sequence/heartbeat;
   reconnect; entitlement/auth; behind the recorder transport seam; **dataset matrix pinned (§5.1)**.
3. **Execution preflight** — pre-submit gate after second quote; blocks broker submission on epoch/session/halt/LULD/
   freshness/tick/lot/can-open failure and writes a machine-readable reject.
4. **Market calendar + session/halt/LULD/SSR/auction gate** with session-aware gap detection + a `status.jsonl`
   halt ledger.
5. **Corporate-action handling, fail-closed** — cross-validated multi-source CA; ex-date/event blackout; broker-
   activity reconciliation; explicit adjustment provenance; CUSIP/FIGI identity.
6. **Order types + TIF + partial fills + slippage** — **narrow first** (marketable-limit + DAY), broker
   rejection paths tested; expand only as verified.
7. **Settlement / margin / short-locate / regulatory-fee layer** — T+1 settlement; **`IntradayMarginModel`
   canonical** + `LegacyPdtCompatMode` (transition); Reg-T margin + buying power **reconciled from the broker**;
   short locate/borrow + SSR; SEC/TAF/FINRA fees; max-drawdown/daily-loss kill.
8. **Position reconciliation vs broker** (SOD/EOD) — broker is ground truth.
9. **Continuous edge/sizing/risk model** — expected-return / z-score / forecast-gap edge; vol-target / Kelly;
   VaR/CVaR/Sharpe/beta; continuous calibration loop; **historical anti-lookahead backtest gate** that evaluates
   `execution_realistic_pnl`, not raw Alpaca paper PnL.

## 9. Safety invariants → verification

Each invariant below MUST have at least one automated test. Exact test names, fixtures, artifact paths, and run
commands are specified in the per-milestone implementation plans (M0/M1 first); this section is the contract.

| # | Invariant | Verified by (test class) |
|---|-----------|--------------------------|
| S1 | On the committed config, **nothing opens**: no **opening / position-increasing** order is ever submitted — only an `OpenPreflightToken` can mint one and it is reject-all when the open run-gates are off. On the committed config this means **zero positions ever exist**, so the canary observes **zero `submit_order` calls of any kind** at the Protocol boundary (no positions → nothing to flatten); a `ReduceOnlyPreflightToken` is structurally unreachable without a held position. When `AlpacaLiveBroker` exists (M8), the live submit is unreachable unless two-key arming + `live_trading.enabled` are both present. | config-canary at the Broker Protocol boundary (assert zero opening submits; zero total submits on committed config). |
| S2 | Float/NaN/Inf never reaches a price/size field; money/qty persisted as Decimal-as-string. **Re-verified where floats are born:** M1 resampler (empty-window VWAP) and M3 feature engine (zero-variance z-score), not only the M0 serializer. | serializer + resampler + feature-engine NaN/Inf-injection tests. |
| S3 | Replay re-derives identical state hashes; a truncated trailing line is dropped, not fatal. | replay idempotency + partial-write tests. |
| S4 | A stale/un-timestamped/epoch-changed quote, changed epoch, halt/LULD/auction transition, invalid tick/lot, or failed `can_open()` blocks broker submission at `execution_preflight`. | execution-preflight + execution-realism freshness/epoch tests. |
| S5 | The broker ledger is never overwritten by a modeled fill; `broker_account_pnl` and `execution_realistic_pnl` remain separate; reconciliation flags broker drift and exits non-zero. | reconcile drift-injection + PnL split tests. |
| S6 | Cross-stream rows correlate by `run_id`/`decision_id`/`order_id` with monotonic `seq`. | journal correlation tests. |
| S7 | A corporate action with missing/uncross-validated provenance triggers blackout, not a silent adjustment. | CA fail-closed tests. |
| S8 | Kill-switch / live-gate flip flattens-then-halts (no frozen open exposure). | kill-switch drill test. |
| S9 | No paper-eligible strategy can open until a **signed/committed backtest artifact** keyed by `(strategy_name, rules_hash, data_pin)` exists (no artifact → no open, fail-closed), scored on `execution_realistic_pnl`. The M5 synthetic strategy can open **only** against a `FakeBroker` (a `SyntheticStrategy` base that can never bind `AlpacaPaperBroker`/Live), so synthetic ≠ paper-eligible structurally. | strategy-gate + synthetic-isolation + backtest metric tests. |
| S10 | Intraday margin model covers IML-reducing transactions, intraday margin deficit calculation, 15-business-day outstanding window, 5-business-day/90-calendar-day freeze trigger, and minor-deficit exceptions; legacy PDT is compat-only. | intraday-margin fixture tests. |

## 10. Phased roadmap (each milestone: its own spec → plan → review → verify)

| Milestone | Deliverable | Acceptance (invariants + checks) |
|-----------|-------------|----------------------------------|
| **M0 — Skeleton + abstractions** | Repo layout + **charter files** (AGENTS/CLAUDE/PLAN restating §12); **`requirements.txt`** (M0 stdlib-only; third-party deps pinned per milestone — `databento`/`exchange_calendars` in M1, `alpaca-py` in M5) + **import wiring** (`conftest.py`/`__init__.py` so dotted-path unittest resolves); `Broker` iface + spy/no-op `AlpacaPaperBroker` (imports no `alpaca-py`); `MarketDataTransport` iface + fake transport; deterministic journal w/ correlation IDs + writer lock; config/gates + `rules_hash`; **typed preflight tokens** (`OpenPreflightToken`/`ReduceOnlyPreflightToken` reject-all stubs; full S4 in M5); `kill_switch.py` (reduce-only state machine); dashboard stub + sandbox test; `.secrets/` layout + no-network test; two-key arming stub. Plan: `docs/superpowers/plans/2026-06-08-M0-skeleton-abstractions-implementation-plan.md`. | S1 (Broker-boundary, all paths), S2, S3 (partial-write), S6, S8 (reduce-only); live gate OFF in committed config. |
| **M1 — Data tier** | Databento recorder behind transport seam; **dataset-scoped** pinned dataset/schema/entitlement matrix (§5.1) + offline-tested verification command (per-`(dataset,schema)` pair); equity `book_hash` (L2 rewrite); replay/reconcile; bar cache + **ET-boundary resampler (DST-tested)**. Plan: `docs/superpowers/plans/2026-06-08-M1-data-tier-implementation-plan.md`. | S3 (replay hashes), **S4 (inputs only; full S4 in M5)**; reconcile vs Databento historical passes; sequence-gap detection fires on injected gaps; Fake/Flaky tests green. *(market calendar + session gate moved to M2.)* |
| **M2 — Market-state** | **Market calendar + session gate** (moved from M1) + session/halt/LULD/SSR/auction state machine; `status.jsonl` ledger; **fail-closed** corporate-actions w/ **tri-state** cross-validation + blackout + broker-adjusted-while-blacked-out detector. | S7; decider unit-tested across sessions/halts; split adjusts a held position only with ≥2-source provenance, else blackout; broker-silent-adjust freezes + forces reconcile. |
| **M3 — Signal + calibration probe** | Feature engine; observe-only calibration probe (`paper_eligible=False`). | S1 (probe opens nothing); probe logs forecasts + realized-move scoring; calibration report renders. |
| **M4 — Risk core** | `IntradayMarginModel` + `LegacyPdtCompatMode`; locate/SSR; exposure caps; max-drawdown/daily-loss kill switch reconciled from broker buying power. | S8, S10; `can_open()` rejects per sub-gate in tests; buying power reconciled from broker, not re-derived. |
| **M5 — Paper-exec hybrid** | Alpaca paper + Databento second-quote gate + `execution_preflight` (PreflightToken chokepoint, all submit paths) + the `orchestrator.py` latency-wait seam (fake clock); **post-submit cancel-on-state-change**; broker-fill vs modeled-fill/PnL separation (`BrokerUSD`/`ModeledUSD` newtypes); **narrow order matrix** + rejection paths; first open/mark/close via a **synthetic strategy** (FakeBroker-only; real paper-eligible awaits the M7 gate per S9). | S4, S5, S9 (synthetic-isolation); preflight blocks broker submit on unstable state; post-submit halt triggers cancel + price-bounded fill; optimistic vs realistic journaled separately; ledger from broker. |
| **M6 — Reconcile hardening** | SOD/EOD broker reconciliation; drift injection. | S5 (drift flagged, non-zero exit); broker = truth on conflict. |
| **M7 — Backtest gate** | Historical anti-lookahead backtest + raw-vs-adjusted policy + exposure-aware benchmark attribution; **first paper-eligible directional strategy** behind the gate. | S9; backtest reproducible from pinned data; no lookahead (point-in-time fixtures); strategy gate uses `execution_realistic_pnl`; paper-eligible only after pass. |
| **M8 — Live canary** | (Only after realized edge.) Tightly-capped single-name live canary behind two-key arming + flatten-then-halt + **go-live checklist** (broker dry-run, kill-switch drill, caps, runbook). | Separately-approved live gate; caps enforced; live-validation runbook + real-money boundary documented. |

## 11. Conventions

- **Timezone policy:** all market logic (sessions, halts, ex-dates, official close) computed in **America/New_York
  (ET)**; all timestamps **persisted in UTC** (`ts_utc`) plus a monotonic clock for latency; UI may render local.
- **Determinism:** `json.dumps(sort_keys=True, separators=(",", ":"))`, Decimal-as-string, one write per row.
- **Subprocess** only via fixed command arrays, never `shell=True`.
- **Secrets** in `.secrets/`, never committed.

## 12. Hard boundaries (committed default)

- `config/risk_rules.json → live_trading.enabled = false` **always** until an explicit, separately-approved go.
- `config/agent_rules.json → enabled = false` and `paper_trading.enabled = false` are the run gates; they stay
  `false` on the committed config. Live capital additionally requires **two-key arming** + the §10/M8 go-live
  checklist (broker dry-run, kill-switch drill, caps, runbook). **Two-key = key A** (committed, git-visible,
  code-reviewable config flag) **+ key B** (a runtime credential/token in `.secrets/` or operator env, **never
  committed**); the live broker must require **both, independently** — no single commit or process can supply
  both, and constructing a live-capable broker with only one key fails (tested).
- Paper-only is the default; canary tests (S1) fail if anything opens on the committed config.
- The broker is the position-of-record; the local journal is reconciled against it and never silently mutated;
  the modeled fill never overrides the broker ledger.
- Paper realism improves evidence quality but is **not** live-money proof.

## 13. Repo layout (mirrors Polymarket)

```
Stocks/
├── AGENTS.md · CLAUDE.md · PLAN.md · MEMORY.md
├── config/        risk_rules.json · agent_rules.json · data_retention.json
├── scripts/agent/ __main__ · orchestrator · strategy · candidate · scan_context
│                  paper_book · portfolio · execution_realism · fees · settlement · rehydrate
│                  broker/ (base · alpaca) · marketdata/ (base · databento)
│                  market_state · market_state_cache · corporate_actions
│                  features · gates · config · risk (intraday_margin · pdt_compat · locate)
│                  strategies/ (calibration_probe · ...)
├── scripts/recorder/  recorder · book_state · event · persistence · replay · reconcile · reconcile_runner
├── journal/   decisions · positions · fills · status · reconcile_alerts · data_quality_alerts (.jsonl)
├── data/      bars/ · snapshots/ · live/          # git-ignored
├── dashboard/ app.py                              # stdlib, 127.0.0.1
├── tests/     agent/ · recorder/ · lib/           # unittest + Fake/Flaky + canary
├── .secrets/  (git-ignored)                       # Alpaca + Databento keys
└── docs/superpowers/specs + plans · runbooks
```

## 14. Open questions and risks

- **Data quality is the load-bearing risk.** The data tier is a from-scratch rebuild; M1 is make-or-break.
- **Databento dataset choice (cost vs depth).** L1 composite is cheap but no depth; L2 MBP-10 adds depth-aware
  fills; L3 MBO adds true queue position at higher cost. Pin per milestone (§5.1). Polygon remains an alternate
  provider candidate behind `MarketDataTransport`, not a guaranteed drop-in replacement.
- **Regulatory regime in transition.** FINRA 26-10 intraday margin is canonical, but brokers phase in until
  Oct 2027 — so `LegacyPdtCompatMode` must mirror **Alpaca's actual** enforcement, detected from the broker,
  not assumed.
- **Alpaca paper fidelity.** Fills are idealized and paper does not simulate market impact, queue position,
  latency slippage, regulatory fees, liquidity-size checks, or dividends; broker-vs-modeled separation plus
  `broker_account_pnl`/`execution_realistic_pnl` split keeps reconciliation separate from strategy evidence. Our
  own gates (not paper enforcement) are authoritative for opening.
- **Calibration + backtest metric design.** The continuous-PnL calibration metric (e.g. information coefficient
  / forecast-vs-realized regression) and the anti-lookahead backtest fixtures are specified in M3/M7.
- **Short-selling timing.** If the first directional strategy is long-only, locate/SSR work (M4) can be scoped
  down accordingly.

## 15. References

- Engine/lifecycle: `scripts/auto_trader/{orchestrator,__main__,strategy,candidate,paper_book,portfolio,book_feed,fees,rehydrate}.py`
- Gates/config: `scripts/auto_trader/{gates,config}.py`; `config/{risk_rules,auto_trader_rules}.json`
- Status/cache pattern: `scripts/auto_trader/{market_status,market_status_cache,resolution_cache}.py`
- Recorder/replay: `scripts/clob_recorder/{recorder,book_state,event,persistence,replay,reconcile,reconcile_runner,token_ids,book_hash,lock,status}.py`; `config/data_retention.json`
- Snapshot/signal: `scripts/{pm_snapshot,equity_threshold_check}.py`
- Journal: `scripts/auto_trader/paper_positions.py`; `journal/decisions.jsonl`
- Dashboard: `dashboard/app.py`
- Execution-realism design: `docs/superpowers/plans/2026-06-03-execution-realistic-paper-mode.md`; `docs/REALISTIC_PAPER_TRADING_HANDOFF.md`
- External (verified 2026-06-08): FINRA Regulatory Notice 26-10 — https://www.finra.org/rules-guidance/notices/26-10 (intraday margin replaces PDT/$25k, eff. 2026-06-04); Databento EQUS.MINI docs — https://databento.com/docs/venues-and-datasets/equs-mini (L1 top-of-book composite); Databento schema docs — https://databento.com/docs/schemas-and-data-formats (MBO vs MBP-10 vs BBO semantics); Alpaca paper trading docs — https://docs.alpaca.markets/us/docs/paper-trading; Alpaca corporate actions docs — https://docs.alpaca.markets/us/reference/corporateactions-1.
