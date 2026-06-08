# Stocks Trading Agent — Design Spec

- **Date:** 2026-06-08
- **Status:** Approved design (pre-implementation)
- **Owner:** Robin
- **Sibling project:** `<sibling-workspace>` (the engineering spine this design ports)

> **TL;DR (DK):** Vi bygger en autonom aktie-handelsagent med samme `observe → paper → live`-disciplin som
> Polymarket-agenten. ~60–70 % af Polymarket-rygraden genbruges (engine, plugin-model, deterministisk journal,
> fail-closed gates, recorder/replay, dashboard, eksekverings-realisme). Det aktie-specifikke (broker, real-time
> data, markedskalender/halts, corporate actions, ordretyper, settlement/PDT/margin, kontinuerlig edge) bygges
> nyt. Stack: **Alpaca** (broker, paper≡live) + **Databento** (data, live≡historik), **hybrid fill-model**,
> kurateret single-name US large-cap, kalibrerings-probe først, live-penge-gate OFF bag two-key arming.

---

## 1. Executive summary

This project builds an autonomous **stocks/equities** trading agent that mirrors the discipline of Robin's
Polymarket workspace: it starts **paper-only with "nothing opens by default"** and is otherwise **fully
live-like** (live data, live order semantics, live-equivalent fill realism), so that going live is a
**gated config flip, not a rebuild**.

A deep review of the Polymarket codebase (9 subsystems mapped) found that roughly **60–70 % transfers** — and
the transferable portion is exactly the hard-won engineering spine, not the strategy alpha. What must be rebuilt
is everything prediction-market-shaped (binary 0/1 payoffs, arbitrage edge math, the UMA/Gamma resolution
oracle, the `condition_id`/token identity model) and the **entire live-data tier** (the one existing equity feed
was observed intermittent and bar-less).

The design is a **seven-tier layered architecture** that keeps the proven spine at the top (engine, Strategy
plugin model, deterministic journal, risk gates, dashboard) and concentrates the rebuild in the bottom (data)
and the settlement/risk middle. We start as an **observe-only calibration probe** on a bounded single-name
universe before any paper-eligible strategy, and well before any live canary.

The single biggest risk is **data quality**: the data tier is a from-scratch rebuild, and getting it right
(streaming NBBO + depth + bars, replay/reconcile, sequence-gap detection) is the precondition for the
execution-realism discipline to mean anything.

## 2. Goals and non-goals

### Goals
1. A long-running autonomous agent that ingests live equity market data, runs pluggable strategies, and opens /
   marks / closes **paper** positions with **live-equivalent fill realism**.
2. **Paper → live is a gate flip, not a rewrite.** Every seam (broker, data, fill model) is built so the live
   implementation is the same code path behind a flag.
3. Reuse the Polymarket spine wherever it is asset-agnostic; rebuild only what is genuinely equity-specific.
4. **Safety-first posture:** observe → paper → live, fail-closed, "nothing opens" by default, enforced by
   config-canary tests.
5. Evidence-grounded edge: a calibration loop proves a forecasting model against realized price moves before
   any capital (even paper) is risked on it.

### Non-goals (initial scope)
- Options, futures, FX, crypto, international markets. (Single-name **US equities** only at start.)
- High-frequency / sub-millisecond latency. (We target seconds-scale decision cadence.)
- Portfolio optimization across thousands of names. (Bounded curated universe.)
- Real money, until the calibration loop demonstrates realized edge **and** a separately-approved live gate is
  armed. Paper-only is the committed default.

## 3. Locked decisions

| # | Decision | Choice | Why (sustainability / minimal paper→live rebuild) |
|---|----------|--------|----------------------------------------------------|
| 1 | Broker / execution seam | **Alpaca** | Paper API ≡ live API (only base-URL + key differ) → go-live is a flip. Simple REST/WS, fractional shares. |
| 2 | Market-data vendor | **Databento** (primary) | Live and historical share one normalized schema → recorder/replay/backtest/live run identical code. True MBP-10 depth makes depth-VWAP fills honest. Metered, cheap on a small universe. `MarketData` abstraction keeps **Polygon** a drop-in swap. |
| 3 | Fill model | **Hybrid** | Alpaca paper drives order-lifecycle + account/PDT/buying-power realism; Databento depth drives fill-*price* realism via the second-quote gate. Same code validates Alpaca **live** fills later. |
| 4 | Asset universe | **Curated single-name US large-cap (~20–50)** | Keeps the data tier tractable; JSONL persistence holds; depth is affordable + high-fidelity on a focused set. Going broad later is a throughput change, not an architecture change. |
| 5 | First strategy | **Observe-only calibration probe** → then directional | Validates the model-vs-market edge and the whole data/execution pipeline before risking even paper capital. |
| 6 | Live posture | **Live-capable from day one, live-money gate OFF** | Build the broker live-seam, flatten-then-halt kill switch, and PDT/margin/locate gates as first-class from the start; flip to a tightly-capped live canary only after realized edge is shown, behind two-key arming. |

**Considered and rejected:** adopting an off-the-shelf framework (QuantConnect Lean, backtrader, zipline). These
are optimized for backtesting, not for the second-quote execution-realism discipline we require, and would be a
larger conceptual rebuild for live. Building on the proven Polymarket spine wins for our goals (full control +
live-true paper).

## 4. Architectural principles (carried from Polymarket)

These are asset-agnostic and adopted verbatim:

1. **Plugin protocol + per-tick read-only context.** A `Strategy` is a `runtime_checkable` Protocol
   (`name`, `paper_eligible`, `async scan(feed, ctx) -> list[Candidate]`). Everything a strategy needs is handed
   in via a frozen `ScanContext` built once per tick. Adding a strategy = drop a class in the list.
2. **Structural price-object gate.** The paper book refuses to fill against anything but a typed, fully-fillable,
   fresh, provenance-stamped quote object. A bare float or a discovery/REST price is structurally rejected.
   Discovery data may never become a fill price.
3. **Exact-integrated-notional cost basis, never `size * vwap`.** Cost = sum of integrated price×size walk;
   VWAP is reporting-only. `Decimal` throughout; float only at a single documented seam.
4. **Separation of mark source vs settlement source.** Marking uses a live conservative exit price; closing /
   settlement uses a distinct authoritative path.
5. **Fail-safe / fail-closed defaults everywhere.** Missing status → blocks new opens but never force-closes a
   held one; ambiguous → no action. The wrong action is always more expensive than no action.
6. **Append-only deterministic JSONL + rehydrate.** `json.dumps(sort_keys=True, separators=(",", ":"))`,
   Decimal-as-string, one write per row → byte-stable, hashable, replayable; restart rebuilds positions from the
   OPEN row (immutable facts) + LATEST row (evolving state).
7. **Fail-closed run gates + tighten-only config merge + `rules_hash` + a single `can_open()` chokepoint.**
   Identity-strict booleans (`is False`/`is True`), an `observe → paper → live` ladder AND-ed together, two-key
   arming, authoritative-config-may-only-tighten merge, SHA-256 provenance.
8. **Pluggable-transport + dual-hash replay/reconcile.** A duck-typed transport seam lets a `FakeTransport` /
   `FlakyTransport` drive the entire ingest/parse/persist pipeline deterministically offline; `raw_event_hash`
   (feed integrity) + derived-state hash (state-machine integrity) + replay re-derivation + external reconcile.
9. **Config-canary "nothing opens" tests.** Force every enable-gate ON and assert no order attempt / no position
   file / open-count 0 / no open decision rows. Re-targeted at the real order-submission seam.
10. **`plan → adversarial multi-lens review → harden → verify → live-validate`** for every increment.

### Paper-realism principles (the crux of "live-like")
1. **Never fill on the quote you decided on.** Re-quote a **second** fresh NBBO after a real awaited latency
   budget whose timestamp strictly post-dates the decision stamp; reject with an explicit reason if it would no
   longer fill at your size/limit.
2. **Model fillability against depth, not top-of-NBBO.** Walk available size (L2 depth) and compute a
   depth-integrated VWAP with explicit `worst_price = slippage`; only "fully filled" when displayed/estimated
   size covers the order.
3. **Fail-closed on unknown.** A stale/un-timestamped quote is unfillable; unknown tick/lot size rejects in
   strict mode; malformed config enables-but-rejects-all.
4. **Provenance-gated truth source.** Fills/marks accepted only from a typed quote carrying provenance +
   `book_hash` + timestamps (e.g. `databento_mbp10`).
5. **Two-class evidence separation, never collapsed.** *Optimistic* paper (filled at decision-time NBBO) vs
   *execution-realistic* paper (passed the second-quote re-validation), journaled and dashboarded as distinct
   counts/PnL.
6. **Enumerate machine-readable reject reasons**, ordered cheap/structural first (`unsupported_side`,
   `crossed_locked_nbbo`, `halt_luld_ssr_block`, `missing_quote`) then size/tick (`min_lot`,
   `reg_nms_increment`) then freshness/epoch/second-quote then economic (`latency_lost_edge`).
7. **Model real order semantics, not just FOK:** marketable-limit (live-safe market analogue), limit with
   resting/queue-position + partial fills, IOC/FOK; `latency_lost_edge` when NBBO trades through your limit
   while you waited.
8. **Don't trust a broker paper fill blindly.** Validate Alpaca's reported fill against an independent fresh
   Databento NBBO; flag any fill that beats NBBO as optimistic.
9. **Reconnect/epoch + session-stability gates.** Reject a fill if the feed `reconnect_epoch` changed between
   decision and re-quote, or the symbol entered halt/auction/LULD in the latency window.
10. **Calibrate latency/freshness budgets to the actual feed/broker**, not Polymarket's 500 ms.
11. **Paper realism is not live-money proof.** Keep the hard boundary; require a separate real-feed
   live-validation and a separately-approved real-money canary before any live capital.

## 5. System architecture — seven tiers

Each tier names the Polymarket module that seeds it. `reuse` = port with low change; `adapt` = reuse shape,
rebuild body; `new` = net-new.

### Tier 1 — Data / ingest
**Responsibility:** subscribe to streaming NBBO quotes, trade prints, and L2 depth; maintain per-symbol
`EquityBookState` + `TradeTape`; persist hash-stamped append-only events; maintain a bar cache
(1s/1m/daily) with resampling.

- **reuse:** `clob_recorder/recorder.py` single-writer loop + reconnect/backoff; `persistence.py` EventWriter
  (append-only, daily rotation, gzip, retention from config); `replay.py` + `reconcile.py` dual-hash
  data-quality harness; `lock.py` PID lock; `status.py` heartbeat; `FakeTransport`/`FlakyTransport` tests.
- **adapt:** `event.py` → parse Databento schemas (`mbp-10`, `tbbo`/`bbo`, `trades`, `ohlcv-1s`/`ohlcv-1m`,
  `status`, `definition`); `token_ids.py` → `instrument_id` ↔ `symbol`+MIC identity; reconcile ground-truth →
  Databento historical + Alpaca positions.
- **new:** **sequence-gap detection** (first-class equity signal — a gap is a dropped-message alert); the
  **bar cache** + resampler; entitlement/auth + heartbeat handling for the vendor WS.
- **Key types:** `MarketDataTransport` (Protocol: `async stream(symbols) -> AsyncIterator[bytes/str]`),
  `EquityBookState`, `TradeTape`, `BarCache`, `Quote` (typed, provenance + `book_hash` + monotonic+wall ts).

### Tier 2 — Market-state
**Responsibility:** decide per-symbol tradability state and apply corporate actions.

- **reuse:** `market_status.py` pure-decider behind an injectable fetcher seam; `market_status_cache.py`
  freshness-gated non-blocking cache that degrades to a safe default when stale; tighten-only severity merge;
  union open-position symbols into the refresh set.
- **adapt:** decider body → a session state machine `{pre, rth, post, closed, auction, halted}` plus
  `halt/LULD/SSR` flags, driven by a market calendar (`exchange_calendars` / `pandas-market-calendars`) and
  vendor/broker status messages; replace the boolean orderbook flag with a live two-sided-NBBO check.
- **new:** **corporate-actions** subsystem — consume Alpaca's corporate-actions feed (splits, reverse-splits,
  dividends, ticker changes, mergers, spinoffs, delistings); emit explicit `corporate_action` adjustment-event
  rows; a cost-basis/quantity adjuster in the position store; durable instrument identity (CUSIP/FIGI) under
  unstable tickers.
- **Key types:** `SessionState`, `TradabilityDecider` (pure), `MarketStateCache`, `CorporateActionFeed`,
  `AdjustmentEvent`.

### Tier 3 — Snapshot / signal
**Responsibility:** quote-quality filtering + the feature/indicator engine that produces signals.

- **reuse:** `pm_snapshot.summarize_book()` depth/spread math + warnings-as-data → quote-quality filters
  (re-expressed in **bps**, not 0..1 prediction cents); the atomic-snapshot pattern; public-API-with-fallback
  fetch discipline.
- **new:** a **feature/indicator engine** fed by the bar cache, holding **rolling time-series state outside the
  stateless `scan()` hot path**, refreshed on a background cadence (mirroring how discovery refreshes today):
  moving averages, RSI, z-scores, realized-vol estimates, and forecast features.
- **Key types:** `FeatureEngine` (rolling state, background-refreshed), `SignalSnapshot`, `QuoteQuality`.

### Tier 4 — Strategy
**Responsibility:** plugins that emit `Candidate`s.

- **reuse directly:** `strategy.py` `Strategy` Protocol + frozen `ScanContext`; `candidate.py` `Candidate`/`Leg`
  (single-leg is the N=1 case); the observe-only probe template (`paper_eligible=False`, `scan()` returns `[]`);
  the two-tier edge discipline (cheap pre-filter signal vs binding realized fill-aware figure) and the
  `journal_deduped` staging (initial-state + material-change rows, not a per-tick firehose).
- **new:** the **continuous edge/sizing function** — expected-return / signal-z-score / forecast-vs-price gap
  instead of `net_surplus_pp`; vol-target / Kelly sizing feeding `Candidate` leg size; a continuous-PnL
  calibration loop (settle forecasts against realized price moves, not a binary Brier score). The first concrete
  strategies: a **calibration probe** (then a directional momentum/mean-reversion strategy; an event/threshold
  probe — "will AAPL close above X" — as an easy early calibration target reusing the lognormal probe).
- **Do NOT port:** `arb_strategy.evaluate_bundle` / `net_surplus_pp` (no guaranteed-payoff constraint in
  equities). Only the staging/dedup scaffolding survives.

### Tier 5 — Risk / gates
**Responsibility:** fail-closed run gates + a single pre-trade `can_open()` chokepoint enforcing exposure caps.

- **reuse:** `gates.py` identity-strict run-gate ladder; `config.py` tighten-only merge + `rules_hash`;
  `portfolio.can_open()` chokepoint structure (per-instrument / per-theme / bankroll → renamed
  `market_id→symbol`, `theme→sector/factor`).
- **new sub-gates:** PDT day-trade ledger (rolling day-trade count over 5 business days; block the 4th when
  account equity < \$25k); buying-power / Reg-T initial & maintenance margin (intraday vs overnight); short-sale
  locate/borrow availability + borrow cost + SSR uptick handling; gross & net leverage; per-sector / per-factor
  / beta exposure caps; a **max-drawdown / daily-loss kill switch** as a first-class halt condition.
- **Behavior change:** on a kill-switch / live-gate flip, prefer **flatten-then-halt** over freeze-with-open-risk
  (a frozen halt leaves live market exposure unmanaged).
- **Key types:** `RunGates`, `RiskRules` (hashed), `can_open(candidate, portfolio, account) -> Verdict`,
  `KillSwitch`, `PdtLedger`, `MarginModel`, `LocateCheck`.

### Tier 6 — Paper execution (hybrid)
**Responsibility:** the open → mark → close lifecycle with live-equivalent fill realism.

- **reuse:** `paper_book.py` lifecycle shape (`try_open → mark → close`), the typed-price-object FOK fill loop,
  exact-integrated-notional Decimal cost basis, per-position fee assumption stamped on rows;
  `execution_realism.py` second-quote re-validation gate; `fees.py` FeeModel (`rate*notional`, `from_config`,
  replayable `assumption()` stamp).
- **adapt:** binary 0/1 payoff math → **continuous close-at-price** (`realized = qty*exit - cost - fee` for a
  long); add a **short path** (sell-to-open, mark at best_ask, inverted sign) since `try_open` hard-rejects
  non-buy legs today; `resolution.py` **replaced** by a settlement / corporate-action / exit engine (the
  terminal-state *pattern* survives — delisting/expiry is terminal — but the oracle is net-new); FeeModel +=
  SEC/TAF/FINRA regulatory fees, maker/taker, and borrow cost for shorts.
- **new:** the **`Broker` abstraction** (`AlpacaPaperBroker` / `AlpacaLiveBroker` behind one interface) with a
  hard kill switch; **partial-fill modeling** + limit-order resting/queue position; order intent encoded
  explicitly (`order_type` / `tif` / `limit_price` / `qty`); a separate **fills stream**
  (`fill_price` / `avg_price` / `filled_qty` / liquidity flag / venue); Reg-NMS price-dependent sub-penny ticks.
- **Hybrid wiring:** decision → await latency budget → re-quote fresh Databento depth → (a) execution-realism
  gate computes honest fill price + partial fills against depth; (b) Alpaca paper order gives authentic
  order-lifecycle + account/PDT/buying-power state; cross-check the Alpaca fill vs the independent Databento
  NBBO and flag/override optimistic fills. We **do not** rely on Alpaca paper to enforce margin/PDT/locate — our
  own `can_open()` sub-gates are authoritative.
- **Key types:** `Broker` (Protocol), `PaperBook`, `OrderIntent`, `Fill`, `SettlementEngine`.

### Tier 7 — Journal / reconcile
**Responsibility:** event-sourced durable ledger + restart rehydration + broker reconciliation.

- **reuse:** `paper_positions.py` append-only deterministic JSONL writer + `rehydrate.py` (verbatim; Decimal-as-
  string; schema field vocabulary changes but the two-stream event-sourced shape + rehydrate contract are
  unchanged); idempotent-by-foreign-key resolution; first-class `do_nothing` rows.
- **new:** a **fills/settlement stream** joined to positions; a **SOD/EOD broker reconciliation** built on the
  `reconcile_runner.py` structure (replay local state, diff against Alpaca positions/account as ground truth,
  write status + alerts, non-zero exit on mismatch); on conflict treat the broker as truth and emit a
  `reconcile` adjustment row rather than silently mutating.

### Dashboard / observability
- **reuse:** `dashboard/app.py` `ThreadingHTTPServer` bound to 127.0.0.1; `_safe_workspace_path()`
  canonicalize-then-contain sandbox; `html.escape` + restrictive CSP + `MAX_FILE_BYTES`; the pure
  `_capital_summary()` / `_to_decimal()` money roll-up (unit-tested without a server).
- **adapt:** the ~40 domain renderer bodies → tickers / shares / fills / NBBO / sector exposure; `caps_used`
  bundle-counting → equity risk dimensions (gross/net/sector/beta/drawdown).
- **guard:** if it ever exposes live order actions or leaves localhost, add auth/CSRF/TLS.

## 6. The hybrid execution-realism fill model

```
decision (t0, Databento quote A, signal+forecast)
        │
        ▼  await latency_budget_ms (calibrated to feed+broker)
RE-QUOTE (t1 > t0): fresh Databento NBBO + MBP-10 depth, require monotonic_seen_at_ms(t1) > decision_stamp(t0)
        │
        ├──────────────────────────────────────────────┬───────────────────────────────────────────┐
        ▼                                                ▼                                             ▼
[execution-realism gate]                       [Alpaca paper order]                         [reconnect/epoch gate]
 depth-VWAP fill at quote B size                 lifecycle: ack / partial / cancel / TIF       reject if reconnect_epoch
 partial-fill modeling (queue/resting)           account: buying-power / PDT as live            changed or symbol entered
 Reg-NMS tick validation                         (fills are idealized → must be cross-checked)  halt/auction/LULD in window
 enumerated reject reasons
 reject if fill beats NBBO or under-size
        └──────────────────────────────────────────────┴───────────────────────────────────────────┘
                                                  │
                       honest fill PRICE (Databento depth) + authentic order STATE (Alpaca)
                                                  │
                journal: realism_class ∈ {optimistic, execution_realistic} — never collapsed
```

Going live: `AlpacaPaperBroker` → `AlpacaLiveBroker` (same interface, same gate). The gate switches from
"validate paper fill" to "validate live fill" — no rebuild.

## 7. Data model — event-sourced JSONL

All rows: `json.dumps(sort_keys=True, separators=(",", ":"))`, Decimal-as-string, one write per line, with a
row hash. Streams (separate files): `journal/decisions.jsonl`, `journal/positions.jsonl`,
`journal/fills.jsonl`, `journal/reconcile_alerts.jsonl`, `data_quality_alerts.jsonl`.

Event types (illustrative fields):
- `decision` — `ts`, `symbol`, `strategy`, `action ∈ {do_nothing, would_open}`, `forecast`, `edge`,
  `signal_provenance`, `quote_provenance`.
- `paper_open` — `position_id`, `symbol`, `side ∈ {long, short}`, `qty`, `order_intent {order_type, tif,
  limit_price}`, `fill_price`, `vwap`, `cost_usd`, `slippage`, `fee_assumption`, `quote {provenance, book_hash,
  seen_at_ms, reconnect_epoch}`, `realism_class`.
- `mark` — `position_id`, `mark_price`, `mark_source ∈ {best_bid, best_ask}`, `unrealized_pnl`, `ts`.
- `fill` / `partial_fill` — `order_id`, `filled_qty`, `avg_price`, `liquidity_flag`, `venue`, `ts`.
- `corporate_action` — `symbol`, `type`, `factor`, `adjusted_qty`, `adjusted_cost_basis`, `ts`.
- `close` — `position_id`, `exit_price`, `realized_pnl`, `fees`, `reason`, `ts`.
- `reject` — `symbol`, `reason` (enumerated), `stage`, `ts`.
- `reconcile` — `symbol`, `local`, `broker`, `diff`, `action`, `ts`.

Rehydrate: OPEN row = immutable facts; LATEST row per `position_id` = evolving state; `close`/terminal = skip.

## 8. Stock-specific subsystems (the must-builds)

1. **Broker execution abstraction** (Alpaca paper + live) with async fills, order submit/cancel,
   positions/account queries, and a hard live kill switch. The config-canary tests target this real
   order-submission seam.
2. **Real-time market-data feed** (Databento): streaming NBBO + trade prints + MBP-10 depth + bars, with
   sequence/heartbeat handling, reconnect, and entitlement/auth; behind the recorder's transport seam.
3. **Market calendar + session/halt/LULD/SSR/auction gate** (Tier 2) with session-aware gap detection (a quiet
   symbol at 02:00 ET is not a dropped feed).
4. **Corporate-action handling** (Tier 2): splits/dividends/ticker-changes/mergers/spinoffs/delistings mutate
   positions; emit adjustment events; durable CUSIP/FIGI identity.
5. **Order types + TIF + partial fills + slippage** (Tier 6): market / marketable-limit / limit / stop / IOC /
   FOK; genuine partial fills + resting/queue position.
6. **Settlement / PDT / margin / short-locate / regulatory-fee layer** (Tier 5/6): T+1 settlement; PDT ledger;
   Reg-T margin + buying power; short locate/borrow + SSR; SEC/TAF/FINRA fees; max-drawdown/daily-loss kill.
7. **Position reconciliation vs broker** (Tier 7): SOD/EOD diff vs Alpaca; broker is ground truth.
8. **Continuous edge/sizing/risk model** (Tier 3/4): expected-return / z-score / forecast-gap edge; vol-target /
   Kelly sizing; VaR/CVaR/Sharpe/beta risk metrics; continuous calibration loop.

## 9. Testing and quality strategy

- **Deterministic offline harness:** `FakeTransport` / `FlakyTransport` drive the full ingest/parse/persist
  pipeline; injected clocks; spy doubles to prove negatives. Plain `python3 -m unittest discover`.
- **Dual-hash replay/reconcile:** `raw_event_hash` (feed integrity) + derived-state hash (state-machine
  integrity); replay re-derives state and checks hashes; reconcile diffs against Databento historical + Alpaca
  positions.
- **Config-canary "nothing opens" tests:** force every enable-gate ON; assert no order attempt, no position
  file, open-count 0, no open decision rows — on the **committed** config. (Red-by-design only when the
  working-tree config is explicitly armed.)
- **Process:** `plan → adversarial multi-lens review → harden → verify-self → live-validate` per increment.
  Live validation against the real feed catches what unit tests miss (e.g. journal firehoses).

## 10. Phased roadmap (each milestone gets its own spec → plan → review → verify)

| Milestone | Deliverable | Acceptance criteria |
|-----------|-------------|---------------------|
| **M0 — Skeleton + abstractions** | Repo layout; `Broker` iface (Alpaca paper impl); `MarketData` iface (Databento adapter); deterministic journal + Decimal-as-string; config/gates + `rules_hash`; stdlib dashboard stub; canary tests. | Canary tests pass (nothing opens); journal round-trips + rehydrates; gates fail-closed; live gate OFF in committed config. |
| **M1 — Data tier** | Databento recorder behind transport seam; replay/reconcile; bar cache; market calendar + session gate. | Replay re-derives state with matching hashes; reconcile vs Databento historical passes; sequence-gap detection fires on injected gaps; Fake/Flaky tests green. |
| **M2 — Market-state** | Session/halt/LULD/SSR/auction state machine; corporate-actions adjustment events. | Decider unit-tested across session windows + halts; a synthetic split adjusts a held position's qty + cost basis correctly. |
| **M3 — Signal + calibration probe** | Feature/indicator engine; observe-only calibration probe (`paper_eligible=False`). | Probe logs forecasts + realized-move scoring; **opens nothing**; calibration report renders in dashboard. |
| **M4 — Paper-exec hybrid** | Alpaca paper + Databento second-quote gate; partial fills; order types; first paper-eligible directional strategy. | Optimistic vs execution-realistic paper journaled separately; second-quote rejects logged with enumerated reasons; first paper position opens/marks/closes with realized PnL. |
| **M5 — Risk + reconcile** | PDT/margin/buying-power/locate/drawdown kill switch; SOD/EOD broker reconciliation. | `can_open()` rejects on each new sub-gate in tests; kill switch flattens-then-halts; reconciliation flags an injected drift and exits non-zero. |
| **M6 — Live canary** | (Only after realized edge.) Tightly-capped single-name live canary behind two-key arming + flatten-then-halt. | Separately-approved live gate; canary caps enforced; live-validation runbook + real-money boundary documented. |

## 11. Repo layout (mirrors Polymarket)

```
Stocks/
├── AGENTS.md · CLAUDE.md · PLAN.md · MEMORY.md          # charter + hard boundaries + session-startup order
├── config/
│   ├── risk_rules.json          # sizing + structural risk gates; live_trading.enabled=false
│   ├── agent_rules.json         # run gates (enabled=false; paper_trading.enabled=false), universe, latency budgets
│   └── data_retention.json      # recorder paths + rotation/retention
├── scripts/agent/
│   ├── __main__.py · orchestrator.py · strategy.py · candidate.py · scan_context.py
│   ├── paper_book.py · portfolio.py · execution_realism.py · fees.py · settlement.py · rehydrate.py
│   ├── broker/      (base.py · alpaca.py)         # Broker abstraction (paper/live)
│   ├── marketdata/  (base.py · databento.py)      # MarketData abstraction (Databento; Polygon swap)
│   ├── market_state.py · market_state_cache.py · corporate_actions.py
│   ├── features.py · gates.py · config.py · risk.py
│   └── strategies/  (calibration_probe.py · ...)
├── scripts/recorder/   recorder.py · book_state.py · event.py · persistence.py · replay.py · reconcile.py · reconcile_runner.py
├── journal/    decisions.jsonl · positions.jsonl · fills.jsonl · reconcile_alerts.jsonl · data_quality_alerts.jsonl
├── data/       bars/ · snapshots/ · live/        # git-ignored runtime captures
├── dashboard/  app.py                            # stdlib only, 127.0.0.1
├── tests/      agent/ · recorder/ · lib/         # unittest + Fake/Flaky transports + canary
├── .secrets/   (git-ignored)                     # Alpaca + Databento keys
└── docs/superpowers/specs + plans · runbooks
```

## 12. Hard boundaries (committed default)

- `config/risk_rules.json → live_trading.enabled = false` **always** until an explicit, separately-approved go.
- `config/agent_rules.json → enabled = false` and `paper_trading.enabled = false` are the run gates; they stay
  `false` on the committed config. Live capital additionally requires **two-key arming**.
- Paper-only is the default; canary tests fail if anything opens on the committed config.
- Secrets live in `.secrets/`, never committed.
- The broker is ground truth; the local journal is reconciled against it and never silently mutated.
- Paper realism improves evidence quality but is **not** live-money proof; a separate real-feed live-validation
  and an approved real-money canary precede any live capital.

## 13. Open questions and risks

- **Data quality is the load-bearing risk.** The entire data tier is a from-scratch rebuild; if streaming /
  depth / bars / reconcile / gap-detection are not solid, the execution-realism discipline is meaningless. M1 is
  the make-or-break milestone.
- **Databento cost at scale.** Metered pricing is cheap on ~20–50 names; revisit if the universe broadens (the
  `MarketData` abstraction keeps Polygon a flat-fee swap).
- **Alpaca paper fill fidelity.** Alpaca paper fills are idealized; the hybrid model's Databento cross-check is
  essential, and Alpaca's margin/short-locate realism in paper is approximate — our own gates are authoritative.
- **Calibration metric design.** The continuous-PnL calibration loop (forecast vs realized move) needs a sound
  scoring metric (e.g. information coefficient / forecast-vs-realized regression) — to be specified in M3.
- **Short-selling timing.** Whether to pull short-locate/SSR forward depends on the first directional strategy;
  if it is long-only, M5's locate work can be deferred.

## 14. References (Polymarket source modules to port)

- Engine/lifecycle: `scripts/auto_trader/{orchestrator,__main__,strategy,candidate,paper_book,portfolio,book_feed,fees,rehydrate}.py`
- Gates/config: `scripts/auto_trader/{gates,config}.py`; `config/{risk_rules,auto_trader_rules}.json`
- Status/cache pattern: `scripts/auto_trader/{market_status,market_status_cache,resolution_cache}.py`
- Recorder/replay: `scripts/clob_recorder/{recorder,book_state,event,persistence,replay,reconcile,reconcile_runner,token_ids,lock,status}.py`; `config/data_retention.json`
- Snapshot/signal: `scripts/{pm_snapshot,equity_threshold_check}.py`
- Journal: `scripts/auto_trader/paper_positions.py`; `journal/decisions.jsonl`
- Dashboard: `dashboard/app.py`
- Execution-realism design: `docs/superpowers/plans/2026-06-03-execution-realistic-paper-mode.md`; `docs/REALISTIC_PAPER_TRADING_HANDOFF.md`
- Agent design: `docs/AUTONOMOUS_PAPER_AGENT_SPEC.md`; `docs/AUTONOMOUS_BOT_SPEC.md`
