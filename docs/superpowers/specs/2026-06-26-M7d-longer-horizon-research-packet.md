# M7d Longer-Horizon Substrate Research Packet

- **Date:** 2026-06-26
- **Status:** GPT-reviewed rev 3 (2026-07-02) — predeclared research/design packet. No credentialed run is authorized. Rev 2 folded in a full-project review (4-agent deep review + own-run reproduction): a MEASURED pre-run feasibility read (the entry-leg realism floor), the fix-A-contract baseline C2 must be compared against, a max-cap sample-size caveat, pinned latency/slippage numerics, a pinned C1/C2 run order, a GO-path lead-time acknowledgement, operational prerequisites, and the `PLAN.md` substrate-search budget. Rev 3 applies the GPT review fixes: no undefined "comfortable margin" gate, route-B does not auto-select the second substrate lever, and current-HEAD operational state is reconciled (1846 tests green; calendar provider/fixture and M7d driver built). Three general harness-correctness fixes from the rev-2 review are committed (decision-bar FD-2 eligibility in the cross-sectional runner; both writers bind `rules_hash` to the config-derived hash; builder `horizon` required-explicit) — all byte-identical on the clean-window baseline.
- **Parent contract:** `docs/superpowers/specs/2026-06-13-M7-backtest-gate-contract.md`
- **Predecessor family:** `docs/superpowers/specs/2026-06-13-M7c-relative-strength-research-packet.md` (rev 2, APPROVE) — the relative-strength family this experiment inherits and holds constant.
- **Predecessor null:** `reports/m7_historical_runs/2026-03-10-clean-rs-v1/summary.json` (M7c phase-1 broad NULL, staged-only).
- **Hypothesis id:** `m7d_horizon_substrate_v0_20260626`. This is a SUBSTRATE experiment within the existing M7c relative-strength family — **not** a new strategy family and **not** a new milestone. Strategy id is unchanged: `relative_strength.long_only_proxy_v1`. It cross-references and re-pins the M7c universe block (`m7c_relative_strength_market_neutral_v0_20260613`).

## Review Disposition

- **GPT-reviewed rev 3; not run-authorized.** Authored by a multi-agent design+critique pass (3-architect panel → synthesis → 5-lens adversarial critique; 2 blockers + 8 majors applied), then GPT-reviewed on 2026-07-02. The GPT review returned two must-fix methodology issues (undefined margin gate; route-B auto-selection ambiguity) plus one stale-state documentation issue; all three are applied in rev 3. It must not be acted on (no harness change, no manifest build, no credentialed pull, no run) until Robin explicitly decides to run M7d rather than route straight to a realism-matched lever **AND** gives a separate go.
- This packet predeclares **one** decision-carrying configuration, the universe, the windows, the entry-latency convention, and the routing-on-null **before** any new PnL is computed. The pinned M7 paper-phase criteria are reproduced and are **not** relaxed, scaled, or re-indexed anywhere in this document.
- No thresholds were relaxed, no gates flipped, no artifact written. `artifacts/backtests/` remains `.gitkeep`-only; paper edge-validation and M8 stay blocked.

## Purpose

The M7c phase-1 long-only relative-strength proxy nulled broadly on its preferred clean window (`2026-03-10 → 2026-04-08`, 21 sessions, 1144 trades, staged-only). The two-family search-budget stop rule in `PLAN.md` (momentum = family 1 nulled; relative-strength = family 2 nulled) routes the program to a **substrate decision**, not a third same-substrate strategy family. Robin's call (2026-06-26) selected the **cheapest** substrate axis to test first: a longer decision/holding horizon on the same L1 `EQUS.MINI:bbo-1m` data — explicitly NOT the L2/MBP-10 depth build, NOT a wider universe first, and NOT the phase-2 short side.

This packet predeclares that experiment as a **falsifiable test**, not as a claimed fix. The M7c null had **two distinct failure modes**, and this packet is honest about how each does (and does not) respond to holding horizon:

1. **No edge after costs** — PF `0.55`, `active_pnl_usd` −$120.65 vs `exposure_matched_midbar_v1`, `avg_trade_bps < 0`. This is *plausibly* horizon-sensitive: a relative-strength rank held only 30 minutes may be dominated by bid-ask-bounce noise and too short for genuine cross-sectional drift to clear round-trip friction. A longer hold raises the drift/cost ratio. **Plausible lever — not proven.**
2. **Realism caps blown** — p95 realism gap `29.8` > 15; max single-fill divergence `97.5` > 50. The modeled gap is a **round-trip touch-to-mid half-spread** (derived in Evidence Grounding): `gap_bps ≈ entry_half_spread_bps + exit_half_spread_bps`. The **entry** leg is horizon-invariant; the **exit** leg is **horizon-dependent** (a different horizon selects a different exit bar, with a different spread and time-of-day), and the late-day `session_horizon_crosses_close` drops re-compose which trades survive. So horizon *could* move p95/max through the exit leg and sample re-composition — but a longer hold is **not designed** to compress per-fill spread, so it is **plausible-not-proven** that the caps stay blown. **This packet does NOT assert that a longer horizon will fix the realism caps, nor that it cannot move them.**

The honest framing: this is the **cheapest edge-existence probe that must clear BEFORE the heavy L2/MBP-10 execution-realism build is worth funding** — not a likely GO. P(GO) is low and **realism-cap-dominated**: a GO requires BOTH caps to clear, and 30m blows both; whatever residual GO mass exists lives precisely in the hedged exit-leg-spread / late-day-drop re-composition channel above, not in any claimed invariance. **Measured (2026-07-02, snooped window only — see Evidence Grounding):** the horizon-invariant entry-leg floor alone is p95 ≈ 13.3 bps over all 1147 fix-A-baseline trades and ≈ **14.1 bps over the 120m-survivor subset (94% of the 15-bps cap)**; the exit leg adds ≈ 20 bps at p95; the survivor-subset combined gap is p95 ≈ **31.7 (2.1× the cap)**; and max(entry_half) ≈ 85.8 bps exceeds the 50 max-cap on its own. The late-day-drop re-composition RAISES the floor (13.27 → 14.13: the widest spreads are the first hour, which the 120m cutoff keeps, while it drops the tight close). A C2 GO therefore requires the fresh window's spread regime to be roughly HALF the snooped window's — formally reachable, structurally improbable. The run's realistic decision value is the EDGE read (route A vs route B), and the packet is honest about that. The step is still decision-relevant: no post-cost edge at a longer hold means L2 is not worth its cost; edge-but-blown-realism tells us the binding constraint is execution friction and routes the *next* (separately-predeclared) substrate spend.

This packet does not authorize paper trading, live trading, production artifact writes, threshold relaxation, or symbol cherry-picking.

## Evidence Grounding

All claims are code- or artifact-cited; re-verify against HEAD before acting.

- **M7c two-failure-mode null (what this experiment responds to).** `reports/m7_historical_runs/2026-03-10-clean-rs-v1/summary.json`: `p95_realism_gap_bps = 29.817444`, `max_single_fill_divergence_bps = 97.484375`. Net −$839.68, PF 0.55, `active_pnl_usd` −$120.65 vs `exposure_matched_midbar_v1`, `avg_trade_bps < 0`, on interval `1m` / horizon `30m`, top-2 equal-notional long, 10-symbol universe, 21 sessions, 1144 trades. Breadth was broad (10 symbols traded, max 12.3% of legs / 16.8% of net-positive PnL — no concentration breach), so the null is broad, not a single-name artifact.
- **Fix-A contract skew + the like-for-like baseline (2026-07-02).** The staged clean-rs-v1 quotes predate the
  fix-A `ts_event` session-membership rule (`19786cf`) and no longer pass the HEAD validator (the 102
  event-outside-session rows). The fix-A-compliant rerun — rows re-filtered by the fix-A rule, manifest rebuilt
  with the committed `build_cross_sectional_input_manifest` (manifest_hash `90866e9021c477285d6267aaa27ac696bdd8d079a0d95b204994006ebed58a5d`),
  run through the committed harness — gives **1147 trades, net −$858.01, active −$111.51 vs
  `exposure_matched_midbar_v1`, p95 29.949, max 97.484**: the NULL is robust under the current contract (this
  reproduces GPT's own falsification first-hand). **C2 must be compared against THIS baseline** — against the
  staged pre-fix numbers, C2 would differ in BOTH horizon AND data contract. The 2026-07-02 FD-2 decision-bar
  eligibility fix left this baseline byte-identical (real `bbo-1m` rows carry their minute boundary in `ts_recv`,
  so no decision bar is late-received on this data).
- **Measured entry-leg realism floor (2026-07-02, snooped window only — no fresh-holdout leak).** Per trade,
  `gap ≈ entry_half_spread + exit_half_spread` (identity residual ≤ 1.0 bp across all 1147 trades), so
  `p95(gap) ≥ p95(entry_half)` at ANY horizon. Measured on the fix-A baseline: p95(entry_half) = 13.27 (all
  trades) / **14.13 in the 120m-survivor subset** (decisions with decision+120m ≤ pinned close — the closest
  available approximation of C2's entry population); p95(exit_half) ≈ 20.2–20.4; survivor-subset combined gap
  p95 = **31.67**; max(entry_half) = 85.8 > the 50 max-cap alone; first-hour (13Z UTC) entry_half p95 = 28.2 vs
  8.8–14.6 for every later hour. The 120m late-day drop KEEPS the wide-spread morning and DROPS the tight close,
  so sample re-composition raises the floor rather than lowering it. This is the quantitative basis for "P(GO)
  is low and realism-cap-dominated" — stated as measurement, not hedged prose.
- **The null does NOT causally isolate the substrate.** `CLAUDE.md` / `PLAN.md`: the M7c result is "one tested configuration nulled clean; it does NOT causally isolate the substrate from the horizon, universe, or strategy shape." The GPT review (2026-06-26) softened the earlier "substrate proven" overclaim to predeclared-rule-based routing. This packet inherits that discipline in **both** causal directions: it asserts neither that horizon will fix the edge nor that it cannot move the realism caps.
- **Realism-gap formula — the gate, and why the EXIT leg is horizon-dependent.** The gating metrics in the cross-sectional harness are `p95_realism_gap_bps` and `max_single_fill_divergence_bps`, BOTH produced by `_gap_summary → _realism_gap_bps` (`backtest_historical.py:1573-1587`), over **per-trade** values. The modeled fills are `entry_fill = entry.ask`, `exit_fill = exit_bar.bid` (`backtest_historical.py:1544-1545`), so `gross_modeled = (exit_bid − entry_ask)·qty` and the gap reduces algebraically to `|(exit_mid − entry_mid) − (exit_bid − entry_ask)|·qty / notional·1e4 = entry_half_spread_bps + exit_half_spread_bps`. The **entry** half-spread is fixed at the +1-bar entry bar (horizon-invariant); the **exit** half-spread is read from `exit_bar`, whose identity is `decision + horizon` via `horizon_gate` (`signal_snapshot.py:160-178`) — so it **is** horizon-dependent. Note: despite its name, `max_single_fill_divergence_bps` is `_gap_summary`'s max of this **combined per-trade round-trip** gap, **not** a per-fill divergence (it is NOT the `execution_realism.py` live-exec `divergence_bps`, which is the unrelated M6 paper-exec path).
- **The modeled gap is SIZE-INDEPENDENT.** `qty` never moves the fill price (`entry.ask` / `exit_bar.bid` are touch prices; no book-walking). Therefore depth/MBP-10 modeling, which adds size-vs-depth **impact** on top of the touch, would *enlarge* this gap, not clear it. The gap-as-computed shrinks only via **tighter spreads** (more-liquid universe / better time-of-day) or **passive/non-crossing fills** that capture rather than pay the half-spread. This materially constrains the routing (see Stop Rules §B).
- **Holding horizon IS a parameter (the cheap lever).** `horizon_gate()` resolves `exit_bar_end = event_start_bar_end + horizon minutes` (`signal_snapshot.py:160-178`); `backtest_historical.py:1813-1815` validates `manifest.execution.horizon ∈ config.horizons`. Lengthening the hold at interval `1m` is **config-only**: add the horizon to `signal.horizons` + a coefficient block, and set `manifest.execution.horizon`. The gate infrastructure needs **no** change.
- **Coarser DECISION bars are NOT runnable today (and are out of scope here).** The manifest carries an `interval` threaded to `resample_midbars()`, BUT `resample_midbars()` hard-rejects non-`1m` (`bar_series.py:114-115`), as do `signal_config.py:117-119` and the manifest validators (`backtest_historical.py:414-415`, `:526-527`). Coarser bars would require real un-hardcoding AND would rescale the frozen `[9,21,50]`-**bar** feature windows in wall-clock time (confounding the strategy), AND would need a realism-model re-validation. They are therefore **deferred to a separate future predeclaration** (see Future Work) and play no part in this experiment's GO/NULL.
- **Entry latency is hardcoded +1 minute.** `_entry_bar_end()` returns `_add_minutes(decision_bar_end, 1)` (`backtest_historical.py:740-741`). At interval `1m` that is exactly +1 bar — so for this experiment (interval `1m` only) the entry convention is unchanged from the M7c null and needs no code change.
- **Late-day decisions crossing RTH close are dropped.** `horizon_gate` returns `GateFail('session_horizon_crosses_close')` (`signal_snapshot.py:175-177`); the runner `continue`s past the decision (`backtest_historical.py:1911-1912`). A longer hold drops MORE late-day decisions → a morning/midday entry-time selection bias and a smaller sample (directly relevant to `min_trades` / `min_traded_sessions` and to statistical power).
- **Dual-benchmark gating lives in two places.** The exposure-matched active-PnL gate is `evaluate_paper_phase_criteria` (`paper_phase_criteria.py:173-178`, `metrics.benchmark.active_pnl_usd`); the `universe_equal_weight_long_v1` active-PnL gate is a separate fail-closed guard in the cross-sectional writer (`backtest_historical.py:2347`, failure key `positive_equal_weight_long_active_pnl`, the GPT-review finding B fix). Both must be strictly positive to pass.
- **Two-family search-budget stop rule already routed here.** `PLAN.md`: two families max on the fixed L1 substrate before a substrate decision is forced; this experiment is the sanctioned substrate step, not a third family.

## Non-Authorization

Do not do any of the following from this packet alone:

- flip `config/agent_rules.json` or `config/risk_rules.json` gates,
- start paper edge-validation,
- start M8/live work,
- write to `artifacts/backtests/`,
- relax, scale, or re-index `paper_phase_criteria.py` or any artifact threshold floor,
- run a reviewed artifact loop or any credentialed pull/run before this packet is reviewed AND Robin gives a separate explicit go,
- add any `(interval, horizon)` cell beyond the predeclared grid, or spin up a fresh re-test of an in-grid horizon motivated by a non-deciding config's result (that recursive horizon-walking is exactly the substrate p-hacking the two-family budget rule exists to stop),
- copy raw licensed quote rows into docs or prompts.

## Research Decision

Hold the **strategy shape** (`relative_strength.long_only_proxy_v1`), the **universe** (M7c 10 names), the **interval** (`1m`), the **frozen feature/model block**, and the **pinned M7 criteria** constant, and vary **only the holding horizon** on the same L1 `bbo-1m` data, to test whether a longer hold restores a post-cost edge — while honestly expecting the per-trade realism caps to be the binding, largely-horizon-insensitive constraint (residual sensitivity confined to the exit-leg half-spread and the late-day-drop sample re-composition).

This is the cleanest isolation the harness allows: feature-window semantics, decision cadence, +1-bar entry latency, universe, and strategy shape are all held exactly constant, so a pass/fail on the decision-carrying config is attributable to **hold length alone**, and it is **runnable today** with config-only changes. Coarser decision bars are explicitly deferred (Evidence Grounding; Future Work).

This is a substrate experiment, not a new strategy family and not the phase-2 short side. A GO here is a **precondition test** for paper-phase entry under the existing M7c relative-strength family; it does not by itself authorize paper, M8, or any production write, and (given reduced sample power, below) is itself provisional pending a confirmation window.

## Strongest Antithesis

The rigorous case against the whole step, stated so it cannot be wished away: the M7c null's **binding** obstacle to a clean GO is the realism-cap blowout, and that metric is the per-trade round-trip touch-to-mid half-spread. Its **entry** leg is horizon-invariant, and its **exit** leg, while horizon-dependent through exit-bar re-timing, is not something a longer hold is *designed* to compress — a 2-hour hold still crosses the spread once in and once out. A longer hold changes turnover and signal-to-noise but is not a spread-reduction mechanism, so p95/max plausibly stay blown and a GO is impossible regardless of edge. Worse, the late-day `session_horizon_crosses_close` drops shrink and bias the sample, and the only axes that *mechanically* address the half-spread gap — passive/non-crossing execution, a tighter-spread/liquidity-screened universe, and entry-latency — are explicitly OUT of this step. Depth modeling (L2/MBP-10) does **not** clear a touch-to-mid gap; it adds impact on top of it.

**Response (conceded and built into the design):** P(GO) is low by construction and realism-cap-dominated; the residual GO mass lives only in the hedged exit-leg-spread / late-day-drop re-composition channel, which the Diagnostics section instruments and falsifies. The step is justified strictly as the cheapest edge-**existence** precondition gating the expensive next spend; the decision rule pre-commits a "realism caps still blown" outcome to **diagnostic routing, not to a GO**, and the unchanged realism caps remain hard gates on the single decision-carrying config, so the step **cannot manufacture a false GO**. A null here is informative: it tells us whether any post-cost edge exists at a longer hold before heavier substrate work is funded.

## Predeclared Parameter Grid

Two configs, interval `1m` only, both runnable today with config-only changes. **Holding horizon is named in minutes** and is distinct from bar width.

| id | interval | horizon | role | window | can trigger GO? |
|----|----------|---------|------|--------|-----------------|
| **C1** | `1m` | `60m` | DESCRIPTIVE — monotonicity diagnostic | snooped `2026-03-10 → 2026-04-08` only | **No** |
| **C2** | `1m` | `120m` | **PREFERRED — sole decision-carrying config** | fresh forward holdout | **Yes** |

- **C2 (`1m`/`120m`) — PREFERRED, the ONLY GO-eligible config.** The cleanest isolation of the holding horizon: the ONLY change vs the M7c `1m`/`30m` null is the hold (30m → 120m). A 2-hour hold gives genuine cross-sectional drift room to exceed round-trip friction, while no-overlap-per-symbol caps effective turnover (~2 non-overlapping holds/symbol/session: the ~270-minute eligible decision window / 121) → lower aggregate cost drag and fewer independent draws. Cheapest credentialed run; reuses the harness wholesale (config-only: `signal.horizons` + `model.coefficients['120m']` + `manifest.execution.horizon`). **Runs on the fresh forward holdout, which it consumes (single-use).**
- **C1 (`1m`/`60m`) — DESCRIPTIVE monotonicity diagnostic, zero decision authority.** Doubles the M7c null hold (30m → 60m) on identical 1m bars/feature-windows/entry-latency/universe/shape. Provides a cross-window read on whether edge moves monotonically with hold length. It has **no veto and no rescue power** over C2 (see decision rule). To keep the fresh holdout single-use for C2, **C1 runs only on the already-snooped `2026-03-10 → 2026-04-08` window**, where it can leak no fresh-window information because it cannot decide; the monotonicity read is therefore explicitly cross-window and descriptive only.

**Attestation (pin before the credentialed pull):** No `60m` or `120m` decision-bar configuration (nor any coarser-bar / `15m` rank) has been backtested on **any** window — including the snooped `2026-03-10 → 2026-04-08` clean window — prior to this predeclaration. The grid values were selected by ex-ante drift/cost-ratio reasoning only, never by observing in-sample results.

### Multiple-comparisons / family-wise decision rule (this is not substrate p-hacking)

1. **Exactly ONE config (C2) is decision-carrying, predeclared BEFORE any run.** The GO/NULL verdict rides solely on C2 on the fresh holdout. C1 is pre-registered descriptive and **cannot** trigger a GO under any outcome.
2. **This is not "take the best."** If C1 reads favorably while C2 fails, that is explicitly **NOT a GO** and does **NOT** license a fresh re-test of `60m` (or any in-grid horizon) on a new window. It is logged as a descriptive observation only; the stop rule governs (route strictly by C2's failing gate family).
3. **The genuine multiplicity control is "only C2 decides, on a single-use fresh holdout."** The pinned criteria are a conjunction of correlated gates (the edge metrics move together; the sample-count metrics move together), so their joint pass probability is **not** `(0.5)^n` and should not be sold as such. The real stringency comes from the **two realism caps** plus the **dual-benchmark active-PnL** requirement on a never-inspected window — not from counting correlated edge metrics.
4. **No post-hoc cell additions and no in-grid re-tests.** If C2 nulls, the stop rule FORBIDS adding `10m`/`30m`/other cells AND forbids re-running `60m`/`120m` on a new window. The grid is frozen and hash-bound before any credentialed pull.
5. **Run order is pinned.** C1's snooped window exists today and C1 may therefore run first — but **C2 runs and its verdict locks regardless of C1's outcome**. A favorable or unfavorable C1 read cannot inform WHETHER C2 runs, cannot delay it, and cannot re-scope it; C1 is archived as the descriptive monotonicity diagnostic either way.

## Predeclared Universe

**HOLD CONSTANT** — exactly the M7c 10-name universe in exact predeclared order, hash-bound in the manifest universe block and cross-referencing `m7c_relative_strength_market_neutral_v0_20260613` under the new `m7d_horizon_substrate_v0_20260626` hypothesis id:

```
AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AVGO, COST, NFLX
```

Justification: (a) the experiment's explicit purpose is to ISOLATE horizon — changing the universe simultaneously would confound the horizon signal with a universe-composition effect and forfeit clean attribution against the M7c null; (b) the sanctioned substrate step is "longer horizon on the same L1 data," explicitly NOT wider-universe-first per the two-family stop rule (a wider, liquidity-screened universe stays reserved as a SEPARATE downstream substrate branch); (c) reusing the identical 10 names preserves a clean A/B against the M7c `1m`/`30m` null. No symbol additions, removals, or liquidity re-screening. The M7c minimum-decision-set rule (≥ 8 of 10 symbols with valid features, two-sided quotes, market-state approval; < 8 → skip) and the concentration caps (no single symbol > 35% of gross legs or > 50% of net-positive PnL) carry over **unchanged**.

## Predeclared Windows

**Decision window = a FRESH, never-inspected forward holdout. Do NOT reuse `2026-03-10 → 2026-04-08` (or any already-inspected window) for the GO/NULL verdict OR for the onward routing/funding decision.**

- The M7c preferred window `2026-03-10 → 2026-04-08` is now **snooped** for this family — it produced the family-2 null, and its p95/max and per-symbol breadth have been inspected. The diagnostic windows `2026-04-09 → 2026-05-08` and `2026-05-11 → 2026-06-09` are likewise snooped. Any metric from these windows (including the C1 `60m` diagnostic and an optional C2 same-window replay) is **descriptive-only** and is barred from the GO/NULL verdict, the route-A/B/C decision, AND any onward substrate-funding (e.g. L2) decision.
- **C2 decision window:** predeclare and hash-bind **exactly the first 20 consecutive full-RTH sessions strictly after `2026-06-13`** (per the M7c fallback), session dates pinned from the market calendar via `pin_sessions_from_provider` (half-day/DST-aware) and hash-bound **before** the credentialed pull. The window length is fixed at exactly 20 sessions — no "≥ 20" end-date freedom, no window-shopping. C2 is the **only** config that touches this window, and running it **consumes** the window: no metric from it (pass or fail) may justify a fresh in-grid retest, add an unpredeclared cell, or auto-fund a future predeclaration. Its metrics may only classify the predeclared route (edge fail vs edge-pass/realism-fail vs full pass) and provide factual context for a separate Robin-mandated packet; any future run must use a brand-new, never-inspected window.
- **Timing caveat:** as of 2026-06-26 only ~12 sessions exist after the last inspected window, so the fixed first-20-session fresh holdout will not exist until ~mid/late-July 2026. The credentialed run is gated on BOTH window-completeness AND Robin's separate go.
- **Optional snooped replay (non-deciding for verdict AND routing):** a same-window C2 replay on `2026-03-10 → 2026-04-08` MAY be run **after** the verdict and routing are locked, purely as a descriptive same-substrate delta (longer hold vs the known `1m`/`30m` null on identical data) for mechanism diagnosis. It must be archived under `reports/` labeled "snooped — non-deciding for verdict AND routing" and can never contribute to GO/NULL, route A/B/C, or onward funding.

## Strategy Shape

**Unchanged: `relative_strength.long_only_proxy_v1`.** The pure cross-sectional ranking + proxy candidate construction + the multi-symbol same-timestamp decision harness (`run_historical_cross_sectional_backtest`) are reused exactly as built and reviewed in M7c (`b9a8756`):

- Rank the valid universe by the existing point-in-time relative-strength feature; **long top 2** with equal per-leg notional; close on the predeclared deterministic horizon; reuse the single-leg fill/exit machinery (`_simulate_historical_long_trade`) unchanged; no-overlap per symbol; both long legs aggregate into one `(strategy_id, rules_hash, data_pin)` v2 artifact.
- **No short side, no multi-leg basket atomicity, no locate/SSR.** Phase 2 of M7c remains conditional and out of scope here.
- **Feature set and model are frozen.** `signal.feature_windows` stay `[9, 21, 50]`; `rsi_period = 14`, `z_window = 21`, `vol_window = 21`, `prob_bins = 10`, `standardization = 'identity'`. For the new horizons, the existing hand-set `logit-mom-v1` coefficient vector is reused **verbatim** (no per-window fit, no interpolation, no retraining) so that the only thing that varies is hold length — never a tuned model. This is the central anti-overfitting commitment: no researcher degree of freedom is introduced into the coefficient block.

## Required Harness Changes

All config-only — runnable today; no resampler/clock/entry-latency code change (interval stays `1m`).

1. **Add `60m` and `120m` to `config/agent_rules.json` `signal.horizons`.** This changes `rules_hash` (intended and acceptable — new experiment). The `rules_hash` delta must be documented so it is NOT mistaken for an `artifact_mismatch`.
2. **Add `signal.model.coefficients['60m']` and `['120m']`**, each with all 8 keys (`intercept` + the 7 `FEATURE_NAMES`), **reusing the existing hand-set `logit-mom-v1` vector verbatim**. No per-window fit.
3. **Set `manifest.execution.horizon`** to the tested value (`60m` for C1, `120m` for C2); it is already hash-bound into `manifest_hash` and validated against `config.horizons` by the step-5 plumbing (`backtest_historical.py:1813-1815`). The full execution block is pinned numerically here so no execution numeric is operator-settable post hoc: **`latency_budget_ms = 250`** and **`slippage_cap_bps = "25"`** (the unchanged M7 quote-B conventions, identical to the M7c null).
4. **No resampler / clock / entry-latency change** is needed: at interval `1m`, `_entry_bar_end()`'s +1 minute already equals +1 bar.
5. **Confirm (no code change) the late-day drop behavior** and document the resulting morning/midday entry-selection bias: long-hold decisions crossing RTH close are dropped via `horizon_gate → GateFail('session_horizon_crosses_close') → continue`. Do NOT add a hold-to-close clamp or `session_end_buffer` in this packet (keeps it minimal). Inherited and likewise unchanged: a missing/invalid exit bar at decision+horizon drops that leg deterministically (it adds to under-sampling; it is not an operator degree of freedom). **Verify `trade_count ≥ 30` and `traded_session_count ≥ 5` actually hold on the fresh window before claiming a pass** — the 120m hold + no-overlap + late-day drops sharply reduce effective trade count (see Decision rule on reduced power).
6. **Build the cross-sectional input manifest** for the predeclared fresh 20-session forward holdout via the committed `historical_backfill.py` tool; mint `m7d_horizon_substrate_v0_20260626`, hash-bind the unchanged M7c 10-name universe block (cross-referencing the M7c hypothesis id). Staged-only; no production write.
7. **No change to `scripts/agent/paper_phase_criteria.py`** — all pinned criteria stay byte-for-byte unchanged. The `universe_equal_weight_long_v1` writer gate (`backtest_historical.py:2347`) must remain in force. Verify each metric remains computed at the longer horizon (the horizon only moves the exit bar; the `_gap_summary` p95 index simply runs over a smaller trade count). Keep the full offline suite (currently 1846 tests) green plus new TDD coverage for the horizon-config plumbing. Tests stay offline-only with spy/no-op brokers and zero network/credential reads.
8. **Committed review fixes the run inherits (2026-07-02, 1846 tests green at current HEAD):** (a) the cross-sectional runner now requires the DECISION bar to be FD-2-eligible at decision time — late-receipt bars are excluded from the ranked set as `decision_bar_*` exclusion counts, so the ranked set equals the tradable set (byte-identical on the fix-A baseline); (b) BOTH reviewed-artifact writers now fail closed unless `rules_hash` equals the config-DERIVED hash for the `agent_rules_path` they run — after the horizon config edit (item 1) the M7d writer invocation MUST therefore pass the NEW derived hash (the expected, documented `rules_hash` delta, not an `artifact_mismatch`); (c) `build_cross_sectional_input_manifest` now requires an explicit `horizon` (`120m` for C2, `60m` for C1 — the silent `30m` default is removed).
9. **Calendar-pinning prerequisite (operational, satisfied in current HEAD; still mandatory before the credentialed pull).** `ExchangeCalendarsScheduleProvider` is implemented with a lazy `exchange_calendars==4.13.2` version pin, and the committed cross-checked fixture `tests/fixtures/calendar/xnys_sessions_2026H2_v1.json` covers the fixed first-20-session holdout. Use either the pinned provider or the committed fixture, but `pin_sessions_from_provider` must pin the exact session map before any credentialed spend. The pinned sessions are hash-bound into the manifest either way; a mis-pinned window fails closed in the validator.
10. **Committed orchestration driver (operational, satisfied in current HEAD; still mandatory before the credentialed pull).** The reproducible `agent.m7_run_driver` now owns pull→pin→build→write sequencing at the orchestration level: pin sessions before credentialed spend, filter rows to pinned sessions, derive `rules_hash` from the agent rules, stage rows/manifest, re-read staged bytes, and write only staged artifacts.

## Benchmark Contracts

**BOTH benchmarks, unchanged from M7c phase 1.** Active PnL must be strictly positive vs BOTH simultaneously to pass.

- **`exposure_matched_midbar_v1`** (mandatory M7 benchmark; gated in `paper_phase_criteria.py`): the pinned exposure-matched mid-bar attribution. Same entry/exit bars, latency, fees, market-state, and CA-blackout as the strategy.
- **`universe_equal_weight_long_v1`** (family benchmark; gated fail-closed in the cross-sectional writer at `backtest_historical.py:2347`): buy an equal-notional basket of every valid symbol; same entry/exit bars, latency, fees, market-state, and CA-blackout as the proxy.

Because both benchmarks share the strategy's identical entry/exit-bar selection, the morning/midday entry-selection bias from late-day `horizon_crosses_close` drops is largely neutralized for the **active-PnL** gates (it is flagged only for the absolute `net_execution_realistic_pnl_usd` gate and for statistical power). No new benchmark is introduced; the phase-2 `universe_equal_weight_spread_v1` is out of scope (no short side here).

## Metrics And Acceptance Gates

The pinned M7 paper-phase criteria are reproduced and are **NOT** relaxed, scaled, or re-indexed by this packet. The GO conjunction is the **full pinned matrix** — enumerated explicitly so the zero-quality-breach gate is unambiguously inside it:

1. `session_count >= 20` (`min_sessions`)
2. `trade_count >= 30` (`min_trades`)
3. `traded_session_count >= 5` (`min_traded_sessions`)
4. `profit_factor >= 1.10`
5. `max_drawdown_pct_allocated <= 0.0150`
6. `worst_day_pct_allocated <= 0.0075`
7. `p95_realism_gap_bps <= 15`
8. `max_single_fill_divergence_bps <= 50`
9. `net_execution_realistic_pnl_usd > 0`
10. `active_pnl_usd > 0` vs **BOTH** `exposure_matched_midbar_v1` (gated in `paper_phase_criteria.py`) **AND** `universe_equal_weight_long_v1` (gated in the writer at `backtest_historical.py:2347`)
11. `avg_trade_bps > 0`
12. **Zero** quality-breach counts (all five): `unresolved_reconcile_drift_count`, `s1_canary_breach_count`, `live_broker_submit_count`, `artifact_mismatch_count`, `unhandled_exception_count`

Plus the M7c anti-cherry-pick breadth/concentration rule: **≥ 8 of 10 symbols valid/traded**, and **no single symbol > 35% of gross legs or > 50% of net-positive PnL**.

**Max-cap sample-size caveat (predeclared interpretation; NO threshold change):** `max_single_fill_divergence_bps` is a raw MAX order statistic and shrinks mechanically with sample size — C2's expected ~90–350 trades draw a far smaller tail than the baseline's 1147, so a `max ≤ 50` pass at C2's sample size is NOT, on its own, evidence of tighter execution. A C2 realism pass is credited only if `p95_realism_gap_bps` ALSO clears its cap; the p95 distance-to-cap and the per-trade gap distribution's **p99 are exported as diagnostics** alongside p95/max. The pinned thresholds themselves are unchanged, and there is no undefined extra "comfortable margin" gate.

### Decision rule (GO / NULL)

**GO iff** the single PREFERRED config **C2 (`interval = 1m`, `horizon = 120m`, +1-bar entry latency)**, run staged-only on the predeclared FRESH 20-session holdout, satisfies **the full pinned matrix above (no threshold relaxed) AND the breadth/concentration rule**, and the reviewed v2 artifact verifies `ok` under this exact rule.

- The two specifically-targeted binding gates — `p95_realism_gap_bps <= 15` AND `max_single_fill_divergence_bps <= 50` — must clear **in addition to** the edge gates. This packet does **not** assume they will.
- **Anything short of the full conjunction is NULL / NO-GO** for the preferred config. C1 is descriptive only: it can **neither** rescue a failing C2 **nor** weaken any gate.
- **Reduced statistical power — explicitly acknowledged.** The 120m hold + no-overlap + late-day drops cut trade count far below the M7c 1144-trade null; a conjunctive pass on a much smaller sample is weaker evidence and more likely to pass by chance. Therefore the run must report exact distance-to-floor/cap for `trade_count`, `traded_session_count`, the edge gates, and both realism caps, but those distances are diagnostics and do **not** create an operator-discretion margin gate. **Even a clean C2 GO is PROVISIONAL**: it does not immediately unlock paper but routes to a predeclared, separately-reviewed **confirmation holdout** (the next fixed 20 sessions) that must also pass before paper-phase entry.
- **GO-path lead time (acknowledged).** Even a confirmed GO cannot start paper promptly: the paper phase needs live realtime data (the Databento key is historical-only; the live subscription is not provisioned) and the live broker path still needs Robin's paper-account verifier step. A GO therefore triggers a provisioning step with real lead time, not an immediate paper start.
- **No production write** (`artifacts/backtests/` stays `.gitkeep`-only) until a reviewed artifact verifies `ok` under this rule AND Robin gives a separate go. A GO here is a precondition test only; it does not by itself authorize paper, M8, or a production artifact write.

## Diagnostics To Export On Failure

On any NULL, export (staged-only) to attribute the failure mode and inform onward routing — without ever relaxing a gate:

- **Per-gate pass/fail table** for C2 (and, descriptively, C1), with exact metric values vs each pinned threshold, so the EDGE-leg vs REALISM-leg split is unambiguous.
- **Realism decomposition (the falsification instrument for the exit-leg hedge):** the full per-trade `_realism_gap_bps` distribution (not just p95/max — include the **p99**, per the max-cap caveat), AND the separate `trade.entry_spread_bps` vs `trade.exit_spread_bps` distributions (`backtest_historical.py:1567-1568`, already emitted), split by symbol. The same decomposition was computed PRE-RUN on the snooped fix-A baseline (Evidence Grounding) — export C2's version **regardless of outcome** so the fresh-window read is directly comparable to the measured floor. Because the combined gap ≈ entry_half_spread + exit_half_spread, the entry-leg term tests the (horizon-invariant) latency/adverse-selection channel while the exit-leg term tests the (horizon-dependent) re-timing channel — i.e. whether the binding friction is a latency phenomenon (→ entry-latency probe) or a spread/depth phenomenon (→ tighter-universe / passive-execution / L2).
- **Horizon monotonicity (cross-window, descriptive):** C1 `60m` (snooped window) vs C2 `120m` (fresh window) on net PnL, active PnL vs both benchmarks, PF, and `avg_trade_bps`, to show whether edge moves monotonically with hold — flagged explicitly as a cross-window read, not a controlled comparison.
- **Sample audit:** `trade_count`, `traded_session_count`, count and timestamp distribution of `session_horizon_crosses_close` skips, and the morning/midday entry-time histogram, to flag under-sampling vs a genuine edge null.
- **Breadth/concentration:** per-symbol share of gross legs and of net-positive PnL, to confirm the null is broad (not a single-name artifact) — mirroring the M7c breadth report.
- **rules_hash / manifest_hash provenance** for each config, so the expected `rules_hash` delta (from the new horizon blocks) is documented and not mistaken for an `artifact_mismatch`.
- A short **failure-review note** under `reports/` (gitignored), maintaining the `.gitkeep`-only state of `artifacts/backtests/`.

## Stop Rules

The two-family search budget is already spent (momentum = family 1 nulled; relative-strength = family 2 nulled). M7d is the sanctioned substrate step, **NOT** a third family and **NOT** a license to keep walking the horizon. If C2 nulls on the fresh holdout, route by **WHICH gate family fails on C2 (fresh window only)** — snooped/in-sample metrics inform nothing here:

- **(A) EDGE gates fail on C2** (`profit_factor < 1.10`, OR non-positive `net_execution_realistic_pnl_usd`, OR non-positive `active_pnl_usd` vs either benchmark, OR `avg_trade_bps <= 0`): a longer hold does not rescue the edge → **documented STOP of the L1 1-minute cross-sectional relative-strength program.** Do NOT open a third same-substrate family, do NOT re-test `60m`/`120m` on a new window, and do NOT use wider-universe as a rescue. If the diagnostics show the realism caps clearing while only the edge fails — i.e. the mega-cap set may simply lack cross-sectional dispersion — a wider liquidity-screened universe becomes the lighter-build candidate for a SEPARATE predeclared packet, never an auto-spend.
- **(B) EDGE gates PASS but the REALISM caps still fail** (`p95_realism_gap_bps > 15` OR `max_single_fill_divergence_bps > 50`): the binding constraint is per-trade execution friction = round-trip half-spread, which a longer hold is not designed to compress. This is a **DIAGNOSTIC PARTIAL — not a GO, not a stop, and not an automatic next spend.** The realism decomposition may classify the likely mechanism for Robin's routing decision: because the modeled gap is **size-independent touch-to-mid**, depth/MBP-10 modeling alone does NOT clear it — so the causally-matched candidates are **passive/limit (non-crossing) execution** that captures rather than pays the half-spread, a **tighter-spread / liquidity-screened universe**, and **entry-latency** (if the entry-leg term dominates). L2/MBP-10 is relevant only insofar as it enables passive/queue-aware fills; framed as **plausible-not-proven**, it is one candidate among these lighter levers. The second substrate slot is consumed only if Robin separately chooses one of these candidates under its OWN predeclared packet + review + separate go.
- **(C) Both gate families pass AND breadth holds** → **provisional GO** per the decision rule, with margin diagnostics exported, routing to the confirmation holdout before any paper step.

**Explicitly NOT permitted on a null here:** adding a 4th `(interval, horizon)` cell (`10m`/`30m`/etc.); a fresh re-test of any in-grid horizon (`60m`/`120m`) motivated by a non-deciding C1 result; a third same-substrate strategy family; the phase-2 short-side build; any threshold relaxation; or any production write. Each onward lever (passive execution, tighter universe, entry-latency, L2/MBP-10, coarser bars) is its OWN predeclared packet + review + separate Robin go; M7d's null does NOT auto-authorize the next run. The sequencing is the point: the cheap horizon probe establishes whether ANY post-cost edge exists at a longer hold BEFORE heavier substrate work is funded.

**Substrate-search budget (pinned in `PLAN.md`, 2026-07-02):** M7d is substrate experiment **1 of at most 2** on this family line. A route-B lever, if Robin separately selects one after reviewing the decomposition, consumes the second and final slot (its own packet + review + separate go). A second substrate null → **documented program STOP**; any continuation — including a daily-horizon/EOD line, which is a NEW substrate — requires a fresh explicit mandate from Robin, not a routing rule.

## Future Work (explicitly deferred, NOT part of this experiment)

- **Coarser DECISION bars (e.g. `15m`).** Deferred to a separate predeclaration because (a) interval ≠ `1m` requires real un-hardcoding (`bar_series.py:114-115`, the manifest validators `:414-415`/`:526-527`, `signal_config.py:117-119`, the bucket-end and `_entry_bar_end` plumbing, plus the `MissingBar` interval at `bar_series.py:263` and the `signal_snapshot.py:114` minutes-as-intervals coupling); (b) coarsening rescales the frozen `[9,21,50]`-bar feature windows in wall-clock time (135/315/750 min at 15m), altering strategy shape and confounding the horizon read; (c) the realism/pricing model was calibrated for 1m books and would need its own predeclared re-validation at the coarser cadence. That future packet must also pin the invariant that **horizon minutes is an integer multiple of the interval minutes** (else `exit_bar_end` lands off the bucket grid and legs silently skip).
- **Passive/limit execution, tighter-spread universe, entry-latency, L2/MBP-10** — the realism-cap-matched levers above, each its own predeclared packet if and when routing §B selects it.

## Verification Before Handoff

Before this packet is acted on (and again before any future code/run loop), verify the expected clean state:

- The repo is on **`main`** at the commit introducing the current packet rev (rev 1 = `013f9b6`; the M7 stack was fast-forward-merged into `main` 2026-06-26 at `19786cf`, docs-synced `0e7d136`). **State explicitly whether M7d runs on `main` or on a fresh working branch off `main`** (recommended: a fresh branch off `main`), and reconcile any "nothing merged to main" staleness in `CLAUDE.md` / memory before starting.
- `git status` is clean; no uncommitted edits to `paper_phase_criteria.py`, the pinned criteria constants, the `verify_artifact` OPTIONAL provenance allow-set, or any config gate. Confirm those are byte-for-byte unchanged on HEAD.
- `artifacts/backtests/` contains **only** `.gitkeep` (no reviewed artifact verifies `ok`).
- `config/agent_rules.json` and `config/risk_rules.json` run-gates remain `false` (`agent_rules.enabled`, `paper_trading.enabled`, `live_trading.enabled`); the short-side disablement remains in force.
- This packet is GPT-reviewed rev 3, but Robin has explicitly chosen to run M7d rather than route straight to a realism-matched lever, Robin has given a separate go, and the fresh fixed-20-session post-`2026-06-13` holdout is complete and calendar-pinned — all three — before any harness change, manifest build, credentialed pull, or run.
- Any future code loop runs the full offline suite (≥ 1846 tests) green, adds TDD coverage for the new horizon-config plumbing, makes no network calls or credential reads in tests, and writes nothing to `artifacts/backtests/` until a reviewed artifact verifies `ok` under the decision rule above.
