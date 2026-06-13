# M7c Phase-1 Long-Only Relative-Strength Proxy — Contract (RED-ready)

- **Date:** 2026-06-13
- **Status:** Contract + RED tests for the phase-1 gating probe. No implementation written yet (tests are RED).
- **Parent packet:** `docs/superpowers/specs/2026-06-13-M7c-relative-strength-research-packet.md` (rev 2, proxy-first, APPROVED).
- **Strategy id:** `relative_strength.long_only_proxy_v1`
- **Hypothesis id (family umbrella):** `m7c_relative_strength_market_neutral_v0_20260613`

## 0. Scope and boundaries

Phase 1 is the long-only cross-sectional gating probe. It answers one question before any short-side
investment: *does cross-sectional residual signal exist on this universe/substrate at all?*

**In scope**

- New pure module `scripts/agent/strategies/relative_strength.py`: cross-sectional ranking over the valid
  decision set at one timestamp, plus the long-only proxy that selects the top 2 and emits BUY candidates.
- One new benchmark identifier `universe_equal_weight_long_v1` in `scripts/agent/backtest_metrics.py` (additional
  metric/provenance only; the M7 verifier's pinned `exposure_matched_midbar_v1` is unchanged).
- RED tests in `tests/agent/test_relative_strength_proxy_m7c.py`.

**Out of scope (deferred to phase 2 / later loops)**

- Short side, multi-leg `Candidate`, locate/borrow/SSR/short-fee, basket atomicity, the `universe_equal_weight_spread_v1` benchmark.
- The credentialed historical clean-window run + Phase Gate evaluation (separate, needs Databento creds).
- Any production artifact write, threshold change, or run-gate flip. `artifacts/backtests/` stays `.gitkeep`.

## 1. Repo facts this builds on

| Fact | Source |
|---|---|
| `Strategy.scan(ctx)` and `run_historical_backtest(*, symbol, ...)` are **single-symbol** — the historical loop holds one symbol's `SignalSnapshot` per decision instant and cannot compute a cross-sectional rank. | `scripts/agent/strategy.py`, `scripts/agent/backtest_historical.py:1324` |
| The single-leg fill/exit machinery `_simulate_historical_long_trade(...)` is reusable unchanged for a long BUY leg. | `scripts/agent/backtest_historical.py:1221` |
| The historical loop already rejects anything but one whole-share BUY leg. | `scripts/agent/backtest_historical.py:1424-1430` |
| `Candidate.legs` is a tuple, `len >= 1`; multi-leg is a convention-not-cap but unused here (phase 1 emits single-leg BUY candidates). | `scripts/agent/candidate.py` |
| `build_v2_artifact_payload(*, trades, skips, ...)` aggregates trades into one artifact and computes one benchmark section (`exposure_matched_midbar_v1`) from `trade.benchmark_pnl_usd`. | `scripts/agent/backtest_metrics.py:111` |
| M7 pinned criteria live in `paper_phase_criteria.py` with one-directional floors in `verify_artifact`. | `scripts/agent/paper_phase_criteria.py`, `scripts/agent/backtest_gate.py` |

## 2. Frozen decisions

| # | Decision |
|---|---|
| FD-P1-1 | **Strategy:** `relative_strength.long_only_proxy_v1`, long-only, emits `paper_eligible=True` BUY candidates only. Never labeled market-neutral; it is long-only and dollar-directional. |
| FD-P1-2 | **Score (verbatim from packet):** `rs_score = rank(momentum_21) + 0.50*rank(ema_gap_9_21) + 0.50*rank(sma_gap_21_50) + 0.25*rank(rsi14_centered) - 0.25*rank(realized_vol_21)`, where `rank` is the cross-sectional rank over the valid decision set. |
| FD-P1-3 | **Ranking:** ranks are computed only across the valid decision set at one `decision_ts_utc`. Ties in final `rs_score` break by predeclared universe order. No future data; ranks never use future returns. Exclusion counts are reported. |
| FD-P1-4 | **Validity:** a symbol is valid at the decision instant iff its features are `available` and the five scored features are finite, its quote verdict is `ok` with finite `mid > 0`, its market state is tradable with two-sided NBBO, and its spread is finite and non-negative. An invalid symbol is excluded with a machine-readable reason and never ranked. |
| FD-P1-5 | **Minimum decision set:** `MIN_VALID_SYMBOLS = 8`. Fewer than 8 valid symbols means `do_nothing` (empty selection / decision skip). |
| FD-P1-6 | **Selection / sizing:** select the top `TOP_N = 2` by `rs_score`. Equal per-leg notional `PAPER_NOTIONAL_USD = Decimal("1000")`; whole-share `qty` (floor); one single-leg BUY `Candidate` per selected symbol. No multi-leg `Candidate`, no short leg. |
| FD-P1-7 | **Reuse vs new:** the per-symbol fill/exit machinery (`_simulate_historical_long_trade`) is reused unchanged. The one genuinely new piece is a multi-symbol, same-timestamp decision harness that assembles every valid symbol's `SignalSnapshot` at the decision instant, ranks them, and selects the top 2. |
| FD-P1-8 | **Anti-lookahead:** reuse the M7 predicate (FD-M7-3/4). The rank reads only as-of-eligible features; tie-breaking uses the static universe order, never future returns. |
| FD-P1-9 | **Benchmarks:** report active PnL versus both `exposure_matched_midbar_v1` (existing pinned) and `universe_equal_weight_long_v1` (new: at each decision timestamp, an equal-notional long basket of every valid symbol). The new benchmark is additional metric/provenance; the verifier's pinned benchmark is unchanged. |
| FD-P1-10 | **Artifact aggregation:** the two long positions are scored under one `(strategy_id, rules_hash, data_pin)` artifact; their fills/PnL aggregate into that single artifact's M7 sample/risk/realism metrics (not one artifact per symbol). |
| FD-P1-11 | **Purity:** `relative_strength.py` imports no broker, preflight, orchestrator, paper_book, journal, or clock surface, and no `os`/`pathlib`/`subprocess`/`datetime`/`time`/`importlib`; it performs no I/O and no dynamic import; it is deterministic. |
| FD-P1-12 | **Fail-closed:** no production artifact write; M7 pinned criteria reproduced unchanged; `paper_eligible=True` is necessary-not-sufficient (the S9 artifact gate still governs opens). |

## 3. API surface (module `agent.strategies.relative_strength`)

- Constants: `STRATEGY_ID = "relative_strength.long_only_proxy_v1"`, `TOP_N = 2`, `MIN_VALID_SYMBOLS = 8`,
  `PAPER_NOTIONAL_USD = Decimal("1000")`, `SCORE_WEIGHTS` (ordered feature → weight map per FD-P1-2).
- `@dataclass(frozen=True) RankedSymbol`: `symbol: str`, `instrument_id: int`, `rs_score: Decimal`, `mid: Decimal`.
- `@dataclass(frozen=True) Exclusion`: `symbol: str`, `reason: str`.
- `@dataclass(frozen=True) CrossSectionalRanking`: `ranked: Tuple[RankedSymbol, ...]` (desc by `rs_score`, ties by
  universe order), `excluded: Tuple[Exclusion, ...]`, `valid_count: int`.
- `rank_decision_set(snapshots: Sequence[SignalSnapshot], *, universe_order: Sequence[str]) -> CrossSectionalRanking`.
- `class RelativeStrengthLongOnlyProxyV1`: attrs `strategy_id` and `synthetic = False` (the latter is a required
  hook so the S9 artifact gate treats it as a real, non-synthetic strategy, mirroring `directional_momentum`);
  method `decide(self, *, snapshots: Sequence[SignalSnapshot], universe_order: Sequence[str], now_ms: int) -> Sequence[Candidate]`
  returning the top-`TOP_N` BUY candidates, or `()` when `valid_count < MIN_VALID_SYMBOLS`.
- `strategy_for_id(strategy_id: str) -> RelativeStrengthLongOnlyProxyV1`; raises `ValueError` on unknown id.
- In `agent.backtest_metrics`: `UNIVERSE_EQUAL_WEIGHT_LONG_BENCHMARK = "universe_equal_weight_long_v1"`.

**Implementation notes (recorded after the impl review):**

- The proxy stamps a marketable BUY `limit_price` on each leg (`EDGE_BUFFER_BPS = 5 bps`, rounded DOWN to the
  Reg-NMS tick grid). This is a strategy-side cap; the historical harness (build step 2) may apply its own
  marketable-limit cap, in which case the harness value governs.
- Selection is **select-then-floor**: the top `TOP_N` are chosen by `rs_score`, then each leg's whole-share `qty`
  is floored. A selected symbol priced above `PAPER_NOTIONAL_USD` floors to 0 shares and is skipped without
  promoting the next rank, so `decide(...)` may return fewer than `TOP_N` candidates. This is acceptable for the
  mega-cap universe (qty >= 1 for any price <= $1000); the harness/Phase Gate must track basket-fill rate so a
  systematically unfillable name is surfaced rather than silently dropped.
- Ranking is fully deterministic: `rs_score` desc, then predeclared universe order, then symbol string; scores are
  keyed positionally so a duplicated symbol in the decision set cannot collapse two rows.

## 4. RED tests (`tests/agent/test_relative_strength_proxy_m7c.py`)

All fail before implementation (missing module / missing benchmark identifier):

1. Module imports and exposes `STRATEGY_ID == "relative_strength.long_only_proxy_v1"`.
2. `rank_decision_set` over a co-monotonic 10-symbol decision set ranks the strongest signal first and selects the
   intended top 2.
3. Ties (two symbols with identical feature vectors) break by predeclared universe order.
4. Invalid symbols (features unavailable, one-sided quote, market-state not tradable, invalid spread) are excluded
   with reasons and never ranked; `valid_count` counts only valid symbols.
5. Fewer than 8 valid symbols → `decide(...)` returns `()`.
6. `decide(...)` emits exactly 2 BUY candidates, each `paper_eligible`, single-leg, side `buy`, whole-share `qty`.
7. `strategy_for_id` maps the proxy id and raises `ValueError` on an unknown id.
8. Module import-surface purity (ast scan; no broker/preflight/orchestrator/os/pathlib/subprocess/clock imports, no
   `open`/`__import__`).
9. `agent.backtest_metrics` exposes `UNIVERSE_EQUAL_WEIGHT_LONG_BENCHMARK == "universe_equal_weight_long_v1"`.

## 5. Build order after this contract

1. Implement `relative_strength.py` to GREEN against these tests; add the benchmark identifier.
2. Wire the multi-symbol same-timestamp decision harness into the historical runner (assemble all symbols' bars,
   align decision timestamps, rank, select, then reuse `_simulate_historical_long_trade` per selected leg and
   aggregate both legs under one artifact) + emit `universe_equal_weight_long_v1` attribution.
3. Credentialed clean-window run (`2026-03-10` → `2026-04-08`, else next forward window) → failure/success review →
   evaluate the **Phase Gate** go/no-go.

## 6. Verification

```bash
# RED now (missing module / benchmark identifier):
python3 -m unittest tests.agent.test_relative_strength_proxy_m7c -v
# After GREEN, targeted + full suite:
python3 -m unittest discover -s tests -p 'test_*.py' -t .
git diff --check
find artifacts/backtests -maxdepth 2 -type f -print | sort   # expect only .gitkeep
```
