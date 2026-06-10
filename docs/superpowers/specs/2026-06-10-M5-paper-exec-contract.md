# M5 (Paper-exec hybrid) — FROZEN CONTRACT (DRAFT FOR CRITIC PASS)

> **Status:** DRAFT FOR CRITIC PASS, 2026-06-10. Synthesized from THREE independent architect designs
> (execution-correctness / fail-closed-safety / integration-runnability lenses) around Robin's locked
> decisions LD-M5-1…10 (restated as FD-M5-1…10 below; every cross-design disagreement is resolved
> explicitly in §1 with a one-line rationale). Mirrors the M4 contract
> (`2026-06-09-M4-risk-core-contract.md`, rev 2) and the M3 contract in granularity: module-by-module
> APIs, code skeletons, frozen vocabularies, journal row shapes, deterministic ids, fixtures, and a
> test→invariant map. A build agent TDDs from this without relitigating.
>
> **Branch:** `m3-signal` @ `f9ec7c6` + the frozen M4 contract treated as the real API (the M4 build is
> in flight under `scripts/agent/risk/` — this contract relies on the M4 CONTRACT, never on in-flight
> file contents). Baseline suite: 700 tests green at `f9ec7c6` (M4 build adds its own).

## 0. Scope, ground rules, verified facts

**In scope (parent §5 Tier 6, §6, §10 M5):** rebuild `mint_open_token` (full S4 ladder; registry/token
mechanics untouched); grow `broker/base.py` (Protocol + `require_token` open-branch tighten) and
`broker/alpaca.py` (three-mode ctor, wire payloads, lazy SDK); new modules `exec_reasons.py`,
`execution_config.py`, `order_pricing.py`, `execution_realism.py`, `fees.py`, `paper_book.py`,
`exec_ledger.py`, `backtest_gate.py`, `orchestrator.py`, `run_lock.py`, `secrets_runtime.py`,
`__main__.py`, `broker/order_state.py`, `broker/fake.py`, `broker/flatten_proxy.py`,
`strategies/synthetic.py`, `marketdata/replay_feed.py`; three new journal streams
(`orders/fills/positions.jsonl`); `config/agent_rules.json` `execution` block; `requirements.txt`
`alpaca-py` pin (uncommented, FD-M5-5); fixtures + tests per §Q/§R. **Out of scope (owners in §T):**
live realtime feed (M1-2b), SOD/EOD reconcile diff (M6), retry/replace + websocket fills + multi-order
concurrency (M6/M7), backtest-artifact *production* + sizing (M7), extended hours / GTC / fractional /
shorts (post-M5 / short milestone), `AlpacaLiveBroker` + two-key consumption (M8).

**Ground rules (unchanged from M0–M4):**

- Committed gates stay OFF: `agent_rules.enabled=false`, `paper_trading.enabled=false`,
  `live_trading.enabled=false`. M5 flips **no** committed gate. The ONE new gate-reading surface is the
  git-ignored `.secrets/run_gates.json` (FD-M5-2), consulted ONLY for the two run-gate keys, ONLY in
  paper mode — own section §O.2, own tests (§R 13). Absent file ⇒ gates read False ⇒ reject-all.
- Offline suite stays stdlib-only on a bare checkout. `alpaca-py` is pinned exact and UNCOMMENTED in
  `requirements.txt` (FD-M5-5, the `databento==0.79.0` precedent), installed only into `.venv`, imported
  ONLY inside `AlpacaPaperBroker._build_real_client()` — a path no offline test reaches. The AST
  module-scope import guard and the `sys.modules` purity test extend their forbidden set with `alpaca`.
- Tests make no network calls and read no credentials; `.secrets/` is never read by the suite (loaders
  take injected fixture paths).
- Determinism: canonical `dumps()` (`serializer.py:47-50`), Decimal-as-string, per-row sha256
  (`serializer.py:53-55`), per-stream monotonic `seq` under a shared per-path lock
  (`journal.py:62-129`), injected clocks only — **no M5 module reads a wall clock or sleeps**; the
  latency wait is an orchestrator scheduler item over the injected ms clock (FD-M5-10).
- All market logic in ET; persisted timestamps UTC ISO-8601; monotonic ms for staleness; strict-`>`
  staleness boundaries everywhere (the `market_state_cache.py:74-99` form).
- **Fail-closed asymmetry (M4 FD-M4-3, preserved structurally):** no M5 code path may BLOCK a
  reduction. The reduce path gains zero new gates: `mint_reduce_only_token` is not edited,
  `require_token`'s reduce branch is byte-identical, and `consume()`'s new runtime re-checks apply to
  `kind=="open"` authorizations ONLY. The single accepted reduce-side failure mode is FD-M5-1's
  pricing rule: a flatten/close order that cannot be priced is an isolated per-position FAILURE
  (`failed[]`/`residual`, retried) with a journaled reason — never a gate, and never a bare market
  order.
- The broker is the position-of-record. `PaperBook` is journal/evaluation state; on any divergence the
  broker number wins (M6 reconciles and emits adjustment rows); the modeled fill is a label and never
  back-voids or overrides a `broker_fill` row.

### 0.1 Verified repo facts this contract builds on (file:line at `f9ec7c6`)

| Fact | Source |
|---|---|
| `mint_open_token(config, intent)` is reject-all (raises `PreflightRejected`, never issues an authorization); the module-private registry (`_MINT`, `_authorizations`), `_Authorization(kind, symbol, side, qty)`, `_issue`, `authorization_of`, `consume` are the non-forgeable capability core M5 rebuilds AROUND, not replaces | `scripts/agent/execution_preflight.py:20-94` |
| `mint_reduce_only_token` validates the HELD position's sign/size/symbol (never the caller's flag); qty `>0` and `<= held` ("may flatten, never flip") — NOT edited in M5 | `scripts/agent/execution_preflight.py:97-118` |
| Tokens are non-constructible/copyable/picklable; `consume` deletes the nonce entry (single-use) | `scripts/agent/execution_preflight.py:40-87` |
| `require_token` open branch validates **symbol only** today (reduce binds side+qty) — M5 tightens the open branch to bind side+qty+limit_price | `scripts/agent/broker/base.py:43-56` |
| `OrderIntent` already carries `order_type="marketable_limit"`, `tif="day"`, `limit_price`, `is_reducing`, `intent_id`; validates qty positive finite Decimal, limit finite-or-None | `scripts/agent/broker/base.py:17-34` |
| `BrokerBase.submit_order` = `require_token` then `_place` — the non-bypassable chokepoint; concrete brokers implement `_place` only; `Broker` Protocol = `submit_order/positions/account` | `scripts/agent/broker/base.py:59-87` |
| M0 `AlpacaPaperBroker` is a spy/no-op: no SDK import, records to `self.submitted`, returns `{"order_id", "status":"accepted_paper_stub"}`; `account()` returns a 2-key Decimal dict | `scripts/agent/broker/alpaca.py:13-26` |
| M0 `KillSwitch.trigger`: reduce-only flatten per position, per-position failure isolation into `failed[]`, `finally`-guaranteed `halted`; flatten intents are built with **`limit_price=None`** — the gap FD-M5-1's proxy closes | `scripts/agent/kill_switch.py:12-53` (intents at 34-40, mint at 41) |
| Run gates are identity-strict; `opening_allowed(config)` requires BOTH `agent_rules.enabled` and `agent_rules.paper_trading.enabled` | `scripts/agent/gates.py:10-26` |
| `tighten_only_merge`: bools AND / non-bool numerics `min()` / dicts recurse / anything else keeps base / overlay-only keys dropped; `rules_hash()` canonical, `allow_nan=False` | `scripts/agent/config.py:17-43` |
| `BrokerUSD`/`ModeledUSD` distinct Decimal newtypes; `as_broker_usd` guard raises `TypeError` on a non-BrokerUSD; serializer rejects float and non-finite Decimal | `scripts/agent/serializer.py:15-64` |
| Journal `_RESERVED={event_type,run_id,seq,hash,decision_id,order_id,ts_utc}`; `decision_id`/`order_id` ride kwargs; truncated-tail-only drop, complete corrupt line ⇒ `JournalCorruption`; per-resolved-path shared seq+lock; injectable row clock | `scripts/agent/journal.py:21,28-59,88-129` |
| `EventWriter` wraps `JournalWriter` verbatim; `record(event_type, fields, *, decision_id, order_id)`; `replay_stream` delegates to `journal.replay` | `scripts/recorder/persistence.py:52-108` |
| `QuoteSnapshot` carries `bid/ask/bid_sz/ask_sz`, `ts_event_utc`, `ts_recv_utc`, `seen_at_ms`, `reconnect_epoch`, `vendor_seq`, `dataset`, `schema`; `quote_quality.evaluate` collect-all reason vocab `{quote_stale, quote_crossed, quote_locked, quote_one_sided, quote_nonfinite, quote_nonpositive, spread_too_wide}`; `QuoteVerdict.mid` quantized `MID_QUANTUM=Decimal("0.000001")`, `BPS_QUANTUM=Decimal("0.01")`; pinned `_DECIMAL_CTX(prec=28, ROUND_HALF_EVEN)` | `scripts/agent/quote_quality.py:19-23,26-50,61-116` |
| `Candidate`/`Leg` pure types: closed `SIDES={"buy","sell"}`, qty positive finite Decimal, limit finite-or-None, legs non-empty, NO order authority | `scripts/agent/candidate.py:12-57` |
| `Strategy` Protocol (`strategy_id`, `scan(ctx) -> Sequence[Candidate]`) + frozen `ScanContext{snapshot, rules_hash, now_ms}` — first consumed in M5 | `scripts/agent/strategy.py:14-25` |
| M3 `DecisionLedger` freezes `ACTIONS={do_nothing, forecast_only}`, a frozen `_ROW_FIELDS` set, and **raises** on `paper_eligible is not False` — M5 may NOT write `would_open` there (FD-M5-9) | `scripts/agent/strategies/calibration_probe.py:31,34-41,59-78` |
| `CalibrationProbe` ctor seams (`config, calendar, market_state_cache, feature_view, quote_view, ledger, climatology, run_id, clock`) + `on_bar_complete(...)`; `QuoteView` Protocol (`latest(symbol, instrument_id)`) | `scripts/agent/strategies/calibration_probe.py:94-117,214` |
| Resolver: `resolve_due(decision_rows, *, now_utc)`; `AsOfClimatology` | `scripts/agent/calibration.py:52,189-195` |
| `MidBar.watermark_utc` (FD-2 anti-lookahead), `resample_midbars(quote_rows, ...)`, `MidBarSeriesReader` as-of/watermark-gated reads | `scripts/agent/bar_series.py:39-49,103,203,275-292` |
| `FeatureView.refresh(symbol, instrument_id, as_of_utc)` → `FeatureSnapshot` | `scripts/agent/feature_engine.py:51,183-192` |
| M2 `Verdict{symbol,instrument_id,session_state,tradability,halt,luld,ssr,two_sided_nbbo,short_allowed,reasons,ca_blackout,session_date_et}`; `SessionState.RTH/AUCTION`; `Tradability` closed vocab; `MarketStateError` on out-of-vocab | `scripts/agent/market_state.py:29-37,77-99,185-209` |
| `MarketStateCache.get` strict-`>` TTL, stale/missing ⇒ `safe_default_verdict` with `reasons=("cache_stale_safe_default",)`; `DEFAULT_FRESHNESS_TTL_MS=2000`; ctor clamp shorten-only | `scripts/agent/market_state_cache.py:34,52-99` |
| Calendar seam: `ScheduleProvider` Protocol, `FixtureScheduleProvider` raises `UnknownSessionDate` out-of-coverage, `MarketCalendar.phase_at/session_date_for/schedule_for/calendar_pin` | `scripts/agent/market_calendar.py:56-85,167-225,261-285` |
| The lazy-SDK pattern to mirror: injected `raw_source` ⇒ offline; else `_build_real_client()` which alone may `import databento` and read `.secrets/` | `scripts/agent/marketdata/databento.py:31-102` |
| Two-key arming seam: `two_key_armed` (key A committed flag, key B runtime secret), `construct_live_broker` raises `ArmingError`/`NotImplementedError` — untouched in M5 | `scripts/agent/arming.py` |
| Recorder event rows: quote rows carry `bid_px/bid_sz/ask_px/ask_sz` + provenance prefix (`schema, dataset, instrument_id, symbol, vendor_seq, ts_event_utc, ts_recv_utc, reconnect_epoch`); depth rows carry `bids/asks` level lists + `derived_book_hash`; `EquityBookState.apply/apply_quote/snapshot`, `book_hash(snapshot)` | `scripts/recorder/event_row.py:34-101`, `scripts/recorder/book_state.py:49-119`, `scripts/recorder/book_hash.py:103` |
| Committed offline data fixtures: `equs_mini_tbbo_sample.jsonl` (L1 tbbo), `mbp10_depth_sample.jsonl` (L2 depth), sub-dollar sample, out-of-order/gap samples | `tests/fixtures/databento/` |
| Committed calendar fixtures: `nyse_2026_schedule.json` (8 scattered dates) + `nyse_margin_window_v1.json` (**2026-06-01…07-31 contiguous** — covers the 2026-06-09 tbbo fixture dates; M4 §L) | `tests/fixtures/calendar/` |
| `agent_rules.json`: gates false; `universe.symbols:[]`; **`latency_budget_ms: 250` is a top-level JSON int — `min()`-merge polarity is INVERTED for it** (lowering it loosens realism) ⇒ code floor (FD-M5-10); `signal.refresh_cadence_ms="1000"`, `signal.quote_staleness_ms_max="2000"`, `signal.spread_bps_max="50"` | `config/agent_rules.json:2-10,24-27` |
| `risk_rules.json` already carries the committed M4 additions: 7 integer-0 caps + `risk.short_selling.enabled:false` + `risk.universe:{}` — M5 does NOT edit this file (fees are code constants, FD-M5-15) | `config/risk_rules.json:1-19` |
| `requirements.txt`: `databento==0.79.0` uncommented + the header note reserving the M5 `alpaca-py` pin | `requirements.txt:1-8` |
| Test doubles: `FakeClock(now_ms/advance)`; `SpyBroker` records every attempt at entry (`self.calls`) then `require_token` | `tests/lib/fakes.py:83-114` |
| Canary + purity test patterns to EXTEND (never duplicate): committed-config canary at the broker boundary; socket-block + `sys.modules` purity + AST module-scope import guard | `tests/agent/test_config_canary.py:23-64`, `tests/agent/test_no_network_no_creds.py:7-166` |
| M4 build IN FLIGHT under `scripts/agent/risk/` (`reasons.py`, `account_state.py` exist at HEAD) — this contract binds to the M4 CONTRACT API only | `scripts/agent/risk/` (not relied on) |

**M4 contract API consumed here (authority: `2026-06-09-M4-risk-core-contract.md`):**
`RiskEngine.can_open(candidate, portfolio, account, *, market_state, marks, kill_state, kill_generation,
margin_read, pdt_read, loss_read, now_ms, decision_id) -> RiskVerdict{allowed, reasons, gate_stage,
stages_skipped, strategy_id, legs, gross_notional, caps_used, account_snapshot_id, kill_state,
kill_generation, session_date_et, rules_hash, verdict_id}` (§J); `RiskLedger.record_risk_verdict` +
`record_account_snapshot` (F4 obligation on every `AccountStore.put`) (§K); `AccountStore.put/get/
latest_unsafe`, `parse_account_payload`, `parse_positions_payload`, `portfolio_is_stale`,
`AccountReadProvider` Protocol, `ACCOUNT_SOURCES ⊇ {"alpaca_paper","fixture","spy"}` (§B);
`RiskKillSwitch.state/generation/evaluate/trigger/retry_residual/rehydrate` with keyword-only annotation
inputs (§I); `IntradayMarginModel.observe/close_of_day/rehydrate` + `MarginObservation` +
`classify_iml_reducing`; `LegacyPdtCompatMode(rehydrated_state=...)` + the broker-rejection latch hook
(FD-M4-15, "wired in M5"); `LossLimitsMonitor.rehydrate`; `rehydrate_risk_state(rows)` (§K.4); the §O
frozen M5 caller obligations (quoted verbatim in §M.2).

### 0.2 Verified external facts

**Alpaca (docs.alpaca.markets, re-fetched 2026-06-10 by this synthesis; A-numbers cited throughout):**

| # | Fact | Source page |
|---|---|---|
| A1 | Order types market/limit/stop/stop_limit (+trailing/bracket); whole-share USD orders support GTC/DAY (IOC/FOK need sales approval); extended hours = **limit only**, GTC/DAY; fractional = DAY only ⇒ **limit + DAY is the universally available pair** | `docs/orders-at-alpaca` |
| A2 | Sub-penny rule: limit ≥ $1.00 ⇒ max **2** decimals; < $1.00 ⇒ max **4** decimals; violation rejected with code **42210000** ("sub-penny increment does not fulfill minimum pricing criteria") | `docs/orders-at-alpaca` |
| A3 | "Orders not eligible for extended hours submitted after 4:00pm ET will be **queued up for release the next trading day**" (not rejected) — so RTH-only discipline must be OUR gate, not the broker's | `docs/orders-at-alpaca` |
| A4 | Order statuses — common: `new, partially_filled, filled, done_for_day, canceled, expired, replaced, pending_cancel, pending_replace`; rare: `accepted, pending_new, accepted_for_bidding, stopped, rejected, suspended, calculated` (16 strings) | `docs/orders-at-alpaca` |
| A5 | Paper fills match "against the best available current market price (NBBO)"; "partial fills for a random size **10% of the time**"; "Your order quantity is **not checked against the NBBO quantities**" (infinite-liquidity fiction — exactly why the modeled fill exists); no market impact / latency slippage | `docs/paper-trading` |
| A6 | Paper does NOT simulate: market impact, information leakage, latency slippage, **regulatory fees**, **dividends** (borrow fees "Coming Soon") ⇒ `broker_account_pnl` is fee-free on paper; our `FeeModel` applies to the modeled side only (FD-M5-15) | `docs/paper-trading` |
| A7 | Paper base URL `https://paper-api.alpaca.markets`; paper account has its own API key, separate from live | `docs/paper-trading` |
| A8 | POST `/v2/orders`: `type`/`time_in_force` required, `side` required (non-mleg); `limit_price` required for limit; `client_order_id` unique, **≤ 128 characters**, auto-generated if absent; errors **403** "Buying power or shares is not sufficient", **422** "Input parameters are not recognized" | `reference/postorder` |
| A9 | Equities regulatory pass-through on live = FINRA TAF (sells only) + CAT; current effective rates live on the brokerage fee schedule (not the docs page) — re-verified at the credentialed tier (M5-2a) | `docs/regulatory-fees` |
| A10 | `trade_updates` websocket exists but M5 pins REST polling (FD-M5-7); cancel is asynchronous (`pending_cancel` until confirmed) | `docs/websocket-streaming`, `docs/orders-at-alpaca` (architect designs, 2026-06-10) |

**Regulatory fee rates (FD-M5-5 verification duty):**

| # | Fact | Verification |
|---|---|---|
| F1 | FINRA TAF: **$0.000166 per share** for each SALE of a covered equity security, **max $8.30 per trade**; "if the execution price… is less than the Trading Activity Fee rate… no fee will be assessed" | **VERIFIED 2026-06-10** at `finra.org/rules-guidance/rulebooks/corporate-organization/section-1-member-regulatory-fees` (Schedule A §1; SR-FINRA amendment noted on page) |
| F2 | SEC Section 31: **$27.80 per million USD** of covered SALE proceeds = `Decimal("0.0000278")` per dollar (FY2025 fee-rate-advisory figure) | **PINNED, NOT RE-VERIFIED TODAY**: sec.gov returns HTTP 403 to programmatic fetch (four URL forms tried 2026-06-10). Both architect inputs carried the same figure marked TO-VERIFY. Frozen as a CODE CONSTANT with `FEE_MODEL_VERSION="reg_fees_v1"` provenance + a standing M5-2a obligation: re-verify against the current SEC fee-rate advisory (manually, sec.gov) before the first credentialed paper session and bump the constant + version in a reviewed commit if changed. Safety posture: fees touch ONLY `execution_realistic_pnl` (A6), are rounded AGAINST us (§J), and a stale rate is a label-accuracy issue, never a ledger or gate issue. |
| F3 | `alpaca-py` exact pin: **`0.43.4`** (released 2026-04-29) | PyPI is outside this pass's permitted fetch domains; the integration architect verified 0.43.4 as latest on 2026-06-10. Pinned UNCOMMENTED per FD-M5-5; the build step re-verifies at install time (`pip index versions alpaca-py` in `.venv`) and records the answer in the build notes — the exact pin installs regardless; changing it is a reviewed commit. |

## 1. Frozen decisions (FD-M5-1 … FD-M5-30)

FD-M5-1…10 restate Robin's locked decisions LD-M5-1…10 (delegated; NOT relitigated). FD-M5-11…30
resolve every cross-design disagreement, each with a one-line rationale.

| # | Decision | Rationale / resolves |
|---|----------|----------------------|
| FD-M5-1 | **(LD-M5-1) Flatten orders are ALWAYS price-capped marketable-limits.** A symbol with no price ever seen lands in `failed[]`/`residual` with a journaled reason (`no_price_for_cap`) — NEVER a bare market order, no exceptions. Implemented as `PriceCappedFlattenBroker` (§H.2) wrapping the broker handed to `RiskKillSwitch.trigger`, so `kill_switch.py` stays unedited (FD-M4-4); the wire-payload builder additionally has NO market-order shape (a `limit_price=None` intent is unserializable — structural). | Locked. |
| FD-M5-2 | **(LD-M5-2) Local run-gate mechanism = `.secrets/run_gates.json`** (git-ignored), consulted ONLY for the two run-gate keys (`agent_rules.enabled`, `agent_rules.paper_trading.enabled`) and ONLY in paper mode; caps/universe/signal/execution config stay tighten-only committed. Absent/malformed/non-identity-True ⇒ gates read False ⇒ reject-all. The committed-config S1 canary additionally asserts the absent-file case. Own contract section §O.2 + dedicated tests (§R 13). | Locked; resolves integration §4.5's pinned alternative (gate keys via CLI overlay) → rejected: overlays AND-merge and must stay loosening-proof. |
| FD-M5-3 | **(LD-M5-3) First committed caps/universe values for REAL paper opens are DEFERRED to Robin** (a reviewed commit after the build). Synthetic mode uses an in-memory permissive fixture config (`permissive_paper_fixture_config()`, §Q), never written under `config/`. | Locked. |
| FD-M5-4 | **(LD-M5-4) Observe mode is file-driven** (events JSONL from the M1 recorder), constructs NO broker object, may intersect file symbols with `--symbols`, and runs the M3 calibration probe end-to-end TODAY on recorded data (probe → resolver → calibration report). | Locked. |
| FD-M5-5 | **(LD-M5-5) `alpaca-py==0.43.4` pinned exact, UNCOMMENTED** in `requirements.txt` (the `databento==0.79.0` precedent), installed only in `.venv` (PEP-668), imported ONLY inside `_build_real_client()` (never reached offline); AST + `sys.modules` guards extend with `alpaca`. Rate/version verification per §0.2 F1–F3. | Locked; resolves execution/fail-closed "commented pin" → uncommented (comment-only pins drift; the purity tests prove non-import, not absence). |
| FD-M5-6 | **(LD-M5-6) Modeled fills: top-of-book v1 (`model="tob_l1_v1"`) is the wired default** (honest for EQUS.MINI L1); **depth-VWAP v2 (`model="depth_vwap_l2_v2"`) exists code+tests** behind a `DepthView` seam left `None` in observe/paper until L2 recording is provisioned. Divergence: flag ALWAYS (`fill_divergence` row); a `divergence_alert` row when `|divergence_usd| > notional × 10 bps` (`DIVERGENCE_ALERT_BPS = Decimal("10")`, code constant). | Locked. |
| FD-M5-7 | **(LD-M5-7) Broker order-state via REST polling** (no websocket in M5); **`client_order_id = our deterministic `order_id`** (idempotency + the M6 reconcile join key; ≤128 chars per A8). | Locked. |
| FD-M5-8 | **(LD-M5-8) Synthetic structural isolation uses BOTH walls:** (wall 1) the orchestrator pipeline ctor type check — `isinstance(strategy, SyntheticStrategy) and type(broker) is not FakeBroker ⇒ SyntheticConfinementError` at construction; (wall 2) the broker-side namespace refusal — `AlpacaPaperBroker._place` (ALL modes, spy included) raises `SyntheticConfinementError` on any intent whose `intent_id` starts with `"synthetic-"`, and `FakeBroker._place` refuses any intent whose `intent_id` does NOT; plus (wall 3) the AST import guard on `strategies/`. The **S9 backtest-artifact gate ships in M5 with an empty `artifacts/backtests/` dir ⇒ every real strategy rejects fail-closed** (`backtest_artifact_missing`). | Locked; `type(broker) is FakeBroker` is type-identity (execution DD-M5-13) so a hostile `FakeBroker` subclass of `AlpacaPaperBroker` cannot spoof wall 1. |
| FD-M5-9 | **(LD-M5-9) M5 does NOT edit M3's `DecisionLedger` or its frozen `ACTIONS` vocabulary.** `would_open`/`would_close`/order facts live in the NEW exec streams: `strategy_decision` rows go on `journal/orders.jsonl` (not `decisions.jsonl`), `STRATEGY_DECISION_ACTIONS = {"would_open","would_close"}`. An empty `scan()` journals nothing on the strategy path (the M3 probe already owns per-bar observe rows). | Locked; resolves execution DD-M5-16 (new event type on `decisions.jsonl`) → moved to `orders.jsonl` so the M3 stream stays byte-frozen. |
| FD-M5-10 | **(LD-M5-10) Latency budget:** committed `agent_rules.latency_budget_ms` (JSON int 250) is honored but **floored by `LATENCY_BUDGET_MIN_MS = 250`** (code constant — the `min()`-merge polarity trap: an overlay lowering it would loosen realism); `effective_latency_budget_ms = max(parsed, 250)`, computed once in `ExecutionConfig`. **The await lives in `orchestrator.py` as a scheduler item over the injected clock** (an `OrderTask` in state `AWAIT_LATENCY` becomes due when `clock.now_ms() >= decision_seen_at_ms + effective_latency_budget_ms`), never in `execution_realism.py`, never a real sleep in any decision path. | Locked; resolves execution DD-M5-14's `Sleeper`/async seam → no asyncio in M5: the run loop is a synchronous tick loop (replay-driven offline; honest, since no live feed exists until M1-2b). |
| FD-M5-11 | **Ladder shape + rung 1.** Two phases mirroring M2 `decide()`/M4 FD-M4-9: Phase 1 terminal short-circuits in frozen order `run_gates → kill → stamp`; Phase 2 collect-ALL in frozen order `candidate → strategy_gate → inflight → latency → quote → market_state → order → risk` (sorted, deduped union; `gate_stage=null`). **Rung 1 is the literal `gates.opening_allowed`** ⇒ on the committed config every preflight terminates at `run_gates` with the byte-exact terminal shape (S1 unchanged in substance — reject-all by ladder instead of by stub). | Resolves execution (stamp before gates) vs fail-closed/integration (gates first) → gates first: M4 FD-M4-9 precedent + the S1 canary's byte-exact terminal assertion; parent PR-1 is honored because `missing_decision_stamp` is a TERMINAL hard reject wherever evaluated and no later stage can run without the stamp. |
| FD-M5-12 | **`missing_decision_stamp` is a terminal hard reject** (phase-1 rung 3): `stamp is None`, `quote_a` missing, `decision_seen_at_ms` not an int, or `decision_ts_utc` unparseable ⇒ reject; nothing downstream is meaningful without t0 (parent PR-1 verbatim). | All three designs agree on the semantics; only placement differed (FD-M5-11). |
| FD-M5-13 | **TOCTOU binding at `consume()`** (the task-pinned mechanism): an open `_Authorization` additionally stores `limit_price`, `kill_generation`, `minted_at_ms`; `execution_preflight.bind_runtime(clock=…, kill_generation_source=…)` is set once by the orchestrator; `consume()` of an `"open"` authorization re-checks — runtime unbound ⇒ `PreflightStale("preflight_runtime_unbound")`; `clock.now_ms() − minted_at_ms > OPEN_TOKEN_TTL_MS` (strict `>`) ⇒ `PreflightStale("open_token_expired")`; `kill_generation_source() != kill_generation` ⇒ `PreflightStale("kill_generation_changed")`. Runs INSIDE `BrokerBase.submit_order → require_token → consume` — non-bypassable without touching the `Broker` Protocol. **Reduce-only consume is exempt** (zero new checks; FD-M4-3). `OPEN_TOKEN_TTL_MS = 2000`. | Resolves fail-closed's `require_token(broker=, now_ms=, kill_generation=)` signature change + broker-instance binding → consume-side module runtime (task-locked; keeps `require_token(intent, token)` and the M0 call sites byte-stable); TTL 2000 over fail-closed's 1000 (one freshness family with `quote_staleness_ms_max`/`DEFAULT_FRESHNESS_TTL_MS`). |
| FD-M5-14 | **Preflight is PURE and the mint runs it ITSELF.** `evaluate_preflight(inputs: PreflightInputs) -> PreflightPass | PreflightReject` (no I/O, no clock read, no journal write — LD5/FD-M4-5 posture; the CALLER journals); `mint_open_token(inputs) -> (OpenPreflightToken, PreflightPass)` evaluates internally (a caller can never assert "passed"), raising `PreflightRejected(reject)` on failure. The legacy `mint_open_token(config, intent)` 2-positional signature is REPLACED (its only legitimate caller was the reject-all test, which is rewritten to the new signature; the registry mechanics are untouched). | All three designs agree on purity + mint-runs-ladder; signature unified on execution's `PreflightInputs` dataclass (one input object beats 20 kwargs for byte-stable tests). |
| FD-M5-15 | **Fee rates are CODE CONSTANTS** (`fees.py` §J): `SEC_SECTION31_RATE=Decimal("0.0000278")`, `TAF_PER_SHARE_SOLD=Decimal("0.000166")`, `TAF_CAP_PER_TRADE_USD=Decimal("8.30")`, `FEE_MODEL_VERSION="reg_fees_v1"`. Sells only; buys ⇒ zero; each component ceil-rounded to the cent (fees round AGAINST us); applied ONLY to `execution_realistic_pnl` (A6: paper charges none; injecting synthetic fees into the broker side would manufacture permanent reconcile drift). `config/risk_rules.json` is NOT edited. | Resolves execution/fail-closed (config-string rates) vs integration (code constants) → code constants: regulatory facts are not knobs (M2 §G / FD-M4-6 discipline); changing a rate = a reviewed commit + version bump either way. |
| FD-M5-16 | **Order-state vocabulary + fail-closed mapping.** Local `ORDER_STATES = {accepted, partially_filled, filled, canceled, expired, rejected, done_for_day, pending_cancel, unknown}`; `TERMINAL_STATES = {filled, canceled, expired, rejected, done_for_day}`; the frozen `ALPACA_STATUS_MAP` (§F) maps all 16 verified strings — rare/ambiguous ones (`replaced, pending_replace, suspended, accepted_for_bidding, stopped, calculated`) map to **`unknown`**. `unknown` (mapped or unmapped-string) is NEVER terminal and never filled: `order_state_alert` row + best-effort cancel + keep polling. | Resolves execution (rare → SUBMITTED) vs fail-closed (rare → unknown) → unknown: never assume understanding of a status we cannot test against fixtures. |
| FD-M5-17 | **Write-ahead submit protocol + idempotent recovery.** `order_submit_attempt` is journaled BEFORE the network call; ack ⇒ `order_submitted`; HTTP reject ⇒ `broker_reject` (terminal for that `client_order_id`); timeout/ambiguous ⇒ NEVER blind-resubmit: query by `client_order_id` up to `SUBMIT_RECOVERY_ATTEMPTS=3`, found ⇒ adopt, not found ⇒ `order_submit_unconfirmed` + the symbol enters an in-memory **open-deny set** for the rest of the run (presumed-live until reconciled — fail-closed). `client_order_id` is never reused; a retry is a NEW decision → NEW preflight → NEW id. | Fail-closed §5.4 adopted whole (strongest crash story); execution's adopt-and-watch folded into restart recovery (FD-M5-24). |
| FD-M5-18 | **Exact integrated notional from polled aggregates.** `delta_qty = cur.filled_qty − prev.filled_qty` (> 0); `delta_cost = cur.filled_qty×cur.filled_avg_price − prev.filled_qty×prev.filled_avg_price` (exact Decimal — never `delta_qty × avg`); `filled_qty` regression or `filled_avg_price` present with `filled_qty == 0` ⇒ `OrderInvalid` (alert, fail-closed, no fill row). Position `broker_cost_usd = Σ delta_cost` exact (parent principle 3). | Both designs that addressed it agree; frozen here with the §F parser as the ONE chokepoint. |
| FD-M5-19 | **Modeled-fill basis covers FULL qty conservatively.** `tob_l1_v1`: `min(qty, displayed opposite size)` modeled at quote-B's opposite touch, the REMAINDER priced at `capped_limit` (the worst-price bound — never better than the cap by construction); `realism_class ∈ {modeled_full, modeled_partial, modeled_unfillable}` with `modeled_fillable_qty` journaled so the honest partial fact is preserved. `depth_vwap_l2_v2`: walk levels `≤ capped_limit`, exact integrated VWAP, same remainder rule. Not marketable vs quote B / no usable quote ⇒ `modeled_unfillable`, `modeled_cost_usd=null`, PnL modeled side `unassessed`. Computed ONCE from quote B (no hindsight, never re-shopped). | Resolves execution (remainder explicitly unfilled) vs integration (remainder at cap): a full-qty modeled basis is required because the broker WILL fill full qty (A5) and a partial-qty basis cannot price a full-qty position; the cap is the documented conservative bound. |
| FD-M5-20 | **Divergence flag semantics.** `fill_divergence` row on every order that reaches `filled` (and on `partially_filled` terminal states, over the filled qty): `divergence_usd = broker_cost_usd − modeled_cost_over_filled_qty` (plain Decimal — the ONE documented cross-newtype comparison seam), `divergence_bps = divergence_usd / broker_cost_usd × 10000` quantized `BPS_QUANTUM`; `flag ∈ DIVERGENCE_FLAGS = {aligned, broker_optimistic, broker_conservative, unassessed}` — buys: broker cheaper than model ⇒ `broker_optimistic` (the parent PR-8 flag: paper filling flatteringly), dearer ⇒ `broker_conservative`, equal ⇒ `aligned`, modeled side null ⇒ `unassessed`. `divergence_alert` row iff `|divergence_usd| > broker_cost_usd × DIVERGENCE_ALERT_BPS/10000`. `pnl_snapshot.realism_class` = the position's latest flag. | Disentangles the three designs' conflated "realism class" vocabularies into modeled-fillability (FD-M5-19) vs broker-vs-model divergence (this row). |
| FD-M5-21 | **One in-flight order at a time.** `execution.max_open_orders` committed `1`; the parser REJECTS ≠ 1 in M5; the ladder still carries `open_order_in_flight` (stage `inflight`) as journaled defense-in-depth. Serial decide→await→requote→preflight→submit→watch→book pipeline; concurrency is an M6/M7 loosening. | Execution DD-M5-11 adopted; integration agrees. |
| FD-M5-22 | **No automatic retry/replace.** A failed requote/preflight/reject/cancel/expiry ENDS the attempt with journal rows; we never send replace; broker-initiated `replaced`/`pending_replace`/`suspended` ⇒ `unknown` + alert + best-effort cancel (FD-M5-16). Retry policy is M6. | Execution DD-M5-12; no dissent. |
| FD-M5-23 | **Post-submit watcher + cancel-on-state-change.** Until terminal, each open order is watched every poll: trigger set `CANCEL_CAUSES` (§2.4) — feed `reconnect_epoch` change vs the order's bound epoch, halt/LULD/auction, NOT_TRADABLE, stale-default verdict, session leaving RTH, kill trip, unexpected status, restart. On trigger: best-effort `cancel_order` (NO token — cancel is risk-reducing-or-neutral and must never be gated; test-asserted that the cancel path performs no mint and no `require_token`) + `post_submit_cancel_attempt` row; **late fills that land anyway remain authoritative `broker_fill` rows** — price-bounded by `capped_limit` by construction — while the modeled side is flagged, never back-voided (parent PR-9). | All three designs agree; vocabulary unified in §2.4. |
| FD-M5-24 | **Restart recovery is conservative.** A journaled `order_submit_attempt`/`order_submitted` without `order_terminal` at rehydrate: paper mode ⇒ re-query by `client_order_id`, ADOPT the broker's answer, resume polling **and** issue one best-effort cancel (`cause="restart_unknown_state"` — a restart cannot vouch for pre-restart preflight conditions); not found after 3 attempts ⇒ `order_submit_unconfirmed{resolution:"not_found"}` + open-deny; observe/offline ⇒ `order_submit_unconfirmed{resolution:"offline_orphan"}` + open-deny. | Resolves execution (adopt-and-watch only) vs integration (adopt + cancel) → adopt + cancel: conservative wins; fills stay authoritative either way. |
| FD-M5-25 | **Kill sequence: cancel-opens-then-flatten.** On an accepted trip: (1) best-effort cancel every open order (`cause="kill_trip"`) so nothing can still increase exposure mid-flatten; (2) `void_token` any minted-unconsumed open token (hygiene; the generation bump already invalidates them at consume); (3) `RiskKillSwitch.trigger(cause, PriceCappedFlattenBroker(...), portfolio, evaluation=…, account=…, tradability=…)` — flatten attempts ALL positions (FD-M4-20). | Fail-closed froze it; execution OQ-3 asked — resolved: flatten-first would leave live opening orders able to increase exposure mid-flatten. |
| FD-M5-26 | **Strategy closes ride the reduce path, price-capped tight.** A close = `strategy_decision(action="would_close")` → `mint_reduce_only_token(held, intent)` → submit; limit = `bid_B × (1 − slippage_cap_bps/10000)` per §C (tight cap; the kill path uses the wider `FLATTEN_CAP_BPS=100`). No open preflight, no risk verdict (FD-M4-3: the reduce path never consults `can_open`). No usable quote this tick ⇒ `reject{stage:"reduce_pricing", reasons:("no_price_for_cap",)}` + retry next tick (deferral, never a gate; the kill path's urgency machinery is `failed[]`/`residual`). | Synthesizes the synthetic-E2E close need (integration §3.5) with FD-M5-1's pricing rule; tight-vs-wide cap split is new and frozen here. |
| FD-M5-27 | **Backtest artifact = committed + reviewed + hash-bound (no crypto in M5).** `artifacts/backtests/<strategy_id>.json` = `{v, strategy_id, rules_hash, data_pin, metrics{…, basis:"execution_realistic_pnl"}, created_utc, artifact_hash}` with `artifact_hash = serializer.row_hash(payload minus artifact_hash)`; `verify` ⇒ `{ok, missing, key_mismatch, hash_invalid}`; any `rules_hash`/`data_pin` drift re-closes the gate. M5 ships the verifier + an EMPTY dir (`.gitkeep`) ⇒ every real strategy rejects (FD-M5-8). M7 owns artifact production and may upgrade to an HMAC signing ceremony. | Resolves fail-closed (HMAC + `.secrets/` key) vs integration/execution (hash-bound committed JSON) → hash-bound: the repo has no key-ceremony infrastructure and review is the actual authority; a hostile committer defeats an in-repo HMAC check anyway. |
| FD-M5-28 | **Synthetic namespaces are frozen strings.** `SyntheticStrategy.strategy_id` MUST start with `"synthetic."` (enforced by `__init_subclass__`); synthetic-pipeline order ids use the prefix `"synthetic-o-"` (real: `"o-"`), and `client_order_id = order_id` always — so the namespace rides the id through journal, registry, and broker seam, making wall 2 a pure string check at `_place`. | Merges execution's `"synthetic_"` and integration's `"synthetic."` conventions; the id-prefix transport of the namespace is integration's, adopted. |
| FD-M5-29 | **Execution knobs: polarity-checked config vs code constants** (§B table). Config (JSON ints, `min()`-merge correct because smaller = tighter/fresher): `slippage_cap_bps: 25`, `order_poll_interval_ms: 500`, `account_refresh_interval_ms: 2500`, `max_open_orders: 1`. CODE CONSTANTS (inverted polarity or safety quanta): `LATENCY_BUDGET_MIN_MS=250`, `MIN_REQUOTE_DELTA_MS=1`, `OPEN_TOKEN_TTL_MS=2000`, `RISK_VERDICT_TTL_MS=2000`, `ORDER_POLL_INTERVAL_MS_MAX=1000` (ceiling), `SUBMIT_RECOVERY_ATTEMPTS=3`, `DIVERGENCE_ALERT_BPS=10`, `FLATTEN_CAP_BPS=100`, `DEPTH_FRESHNESS_TTL_MS=2000`, fee rates (FD-M5-15). `latency_lost_edge` reuses `slippage_cap_bps` as its adverse-move bound (ONE economics knob in M5; finer edge economics are M7). | Resolves the three designs' three different knob sets; one-knob edge check resolves integration's separate `edge_decay_bps_max` → dropped. |
| FD-M5-30 | **`risk_verdict` binding is structural.** `PreflightInputs` carries BOTH the `RiskVerdict` object AND its journaled row (`risk_verdict_row`); the ladder re-verifies `row["hash"]` via `serializer.row_hash` and `row's verdict_id == verdict.verdict_id` (journaled-BEFORE-mint, made checkable), plus field binding (decision_id, symbol, qty, kill_generation) and freshness (`now_ms − risk_verdict_now_ms > RISK_VERDICT_TTL_MS` strict ⇒ `risk_verdict_stale`). Mismatch on identity fields the caller controls ⇒ `risk_verdict_mismatch` (a reason, not an exception); a tampered row hash ⇒ `ExecError` (bug, not data). | Fail-closed rung 14 + integration rung 13 merged; honors the M4 §J announcement ("M5's rebuilt mint will REQUIRE a journaled allowed RiskVerdict"). |

## 2. Frozen vocabularies + ladder order

### 2.1 `PREFLIGHT_REASONS` (closed set; emitting out-of-vocab raises `ExecError`)

```
phase 1 — terminal (one stage per reject):
  run_gates_off, kill_switch_halted, missing_decision_stamp

phase 2 — collected (collect-ALL, sorted deduped union):
  order_matrix_unsupported, invalid_lot,                                  # candidate
  strategy_not_paper_eligible, backtest_artifact_missing,                 # strategy_gate
  artifact_key_mismatch, artifact_hash_invalid,
  synthetic_requires_fake_broker, fake_broker_requires_synthetic,
  open_order_in_flight,                                                   # inflight
  latency_not_elapsed, requote_not_later, epoch_changed,                  # latency
  quote_missing, quote_stale, quote_crossed, quote_locked,                # quote
  quote_one_sided, quote_nonfinite, quote_nonpositive, spread_too_wide,   #   (M3 strings verbatim)
  market_state_not_tradable, market_state_stale_default,                  # market_state
  market_state_not_rth, halt_luld_auction, ca_blackout,
  unpriceable_candidate, invalid_tick, not_marketable, latency_lost_edge, # order
  risk_verdict_missing, risk_verdict_stale, risk_verdict_mismatch,        # risk
  can_open_denied, kill_generation_changed

consume-time only (raised as PreflightStale; journaled stage="consume"):
  preflight_runtime_unbound, open_token_expired
  (+ kill_generation_changed — shared string, also emittable at stage "risk")

RESERVED (in the frozenset, never emitted in M5):
  ssr_short_blocked, locate_unavailable          # short-side milestone
  extended_hours_blocked                          # M6+ matrix expansion
```

Arithmetic (frozen): 3 terminal + 34 collected + 2 consume-only + 3 reserved = **42 members** in
`PREFLIGHT_REASONS`. Reused strings are deliberate (the M4 `market_state_stale_default` precedent):
`quote_*`/`spread_too_wide` are M3's verbatim, `strategy_not_paper_eligible`/`unpriceable_candidate`/
`can_open_denied`-adjacent strings echo M4, `missing_decision_stamp`/`epoch_changed`/`latency_lost_edge`
echo parent §7 — funnel joins across streams never re-key.

Reject-row supplements (NOT in `PREFLIGHT_REASONS`; legal only on their own stages):
`EXTRA_REJECT_REASONS = {"no_price_for_cap", "broker_rejected"}` — `no_price_for_cap` on
`stage="reduce_pricing"` (FD-M5-26) and inside kill `failed[]` tuples (FD-M5-1); `broker_rejected` on
`stage="broker"` rows (the broker's own code/message ride `detail`).
`REJECT_STAGES = PREFLIGHT_STAGES ∪ {"consume", "broker", "reduce_pricing"}`.

### 2.2 The open-preflight ladder (S4 — frozen ORDER; stage names journaled as `gate_stage`/`stages_skipped`)

`PREFLIGHT_STAGES: Tuple[str, ...] =
("run_gates","kill","stamp","candidate","strategy_gate","inflight","latency","quote","market_state","order","risk")`

**Phase 1 — terminal short-circuits** (stop at first hit; the reject's `reasons` is exactly that stage's
sorted reasons; ALL later stage names land in `stages_skipped`):

| # | Stage | Reason(s) | Frozen check |
|---|-------|-----------|--------------|
| 1 | `run_gates` | `run_gates_off` | `not gates.opening_allowed(inputs.gates_config)` — the literal M0 function, identity-strict. **On the committed config every preflight terminates here** (S1; the canary asserts the byte-exact terminal shape: `reasons=("run_gates_off",)`, `gate_stage="run_gates"`, `stages_skipped` = the 10 later stages in order, `capped_limit=None`). |
| 2 | `kill` | `kill_switch_halted` | `inputs.kill_state != "monitoring"` (covers `flattening` and `halted`; the M4 §A string vocabulary). Out-of-vocab state string ⇒ `ExecError`. |
| 3 | `stamp` | `missing_decision_stamp` | `inputs.stamp is None`, or `stamp.quote_a is None`, or `stamp.decision_seen_at_ms` is not an `int` (bool excluded), or `stamp.decision_ts_utc` unparseable ISO-8601 UTC. **Hard reject — a check that cannot run is never skipped** (parent PR-1; FD-M5-12). |

**Phase 2 — collect-ALL** (no short-circuit between stages 4–11; every applicable reason from every
evaluable stage; `reasons` = sorted deduped union; `gate_stage = null`; a stage rendered unevaluable by
an upstream phase-2 finding is recorded in `stages_skipped` — the M4 phase-2 skip convention):

| # | Stage | Reason(s) | Frozen check |
|---|-------|-----------|--------------|
| 4 | `candidate` | `order_matrix_unsupported`, `invalid_lot` | Single-leg M5: `len(candidate.legs) != 1` ⇒ `ExecError`. Matrix (narrow, A1): leg `side != "buy"` (long-only opens, FD-M4-1), or the intended `order_type != "marketable_limit"`, or `tif != "day"`, or an `is_reducing` open ⇒ `order_matrix_unsupported`. Lot: `qty != qty.to_integral_value()` or `qty < 1` ⇒ `invalid_lot` (whole shares only; fractional is out of matrix). |
| 5 | `strategy_gate` | `strategy_not_paper_eligible`, `backtest_artifact_missing`, `artifact_key_mismatch`, `artifact_hash_invalid`, `synthetic_requires_fake_broker`, `fake_broker_requires_synthetic` | `candidate.paper_eligible is not True` (identity) ⇒ not eligible. **Real strategy** (`inputs.strategy_is_synthetic is False`): `inputs.artifact_check.status` maps `missing/key_mismatch/hash_invalid` → its reason (S9: the shipped-empty dir makes `backtest_artifact_missing` the M5 constant for every real strategy — FD-M5-8); `inputs.broker_kind == "fake"` ⇒ `fake_broker_requires_synthetic`. **Synthetic strategy:** artifact NOT required (`artifact_check` recorded, not consulted); `inputs.broker_kind != "fake"` ⇒ `synthetic_requires_fake_broker` (defense-in-depth; the structural walls are FD-M5-8's). |
| 6 | `inflight` | `open_order_in_flight` | `inputs.open_orders_in_flight > 0` (FD-M5-21 defense-in-depth; the serial orchestrator never gets here with one in flight). |
| 7 | `latency` | `latency_not_elapsed`, `requote_not_later`, `epoch_changed` | Evaluated iff `quote_b` present (else recorded in `stages_skipped`; absence is stage 8's). `quote_b.seen_at_ms − stamp.decision_seen_at_ms < exec_config.effective_latency_budget_ms` (strict `<`; passes at exactly the budget) ⇒ `latency_not_elapsed`. **Strictly-later discipline:** `quote_b.seen_at_ms < quote_a.seen_at_ms + MIN_REQUOTE_DELTA_MS` (=1) **or** identical `(vendor_seq, ts_event_utc)` provenance pair (a re-served quote A is not a second quote) ⇒ `requote_not_later`. `quote_b.reconnect_epoch != quote_a.reconnect_epoch` **or** `inputs.feed_epoch_now != quote_b.reconnect_epoch` ⇒ `epoch_changed` (S4). |
| 8 | `quote` | `quote_missing` + the seven M3 strings verbatim | `quote_b is None` ⇒ `quote_missing`. Else embed `inputs.quote_b_verdict.reasons` UNCHANGED — `quote_quality.evaluate` is the ONE quote decider (the caller computed it with the SAME `now_ms` and the committed `spread_bps_max`/`quote_staleness_ms_max`); the preflight re-derives nothing. |
| 9 | `market_state` | `market_state_not_tradable`, `market_state_stale_default`, `market_state_not_rth`, `halt_luld_auction`, `ca_blackout` | Identity first: verdict `symbol`/`instrument_id` mismatching the leg ⇒ `ExecError`. `verdict.tradability != TRADABLE` ⇒ not_tradable (REDUCE_ONLY blocks opens too). `"cache_stale_safe_default" in verdict.reasons` ⇒ stale_default. `verdict.session_state != RTH` ⇒ not_rth (M5 opens are RTH-only — A3's queue-for-next-day trap is structurally unreachable). `halt != NONE` or `luld != NONE` or `session_state == AUCTION` ⇒ `halt_luld_auction`. `verdict.ca_blackout` ⇒ `ca_blackout`. (The safe-default verdict fires not_tradable + stale_default + not_rth together — collect-all.) |
| 10 | `order` | `unpriceable_candidate`, `invalid_tick`, `not_marketable`, `latency_lost_edge` | Recorded in `stages_skipped` iff stage 8 found `quote_b` missing or unusable (`quote_b_verdict.ok is False`). Else run `order_pricing.marketable_limit_cap` (§C) on quote A/B: strategy `limit_price <= 0` ⇒ `unpriceable_candidate` (FD-M4-16 mirror); strategy limit off the Reg-NMS grid ⇒ `invalid_tick` (the derived cap is on-grid by construction); final `capped_limit` not marketable vs quote B ⇒ `not_marketable` (we do not rest orders in M5); adverse move A→B beyond `slippage_cap_bps` (buy: `ask_B > ask_A × (1 + bps/10000)`) ⇒ `latency_lost_edge`. On pass, `capped_limit` is fixed here and becomes THE submitted limit. |
| 11 | `risk` | `risk_verdict_missing`, `risk_verdict_stale`, `risk_verdict_mismatch`, `can_open_denied`, `kill_generation_changed` | `risk_verdict is None or risk_verdict_row is None or risk_verdict_now_ms is None` ⇒ missing (the caller failed its journal-then-mint duty; fail closed). Row binding per FD-M5-30: re-hash mismatch ⇒ `ExecError`; `verdict_id`/`decision_id`/symbol/qty mismatch ⇒ `risk_verdict_mismatch`. `now_ms − risk_verdict_now_ms > RISK_VERDICT_TTL_MS` (strict) ⇒ `risk_verdict_stale`. `verdict.allowed is not True` ⇒ `can_open_denied` (the M4 reasons ride the reject row's `detail.risk_reasons`, never this vocabulary). `verdict.kill_generation != inputs.kill_generation` ⇒ `kill_generation_changed` (the verdict was computed under a stale world). |

Pass ⟺ zero reasons collected ⟺ `evaluate_preflight` returns `PreflightPass`. Reject ⇒ no
authorization is written, the CALLER journals the `reject` row (§P), nothing reaches the broker.

### 2.3 Consume-time re-checks (TOCTOU; FD-M5-13)

Inside `consume()` (reached only via `BrokerBase.submit_order → require_token`), for `kind=="open"`
authorizations ONLY, in this order: runtime unbound ⇒ `PreflightStale("preflight_runtime_unbound")`;
TTL `clock.now_ms() − minted_at_ms > OPEN_TOKEN_TTL_MS` strict ⇒ `PreflightStale("open_token_expired")`;
`kill_generation_source() != auth.kill_generation` ⇒ `PreflightStale("kill_generation_changed")`. A
`PreflightStale` REVOKES the authorization (deleted before raising — the token is spent, not reusable);
the caller journals `reject{stage:"consume"}`. Reduce-kind consume: byte-identical to M0.

### 2.4 Other frozen vocabularies (all in `exec_reasons.py`, §A)

```
ORDER_STATES        = {accepted, partially_filled, filled, canceled, expired,
                       rejected, done_for_day, pending_cancel, unknown}
TERMINAL_STATES     = {filled, canceled, expired, rejected, done_for_day}
CANCEL_CAUSES       = {epoch_changed, halt_luld_auction, market_state_not_tradable,
                       market_state_stale_default, session_end, kill_trip,
                       unexpected_status, restart_unknown_state}
CANCEL_OUTCOMES     = {cancel_submitted, already_terminal, cancel_rejected, error}
CLOSE_REASONS       = {strategy_exit, kill_flatten, session_end, synthetic_script, operator}
REALISM_CLASSES     = {modeled_full, modeled_partial, modeled_unfillable}
MODELED_FILL_MODELS = {tob_l1_v1, depth_vwap_l2_v2}
DIVERGENCE_FLAGS    = {aligned, broker_optimistic, broker_conservative, unassessed}
FILL_POLICIES       = {immediate_full, partial_then_full, never_fill, reject_all}   # FakeBroker
BROKER_KINDS        = {spy, fake, alpaca_paper, alpaca_live}                        # alpaca_live RESERVED (M8)
STRATEGY_DECISION_ACTIONS = {would_open, would_close}
FILL_SOURCES        = {alpaca_paper, fake}
SUBMIT_RESOLUTIONS  = {adopted, not_found, offline_orphan}
```

Out-of-vocab anywhere ⇒ `ExecError` (FATAL, never coerced — the `MarketStateError`/`RiskError` posture).
Reserved members (`alpaca_live`) emitted in M5 ⇒ test failure.

## 3. Module map + import discipline

**Existing files that GROW (additive; every existing test stays green unmodified except the M0
reject-all mint test, rewritten to the new signature — FD-M5-14):**

| File | Growth | Frozen things preserved |
|---|---|---|
| `scripts/agent/execution_preflight.py` | `DecisionStamp`, `PreflightInputs`, `PreflightPass`, `PreflightReject`, `PreflightStale(PreflightRejected)`; `evaluate_preflight`; `mint_open_token` REBUILT (§D); `bind_runtime`/`unbind_runtime`/`void_token`; open `_Authorization` gains `limit_price`, `kill_generation`, `minted_at_ms`; `consume()` open-branch re-checks (§2.3) | `PreflightToken` non-constructible/copyable/picklable; `_MINT`/`_authorizations` registry; `mint_reduce_only_token` **byte-identical (not edited)**; reduce consume byte-identical |
| `scripts/agent/broker/base.py` | `require_token` open branch binds side+qty+limit_price (§E); `Broker` Protocol += `kind: str`, `cancel_order(order_id) -> Mapping`, `order_status(order_id) -> Mapping`; `BrokerBase.cancel_order/order_status` default `NotImplementedError` | `OrderIntent` unchanged; `submit_order` body unchanged (`require_token` then `_place`); reduce branch of `require_token` byte-identical |
| `scripts/agent/broker/alpaca.py` | three-mode ctor (§G); `kind="alpaca_paper"`; `_place` wire-payload build + synthetic refusal (wall 2); `cancel_order`/`order_status`; `AlpacaAccountProvider`; `BrokerHttpError` | default no-arg ctor stays the M0 spy/no-op byte-identical (plus the synthetic refusal, unreachable by M0 tests) |
| `config/agent_rules.json` | new `"execution"` block (§B) | gates byte-identical `false`; `latency_budget_ms: 250` unchanged |
| `requirements.txt` | `alpaca-py==0.43.4` UNCOMMENTED (+ header note updated) | `databento==0.79.0` unchanged |
| `tests/lib/fakes.py` | `SpyBroker` += `kind="spy"`, `cancel_order`/`order_status` recorders | `SpyBroker.calls` entry-recording semantics |

**NEW files:**

```
scripts/agent/
├── exec_reasons.py          # §A — every §2 vocabulary + ExecError                     [stdlib only]
├── execution_config.py      # §B — ExecutionConfig.from_config, the ONE parser         [stdlib, agent.serializer]
├── order_pricing.py         # §C — tick_for / on_tick_grid / marketable_limit_cap (PURE)
│                            #                                                          [stdlib, agent.quote_quality (types), exec_reasons]
├── execution_realism.py     # §I — DepthSnapshot/DepthView, model_fill, divergence (PURE)
│                            #                                                          [stdlib, agent.serializer, agent.quote_quality, exec_reasons]
├── fees.py                  # §J — FeeModel (SEC §31 + FINRA TAF code constants)       [stdlib only]
├── paper_book.py            # §K — PaperBook/PaperPosition lifecycle + PnL split       [stdlib, agent.serializer, agent.quote_quality (types), exec_reasons, fees]
├── exec_ledger.py           # §P — validating facades for orders/fills/positions + rehydrate folds
│                            #                                                          [exec_reasons, recorder.persistence, agent.journal, agent.serializer]
├── backtest_gate.py         # §L.2 — ArtifactCheck / verify_artifact (S9)              [stdlib, agent.serializer]
├── run_lock.py              # §M.1 — PID lock, one writer per journal tree             [stdlib only]
├── secrets_runtime.py       # §O.2 — load_run_gates / load_alpaca_paper_credentials    [stdlib only]
├── orchestrator.py          # §M — the IMPURE composer: startup/rehydrate, tick loop,
│                            #       OrderTask FSM, watcher, kill wiring, close path
├── __main__.py              # §O.1 — CLI: observe | synthetic | paper
├── broker/
│   ├── order_state.py       # §F — BrokerOrder/OrderInvalid/BrokerRejection parse chokepoint,
│   │                        #       ALPACA_STATUS_MAP, fill_delta, OrderApi Protocol   [stdlib, agent.serializer, exec_reasons]
│   ├── fake.py              # §H.1 — FakeBroker (BrokerBase subclass; deterministic)   [agent.broker.base, broker.order_state, stdlib]
│   └── flatten_proxy.py     # §H.2 — PriceCappedFlattenBroker (FD-M5-1)                [agent.broker.base (OrderIntent), order_pricing, exec_reasons]
├── marketdata/replay_feed.py# §N — ReplayQuoteFeed + ReplayClock + DepthView builder   [stdlib, agent.quote_quality, agent.bar_series, recorder.persistence/event_row/book_state/book_hash]
└── strategies/synthetic.py  # §L.1 — SyntheticStrategy base + ScriptedSyntheticStrategy
                             #       + ExitInstruction/ExitProvider                     [stdlib, agent.candidate, agent.strategy]

artifacts/backtests/.gitkeep # the S9 dir, shipped EMPTY (FD-M5-8)
tests/lib/alpaca_fixtures.py # §Q — payload builders + ScriptedOrderApi
tests/lib/exec_fixtures.py   # §Q — permissive fixture config, quote-pair/book/artifact builders
```

**Import discipline (extends M3 FD-12 / M4 FD-M4-24; §R test 11 implements EXACTLY this):**

- The pure pricing/labeling/booking family — `order_pricing.py`, `execution_realism.py`, `fees.py`,
  `paper_book.py`, `exec_ledger.py`, `backtest_gate.py`, `execution_config.py`, `exec_reasons.py`,
  `marketdata/replay_feed.py`, `run_lock.py`, `secrets_runtime.py` — must NOT import (any scope)
  `agent.broker*`, `agent.execution_preflight`, `agent.kill_switch`, `agent.arming`, and must not
  reference `submit_order`, `mint_open_token`, `mint_reduce_only_token`, `OrderIntent`,
  `OpenPreflightToken`, `ReduceOnlyPreflightToken`, `PreflightToken`, `require_token`, `consume`, nor
  `importlib`/`__import__` at all. They price, label, and book; they never submit.
- `strategies/` (synthetic.py AND calibration_probe.py) keep the full M3 FD-12 closed set: no
  `agent.broker*` (FakeBroker included), no `agent.execution_preflight`, no `agent.kill_switch`, no
  `agent.arming`.
- `execution_preflight.py` must NOT import `agent.broker*` (no cycle: the ladder consumes
  `Candidate`/`Leg`, never `OrderIntent`).
- `orchestrator.py` is the ONLY module that may import both `agent.risk.*` and `agent.broker.*` (and
  the preflight mint). `__main__.py` imports `orchestrator`, `config`, `secrets_runtime` only.
- `alpaca` (the SDK) appears in exactly ONE function body (`broker/alpaca.py::_build_real_client`) and
  nowhere at module scope (AST + `sys.modules` purity tests extend with `"alpaca"`).
- No module under `scripts/agent/risk/` and no M3 module is edited. `kill_switch.py`, `gates.py`,
  `config.py`, `arming.py`, `journal.py`, `serializer.py` are NOT edited.

## A. `scripts/agent/exec_reasons.py` — vocabularies + `ExecError`

```python
# scripts/agent/exec_reasons.py
"""Closed vocabularies for the M5 execution tier. Out-of-vocab anywhere -> ExecError
(FATAL, fail-closed, never coerced) — mirrors MarketStateError / RiskError."""
from typing import FrozenSet, Tuple

class ExecError(ValueError):
    """Execution-tier invariant violation (out-of-vocab reason/state/cause, identity
    mismatch, tampered risk-verdict row, malformed collaborator input) -> FATAL.
    A rejectable market/account condition is DATA and never raises ExecError."""

TERMINAL_PREFLIGHT_REASONS: FrozenSet[str] = frozenset({
    "run_gates_off", "kill_switch_halted", "missing_decision_stamp"})
COLLECTED_PREFLIGHT_REASONS: FrozenSet[str] = frozenset({ ... })   # the 34 of §2.1, verbatim
CONSUME_REASONS: FrozenSet[str] = frozenset({
    "preflight_runtime_unbound", "open_token_expired", "kill_generation_changed"})
RESERVED_PREFLIGHT_REASONS: FrozenSet[str] = frozenset({
    "ssr_short_blocked", "locate_unavailable", "extended_hours_blocked"})
PREFLIGHT_REASONS: FrozenSet[str] = (TERMINAL_PREFLIGHT_REASONS | COLLECTED_PREFLIGHT_REASONS
                                     | CONSUME_REASONS | RESERVED_PREFLIGHT_REASONS)   # 42 members

PREFLIGHT_STAGES: Tuple[str, ...] = (
    "run_gates", "kill", "stamp", "candidate", "strategy_gate", "inflight",
    "latency", "quote", "market_state", "order", "risk")
EXTRA_REJECT_REASONS: FrozenSet[str] = frozenset({"no_price_for_cap", "broker_rejected"})
REJECT_STAGES: FrozenSet[str] = frozenset(PREFLIGHT_STAGES) | {"consume", "broker", "reduce_pricing"}

# ... every §2.4 frozenset verbatim: ORDER_STATES, TERMINAL_STATES, CANCEL_CAUSES,
# CANCEL_OUTCOMES, CLOSE_REASONS, REALISM_CLASSES, MODELED_FILL_MODELS, DIVERGENCE_FLAGS,
# FILL_POLICIES, BROKER_KINDS, STRATEGY_DECISION_ACTIONS, FILL_SOURCES, SUBMIT_RESOLUTIONS

def require_reason(code: str) -> str: ...     # ExecError on non-membership / reserved-in-M5
def require_stage(name: str) -> str: ...
def require_member(vocab: FrozenSet[str], value: str, *, what: str) -> str: ...
```

`KILL_STATES`/`Tradability`/`SessionState` are NOT duplicated here — they are imported from their M4/M2
homes by consumers (one vocabulary, one home).

## B. `scripts/agent/execution_config.py` + committed config additions

```python
# scripts/agent/execution_config.py
"""ExecutionConfig: the ONE parser of agent_rules.execution + latency_budget_ms
(SignalConfig posture: closed key sets, fail-loud ValueError at startup, parsed once)."""
from dataclasses import dataclass
from decimal import Decimal

LATENCY_BUDGET_MIN_MS = 250        # CODE CONSTANT — floors the committed value (FD-M5-10:
                                   #   min()-merge polarity is INVERTED for latency_budget_ms)
MIN_REQUOTE_DELTA_MS = 1           # quote B must post-date quote A by >= 1 ms (§2.2 stage 7)
OPEN_TOKEN_TTL_MS = 2000           # consume-time TTL (FD-M5-13); strict '>'
RISK_VERDICT_TTL_MS = 2000         # mint-time verdict freshness (FD-M5-30); strict '>'
ORDER_POLL_INTERVAL_MS_MAX = 1000  # ceiling clamp: effective = min(parsed, 1000)
SUBMIT_RECOVERY_ATTEMPTS = 3       # FD-M5-17
DIVERGENCE_ALERT_BPS = Decimal("10")   # FD-M5-6
FLATTEN_CAP_BPS = Decimal("100")       # kill-path wide cap (FD-M5-26); strategy closes use slippage_cap_bps
DEPTH_FRESHNESS_TTL_MS = 2000          # DepthSnapshot age bound (strict '>'); stale ⇒ degrade to tob

_EXECUTION_KEYS = frozenset({"slippage_cap_bps", "order_poll_interval_ms",
                             "account_refresh_interval_ms", "max_open_orders"})

@dataclass(frozen=True)
class ExecutionConfig:
    slippage_cap_bps: Decimal              # from JSON int; > 0
    order_poll_interval_ms: int            # ceiling-clamped
    account_refresh_interval_ms: int       # must be < 5000 (ACCOUNT_FRESHNESS_TTL_MS) at parse
    max_open_orders: int                   # parser REJECTS != 1 in M5 (FD-M5-21)
    effective_latency_budget_ms: int       # max(agent_rules.latency_budget_ms, LATENCY_BUDGET_MIN_MS)
    quote_staleness_ms_max: int            # re-read from signal block (one source)
    spread_bps_max: Decimal                # re-read from signal block (one source)
    rules_hash: str                        # of the WHOLE assembled config (config.py:17 semantics)

    @classmethod
    def from_config(cls, config: dict) -> "ExecutionConfig": ...
        # config = the assembled {"agent_rules":..., "risk_rules":...} dict (M4 RiskConfig shape;
        # the M4C-9 note transfers: this rules_hash matches RiskConfig's, not M3's — joins key on
        # run_id/decision_id). Unknown/missing keys in agent_rules.execution -> ValueError;
        # ints must be JSON ints (bool excluded) > 0; latency_budget_ms must be a JSON int > 0.
```

Committed `config/agent_rules.json` gains (everything else byte-identical):

```json
"execution": {
  "slippage_cap_bps": 25,
  "order_poll_interval_ms": 500,
  "account_refresh_interval_ms": 2500,
  "max_open_orders": 1
}
```

Polarity table (M2 §G / M4 FD-M4-22 discipline, applied):

| Quantity | Home | Merge polarity / guard |
|---|---|---|
| `slippage_cap_bps` (25) | config JSON int | smaller = tighter worst-price cap = safer ⇒ `min()` correct |
| `order_poll_interval_ms` (500) | config JSON int | smaller = fresher watch = safer ⇒ `min()` correct; code CEILING 1000 |
| `account_refresh_interval_ms` (2500) | config JSON int | smaller = fresher = safer ⇒ `min()` correct; parse-time `< 5000` check (must outrun `ACCOUNT_FRESHNESS_TTL_MS`) |
| `max_open_orders` (1) | config JSON int | smaller safer ⇒ `min()` correct; parser rejects ≠ 1 anyway |
| `latency_budget_ms` (250, existing top-level) | config JSON int | **INVERTED** — lowering loosens realism ⇒ code FLOOR `LATENCY_BUDGET_MIN_MS=250` |
| TTLs, requote delta, recovery attempts, divergence/flatten bps, fee rates | CODE CONSTANTS | regulatory facts and inverted-polarity values are not knobs (FD-M5-15/29) |

Canary obligations (extends `test_config_canary.py`, loses nothing): the four execution values read as
committed; a hostile overlay (huge `slippage_cap_bps`, `latency_budget_ms: 0`, `max_open_orders: 99`,
injected keys) merges back ineffective; gates still identity-False.

## C. `scripts/agent/order_pricing.py` — tick grid + marketable-limit cap (PURE)

```python
# scripts/agent/order_pricing.py
"""Reg-NMS Rule 612 / Alpaca A2 tick grid + the frozen price-cap formula. PURE Decimal
math under quote_quality's pinned context discipline; floats raise (S2)."""
from decimal import Decimal, ROUND_DOWN, ROUND_UP

def tick_for(price: Decimal) -> Decimal: ...
    # price >= Decimal("1.00") -> Decimal("0.01"); 0 < price < 1.00 -> Decimal("0.0001");
    # price <= 0 or non-finite -> ExecError (callers pre-validate; a bug, not data).

def on_tick_grid(price: Decimal) -> bool: ...
    # exact Decimal: price % tick_for(price) == 0

@dataclass(frozen=True)
class CapResult:
    capped_limit: Optional[Decimal]    # ON the tick grid; None iff not derivable
    marketable: bool
    adverse_move_bps: Decimal          # signed, quote-A touch -> quote-B touch, BPS_QUANTUM
    reasons: Tuple[str, ...]           # sorted ⊆ {unpriceable_candidate, invalid_tick,
                                       #            not_marketable, latency_lost_edge}

def marketable_limit_cap(*, side: str, quote_a: QuoteSnapshot, quote_b: QuoteSnapshot,
                         slippage_cap_bps: Decimal,
                         strategy_limit: Optional[Decimal]) -> CapResult: ...

def reduce_cap(*, side: str, quote: QuoteSnapshot, cap_bps: Decimal) -> Optional[Decimal]: ...
    # the close/flatten pricing helper (FD-M5-1/26): sell -> bid×(1−bps/1e4) quantized
    # ROUND_DOWN to grid; buy-to-cover -> ask×(1+bps/1e4) quantized ROUND_UP; the needed
    # side missing/non-finite/<=0 -> None (caller maps to no_price_for_cap).
```

**Frozen cap formula (the load-bearing piece):**

- **BUY (open):** `raw = ask_B × (1 + slippage_cap_bps/10000)` (multiply under the pinned
  `Context(prec=28, ROUND_HALF_EVEN)` — the `quote_quality._DECIMAL_CTX` precedent);
  `cap = raw.quantize(tick_for(raw), ROUND_DOWN)` — directed rounding TOWARD the budget, never past
  it; `capped_limit = min(cap, strategy_limit)` when the strategy supplied its own worst price.
  **Marketable iff `capped_limit >= ask_B`** (boundary-equal IS marketable) — else `not_marketable`
  (M5 never rests orders).
- **SELL (close/flatten path):** `raw = bid_B × (1 − slippage_cap_bps/10000)`;
  `cap = raw.quantize(tick_for(raw), ROUND_UP)`; `capped_limit = max(cap, strategy_limit)`;
  marketable iff `capped_limit <= bid_B`.
- `adverse_move_bps`: buy = `(ask_B − ask_A)/ask_A × 10000` quantized `BPS_QUANTUM` ROUND_HALF_EVEN
  (sell mirrors on bids, sign flipped so adverse is positive); `latency_lost_edge` iff
  `adverse_move_bps > slippage_cap_bps` (strict). `invalid_tick` iff `strategy_limit` is not on its own
  grid. `unpriceable_candidate` iff `strategy_limit <= 0`, or the needed quote-B side is missing /
  non-finite / `<= 0` while the quote stage somehow passed (belt-and-braces; normally stage 8 owns it).
- The submitted Alpaca `limit_price` **is** `capped_limit` — a late fill after any post-submit state
  change stays price-bounded by construction (parent Tier 6: never a bare market order). The cap
  crossing the $1.00 boundary uses `tick_for(raw)` of the RAW value (frozen; sub-dollar fixtures in §Q
  exercise the 4dp grid + the 42210000 mirror).

## D. `scripts/agent/execution_preflight.py` — the mint rebuild

```python
# additions to scripts/agent/execution_preflight.py (registry/token mechanics UNTOUCHED)
@dataclass(frozen=True)
class DecisionStamp:
    decision_id: str
    decision_ts_utc: str             # parseable ISO-8601 UTC
    decision_seen_at_ms: int         # injected monotonic stamp at decision time (t0)
    quote_a: QuoteSnapshot           # the quote the strategy decided on (provenance-stamped)

@dataclass(frozen=True)
class PreflightInputs:
    run_id: str
    stamp: Optional[DecisionStamp]
    candidate: "Candidate"                       # single-leg in M5 (len != 1 -> ExecError)
    strategy_id: str
    strategy_is_synthetic: bool                  # computed by the orchestrator (isinstance)
    quote_b: Optional[QuoteSnapshot]
    quote_b_verdict: Optional[QuoteVerdict]      # quote_quality.evaluate(quote_b, now_ms=now_ms, ...)
    feed_epoch_now: int
    market_state: "market_state.Verdict"         # FRESH MarketStateCache.get at preflight time
    risk_verdict: Optional["RiskVerdict"]
    risk_verdict_row: Optional[dict]             # the journaled row (FD-M5-30)
    risk_verdict_now_ms: Optional[int]           # the now_ms the caller passed to can_open
    kill_state: str
    kill_generation: int
    open_orders_in_flight: int
    artifact_check: "ArtifactCheck"              # backtest_gate.verify_artifact result (§L.2)
    broker_kind: str                             # ∈ BROKER_KINDS (defense-in-depth input)
    gates_config: dict                           # the assembled gates view (§O.2 in paper mode)
    exec_config: ExecutionConfig
    now_ms: int

@dataclass(frozen=True)
class PreflightReject:
    reasons: Tuple[str, ...]                     # sorted, deduped, ⊆ PREFLIGHT_REASONS
    gate_stage: Optional[str]                    # terminal stage, or None (phase 2 reached)
    stages_skipped: Tuple[str, ...]              # frozen PREFLIGHT_STAGES members, in order
    preflight_id: str                            # §P.3 deterministic id
    capped_limit: Optional[Decimal]              # set iff stage 'order' was reached and derivable
    detail: Mapping                              # {"risk_reasons": [...]|None, "quote_reasons": [...]|None}

@dataclass(frozen=True)
class PreflightPass:
    preflight_id: str
    symbol: str; instrument_id: int; side: str
    qty: Decimal
    capped_limit: Decimal                        # THE submitted limit (on grid; §C)
    quote_b_provenance: Mapping                  # §P.2 frozen provenance key set
    latency_observed_ms: int                     # quote_b.seen_at_ms - decision_seen_at_ms
    kill_generation: int
    risk_verdict_id: str

class PreflightStale(PreflightRejected):
    """Raised by consume() on the §2.3 open-branch re-checks. The authorization is
    revoked before raising; the caller journals reject{stage:'consume'}."""

def evaluate_preflight(inputs: PreflightInputs) -> Union[PreflightPass, PreflightReject]: ...
    # PURE: no I/O, no clock read, no journal write (FD-M5-14). Deterministic. ExecError
    # only on invariant breaks (§2.2: out-of-vocab kill state, identity mismatch, multi-leg,
    # tampered risk_verdict_row hash, float in a money slot).

def mint_open_token(inputs: PreflightInputs) -> Tuple[OpenPreflightToken, PreflightPass]: ...
    # THE ONLY open-mint path. Runs evaluate_preflight ITSELF; on PreflightReject raises
    # PreflightRejected with .reject attached (the caller journals); on pass _issue()s
    # _Authorization(kind="open", symbol=..., side=..., qty=...,
    #                limit_price=pass_.capped_limit, kill_generation=inputs.kill_generation,
    #                minted_at_ms=inputs.now_ms)
    # and returns (token, pass_). Registry/nonce/forgery mechanics untouched.

def bind_runtime(*, clock, kill_generation_source: Callable[[], int]) -> None: ...
    # Set ONCE by the orchestrator at startup (§M.2 step 8). Rebinding without
    # unbind_runtime() raises ExecError (no silent swap).
def unbind_runtime() -> None: ...                # tests only (teardown hygiene)
def void_token(token, reason: str) -> None: ...
    # Deletes an unconsumed authorization (idempotent; reason ∈ PREFLIGHT_REASONS ∪
    # EXTRA_REJECT_REASONS for the caller's journal row). Used on kill trips and on
    # minted-but-abandoned paths (e.g. session end between mint and submit).
```

Frozen semantics:

- `_Authorization` grows three OPTIONAL fields (`limit_price=None`, `kill_generation=None`,
  `minted_at_ms=None`) so reduce authorizations are constructed exactly as in M0 — byte-identical
  reduce behavior is a test, not a hope.
- `consume()` open-branch re-checks per §2.3 run BEFORE the registry delete on success and the
  authorization is deleted on `PreflightStale` too (revoked, single-use either way).
- The M0 spy default (no runtime bound) means **every** open consume fails
  `preflight_runtime_unbound` — a broker wired outside the orchestrator can never place an open
  (fail-closed default). Only the orchestrator calls `bind_runtime(clock, lambda: risk_kill.generation)`.
- On the committed config `mint_open_token` rejects at rung 1 for EVERY input (S1 unchanged in
  substance — reject-all by ladder instead of by stub); the legacy two-positional reject-all test is
  rewritten to assert exactly this.

## E. `scripts/agent/broker/base.py` — chokepoint growth

```python
def require_token(intent: OrderIntent, token) -> None:
    # reduce branch: BYTE-IDENTICAL to M0 (symbol/side/qty vs stored auth; consume).
    # open branch (tightened): auth.kind == "open" AND auth.symbol == intent.symbol
    #   AND auth.side == intent.side AND auth.qty == intent.qty
    #   AND auth.limit_price == intent.limit_price       # mutating the intent after mint
    #   else PreflightForgery.                           #   is a forgery, not a reject
    # then consume(token)  -> §2.3 re-checks fire here for open kind (PreflightStale).

@runtime_checkable
class Broker(Protocol):
    kind: str                                            # ∈ BROKER_KINDS
    def submit_order(self, intent: OrderIntent, token) -> object: ...
    def cancel_order(self, order_id: str) -> Mapping: ...    # NO token: cancel is risk-
    def order_status(self, order_id: str) -> Mapping: ...    #   reducing-or-neutral, never gated
    def positions(self) -> object: ...
    def account(self) -> object: ...

class BrokerBase:
    kind = "spy"                                  # subclasses override
    # submit_order body UNCHANGED: require_token(intent, token); return self._place(intent)
    def cancel_order(self, order_id): raise NotImplementedError
    def order_status(self, order_id): raise NotImplementedError
```

`cancel_order`/`order_status` take the **client_order_id** (= our `order_id`, FD-M5-7) and return RAW
payload dicts; parsing happens at the §F chokepoint. The cancel path performs no mint and no
`require_token` (test-asserted, FD-M5-23).

## F. `scripts/agent/broker/order_state.py` — the ONE order-payload parser

```python
ALPACA_STATUS_MAP = {                       # FROZEN; total over the 16 verified strings (A4)
  "new": "accepted", "accepted": "accepted", "pending_new": "accepted",
  "partially_filled": "partially_filled", "filled": "filled",
  "canceled": "canceled", "pending_cancel": "pending_cancel",
  "expired": "expired", "rejected": "rejected", "done_for_day": "done_for_day",
  "replaced": "unknown", "pending_replace": "unknown", "suspended": "unknown",
  "accepted_for_bidding": "unknown", "stopped": "unknown", "calculated": "unknown",
}
# any string not in the map -> "unknown". unknown is NEVER terminal, never filled:
# order_state_alert + best-effort cancel + keep polling (FD-M5-16).

@dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: str; client_order_id: str; symbol: str; side: str
    state: str                                  # ∈ ORDER_STATES
    raw_status: str                             # the Alpaca string, provenance
    qty: Decimal; filled_qty: Decimal
    filled_avg_price: Optional[Decimal]         # Decimal(str(v)); float-typed JSON ⇒ OrderInvalid
    limit_price: Optional[Decimal]
    ts_broker_utc: Optional[str]                # updated_at|filled_at|canceled_at best available
    source: str                                 # ∈ FILL_SOURCES

@dataclass(frozen=True)
class OrderInvalid:
    reason: str            # e.g. "float_typed:filled_avg_price", "filled_qty_regression",
    raw: Mapping           #      "missing_field:status", "avg_price_without_fill"

@dataclass(frozen=True)
class BrokerRejection:
    http_status: Optional[int]; code: Optional[int]; message: str
    pdt_marker_matched: bool      # code 40310100 OR a frozen marker substring,
                                  # case-insensitive (the M4 FD-M4-15 set; finalized at M5-2a)

def parse_order_payload(payload: Mapping, *, source: str) -> Union[BrokerOrder, OrderInvalid]: ...
    # required keys: id, client_order_id, status, symbol, side, qty, filled_qty;
    # money via Decimal(str(v)); float instance / bool in a money slot / non-finite /
    # negative qty ⇒ OrderInvalid (S2 at the broker seam — the M4 §B parser posture;
    # never an exception on the read path, never a constructed order).

@dataclass(frozen=True)
class FillDelta:
    delta_qty: Decimal
    delta_cost_usd: "BrokerUSD"     # EXACT integrated notional (FD-M5-18)
    cum_filled_qty: Decimal
    filled_avg_price_after: Decimal

def fill_delta(prev: Optional[BrokerOrder], cur: BrokerOrder) -> Union[Optional[FillDelta], OrderInvalid]: ...
    # None when filled_qty unchanged; FD-M5-18 formula; regression / avg-without-fill ⇒ OrderInvalid.

@runtime_checkable
class OrderApi(Protocol):           # RAW wire dicts (the AccountReadProvider precedent, LD8)
    def submit(self, payload: Mapping) -> Mapping: ...        # POST /v2/orders
    def get_by_client_order_id(self, client_order_id: str) -> Mapping: ...
    def cancel(self, broker_order_id: str) -> Mapping: ...    # DELETE /v2/orders/{id}
    def get_account(self) -> Mapping: ...                     # GET /v2/account
    def list_positions(self) -> list: ...                     # GET /v2/positions
    def list_open_orders(self) -> list: ...                   # GET /v2/orders?status=open
```

## G. `scripts/agent/broker/alpaca.py` — three-mode adapter (FD-M5-5/7; A7/A8)

```python
_PAPER_HOST = "https://paper-api.alpaca.markets"     # CODE CONSTANT (A7)

class BrokerHttpError(Exception):
    status_code: int          # 403 / 422 / ...
    code: Optional[int]       # Alpaca error code, e.g. 42210000, 40310100
    message: str

class AlpacaPaperBroker(BrokerBase):
    kind = "alpaca_paper"
    def __init__(self, *, order_api: Optional[OrderApi] = None,
                 credentials_loader: Optional[Callable[[], Mapping]] = None) -> None: ...
    # NEITHER set  -> M0 spy/no-op byte-identical (records to self.submitted; existing
    #                 tests untouched) — plus the wall-2 synthetic refusal below.
    # order_api    -> offline fixture-shaped adapter: the FULL lifecycle code path
    #                 (payload build, parse chokepoint, polling, cancel) with zero network
    #                 (tests drive it via ScriptedOrderApi).
    # creds only   -> _build_real_client(): the ONLY place that may `import alpaca`
    #                 (alpaca-py 0.43.4) and call credentials_loader (which reads
    #                 .secrets/alpaca_paper.json via secrets_runtime). base_url is PINNED
    #                 to _PAPER_HOST — a loader returning any other base_url raises
    #                 ValueError at construction (this class is structurally paper-only;
    #                 AlpacaLiveBroker is a separate M8 class behind construct_live_broker).
    def _place(self, intent: OrderIntent) -> Mapping: ...
    def cancel_order(self, order_id: str) -> Mapping: ...
    def order_status(self, order_id: str) -> Mapping: ...

class SyntheticConfinementError(ExecError): ...

class AlpacaAccountProvider:        # the M4 AccountReadProvider adapter (LD8 near-passthrough)
    def __init__(self, api: OrderApi) -> None: ...
    def account_payload(self) -> Mapping: ...     # raw GET /v2/account (M4 §B field names)
    def positions_payload(self) -> list: ...      # raw GET /v2/positions
```

Frozen semantics:

- **Wall 2 (FD-M5-8/28), first line of `_place` in ALL modes (spy included):**
  `if intent.intent_id.startswith("synthetic-"): raise SyntheticConfinementError` — the
  position-of-record seam itself refuses synthetic flow even against a forged-token white-box attempt.
  M0 tests never construct such intent_ids, so spy behavior is otherwise byte-identical.
- **Wire payload (frozen fixture shape, A8):** `{"symbol", "qty": "<int-str>", "side", "type":
  "limit", "time_in_force": "day", "limit_price": "<Decimal-str>", "extended_hours": false,
  "client_order_id": "<order_id>"}`. There is NO market-order payload shape in the codebase
  (FD-M5-1 structural); `limit_price is None` ⇒ `BrokerHttpError`-shaped local rejection
  ("unpriceable") BEFORE any wire attempt — on the kill path the M0 loop isolates it into
  `failed[]`/`residual` (never a blocked reduce, never a bare market order).
- **Order-api mode networking discipline:** every call site catches `BrokerHttpError` and returns it
  as DATA to the orchestrator (which journals `broker_reject`); 403 (insufficient BP), 422/42210000
  (sub-penny), and `status:"rejected"` each have committed fixtures (§Q). A rejection whose
  `pdt_marker_matched` is True is forwarded to the M4 `LegacyPdtCompatMode` rejection-latch hook
  (FD-M4-15 — the wiring M4 reserved for M5).
- `positions()`/`account()` return RAW payloads → fed straight to M4's
  `parse_account_payload`/`parse_positions_payload` with `source="alpaca_paper"`.
- The real-client path converts SDK models to plain dicts so the SAME §F parser chokepoint runs on
  fixture and live payloads alike (verified at M5-2a).

## H. `broker/fake.py` + `broker/flatten_proxy.py`

### H.1 `FakeBroker` — the S9 actuator (offline, deterministic)

```python
class FakeBroker(BrokerBase):
    kind = "fake"
    def __init__(self, *, quote_view: "QuoteView", clock,
                 starting_cash: Decimal = Decimal("100000"),
                 fill_policy: str = "immediate_full") -> None: ...   # ∈ FILL_POLICIES
```

- Deterministic Alpaca-shaped lifecycle with NO randomness: a marketable buy
  (`limit >= current ask`) fills at the ASK (mirrors A5 NBBO matching) for the full qty
  (`immediate_full`) or scripted 30%-then-remainder over two polls (`partial_then_full` — A5's random
  partials made deterministic); `never_fill` stays `accepted` until canceled/expired; `reject_all`
  returns `status:"rejected"` payloads. Sells mirror at the bid.
- Emits the §G wire-shaped order dicts and Alpaca-fixture-shaped `account()`/`positions()` payloads
  (Decimal-string money, M4 §L shape) so the M4 parsers and M5 ledgers run IDENTICAL code paths.
- Extends `BrokerBase` ⇒ even the FakeBroker is token-gated (S1 holds inside the synthetic E2E).
- **Reverse wall (FD-M5-8):** `_place` raises `SyntheticConfinementError` unless
  `intent.intent_id.startswith("synthetic-")` — a real strategy can never book against the fake.

### H.2 `PriceCappedFlattenBroker` — the FD-M5-1 actuator

```python
class PriceCappedFlattenBroker:     # NOT a BrokerBase — it must not consume the token itself
    kind = "flatten_proxy"
    def __init__(self, *, inner: "Broker", quote_view: "QuoteView") -> None: ...
    def submit_order(self, intent, token):
        # kill_switch.py:34-40 builds reduce intents with limit_price=None. This proxy:
        #   quote = quote_view.latest(intent.symbol, ...)   # STALE ACCEPTED (staleness never
        #                                                   #   blocks a reduce — FD-M4-3)
        #   cap = order_pricing.reduce_cap(side=intent.side, quote=quote, cap_bps=FLATTEN_CAP_BPS)
        #   cap is None  -> raise FlattenUnpriced("no_price_for_cap")   # the M0 per-position
        #                  # isolation catches it into failed[] -> residual -> retry_residual
        #                  # when quotable. NEVER a bare market order (FD-M5-1).
        #   else rebuild OrderIntent(... limit_price=cap ...) — the reduce authorization binds
        #   symbol/side/qty ONLY, so the SAME token stays valid; inner.submit_order consumes it.
    def positions(self): return self._inner.positions()
    def account(self):   return self._inner.account()

class FlattenUnpriced(Exception): ...   # message string IS the journaled reason
```

The proxy adds a PRICE, never a gate; it wraps ONLY the broker handed to
`RiskKillSwitch.trigger`/`retry_residual` (§M.6) — `kill_switch.py` stays unedited (FD-M4-4).

## I. `scripts/agent/execution_realism.py` — modeled fill (PURE; honest L1 scope)

**Honesty pins (FD-M5-6, stated plainly):** the recorded primary feed is EQUS.MINI L1 (`tbbo`) — what
M5 can model EVERYWHERE is top-of-book v1. Depth-VWAP v2 is fully built and tested against the
committed `mbp10_depth_sample.jsonl` fixtures, but the `DepthView` seam stays `None` in observe and
paper modes until L2 recording is provisioned (Robin's Databento realtime decision). Absence of depth
**degrades, never upgrades** (`model="tob_l1_v1"`, journaled); the two model classes are never pooled
in any report. Conservative displayed-liquidity fillability, NOT queue position (L3/MBO is the
documented out-of-scope upgrade, parent §5.1). No probabilistic queue claims anywhere.

```python
@dataclass(frozen=True)
class DepthSnapshot:
    symbol: str; instrument_id: int
    bids: Tuple[Tuple[Decimal, Decimal], ...]   # (px, sz) best-first, from EquityBookState.snapshot()
    asks: Tuple[Tuple[Decimal, Decimal], ...]
    book_hash: str                               # recorder book_hash — provenance gate (parent PR-4)
    seen_at_ms: int; reconnect_epoch: int
    dataset: str; schema: str                    # schema MUST be "mbp-10" (else ExecError)

@runtime_checkable
class DepthView(Protocol):
    def latest_book(self, symbol: str, instrument_id: int) -> Optional[DepthSnapshot]: ...

@dataclass(frozen=True)
class ModeledFill:
    modeled_fill_id: str
    model: str                                   # ∈ MODELED_FILL_MODELS
    realism_class: str                           # ∈ REALISM_CLASSES (FD-M5-19)
    requested_qty: Decimal
    modeled_fillable_qty: Decimal                # displayed-size-bounded qty at/inside the touch
    modeled_vwap: Optional[ModeledUSD]           # per-share, quantized MID_QUANTUM (provenance)
    worst_price: Optional[ModeledUSD]            # deepest level consumed, or the cap for remainder
    slippage_vs_mid_bps: Optional[Decimal]       # vs quote_b QuoteVerdict.mid, BPS_QUANTUM
    modeled_cost_usd: Optional[ModeledUSD]       # FULL-qty integrated cost (FD-M5-19), EXACT
    quote: Mapping                               # frozen provenance keys (§P.2) + book_hash|null
    reasons: Tuple[str, ...]                     # sorted ⊆ {"depth_stale","depth_epoch_mismatch",
                                                 #            "no_liquidity_at_cap"}

def model_fill(*, side: str, qty: Decimal, capped_limit: Decimal,
               quote_b: QuoteSnapshot, quote_b_verdict: QuoteVerdict,
               depth: Optional[DepthSnapshot], now_ms: int) -> ModeledFill: ...

def assess_divergence(*, broker_cost_usd: BrokerUSD, filled_qty: Decimal,
                      modeled: ModeledFill) -> "DivergenceResult": ...
    # DivergenceResult{divergence_usd: Decimal|None, divergence_bps: Decimal|None,
    #                  flag ∈ DIVERGENCE_FLAGS, alert: bool}   — FD-M5-20 formulas verbatim;
    # the ONE documented cross-newtype comparison seam (plain Decimal outputs).
```

**Frozen algorithm (buy side; sell mirrors on bids — used by closes):**

- **Structural price-object gate (S5/parent PR-2/PR-4):** only the typed `QuoteSnapshot`/
  `DepthSnapshot` are accepted — a bare number/dict is unrepresentable in the signature; a float in
  any slot raises (S2). Depth identity (`symbol`/`instrument_id`) or schema mismatch ⇒ `ExecError`.
- **Model selection:** depth is USED iff present AND `reconnect_epoch == quote_b.reconnect_epoch`
  AND `now_ms − depth.seen_at_ms <= DEPTH_FRESHNESS_TTL_MS` (strict `>` ⇒ stale). Stale/epoch-
  mismatched depth ⇒ degrade to `tob_l1_v1` with the reason recorded (`depth_stale` /
  `depth_epoch_mismatch`) — never a reject by itself, never an upgrade.
- **`tob_l1_v1`:** `modeled_fillable_qty = min(qty, quote_b.ask_sz)` at `quote_b.ask` iff
  `quote_b.ask <= capped_limit`; remainder `qty − fillable` priced at `capped_limit` (FD-M5-19);
  `modeled_cost_usd = fillable×ask + remainder×capped_limit` EXACT; `modeled_vwap =
  (modeled_cost_usd/qty).quantize(MID_QUANTUM)`; `realism_class = modeled_full` iff remainder == 0
  else `modeled_partial`. `quote_b.ask > capped_limit` (not marketable at the cap) ⇒
  `modeled_unfillable` + reason `no_liquidity_at_cap`, all money fields None.
- **`depth_vwap_l2_v2`:** walk asks with `px <= capped_limit` best-first, integrating
  `min(remaining, displayed_sz)` per level (EXACT integrated notional); `worst_price` = deepest level
  touched; remainder beyond displayed liquidity at the cap (same FD-M5-19 rule); classes as above.
- Computed **once, from quote B** (the quote the preflight validated) at submit time — never quote A,
  never re-shopped after broker fills land (no hindsight). All modeled money is `ModeledUSD`;
  `as_broker_usd` makes writing it into a ledger field a `TypeError` (S5, serializer.py:58-64).
- This already breaks Alpaca paper's infinite-liquidity fiction (A5) — the single biggest realism
  delta M5 ships.

## J. `scripts/agent/fees.py` — `FeeModel` (FD-M5-15; verified rates §0.2 F1/F2)

```python
SEC_SECTION31_RATE   = Decimal("0.0000278")   # per USD of covered SALE proceeds ($27.80/million;
                                              #   FY2025 advisory figure — provenance + re-verify
                                              #   duty in §0.2 F2)
TAF_PER_SHARE_SOLD   = Decimal("0.000166")    # FINRA TAF, covered equities, sells only (VERIFIED
                                              #   2026-06-10, finra.org Schedule A §1)
TAF_CAP_PER_TRADE_USD = Decimal("8.30")       # per-trade cap (VERIFIED 2026-06-10)
FEE_MODEL_VERSION    = "reg_fees_v1"
CENT = Decimal("0.01")

@dataclass(frozen=True)
class FeeAssumption:
    model_version: str            # FEE_MODEL_VERSION
    sec_usd: Decimal; taf_usd: Decimal; total_usd: Decimal

def fees_for(*, side: str, qty: Decimal, notional: Decimal) -> FeeAssumption: ...
    # side == "buy"  -> FeeAssumption(FEE_MODEL_VERSION, 0, 0, 0)
    # side == "sell" -> sec = (notional × SEC_SECTION31_RATE).quantize(CENT, ROUND_CEILING)
    #                   taf = min((qty × TAF_PER_SHARE_SOLD).quantize(CENT, ROUND_CEILING),
    #                             TAF_CAP_PER_TRADE_USD)
    #                   total = sec + taf
    # Ceil-rounding is frozen: fee assumptions round AGAINST us (conservative realistic PnL).
    # qty/notional must be positive finite Decimals (float/bool ⇒ ValueError, S2).
```

**S5-critical split:** Alpaca paper charges NO regulatory fees (A6) ⇒ `broker_account_pnl` stays pure
broker numbers (injecting synthetic fees there would manufacture permanent M6 reconcile drift). The
`FeeAssumption` is stamped on `position_open` (round-trip assumption = open-side 0 + projected close
fees are NOT pre-charged; fees are assessed per SELL fill as it books) and enters ONLY
`execution_realistic_pnl`. On live (M8) broker-reported fees join the broker side.

## K. `scripts/agent/paper_book.py` — lifecycle + PnL split (S5)

```python
@dataclass(frozen=True)
class PaperPosition:
    position_id: str; symbol: str; instrument_id: int
    side: str                                   # "long" only in M5
    qty: Decimal                                # open qty (shrinks on partial closes)
    broker_cost_usd: BrokerUSD                  # Σ FillDelta.delta_cost_usd EXACT (never qty×avg)
    modeled_cost_usd: Optional[ModeledUSD]      # FD-M5-19 full-qty basis; None ⇒ unassessed
    fee_assumption: FeeAssumption               # stamped at open (parent Tier 6)
    opening_order_id: str; strategy_id: str
    opened_ts_utc: str
    status: str                                 # ∈ {"open", "closed"}

class PaperBook:
    def __init__(self, *, ledger: "ExecLedger", run_id: str) -> None: ...
    def open_position(self, *, decision_id, order_id, symbol, instrument_id, strategy_id,
                      fills: Sequence[FillDelta], modeled: Optional[ModeledFill],
                      opened_ts_utc: str) -> PaperPosition: ...
    def apply_fill(self, position_id: str, fill: FillDelta) -> PaperPosition: ...
    def mark(self, position_id: str, quote: QuoteSnapshot, *, now_ms: int,
             bar_key: str) -> Optional[Mapping]: ...
        # STRUCTURAL price-object gate (parent §4.2): only a typed QuoteSnapshot is
        # accepted; staleness/usability re-checked via quote_quality.evaluate with the
        # committed budgets — a stale/unusable quote produces NO new mark (the last mark
        # stands; staleness never closes a position). Long marks at best_bid
        # (conservative liquidation value); mark_source journaled.
    def pnl_snapshot(self, position_id: str, *, bar_key: str) -> Mapping: ...
        # broker_account_pnl = BrokerUSD(qty×mark_bid − broker_cost_usd)      (fee-free, A6)
        # execution_realistic_pnl = ModeledUSD(qty×mark_bid − modeled_cost_usd
        #                                      − fees_assessed_to_date)       or None (unassessed)
        # carries BOTH + realism_class (the FD-M5-20 flag) + the verbatim
        # used_for_strategy_evaluation = "execution_realistic_pnl" (parent §7). Never collapsed.
    def close_position(self, *, position_id, order_id, fills: Sequence[FillDelta],
                       modeled: Optional[ModeledFill], reason: str) -> Mapping: ...
        # reason ∈ CLOSE_REASONS; realized_broker_pnl = exit_notional − cost (EXACT);
        # realized_modeled_pnl = modeled exit − modeled cost − fees_for(sell) — or None;
        # sell-side FeeAssumption assessed HERE (the fees the realistic side pays).
    @staticmethod
    def rehydrate(position_rows, fill_rows) -> Mapping[str, PaperPosition]: ...
        # pure fold by ascending seq: position_open = immutable facts; broker_fill deltas
        # accumulate; position_close ⇒ terminal-skip; mark/pnl rows are derived state and
        # do NOT fold (recomputed live). Rehydrated == live, byte-exact (LD-R5 discipline:
        # every fold-bearing money field is journaled EXACT).
```

- **Typing wall (S5):** every broker-basis assignment passes `as_broker_usd()`; a `ModeledUSD` into a
  broker field is a `TypeError` (white-box test feeds hostile modeled inputs and asserts the broker
  fields unchanged). `BrokerUSD` marks broker-fill lineage (cost from broker fills + market mark);
  `ModeledUSD` marks modeled lineage; the ONE sanctioned cross-newtype comparison is
  `assess_divergence` (§I), whose outputs are plain Decimal.
- The broker stays position-of-record: `PaperBook` never silently mutates; M6's reconcile diff emits
  adjustment rows on divergence (the broker number wins).

## L. S9 — synthetic strategy + backtest-artifact gate

### L.1 `scripts/agent/strategies/synthetic.py`

```python
class SyntheticStrategy:
    """Base for offline E2E drivers. Structural facts (FD-M5-8/28):
    - synthetic = True (class attr, identity-checked);
    - __init_subclass__ raises unless strategy_id.startswith("synthetic.");
    - emitted Candidates MAY carry paper_eligible=True — that means only 'may pass the
      M4 ladder stage 6 under a fixture config'; reaching a REAL broker is blocked by
      walls 1+2 and the artifact gate, which synthetics can never present."""
    synthetic = True
    strategy_id: str                       # MUST start "synthetic."
    def scan(self, ctx: ScanContext) -> Sequence[Candidate]: ...

class ScriptedSyntheticStrategy(SyntheticStrategy):
    strategy_id = "synthetic.scripted_v1"
    def __init__(self, script: Sequence[Mapping]) -> None: ...
        # deterministic script rows: {"on_bar": <bar_key|ordinal>, "action": "open"|"close",
        #  "symbol", "qty": "<int-str>", "limit": "<Decimal-str>"|None} — drives the first
        # open→mark→close E2E (S9) with zero randomness.
    def exits(self, ctx: ScanContext) -> Sequence["ExitInstruction"]: ...

@dataclass(frozen=True)
class ExitInstruction:
    symbol: str; instrument_id: int
    qty: Decimal                  # > 0, <= held (validated downstream by the reduce mint)
    reason: str                   # ∈ CLOSE_REASONS

@runtime_checkable
class ExitProvider(Protocol):     # optional; the orchestrator polls it when present
    def exits(self, ctx: ScanContext) -> Sequence[ExitInstruction]: ...
```

Import guard: `strategies/synthetic.py` imports ONLY stdlib + `agent.candidate` + `agent.strategy`
(wall 3 — it cannot even name a broker or token type).

### L.2 `scripts/agent/backtest_gate.py` (FD-M5-27)

```python
ARTIFACTS_DIR = "artifacts/backtests"     # shipped EMPTY (.gitkeep) — FD-M5-8

@dataclass(frozen=True)
class ArtifactCheck:
    status: str                  # ∈ {"ok", "missing", "key_mismatch", "hash_invalid"}
    artifact_path: Optional[str]
    artifact_hash: Optional[str]

def verify_artifact(strategy_id: str, *, rules_hash: str, data_pin: str,
                    artifacts_dir: str = ARTIFACTS_DIR) -> ArtifactCheck: ...
    # file absent                          -> missing
    # JSON shape wrong / row_hash(payload sans artifact_hash) != artifact_hash -> hash_invalid
    # (strategy_id, rules_hash, data_pin) triple not EXACTLY current           -> key_mismatch
    # else ok. metrics.basis MUST equal "execution_realistic_pnl" (else hash_invalid —
    # the S9 metric pin). data_pin uses the M3 frozen format
    # "{dataset}:{schema}:{interval}:{source_id}".
```

The orchestrator computes `artifact_check` once per (strategy, rules_hash, data_pin) per run and feeds
it into `PreflightInputs` (ladder stage 5). M5 ships NO artifact ⇒ every real strategy rejects
`backtest_artifact_missing` (fail-closed); M7 produces the first artifact + the review/signing runbook.

## M. `scripts/agent/orchestrator.py` — the impure composer

### M.1 Process discipline

`run_lock.py`: a PID lock file at `<journal_dir>/.lock` (fixed path under the journal tree; stdlib
`os.open(O_CREAT|O_EXCL)` + liveness check on the recorded PID); a live lock ⇒ refuse to start (ONE
writer per journal tree). Released on clean exit; a stale lock from a dead PID is reclaimed with a
journaled `status` note.

### M.2 Startup sequence (frozen ORDER; any step failing ⇒ fail-loud exit, nothing submitted)

1. **Lock** — acquire `run_lock`.
2. **Config** — load committed `config/*.json` (+ optional `--overlay` via `tighten_only_merge`);
   paper mode ONLY: assemble the gates view per §O.2; parse `SignalConfig` / `RiskConfig` /
   `ExecutionConfig` (each fail-loud at startup); compute `rules_hash` over the assembled dict.
3. **run_id** — `"run-" + strftime("%Y%m%dT%H%M%SZ") + "-" + row_hash({host, pid, ts_utc})[:12]`
   (CLI; tests inject) — injected into every writer; the recorder shares the namespace (S6).
4. **Ledgers** — open `DecisionLedger` (M3, untouched), `RiskLedger` (M4), `ExecLedger`
   (orders/fills/positions, §P), status writer.
5. **Risk rehydrate — the M4 §O caller contract, honored VERBATIM:** *"the orchestrator MUST fold
   `journal/risk.jsonl` via `rehydrate_risk_state` and seed ALL FOUR stateful components —
   `IntradayMarginModel.rehydrate`, `RiskKillSwitch.rehydrate`,
   `LegacyPdtCompatMode(rehydrated_state=...)` (LD-R3 — the pdt rejection latch is durable across
   runs), `LossLimitsMonitor.rehydrate` — BEFORE the first `AccountStore.put`/`can_open` of a run
   (safety-F14: a fresh-instance-without-rehydrate run would silently start
   unfrozen/unhalted/unlatched); MUST produce a `MarginObservation` after every IML-reducing fill, or
   document the accepted under-detection (RM-8 — EOD-only equals the rule's deficit only when EOD is
   the day's worst point); MUST precompute `PortfolioRead.stale` via `portfolio_is_stale` (FD-M4-22);
   and the runbook MUST cover the operator-attended `retry_residual` pass after a crash-mid-flatten
   rehydrate (safety-F5/M4C-10)."* Test-asserted call ordering (§R 10). M5 discharges RM-8 at fill
   granularity: long-only ⇒ every opening buy fill is IML-reducing (`classify_iml_reducing`) ⇒ one
   `MarginObservation` per buy fill + `close_of_day` at the session-close edge.
6. **Exec rehydrate** — fold `orders/fills/positions.jsonl`: `PaperBook.rehydrate` + the open-order
   set; every non-terminal order ⇒ FD-M5-24 recovery (paper: query-by-`client_order_id`, adopt, one
   best-effort cancel `restart_unknown_state`, resume polling; not found ×3 ⇒
   `order_submit_unconfirmed{not_found}` + open-deny; observe/offline ⇒ `offline_orphan` + open-deny).
7. **HALTED latch** — a rehydrated HALTED kill state ⇒ the run starts halted: observe-only ticks
   continue, opens are structurally impossible (M4 rung 2 + ladder rung 2), and the runbook's
   operator-attended `retry_residual` is the only trade action.
8. **Bind runtime** — `execution_preflight.bind_runtime(clock=clock,
   kill_generation_source=lambda: risk_kill.generation)` (FD-M5-13).
9. **Mode select (derived, never a flag):** observe ⇒ NO broker object constructed (FD-M5-4;
   strongest S1); synthetic ⇒ `FakeBroker` + the in-memory permissive fixture config (FD-M5-3);
   paper ⇒ `AlpacaPaperBroker(credentials_loader=...)` iff the §O.2 gates view is identity-True AND
   `.secrets/alpaca_paper.json` exists — else the run degrades to observe with a loud status row.
   `--mode` only ASSERTS the expectation (fails loud on mismatch); no flag can enable anything.

### M.3 Tick loop (cadence = `signal.refresh_cadence_ms`, committed "1000"; frozen step order)

```
TICK n:
 1 ingest      replay/feed events up to the tick horizon -> QuoteView/DepthView update;
               track feed_epoch_now
 2 market state for refresh_set = universe ∪ held symbols ∪ in-flight-order symbols:
               build TradabilityInputs (calendar schedule + latest NBBO + scripted/UNKNOWN
               status — §N honesty note) -> decider.decide -> MarketStateCache.put
 3 account     (paper/synthetic, every account_refresh_interval_ms): provider payloads ->
               M4 parse chokepoints -> AccountStore.put + RiskLedger.record_account_snapshot
               (the F4 obligation, valid and invalid alike) -> PortfolioRead with
               stale=portfolio_is_stale(...)
 4 bars        completed 1m ET buckets -> FeatureView.refresh -> M3 probe.on_bar_complete
               (ALWAYS runs — the observe core) -> resolver.resolve_due(...)
 5 kill        RiskKillSwitch.evaluate(account, loss_read); accepted cause -> §M.6 sequence
 6 orders      advance every in-flight OrderTask (§M.4) + post-submit watcher (§M.5);
               fills -> exec ledger -> PaperBook -> MarginObservation per buy fill
 7 scan        ONLY when gates view is identity-True AND a non-spy broker is bound AND no
               order in flight (FD-M5-21) AND symbol not in the open-deny set:
               strategy.scan(ScanContext) -> first Candidate -> §M.4 pipeline
 8 exits       ExitProvider.exits(...) -> §M.7 close path (reduce; independent of gates'
               open keys — closes are risk-reducing)
 9 marks       per held position per COMPLETED bar: PaperBook.mark + pnl_snapshot
10 session edge leaving RTH: best-effort cancel of open orders (cause session_end) +
               IntradayMarginModel.close_of_day
```

### M.4 Per-candidate open pipeline (`OrderTask` FSM; frozen)

```
DECIDED(t0)        quote_a := QuoteView.latest; stamp := DecisionStamp(decision_id,
                   wall_ts, clock.now_ms(), quote_a)
   │ journal: strategy_decision(action="would_open")            [orders.jsonl, FD-M5-9]
   │ risk_verdict := engine.can_open(candidate, portfolio, account, market_state=…,
   │     marks={symbol: Mark(quote mid)}, kill_state=…, kill_generation=…, margin_read=…,
   │     pdt_read=…, loss_read=…, now_ms=t0, decision_id=stamp.decision_id)
   │ row := RiskLedger.record_risk_verdict(verdict, decision_id=…)   # BEFORE any mint (M4 §O)
   │ verdict.allowed is not True ⇒ STOP (the risk row is the refusal record; no preflight)
   ▼
AWAIT_LATENCY      due when clock.now_ms() >= t0 + effective_latency_budget_ms
                   (a SCHEDULER ITEM over the injected clock — FD-M5-10; no sleep)
   ▼
REQUOTE(t1)        quote_b := QuoteView.latest (ONE read; no retry — FD-M5-22);
                   quote_b_verdict := quote_quality.evaluate(quote_b, now_ms=t1, …);
                   fresh MarketStateCache.get; artifact_check (cached per run)
   ▼
PREFLIGHT          token, pf := mint_open_token(PreflightInputs(..., now_ms=t1))
   │ PreflightRejected ⇒ journal reject row, STOP
   │ journal: order_submit_attempt (WRITE-AHEAD, before the network call — FD-M5-17)
   ▼
SUBMIT             intent := OrderIntent(symbol, "buy", qty, "marketable_limit", "day",
                       limit_price=pf.capped_limit, is_reducing=False, intent_id=order_id)
                   result := broker.submit_order(intent, token)
   │ PreflightStale ⇒ reject{stage:"consume"}, STOP
   │ BrokerHttpError / status "rejected" ⇒ broker_reject row (+ pdt latch forward), STOP
   │ timeout/ambiguous ⇒ FD-M5-17 recovery (adopt / unconfirmed + open-deny)
   │ journal: order_submitted; modeled := model_fill(... quote_b ...) ⇒ modeled_execution_fill row
   ▼
WATCH              poll order_status every effective order_poll_interval_ms:
                   parse -> state transitions -> broker_order_update rows;
                   fill_delta -> broker_fill rows -> PaperBook (+MarginObservation);
                   §M.5 watcher guards run every poll
   ▼
TERMINAL ∈ TERMINAL_STATES
   │ journal: order_terminal; fill_divergence (+divergence_alert if over threshold);
   │ position_open (first fill already opened it) … pnl_snapshot
   ▼
BOOKED
```

### M.5 Post-submit watcher (FD-M5-23)

Until terminal, every poll evaluates the trigger set: feed `reconnect_epoch != the epoch bound at
quote B`; fresh M2 verdict shows halt/LULD/AUCTION (`halt_luld_auction`), `NOT_TRADABLE`
(`market_state_not_tradable`), or the stale safe default (`market_state_stale_default`); session
leaving RTH (`session_end`); kill trip (`kill_trip`); `unknown` order state (`unexpected_status`). On
trigger: ONE best-effort `broker.cancel_order(order_id)` (no token) + a `post_submit_cancel_attempt`
row `{cause, outcome ∈ CANCEL_OUTCOMES, broker_state_at_attempt}`; keep watching — late fills remain
authoritative `broker_fill` rows (price-bounded by `capped_limit`), the modeled side is flagged, never
back-voided. Cancel re-triggering is suppressed per cause (one attempt per cause per order; a second
distinct cause may attempt again).

### M.6 Kill sequence (FD-M5-25; S8)

On `RiskKillSwitch.evaluate` returning an accepted cause: (1) best-effort cancel EVERY open order
(`post_submit_cancel_attempt{cause:"kill_trip"}`); (2) `void_token` any minted-unconsumed open token
(journaled reject `stage:"consume"`, reason `kill_generation_changed`); (3)
`risk_kill.trigger(cause, PriceCappedFlattenBroker(inner=broker, quote_view=quote_view), portfolio,
evaluation=…, account=wrap(AccountStore.latest_unsafe()), tradability=…)` — flatten attempts ALL
positions (FD-M4-20); a `FlattenUnpriced` symbol lands in `failed[]`/`residual` with reason
`no_price_for_cap` (FD-M5-1) and is retried via the operator-attended `retry_residual` when quotable;
(4) the generation bump invalidates every outstanding open token at consume (FD-M5-13). Position
closes that result book through `PaperBook.close_position(reason="kill_flatten")`.

### M.7 Strategy close path (FD-M5-26)

`ExitInstruction` → journal `strategy_decision(action="would_close")` →
`cap := reduce_cap(side="sell", quote=quote_b, cap_bps=exec_config.slippage_cap_bps)`; `cap is None`
⇒ `reject{stage:"reduce_pricing", reasons:("no_price_for_cap",)}` + retry next tick; else
`token := mint_reduce_only_token(held_position, intent)` (M0, unedited) → write-ahead
`order_submit_attempt(token_kind="reduce_only")` → submit → WATCH → `position_close`. No open
preflight, no risk verdict, no run-gate consultation (closes work even with gates off — they are
risk-reducing; S1 is about opens).

## N. `scripts/agent/marketdata/replay_feed.py` — file-driven feed + clock

```python
class ReplayClock:
    """now_ms = ms offset of the LAST DELIVERED event's ts_recv_utc from stream start.
    Deterministic: 'awaiting the latency budget' = delivering more recorded events until
    the budget has elapsed in RECORDED time. FakeClock-compatible surface (now_ms())."""

class ReplayQuoteFeed:
    def __init__(self, path, *, symbols: Optional[Sequence[str]] = None) -> None: ...
        # Reads an M1 recorder events.jsonl via replay_stream (hash-verified, truncated-tail
        # rule inherited). Quote rows: schema ∈ {"tbbo", "bbo-1s"} -> QuoteSnapshot (field
        # map: bid_px/ask_px/bid_sz/ask_sz -> bid/ask/bid_sz/ask_sz; provenance verbatim;
        # seen_at_ms = ReplayClock offset of ts_recv_utc). Depth rows: schema "mbp-10" ->
        # EquityBookState.apply -> DepthSnapshot (book_hash = the recorded derived_book_hash)
        # exposed via DepthView — used by TESTS ONLY in M5 (FD-M5-6: observe/paper leave the
        # seam None). symbols filter = file symbols ∩ the given set (FD-M5-4).
    def clock(self) -> ReplayClock: ...
    def quote_view(self) -> "QuoteView": ...
    def depth_view(self) -> "DepthView": ...
    def run(self, *, on_tick, on_bar_complete) -> None: ...
        # Bars: the file is resampled ONCE via bar_series.resample_midbars into a
        # MidBarSeriesReader — SAFE because every feature/label read is as-of/watermark-
        # gated (M3 FD-2 anti-lookahead), so preloading cannot leak the future;
        # on_bar_complete fires in recorded-time order; on_tick fires per refresh_cadence_ms
        # of RECORDED time.
```

**Honesty note (frozen):** EQUS.MINI has no `status` schema, so observe/synthetic-mode
`TradabilityInputs` carry halt/LULD/SSR = UNKNOWN unless a fixture status script is injected;
tradability is calendar+NBBO-driven and the M2 decider's fail-closed UNKNOWN handling governs.
Synthetic-mode E2E fixtures script TRADABLE verdicts explicitly. Calendar coverage comes from the
committed fixtures (`--calendar-fixture`, default `nyse_margin_window_v1.json` which covers the
committed tbbo sample's 2026-06-09 dates); out-of-coverage dates fail closed via `UnknownSessionDate`.

## O. Entry point, secrets layout, and the run-gates file

### O.1 `scripts/agent/__main__.py` — CLI (FD-M5-4)

```
PYTHONPATH=scripts python3 -m agent observe   --events <events.jsonl> [--symbols A,B]
        [--journal-dir journal/] [--calendar-fixture <path>] [--report-out <path>] [--ticks N]
PYTHONPATH=scripts python3 -m agent synthetic --events <events.jsonl> [--journal-dir journal/synthetic/]
        [--script <path>] [--calendar-fixture <path>]
PYTHONPATH=scripts python3 -m agent paper     [--overlay <local-overlay.json>] [--journal-dir journal/]
```

- **`observe` (default):** committed config (gates OFF is fine — observe mints nothing and
  **constructs no broker object at all**, FD-M5-4); symbol set = events-file symbols ∩ `--symbols`
  (when given) ∩ `agent_rules.universe.symbols` (when non-empty; committed `[]` ⇒ file-driven —
  read-only, so safe). Runs ingest → bars → features → market-state → M3 probe → resolver →
  calibration report. **Runnable TODAY** on any M1-recorded file or the committed tbbo fixture.
- **`synthetic`:** builds the in-memory permissive fixture config (FD-M5-3 — gates identity-True,
  nonzero caps, one-symbol universe WITH sector/beta metadata; the M4 §L
  `permissive_fixture_config` mirror; NEVER written under `config/`), a `FakeBroker`, and
  `ScriptedSyntheticStrategy`; journals to an isolated dir; refuses (`ExecError`) if the gates path
  would consult the committed config or construct any non-fake broker. The first
  open→mark→close E2E, offline, today.
- **`paper`:** committed config + optional tighten-only overlay + the §O.2 run-gates view +
  `.secrets/alpaca_paper.json`. Until the live quote feed exists (M1-2b) every open preflight rejects
  at the `quote` stage (`quote_missing`/`quote_stale`) — fail-closed and correct; account polling,
  journaling, kill drill, and the reduce path are exercisable. REAL opens additionally require
  Robin's reviewed caps/universe commit (FD-M5-3). `--mode`-style expectations: each subcommand
  asserts its derived mode and exits non-zero on mismatch; no flag can flip a gate.

### O.2 The run-gates file (FD-M5-2 — the ONE new gate-reading surface)

**File:** `.secrets/run_gates.json` (git-ignored — `.secrets/` already is). Shape (frozen):

```json
{ "enabled": true, "paper_trading": { "enabled": true } }
```

**Frozen semantics:**

1. Read by `secrets_runtime.load_run_gates(path)` ONLY from the paper-mode startup path (§M.2 step
   2). Observe and synthetic modes NEVER read it (observe has no broker; synthetic's gates come from
   the in-memory fixture config).
2. It supplies EXACTLY two values: the identity-strict booleans for `agent_rules.enabled` and
   `agent_rules.paper_trading.enabled`. The assembled **gates view** = the committed(+overlay) config
   with ONLY those two keys replaced by the file's values. Everything else — caps, universe, signal,
   execution, risk — comes from committed config under `tighten_only_merge` and CANNOT be touched by
   this file (test: a hostile run-gates file carrying extra keys — caps, universe, latency — has them
   IGNORED; only the two gate keys are ever read out of it).
3. Absent file ⇒ both read False. Malformed JSON, wrong shape, or any non-identity-`True` value ⇒
   both read False (one rule, fail-closed) — plus a loud startup `status` row
   `run_gates_file{present, parse_ok, enabled, paper_enabled}` journaling the resolved reading
   (provenance for every run, including the all-False default).
4. `gates.opening_allowed` (`gates.py`, unedited) is still the ONE evaluator — it runs against the
   assembled gates view; the ladder's rung 1 and `can_open`'s rung 1 therefore answer identically.
5. **S1 canary additions (LD-M5-2):** (a) committed config + ABSENT run-gates file ⇒ the assembled
   view is identity-False and a full paper-composition orchestrator run over hostile replay data with
   a SpyBroker submits NOTHING; (b) the committed config itself remains gate-False byte-identical;
   (c) deleting the file after a gates-on session re-closes everything on the next start (the
   uninstall story: `rm .secrets/run_gates.json` ⇒ reject-all again).
6. Two-key arming is UNTOUCHED: this file is a paper-mode convenience analogous in posture to key B
   (runtime, never committed) but it arms NOTHING live — `live_trading.enabled` stays committed-False
   and `construct_live_broker` still requires both keys (M8).

**`.secrets/` layout after M5 (git-ignored):**

```
.secrets/
├── databento.json        # M1 historical key (existing)
├── alpaca_paper.json     # {"key_id": "...", "secret_key": "...",
│                         #  "base_url": "https://paper-api.alpaca.markets"}   (M5)
├── run_gates.json        # §O.2 (optional; absent ⇒ reject-all)               (M5)
└── (alpaca_live.json)    # key-B territory — M8, never before
```

## P. `scripts/agent/exec_ledger.py` — streams, row shapes, deterministic ids

### P.1 Streams + writer

Three NEW streams — `journal/orders.jsonl`, `journal/fills.jsonl`, `journal/positions.jsonl` —
written EXCLUSIVELY through `ExecLedger`, a validating facade over `recorder.persistence.EventWriter`
(the `StatusLedger`/`RiskLedger` pattern): NO new writer/hash/serialization; one ledger per resolved
path; injected `run_id`; `EXEC_LEDGER_VERSION = 1` as the `"v"` FIRST key; `rules_hash` on every row;
no payload key in `journal._RESERVED` — `decision_id`/`order_id` ride the journal kwargs (S6); closed
vocabularies enforced (`require_member`); Decimals exact; all lists sorted; no set enters a row.
Replay = the shared `replay_stream` (truncated-tail / `JournalCorruption` semantics unchanged, S3).
M3's `decisions.jsonl` / `forecast_scored.jsonl` and M4's `risk.jsonl` are untouched (FD-M5-9).

```python
class ExecLedger:
    def __init__(self, *, orders: EventWriter, fills: EventWriter,
                 positions: EventWriter, rules_hash: str) -> None: ...
    # one kwarg-only record_* method per event type below (StatusLedger shape); each
    # validates vocabularies and field sets, and refuses _RESERVED collisions.
def replay_orders/replay_fills/replay_positions(path) -> list: ...
def rehydrate_exec_state(order_rows, fill_rows, position_rows) -> dict: ...
    # pure fold by ascending seq -> {"open_orders": {order_id -> latest state row},
    #   "positions": PaperBook.rehydrate(...), "open_deny": ()}   (open_deny is per-run,
    #   rebuilt from order_submit_unconfirmed rows of THIS run only)
```

### P.2 Frozen payload field sets (beyond the common `v`, `rules_hash` prefix; kwargs noted)

Frozen provenance sub-dict (every `quote_a`/`quote_b`/`quote` key below):
`{dataset, schema, ts_event_utc, ts_recv_utc, seen_at_ms, reconnect_epoch, vendor_seq|null}`
(+ `book_hash|null` where noted — the §I depth provenance).

**`journal/orders.jsonl`:**

| event_type | payload fields |
|---|---|
| `strategy_decision` | `symbol, instrument_id, strategy_id, strategy_kind ∈ {real, synthetic}, action ∈ STRATEGY_DECISION_ACTIONS, side, qty, strategy_limit\|null, score\|null, paper_eligible, position_id\|null (set on would_close), event_basis, decision_ts_utc, decision_seen_at_ms, quote_a{provenance}` (+`decision_id` kwarg) |
| `reject` | `symbol, instrument_id, strategy_id, stage ∈ REJECT_STAGES\|null, reasons[] (sorted; ⊆ PREFLIGHT_REASONS ∪ EXTRA_REJECT_REASONS), stages_skipped[], detail {risk_reasons[]\|null, quote_reasons[]\|null, broker_code\|null, broker_message\|null}, preflight_id\|null, capped_limit\|null, token_kind ∈ {open, reduce_only}, kill_state, kill_generation, quote_b{provenance}\|null` (+`decision_id`; +`order_id` when one existed) |
| `order_submit_attempt` | `client_order_id, preflight_id\|null (null on reduce path), risk_verdict_id\|null, strategy_id, symbol, instrument_id, side, qty, order_intent {order_type:"marketable_limit", tif:"day", limit_price}, token_kind ∈ {open, reduce_only}, kill_generation, quote_b{provenance}\|null` (+`decision_id`, `order_id`) — the WRITE-AHEAD row (FD-M5-17); submission ≠ fill (parent §7) |
| `order_submitted` | `client_order_id, broker_order_id, state ∈ ORDER_STATES, raw_status, ts_broker_utc\|null, source ∈ FILL_SOURCES` (+`decision_id`, `order_id`) |
| `broker_order_update` | `broker_order_id, from_state, to_state (∈ ORDER_STATES), raw_status, filled_qty, filled_avg_price\|null, ts_broker_utc\|null` (+`order_id`) — one row per observed transition, none on a no-change poll |
| `broker_reject` | `broker_order_id\|null, http_status\|null, broker_code\|null, message, pdt_marker_matched` (+`decision_id`, `order_id`) |
| `order_submit_unconfirmed` | `client_order_id, error, attempts, resolution ∈ SUBMIT_RESOLUTIONS` (+`order_id`) — `not_found`/`offline_orphan` additionally enter the run's open-deny set |
| `post_submit_cancel_attempt` | `broker_order_id\|null, cause ∈ CANCEL_CAUSES, outcome ∈ CANCEL_OUTCOMES, broker_state_at_attempt\|null` (+`order_id`) |
| `order_state_alert` | `broker_order_id\|null, raw_status, note` (+`order_id`) — the unknown-status fail-closed marker (FD-M5-16) |
| `order_terminal` | `terminal_state ∈ TERMINAL_STATES, filled_qty, cum_notional_usd (EXACT), ts_broker_utc\|null` (+`decision_id`, `order_id`) |

**`journal/fills.jsonl`:**

| event_type | payload fields |
|---|---|
| `broker_fill` | `fill_id, broker_order_id, position_id\|null, symbol, side, delta_qty, delta_cost_usd (BrokerUSD, EXACT — drives the ledger), cum_filled_qty, filled_avg_price_after, liquidity_flag\|null, venue\|null, ts_broker_utc\|null, source ∈ FILL_SOURCES` (+`decision_id`, `order_id`) |
| `modeled_execution_fill` | `modeled_fill_id, model ∈ MODELED_FILL_MODELS, realism_class ∈ REALISM_CLASSES, requested_qty, modeled_fillable_qty, modeled_vwap\|null (MID_QUANTUM), worst_price\|null, slippage_vs_mid_bps\|null (BPS_QUANTUM), modeled_cost_usd\|null (EXACT), fees_assumed {model_version, sec_usd, taf_usd, total_usd}, quote {provenance + book_hash\|null}, reasons[] (sorted)` (+`decision_id`, `order_id`) — **label only** |
| `fill_divergence` | `broker_cost_usd, modeled_cost_usd\|null, divergence_usd\|null, divergence_bps\|null, flag ∈ DIVERGENCE_FLAGS` (+`order_id`) — FD-M5-20, flag ALWAYS |
| `divergence_alert` | `divergence_usd, divergence_bps, threshold_bps:"10"` (+`order_id`) — iff over `DIVERGENCE_ALERT_BPS` |

**`journal/positions.jsonl`:**

| event_type | payload fields |
|---|---|
| `position_open` | `position_id, symbol, instrument_id, side:"long", qty, broker_cost_usd (EXACT), modeled_cost_usd\|null (EXACT), fee_assumption {model_version, sec_usd, taf_usd, total_usd}, opening_order_id, strategy_id, opened_ts_utc` (+`decision_id`, `order_id`) — the OPEN row = immutable facts (parent §7 rehydrate contract) |
| `mark` | `position_id, mark_price, mark_source ∈ {best_bid, best_ask}, quote{provenance}, unrealized_broker_usd, unrealized_modeled_usd\|null, bar_key` |
| `pnl_snapshot` | `position_id, broker_account_pnl, execution_realistic_pnl\|null, realism_class ∈ DIVERGENCE_FLAGS, basis {broker:"broker_fills", modeled:"modeled_fill_plus_fees"}, used_for_strategy_evaluation:"execution_realistic_pnl", bar_key` — both classes, never one (S5) |
| `position_close` | `position_id, closing_order_id, exit_qty, broker_exit_notional_usd (EXACT), realized_broker_pnl (EXACT), realized_modeled_pnl\|null (EXACT), fees_assessed {model_version, sec_usd, taf_usd, total_usd}, reason ∈ CLOSE_REASONS` (+`decision_id`, `order_id`) |

**Money-field discipline (the M4 LD-R5 rule, applied):** journaled EXACT/unquantized because
rehydrate or replay-determinism reads them back: `delta_cost_usd`, `cum_filled_qty`,
`filled_avg_price_after`, `cum_notional_usd`, `broker_cost_usd`, `modeled_cost_usd`,
`broker_exit_notional_usd`, `realized_*_pnl`, every `qty`/`limit_price`/`capped_limit`, every
`fees_*` component. Quantize-only provenance (nothing rehydrates from them): `modeled_vwap`/
`worst_price` (`MID_QUANTUM`), `slippage_vs_mid_bps`/`divergence_bps`/`adverse_move_bps`
(`BPS_QUANTUM`), `mark_price` (the quote's own grid), `unrealized_*` (recomputed live, fold-exempt).

### P.3 Deterministic ids (S6 — all via `serializer.row_hash` over a canonical dict with the EXACT key set)

- `decision_id = "d-" + row_hash({run_id, strategy_id, symbol, instrument_id, event_basis})` —
  strategy path; `event_basis` = the completed bar_key that triggered `scan` (M3 bar-key format), or
  `"exit:" + position_id` on the close path. M3 probe ids are a different namespace and unchanged.
- `preflight_id = "pf-" + row_hash({run_id, decision_id, symbol, side, qty, reasons (sorted list; []
  on pass), quote_b_seen_at_ms|null, kill_generation})` — Decimal values as canonical serializer
  strings.
- `order_id = "o-" + row_hash({run_id, decision_id, preflight_id|null})`; on the synthetic pipeline
  the prefix is `"synthetic-o-"` (FD-M5-28). **`client_order_id = order_id`** (FD-M5-7; ≤128 chars
  per A8: 66 / 76 chars). Never reused; a retry is a new decision → new preflight → new id.
- `fill_id = "bf-" + row_hash({order_id, cum_filled_qty_after, cum_notional_after})` — stable under
  polling re-reads (idempotent fill journaling).
- `modeled_fill_id = "mf-" + row_hash({order_id, model, quote_b_seen_at_ms, vendor_seq|null})`.
- `position_id = "pos-" + row_hash({symbol, opening_order_id})` — **run-independent** (positions are
  account-level facts that survive restarts; the FD-M4-13 deficit-id rationale).
- Correlation chain (S6 join tests): `decision_id → risk_verdict (risk.jsonl) → preflight_id →
  order_id/client_order_id → fill_id/modeled_fill_id → position_id`, each row carrying its upstream
  id; the `risk_verdict` row's `seq` precedes the `order_submit_attempt` row's `seq` on the same
  decision (journaled-before-mint, §R 6).

Replaying identical fixtures with the same `run_id` reproduces every id and row byte-for-byte.

## Q. Fixtures (programmatic builders + committed files)

| Fixture | Contents | Used by |
|---|---|---|
| `tests/fixtures/alpaca/order_accepted.json` | wire-shaped order ack (`status:"new"`) — exact documented REST field names | adapter, watcher |
| `tests/fixtures/alpaca/order_fill_sequence.json` | cumulative-aggregate sequence `new → partially_filled(30 @ 100.10) → partially_filled(70 @ 100.18) → filled(100 @ 100.20)` with avg-price drift — provably wrong under `delta_qty×avg`, exact under FD-M5-18 | fill_delta, PaperBook |
| `tests/fixtures/alpaca/order_canceled.json`, `order_pending_cancel.json` | cancel lifecycle (async, A10) | watcher |
| `tests/fixtures/alpaca/order_rejected_subpenny.json` | 422 / code **42210000** (A2) | broker_reject path |
| `tests/fixtures/alpaca/order_rejected_insufficient_bp.json` | 403 "Buying power or shares is not sufficient" (A8) | broker_reject path |
| `tests/fixtures/alpaca/order_rejected_pdt.json` | code **40310100** + PDT marker message | pdt latch forward (FD-M4-15 hook) |
| `tests/fixtures/alpaca/order_unknown_status.json` | a status string outside A4's 16 | FD-M5-16 fail-closed |
| `tests/fixtures/alpaca/account_paper.json`, `positions_paper.json` | M4 §L wire-shape account/positions (Decimal-string money) | AlpacaAccountProvider → M4 parsers |
| `tests/lib/alpaca_fixtures.py` | builders over the above (`order_payload(**overrides)` etc.) + `ScriptedOrderApi(script)` — scripted submit/get/cancel responses incl. timeout injection (`raise BrokerTimeout`) and submit-then-found / submit-then-not-found recovery scripts | adapter, recovery, orchestrator |
| `tests/lib/exec_fixtures.py` | `permissive_paper_fixture_config()` (IN-MEMORY gates-True + nonzero caps + one-symbol universe with sector/beta — FD-M5-3, M4 §L mirror); quote A/B pair builders (clean pass / B-not-later / identical-provenance re-serve / epoch flip / adverse move / sub-$1 / crossed-locked-stale B); `depth_snapshot()` builder over `mbp10_depth_sample.jsonl`; artifact builders (valid triple, tampered hash, mismatched rules_hash/data_pin); run-gates-file builders (valid/malformed/hostile-extra-keys) | preflight, realism, backtest gate, §O.2 tests |
| `tests/fixtures/execution/observe_session_tbbo.jsonl` | GENERATED by a `tests/lib/exec_fixtures.py` builder: schema-exact recorder rows, one symbol, ≥ 60 one-minute buckets (the 51-bar feature gate opens) + a variant with an epoch flip mid-session | observe E2E, S1 canary, watcher |
| `tests/fixtures/execution/golden/` | byte-exact expected orders/fills/positions streams for the synthetic E2E + the observe-run decisions/report golden (M3 golden discipline) | S3/S6 determinism |
| Reuse | `equs_mini_tbbo_sample.jsonl`, `mbp10_depth_sample.jsonl`, `sub_dollar_subpenny_sample.jsonl`, `nyse_margin_window_v1.json`, `nyse_2026_schedule.json`, M4 `risk_fixtures` builders | throughout |

**M5-2a — credentialed verification (Robin-run; mirrors M1's 2a/2b split):** a read-only verifier
`scripts/agent/verify_alpaca_entitlements.py` (lazy SDK import; `.secrets/alpaca_paper.json`; paper
host pinned) fetches account / positions / open orders, asserts the §F/§B field names and the A4
status vocabulary against the live paper API, re-verifies the §0.2 F2/F3 pins, and writes a REDACTED
artifact under `docs/superpowers/verification/` (the M1 precedent). **M5-2b** (live Databento realtime
for real paper opens) stays deferred — blocked on the unprovisioned subscription (M1-2b). Offline
tests never import or run the verifier path.

## R. Test list — each test file → cases → safety invariant

`tests/agent/` (offline, stdlib-only; FakeClock / ReplayClock / SpyBroker / ScriptedOrderApi; extend
`test_config_canary.py` and `test_no_network_no_creds.py` rather than duplicating them):

1. **`test_execution_config.py`** — parser: committed JSON parses; ints must be JSON ints > 0 (bool/
   float/string ⇒ `ValueError`); unknown/missing keys raise; `max_open_orders != 1` raises;
   `account_refresh_interval_ms >= 5000` raises; `order_poll_interval_ms` ceiling-clamped at 1000;
   `effective_latency_budget_ms == max(parsed, 250)` incl. a hostile `latency_budget_ms: 0` overlay
   merging to 0 then FLOORED to 250 (the FD-M5-10 polarity trap, test-pinned); changing any execution
   leaf changes `rules_hash`. [S1-config, FD-M5-29]
2. **`test_order_pricing.py`** — `tick_for` boundaries ($1.00 ⇒ 0.01; $0.9999 ⇒ 0.0001);
   `on_tick_grid` exactness; BUY cap: directed ROUND_DOWN toward budget (hand-computed Decimals);
   SELL cap ROUND_UP; `min/max` with strategy_limit; marketable boundary-equal passes; sub-$1 4dp grid
   (the 42210000 mirror); `latency_lost_edge` strict boundary at exactly `slippage_cap_bps`;
   `reduce_cap` returns None on missing/nonpositive side; float injection raises. [S2, S4-economics]
3. **`test_execution_preflight_m5.py`** — ONE test per §2.1 member (one-bad-input matrix over a
   golden-good `PreflightInputs`); phase-1 terminal ordering (multi-fault input trips the EARLIEST
   stage; `gate_stage` + `stages_skipped` byte-exact); phase-2 collect-all sorted union (e.g.
   safe-default verdict ⇒ exactly `market_state_not_rth + market_state_not_tradable +
   market_state_stale_default` from stage 9); `missing_decision_stamp` hard-rejects on EACH missing
   stamp component; latency boundary (pass at exactly budget, reject at budget−1); `requote_not_later`
   incl. the identical-provenance re-serve; epoch flip A→B AND B→feed_now; quote-quality reasons
   embedded verbatim; risk binding: missing row / stale (strict at 2001) / verdict_id mismatch /
   decision_id mismatch / `allowed=False` (risk reasons in `detail.risk_reasons`, not the vocab) /
   generation mismatch; tampered row hash ⇒ `ExecError`; multi-leg ⇒ `ExecError`; out-of-vocab kill
   state ⇒ `ExecError`; **committed-config canary:** real committed JSON ⇒ byte-exact terminal reject
   for a sweep of inputs; **purity:** same inputs ⇒ identical result + identical `preflight_id`. [S1, S4]
4. **`test_preflight_token.py` (extended)** — mint-on-pass issues an authorization binding
   side+qty+limit_price; intent mutation after mint ⇒ `PreflightForgery`; TOCTOU at consume: kill
   trip between mint and submit (generation bump) ⇒ `PreflightStale` raised INSIDE
   `BrokerBase.submit_order`, zero `_place` calls, authorization revoked; token age 2001 ms ⇒
   `open_token_expired` (2000 passes); unbound runtime ⇒ `preflight_runtime_unbound`; `bind_runtime`
   twice ⇒ `ExecError`; `void_token` idempotent; **paired FD-M4-3 tests:** the reduce mint + consume
   succeed unchanged under EVERY one of these conditions (unbound runtime, expired-clock, bumped
   generation). [S4-TOCTOU, FD-M5-13]
5. **`test_order_state.py`** — `ALPACA_STATUS_MAP` total over all 16 A4 strings; unmapped ⇒
   `unknown`; `unknown` never terminal (watcher keeps polling + `order_state_alert` + cancel attempt);
   parse matrix (float-typed money / bool / non-finite / negative qty / missing keys ⇒ `OrderInvalid`,
   never an exception or a constructed order); `fill_delta` exactness on the avg-drift fixture
   (delta_cost ≠ delta_qty×avg, hand-computed); regression ⇒ `OrderInvalid`; idempotent re-read ⇒
   `None` delta + stable `fill_id`. [S2, FD-M5-16/18]
6. **`test_exec_ledger.py`** — every `record_*` round-trips through `replay_stream` (hash-verified);
   field-set exactness per §P.2 (missing/extra ⇒ raise); out-of-vocab anything raises; reserved
   collisions impossible; truncated-tail / corrupt-line semantics on all three streams (S3);
   deterministic ids byte-stable on replay with the same `run_id`; **journal-before-mint ordering:**
   the `risk_verdict` row `seq` < `order_submit_attempt` `seq` per decision; correlation chain joins
   across all five streams (S6); `rehydrate_exec_state` fold == live state byte-exact. [S2, S3, S6]
7. **`test_paper_book.py`** — exact-integrated-notional vs `qty×avg` divergence on the 3-partial
   fixture; `ModeledUSD` into a broker field ⇒ `TypeError` (`as_broker_usd`); hostile modeled inputs
   leave broker fields unchanged (white-box); `pnl_snapshot` carries BOTH classes + the verbatim
   `used_for_strategy_evaluation`; fees enter ONLY the realistic side (buy open: zero; sell close:
   ceil-rounded SEC+TAF, cap boundary at exactly $8.30); mark refuses bare numbers/stale quotes (last
   mark stands); close splits realized broker/modeled; rehydrate fold byte-exact incl. partial-close.
   [S5, S2, S3]
8. **`test_execution_realism.py`** — tob: full/partial/unfillable matrix incl. `min(qty, ask_sz)` and
   remainder-at-cap (FD-M5-19 hand-computed); depth: 3-level walk on the MBP-10 fixture, exact VWAP,
   `worst_price`, liquidity-short remainder; stale/epoch-mismatched depth ⇒ degrade to tob with
   reasons (never upgrade, never reject); identity/schema mismatch ⇒ `ExecError`; divergence flags +
   alert threshold strict boundary (FD-M5-20); modeled money is `ModeledUSD` end-to-end. [S5, S2]
9. **`test_alpaca_adapter.py`** — spy default byte-identical to M0 (existing assertions re-run);
   wall 2: a `"synthetic-"`-prefixed intent ⇒ `SyntheticConfinementError` in spy AND order_api modes,
   even with a valid forged-path token (white-box); wire payload byte-shape (frozen §G dict; qty as
   int-string; `extended_hours: false`; `client_order_id == order_id`); `limit_price=None` ⇒ local
   rejection BEFORE any api call (FD-M5-1 structural); 403/422/rejected fixtures ⇒ `broker_reject`
   rows with codes; pdt fixture ⇒ latch forward observed; base_url ≠ paper host ⇒ `ValueError`;
   cancel/order_status pass-through + no-token assertion. [S1, S4-broker, FD-M5-8]
10. **`test_orchestrator.py`** — **startup ordering (M4 §O):** rehydrate-seeds ALL FOUR risk
    components BEFORE the first `AccountStore.put`/`can_open` (call-order spy; violation fails);
    `MarginObservation` after every buy fill; `portfolio_is_stale` used for `PortfolioRead.stale`;
    `record_account_snapshot` on every put (F4); HALTED journal ⇒ run starts halted, opens
    structurally impossible, `retry_residual` legal; **latency seam:** clock not advanced ⇒ task not
    due (no submit); advanced to budget ⇒ due; no `time.sleep`/wall-clock reference outside the seam
    classes (AST scan); **FSM:** decide→requote→preflight→submit→watch→book over ScriptedOrderApi;
    one-in-flight discipline; session-edge cancel + `close_of_day`; **recovery:** timeout ⇒ no blind
    resubmit, by-client-order-id adopt, not-found ⇒ unconfirmed + open-deny; restart with dangling
    order ⇒ FD-M5-24 adopt+cancel path. [S4, S6, M4 §O]
11. **`test_no_network_no_creds.py` (extended)** — socket-block over every new module incl. a full
    orchestrator observe run; `alpaca` never in `sys.modules` after the suite; AST guard per §3
    (module list + forbidden imports/tokens + `importlib`/`__import__`), subprocess-isolated fresh
    imports; `.secrets/` never read (loaders only ever see injected tmp paths). [S1, R11-style]
12. **`test_config_canary.py` (extended)** — committed gates still identity-False; execution block
    values as committed; hostile overlay (gates true, slippage 10000, latency 0, max_open_orders 99)
    merges back ineffective; **the full-orchestrator S1 canary:** committed config + ABSENT run-gates
    file + hostile replay data (would_open-rich) + SpyBroker ⇒ `broker.calls == []` (zero submits of
    ANY kind at the Protocol boundary), preflight registry empty afterwards, M3 decision rows > 0
    (the probe ran), every refusal journaled; observe mode constructs NO broker (object-graph +
    `sys.modules` assert). [S1]
13. **`test_run_gates_file.py` (NEW — the LD-M5-2 dedicated suite)** — absent ⇒ both False; malformed
    JSON ⇒ both False + status row; non-identity-True (1, "true", null) ⇒ False; hostile extra keys
    (caps/universe/latency) IGNORED — assembled view differs from committed ONLY at the two gate
    keys; valid file ⇒ `opening_allowed(view) is True` while the COMMITTED config alone stays False;
    delete-file-re-closes (the uninstall story); observe/synthetic never call the loader (spy).
    [S1, FD-M5-2]
14. **`test_synthetic_isolation.py` + `test_synthetic_e2e.py`** — `__init_subclass__` rejects a bad
    `strategy_id`; wall 1: SyntheticStrategy + (spy) AlpacaPaperBroker pipeline ctor ⇒
    `SyntheticConfinementError`; wall 1 type-identity: a `FakeBroker`-subclass-of-`AlpacaPaperBroker`
    hybrid is REJECTED; wall 2 both directions (§R 9 + FakeBroker refuses non-synthetic intents);
    real strategy + no artifact ⇒ `backtest_artifact_missing`; tampered/mismatched artifact fixtures
    ⇒ `artifact_hash_invalid`/`artifact_key_mismatch`; AST guard on `strategies/`; **E2E:** scripted
    open→mark→close offline (FakeBroker `partial_then_full`, in-memory fixture config, replay
    fixture): all four streams journaled, golden byte-exact, rehydrate reproduces the book, S1
    registry empty of open-kind entries at exit. [S9, S1, S3, S6]
15. **`test_kill_flatten_driver.py`** — the S8 drill through `PriceCappedFlattenBroker`: every intent
    reaching the inner broker has `is_reducing=True` AND a tick-valid `limit_price` (no `limit=None`
    passes through); STALE quote still prices (staleness never blocks a reduce); no-price symbol ⇒
    `FlattenUnpriced` ⇒ `failed[]`/`residual` with `no_price_for_cap` ⇒ `retry_residual` succeeds
    once quotable; cancel-opens-first ordering (kill_trip cancel rows precede the flatten submits);
    generation bump kills an outstanding open token at consume; zero open-kind authorizations after
    the drill; `close_position(reason="kill_flatten")` rows. [S8, S1, FD-M5-1/25]
16. **`test_replay_feed.py` + `test_observe_e2e.py`** — field mapping events.jsonl → QuoteSnapshot
    byte-exact; ReplayClock determinism; symbol intersection (FD-M5-4); depth rows → DepthSnapshot
    with recorded book_hash; bar preload == incremental resample (anti-lookahead preserved by as-of
    reads); **observe E2E TODAY:** committed tbbo fixture + committed config ⇒ probe decisions +
    scored rows + calibration report match goldens; no broker in the object graph. [S3, FD-M5-4]

**Invariant map:**

| ID | Invariant | Primary tests |
|----|-----------|---------------|
| S1 | Committed config (+absent run-gates file) ⇒ zero submits of any kind at the Broker boundary over a FULL orchestrator run; mint terminates at `run_gates`; observe constructs no broker; no new module can reach a mint/submit (AST) | 3, 9, 11, 12, 13, 14 |
| S2 | Float/NaN/Inf never reaches a money/qty field at the new seams (broker payloads, cap math, modeled fills, fees, ledger rows) | 2, 5, 6, 7, 8 |
| S3 | New streams replay byte-identical; truncated-tail dropped / corrupt line fatal; rehydrate == live byte-exact; restart recovery deterministic | 6, 7, 14, 16 |
| S4 | Stale/un-timestamped/epoch-changed quote, missing stamp, halt/LULD/auction, invalid tick/lot, missing-or-denied `can_open` ⇒ NO broker submit, machine-readable reject; TOCTOU re-check at consume; post-submit state change ⇒ cancel + price-bounded late fills | 2, 3, 4, 5, 10 |
| S5 | Broker ledger never overwritten by a modeled value (type-enforced); PnL split never collapsed; fees modeled-side only; divergence flagged, never applied | 7, 8 |
| S6 | decision→risk→preflight→order→fill→position correlation with monotonic seq; deterministic ids; journal-before-mint ordering | 6, 10, 14 |
| S8 | Kill ⇒ cancel-opens, flatten-then-halt, price-capped reduce-only, residual retry, no opens possible after | 15 |
| S9 | No real strategy opens without a committed artifact (none exists ⇒ all reject); synthetic isolated by both walls + AST; synthetic E2E green offline | 14, 3 |
| LD-M5-2 | Run-gates file: two keys only, fail-closed absent/malformed, hostile keys inert, uninstall re-closes | 13, 12 |
| M4 §O | All four rehydrate-seeds before first put/can_open; MarginObservation per buy fill; F4 snapshot rows; portfolio_is_stale | 10 |

## S. Conventions-to-mirror table

| Convention | Source | M5 usage |
|---|---|---|
| Registry-authorized single-use tokens; mint = the only authorization writer | `execution_preflight.py:20-94` | the rebuilt open mint (§D) |
| Non-bypassable `submit_order` → `require_token` → `_place` | `broker/base.py:70-87` | unchanged chokepoint; consume-time TOCTOU rides inside it |
| Lazy SDK import inside the one credentialed build path | `marketdata/databento.py:89-102` | `_build_real_client` (§G) |
| Validating-ledger facade over `EventWriter`; `"v"`-first payload | `status_ledger.py` pattern, M4 §K | `ExecLedger` (§P) |
| Reserved journal keys — `decision_id`/`order_id` as kwargs | `journal.py:21,110-125` | every §P row |
| Strict-`>` staleness on injected monotonic ms | `market_state_cache.py:74-99` | quote/depth/token/verdict TTLs |
| Code-constant safety values; shorten-only/ceiling clamps; polarity table | M2 §G, M4 FD-M4-6/22 | §B constants + the latency floor |
| Closed vocabularies as frozensets; out-of-vocab raises | `market_state.py:77-99`, M4 §A | `exec_reasons.py` (§A) |
| Two-phase ladder: terminal short-circuits + collect-all; sorted reasons | M2 `decide()`, M4 FD-M4-9 | §2.2 |
| Pure decider — restriction is DATA; caller journals | `market_state.py:212-218`, M4 FD-M4-5 | `evaluate_preflight` (FD-M5-14) |
| One-parser config, fail-loud at startup | `signal_config.py:102-231` | `ExecutionConfig` (§B) |
| Raw-payload provider seam + ONE parse chokepoint | M4 §B (`AccountReadProvider`) | `OrderApi` + `order_state.py` (§F) |
| Reduce-only validated against the HELD position; flatten-then-halt + per-position isolation | `execution_preflight.py:97-118`, `kill_switch.py:18-47` | close path (§M.7), flatten proxy (§H.2) |
| Exact money journaled for rehydrate; quantize-only provenance | M4 FD-M4-25/LD-R5 | §P.2 money discipline |
| `row_hash` deterministic ids; replay byte-exact | `serializer.py:53-55`, M3 §I, M4 §K.3 | §P.3 |
| Pinned Decimal context for derived values | `quote_quality.py:23` | cap math (§C), VWAP (§I) |
| AST import guard + subprocess isolation + socket block | `test_no_network_no_creds.py`, M3 FD-12, M4 FD-M4-24 | §3 discipline, §R 11 |
| Committed-config canary at the Broker boundary | `test_config_canary.py:23-64` | the full-orchestrator S1 canary (§R 12) |
| Two-key arming untouched; paper ≠ live | `arming.py`, parent §12 | §O.2 point 6 |

## T. Deferred items (deliberately out of M5 — owners assigned)

- **Robin (reviewed commits):** first committed caps/universe values for real paper opens (FD-M5-3);
  the Databento live realtime subscription decision (unblocks M1-2b → real paper opens AND live-paper
  depth for `depth_vwap_l2_v2`); SEC §31 rate re-verification at M5-2a (§0.2 F2).
- **M1-2b:** live realtime transport; until then paper-mode opens reject fail-closed at the quote
  stage and observe/synthetic are the runnable modes.
- **M6:** SOD/EOD reconcile diff job (consumes §P streams + `client_order_id` join; sets
  `PortfolioRead.unreconciled_drift`); `trade_updates` websocket fill stream; per-execution fill
  granularity (activities endpoint); retry/replace policy + `max_open_orders > 1`; dividend/CA
  position adjustment in `PaperBook`; auto-retry cadence for kill residuals (M5 = operator-attended
  runbook only, FD-M4-19); journal rotation (M1 MINOR-9 mirror).
- **M7:** backtest-artifact PRODUCTION + review/signing runbook (possible HMAC upgrade, FD-M5-27);
  sizing; finer edge economics (`latency_lost_edge` beyond the one-knob rule, FD-M5-29);
  `extended_hours_blocked` activation; gate metrics / funnel dashboards; resting (non-marketable)
  limits; calibrating `latency_budget_ms` to measured feed/broker latency (parent PR-10).
- **M8:** `AlpacaLiveBroker` (separate class behind `construct_live_broker`); two-key consumption;
  `live_gate_flip` kill cause; live kill drill + go-live checklist.
- **Short-side milestone:** sell-to-open matrix, locate, SSR consumption; activates
  `ssr_short_blocked`/`locate_unavailable`.
- **Unmodeled by choice:** queue position (L3/MBO — parent §5.1); partial-fill probability modeling;
  Alpaca paper's 10%-random partials in credentialed runs are accepted as broker-authoritative (the
  M5-2a runbook asserts invariants — price ≤ cap, Σ deltas = filled_qty — never exact fills);
  borrow/maker-taker fees (FeeModel covers SEC+TAF only).

## U. References

- Parent design: `docs/superpowers/specs/2026-06-08-stocks-agent-design.md` §4 (paper-realism 1–11),
  §5 Tier 6 (lines 227-274), §6 (296-319), §7 (321-354), §9 S1/S2/S4/S5/S6/S8/S9 (383-394), §10 M5
  (405), §12 (418-430).
- Frozen upstream contracts: `2026-06-09-M4-risk-core-contract.md` (rev 2 — §B/§I/§J/§K/§O consumed
  as the real API); `2026-06-09-M3-signal-calibration-contract.md` (§A/§E/§I formats; FD-12 import
  guard); M2 conventions via code.
- Input designs (synthesized here): the three independent M5 architect proposals
  (execution-correctness / fail-closed-safety / integration-runnability lenses, 2026-06-10).
- External: Alpaca docs (verified 2026-06-10 — §0.2 A1–A10: `docs/orders-at-alpaca`,
  `docs/paper-trading`, `reference/postorder`, `docs/regulatory-fees`); FINRA Schedule A §1 TAF rates
  (verified 2026-06-10 — §0.2 F1); SEC Section 31 fee rate $27.80/million (FY2025 advisory; pinned
  with re-verify duty — §0.2 F2); `alpaca-py==0.43.4` (§0.2 F3).
- Repo facts: §0.1 table (file:line verified at `f9ec7c6` by this synthesis).

