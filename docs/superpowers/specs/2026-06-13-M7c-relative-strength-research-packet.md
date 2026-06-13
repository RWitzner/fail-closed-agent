# M7c Relative-Strength / Market-Neutral Research Packet

- **Date:** 2026-06-13
- **Status:** Predeclared research/design packet, revised to proxy-first (rev 2). No code has been written for this family.
- **Parent:** `docs/superpowers/specs/2026-06-13-M7-backtest-gate-contract.md`
- **Predecessor closeout:** `docs/superpowers/reviews/2026-06-13-M7b-diagnostic-family-closeout-review.md`
- **Hypothesis id:** `m7c_relative_strength_market_neutral_v0_20260613` (family umbrella; phase-1 strategy id is `relative_strength.long_only_proxy_v1`)

## Review Disposition

- **Rev 1 (original draft):** named true market-neutral the *preferred* phase-1 target and the long-only
  relative-strength proxy a *fallback*. That draft's Architect/Critic pass approved the rev-1 framing.
- **Rev 2 (this version):** a direction review found the rev-1 sequencing inverted versus the packet's own
  Strongest Antithesis. True market-neutral is the largest possible build — it re-opens the deliberately deferred
  short-side risk path (`short_side_disabled` in `scripts/agent/risk/locate.py` and `scripts/agent/risk/can_open.py`,
  single-leg/long-only M5 preflight in `scripts/agent/execution_preflight.py`, single-BUY-leg historical
  simulation, benchmark hard-pinned to `exposure_matched_midbar_v1`) — while the long-only proxy is a far smaller
  build that answers the actual open question (*does cross-sectional residual signal exist on this substrate at
  all?*) without any of that short-side surface. The proxy reuses the existing single-leg fill/exit machinery
  (`_simulate_historical_long_trade`) and adds only the cross-sectional ranking/decision harness — which phase 2
  needs anyway — with no short side, locate/SSR, multi-leg preflight, or basket-atomicity work. Rev 2 makes
  `relative_strength.long_only_proxy_v1`
  the **phase-1 gating probe** and demotes true market-neutral to a **conditional phase 2** behind a predeclared
  go/no-go. The rev-1 approval does **not** carry forward to this materially different sequencing; rev 2 was
  re-reviewed independently before commit (see "Independent Review (rev 2)" at the end of this document).
- No thresholds were relaxed, no gates flipped, no artifact written. `artifacts/backtests/` remains `.gitkeep`
  only; paper edge-validation and M8 stay blocked.

## Purpose

This packet starts a new post-M7 strategy/universe family after the M7b no-trade closeout. It intentionally
breaks from the failed L1/BBO 1-minute long-only momentum family and predeclares a relative-strength direction
before code, threshold work, or a reviewed artifact loop.

The research runs in two phases. **Phase 1 (the gating probe)** is a long-only cross-sectional relative-strength
proxy: rank a fixed universe using only point-in-time features, buy the strongest names with equal notional, close
on a deterministic horizon, and score net of execution-realistic costs against both an exposure-matched and an
equal-weight benchmark. It reuses the existing single-leg fill/exit machinery and adds only a cross-sectional
ranking/decision harness (which phase 2 needs anyway); it builds no short-side, locate/SSR, multi-leg-preflight,
or basket-atomicity infrastructure. **Phase 2 (conditional)** is a market-factor-reduced long-short
relative-strength spread; it is
authorized only if phase 1 clears a predeclared go/no-go, because it requires a large new build (multi-leg, short
side, locate/borrow/SSR, basket atomicity, second benchmark). Phase 2 is reached only when the cheap probe has
shown cross-sectional residual signal exists on this substrate.

This packet does not authorize paper trading, live trading, production artifact writes, threshold relaxation, or
symbol cherry-picking.

## Evidence Grounding

- `PLAN.md` and `CLAUDE.md` state that M7 is offline complete, M7b failed, and the next loop is a new
  predeclared strategy/universe family.
- Production `artifacts/backtests/` contains only `.gitkeep`; no reviewed artifact currently verifies `ok`.
- `directional.momentum_v1` failed AAPL-only, broader, and holdout attempts.
- `directional.momentum_v2` reduced activity but failed every valid holdout symbol on net execution-realistic PnL.
- The M7b diagnostic closeout rejects an M7c gap/adverse-selection gate for the current long-only momentum family.
- Current code can represent multiple candidate legs in `Candidate`, but the reviewed historical artifact path
  currently accepts only one BUY leg and risk structurally rejects short-establishing legs with
  `short_side_disabled`.

## Non-Authorization

Do not do any of the following from this packet alone:

- flip `config/agent_rules.json` or `config/risk_rules.json` gates,
- start paper edge-validation,
- start M8/live work,
- write to `artifacts/backtests/`,
- relax `paper_phase_criteria.py` or artifact threshold floors,
- run a reviewed artifact loop before the preconditions in this packet are met,
- copy raw licensed quote rows into docs or prompts.

## Research Decision

The next family is `relative_strength` as a research design target, built in two predeclared phases:

- **Phase 1 (first, unconditional build): `relative_strength.long_only_proxy_v1`.** The long-only cross-sectional
  gating probe. It is the first and possibly only experiment in this family. It exists to answer one question
  before any short-side investment: does cross-sectional residual signal exist on this universe/substrate at all?
- **Phase 2 (conditional, only after the Phase Gate passes): `relative_strength.market_neutral_v1`.** The
  long-short market-neutral spread. Not authorized until phase 1 produces a reviewed artifact that verifies `ok`
  and clears the go/no-go in "Phase Gate" below.

The term "market-neutral" is conditional and applies to phase 2 only. A production-track artifact may use that
label only if the code models both sides honestly: multi-leg basket simulation, short-side risk semantics,
locate/SSR/borrow treatment or a documented no-borrow assumption that remains stricter than the current gate, and
basket-level exposure checks. Phase 1 must never be labeled market-neutral; it is long-only and dollar-directional.
Until phase 2 is built and reviewed, this is a relative-strength research packet, not paper approval evidence.

### Phase Gate (predeclared go/no-go)

These GO thresholds are fixed now, before any phase-1 PnL is computed, and may not be relaxed after seeing results:

- **GO to phase 2** only if the phase-1 proxy's reviewed artifact verifies `ok`: it meets every M7 pinned criterion
  (positive net execution-realistic PnL, positive active PnL versus *both* `exposure_matched_midbar_v1` and
  `universe_equal_weight_long_v1`, profit factor >= 1.10, positive average trade bps, the sample/risk/realism
  gates), with broad symbol breadth and no single-symbol concentration breach (see concentration limits below).
- **NO-GO / STOP** if the phase-1 proxy fails broadly (e.g. net <= 0 or active <= 0 across most symbols, or a
  pass that depends on one symbol or a cherry-picked subset). A broad phase-1 null is decisive evidence that the
  binding constraint is the **substrate**, not the strategy shape. The next decision is then a SUBSTRATE decision —
  a longer decision/holding horizon, an L2/MBP-10 depth-aware fill tier, or a wider liquidity-screened universe — or
  an explicit stop. It is NOT "start a sixth strategy family on the same L1 1-minute substrate," and it is NOT the
  phase-2 short-side build. See the search-budget stop rule in `PLAN.md`.

## Strongest Antithesis

This is the argument that drove the proxy-first sequencing. True market-neutral support may be a larger contract
expansion than the likely alpha justifies: it touches multi-leg execution, short-side risk, locate/borrow/SSR
semantics, basket atomicity, new benchmark attribution, and concentration diagnostics before the strategy can even
be tested honestly. If that work cannot be implemented with the same safety standard as M0-M7, the correct outcome
is no-trade or a clearly labeled long-only relative-strength proxy, not a rushed "neutral" artifact. That is exactly
why phase 1 is the long-only proxy and phase 2 (true-neutral) is conditional on the Phase Gate.

The counter-argument — and the reason phase 2 still exists at all — is that true long-short baskets are a cleaner
test of cross-sectional residual edge than a long-only proxy. But they also create more ways to fool the gate with
under-modeled short costs, partial basket fills, or benchmark labels, so they are only worth building once the cheap
proxy has shown the cross-sectional signal exists.

## Phase-2 Architecture Preconditions

These are phase-2-only gates. Phase 1 (the long-only proxy) does not touch them and is built first regardless.
True long-short relative-strength (phase 2) is blocked until these are designed, tested, and reviewed:

1. Historical artifact simulation accepts and scores multi-leg candidates instead of rejecting anything except a
   single BUY leg.
2. Execution preflight supports the chosen opening matrix instead of rejecting multi-leg candidates and non-BUY
   opens.
3. Short-establishing legs have explicit risk semantics instead of the current structural `short_side_disabled`
   refusal and deny-all locate seam.
4. Locate, borrow-cost, SSR, and short-sale fee assumptions are either modeled end-to-end or the strategy is not
   allowed to claim true market-neutral behavior.
5. Entry and exit simulation treats a basket atomically: if any required leg cannot be priced or fails market
   state/quality gates, the entire basket is skipped.
6. Metrics aggregate at basket level and still expose leg-level diagnostics for concentration, side imbalance,
   and execution realism.
7. Artifact verification remains keyed by the exact `(strategy_id, rules_hash, data_pin)` triple.

Phase 2 is reached only if the phase-1 proxy clears the Phase Gate. If the proxy fails broadly, the next step is
the substrate decision in "Phase Gate", not these short-side preconditions. The phase-1 proxy is never called
market-neutral and must carry stricter active-benchmark evidence (both `exposure_matched_midbar_v1` and
`universe_equal_weight_long_v1`).

## Predeclared Universe

Use the full existing broad M7 universe, in this exact order:

```text
AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AVGO, COST, NFLX
```

Selection rule:

- Reuse the full ordered M7 broad universe because it was already documented before the M7b closeout and has
  comparable local historical source coverage.
- Do not remove symbols based on v1/v2 PnL, active PnL, gap buckets, spread buckets, or diagnostic rank.
- Do not add symbols to improve a result after seeing relative-strength metrics.
- A later sector-balanced 20-50 name universe requires a new packet and a new hash-bound universe block before
  any PnL is reviewed.

Minimum valid decision set:

- At least 8 of 10 symbols must have eligible point-in-time features, two-sided quotes, and market-state approval
  at the decision instant.
- Fewer than 8 valid symbols means `do_nothing` / basket skip.
- No single symbol may represent more than 35% of gross opened legs or more than 50% of net positive PnL in a
  passing artifact candidate.

Manifest universe block:

```json
{
  "hypothesis_id": "m7c_relative_strength_market_neutral_v0_20260613",
  "selection_rule": "Reuse the full ordered M7 broad universe before relative-strength metrics; no symbol additions or removals from failed v1/v2 diagnostics.",
  "symbols": ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "COST", "NFLX"]
}
```

## Predeclared Windows

The already-inspected M7/M7b windows may be used for implementation smoke tests, diagnostics-format tests, and
sanity checks only. They are not pristine approval evidence for this family because the new packet is being
written after those failure reviews.

Known inspected windows:

- `2026-05-11T13:30:00Z` to `2026-06-09T20:00:00Z`
- `2026-04-09T13:30:00Z` to `2026-05-08T20:00:00Z`

Clean reviewed historical window:

- Preferred clean backfill window: `2026-03-10T13:30:00Z` to `2026-04-08T20:00:00Z`, only if no prior local
  relative-strength metrics or symbol-level strategy outcomes exist for that exact family/window.
- If that condition is false or data is unavailable, use the next completed 20+ full-RTH-session forward window
  after 2026-06-13.
- The manifest must list exact RTH session windows and must meet the existing M7 minimum sample constraints.

## Feature Set

Use only features available as-of the decision timestamp. The first design should use the existing M3 feature
surface before adding new indicators:

- `momentum_21`
- `ema_gap_9_21`
- `sma_gap_21_50`
- `rsi14_centered`
- `z_ret_21`
- `realized_vol_21`
- point-in-time quote spread / mid from the `SignalSnapshot`

Predeclared relative-strength score:

```text
rs_score =
  cross_sectional_rank(momentum_21)
  + 0.50 * cross_sectional_rank(ema_gap_9_21)
  + 0.50 * cross_sectional_rank(sma_gap_21_50)
  + 0.25 * cross_sectional_rank(rsi14_centered)
  - 0.25 * cross_sectional_rank(realized_vol_21)
```

Ranking rules:

- Ranks are computed only across the valid decision set at the same timestamp.
- Ties sort by the predeclared universe order and do not use future returns.
- A symbol with unavailable features, one-sided quotes, failed market state, or invalid spread is excluded from
  the decision set; exclusion counts are reported.
- The score cannot use prior v1/v2 PnL, symbol-level artifact outcomes, or diagnostics buckets.

## Candidate Construction

### Phase 1 (build first): long-only cross-sectional proxy `relative_strength.long_only_proxy_v1`

- Long only the top 2 ranked valid symbols with equal per-leg notional. No short side.
- Entry uses the M7 quote-B latency semantics; exit uses a single deterministic horizon for all legs.
- First horizon: 30 completed 1-minute bars, forced skip if the horizon crosses RTH close.
- Reuses the existing single-leg fill/exit machinery (`_simulate_historical_long_trade`); emits one BUY `Candidate`
  per held leg (no multi-leg `Candidate`, no short-establishing leg, no change to the risk/locate/preflight seams).
- The one genuinely new piece is a cross-sectional ranking/decision harness: it assembles every valid symbol's
  point-in-time `SignalSnapshot` at the same decision timestamp, ranks them, and selects the top 2. The current
  per-symbol `run_historical_backtest` cannot do this because it only holds one symbol's snapshot at a time.
- Artifact aggregation: the two long positions are scored under one `(strategy_id, rules_hash, data_pin)` artifact,
  with their fills/PnL aggregated into that single artifact's M7 sample/risk/realism metrics (not one artifact per
  symbol).
- Score against both the existing exposure-matched benchmark (`exposure_matched_midbar_v1`) and a full-universe
  equal-weight long benchmark (`universe_equal_weight_long_v1`).
- Label as `relative_strength.long_only_proxy_v1`, never market-neutral. It is long-only and dollar-directional.
- No pyramiding and no overlapping position for the same symbol/strategy/window.

### Phase 2 (conditional, only after the Phase Gate passes): true-neutral basket `relative_strength.market_neutral_v1`

Build only after phase 1 verifies `ok` and the architecture preconditions are met:

- Long basket: top 2 ranked valid symbols.
- Short basket: bottom 2 ranked valid symbols.
- Gross notional: fixed and symmetric by side before fees.
- Per-leg notional: equal within side.
- Net dollar exposure target: absolute long notional minus short notional <= 10% of gross notional. (This is
  dollar-neutral at best, not beta-neutral; the artifact must not claim beta-neutrality it does not model.)
- The basket emits one multi-leg `Candidate` only when all legs are priceable and pass quality gates.
- Entry uses the M7 quote-B latency semantics; exit uses the same deterministic horizon for all legs.
- First horizon: 30 completed 1-minute bars, forced skip if the horizon crosses RTH close.
- No pyramiding and no overlapping basket for the same strategy/window unless a separate packet adds portfolio
  accounting semantics.

## Benchmark Contracts

The existing M7 artifact verifier currently accepts only `exposure_matched_midbar_v1` as the primary benchmark
method. The extra family benchmarks below are additional metrics/provenance requirements for this family; they do
not replace the pinned M7 benchmark unless the verifier contract is separately extended and reviewed.

`universe_equal_weight_spread_v1`:

- Applies only to true-neutral mode.
- At each basket decision timestamp, use the same valid decision set and the same entry/exit bars as the strategy.
- Long the top 2 ranked symbols and short the bottom 2 ranked symbols by universe order, not by strategy score,
  with equal per-leg notional and equal gross notional per side.
- If fewer than 8 symbols are valid or any benchmark leg is not priceable at entry/exit, the benchmark and the
  strategy basket both skip that decision.
- Use the same spread, latency, fee, borrow/short-cost, market-state, and CA-blackout assumptions as the strategy.
- Active PnL is strategy basket net execution-realistic PnL minus this benchmark basket net PnL over the same
  timestamps.

`universe_equal_weight_long_v1`:

- Applies only to the phase-1 long-only proxy.
- At each decision timestamp, buy an equal-notional basket of every valid symbol in the predeclared universe.
- Use the same entry/exit bars, latency, fees, market-state, and CA-blackout assumptions as the proxy strategy.
- If fewer than 8 symbols are valid, both the benchmark and strategy skip that decision.
- Active PnL is strategy net execution-realistic PnL minus equal-weight benchmark net PnL over the same timestamps.

## Metrics And Acceptance Gates

The M7 pinned criteria remain mandatory and unchanged:

- `metrics.pass == true`
- at least 20 sessions
- at least 30 opened-and-closed trades
- at least 5 traded sessions
- positive `net_execution_realistic_pnl_usd`
- positive `active_pnl_usd`
- profit factor >= 1.10
- positive average trade bps
- maximum drawdown <= 1.50% of allocated paper notional
- worst single-session loss <= 0.75% of allocated paper notional
- p95 realism gap <= 15 bps
- max single fill divergence <= 50 bps
- zero required safety, artifact mismatch, and exception counts

Additional family-specific gates (the basket/exposure and equal-leg gates apply to phase 2 only; the concentration
limits and the proxy benchmark gate apply to phase 1):

- Basket net dollar exposure must stay within +/-10% of gross notional on every opened basket.
- Long and short leg counts must be equal for true-neutral mode.
- (Phase 2 only) At least 70% of opened baskets must have all intended legs filled in simulation; otherwise the
  family is too fragile for the current data/latency model.
- No single symbol may contribute more than 35% of gross opened legs.
- No single symbol may contribute more than 50% of net positive PnL in a passing candidate.
- True-neutral mode must report active PnL versus:
  - `exposure_matched_midbar_v1`, and
  - `universe_equal_weight_spread_v1`.
- Long-only proxy mode must report active PnL versus:
  - `exposure_matched_midbar_v1`, and
  - `universe_equal_weight_long_v1`.

Passing all gates only permits paper edge-validation to start. It does not approve M8 or live trading.

## Diagnostics To Export On Failure

Export diagnostics outside `artifacts/backtests/`. The first reviewed attempt is the phase-1 proxy, so the phase-1
set applies first; the phase-2 set applies only if phase 2 is built and fails.

Phase 1 (long-only proxy):

- opened-leg count, skipped-decision count,
- valid decision-set size distribution and exclusion counts,
- per-symbol gross leg count and PnL contribution,
- PnL by long-side rank bucket (top-2 selection),
- spread/latency realism-gap distribution,
- benchmark attribution against both `exposure_matched_midbar_v1` and `universe_equal_weight_long_v1`,
- rows-truncated flags.

Phase 2 (true-neutral basket), additionally:

- basket count, opened-leg count, skipped-basket count,
- long/short side PnL split,
- rank bucket PnL by long-side rank and short-side rank,
- spread/latency gap distribution by side,
- basket net exposure distribution,
- benchmark attribution against both `exposure_matched_midbar_v1` and `universe_equal_weight_spread_v1`.

Diagnostics are not approval evidence and must not become a threshold source.

## Stop Rules

Stop and write a failure review if any of these are true:

- (Phase 1) The proxy passes only via one symbol, a cherry-picked subset, or after threshold relaxation, or it
  fails any of net PnL, active PnL (vs both benchmarks), profit factor, average bps, sample, risk, or realism gates
  on the clean window. A broad phase-1 failure routes to the substrate decision in "Phase Gate", not phase 2.
- (Phase 2) Multi-leg or short-side semantics are not implemented and reviewed, but the branch still tries to call
  itself market-neutral.
- The only passing result depends on removing symbols from the predeclared universe.
- The only passing result is concentrated in one symbol or one side.
- The clean window fails net PnL, active PnL, profit factor, average bps, sample, risk, or realism gates.
- A result passes only after threshold relaxation.
- Any production artifact write would target a nested or non-production artifact directory.

On stop, leave `artifacts/backtests/` unchanged and keep paper/M8 blocked.

## Future Implementation Order

No implementation should start until this packet is reviewed. If approved, build in this order.

**Phase 1 — long-only cross-sectional proxy (build first, the gating probe):**

1. Contract and RED tests for cross-sectional ranking over the predeclared valid decision set at one timestamp,
   with deterministic tie breaking and exclusion-count reporting.
2. A NEW multi-symbol, same-timestamp decision harness that assembles every valid symbol's point-in-time
   `SignalSnapshot` at the decision instant, ranks them, and selects the top 2. This is the one genuinely new piece
   phase 1 requires; the existing per-symbol `run_historical_backtest` holds only one snapshot at a time and cannot
   compute the rank. The single-leg fill/exit machinery (`_simulate_historical_long_trade`) is reused unchanged.
3. Pure `relative_strength.long_only_proxy_v1` strategy implementation (no I/O, no broker imports, no clocks) that
   emits long BUY legs for the top 2 ranked valid symbols.
4. The one new benchmark method `universe_equal_weight_long_v1`, plus metrics/provenance for the proxy, with the
   two long positions aggregated under one artifact.
5. Historical artifact builder support for the new strategy id and the manifest universe block.
6. Failure/success review using the clean window rule above, then evaluate the **Phase Gate** go/no-go.

**Phase 2 — true-neutral long-short basket (build ONLY if the Phase Gate GO is met):**

7. Contract and RED tests for multi-leg historical simulation and short-side refusal/approval semantics.
8. Basket-level backtest primitives that score all legs atomically.
9. Short-side risk semantics, locate/borrow/SSR/short-fee modeling (must round against the strategy), and
   multi-leg/non-BUY execution preflight support.
10. Metrics/provenance additions for basket exposure, concentration, and second benchmark
    (`universe_equal_weight_spread_v1`) attribution.
11. `relative_strength.market_neutral_v1` strategy implementation and historical artifact builder support.
12. Failure/success review using the clean window rule above.

If the Phase Gate is NO-GO, do not start phase 2. Take the substrate decision in "Phase Gate" instead.

Only if a reviewed production artifact (phase 1, or later phase 2) verifies `ok`, start the existing paper
edge-validation runbook.

## Verification Before Handoff

Before any code handoff, re-check:

```bash
git status --short --branch
find artifacts/backtests -maxdepth 2 -type f -print | sort
```

Expected artifact state before implementation: only `artifacts/backtests/.gitkeep`.

No source tests are required for this packet alone because it changes documentation only. Any future code loop must
run targeted tests plus the full suite:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -t .
```

## Independent Review (rev 2)

Rev 2 was re-reviewed by two independent read-only passes (architect + adversarial critic) on 2026-06-13, since the
rev-1 approval does not carry forward to the proxy-first sequencing. Both returned ITERATE; all blocking findings
were incorporated into this revision:

- The proxy-first cost claim was corrected. Phase 1 does NOT reuse the existing simulator "unchanged": the
  per-symbol `run_historical_backtest` cannot compute the cross-sectional rank because it holds only one symbol's
  snapshot at a time. Phase 1 now explicitly adds a multi-symbol, same-timestamp ranking/decision harness (the one
  genuinely new piece) and reuses only the single-leg fill/exit machinery; Future Implementation Order step 2 calls
  this out. The proxy remains far smaller than phase 2 (no short-side/locate/SSR/multi-leg/basket surface).
- The Phase Gate wording was fixed: the GO thresholds are predeclared and fixed before any phase-1 PnL is computed
  and may not be relaxed after results (the earlier "decide before looking at PnL" phrasing was impossible because
  the GO criteria are themselves PnL-based).
- Three leftover rev-1 "fallback"/"preferred path" labels (Strongest Antithesis, Phase-2 Architecture
  Preconditions, Benchmark Contracts) were scrubbed so the long-only proxy is consistently the phase-1 build-first
  probe, not a contingency.
- Diagnostics and Stop Rules were split per phase so the first reviewed attempt (the phase-1 proxy) has directly
  applicable failure handling instead of long-short basket vocabulary.

Confirmed unchanged by the review: the M7 pinned criteria are reproduced verbatim with no relaxation; no artifact
write is authorized; fail-closed and `.gitkeep`-only discipline hold; the net-dollar-neutral target is honestly
labeled dollar-neutral (not beta-neutral); the PLAN.md two-family search-budget stop rule is coherent.
