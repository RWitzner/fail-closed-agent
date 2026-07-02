# M4 (Risk core) — FROZEN CONTRACT (READY-TO-BUILD, rev 2)

> **Status:** READY-TO-BUILD, 2026-06-09 (rev 2). Synthesized from THREE independent architect designs
> (regulatory-correctness / fail-closed-safety / integration-seams lenses) around Robin's locked decisions
> LD1–LD8 (restated and resolved as FD-M4-1…8 below; every cross-design disagreement is resolved explicitly
> in §1), then revised through a 4-lens critic pass (repo-facts / buildability / safety-invariants /
> regulatory-math; **50 findings — 2 blockers, 24 majors, 24 minors — ALL applied**, deduplicated to 29
> unique defects; judgment calls resolved by Robin as LD-R1…LD-R6; see §Q revision log). Mirrors the M3
> contract (`2026-06-09-M3-signal-calibration-contract.md`) in granularity: module-by-module APIs, code
> skeletons, frozen vocabularies, fixtures, and a test→invariant map. The build agent follows it without
> relitigating.
>
> **Branch:** `m3-signal` @ `f9ec7c6`. Baseline suite: 700 tests green.

## 0. Scope, ground rules, verified repo facts

**In scope (parent §5 Tier 5, §10 M4):** new package `scripts/agent/risk/` — `reasons.py`,
`account_state.py`, `risk_config.py`, `exposure.py`, `intraday_margin.py`, `pdt_compat.py`,
`loss_limits.py`, `locate.py`, `risk_kill.py`, `can_open.py`, `risk_ledger.py`; one new journal stream
`journal/risk.jsonl`; committed config additions to `config/risk_rules.json` (caps stay **0**); one new
committed calendar fixture (`tests/fixtures/calendar/nyse_margin_window_v1.json`); fixture builders in
`tests/lib/risk_fixtures.py`; tests per §M. **Out of scope:** order submission, `mint_open_token` rebuild
(stays reject-all), fees (M5 `FeeModel`), orchestrator cadence/wiring (M5), SOD/EOD reconcile diff job (M6),
backtest gate / sizing / ratio-leverage caps (M7), live arming (M8), locate/borrow provider (short-side
milestone), any network/credential call.

**Ground rules (unchanged from M0–M3):**

- Committed gates stay OFF: `agent_rules.enabled=false`, `paper_trading.enabled=false`,
  `live_trading.enabled=false`. M4 adds **no** gate and flips **no** gate. M4 PRODUCES the risk read; it
  never acts on it — the only acting component is the kill-switch flatten path, which is reduce-only by
  construction (S1/S8).
- Offline suite stays stdlib-only; no new dependency; tests make no network calls and read no credentials;
  no module under `scripts/agent/risk/` imports `alpaca-py`, `databento`, or `exchange_calendars` (any scope).
- Determinism conventions: canonical `dumps()` (`serializer.py:47-50`), Decimal-as-string, per-row sha256
  (`serializer.py:53-55`), per-stream monotonic `seq` under a shared per-path lock (`journal.py:62-129`),
  injected clocks only (no wall-clock reads in M4 modules).
- All market logic in ET; persisted timestamps UTC ISO-8601; monotonic ms for staleness; strict-`>`
  staleness boundaries everywhere (the `market_state_cache.py:97` form).
- **Fail-closed asymmetry (FD-M4-3, load-bearing):** every missing/stale/malformed account or portfolio
  input degrades to **reject-opens**. **No M4 code path can block a reduction** — the reduce path
  (`mint_reduce_only_token` → `submit_order`) never consults `can_open`, any freshness gate, or any M4
  read. This is structural (import-guard + paired tests), not behavioral.

### 0.1 Verified repo facts this contract builds on (file:line at `f9ec7c6`)

| Fact | Source |
|---|---|
| Run gates are identity-strict (`value is True`); `opening_allowed` requires BOTH agent gates; `live_allowed` reads `risk_rules.live_trading.enabled` | `scripts/agent/gates.py:10-30` |
| `mint_open_token` is reject-all (raises `PreflightRejected`, never issues an authorization) | `scripts/agent/execution_preflight.py:90-94` |
| `mint_reduce_only_token` validates the HELD position's sign/size/symbol — never the caller's self-asserted flag; qty `>0` and `<= held` ("may flatten, never flip") | `scripts/agent/execution_preflight.py:97-118` |
| Tokens are registry-authorized, single-use, non-constructible/copyable/picklable; `require_token` re-validates side+qty then consumes | `scripts/agent/execution_preflight.py:20-87`, `scripts/agent/broker/base.py:37-56` |
| M0 `KillSwitch`: states `"active"→"flattening"→"halted"`, reduce-only flatten per position, per-position failure isolation into `failed[]`, `finally`-guaranteed `halted`, `allows_opening()` always False | `scripts/agent/kill_switch.py:12-53` |
| `OrderIntent` validates qty positive finite Decimal / limit finite-or-None; `BrokerBase.submit_order` is the non-bypassable chokepoint; `Broker` Protocol has `positions()`/`account()` returning `object` | `scripts/agent/broker/base.py:17-87` |
| M0 spy broker `account()` returns `{"equity": Decimal("0"), "buying_power": Decimal("0")}` (Decimal-typed dict — the seam M4's parser must accept) | `scripts/agent/broker/alpaca.py:26` |
| `rules_hash()` = sha256 over the canonical assembled config (`allow_nan=False`); `tighten_only_merge`: bools AND / numerics (non-bool) `min()` / dicts recurse / **anything else keeps base** / overlay-only keys dropped | `scripts/agent/config.py:17-43` |
| `BrokerUSD` / `ModeledUSD` distinct Decimal newtypes; `as_broker_usd` guard; serializer rejects float and non-finite Decimal | `scripts/agent/serializer.py:15-64` |
| Journal `_RESERVED={event_type,run_id,seq,hash,decision_id,order_id,ts_utc}`; `replay` drops ONLY a truncated (no-newline) tail, complete corrupt line ⇒ `JournalCorruption`; per-resolved-path shared seq+lock | `scripts/agent/journal.py:21,28-59,62-129` |
| `EventWriter` wraps `JournalWriter` verbatim (no second writer/hash/serialization); `record(event_type, fields, *, decision_id, order_id)`; `replay_stream` delegates to `journal.replay` | `scripts/recorder/persistence.py:52-108` |
| Validating-ledger precedent: `StatusLedger(writer, *, rules_hash)`; `canonical_status_payload` puts `"v"` FIRST; `PRICE_QUANTUM` quantize **with round-trip check**; `rehydrate_state` = pure fold by ascending `seq`, latest-row-wins | `scripts/agent/status_ledger.py:93-105,114-121,211-219,356-371` |
| M2 `Verdict{symbol,instrument_id,session_state,tradability,halt,luld,ssr,two_sided_nbbo,short_allowed,reasons,ca_blackout,session_date_et}`; `short_allowed = tradability==TRADABLE AND ssr==INACTIVE` (conservative blanket, MR-3) | `scripts/agent/market_state.py:184-201,362-364` |
| `Tradability` closed vocab {TRADABLE, REDUCE_ONLY, NOT_TRADABLE}; `merge_severity` tighten-only; `MarketStateError` on out-of-vocab | `scripts/agent/market_state.py:77-99,204-209` |
| `MarketStateCache.get` strict-`>` TTL, instrument-id mismatch = MISS, stale/missing ⇒ `safe_default_verdict` with `reasons=("cache_stale_safe_default",)`, `tradability=NOT_TRADABLE`, `short_allowed=False`; `DEFAULT_FRESHNESS_TTL_MS=2000`; ctor override may only SHORTEN (HIGH-3 clamp) | `scripts/agent/market_state_cache.py:34,52-99,101-125` |
| `OPEN_CLOSE_BUFFER_S = 0` (OFF) — M2 routed the policy decision to "M4/M7"; resolved here as FD-M4-26 (stays OFF in M4) | `scripts/agent/market_state.py:90-95` |
| Calendar seam: `ScheduleProvider` Protocol (`schedule_for` / `is_trading_day` / `calendar_pin`); `FixtureScheduleProvider` offline, raises `UnknownSessionDate` out-of-coverage (never "assume open"); `MarketCalendar.schedule_for`/`.calendar_pin` passthroughs | `scripts/agent/market_calendar.py:56-85,167-225,261-285` |
| The committed M2 calendar fixture covers only **8 scattered dates** — NOT enough for 15-business-day window fixtures ⇒ M4 ships a new dedicated contiguous fixture (§L) | `tests/fixtures/calendar/nyse_2026_schedule.json` (keys: 2026-03-08/09, 06-15, 11-01/02, 11-26/27, 12-25) |
| `Candidate`/`Leg` pure types: closed `SIDES={"buy","sell"}`, qty positive finite Decimal, limit finite-or-None, legs non-empty; carry NO order authority | `scripts/agent/candidate.py:12-57` |
| One-parser config precedent: `SignalConfig.from_config` — closed key sets, fail-loud `ValueError` at startup, computes `rules_hash` itself over the dict it is PASSED (currently `agent_rules` only at every call site — NOT the assembled pair; M4 cross-stream consequence noted in §C, M4C-9) | `scripts/agent/signal_config.py:76-115` |
| `QuoteVerdict.mid` quantized `MID_QUANTUM=Decimal("0.000001")` (the mark source for FD-M4-16) | `scripts/agent/quote_quality.py:19-20,46-51,101-103` |
| Committed `risk_rules.json` today: `live_trading{enabled:false, max_live_position_usd:0}` + `caps{max_position_usd:0, max_gross_exposure_usd:0, max_daily_loss_usd:0}` — M4 EXTENDS `caps`, adds `risk`, touches nothing else | `config/risk_rules.json:1-11` |
| Committed `agent_rules.json`: `enabled:false`, `paper_trading.enabled:false`, `universe.symbols:[]` — M4 does NOT edit this file (FD-M4-21) | `config/agent_rules.json:1-9` |
| Test doubles: `FakeClock(now_ms/advance)`; `SpyBroker` records every `submit_order` attempt at entry, enforces `require_token` | `tests/lib/fakes.py:83-114` |
| Committed-config canary pattern (real committed JSON, identity-False gates, zero submits at the broker boundary, overlay-cannot-loosen) | `tests/agent/test_config_canary.py:23-64` |
| Socket-block + `sys.modules` purity + AST module-scope import-guard test patterns to extend | `tests/agent/test_no_network_no_creds.py:7-166` |
| Parent spec: Tier 5 responsibilities + key types (`can_open(candidate, portfolio, account) -> Verdict`, `KillSwitch`, `IntradayMarginModel`, `LegacyPdtCompatMode`, `LocateCheck`); S8/S10 wording; M4 acceptance row; long-only scoping license | `docs/superpowers/specs/2026-06-08-stocks-agent-design.md:200-225,385-394,404,468-469` |

### 0.2 Verified regulatory facts (FINRA Regulatory Notice 26-10)

Verified against the primary source `https://www.finra.org/rules-guidance/notices/26-10` on 2026-06-09 by
the regulatory-lens architect (three independent targeted fetches; quotes from the notice page). This
contract's frozen behavior is a **strict superset (tighter)** of every reading below with exactly ONE
documented exception (RM-2/RM-7): **FD-M4-12's satisfaction basis** — the literal V6 EOD-IML-delta counts
passive market appreciation as satisfaction; a "customer-caused-increase" reading would be stricter, and
tightening to deposits-only is a one-line M5 change (the mirror gap surfaces as broker rejections). No
other mechanic depends on re-resolving a quote ambiguity (see FD-M4-10/11; the V9 minor-deficit carve-out
is NOT relied on for the freeze trigger — LD-R2).

| # | Fact | Verified language (abridged) |
|---|------|------------------------------|
| V1 | PDT replaced entirely | The intraday margin rule "replaces the outdated requirements in their entirety" — eliminates the pattern-day-trader designation and the $25,000 minimum equity requirement. No replacement minimum-equity threshold. |
| V2 | IML definition | "IML" = intraday margin level; an "IML-reducing transaction" is any transaction that reduces what the customer could withdraw while still meeting maintenance margin (e.g. a short sale, or a purchase other than to cover a short). |
| V3 | Deficit definition | "intraday margin deficit" = the highest deficiency following an IML-reducing transaction between the **margin to be maintained** and the **equity** in the account (maintenance basis, not Reg-T initial). |
| V4 | EOD calculation permitted | Real-time monitoring is not required; members may make a single calculation of the day's intraday margin deficit. |
| V5 | Same-day ordering ambiguity | When ordering cannot be demonstrated, compute on the assumption that activities occurred in the order producing the **highest** deficit. |
| V6 | Satisfaction | A deficit is satisfied if, from the end of such day to the end of a subsequent day, the customer has made net deposits **or otherwise caused an increase in the account's IML** equal to the deficit. |
| V7 | Outstanding window | A deficit remains outstanding until satisfied or until immediately after the close of business on the **15th business day** after the deficit date. |
| V8 | Freeze trigger (quoted in FULL — RM-7) | "If a customer **makes a practice** of failing to satisfy intraday margin deficits as promptly as possible **and** fails to satisfy an intraday margin deficit by the close of business on the **fifth business day** after it occurs", the member must prevent the customer from creating or increasing a short position or debit balance (other than by closing a short position) — "for **90 calendar days after such fifth business day or until the intraday margin deficit has been satisfied**". Both the bd5 anchor and the "or until satisfied" disjunct are resolved strictly tighter in FD-M4-11 (anchor shifted to `effective_from`, no early lift). |
| V9 | Minor-deficit exception | Deficits not exceeding the **lesser of 5% of the equity** in the margin account **or $1,000** (or arising under extraordinary circumstances) do not establish the practice. |
| V10 | Dates / scope | Effective 2026-06-04; phase-in until 2027-10-20; Rule 4210(d)(2) + (a)(17)–(a)(19), (g)(1)(J)–(K); margin accounts only. |

Two load-bearing consequences (FD-M4-2): `IML = equity − maintenance_margin` is **pure subtraction on
broker-reported numbers**; the model never re-derives a maintenance requirement from a 25%/30% table for any
decision. And the "makes a practice" prong (V8) is a member-firm determination we cannot observe — assumed
met (FD-M4-10).

## 1. Frozen decisions (FD-M4-1 … FD-M4-26)

FD-M4-1…8 restate Robin's locked decisions LD1–LD8 (not relitigated). FD-M4-9…26 resolve every
cross-design disagreement, each with a one-line rationale.

| # | Decision | Rationale / resolves |
|---|----------|----------------------|
| FD-M4-1 | **(LD1) Long-only.** Short locate/borrow is OUT of build scope. `locate.py` ships only the `LocateCheck` Protocol stub + `DenyAllLocate` (always fail-closes shorts); `can_open` rejects every sell-to-open / short-increasing leg with `short_side_disabled` structurally, before any locate call. SSR consumption = reading the M2 `Verdict.short_allowed` field, nothing recomputed — and in M4 even `short_allowed=True` cannot open a short. | Locked. A locate ledger with no borrow feed is untestable assurance theater; deny-all is strictly safe; the seam + reserved reasons keep the later milestone contract-stable. |
| FD-M4-2 | **(LD2) Broker ground truth.** `equity`, `last_equity`, `cash`, `buying_power`, `maintenance_margin`, `daytrading_buying_power`, `pattern_day_trader`, `daytrade_count`, position `market_value` are RECONCILED (copied/validated/stamped), never re-derived. The only arithmetic is subtraction/comparison/candidate-delta projection: `IML = equity − maintenance_margin` (V2/V3). **No margin-percentage, no $25k, no day-trade-count constant exists anywhere in `risk/`** (source-scan test). | Locked. Resolves regulatory-design's mirrored $25k/3-trade/5-day PDT constants vs integration's mirror-only posture → **mirror-only wins** (parent §5 Tier 5: "mirrors whatever Alpaca actually enforces"; counting ourselves is re-derivation). |
| FD-M4-3 | **(LD3) Fail-closed asymmetry.** Missing/stale/malformed account or portfolio data ⇒ reject OPENS (precise reason, dependent stages skipped), never block reduces. Kill/freeze states permit reduce-only. The kill switch never trips on stale/missing data (flattening blind is acting on unknown) — it skips, journals, and lets `can_open` hold the line. | Locked. All three designs agree; the skip-not-trip rule is the regulatory/fail-closed designs' shared posture. |
| FD-M4-4 | **(LD4) One reduce-only path.** `risk_kill.RiskKillSwitch` owns trigger evaluation + state machine + journaling and DELEGATES flattening to the existing M0 `agent.kill_switch.KillSwitch` (a FRESH M0 instance per flatten/retry pass; durable state lives in `RiskKillSwitch` + journal). `scripts/agent/kill_switch.py` is **not edited**. | Locked. Resolves the fail-closed design's in-place upgrade of `kill_switch.py` → rejected (M0 module stays frozen; exactly one reduce-only minting site, `kill_switch.py:41`). |
| FD-M4-5 | **(LD5) `can_open` is PURE.** Signature keeps the parent-frozen positional triple `can_open(candidate, portfolio, account)`; the M2 verdicts, marks, kill/margin/pdt/loss reads and `now_ms` arrive as keyword-only INPUTS. No I/O, no clock read, no fetch, no journal write inside; the **caller** journals every verdict via `RiskLedger.record_risk_verdict` (M2 decider/ledger split). Rung 1 of the ladder is the M0 run-gate check (`gates.opening_allowed`, reason `run_gates_off`) so there is ONE chokepoint answer. | Locked. Resolves regulatory-design's journal-inside-can_open → journaling moves to the caller through the ledger (purity precedent `market_state.py:212-218`); resolves integration's collaborator-free engine vs others' collaborator ctor → static ctor holds only `cfg`, `gates_config`, `run_id`. |
| FD-M4-6 | **(LD6) Committed caps stay 0.** All 7 `caps.*` values are integer whole-USD JSON numbers committed at **0** (tighten-only `min()`-merge native; an overlay can lower, never raise; raising = a committed, reviewed change). Cap breach is strict `>` ⇒ a 0 cap rejects ANY positive exposure: the S1 canary extends to `can_open` (zero caps ⇒ every open rejected even on a hypothetically-gates-on fixture config). Safety mechanics (windows, day counts, TTLs, minor thresholds) are CODE CONSTANTS (M2 §G precedent). `intraday_margin_buffer_usd` is **NOT config** — inverted polarity under `min()`-merge — it is the code constant `INTRADAY_MARGIN_BUFFER_USD = Decimal("0")` (raising it is a code change). | Locked + resolves regulatory OQ-6 (buffer polarity) → code constant. |
| FD-M4-7 | **(LD7) One new stream `journal/risk.jsonl`** via `RiskLedger`, a validating facade over `recorder.persistence.EventWriter` (the `status_ledger.py` pattern): no new writer/hash/serialization, `v` FIRST key (`RISK_LEDGER_VERSION=1`), `rules_hash` on every row, no `_RESERVED` collision, deterministic ids via `serializer.row_hash`, pure rehydrate fold. | Locked. |
| FD-M4-8 | **(LD8) Offline purity + seams.** Broker data arrives through the `AccountReadProvider` Protocol carrying **raw dict payloads** (fixture-shaped offline; M5's Alpaca adapter is a near-passthrough); parsing has ONE chokepoint. Business-day arithmetic uses the existing `ScheduleProvider`/`MarketCalendar` seam — **business day := XNYS trading session** (`is_trading_day`, market_calendar.py:84), fixture-driven offline, no `exchange_calendars` import. The Good-Friday edge (exchange closed, many firms open ⇒ our bd5/bd15 can land one day later than a bank-day reading) is DOCUMENTED and accepted; revisit in M5 if Alpaca's enforcement disagrees. | Locked + resolves regulatory OQ-4 / integration Q5 (business-day definition) → XNYS sessions. |
| FD-M4-9 | **Ladder shape.** Two phases mirroring M2 `decide()`: Phase 1 terminal short-circuits in frozen order `run_gates → kill → margin_freeze → account → portfolio` (stop at first hit, downstream stages recorded in `stages_skipped`); Phase 2 collect-ALL accumulation in frozen order `candidate → universe → market_state → short → caps → margin → pdt → loss` (every applicable reason, sorted union, M3 collect-all posture). `allowed ⟺ reasons == ()` — no "allow with warnings". Run gates are rung 1 (LD5), so on the committed config the terminal stage is always `run_gates`. | Resolves regulatory/fail-closed (kill first) vs LD5 (gates first) → LD5 wins; resolves terminal-vs-accumulate disagreements per stage. |
| FD-M4-10 | **Freeze triggers on the FIRST qualifying deficit — minor included (LD-R2).** The V8 "makes a practice" prong is a member-firm determination we cannot observe ⇒ assumed met. ANY single deficit — minor included — unsatisfied at the close of `add_business_days(D, 5)` triggers the freeze. `practice_count()` exists for observability only and gates nothing. | Strictly tighter than the rule (tighten-only is always allowed). V9's minor-deficit exception scopes ONLY the "makes a practice" prong, not the bd5 prong (RM-2) — exempting minor deficits from the trigger would NOT be a superset, so they are not exempted. Rung 11's `intraday_margin_deficit_outstanding` stays NON-minor-scoped (unchanged). |
| FD-M4-11 | **Freeze scope/duration: ALL opens, full 90 calendar days, no early lift.** `frozen iff effective_from_et ≤ session_date_et < expires_on_et` where `effective_from_et` = first business day after the triggering bd5 close and `expires_on_et = effective_from_et + 90 days` (pure date math). Satisfying the trigger deficit does NOT end the freeze early; a buy is blocked even if cash-secured (we will not re-derive the broker's cash-vs-margin attribution). Reduces untouched, structurally. | Strict superset of BOTH quoted readings of V8 ("90 calendar days" vs "…or until satisfied") and of the short/debit-balance scope ⇒ no re-fetch needed; resolves regulatory OQ-5 + integration Q4. |
| FD-M4-12 | **Satisfaction (V6) = literal EOD-IML-delta.** Deficit on day D is satisfied at the first EOD E **strictly after** D with `IML_eod(E) − IML_eod(D) ≥ amount` (deposits or any other IML increase count). `iml_eod_d` is stored on the record; the basis is journaled. If Alpaca proves stricter (deposits-only) in M5, tightening is a one-line, tighten-only change. | Resolves regulatory OQ-1 → literal read, journaled. |
| FD-M4-13 | **Deficit identity: ONE record per ET session date** (single account), `deficit_id = "imd-" + row_hash({"session_date_et": D})` — run-independent (deficits are account-level facts that must survive restarts). The day's amount is **max-merged monotonically** over all `after_iml_reducing` observations (V3/V5 worst-ordering); each increase journals a `margin_deficit_detected` row (`cause ∈ {"opened","increased"}`); `minor` is recomputed at each increase using THAT observation's equity and is **LATCHED one-way: once non-minor, never back to minor** (a later higher-equity observation cannot loosen — safety-F3). EOD single-calculation mode (V4) equals the day's deficit ONLY when the EOD read is the day's worst after-IML-reducing point (the one-observation degenerate case; fidelity depends on the M5 observation cadence — obligation recorded in §O, RM-8); EOD never lowers an intraday-observed amount. | Resolves regulatory's amount-in-the-id hash (id would churn on max-merge) and integration's `opened_ts_utc`-keyed id (not replay-stable across runs). |
| FD-M4-14 | **Minor boundary:** `minor = amount ≤ min(equity × Decimal("0.05"), Decimal("1000"))` — boundary-equal IS minor ("do not exceed", V9); exact Decimal compare; equity unknown/invalid at the observation ⇒ **not** minor (fail-closed: minor is the permissive direction); `minor` is one-way LATCHED (safety-F3, FD-M4-13). Minor deficits are journaled and tracked (satisfaction/expiry identical), never set `intraday_margin_deficit_outstanding` (rung 11 stays non-minor-scoped) and never enter `practice_count` — but they DO trigger the bd5 freeze (LD-R2/FD-M4-10). The "extraordinary circumstances" prong is not modeled (we are stricter without it). | All three designs agree; constants are code constants (config could only widen the permissive direction). |
| FD-M4-15 | **PDT compat is mirror-only and never blocks on UNKNOWN alone.** Evidence: `pattern_day_trader is True` ⇒ enforcing; a latched broker PDT rejection (frozen markers, hook wired in M5) ⇒ enforcing + `pdt_compat_blocked` for the rest of the run; `pattern_day_trader is False` ⇒ not enforcing; `None` ⇒ UNKNOWN — journaled, NOT a reject by itself. When enforcing and `daytrading_buying_power` is present: `Σ cap_notional > daytrading_buying_power` ⇒ `pdt_compat_dtbp_exceeded`. No day-trade counting, no $25k constant (FD-M4-2). | Resolves the 3-way disagreement (regulatory: mirror $25k/count constants; fail-closed: UNKNOWN ⇒ reject; integration: mirror-only, UNKNOWN ⇒ no block) → integration wins: genuinely missing account data already fail-closes at the terminal `account` stage (required-field validation), while blocking on a legitimately-absent optional field would deadlock paper trading; tighten-only is preserved because pdt_compat can only ADD reasons. |
| FD-M4-16 | **Notional formula.** Per opening leg: `cap_notional = qty × max(limit_price, mark.mid)` when a FRESH, identity-matched `Mark` for the leg's symbol is provided, else `qty × limit_price`. `limit_price is None` **or `limit_price <= 0`** ⇒ `unpriceable_candidate` (a mark alone is NOT a price cap — the supported order matrix is marketable-limit only, parent §5 Tier 6; a non-positive limit cannot be a marketable limit and is excluded from ALL notionals exactly like limit-None, evaluated before `cap_notional` — RM-3: a negative limit must never offset another leg's notional in `gross_notional`/margin/dtbp). A stale/absent mark never weakens the bound (any buy fills at ≤ limit), so marks are an optional conservative tightener + provenance (`mark_used` on the leg), never a reject reason; a mark whose `symbol`/`instrument_id` mismatches its key/leg ⇒ `RiskError` (programming error). | Resolves integration D-1 (mark required, 3 mark reasons) vs fail-closed (limit-only) → max-of-bounds: keeps LD5's "mark arrives as input" without making liveness of a redundant input a new failure mode. |
| FD-M4-17 | **Exposure basis.** Held positions use broker-reported signed `market_value` (REQUIRED field on `PositionRead` — ground truth, never re-priced); held sector exposure = `Σ |market_value|` per sector (absolute — a held short ADDS to its sector exposure, RM-4). Candidate impact is additive-conservative: `|cap_notional|` adds to gross/position/sector regardless of side; signed `cap_notional` (buy +, sell −) to net/beta. Any HELD symbol — and any CANDIDATE leg symbol (RM-5/M4C-5: the always-case on the committed empty universe) — missing from `cfg.universe` POISONS the sector/beta aggregate ⇒ `sector_unknown`/`beta_unknown` (never a partial sum, never a silent skip; the second wall's exact frozen reason tuple is enumerated in §J). Multi-leg candidates: per-leg checks per leg, aggregate checks on the additive union; any reason rejects the whole candidate (hedge-aware netting is a deliberate later loosening — out of M4). | Fail-closed design's poisoned-aggregate + regulatory's additive-conservative; resolves regulatory OQ-7. |
| FD-M4-18 | **Loss limits.** `daily_loss = last_equity − equity` (both broker-reported; no journal dependence — purest LD2 read); `drawdown = hwm_equity − equity` where `hwm_equity` = running max of FRESH equity reads within the run lineage (journaled `loss_hwm_update`, rehydrated; run-lifetime horizon). Breach iff value `> cap` (strict). **Cap 0 = zero budget — any positive loss breaches; 0 NEVER means "disabled".** `LossRead.hwm_equity is None` (no observation yet) ⇒ `loss_baseline_unavailable` (reject opens, fail-closed). The monitor returns a `TripSignal`; it holds no broker and submits nothing. | Resolves SOD-baseline (regulatory) vs `last_equity` (fail-closed) vs journaled baseline (integration) → `last_equity`; resolves integration Q3 (HWM horizon) → run-lifetime, rehydrated. |
| FD-M4-19 | **Kill switch is one-way per run:** `MONITORING → FLATTENING → HALTED`; HALTED latches across rehydrate; **no in-process re-arm API in M4** (re-arm = operator-attended NEW run over a journal that shows the halt; the fail-closed design's single-use reset-authority object is deferred to M5/M8 with the orchestrator/runbook). Retrip while not MONITORING is an idempotent journaled no-op (`kill_retrip` row; nothing resubmitted). `generation: int` (+1 per accepted trip, never reset) is journaled now so M5's preflight can bind TOCTOU re-checks to it. | Resolves fail-closed's reset-authority (build later) vs integration's operator-only (adopted); generation field kept from fail-closed (cheap, M5-load-bearing). |
| FD-M4-20 | **Flatten attempts ALL positions regardless of M2 tradability.** The broker is the authority on executability; a halted symbol's reduce order fails into `failed[]` → `residual`, retried by `retry_residual` (reduce-only only). The M2 verdict is attached to the transition row as annotation, never used to skip a reduce (skipping on our own possibly-wrong state risks frozen exposure — the exact S8 bug; submitting a reduce into a halt is a journaled rejection at worst). | Resolves fail-closed's defer-on-NOT_TRADABLE vs regulatory/integration attempt-all → attempt-all. |
| FD-M4-21 | **Risk universe lives in `risk_rules.risk.universe`** — `symbol → {"sector": "<slug>", "beta": "<Decimal-string>"}` (strings/dicts ⇒ `tighten_only_merge` keeps base; overlays cannot tamper). Membership for `can_open` = `symbol ∈ risk.universe` (a symbol without sector+beta metadata cannot pass caps anyway ⇒ one ladder, no second membership source). Committed **empty** ⇒ `universe_excluded` for every symbol (another all-reject layer). `agent_rules.universe.symbols` (data/refresh universe) is NOT edited and NOT consulted by M4. Hand-curated committed metadata is accepted for the ≤50-name universe (reviewed per change ⇒ new `rules_hash`). | Resolves fail-closed (reuse agent_rules universe + separate maps) vs regulatory/integration (risk-level map) → one map under risk_rules; resolves integration Q6 / fail-closed Q3. |
| FD-M4-22 | **Freshness TTLs are code constants with shorten-only clamps at EVERY override surface** (the market_state_cache HIGH-3 pattern): `ACCOUNT_FRESHNESS_TTL_MS = 5000` (clamped in the `AccountStore` ctor), `PORTFOLIO_FRESHNESS_TTL_MS = 5000` (clamped in the `portfolio_is_stale` helper, §B — the M5 caller MUST use it to precompute `PortfolioRead.stale`, so the arithmetic is owned and tested in M4), `MARK_FRESHNESS_TTL_MS = 2000` (mirrors `DEFAULT_FRESHNESS_TTL_MS`; clamped via `leg_cap_notional`'s `ttl_ms` kwarg, §D). Each surface raises `ValueError` on a longer TTL. Strict-`>` boundaries (fresh at exactly TTL). `now_ms < as_of_ms` ⇒ SKEW (clock regression is DATA ⇒ `account_clock_skew`, reject-opens, never an exception). | Fail-closed design adopted; all THREE TTLs get an enforcing clamp home (M6-build). |
| FD-M4-23 | **Reserved vocabulary, never emitted in M4:** reasons `locate_unavailable`, `ssr_short_blocked`; kill cause `live_gate_flip` (M8). Emitting any reserved code in M4 is a test failure; the strings exist so the short-side and live milestones extend without re-keying vocabularies. | Vocabulary stability across milestones (M3 FD-12 spirit). |
| FD-M4-24 | **S1 import guard (extends the M3 FD-12 closed set; this is the ONE normative statement — §M test 11 implements exactly it, safety-F7).** EVERY module under `scripts/agent/risk/` — `risk_kill.py` INCLUDED — must not import (any scope) `agent.broker*`, `agent.execution_preflight`, `agent.kill_switch`, `agent.arming`, and must not reference the tokens `submit_order`, `mint_open_token`, `mint_reduce_only_token`, `OrderIntent`, `OpenPreflightToken`, `ReduceOnlyPreflightToken`, `PreflightToken`, `require_token`, `consume`, nor `importlib`/`__import__` at all. The SOLE exemption: `risk_kill.py` may `import agent.kill_switch` and reference the `KillSwitch` name it needs from it — nothing else is relaxed for it (in particular `risk_kill.py` may NOT reference `submit_order`/`OrderIntent`/`require_token`/`consume` or any mint/token name — flattening is delegated, FD-M4-4; the broker reaches it only as an untyped call parameter). Subprocess-isolated import check mirrors M3. | Structural S1; resolves who may touch the actuator. |
| FD-M4-25 | **Money discipline.** Parsed broker money = exact Decimal (`Decimal(str(value))`) wrapped `BrokerUSD`; ALL comparisons/arithmetic on the exact parsed values. **Every REHYDRATE-BEARING model-state money field is journaled EXACT** — unquantized canonical Decimal-string via the serializer — so rehydrated state == live state **byte-exact** (LD-R5, resolving the RM-1/M4C-4/safety-F1 blocker; per-field enumeration in §K.2). Quantization to `USD_QUANTUM = Decimal("0.01")`, **quantize-only** ROUND_HALF_EVEN, applies ONLY to the `account_snapshot` provenance row's money fields — nothing rehydrates from that row (broker payloads may legitimately carry sub-cent precision — a round-trip requirement would reject ground truth; mirrors M3's quantize-only-for-float-born posture). Float/bool/NaN/Inf anywhere in a money/qty field ⇒ `AccountInvalid` at the parser, never a constructed snapshot (S2 at the account seam). | Resolves regulatory's round-trip-checked USD_QUANTUM vs broker-precision reality → exact for rehydrate-bearing fields, quantize-only for account-snapshot provenance (LD-R5). |
| FD-M4-26 | **Explicitly NOT in M4** (each with its owner): ratio/leverage caps (M7 — USD caps strictly bound exposure while committed at 0; a gross/equity division adds coupling with no added safety now; resolves integration's `ratios` block); `OPEN_CLOSE_BUFFER_S` stays 0/OFF (M7, with backtest evidence); fees (M5 `FeeModel`); real flatten driver — price-capped flatten orders, post-submit cancel, deferred-retry scheduling (M5); account-refresh cadence + who calls `observe`/`close_of_day`/`evaluate` (M5 orchestrator); SOD/EOD reconcile diff (M6 — M4 only consumes `PortfolioRead.unreconciled_drift`, default False); auto-trip on prolonged staleness (M5 liveness; M4 = reject-opens + edge-triggered alert); risk.jsonl rotation (mirrors M1 MINOR 9 deferral). | Scope cuts named once, here. |

## 2. Frozen reason vocabulary + ladder order

### 2.1 `RISK_REASONS` (closed set; emitting out-of-vocab raises `RiskError`)

```
terminal (phase 1, one per verdict):
  run_gates_off, kill_switch_halted, margin_freeze_active,
  account_missing, account_stale, account_invalid, account_clock_skew,
  portfolio_missing, portfolio_stale, portfolio_unreconciled

accumulated (phase 2, collect-all, sorted union):
  strategy_not_paper_eligible, reduce_path_not_can_open, short_side_disabled,
  unpriceable_candidate, universe_excluded,
  market_state_missing, market_state_not_tradable, market_state_stale_default,
  position_cap_exceeded, gross_exposure_cap_exceeded, net_exposure_cap_exceeded,
  sector_cap_exceeded, sector_unknown, beta_cap_exceeded, beta_unknown,
  intraday_margin_insufficient, intraday_margin_deficit_outstanding,
  pdt_compat_blocked, pdt_compat_dtbp_exceeded,
  daily_loss_breached, drawdown_breached, loss_baseline_unavailable

RESERVED (in the frozenset, never emitted in M4 — FD-M4-23):
  locate_unavailable, ssr_short_blocked
```

`market_state_stale_default` is deliberately the SAME string M3 uses for the cache safe-default
(`signal_snapshot` gate 4), keyed off `"cache_stale_safe_default" in verdict.reasons`.

### 2.2 The ladder (frozen ORDER; stage names frozen)

Stage names (frozen, journaled as `gate_stage` / `stages_skipped` members):
`("run_gates","kill","margin_freeze","account","portfolio","candidate","universe","market_state","short","caps","margin","pdt","loss")`.

**Phase 1 — terminal short-circuits** (stop at first hit; the verdict's `reasons` is exactly that stage's
sorted reasons; ALL later stage names land in `stages_skipped`):

| # | Stage | Reject reason(s) | Check (frozen semantics) |
|---|-------|------------------|--------------------------|
| 1 | `run_gates` | `run_gates_off` | `not gates.opening_allowed(gates_config)` — identity-strict, the literal M0 function (gates.py:23). On the committed config EVERY verdict terminates here (S1 extension). |
| 2 | `kill` | `kill_switch_halted` | injected `kill_state != "monitoring"` (covers `flattening` and `halted`; no open may interleave with a flatten). Out-of-vocab state string ⇒ `RiskError`. |
| 3 | `margin_freeze` | `margin_freeze_active` | `margin_read.freeze.active_on(margin_read.asof_session_date_et)` per FD-M4-11 (ledger-backed model state; evaluable without a fresh account read; the date is PINNED to the MarginRead's own `asof_session_date_et` — safety-F6/RM-12). Stage 8 cross-checks this date against the leg verdicts (`RiskError` on mismatch, §J). |
| 4 | `account` | `account_missing` / `account_stale` / `account_invalid` / `account_clock_skew` | `account.status` (computed by `AccountStore.get` at snapshot time against the caller's `now_ms`; `can_open` consumes it, re-checks nothing). Maps 1:1 from `{"missing","stale","invalid","skew"}`. |
| 5 | `portfolio` | `portfolio_missing` / `portfolio_stale` / `portfolio_unreconciled` | `portfolio is None` ⇒ missing; `now-stamp > PORTFOLIO_FRESHNESS_TTL_MS` precomputed by the caller onto `portfolio.stale: bool`; `portfolio.unreconciled_drift is True` ⇒ unreconciled (M6 sets it; M4 default False = "no known drift"). |

Order rationale: rung 1 is the cheapest committed-config fact and the ONE chokepoint answer (LD5); 2–3 are
latched halts no fresh data can lift; 4–5 mean nothing downstream can be evaluated honestly.

**Phase 2 — collect-ALL accumulation** (no short-circuit between stages 6–13; every applicable reason from
every evaluable stage is collected; `reasons` = sorted, deduped union; `gate_stage = null`):

| # | Stage | Reason(s) | Check (frozen semantics) |
|---|-------|-----------|--------------------------|
| 6 | `candidate` | `strategy_not_paper_eligible`, `reduce_path_not_can_open`, `short_side_disabled`, `unpriceable_candidate` | `candidate.paper_eligible is not True` (identity) ⇒ not eligible (the S9 placeholder wall). Per leg, classify against `portfolio.qty_for(symbol)` (held): **buy** with `held < 0` ⇒ `reduce_path_not_can_open` (cover — wrong chokepoint; a flip buy must be decomposed by the strategy, never silently split); **sell** with `held > 0 and qty <= held` ⇒ `reduce_path_not_can_open` (close — wrong chokepoint); **sell** otherwise (`held <= 0`, or `qty > held`) ⇒ `short_side_disabled` (FD-M4-1); `limit_price is None` **or `limit_price <= 0`** on an opening leg ⇒ `unpriceable_candidate` (FD-M4-16/RM-3). Every leg additionally carries the frozen `classification` per the §A total mapping (LD-R1 — classification and reason are assigned independently: e.g. a flip buy classifies `short_or_flip` while emitting `reduce_path_not_can_open`). |
| 7 | `universe` | `universe_excluded` | per leg: `symbol not in cfg.universe` (committed `{}` ⇒ always fires). |
| 8 | `market_state` | `market_state_missing`, `market_state_not_tradable`, `market_state_stale_default` | per leg: no verdict under the leg's symbol ⇒ missing; `verdict.tradability != TRADABLE` ⇒ not_tradable (REDUCE_ONLY blocks opens too); `"cache_stale_safe_default" in verdict.reasons` ⇒ stale_default (the safe default fires BOTH not_tradable and stale_default). Verdict identity mismatch (symbol/instrument_id vs leg), cross-leg `session_date_et` disagreement, or leg `session_date_et != margin_read.asof_session_date_et` (stale collaborator wiring is a bug, not data — safety-F6) ⇒ `RiskError`. |
| 9 | `short` | (none new in M4) | Structural slot for the locate milestone: in M4 every short-establishing leg was already rejected at stage 6; `DenyAllLocate` is NOT called by `can_open` (FD-M4-1). Reserved reasons `locate_unavailable`/`ssr_short_blocked` attach here later; `verdict.short_allowed` is the only SSR read and only this stage may ever consume it. |
| 10 | `caps` | `position_cap_exceeded`, `gross_exposure_cap_exceeded`, `net_exposure_cap_exceeded`, `sector_cap_exceeded`, `sector_unknown`, `beta_cap_exceeded`, `beta_unknown` | post-trade projection per §D (`exposure.py`): held basis = broker `market_value`; candidate additive-conservative (FD-M4-17); strict `>` vs `cfg` caps; poisoned sector/beta aggregates — a HELD or CANDIDATE symbol missing from `cfg.universe` (FD-M4-17/RM-5) — emit the `*_unknown` reasons INSTEAD of the cap reasons (never partial sums). Skipped (recorded in `stages_skipped`) iff every opening leg is unpriceable. |
| 11 | `margin` | `intraday_margin_insufficient`, `intraday_margin_deficit_outstanding` | `Σ cap_notional > account.read.buying_power − INTRADAY_MARGIN_BUFFER_USD` ⇒ insufficient (broker BP is the only headroom source, FD-M4-2); any non-minor outstanding deficit in `margin_read` ⇒ outstanding (deliberately stricter than the rule's bd5 clock — keeps the agent structurally out of "practice" territory; tighten-only). Skipped iff every opening leg is unpriceable. |
| 12 | `pdt` | `pdt_compat_blocked`, `pdt_compat_dtbp_exceeded` | per FD-M4-15: `pdt_read.state == "enforcing_legacy_pdt"` via rejection latch ⇒ blocked; enforcing AND `daytrading_buying_power` present AND `Σ cap_notional > daytrading_buying_power` ⇒ dtbp_exceeded. UNKNOWN ⇒ nothing. dtbp check skipped iff every opening leg is unpriceable. |
| 13 | `loss` | `daily_loss_breached`, `drawdown_breached`, `loss_baseline_unavailable` | defense-in-depth vs kill-switch latency (FD-M4-18): `account.read.last_equity − equity > max_daily_loss_usd` (strict) ⇒ daily_loss_breached; `loss_read.hwm_equity is None` ⇒ baseline_unavailable, else `hwm − equity > max_drawdown_usd` (strict) ⇒ drawdown_breached. |

Boundary semantics frozen for every cap: reject on strict `>` (a cap of 0 rejects any positive exposure).
Multi-leg: stages 6–8 evaluate per leg; 10–12 on the additive aggregate; any reason rejects the whole
candidate.

## 3. Module map + import discipline

```
scripts/agent/risk/
├── __init__.py          # empty package marker (mirrors broker/)
├── reasons.py           # RISK_REASONS / stage names / KILL_* vocabularies + RiskError   [stdlib only]
├── account_state.py     # BrokerAccountRead, AccountInvalid, AccountRead, AccountStore,
│                        #   PositionRead, PortfolioRead, Mark, parsers, AccountReadProvider
│                        #   [reasons, stdlib + agent.serializer]
├── risk_config.py       # RiskConfig.from_config — the ONE parser of risk_rules additions
│                        #   [stdlib + agent.config + agent.serializer]
├── exposure.py          # PURE exposure math (no I/O, no clock)  [reasons, account_state, risk_config, agent.candidate]
├── loss_limits.py       # LossLimitsMonitor + LossRead + TripSignal  [account_state, risk_config, reasons, risk_ledger]
├── intraday_margin.py   # IntradayMarginModel (CANONICAL, 26-10) + MarginObservation,
│                        #   DeficitRecord, FreezeState, add_business_days
│                        #   [account_state, reasons, risk_ledger, agent.market_calendar (types/seam only)]
├── pdt_compat.py        # LegacyPdtCompatMode (TRANSITION ONLY, mirror-only)  [account_state, reasons, risk_ledger]
├── locate.py            # LocateCheck Protocol + DenyAllLocate stub (FD-M4-1)  [stdlib only]
├── risk_kill.py         # RiskKillSwitch — SOLE sanctioned importer of agent.kill_switch (FD-M4-24)
│                        #   [agent.kill_switch, account_state, loss_limits, reasons, risk_ledger]
├── can_open.py          # RiskEngine.can_open — THE chokepoint  [agent.gates, agent.candidate,
│                        #   agent.market_state (types), reasons, risk_config, account_state,
│                        #   exposure, intraday_margin (MarginRead), pdt_compat (PdtRead),
│                        #   loss_limits (LossRead)]
└── risk_ledger.py       # RiskLedger facade → journal/risk.jsonl + rehydrate_risk_state
                         #   [reasons, recorder.persistence, agent.journal, agent.serializer]
```

`reasons.py` and `locate.py` are dependency-free within `risk/`; `account_state.py` imports only
`reasons` from `risk/` (it validates against `ACCOUNT_STATUSES` — M2-build); `risk_config.py` imports
nothing else from `risk/`; nothing imports `can_open.py` (the composer) except tests. No module in
`risk/` imports `agent.execution_preflight`, `agent.broker*`, or `agent.arming` — and only `risk_kill.py`
imports `agent.kill_switch` (FD-M4-24). Existing modules edited: **none** (`kill_switch.py`, `gates.py`,
`config.py`, `execution_preflight.py`, `broker/*` untouched; `config/risk_rules.json` is the only committed
file extended).

## A. `scripts/agent/risk/reasons.py` — vocabularies + `RiskError`

```python
# scripts/agent/risk/reasons.py
"""Closed vocabularies for the risk core. Out-of-vocab anywhere -> RiskError (FATAL,
fail-closed, never coerced) — mirrors MarketStateError (market_state.py:98)."""
from typing import FrozenSet, Tuple

class RiskError(ValueError):
    """Risk-core invariant violation (out-of-vocab reason/state/cause, identity mismatch,
    malformed collaborator input) -> FATAL. Restriction is DATA; RiskError is for bugs."""

TERMINAL_REASONS: FrozenSet[str] = frozenset({
    "run_gates_off", "kill_switch_halted", "margin_freeze_active",
    "account_missing", "account_stale", "account_invalid", "account_clock_skew",
    "portfolio_missing", "portfolio_stale", "portfolio_unreconciled",
})
ACCUMULATED_REASONS: FrozenSet[str] = frozenset({
    "strategy_not_paper_eligible", "reduce_path_not_can_open", "short_side_disabled",
    "unpriceable_candidate", "universe_excluded",
    "market_state_missing", "market_state_not_tradable", "market_state_stale_default",
    "position_cap_exceeded", "gross_exposure_cap_exceeded", "net_exposure_cap_exceeded",
    "sector_cap_exceeded", "sector_unknown", "beta_cap_exceeded", "beta_unknown",
    "intraday_margin_insufficient", "intraday_margin_deficit_outstanding",
    "pdt_compat_blocked", "pdt_compat_dtbp_exceeded",
    "daily_loss_breached", "drawdown_breached", "loss_baseline_unavailable",
})
RESERVED_REASONS: FrozenSet[str] = frozenset({"locate_unavailable", "ssr_short_blocked"})
RISK_REASONS: FrozenSet[str] = TERMINAL_REASONS | ACCUMULATED_REASONS | RESERVED_REASONS

GATE_STAGES: Tuple[str, ...] = (
    "run_gates", "kill", "margin_freeze", "account", "portfolio",
    "candidate", "universe", "market_state", "short", "caps", "margin", "pdt", "loss",
)

KILL_STATES: FrozenSet[str] = frozenset({"monitoring", "flattening", "halted"})
KILL_CAUSES: FrozenSet[str] = frozenset({"daily_loss_cap", "drawdown_cap", "operator_manual", "drill"})
RESERVED_KILL_CAUSES: FrozenSet[str] = frozenset({"live_gate_flip"})   # M8 (FD-M4-23)

ACCOUNT_STATUSES: FrozenSet[str] = frozenset({"fresh", "stale", "missing", "invalid", "skew"})
PDT_STATES: FrozenSet[str] = frozenset({"unknown", "not_enforcing", "enforcing_legacy_pdt"})
LEG_CLASSIFICATIONS: FrozenSet[str] = frozenset({"opening_long", "short_or_flip", "reducing"})
```

Frozen semantics: validators (`require_reason(code)`, `require_stage(name)`, …) raise `RiskError` on
non-membership; emitting a `RESERVED_REASONS` member in M4 is a test failure (FD-M4-23). `RISK_REASONS`
has exactly **34** members (10 terminal + 22 accumulated + 2 reserved).

**Frozen total classification mapping (LD-R1 — resolves M4C-3/F1/safety-F8/RM-11; journaled on every
`LegRead`; mirrors the `classify_iml_reducing` total-function style):**

| side | held (`portfolio.qty_for`) | qty | `classification` |
|------|---------------------------|-----|------------------|
| buy  | `held >= 0` | any | `opening_long` |
| buy  | `held < 0`  | `qty <= \|held\|` | `reducing` |
| buy  | `held < 0`  | `qty > \|held\|` | `short_or_flip` |
| sell | `held > 0`  | `qty <= held` | `reducing` |
| sell | otherwise   | any | `short_or_flip` |

Opening legs (the notional/caps/margin/pdt basis) = classification ∈ {`opening_long`, `short_or_flip`};
`reducing` legs contribute to no notional (they are rejected at stage 6 with `reduce_path_not_can_open` —
wrong chokepoint). Classification and stage-6 reason are assigned independently (a flip buy is
`short_or_flip` + `reduce_path_not_can_open`). On a phase-1 TERMINAL verdict the candidate stage is never
evaluated and the frozen terminal shape is `legs = ()`, `gross_notional = Decimal("0")`,
`session_date_et = None`, `caps_used = ()`, marks not consulted (LD-R1; asserted byte-exact by the
committed-config canary, §J/§M test 8).

## B. `scripts/agent/risk/account_state.py` — read models, parser chokepoint, freshness store

```python
# scripts/agent/risk/account_state.py
"""Broker account/portfolio read models. The broker's numbers are GROUND TRUTH (FD-M4-2):
this module copies, validates, and stamps them — it never recomputes them. Validation is
constructive: an invalid payload never becomes a snapshot; it becomes AccountInvalid (S2)."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Optional, Protocol, Tuple, Union, runtime_checkable

from agent.serializer import BrokerUSD

ACCOUNT_FRESHNESS_TTL_MS = 5000     # CODE CONSTANTS (FD-M4-22); ctor override may only SHORTEN
PORTFOLIO_FRESHNESS_TTL_MS = 5000   #   (HIGH-3 clamp, market_state_cache.py:52-62)
MARK_FRESHNESS_TTL_MS = 2000        # mirrors DEFAULT_FRESHNESS_TTL_MS (market_state_cache.py:34)
USD_QUANTUM = Decimal("0.01")       # account_snapshot-row-ONLY quantum, quantize-only (FD-M4-25/LD-R5)

ACCOUNT_SOURCES = frozenset({"alpaca_paper", "alpaca_live", "fixture", "spy"})  # closed vocab

@dataclass(frozen=True)
class BrokerAccountRead:
    """One reconciled broker account() read, parsed from a raw dict payload.
    REQUIRED money fields are exact BrokerUSD (no quantization at parse, FD-M4-25)."""
    equity: BrokerUSD                   # REQUIRED, finite
    last_equity: BrokerUSD              # REQUIRED, finite — the daily-loss base (FD-M4-18)
    cash: BrokerUSD                     # REQUIRED, finite
    buying_power: BrokerUSD             # REQUIRED, finite, >= 0
    maintenance_margin: BrokerUSD       # REQUIRED, finite, >= 0 ("margin to be maintained", V3)
    multiplier: Optional[Decimal]       # provenance only
    daytrading_buying_power: Optional[BrokerUSD]
    pattern_day_trader: Optional[bool]  # strict bool or None (absence is pdt_compat evidence)
    daytrade_count: Optional[int]       # provenance only — NEVER an input to any gate (FD-M4-2)
    source: str                         # ∈ ACCOUNT_SOURCES
    ts_read_utc: str                    # broker/report wall stamp (provenance, never compared)
    seen_at_ms: int                     # injected monotonic receipt stamp
    account_snapshot_id: str            # "as-" + row_hash(§K.3 canonical dict)

@dataclass(frozen=True)
class AccountInvalid:
    reason: str       # e.g. "missing_field:buying_power", "non_finite:equity",
                      # "float_typed:cash", "bool_typed:equity", "negative:buying_power"
    source: str
    seen_at_ms: int

def parse_account_payload(payload: Mapping, *, source: str,
                          seen_at_ms: int, ts_read_utc: str
                          ) -> Union[BrokerAccountRead, AccountInvalid]: ...

@dataclass(frozen=True)
class AccountRead:
    """can_open's third positional argument: the latest read + the freshness verdict,
    computed by AccountStore.get at snapshot time (LD5: can_open re-checks nothing)."""
    status: str                          # ∈ ACCOUNT_STATUSES
    read: Optional[BrokerAccountRead]    # present iff status ∈ {"fresh","stale"}
    age_ms: Optional[int]
    invalid_reason: Optional[str]        # set iff status == "invalid"

class AccountStore:
    """Freshness-gated, NON-BLOCKING (MarketStateCache mirror: strict '>' boundary,
    degrade-to-reject-opens, no inline refresh)."""
    def __init__(self, *, clock, ttl_ms: int = ACCOUNT_FRESHNESS_TTL_MS) -> None: ...
        # raises ValueError if ttl_ms > ACCOUNT_FRESHNESS_TTL_MS (shorten-only clamp)
    def put(self, result: Union[BrokerAccountRead, AccountInvalid]) -> None: ...
    def get(self, *, now_ms: Optional[int] = None) -> AccountRead: ...
        # never put -> "missing"; last put AccountInvalid -> "invalid";
        # now_ms < seen_at_ms -> "skew"; age > ttl (strict) -> "stale"; else "fresh".
    def latest_unsafe(self) -> Optional[BrokerAccountRead]: ...
        # Last-known read regardless of freshness. SOLE legitimate consumer: the CALLER
        # assembling the kill/flatten annotation input for RiskKillSwitch.trigger's
        # keyword-only `account` kwarg (M4C-1; FD-M4-3 — staleness never blocks a reduce).
        # can_open MUST NOT call this (test-asserted).

@dataclass(frozen=True)
class PositionRead:
    symbol: str
    qty: Decimal                  # SIGNED, finite, != 0 (zero-qty rows are dropped at parse)
    market_value: BrokerUSD       # REQUIRED signed broker-reported MV (FD-M4-17)
    avg_entry_price: Optional[Decimal]
    instrument_id: Optional[int]  # broker payloads carry none; mapped when the caller can

@dataclass(frozen=True)
class PortfolioRead:
    positions: Tuple[PositionRead, ...]   # sorted by symbol; duplicate symbol -> ValueError
    source: str
    seen_at_ms: int
    stale: bool                           # precomputed by the caller against PORTFOLIO TTL
    unreconciled_drift: bool = False      # M6 sets True on a KNOWN reconcile mismatch
    def qty_for(self, symbol: str) -> Decimal: ...   # Decimal("0") if not held

def parse_positions_payload(rows, *, source: str, seen_at_ms: int,
                            stale: bool = False) -> PortfolioRead: ...
    # malformed row (float/non-finite/missing market_value/duplicate symbol) -> ValueError;
    # qty==0 rows are SILENTLY DROPPED at parse (flat is not held — deterministic filter,
    # LD-R4/M4C-7). (caller maps a failed parse to portfolio_missing — fail-closed)

def portfolio_is_stale(seen_at_ms: int, now_ms: int, *,
                       ttl_ms: int = PORTFOLIO_FRESHNESS_TTL_MS) -> bool: ...
    # strict '>' boundary; raises ValueError if ttl_ms > PORTFOLIO_FRESHNESS_TTL_MS
    # (shorten-only clamp, FD-M4-22). The M5 caller MUST use this helper to precompute
    # PortfolioRead.stale — the TTL arithmetic is owned and tested in M4, not in M5.

@dataclass(frozen=True)
class Mark:
    """Optional conservative notional tightener (FD-M4-16). mid sourced from
    QuoteVerdict.mid (quote_quality.py:101-103, MID_QUANTUM-quantized)."""
    symbol: str
    instrument_id: int
    mid: Decimal                 # finite, > 0, MID_QUANTUM grid
    seen_at_ms: int
    source: str                  # frozen vocab {"quote_mid"} in M4

@runtime_checkable
class AccountReadProvider(Protocol):
    """The broker seam (LD8). Returns RAW payloads; parsing has ONE chokepoint above.
    M4 ships only FakeAccountProvider (tests/lib/risk_fixtures.py); M5 binds Alpaca."""
    def account_payload(self) -> Mapping: ...
    def positions_payload(self) -> list: ...
```

Frozen semantics:

- **Parser posture (S2):** required keys `equity, last_equity, cash, buying_power, maintenance_margin`;
  optional keys `multiplier, daytrading_buying_power, pattern_day_trader, daytrade_count` (these are
  Alpaca's own field names so the M5 adapter is a near-passthrough). Money accepts `Decimal` or `str`
  (`Decimal(str(value))`); a `float` instance, `bool` in a money slot, non-finite value, missing required
  key, or negative `buying_power`/`maintenance_margin` ⇒ `AccountInvalid(reason=...)` — never an exception
  on the read path and never a constructed snapshot. Out-of-vocab `source` ⇒ `ValueError` (programming
  error). The M0 spy payload (`alpaca.py:26`, Decimal-typed, only 2 keys) parses to `AccountInvalid`
  (missing required keys) — correct: the M0 stub is not an account of record.
- **Staleness:** strict `>` against the injected ms clock; `now_ms < seen_at_ms` ⇒ `"skew"` (clock
  regression is DATA, FD-M4-22). `AccountRead.status` ∈ `ACCOUNT_STATUSES`; `"stale"` still carries the
  read (inspectable, but `can_open` terminal-rejects).
- **`account_snapshot_id`** is deterministic: `"as-" + row_hash(...)` over the §K.3 canonical dict — every
  risk decision is traceable to the exact broker numbers it used (S6).
- No wall clock, no I/O, no network; the provider Protocol is consumed by the M5 orchestrator, never by
  `can_open` (LD5).

## C. `scripts/agent/risk/risk_config.py` + committed config additions

```python
# scripts/agent/risk/risk_config.py
"""RiskConfig: the ONE parser of the risk_rules additions (SignalConfig posture,
signal_config.py:76-115 — closed key sets, fail-loud ValueError at startup, parsed once)."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Tuple

INTRADAY_MARGIN_BUFFER_USD = Decimal("0")   # CODE CONSTANT (FD-M4-6: inverted polarity —
                                            # min()-merge would loosen it; raising = code change)
SHORTS_SUPPORTED = False                    # CODE CONSTANT (FD-M4-1): ANDed with config;
                                            # no overlay/commit can enable shorts before locate exists

@dataclass(frozen=True)
class SymbolRisk:
    sector: str        # non-empty slug
    beta: Decimal      # finite, parsed from the committed Decimal-string

@dataclass(frozen=True)
class RiskConfig:
    max_position_usd: Decimal
    max_gross_exposure_usd: Decimal
    max_net_exposure_usd: Decimal
    max_daily_loss_usd: Decimal
    max_drawdown_usd: Decimal
    max_sector_exposure_usd: Decimal
    max_abs_beta_notional_usd: Decimal
    short_selling_enabled: bool            # identity-strict; committed false
    universe: Mapping[str, SymbolRisk]     # FD-M4-21; committed {}
    rules_hash: str                        # of the WHOLE assembled config (config.py:17)

    @classmethod
    def from_config(cls, config: dict) -> "RiskConfig": ...
        # config = the assembled {"agent_rules":..., "risk_rules":...} dict.
        # Caps must be JSON ints >= 0 (bool excluded); unknown/missing keys in
        # risk_rules.caps / risk_rules.risk -> ValueError; universe entries must be
        # exactly {"sector": <non-empty str>, "beta": <finite Decimal-string>}.
```

Note (M4C-9): `RiskConfig.rules_hash` is computed over the assembled `{"agent_rules": ..., "risk_rules":
...}` dict and therefore does NOT match M3's per-stream `rules_hash` (`SignalConfig.from_config` hashes
only the dict it is passed — `agent_rules` alone at every current call site). Cross-stream joins key on
`run_id`/`decision_id`, never on `rules_hash` equality (an M5 alignment may unify the input shape).

Committed `config/risk_rules.json` becomes (run gates + `live_trading` byte-identical; the three existing
caps keep their key names and `0` values):

```json
{
  "live_trading": { "enabled": false, "max_live_position_usd": 0 },
  "caps": {
    "max_position_usd": 0,
    "max_gross_exposure_usd": 0,
    "max_net_exposure_usd": 0,
    "max_daily_loss_usd": 0,
    "max_drawdown_usd": 0,
    "max_sector_exposure_usd": 0,
    "max_abs_beta_notional_usd": 0
  },
  "risk": {
    "short_selling": { "enabled": false },
    "universe": {}
  }
}
```

Posture table (M2 §G discipline, applied):

| Quantity | Home | Why |
|---|---|---|
| `caps.*` (7 keys) | config, **JSON integers (whole USD)**, committed **0** | smaller == safer ⇒ `tighten_only_merge` `min()` is correct (config.py:35-41): overlays lower, never raise. Parsed `Decimal(int)` — no float ever constructed. Zero caps = a second nothing-opens wall behind the run gates (FD-M4-6). |
| `risk.short_selling.enabled` | config bool, committed `false`, ANDed with `SHORTS_SUPPORTED=False` | bool AND-merge tightens; the code constant means no overlay or commit alone can enable shorts (FD-M4-1). |
| `risk.universe` | config, strings/dicts, committed `{}` | non-numeric leaves ⇒ merge keeps base (config.py:42-43): overlays cannot tamper with sector/beta metadata; any change is a commit ⇒ new `rules_hash` (FD-M4-21). |
| `INTRADAY_MARGIN_BUFFER_USD`, minor-deficit 5%/$1,000, bd5/bd15/90cd windows, TTLs | **CODE CONSTANTS** | regulatory facts and inverted-polarity values are not knobs (FD-M4-6/14/22; M2 §G). |

Canary obligations: `test_config_canary.py` gains assertions, loses none — committed caps all read integer
`0`; `risk.short_selling.enabled is False`; a hostile overlay (raised caps / `short_selling: true` /
altered universe / injected keys) merges back to the committed values via `tighten_only_merge`.

## D. `scripts/agent/risk/exposure.py` — pure exposure math

```python
# scripts/agent/risk/exposure.py
"""PURE Decimal exposure math. No I/O, no clock, no journal. Poisoned-aggregate posture:
a held symbol missing from cfg.universe yields a marker, NEVER a partial sum (FD-M4-17)."""

@dataclass(frozen=True)
class UnknownMeta:
    kind: str                      # "sector" | "beta"
    symbols: Tuple[str, ...]       # sorted offenders

def leg_cap_notional(leg: "Leg", mark: Optional[Mark], *, now_ms: int,
                     ttl_ms: int = MARK_FRESHNESS_TTL_MS
                     ) -> Tuple[Optional[Decimal], bool]:
    """FD-M4-16: (qty × max(limit_price, mark.mid), mark_used=True) when a fresh
    (strict '>' ttl_ms) identity-matched mark exists; else (qty × limit_price, False);
    (None, False) when limit_price is None OR limit_price <= 0 (RM-3 — a non-positive
    limit is unpriceable, evaluated before any notional math). ttl_ms may only SHORTEN
    (ValueError if > MARK_FRESHNESS_TTL_MS — FD-M4-22 clamp).
    Mark identity mismatch vs the leg -> RiskError."""

def gross_exposure(portfolio: PortfolioRead) -> Decimal          # Σ |market_value|
def net_exposure(portfolio: PortfolioRead) -> Decimal            # Σ signed market_value
def symbol_exposure(portfolio: PortfolioRead, symbol: str) -> Decimal   # |market_value| of symbol
def sector_exposure(portfolio, universe) -> Union[Mapping[str, Decimal], UnknownMeta]
                                                                 # Σ |market_value| per sector
                                                                 # (absolute — RM-4: a held
                                                                 # short ADDS to its sector)
def beta_notional(portfolio, universe) -> Union[Decimal, UnknownMeta]   # Σ signed mv × beta

def project_caps(candidate: "Candidate", notionals: Mapping[int, Decimal],
                 portfolio: PortfolioRead, cfg: RiskConfig
                 ) -> Tuple[Tuple[str, ...], Tuple[Tuple[str, str, str], ...]]:
    """Returns (sorted cap-violation reasons, caps_used rows). notionals maps leg index ->
    cap_notional for PRICEABLE opening legs only. Candidate impact is additive-conservative:
    |notional| -> gross / per-symbol / sector; signed notional (buy +, sell −) -> net / beta.
    Strict '>' breaches. UnknownMeta -> 'sector_unknown'/'beta_unknown' replaces the cap check.
    A CANDIDATE leg symbol not in cfg.universe poisons the sector AND beta projections exactly
    like a held symbol (sector_unknown + beta_unknown; never a partial sum, never a silent
    skip — FD-M4-17/RM-5: the always-case on the committed empty universe)."""
```

Frozen `caps_used` row format (Dec-strings, sorted by name): `("max_position_usd:<SYMBOL>", used, limit)`
per involved symbol; `("max_gross_exposure_usd", used, limit)`; `("max_net_exposure_usd", |used|, limit)`;
`("max_sector_exposure_usd:<sector>", used, limit)` per involved sector; `("max_abs_beta_notional_usd",
|used|, limit)`; the margin stage contributes `("buying_power", Σnotional, bp)` and (when checked)
`("daytrading_buying_power", Σnotional, dtbp)`; the loss stage contributes `("max_daily_loss_usd", loss,
cap)` and `("max_drawdown_usd", dd, cap)`. `used`/`limit` are the projected post-trade values as EXACT
canonical Decimal strings, rendered once at `RiskVerdict` construction and NEVER re-quantized by the
ledger (LD-R5 — `caps_used` is exempt from `USD_QUANTUM`, which applies only to `account_snapshot` rows).
The FINAL `caps_used` tuple is the FULL union (caps + margin + loss rows) sorted by name ONCE after all
stages contribute (M1-build — "sorted by name" is the single ordering rule; no append-order ambiguity).

## E. `scripts/agent/risk/intraday_margin.py` — the CANONICAL FINRA 26-10 model (S10)

```python
# scripts/agent/risk/intraday_margin.py
"""IntradayMarginModel (Rule 4210(d)(2), Notice 26-10 — §0.2 V1-V10). COMPUTES deficit
detection, the outstanding ledger, business-day windows, freeze state, minor classification.
RECONCILES (never derives) equity / maintenance_margin / buying_power (FD-M4-2): the only
arithmetic on broker numbers is subtraction/max/comparison. NO margin percentage, NO $25k,
NO day-trade count exists in this module (source-scan test). Stateful per account;
rehydratable from journal/risk.jsonl (pure fold, §K)."""

# regulatory CODE CONSTANTS (FD-M4-6/14; never config):
SATISFACTION_DEADLINE_BD = 5          # V8
OUTSTANDING_WINDOW_BD = 15            # V7
FREEZE_CALENDAR_DAYS = 90             # V8
MINOR_DEFICIT_USD = Decimal("1000")   # V9
MINOR_DEFICIT_EQUITY_PCT = Decimal("0.05")  # V9
_BUSINESS_DAY_SCAN_LIMIT = 4          # max calendar days scanned per business day requested

def add_business_days(start_date_et: str, n: int, *, calendar: "ScheduleProvider") -> str:
    """The (n)th XNYS trading session strictly after start_date_et (FD-M4-8): forward
    calendar-date scan (date.fromisoformat + timedelta(days=1)) consulting
    calendar.is_trading_day per date. Scan bound = n * _BUSINESS_DAY_SCAN_LIMIT + 10 days;
    exceeding it, or UnknownSessionDate (fixture coverage exhausted), raises (fail-loud —
    windows are computed EAGERLY at detection, so coverage gaps surface immediately;
    detection-row-first ordering + the margin_window_unresolved fallback: safety-F11,
    normative paragraph below the S10 table)."""

def classify_iml_reducing(side: str, qty: Decimal, held_qty: Decimal) -> bool:
    """V2, validated against the HELD position (never a self-asserted flag — the
    mint_reduce_only_token discipline, execution_preflight.py:97-118). Total function:
      buy,  held >= 0                  -> True   (purchase, not covering)
      buy,  held < 0, qty <= |held|    -> False  (cover)
      buy,  held < 0, qty >  |held|    -> True   (flip remainder increases exposure)
      sell, held > 0, qty <= held      -> False  (close)
      sell, otherwise                  -> True   (short establish/increase)
    Out-of-vocab side -> RiskError."""

@dataclass(frozen=True)
class MarginObservation:
    """One reconciled point-in-time margin read, built from a BrokerAccountRead via
    observation_from_read(read, *, session_date_et, after_iml_reducing, eod=False)."""
    session_date_et: str
    ts_read_utc: str
    equity: BrokerUSD
    maintenance_margin: BrokerUSD
    after_iml_reducing: bool      # True iff this read follows an IML-reducing transaction
    eod: bool                     # True for the close_of_day observation
    account_snapshot_id: str
    @property
    def iml(self) -> Decimal: ...        # equity - maintenance_margin (exact, V2)
    @property
    def deficiency(self) -> Decimal: ... # max(0, maintenance_margin - equity) (exact, V3)

@dataclass(frozen=True)
class DeficitRecord:
    deficit_id: str               # "imd-" + row_hash({"session_date_et": D}) — FD-M4-13
    session_date_et: str          # deficit date D (ONE record per date)
    amount: Decimal               # > 0; max-merged monotonically over the day (V3/V5)
    minor: bool                   # FD-M4-14, recomputed at each increase; LATCHED one-way
                                  #   (once non-minor, never back to minor — safety-F3)
    equity_at_detection: BrokerUSD
    iml_eod_d: Optional[Decimal]  # IML at EOD of D (satisfaction baseline, V6/FD-M4-12);
                                  #   None (close_of_day(D) never ran) => UNSATISFIABLE,
                                  #   skipped by the satisfaction scan (M4C-2/RM-6)
    satisfaction_deadline_et: Optional[str]  # add_business_days(D, 5); None ONLY while
    expires_after_et: Optional[str]          # add_business_days(D, 15); margin_window_unresolved
                                             #   (safety-F11 — record treated outstanding-NON-MINOR)
    satisfied_on_et: Optional[str]

@dataclass(frozen=True)
class FreezeState:
    active: bool
    trigger_deficit_id: Optional[str]
    effective_from_et: Optional[str]  # first business day after the triggering bd5 close
    expires_on_et: Optional[str]      # effective_from + 90 calendar days (EXCLUSIVE end)
    def active_on(self, session_date_et: str) -> bool: ...
        # FD-M4-11: effective_from_et <= session_date_et < expires_on_et (string-date compare
        # is safe: ISO dates). Overlapping triggers max-merge (tighten-only, F13): the
        # ORIGINAL trigger_deficit_id and effective_from_et are KEPT; only expires_on_et
        # extends; the merge journals a SECOND margin_freeze_start row carrying the merged end.

@dataclass(frozen=True)
class MarginRead:
    """The injected margin input to can_open (LD5). Snapshot of model state."""
    outstanding_nonminor: Tuple[DeficitRecord, ...]
    freeze: FreezeState
    asof_session_date_et: str

class IntradayMarginModel:
    def __init__(self, *, calendar: "ScheduleProvider", ledger: Optional["RiskLedger"]) -> None: ...
    def observe(self, obs: MarginObservation) -> Optional[DeficitRecord]: ...
    def close_of_day(self, session_date_et: str, eod: MarginObservation) -> None: ...
    def read(self, asof_session_date_et: str) -> MarginRead: ...
    def outstanding(self, asof_date_et: str) -> Tuple[DeficitRecord, ...]: ...
    def freeze_state(self) -> FreezeState: ...
    def practice_count(self) -> int: ...   # observability ONLY (FD-M4-10); gates nothing.
        # RM-10 definition: count of NON-MINOR deficits that reached their bd5 close
        # unsatisfied — computed on demand from the records (no separate journaled
        # state, hence deliberately rehydrate-free).
    def rehydrate(self, rows) -> None: ... # re-seed from rehydrate_risk_state(rows)["margin"]
```

### S10 mechanics, item by item (each row = testable behavior + fixture design)

| S10 item | Verified basis | Frozen model behavior | Fixture design (`tests/lib/risk_fixtures.py` → `tests/agent/test_intraday_margin.py`) |
|---|---|---|---|
| **IML-reducing transactions** | V2 | `classify_iml_reducing` per the total function above; deficiency is evaluated ONLY at observations with `after_iml_reducing=True` ("highest deficiency *following* an IML-reducing transaction"); closes/deposits create no observation point. The notice's optional start-of-day offset reliefs are NOT used (observed sequence only — conservative). | Scripted fill sequence: deposit → open-buy → close-sell → cover-buy → flip-buy(150 vs short 100) with held-qty bookkeeping; assert classification per step (flip ⇒ True); assert a sell against zero held classifies True (short establish). |
| **Deficit calculation** | V3, V4, V5 | Per session date D: `amount = max over after_iml_reducing observations of max(0, maintenance_margin − equity)` — exact Decimals, broker fields only. Max-merge is monotonic; a later larger observation journals `cause="increased"` on the SAME `deficit_id`; EOD single-calculation (V4) equals the day's deficit ONLY when EOD is the day's worst after-IML-reducing point (the one-observation degenerate case — RM-8; the M5 cadence obligation is in §O) and never lowers an intraday max; same-timestamp observations fold by max (V5 worst-ordering). Changing ONLY broker numbers flips the outcome (R3). | `margin_day()` builder emitting observation sequences from string-Decimal payloads: equity exactly equal to maintenance ⇒ NO deficit (deficiency 0); 0.01 under ⇒ amount `0.01`; sequence 100→250→180 ⇒ amount 250 with two detected rows (opened, increased); dip BEFORE any IML-reducing observation ⇒ no record; single-EOD run equals continuous run when EOD is the day's worst. |
| **15-business-day outstanding window** | V7 | `expires_after_et = add_business_days(D, 15)` (XNYS sessions, FD-M4-8), computed eagerly at detection. Outstanding on date X iff `satisfied_on_et is None` and `X <= expires_after_et`; at `close_of_day(expires_after_et)` the record expires ("immediately after the close") and journals `margin_deficit_expired`. Expired records stop blocking but stay in the ledger. | New committed fixture `nyse_margin_window_v1.json` (§L): ≥30 contiguous sessions incl. one mid-window holiday + weekends; assert bd15 lands on the 15th SESSION (holiday shifts it); outstanding at the bd15 close, expired at the next fold; satisfaction on bd3 clears earlier. |
| **5-bd / 90-calendar-day freeze trigger** | V8 | At every `close_of_day(E)`, AFTER the satisfaction scan and bd15 expiry (the frozen intra-close order, RM-9 — step list below the table), EVERY record — minor included (LD-R2) — with `satisfaction_deadline_et <= E` still unsatisfied ⇒ freeze (catch-up predicate: a missed EOD on the deadline date can NEVER skip a freeze — safety-F2/F7-build): `effective_from_et = add_business_days(satisfaction_deadline_et, 1)` (anchored to the DEADLINE date, not to E — LD-R6), `expires_on_et = effective_from + 90 days` (pure date math), full duration, no early lift, ALL opens blocked via ladder rung 3 (FD-M4-10/11 — "makes a practice" assumed met). Reduces untouched (the rule itself carves out closing). `margin_freeze_start` journaled at trigger/merge; `margin_freeze_end` emitted by the FIRST `close_of_day(E)` with `E >= expires_on_et` (observability only — `active_on` stays the authority, F13). | Timeline fixtures over the new calendar: satisfied bd4 ⇒ never frozen; bd5 EOD delta qualifies ⇒ not frozen (RM-9 — satisfaction is only determinable AT the close); unsatisfied at bd5 close ⇒ frozen with exact dates; close_of_day skipped on bd5, first run on bd6 ⇒ STILL frozen, `effective_from_et` anchored to bd5's deadline (LD-R6); a MINOR record unsatisfied at bd5 close ⇒ frozen too (LD-R2); boundary: open rejected on calendar day 90 (`expires_on − 1`), allowed on day 91 (`expires_on`); reduce path provably independent throughout (paired mint test); second trigger inside a freeze max-merges the end (original `trigger_deficit_id`/`effective_from_et` kept; second `margin_freeze_start` row — F13). |
| **Minor-deficit exceptions** | V9 | `minor = amount <= min(equity × 0.05, 1000)` at the detecting observation's equity; recomputed on each increase but LATCHED one-way — once non-minor, NEVER back to minor, however high a later observation's equity (safety-F3); equity invalid ⇒ NOT minor (FD-M4-14). Minor records journal/satisfy/expire identically, never enter `MarginRead.outstanding_nonminor` or `practice_count`, but DO trigger the bd5 freeze (LD-R2 — V9's exception is practice-prong-only, RM-2). | Boundary triple: equity 18,000 (5% = 900 binds): amount 900 ⇒ minor, 900.01 ⇒ not; equity 100,000 ($1,000 binds): 1000 ⇒ minor, 1000.01 ⇒ not; a minor record unsatisfied at bd5 close ⇒ FREEZES (LD-R2) while still absent from `outstanding_nonminor`; an `increased` merge crossing the threshold flips minor → non-minor; the REVERSE direction (non-minor at low equity, increase at high equity) stays non-minor and still freezes at bd5 (safety-F3). |
| **Legacy PDT is compat-only** | V1, V10 | Quarantined entirely in `pdt_compat.py` (§F): mirror-only, tighten-only, no constants (FD-M4-2/15); never an input to THIS model. | `test_pdt_compat.py` (§F) + source-scan: no literal `25000`, no margin percentage, no day-trade arithmetic anywhere under `risk/`. |

**Satisfaction (V6, cross-cutting — FD-M4-12) + the FROZEN intra-close order (RM-9/LD-R6).** Within
`close_of_day(E)`, in this exact order: **(1)** store `IML_eod(E)` and run the satisfaction scan — for
every outstanding record with `D < E`: satisfied iff `IML_eod(E) − iml_eod_d >= amount` (exact Decimal);
first qualifying E wins; journals `margin_deficit_satisfied` with the delta basis; **(2)** bd15 expiry;
**(3)** the freeze trigger over every record with `satisfaction_deadline_et <= E` still unsatisfied
(catch-up, LD-R6). A record with `iml_eod_d is None` (close_of_day(D) was never executed — crash/downtime
on D) is **UNSATISFIABLE**: the scan SKIPS it (this is DATA, never a raise — close_of_day always continues
to steps 2–3), it stays outstanding (fail-closed), still expires at bd15, still freeze-triggers at bd5; a
`margin_deficit_unbaselined` marker row is journaled ONCE per such record (M4C-2/RM-6/F10). The rehydrate
join is explicit: a deficit date with no matching eod `iml_observation` row ⇒ `iml_eod_d = None` (§K.4).
`iml_eod_d` is set by `close_of_day(D)` (a record detected after its own EOD — impossible by construction
— would raise `RiskError`). Same-day satisfaction is NOT evaluated ("end of such day to the end of a
*subsequent* day").

**Window resolution (safety-F11, normative ordering inside `observe()`):** `observe()` journals the
`margin_deficit_detected` row (amount, minor, equity) BEFORE computing the bd5/bd15 windows. If window
computation fails (`UnknownSessionDate` / scan-limit), the ledger receives a `margin_window_unresolved`
row (once per record), the record is treated as **outstanding-NON-MINOR** with both window dates `None`
until a later successful recomputation resolves them (fail-closed — it blocks opens via rung 11
regardless), and the exception then propagates (fail-loud). A coverage gap can therefore never produce a
silently-unjournaled deficit.

**Pre-trade consumption** is in `can_open` rungs 3 (`margin_freeze_active`, terminal) and 11
(`intraday_margin_deficit_outstanding` while any non-minor record is outstanding — deliberately stricter
than the rule's bd5 clock and deliberately still NON-minor-scoped (LD-R2 widens only the freeze trigger);
`intraday_margin_insufficient` when `Σ cap_notional > buying_power −
INTRADAY_MARGIN_BUFFER_USD`, broker BP used as-is).

## F. `scripts/agent/risk/pdt_compat.py` — transition-only mirror (FD-M4-15)

```python
# scripts/agent/risk/pdt_compat.py
"""LegacyPdtCompatMode: mirror what the broker ACTUALLY enforces during the 26-10 phase-in
(V10, until 2027-10-20) — detected, never assumed, never re-derived (FD-M4-2). Tighten-only
by construction: this module can only ADD reasons; nothing here can mark a candidate allowed
or suppress another gate's reason."""

PDT_REJECTION_CODES = frozenset({40310100})   # Alpaca's documented PDT-protection code
PDT_REJECTION_MARKERS = ("pattern day trad", "day trading buying power", "day-trade")
                                              # lowercase substring match (frozen)

@dataclass(frozen=True)
class PdtRead:
    state: str                  # ∈ PDT_STATES (reasons.py)
    evidence: Optional[str]     # ∈ {"account_flag", "broker_rejection"} when enforcing
    rejection_latched: bool     # True once any rejection evidence seen this run

@dataclass(frozen=True)
class BrokerRejectionObservation:
    """Frozen NOW so detection is fixture-testable; M5's order path produces these."""
    code: Optional[int]
    message: str
    ts_utc: str

class LegacyPdtCompatMode:
    def __init__(self, *, ledger: Optional["RiskLedger"],
                 rehydrated_state: Optional[Mapping] = None) -> None: ...
        # rehydrated_state = rehydrate_risk_state(rows)["pdt"] (LD-R3): a rehydrated
        # rejection_latched=True RE-LATCHES enforcing_legacy_pdt — the latch is DURABLE
        # across runs. M5's orchestrator MUST seed this (§O).
    def observe_account(self, read: BrokerAccountRead) -> PdtRead: ...
    def observe_broker_rejection(self, obs: BrokerRejectionObservation) -> PdtRead: ...
    def read(self) -> PdtRead: ...
```

Frozen semantics:

- **Detection (pure, total — never raises on weird payloads):** `pattern_day_trader is True` ⇒
  `enforcing_legacy_pdt` (evidence `account_flag`); `is False` ⇒ `not_enforcing`; `is None` ⇒ `unknown`. A
  rejection whose `code ∈ PDT_REJECTION_CODES` OR whose lowercased message contains any
  `PDT_REJECTION_MARKERS` substring **latches** `enforcing_legacy_pdt` (evidence `broker_rejection`) —
  a later `pattern_day_trader=False` read NEVER unlatches it, and the latch is **DURABLE across runs**
  (LD-R3, resolving F3/M4C-8): it survives rehydrate via the `rehydrated_state` ctor arg and clears only
  by deliberate operator action (a fresh/rotated journal after review), never by data. Every state change
  journals `pdt_regime_transition`.
- **Gate consumption (ladder rung 12):** `rejection_latched` ⇒ `pdt_compat_blocked` (mirror the broker's
  demonstrated enforcement: stop trying to open until an operator reviews); `enforcing_legacy_pdt` AND
  `daytrading_buying_power is not None` AND `Σ cap_notional > daytrading_buying_power` ⇒
  `pdt_compat_dtbp_exceeded`. `unknown` blocks nothing by itself (FD-M4-15 rationale recorded there).
- `daytrade_count` is carried as journal provenance only. When Alpaca completes the phase-in this mode goes
  `not_enforcing` by observation alone — no code change.

## G. `scripts/agent/risk/loss_limits.py` — daily-loss + HWM drawdown monitor

```python
# scripts/agent/risk/loss_limits.py
@dataclass(frozen=True)
class TripSignal:
    cause: str                    # ∈ {"daily_loss_cap","drawdown_cap"}
    measured_usd: Decimal         # the breaching value (exact)
    cap_usd: Decimal
    equity: BrokerUSD
    basis: str                    # ∈ {"last_equity","high_water_mark"}
    session_date_et: str
    account_snapshot_id: str

@dataclass(frozen=True)
class LossRead:
    hwm_equity: Optional[BrokerUSD]    # None until the first FRESH observation this run
    daily_loss_usd: Optional[Decimal]  # last_equity - equity at last observation
    drawdown_usd: Optional[Decimal]    # hwm - equity at last observation
    breaches: Tuple[str, ...]          # sorted ⊆ {"daily_loss_breached","drawdown_breached"}

class LossLimitsMonitor:
    def __init__(self, *, cfg: RiskConfig, ledger: Optional["RiskLedger"]) -> None: ...
    def observe(self, account: AccountRead, *, session_date_et: str) -> Optional[TripSignal]: ...
    def read(self) -> LossRead: ...
    def rehydrate(self, rows) -> None: ...   # re-seed HWM from loss_hwm_update rows
```

Frozen semantics (FD-M4-18): `observe` acts ONLY on `account.status == "fresh"` — stale/missing/invalid/
skew reads update nothing and can never trip (FD-M4-3; the caller's edge-triggered `account_alert` row is
the visibility). On a fresh read: HWM = `max(hwm, equity)` (a new high journals `loss_hwm_update` —
edge-triggered, sparse); `daily_loss = last_equity − equity`; `drawdown = hwm − equity`; a value strictly
`>` its cap returns the corresponding `TripSignal` (daily-loss checked first; one signal per call). The
monitor holds no broker and submits nothing — the caller feeds the signal to `RiskKillSwitch.trigger`.
Cap 0 = zero budget (any positive loss trips); committed caps ARE 0, which is inert in M4 (gates off ⇒ no
positions ⇒ equity static) and the boundary tests use `permissive_fixture_config()` (§L — M3-build).

## H. `scripts/agent/risk/locate.py` — the LD1 seam stub

```python
# scripts/agent/risk/locate.py
"""Short-locate seam (FD-M4-1). M4 ships ONLY the Protocol + the deny-all implementation.
can_open does NOT call this in M4 (shorts are rejected structurally at ladder stage 6);
the module exists so the short-side milestone is an additive contract change, not a new wire."""
from decimal import Decimal
from typing import Protocol, Tuple, runtime_checkable

@runtime_checkable
class LocateCheck(Protocol):
    def locate(self, symbol: str, qty: Decimal) -> Tuple[bool, str]: ...

class DenyAllLocate:
    """The only M4 implementation: every locate fails closed."""
    def locate(self, symbol: str, qty: Decimal) -> Tuple[bool, str]:
        return (False, "short_side_disabled")
```

## I. `scripts/agent/risk/risk_kill.py` — kill-switch state machine + S8 drill (FD-M4-4/19/20)

```python
# scripts/agent/risk/risk_kill.py
"""RiskKillSwitch: trigger evaluation + latched state machine + journaling. DELEGATES
flattening to the M0 actuator agent.kill_switch.KillSwitch (FD-M4-4: a FRESH M0 instance
per flatten/retry pass; this module is the SOLE sanctioned importer of agent.kill_switch —
FD-M4-24). It can never emit an opening order: it references no mint function and no token
type; the only reduce-only minting site in the codebase stays kill_switch.py:41."""

@dataclass(frozen=True)
class KillEvaluation:
    cause: Optional[str]           # ∈ KILL_CAUSES or None
    skipped: bool                  # True iff account not fresh (no judgment possible)
    daily_loss_usd: Optional[Decimal]
    drawdown_usd: Optional[Decimal]

@dataclass(frozen=True)
class FlattenReport:
    cause: str
    generation: int
    flattened: Tuple[str, ...]                 # symbols successfully submitted
    failed: Tuple[Tuple[str, str], ...]        # (symbol, reason) — M0 failed[] verbatim
    residual: Tuple[str, ...]                  # sorted symbols still presumed held

class RiskKillSwitch:
    def __init__(self, *, cfg: RiskConfig, ledger: Optional["RiskLedger"]) -> None: ...
    @property
    def state(self) -> str: ...            # ∈ KILL_STATES; starts "monitoring"
    @property
    def generation(self) -> int: ...       # starts 0; +1 per accepted trip; never reset
    def evaluate(self, account: AccountRead, loss: LossRead) -> KillEvaluation: ...
    def trigger(self, cause: str, broker, portfolio: PortfolioRead, *,
                evaluation: Optional[KillEvaluation] = None,
                account: Optional[AccountRead] = None,
                tradability: Mapping[str, "market_state.Verdict"] = {}) -> FlattenReport: ...
        # keyword-only ANNOTATION inputs (M4C-1/F2/safety-F4): the caller supplies the
        # KillEvaluation it acted on, its annotation account read (typically wrapped from
        # AccountStore.latest_unsafe()), and per-symbol M2 verdicts (FD-M4-20: annotation
        # only, NEVER a skip). Row fields derived from absent inputs are null (§I semantics).
    def retry_residual(self, broker, portfolio: PortfolioRead) -> FlattenReport: ...
    def residual_symbols(self) -> Tuple[str, ...]: ...
    def rehydrate(self, rows) -> None: ...  # HALTED + generation + residual re-latch (FD-M4-19);
        # a journal ending mid-flatten (trailing monitoring→flattening row) rehydrates to
        # HALTED with residual rebuilt from that row (safety-F5 — semantics below)
```

Transitions (every transition journals a `kill_switch_transition` row; out-of-vocab cause ⇒ `RiskError`):

```
MONITORING --trigger(cause ∈ KILL_CAUSES)--> FLATTENING --(finally, ALWAYS)--> HALTED
HALTED --retry_residual (residual ≠ ∅)--> HALTED   (residual shrinks; reduce-only only)
HALTED --(no in-process re-arm; FD-M4-19)--> operator-attended NEW run
```

Frozen semantics:

- **`evaluate`** is pure: `account.status != "fresh"` ⇒ `KillEvaluation(cause=None, skipped=True, ...)` —
  the switch NEVER fires on unverified numbers (FD-M4-3); the caller journals the edge-triggered
  `kill_eval_skipped` row on the fresh→degraded transition. On a fresh read: daily-loss then drawdown,
  strict `>` vs `cfg` caps; first breach wins (`"daily_loss_cap"` before `"drawdown_cap"`).
- **`trigger`** holds one module lock (shared with `retry_residual`). If `state != "monitoring"`: journal
  `kill_retrip` (cause + current generation; transitions nothing backward, resubmits nothing) and return
  the prior report (idempotent; after a rehydrate with no in-process prior report, the report is
  synthesized from rehydrated state — safety-F5). Else: `generation += 1`; journal `monitoring→flattening`
  with the annotation inputs (M4C-1/F2/safety-F4): `daily_loss_usd`/`drawdown_usd` from the `evaluation`
  kwarg and `cap_usd` from `cfg` per the cause — all null when `evaluation` is absent;
  `account_snapshot_id` from the `account` kwarg's read — null when absent; `stale_inputs = (account is
  None or account.status != "fresh")` — staleness never blocks the flatten, it is journaled; on THIS row
  `residual[]` = ALL at-trigger portfolio symbols and `flattened[]`/`failed[]` are empty (nothing attempted
  yet — this is exactly what a crash-mid-flatten rehydrate rebuilds from, safety-F5); then construct a
  FRESH `agent.kill_switch.KillSwitch()` and call `inner.trigger(broker, portfolio.positions)` — the M0
  actuator mints one reduce-only token per held position and `finally`-guarantees its own halt
  (kill_switch.py:18-47); then `state = "halted"` (under this module's own `finally`) and journal
  `flattening→halted` with the final `flattened[]`, `failed[]`, `residual[]` and per-symbol M2 tradability
  annotations from the `tradability` kwarg (FD-M4-20: annotation only, never a skip; empty mapping ⇒ empty
  annotations). `failed`/`residual` non-empty additionally journals `kill_flatten_incomplete`
  (exposure remains broker-held; runbook item — never silently "done").
- **`retry_residual`**: legal only in HALTED with residual ≠ ∅; filters `portfolio.positions` to residual
  symbols, runs a fresh M0 pass (reduce-only by construction), journals `kill_retry_residual` with
  before/after residual; state stays HALTED.
- **Crash-mid-flatten rehydrate (safety-F5/F6-build/M4C-10):** a kill slice whose LATEST row is
  `monitoring→flattening` (no subsequent halted row — the process died mid-flatten; the in-process
  `finally` cannot survive a crash) rehydrates to **HALTED**, generation from that row, `residual` = that
  row's `residual[]` (the at-trigger symbol set, presumed still held — fail-closed); `rehydrate` journals
  a `kill_flatten_incomplete` row for it (`failed=[]`, the rebuilt `residual[]`). `retry_residual` is then
  legal and runs the normal reduce-only pass over the injected portfolio. The M5 runbook obligation
  (operator-attended retry after such a restart) is recorded in §O.
- **Flatten attempts ALL positions** regardless of M2 tradability (FD-M4-20); positions come from the
  injected `portfolio` (the caller fetches `broker.positions()` at trigger time; a stale portfolio is used
  anyway and journaled — freezing exposure on stale data is worse than reducing on it).
- **Interplay with M0 tokens:** no code path from any state to an opening order — `risk_kill.py` cannot
  name `mint_open_token`/`OpenPreflightToken` (AST guard) and the M0 inner class only ever calls
  `mint_reduce_only_token` (kill_switch.py:41). `can_open` rung 2 rejects in `flattening` AND `halted`.

### The S8 drill (frozen test design — `tests/agent/test_risk_kill.py`)

1. **Happy flatten:** SpyBroker + fixture portfolio (long AAPL 10, short MSFT −5; a zero-qty row dropped at
   parse). `evaluate` on a fresh fixture account breaching daily-loss ⇒ `"daily_loss_cap"`; `trigger` ⇒
   every `broker.calls` intent has `is_reducing=True`, sides correct (sell long / buy-to-cover short), qty
   = |held|; state `halted`; journal shows `monitoring→flattening→halted` with cause + metrics.
2. **Failure isolation:** a broker whose `submit_order` raises for MSFT ⇒ state still `halted`;
   `failed=[("MSFT", ...)]`; `residual=("MSFT",)`; `kill_flatten_incomplete` journaled; `retry_residual`
   with a now-working broker shrinks residual to `()` and submits ONLY MSFT, reduce-only.
3. **Total broker failure:** raises on every order ⇒ `halted`, all symbols in `failed` (M0 invariant
   transferred).
4. **Retrip idempotency:** second `trigger` ⇒ `kill_retrip` row, zero new submits, same generation+1 only
   once.
5. **No-open proof (S8 ∩ S1):** after the drill, the preflight registry holds zero `"open"`
   authorizations (`execution_preflight._authorizations` inspected white-box); `mint_open_token` patched
   with a fail-counter ⇒ never called; every SpyBroker call carries `is_reducing=True`.
6. **Skip-not-trip:** `evaluate` with a stale/missing/invalid/skew account ⇒ `skipped=True, cause=None`;
   no transition, no submit; paired assertion that `mint_reduce_only_token` still succeeds for a held
   fixture position under every degraded account state (FD-M4-3).
7. **Rehydrate latch:** replaying the drill's risk.jsonl into a fresh `RiskKillSwitch.rehydrate` ⇒ state
   `halted`, same generation, same residual; `can_open` rung 2 rejects (`kill_switch_halted`).
8. **Tradability annotation:** a NOT_TRADABLE fixture verdict for AAPL, passed via `trigger`'s
   `tradability` kwarg, changes NOTHING about the submit set (FD-M4-20); the transition row carries
   `[["AAPL","not_tradable"], ...]`; with the kwarg omitted the row carries `[]` and the measured-value
   fields are null when `evaluation`/`account` are omitted (M4C-1).
9. **Crash-mid-flatten replay (safety-F5):** a journal truncated immediately after the
   `monitoring→flattening` row ⇒ `rehydrate` lands in `halted` with residual = the at-trigger symbol set
   and journals `kill_flatten_incomplete`; `retry_residual` is legal and submits ONLY reduce-only orders
   for the residual; a retrip after the rehydrate returns a synthesized report, resubmits nothing.

## J. `scripts/agent/risk/can_open.py` — THE chokepoint (LD5)

```python
# scripts/agent/risk/can_open.py
"""RiskEngine.can_open: the single pre-trade chokepoint (parent §5 Tier 5). PURE: no I/O,
no clock read, no fetch, no journal write (FD-M4-5) — every collaborator read arrives as an
input. NEVER raises on a rejectable condition (restriction is DATA — M2 decider posture);
raises RiskError only on invariant breaks. NEVER consulted on the reduce path (FD-M4-3).
M5's rebuilt mint_open_token will REQUIRE a journaled allowed RiskVerdict (announced, not
built): can_open's rung 1 (run gates) is what lets the reject-all obligation collapse into
ONE ladder."""

@dataclass(frozen=True)
class LegRead:
    """Per-leg provenance carried on the verdict + journal row."""
    symbol: str
    instrument_id: int
    side: str
    classification: str            # ∈ LEG_CLASSIFICATIONS (the frozen §A total mapping — LD-R1)
    qty: Decimal
    limit_price: Optional[Decimal]
    cap_notional: Optional[Decimal]   # None iff unpriceable
    mark_used: bool

@dataclass(frozen=True)
class RiskVerdict:
    """Frozen field set. Named RiskVerdict (not Verdict) to avoid colliding with
    market_state.Verdict."""
    allowed: bool                       # INVARIANT: allowed is True <=> reasons == ()
    reasons: Tuple[str, ...]            # sorted, deduped, ⊆ RISK_REASONS
    gate_stage: Optional[str]           # the terminal stage name, or None (phase 2 reached)
    stages_skipped: Tuple[str, ...]     # frozen GATE_STAGES members not evaluated, in order
    strategy_id: str
    legs: Tuple[LegRead, ...]           # () on phase-1 terminal verdicts (candidate stage
                                        #   in stages_skipped — LD-R1)
    gross_notional: Decimal             # Σ cap_notional over priceable OPENING legs
                                        #   (classification ∈ {opening_long, short_or_flip},
                                        #   §A); Decimal("0") if none or terminal (LD-R1)
    caps_used: Tuple[Tuple[str, str, str], ...]   # §D format; () when not reached
    account_snapshot_id: Optional[str]  # None iff account missing/invalid
    kill_state: str                     # consumed value (TOCTOU provenance)
    kill_generation: int
    session_date_et: Optional[str]      # from the leg verdicts; None if stage not reached
    rules_hash: str
    verdict_id: str                     # §K.3 deterministic id

class RiskEngine:
    def __init__(self, *, cfg: RiskConfig, gates_config: dict, run_id: str) -> None: ...
        # gates_config = the assembled committed(+overlay) dict fed to gates.opening_allowed.

    def can_open(
        self,
        candidate: "Candidate",
        portfolio: Optional[PortfolioRead],
        account: AccountRead,
        *,
        market_state: Mapping[str, "market_state.Verdict"],  # leg symbol -> fresh cache read
        marks: Mapping[str, Mark] = {},                      # optional tighteners (FD-M4-16)
        kill_state: str,                                     # RiskKillSwitch.state
        kill_generation: int,
        margin_read: MarginRead,                             # IntradayMarginModel.read(...)
        pdt_read: PdtRead,                                   # LegacyPdtCompatMode.read()
        loss_read: LossRead,                                 # LossLimitsMonitor.read()
        now_ms: int,                                         # used ONLY for mark freshness
        decision_id: Optional[str] = None,                   # rides into verdict_id (S6)
    ) -> RiskVerdict: ...
```

Frozen semantics (beyond §2.2):

- **Purity (LD5):** deterministic — identical inputs ⇒ an identical `RiskVerdict` (byte-identical journal
  row given the same `run_id`). `account.status` is consumed as computed (the caller's `AccountStore.get`
  used the SAME `now_ms` — caller contract); `now_ms` inside `can_open` touches ONLY mark freshness.
- **`RiskError` (FATAL, never a verdict):** non-`Candidate` input; `account is None`; out-of-vocab
  `kill_state`/leg side; a `marks` entry whose key/identity mismatches; a market-state verdict whose
  `symbol`/`instrument_id` mismatches its leg; cross-leg `session_date_et` disagreement; a leg verdict's
  `session_date_et != margin_read.asof_session_date_et` (stage 8 — stale collaborator wiring is a bug,
  not data; safety-F6/RM-12); any reason about to be emitted that is ∉ `RISK_REASONS`.
- **Journaling (FD-M4-5):** the CALLER journals every evaluation — allowed and denied alike — via
  `RiskLedger.record_risk_verdict(verdict, decision_id=...)` immediately after the call; M4's tests and
  drills always pair the two; M5's orchestrator contract will require the row before any token mint.
- **Reduce path independence (FD-M4-3, structural):** nothing on the
  `mint_reduce_only_token`→`submit_order` path references this module (grep/AST-asserted), and §M carries
  a paired test per terminal reason proving the reduce mint still succeeds under that condition.
- **S1 composition:** on the committed config every call returns
  `allowed=False, gate_stage="run_gates", reasons=("run_gates_off",)` with the frozen TERMINAL shape
  `legs=()`, `gross_notional=Decimal("0")`, `caps_used=()`, `session_date_et=None` (LD-R1 — the
  committed-config canary asserts the exact terminal row shape) regardless of all other inputs; on a
  gates-ON **fixture** config with the committed zero caps + empty universe, every open is STILL rejected:
  for the canonical second-wall input (fresh fixture account, flat portfolio, single priceable buy leg,
  TRADABLE fresh verdict, `monitoring` kill, no deficits, pdt `unknown`, baselined non-breaching loss
  read) the frozen EXACT reason tuple is `("beta_unknown", "gross_exposure_cap_exceeded",
  "net_exposure_cap_exceeded", "position_cap_exceeded", "sector_unknown", "universe_excluded")` (RM-5 —
  candidate-symbol poisoning per FD-M4-17; margin/loss reasons join the union only when their fixture
  inputs apply) — two independent walls, both canary-tested.

## K. `scripts/agent/risk/risk_ledger.py` — journal stream, row shapes, deterministic ids

### K.1 Stream + writer

`journal/risk.jsonl`, event types below, written EXCLUSIVELY through `RiskLedger` — a validating facade
over `recorder.persistence.EventWriter` → `agent.journal.JournalWriter` (status_ledger.py:211-219 pattern):
NO new writer/hash/serialization; one ledger per resolved path; injected `run_id`; `rules_hash` on every
row; no payload key in `journal._RESERVED` (journal.py:21) — `decision_id` rides the journal kwarg;
`RISK_LEDGER_VERSION = 1` as the `"v"` FIRST key (`canonical_status_payload` shape, status_ledger.py:114-121);
Decimals stay Decimal (serializer renders strings) — **rehydrate-bearing model-state money fields are
journaled EXACT/unquantized; `USD_QUANTUM` quantize-only applies ONLY to `account_snapshot` money fields**
(FD-M4-25/LD-R5; per-field enumeration in §K.2); no set/frozenset enters a row; all lists sorted;
`ts_market_utc` is the payload-time
field where a row is a dated market fact. Replay = the shared `replay_stream` (truncated-tail /
`JournalCorruption` semantics unchanged, S3).

```python
STREAM_RISK = "risk"
RISK_LEDGER_VERSION = 1
EVT_RISK_VERDICT = "risk_verdict"
EVT_ACCOUNT_SNAPSHOT = "account_snapshot"
EVT_ACCOUNT_ALERT = "account_alert"
EVT_KILL_TRANSITION = "kill_switch_transition"
EVT_KILL_RETRIP = "kill_retrip"
EVT_KILL_RETRY = "kill_retry_residual"
EVT_KILL_FLATTEN_INCOMPLETE = "kill_flatten_incomplete"
EVT_KILL_EVAL_SKIPPED = "kill_eval_skipped"
EVT_IML_OBSERVATION = "iml_observation"
EVT_MARGIN_DEFICIT = "margin_deficit_detected"
EVT_DEFICIT_SATISFIED = "margin_deficit_satisfied"
EVT_DEFICIT_EXPIRED = "margin_deficit_expired"
EVT_DEFICIT_UNBASELINED = "margin_deficit_unbaselined"      # M4C-2/RM-6 (rev 2)
EVT_MARGIN_WINDOW_UNRESOLVED = "margin_window_unresolved"   # safety-F11 (rev 2)
EVT_FREEZE_START = "margin_freeze_start"
EVT_FREEZE_END = "margin_freeze_end"
EVT_PDT_TRANSITION = "pdt_regime_transition"
EVT_HWM_UPDATE = "loss_hwm_update"

class RiskLedger:
    def __init__(self, writer: "EventWriter", *, rules_hash: str) -> None: ...
    # one kwarg-only record_* method per event type (StatusLedger shape); each validates
    # vocabularies (reasons ⊆ RISK_REASONS, states/causes in their frozensets, reserved
    # codes refused in M4), quantizes money, and refuses _RESERVED collisions.

def replay_risk(path) -> list: ...                  # delegates to replay_stream
def rehydrate_risk_state(rows) -> dict: ...         # §K.4 pure fold
```

### K.2 Frozen payload field sets (beyond the common `v`, `rules_hash` prefix)

| event_type | payload fields |
|---|---|
| `risk_verdict` | `verdict_id, allowed, reasons[], gate_stage\|null, stages_skipped[], strategy_id, legs:[{symbol, instrument_id, side, classification, qty, limit_price\|null, cap_notional\|null, mark_used}], gross_notional, caps_used[[name,used,limit],…], account_snapshot_id\|null, kill_state, kill_generation, session_date_et\|null` (+ `decision_id` via the journal kwarg, S6) |
| `account_snapshot` | `account_snapshot_id\|null (null on an AccountInvalid put — no id is derivable, F4), status ∈ {fresh, invalid} (the put-time pair ONLY: staleness/skew are get-time concepts and NEVER appear on this row — they surface on account_alert rows), equity\|null, last_equity\|null, cash\|null, buying_power\|null, maintenance_margin\|null, daytrading_buying_power\|null, pattern_day_trader\|null, daytrade_count\|null, multiplier\|null, source, ts_read_utc\|null, invalid_reason\|null` (frozen CALLER obligation: written by the caller on every `AccountStore.put`, valid and invalid alike — F4) |
| `account_alert` | `transition ∈ {degraded, recovered}, status, age_ms\|null, invalid_reason\|null` (edge-triggered only) |
| `kill_switch_transition` | `from_state, to_state, cause, generation, daily_loss_usd\|null, drawdown_usd\|null, cap_usd\|null, account_snapshot_id\|null, stale_inputs, flattened[], failed[[symbol,reason],…], residual[], tradability_annotations[[symbol,tradability],…]` (nullable fields derive from `trigger`'s keyword-only annotation inputs — null when absent, M4C-1; the `monitoring→flattening` row carries `residual[]` = ALL at-trigger symbols with `flattened[]`/`failed[]` empty — the crash-mid-flatten rehydrate source, safety-F5) |
| `kill_retrip` | `cause, generation, current_state` |
| `kill_retry_residual` | `generation, residual_before[], residual_after[], flattened[], failed[[symbol,reason],…]` |
| `kill_flatten_incomplete` | `generation, failed[[symbol,reason],…], residual[]` |
| `kill_eval_skipped` | `account_status, generation` (edge-triggered) |
| `iml_observation` | `session_date_et, ts_market_utc, equity, maintenance_margin, iml, deficiency, after_iml_reducing, eod, account_snapshot_id` |
| `margin_deficit_detected` | `deficit_id, cause ∈ {opened, increased}, session_date_et, amount, minor, equity_at_detection, satisfaction_deadline_et, expires_after_et` |
| `margin_deficit_satisfied` | `deficit_id, session_date_et, satisfied_on_et, iml_eod_d, iml_eod_e, basis:"eod_iml_delta"` |
| `margin_deficit_expired` | `deficit_id, session_date_et, expires_after_et` |
| `margin_deficit_unbaselined` | `deficit_id, session_date_et, noted_at_close_et` (journaled ONCE per record at the first satisfaction scan that finds `iml_eod_d is None` — M4C-2/RM-6) |
| `margin_window_unresolved` | `deficit_id, session_date_et, error ∈ {unknown_session_date, scan_limit_exceeded}` (journaled once; the record is outstanding-NON-MINOR with window dates null until resolved — safety-F11) |
| `margin_freeze_start` | `trigger_deficit_id, effective_from_et, expires_on_et` (a max-merge journals a SECOND start row: original `trigger_deficit_id`/`effective_from_et`, extended `expires_on_et` — F13) |
| `margin_freeze_end` | `trigger_deficit_id\|null, expires_on_et` (emitted by the FIRST `close_of_day(E)` with `E >= expires_on_et` — observability only, `active_on` stays the authority; F13) |
| `pdt_regime_transition` | `from_state, to_state, evidence ∈ {account_flag, broker_rejection}, rejection_code\|null, pattern_day_trader\|null, daytrade_count\|null` |
| `loss_hwm_update` | `session_date_et, hwm_equity, equity, account_snapshot_id` |

**Money-field discipline per row (LD-R5 — resolves the RM-1/M4C-4/safety-F1 blocker).** Journaled EXACT
(unquantized canonical Decimal-string via the serializer) because rehydrate or replay-determinism reads
them back: `iml_observation.equity/maintenance_margin/iml/deficiency`;
`margin_deficit_detected.amount/equity_at_detection`; `margin_deficit_satisfied.iml_eod_d/iml_eod_e`;
`loss_hwm_update.hwm_equity/equity`; `kill_switch_transition.daily_loss_usd/drawdown_usd/cap_usd`; every
`caps_used` used/limit value; every `cap_notional`/`gross_notional`; every `limit_price` (a price on the
4dp grid, never USD-quantized — M4C-4). Quantized `USD_QUANTUM` quantize-only: ONLY the
`account_snapshot` row's `equity/last_equity/cash/buying_power/maintenance_margin/daytrading_buying_power`
(pure provenance — nothing rehydrates from this row; FD-M4-25). R9's equality statement is therefore
EXACT: rehydrated state == live state, **byte-exact**, because every rehydrate-bearing field round-trips
unquantized.

### K.3 Deterministic ids (S6 — all via `serializer.row_hash` over a canonical dict with the EXACT key set)

- `verdict_id = "rv-" + row_hash({run_id, strategy_id, symbols (sorted leg symbols), gross_notional,
  reasons (sorted), gate_stage|null, account_snapshot_id|null, rules_hash, decision_id|null})` —
  Decimal values as their canonical serializer strings.
- `account_snapshot_id = "as-" + row_hash({equity, last_equity, cash, buying_power, maintenance_margin,
  daytrading_buying_power|null, pattern_day_trader|null, daytrade_count|null, multiplier|null, source})`
  — note: NO `seen_at_ms` and NO `ts_read_utc` (M4C-6/F9: the same broker numbers re-read produce the
  same id — `ts_read_utc` stays a row FIELD/provenance only, it would defeat the id's stability; the
  journal row's own `seq`/`ts_utc` carry recency).
- `deficit_id = "imd-" + row_hash({"session_date_et": D})` — run-independent (FD-M4-13).

Replaying the same fixtures with the same `run_id` reproduces every id and row hash byte-for-byte (§M).

### K.4 `rehydrate_risk_state(rows) -> dict` (pure fold)

Fold in ascending `seq` (the status_ledger.py:356-371 shape), latest-row-wins per key. Frozen output key
set:

```
{"kill":   {state, generation, residual[]},                  # from kill_* rows; a trailing
                                                             #   monitoring→flattening row folds to
                                                             #   HALTED + that row's residual[] (§I,
                                                             #   safety-F5)
 "margin": {"deficits": {deficit_id -> FIELD-merged record}, # per-deficit_id FIELD-merge (F13):
                                                             #   detected rows supply amount/minor/
                                                             #   equity/deadlines; satisfied/expired/
                                                             #   unbaselined/window rows OVERLAY only
                                                             #   their own fields — NEVER whole-row
                                                             #   replace (a satisfied row has no amount)
            "iml_eod":  {session_date_et -> iml},            # from eod iml_observation rows; a deficit
                                                             #   date with NO matching eod row joins as
                                                             #   iml_eod_d=None (M4C-2/RM-6)
            "freeze":   latest freeze fields | null},
 "pdt":    {state, rejection_latched},
 "loss":   {hwm_equity | null}}
```

`IntradayMarginModel.rehydrate`, `RiskKillSwitch.rehydrate`, `LegacyPdtCompatMode` (the `rehydrated_state`
ctor arg — LD-R3), and `LossLimitsMonitor.rehydrate` each consume their slice. Replaying the same rows
yields identical state; rehydrated state == live state **byte-exact** after the identical event sequence
(S3 test, §M — exactness holds because every rehydrate-bearing money field is journaled unquantized,
LD-R5/§K.2).

## L. Fixtures (programmatic builders + committed files)

Builders live in `tests/lib/risk_fixtures.py` (pure, no wall clock, no randomness; Decimal-string money in
Alpaca wire shape so the M5 adapter is a pass-through).

| Fixture | Contents | Used by |
|---|---|---|
| `account_payload(**overrides)` | canonical Alpaca-shaped account dict: `{"equity":"100000.00","last_equity":"100000.00","cash":"40000.00","buying_power":"200000.00","maintenance_margin":"30000.00","multiplier":"2","daytrading_buying_power":"400000.00","pattern_day_trader":false,"daytrade_count":0}`; overrides delete/replace keys (missing-field, float-typed, non-finite, negative, bool-typed variants for the §B injection matrix) | account_state, can_open, loss_limits, kill |
| `FakeAccountProvider(payloads)` | scripted `AccountReadProvider`: successive `account_payload`/`positions_payload` returns (the LD8 seam double) | account_state, integration paths |
| `portfolio_fixture(name)` | `"flat"` (no positions), `"long_short"` (AAPL +10 @ mv "1900.00", MSFT −5 @ mv "-2100.00"), `"long_only"`, `"dup_symbol"` (raises), `"zero_qty_row"` (dropped at parse) | exposure, can_open, S8 drill |
| `margin_day(observations)` | builds `MarginObservation` sequences from `(equity, maintenance, after_iml_reducing, eod)` string tuples for one session date; incl. a crash-before-EOD day (detection rows journaled, no `close_of_day(D)` — the `iml_eod_d=None` path, M4C-2) | intraday_margin §E table rows |
| `tests/fixtures/calendar/nyse_margin_window_v1.json` | NEW COMMITTED fixture, same schema as `nyse_2026_schedule.json` (mic XNYS, pin, sessions map): **2026-06-01 … 2026-07-31 contiguous** (every calendar date present; weekends `is_trading_day:false`; 2026-06-19 Juneteenth and 2026-07-03 Independence-Day-observed as holidays; 2026-07-02 a synthetic early close for variety) — ≥40 sessions so bd5/bd15 + freeze-boundary fixtures never exhaust coverage; loaded via `FixtureScheduleProvider` (market_calendar.py:167) | add_business_days, S10 windows/freeze |
| `deficit_boundary_cases()` | the FD-M4-14 triples: equity "18000" with amounts "900"/"900.01"; equity "100000" with "1000"/"1000.01"; an invalid-equity observation (⇒ not minor) | minor-exception tests |
| `freeze_timeline()` | deficit on D=2026-06-08; satisfaction variants (bd3 / bd5-EOD-delta-qualifies / never); a skipped-bd5-close variant (close_of_day first run on bd6 — catch-up freeze, LD-R6); a minor-deficit-at-bd5 variant (freezes, LD-R2); asserts effective_from/expires_on exact dates over the new calendar | freeze trigger/boundary |
| `pdt_payloads()` | `pdt_flagged` (pattern_day_trader true), `pdt_clean` (false), `pdt_fields_absent` (keys deleted); rejection dicts `{"code":40310100,"message":"trade denied due to pattern day trading protection"}` + a non-PDT rejection | pdt_compat detection |
| `marks_fixture()` | fresh / stale (seen_at_ms older than `MARK_FRESHNESS_TTL_MS`+1) / identity-mismatched `Mark`s on the `MID_QUANTUM` grid; zero- and negative-`limit_price` legs (unpriceable — RM-3) | FD-M4-16 notional tests |
| `verdict_fixture(symbol, tradability, *, stale_default=False)` | constructed M2 `market_state.Verdict`s incl. the literal `MarketStateCache.safe_default_verdict` output | can_open market_state stage |
| `tests/fixtures/config/risk_armed_overlay.json` | hostile overlay: raised caps, `short_selling:true`, altered universe, injected keys — must merge back to committed values | config canary |
| `gates_on_fixture_config()` | a NON-COMMITTED config dict with gates identity-True + committed-shaped zero caps/empty universe (the second-wall canary input) | can_open canary |
| `permissive_fixture_config()` | a NON-COMMITTED config dict: gates identity-True, named NONZERO integer caps, a small universe WITH sector/beta metadata — the passing-path/strict-`>`-boundary input (`allowed=True` is reachable only here; M3-build) | exposure/loss/can_open pass-side tests (§M 3, 6, 8) |

## M. Test list — each test file → cases → safety invariant

`tests/agent/` (offline, stdlib-only; FakeClock / SpyBroker / FixtureScheduleProvider; extend
`test_no_network_no_creds.py` and `test_config_canary.py` rather than duplicating them):

1. **`test_risk_config.py`** — parser: committed JSON parses; caps must be ints ≥ 0 (bool/float/string ⇒
   `ValueError`); unknown/missing keys in `caps`/`risk` raise; universe entry validation (missing sector,
   non-Decimal beta, extra keys raise); `SHORTS_SUPPORTED is False` and
   `INTRADAY_MARGIN_BUFFER_USD == Decimal("0")` pinned; merge posture: overlay can lower a cap, never
   raise; `short_selling` cannot be enabled; universe metadata immutable under overlay; changing any risk
   leaf changes `rules_hash`. [S1, R5]
2. **`test_account_state.py`** — parse matrix per §B (each `AccountInvalid` reason constructible; the M0
   spy payload ⇒ `AccountInvalid`); BrokerUSD typing; staleness boundaries: fresh at exactly
   `ACCOUNT_FRESHNESS_TTL_MS`, stale at +1 (strict `>`); `now_ms < seen_at_ms` ⇒ `"skew"` (R12); ctor
   clamp raises on a longer TTL; `latest_unsafe` returns under every degraded status;
   `account_snapshot_id` deterministic and `seen_at_ms`/`ts_read_utc`-independent (same broker numbers
   re-read ⇒ same id — M4C-6); positions parser: duplicate symbol raises, zero-qty silently dropped
   (LD-R4), missing market_value raises; out-of-vocab source raises; `portfolio_is_stale`: strict-`>`
   boundary + shorten-only clamp raises on a longer TTL (FD-M4-22). [S2, R1, R12]
3. **`test_exposure.py`** — `leg_cap_notional`: max(limit, mid) with fresh mark, limit-only with stale or
   absent mark, `(None, False)` without limit AND with `limit_price` 0 / negative (unpriceable — RM-3),
   mark fresh at exactly `MARK_FRESHNESS_TTL_MS`, stale at +1, `ttl_ms` clamp raises on a longer TTL
   (FD-M4-22), identity-mismatched mark ⇒ `RiskError`; gross/net/symbol sums exact vs hand-computed
   Decimals; sector/beta aggregates — a held SHORT adds `|market_value|` to its sector (RM-4); poisoned
   aggregate: ONE held symbol missing from the universe ⇒ `UnknownMeta`, never a partial sum (R3-meta);
   a CANDIDATE symbol missing from the universe poisons sector+beta exactly like held (RM-5);
   `project_caps` strict-`>` boundaries (via `permissive_fixture_config()`, §L): projected == cap passes,
   cap + 0.01 rejects; cap 0 rejects any positive exposure; additive-conservative signs; `caps_used` row
   format frozen (full union sorted by name once, exact Decimal strings — M1-build). [S2, R3]
4. **`test_intraday_margin.py`** — one test family per §E S10 table row (classification total-function
   matrix incl. flip; deficit max-merge 100→250→180 ⇒ 250; equality boundary ⇒ no deficit; bd15 window
   over the new calendar with the holiday; freeze trigger paths + day-90/91 boundary + second-trigger
   max-merge (original trigger_deficit_id/effective_from kept, second freeze_start row — F13) +
   skipped-bd5-close catch-up (close_of_day first run on bd6 ⇒ STILL frozen, effective_from anchored to
   the deadline — LD-R6/safety-F2) + the frozen intra-close order (satisfaction → expiry → freeze, RM-9)
   + minor-deficit-at-bd5 freezes (LD-R2); `margin_freeze_end` emitted by the first close past expiry
   (F13); minor boundary triples + minor-latch one-way (non-minor at low equity stays non-minor after a
   high-equity increase — safety-F3) + increase-flips-minor); satisfaction: V6 delta
   ≥ amount at bd2 ⇒ satisfied with journaled basis, same-day E==D never evaluated; crash-before-EOD:
   `iml_eod_d is None` ⇒ unsatisfiable (skipped, never raises), `margin_deficit_unbaselined` journaled
   exactly once, record still expires at bd15 and still freezes at bd5 (M4C-2/RM-6); window-computation
   failure at detection: `margin_deficit_detected` journaled FIRST, then `margin_window_unresolved`,
   record outstanding-non-minor (safety-F11); **R3 (broker ground
   truth):** mutating ONLY `equity`/`maintenance_margin` in fixtures flips detection — and a source-scan
   asserts no margin-percentage / `25000` literal under `risk/`; `add_business_days`: holiday/weekend
   skips, coverage exhaustion raises, scan-limit raises; rehydrate: a replayed risk.jsonl reproduces
   outstanding/freeze state BYTE-identical to the live model, incl. the no-eod-row ⇒ `iml_eod_d=None`
   join (R9, S3, LD-R5). [S10, S3, R3, R9]
5. **`test_pdt_compat.py`** — detection matrix (flagged/clean/absent payloads ⇒
   enforcing/not_enforcing/unknown); rejection latch via code AND via each marker substring
   (case-insensitive), non-PDT rejection does not latch; latch survives a later `False` flag read within
   the run AND across runs: ctor `rehydrated_state` with `rejection_latched=True` re-latches enforcing
   (LD-R3); transitions journaled; gate behavior: latched ⇒ `pdt_compat_blocked`; enforcing + dtbp present
   + Σ > dtbp ⇒ `pdt_compat_dtbp_exceeded` (boundary: Σ == dtbp passes); unknown blocks nothing;
   tighten-only: no code path in `pdt_compat.py` can suppress another reason (white-box: `read()` output
   feeds only reason-ADDING branches); source-scan: no `25000`, no day-trade arithmetic. [S10-compat, R13]
6. **`test_loss_limits.py`** — daily-loss and drawdown strict-`>` boundaries (loss == cap passes, +0.01
   trips); cap 0 = zero budget (any positive loss trips; never "disabled"); HWM monotonic + sparse
   `loss_hwm_update` rows + rehydrate; stale/missing/invalid/skew reads update nothing and never produce a
   `TripSignal` (R14); `TripSignal` field exactness; `LossRead.hwm_equity is None` before first fresh
   observation. [R14, FD-M4-18]
7. **`test_risk_kill.py`** — the nine-case S8 drill of §I verbatim (incl. case 9 crash-mid-flatten
   replay — safety-F5), plus: out-of-vocab cause ⇒
   `RiskError`; reserved cause `live_gate_flip` refused by the ledger; transition rows byte-deterministic
   under a fixed clock/run_id; annotation kwargs omitted ⇒ null fields, never a raise (M4C-1). [S8, S1, S6]
8. **`test_can_open.py`** — **ladder order:** one fixture per stage tripping exactly that stage; a
   multi-fault input trips the EARLIEST terminal stage only (`gate_stage` + `stages_skipped` asserted);
   phase-2 multi-fault collects ALL reasons sorted (e.g. out-of-universe + REDUCE_ONLY verdict + zero caps
   ⇒ union); **committed-config canary (S1):** real committed JSON ⇒ every candidate rejects
   `("run_gates_off",)` at `gate_stage="run_gates"` with the EXACT frozen terminal row shape (`legs=()`,
   `gross_notional=Decimal("0")`, `caps_used=()`, `session_date_et=None` — LD-R1) for a sweep of
   candidates/portfolios/accounts;
   **second wall:** `gates_on_fixture_config()` + the canonical second-wall input ⇒ rejected with EXACTLY
   the frozen §J tuple `("beta_unknown", "gross_exposure_cap_exceeded", "net_exposure_cap_exceeded",
   "position_cap_exceeded", "sector_unknown", "universe_excluded")` (RM-5);
   **pass path:** an `allowed=True` verdict is reachable under `permissive_fixture_config()` (§L) and
   `allowed ⟺ reasons==()` holds (M3-build); **staleness (R1):** each degraded account status ⇒ its exact
   terminal reason + downstream
   stages skipped + paired reduce-mint success; portfolio missing/stale/unreconciled terminal behavior;
   **classification (LD-R1):** every leg carries the §A mapping — buy/held≥0 ⇒ `opening_long`; cover-buy
   ⇒ `reducing`; flip-buy ⇒ `short_or_flip`; sell-close ⇒ `reducing`; sell otherwise ⇒ `short_or_flip` —
   alongside the stage-6 reasons: buy-cover/flip-buy and sell-close legs ⇒ `reduce_path_not_can_open`;
   sell vs zero/short held and sell qty > held ⇒ `short_side_disabled`; `limit_price <= 0` ⇒
   `unpriceable_candidate` (RM-3); `paper_eligible=True`-but-not-identity (e.g. 1) ⇒
   `strategy_not_paper_eligible`; **market_state:** missing verdict / REDUCE_ONLY / NOT_TRADABLE /
   safe-default (⇒ exactly `market_state_not_tradable` + `market_state_stale_default` from that stage);
   identity-mismatch verdict ⇒ `RiskError`; leg `session_date_et != margin_read.asof_session_date_et` ⇒
   `RiskError`, never an allow (safety-F6); **margin/pdt/loss stages:** outstanding non-minor deficit,
   buying-power boundary (Σ == BP passes, +0.01 rejects with buffer 0), dtbp, daily-loss/drawdown/
   baseline-unavailable; **vocab guard (R7):** monkeypatched stage emitting an out-of-vocab reason ⇒
   `RiskError`; **determinism (R4):** same inputs ⇒ identical verdict + identical `verdict_id`;
   `allowed ⟺ reasons==()` property over the whole sweep; reserved reasons never observed (R6-vocab).
   [S1, R1, R2, R4, R7, R8, R10]
9. **`test_risk_ledger.py`** — every `record_*` round-trips through `replay_risk` (hash-verified);
   reserved-key collision refused; out-of-vocab event payloads (bad reason/state/cause/reserved code)
   raise; Decimal-strict: float/NaN/Inf anywhere ⇒ raise (S2); money discipline per §K.2 (LD-R5): a
   sub-cent broker value journals at 2dp ONLY on `account_snapshot` rows, while every rehydrate-bearing
   money field (deficit `amount`, `iml_eod_*`, `hwm_equity`, kill measured values, `caps_used`,
   `gross_notional`, `limit_price`) round-trips EXACT/unquantized; truncated-tail tolerated, complete
   corrupt line ⇒ `JournalCorruption` (S3); `decision_id` correlation on `risk_verdict` rows (S6);
   deterministic ids reproduce byte-for-byte on replay with the same `run_id`; `rehydrate_risk_state`
   fold: frozen key set, latest-row-wins per key with the per-deficit_id FIELD-merge (F13), equals live
   state byte-exact (R4, R9). [S2, S3, S6, R4, R9]
10. **`test_config_canary.py` (extended)** — committed `risk_rules.json`: all 7 caps integer 0;
    `risk.short_selling.enabled is False`; `risk.universe == {}`; gates still identity-False (extends,
    never replaces, the M0 assertions); `risk_armed_overlay.json` merges back to committed values
    (mirrors `test_armed_overlay_cannot_loosen_committed_via_tighten_only`). [S1, R5]
11. **`test_no_network_no_creds.py` (extended)** — import every `risk/` module under
    `mock.patch("socket.socket", ...)`; assert no `alpaca`/`databento`/`exchange_calendars` in
    `sys.modules` after import; **FD-M4-24 AST guard:** walk all `risk/` sources — forbidden imports
    (any scope) + forbidden token references + `importlib`/`__import__`, with the single `risk_kill.py`
    exemption for `agent.kill_switch` only; subprocess-isolated fresh-import check (fixed argv array)
    asserting `agent.broker*`/`agent.execution_preflight`/`agent.arming` do not land in `sys.modules`
    when importing every `risk/` module EXCEPT `risk_kill` (which legitimately pulls the actuator chain).
    [S1, R11]

**Invariant map** (every row above tags its invariants):

| ID | Invariant | Primary tests |
|----|-----------|---------------|
| S1 | Committed config ⇒ every `can_open` rejects (`run_gates_off` first); zero submits at the Broker boundary; no `risk/` path to a mint/submit except the sanctioned kill delegation | 8, 10, 11, 7 |
| S2 | Float/NaN/Inf never reaches a snapshot or row; Decimal-as-string everywhere | 2, 3, 9 |
| S3 | risk.jsonl replay/rehydrate idempotent; truncated tail dropped; corrupt complete line fatal | 4, 9 |
| S6 | `run_id`/`decision_id` correlation; deterministic `verdict_id`/`deficit_id`/`account_snapshot_id` | 7, 8, 9 |
| S8 | Trip ⇒ flatten-then-halt; reduce-only only; ALWAYS halts; residual retried; never an open | 7 |
| S10 | §E table item-by-item: IML-reducing, deficit calc, bd15 window, bd5/90cd freeze, minor exceptions; PDT compat-only | 4, 5 |
| R1 | Fail-closed staleness: degraded account/portfolio ⇒ exact reject reason + skipped stages; reduce path untouched; kill never trips on unknown | 2, 6, 8 |
| R2 | Zero-caps second wall: gates-on fixture config still opens nothing | 8 |
| R3 | Broker ground truth: outcomes flip ONLY with broker numbers; no re-derivation constants exist | 3, 4, 5 |
| R4 | Verdict/row determinism: same inputs ⇒ identical verdict + byte-identical row | 8, 9 |
| R5 | Tighten-only config: overlay lowers, never raises; shorts/universe immutable | 1, 10 |
| R7 | Out-of-vocab reason/state/cause ⇒ `RiskError`, never coerced | 8, 7, 9 |
| R8 | Ladder order frozen; collect-all within phase 2; `allowed ⟺ reasons==()` | 8 |
| R9 | Rehydrated state == live state after identical events | 4, 9, 7 |
| R10 | M2 degrade-to-safe transfers: safe-default verdict ⇒ stale_default + not_tradable | 8 |
| R11 | No network / no credentials / FD-M4-24 import guard | 11 |
| R12 | Clock regression (skew) ⇒ reject-opens as DATA | 2, 8 |
| R13 | PDT mirror-only: latch, dtbp boundary, unknown-no-block, tighten-only | 5 |
| R14 | Loss strict-`>`; cap 0 = zero budget; no trip on degraded reads | 6 |

## N. Conventions-to-mirror table

| Convention | Source | M4 usage |
|---|---|---|
| Validating-ledger over `EventWriter` | `status_ledger.py:211-219` | `RiskLedger` |
| `"v"`-first canonical payload | `status_ledger.py:114-121` | every risk row |
| Reserved journal keys — `decision_id` as kwarg | `journal.py:21,110-125` | `record_risk_verdict` |
| Strict-`>` staleness on injected monotonic ms | `market_state_cache.py:97` | account/portfolio/mark TTLs |
| Code-constant TTL + shorten-only ctor clamp | `market_state_cache.py:34,52-62` | `AccountStore`, FD-M4-22 |
| Fail-closed safe default with explicit marker reason | `market_state_cache.py:101-125` | `market_state_stale_default` mapping |
| Closed vocabularies as frozensets; out-of-vocab raises | `market_state.py:27-99` | `reasons.py`, `RiskError` |
| Sorted machine-readable `reasons` tuple | `market_state.py:375` | `RiskVerdict.reasons` |
| Pure decider — restriction is DATA, journal lives outside | `market_state.py:212-218` | `can_open` (FD-M4-5) |
| Tighten-only merge / severity accumulation | `config.py:24-43`, `market_state.py:204-209` | caps posture, phase-2 collect-all |
| One-parser config, fail-loud at startup | `signal_config.py:76-115` | `RiskConfig.from_config` |
| Reduce-only validated against the HELD position | `execution_preflight.py:97-118` | `classify_iml_reducing`, leg classification |
| Flatten-then-halt with `finally` + per-position isolation | `kill_switch.py:18-47` | delegated actuator (FD-M4-4) |
| Quantize at the persistence boundary | `status_ledger.py:93-105` (round-trip), M3 §L (quantize-only) | `USD_QUANTUM` quantize-only, `account_snapshot` rows ONLY; rehydrate-bearing fields exact (FD-M4-25/LD-R5) |
| `row_hash` deterministic ids | `serializer.py:53-55` | `verdict_id`/`deficit_id`/`account_snapshot_id` |
| Pure rehydrate fold by ascending `seq` | `status_ledger.py:356-371` | `rehydrate_risk_state` |
| AST import guard + subprocess isolation + socket block | `test_no_network_no_creds.py:7-166`, M3 FD-12 | FD-M4-24 |
| Committed-config canary at the broker boundary | `test_config_canary.py:23-64` | S1 extension |
| Injected `clock.now_ms()` / `FakeClock` | `tests/lib/fakes.py:83-94` | every staleness check |

## O. Deferred items (deliberately out of M4 — owners assigned in FD-M4-26)

- **M5:** fees (SEC/TAF/FINRA, borrow) in `FeeModel`; `mint_open_token` rebuild consuming
  `RiskVerdict.allowed` + `kill_generation` TOCTOU re-check; the real flatten driver (worst-price-capped
  marketable-limit flatten orders, post-submit cancel-on-state-change, scheduled residual retry); account
  refresh cadence + who calls `observe`/`close_of_day`/`evaluate`/`set` hooks; Alpaca adapter behind
  `AccountReadProvider` (verify the §B field names against the live schema; `BrokerRejectionObservation`
  wiring); prolonged-staleness auto-trip policy; PDT rejection-marker set finalization against real
  rejections. **Frozen M5 caller contract (mirrors FD-M4-5's journal-every-verdict obligation):** the
  orchestrator MUST fold `journal/risk.jsonl` via `rehydrate_risk_state` and seed ALL FOUR stateful
  components — `IntradayMarginModel.rehydrate`, `RiskKillSwitch.rehydrate`,
  `LegacyPdtCompatMode(rehydrated_state=...)` (LD-R3 — the pdt rejection latch is durable across runs),
  `LossLimitsMonitor.rehydrate` — BEFORE the first `AccountStore.put`/`can_open` of a run (safety-F14: a
  fresh-instance-without-rehydrate run would silently start unfrozen/unhalted/unlatched); MUST produce a
  `MarginObservation` after every IML-reducing fill, or document the accepted under-detection (RM-8 —
  EOD-only equals the rule's deficit only when EOD is the day's worst point); MUST precompute
  `PortfolioRead.stale` via `portfolio_is_stale` (FD-M4-22); and the runbook MUST cover the
  operator-attended `retry_residual` pass after a crash-mid-flatten rehydrate (safety-F5/M4C-10).
- **M6:** SOD/EOD reconcile diff job — M4 only consumes `PortfolioRead.unreconciled_drift` (default
  False); drift detection/adjustment rows.
- **M7:** ratio/leverage caps + sizing (Kelly/vol-target/VaR-CVaR); `OPEN_CLOSE_BUFFER_S` policy decision
  (stays 0/OFF); backtest gate (S9) — `strategy_not_paper_eligible` is the structural placeholder it
  extends.
- **M8:** live arming, `live_gate_flip` kill cause, the live kill drill, the operator reset-authority
  protocol (FD-M4-19's in-process re-arm, if ever).
- **Short-side milestone:** real `LocateCheck` provider + borrow cost; `locate_unavailable`/
  `ssr_short_blocked` activation at ladder stage 9; lifting `SHORTS_SUPPORTED` + `short_selling.enabled`
  (committed change) — touches exactly `locate.py`, the constant, and stage 6/9.
- **Unmodeled by choice:** V9 "extraordinary circumstances" (we are stricter without it); V8 pattern
  fidelity (`practice_count` is observability only); cash-secured-buy carve-out during a freeze;
  risk.jsonl rotation (M1 MINOR 9 mirror).

## P. References

- Parent design: `docs/superpowers/specs/2026-06-08-stocks-agent-design.md` §5 Tier 5 (lines 200-225), §9
  S8/S10 (385-394), §10 M4 (404), §14 (459-469).
- Format precedent: `docs/superpowers/specs/2026-06-09-M3-signal-calibration-contract.md` (structure,
  conventions, test-map granularity); `2026-06-09-M2-market-state-contract.md` (decider/cache/ledger
  patterns referenced via code).
- Input designs (synthesized here): the three independent M4 architect proposals
  (regulatory-correctness / fail-closed-safety / integration-seams lenses, 2026-06-09).
- Regulatory: FINRA Regulatory Notice 26-10 — https://www.finra.org/rules-guidance/notices/26-10
  (verified 2026-06-09; §0.2 V1–V10).
- Repo facts: §0.1 table (file:line verified at `f9ec7c6`).

## Q. Revision log (rev 2, 2026-06-09 — 4-lens critic pass, 50 findings applied)

4 lenses (repo-facts M4C-1…10 / buildability F1-7+M1-7 / safety-invariants F1-14 / regulatory-math
RM-1…12): 50 findings — 2 blockers, 24 majors, 24 minors — deduplicated to 29 unique defects, ALL
applied. Judgment calls locked by Robin as LD-R1…LD-R6. Vocabulary deltas: `RISK_REASONS` unchanged at
**34**; journal event types **16 → 18** (`margin_deficit_unbaselined`, `margin_window_unresolved`).

Blocker fixed: **RM-1/M4C-4/safety-F1 → LD-R5** (exact-vs-quantized journaling contradiction killed R9:
every REHYDRATE-BEARING money field — deficit `amount`/`equity_at_detection`, `iml`/`iml_eod_*`,
`hwm_equity`, kill measured values, `caps_used`, `cap_notional`/`gross_notional`, `limit_price` — now
journals EXACT/unquantized; `USD_QUANTUM` quantize-only scoped to the `account_snapshot` provenance row
ONLY; per-field enumeration in §K.2; R9 restated as byte-exact; FD-M4-25/§K.1/§K.4/§M 4+9 aligned).

Majors fixed: **M4C-3/F1/safety-F8/RM-11 → LD-R1** (frozen total leg-classification table in §A — flip
buy = `short_or_flip`; opening legs = {opening_long, short_or_flip}; frozen terminal-verdict shape
`legs=()`/`gross_notional=Decimal("0")`/`session_date_et=None`/`caps_used=()`, canary-asserted);
**M4C-1/F2-build/safety-F4** (`RiskKillSwitch.trigger` gains keyword-only annotation inputs
`evaluation`/`account`/`tradability`; row fields null when absent; `latest_unsafe` consumer re-pinned to
the caller's annotation path); **safety-F2/F7-build + RM-9 → LD-R6** (freeze trigger is a catch-up
predicate `satisfaction_deadline_et <= E` — a missed bd5 close can never skip the 90-day freeze;
`effective_from_et` anchored to the deadline date; frozen intra-close order satisfaction → bd15 expiry →
freeze trigger); **RM-2 → LD-R2** (ANY deficit, minor included, unsatisfied at bd5 close freezes —
strictly tighter; rung 11 stays non-minor-scoped; §0.2 superset claim re-scoped to the single FD-M4-12
exception; V8 quoted in full per RM-7); **safety-F3** (minor is LATCHED one-way — once non-minor, never
back); **M4C-5/F5/RM-5** (candidate-leg symbol ∉ universe poisons sector+beta exactly like held; exact
frozen second-wall reason tuple enumerated in §J/§M 8); **RM-3/F12-safety** (`limit_price <= 0` ⇒
`unpriceable_candidate`, excluded from all notionals — no negative-notional offset); **RM-4** (held
sector exposure = Σ `|market_value|` per sector); **M4C-2/RM-6/F10** (`iml_eod_d is None` ⇒ record
unsatisfiable — skipped, never raises, stays outstanding, still expires/freezes; one
`margin_deficit_unbaselined` marker row; rehydrate join defined); **safety-F5/F6-build/M4C-10** (journal
ending mid-flatten rehydrates to HALTED with residual rebuilt from the `monitoring→flattening` row —
which now carries the at-trigger symbol set; `retry_residual` legal; drill case 9 added);
**F3-build/M4C-8 → LD-R3** (pdt rejection latch DURABLE across runs via the `rehydrated_state` ctor arg;
M5 must seed it); **safety-F6/M4-build/RM-12** (rung 3 pinned to
`freeze.active_on(margin_read.asof_session_date_et)`; stage-8 date mismatch ⇒ `RiskError`);
**safety-F7** (FD-M4-24 rewritten as the ONE normative import-guard statement — sole exemption is
`risk_kill.py`'s `import agent.kill_switch`, nothing else relaxed); **F4-build** (`account_snapshot` row:
`account_snapshot_id|null`, put-time `status ∈ {fresh, invalid}`, staleness/skew never on this row).

Minors fixed: **safety-F11** (detection row journaled BEFORE window computation; failure ⇒
`margin_window_unresolved` + outstanding-non-minor until resolved); **F13-safety** (`margin_freeze_end`
emitted by the first close past expiry, observability only; max-merge keeps original
`trigger_deficit_id`/`effective_from_et` and journals a second `freeze_start`; deficits fold is a
per-deficit_id FIELD-merge); **M4C-6/F9-safety** (`ts_read_utc` dropped from the `account_snapshot_id`
hash — the stability rationale now holds); **M4C-7/M5-build → LD-R4** (qty==0 position rows SKIPPED at
parse — editorial "?" removed); **M4C-9** (§0.1 SignalConfig row corrected — it hashes the dict it is
passed; §C notes M4-vs-M3 `rules_hash` divergence, joins key on `run_id`/`decision_id`); **RM-8**
(conditional V4 wording in FD-M4-13/§E; M5 observation-cadence obligation in §O); **RM-10**
(`practice_count` = non-minor deficits unsatisfied at their bd5 close, computed on demand,
rehydrate-free); **safety-F14** (§O M5 frozen caller contract: rehydrate-seed all four stateful
components before the first put/can_open); **M1-build** (`caps_used` = full union sorted by name once,
exact Decimal strings, ledger-exempt); **M2-build** (§3 dependency lists fixed — `reasons` added to
exposure/risk_ledger/account_state); **M3-build** (`permissive_fixture_config()` added to §L; §M 3/6/8
pass-path tests wired to it); **M6-build** (shorten-only clamps for ALL three TTLs: `AccountStore` ctor /
`portfolio_is_stale` helper / `leg_cap_notional` `ttl_ms` kwarg; exact-boundary mark test added).

## R. Harden log (round 1, 2026-06-09/10 — 4-lens adversarial code review, repro-gated)

13-agent review (4 reviewers → independent skeptical verification per finding): 9 raw findings, 8
confirmed, 1 refuted. All fixed TDD; suite 888 → 896 tests.

1. **M4-R1 (major)** — a deficit whose detection-time window computation failed never recomputed its
   bd5/bd15 windows unless a strictly LARGER deficiency arrived, so the contract-mandated 90-day freeze
   could be skipped forever. Fix: window resolution is SELF-HEALING (retried on every observation of the
   date and at every `close_of_day` step 0) and the two windows resolve INDEPENDENTLY (a resolvable bd5
   deadline is never discarded because bd15 runs past calendar coverage); newly-resolved windows journal a
   `window_resolved` detected-row re-emission (`_DEFICIT_CAUSES` += `"window_resolved"`) so the §K.4
   field-merge/rehydrate picks them up.
2. **M4-R1-F1 (major)** — a second trigger whose `effective_from` landed exactly ON the active freeze's
   expiry REPLACED it (loosening an in-force freeze; `active_on` flipped True→False inside the window).
   Fix: MERGE-WHEN-IN-FORCE — any trigger at a close E where the existing freeze has not expired
   (`E < expires_on_et`) max-merges (original `trigger_deficit_id`/`effective_from_et` kept, end
   extended); only a freeze already expired at E is replaced.
3. **M4-EDGE-1 (minor)** — `freeze_triggered` latched BEFORE the fallible `add_business_days` trigger-time
   call; a raise left the flag latched and the freeze permanently skipped in-process. Fix:
   COMPUTE-THEN-LATCH (flags set only after the freeze dict is installed and the start row journaled).
4. **M4-R2 (minor)** — `practice_count()` read a transient in-memory flag that rehydrate reset. Fix:
   derived on demand from the records + close history (unsatisfied at the EARLIEST close ≥ deadline) —
   rehydrate-free per RM-10's actual definition.
5. **M4-R3 (minor)** — an UNPRICEABLE out-of-universe candidate leg did not poison the sector/beta
   projections (partial sums journaled). Fix: poisoning scans ALL opening legs via a new
   `project_caps(..., opening_indexes=...)` parameter fed from the LD-R1 classifications.
6. **M4-R1-F2 (minor)** — rehydrate re-emitted `kill_flatten_incomplete` after a fully-successful
   `retry_residual` (spurious empty-residual marker). Fix: the marker is guarded on the REBUILT residual
   (which already folds `kill_retry_residual` rows) being non-empty.
7. **M4-DET-1 (minor)** — `risk_ledger._quantized_usd` quantized under the ambient decimal context (a
   hostile context made a VALID snapshot fail to journal). Fix: pinned `localcontext(Context(prec=28,
   ROUND_HALF_EVEN))` (the M3 convention).
8. **M4-DET-2 (minor)** — `Mark.__post_init__`'s MID_QUANTUM grid check ran under the ambient context.
   Fix: same pinned-context wrap.

Refuted: M4-R4 (truthy non-bool `PortfolioRead.stale` "fails open" — the verifier found the construction
requires a caller violating the typed `portfolio_is_stale` contract; tracked as an M5-caller obligation,
not an M4 defect).

## R.2 Harden log (round 2, 2026-07-02 — full-project review follow-up)

1. **M4-R2-BLIND (pre-live MINOR from the 2026-07-02 full-project review, fixed TDD)** — the
   loss/drawdown auto-kill was SKIPPED whenever the broker account read was not "fresh"
   (`KillEvaluation.skipped`, FD-M4-3), with only one edge-triggered `kill_eval_skipped` row as
   visibility. A persistently degraded account API therefore left HELD exposure unmanaged forever
   (opens were already blocked by the `can_open` account rung; the gap was holdings-with-no-kill).
   Fix: blindness is now BOUNDED — `MAX_ACCOUNT_BLIND_MS = 120_000` (CODE CONSTANT, FD-M4-22
   posture) of CONTINUOUS non-fresh account reads (`missing`/`stale`/`invalid`/`skew`) while the
   local book-of-record holds an open position escalates to a new kill cause `account_blind_cap`
   (KILL_CAUSES += 1): the standard §M.6 flatten-then-halt sequence with `evaluation=None`, so the
   trigger consumes NO account numbers (FD-M4-3 preserved: numeric caps still never fire on
   unverified reads) and journals `stale_inputs=true`. The blind clock resets on any fresh read;
   `missing` keeps its row-silent semantics (observe compositions journal nothing) but counts
   toward the clock; a flat book never trips (broker-less compositions unaffected; S1 canaries
   unchanged). Tests: `test_account_blind_beyond_cap_with_position_flattens_and_halts`,
   `test_account_blind_below_cap_only_journals_skip`,
   `test_account_blind_clock_resets_on_fresh_read`,
   `test_account_blind_beyond_cap_flat_book_never_triggers` (tests/agent/test_orchestrator.py)
   plus the KILL_CAUSES vocabulary pin update (tests/agent/test_risk_config.py). Suite 1777 → 1781.
