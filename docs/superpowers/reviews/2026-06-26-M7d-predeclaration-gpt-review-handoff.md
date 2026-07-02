# M7d predeclaration GPT review handoff (2026-06-26)

**Purpose.** A GPT adversarial review was requested on a **predeclaration packet** (a pre-registered
experiment design), NOT on code or a run/verdict. The packet —
`docs/superpowers/specs/2026-06-26-M7d-longer-horizon-research-packet.md` — predeclares the sanctioned
**substrate** step after the two-family STOP: a **longer holding horizon** on the same L1 `EQUS.MINI:bbo-1m`
data, within the existing M7c relative-strength family. This document lets a FRESH context act on GPT's
findings without re-deriving state. The exact rev-2 prompt sent to GPT is embedded verbatim in the appendix.

> Robin routes reviews through GPT. The most consequential question for a *predeclaration* is methodological:
> is the experiment honestly falsifiable, free of data-snooping and multiple-comparisons escape hatches, and
> are its causal claims and harness-fit claims correct against the actual code — BEFORE any new PnL is computed
> or any credentialed dollar is spent. A predeclaration's whole value is that it pre-commits the verdict and the
> routing so results cannot be cherry-picked.

## Review outcome (2026-07-02)

GPT review returned **CHANGES-REQUIRED** on two methodology issues and an architect **WATCH / RECONSIDER**
on the experiment sequencing:

- The packet used undefined "comfortable margin" language as if it were an extra GO gate. Rev 3 removes that
  discretionary gate: GO/NULL is the pinned matrix + breadth only; margin distances are exported as diagnostics,
  and any clean pass remains provisional pending confirmation.
- Route B could be read as auto-selecting the second substrate lever after seeing C2 diagnostics. Rev 3 makes the
  decomposition mechanism-classification only: the second substrate slot is consumed only if Robin separately
  selects a lever under its own packet + review + separate go.
- The rev-2 handoff/packet had stale current-state claims. Current HEAD has 1846 tests green, the
  `ExchangeCalendarsScheduleProvider` + committed session fixture, and `agent.m7_run_driver` built; no credentialed
  run or production artifact is authorized.

The remaining strategic WATCH is explicit: Robin must still choose whether to run M7d at all versus route straight
to a realism-matched lever. This packet remains non-authorizing until that choice, Robin's separate go, and the
fresh fixed holdout are all present.

## What this packet is (and is NOT)

- It is a GPT-reviewed pre-registered design. **No credentialed pull/run is authorized.** A run is gated on four
  things, ALL required: (1) Robin explicitly chooses M7d rather than routing straight to a realism-matched lever;
  (2) Robin gives a separate go; (3) the predeclared **fresh fixed-20-session post-2026-06-13 holdout** exists;
  (4) the run uses the current rev-3 packet and current HEAD prerequisites.
- It does NOT relax any pinned criterion, flip any gate, or authorize paper/M8/production writes.
- Scope = **longer HOLD only**, interval `1m`, config-only (runnable today). Coarser DECISION bars are
  explicitly **deferred** (the resampler hard-rejects non-`1m`, coarsening confounds the frozen feature
  windows, and the realism model is uncalibrated at coarser cadence).
- **Rev 2 (2026-07-02):** a full-project review (4 read-only deep-review agents + an own-run reproduction)
  hardened the packet before this GPT review. It now carries: a MEASURED pre-run feasibility read (the
  horizon-invariant entry-leg realism floor — p95 ≈ 14.1 bps in the 120m-survivor population = 94% of the
  15-bps cap; survivor combined gap p95 ≈ 31.7), the fix-A-contract baseline C2 must be compared against
  (**1147 trades / net −$858.01 / active −$111.51 / p95 29.949 / max 97.484** — the staged pre-fix quotes no
  longer validate at HEAD), a max-cap order-statistic caveat, pinned latency/slippage numerics, a pinned C1/C2
  run order, a GO-path lead-time acknowledgement, operational prerequisites (calendar fixture, committed
  driver), and a `PLAN.md` substrate-search budget (M7d = substrate experiment 1 of at most 2). Three general
  harness-correctness fixes are committed (cross-sectional decision-bar FD-2 eligibility; both writers bind
  `rules_hash` to the config-derived hash; builder `horizon` required-explicit) — all byte-identical on the
  baseline. Rev 3 then applied the GPT review fixes above; current verification is 1846 tests green.

## Repo state (as of this handoff)

- Branch: **`main`** (the M7 stack was fast-forward-merged 2026-06-26). HEAD = the commit that introduces the
  current packet rev (rev 1 = `013f9b6`; rev 2 = the 2026-07-02 review/fix commit; `git log -1`). Prior context:
  `0e7d136` (docs-sync: merge-to-main DONE) on top of `19786cf` (M7 tip; the 4 GPT-review fixes from the M7c
  null review). **`main` was at M2 (`a82be6d`) before the 2026-06-26 FF merge.**
- Offline suite: **1846 tests green** — `python3 -m unittest discover -s tests -p 'test_*.py' -t .`
  (the `-t .` is required; modules import as `agent.*` / `recorder.*` from `scripts/`).
- Run gates committed `false`; `artifacts/backtests/` holds only `.gitkeep`; no production artifact written.
  `.secrets/databento.json` = historical-only Databento key.

## How this packet was authored (so GPT can attack the process, not just the artifact)

The packet was produced by a 13-agent design+critique workflow: 4 read-only grounding readers → a 3-architect
panel (edge-first / realism-cap-first / discipline-anti-overfit) → a synthesis draft → a 5-lens adversarial
critique (anti-lookahead, multiple-comparisons, harness-fit, criteria-consistency, realism-causality). The
critique returned **2 blockers + 8 majors**, all applied before this handoff. The two most consequential, both
re-verified against code by the orchestrator, were:

1. **A FALSE causal claim (realism-causality blocker).** The draft asserted the realism gap is "structurally
   near-independent of holding horizon." This is wrong. The modeled fills are `entry_fill = entry.ask`,
   `exit_fill = exit_bar.bid` (`backtest_historical.py:1544-1545`), so the per-trade gap reduces algebraically to
   `entry_half_spread_bps + exit_half_spread_bps` (`_realism_gap_bps`, `backtest_historical.py:1573-1587`), and
   the **exit** leg is read from `exit_bar = decision + horizon` — i.e. **horizon-dependent**. It was the mirror
   image of the "substrate proven" overclaim GPT flagged in the M7c review. Corrected to an honest,
   symmetrically-hedged claim; the entry-vs-exit-leg realism decomposition is the predeclared falsification
   instrument.
2. **A multiple-comparisons escape hatch (blocker).** The family-wise rule and the stop rule contradicted: a
   non-deciding config's favorable read could license a fresh re-test of an in-grid horizon on a new window —
   exactly the recursive horizon-walking the two-family budget rule forbids. Closed: routing is strictly by C2's
   failing gate family on the fresh window; in-grid re-tests are explicitly prohibited.

Other applied majors: dropped the `15m` coarser-bar config entirely (non-minimal, primes a `15m` p-hack, needs
real un-hardcoding) → deferred; restricted the scarce fresh holdout to C2 alone (C1 runs on the snooped window);
struck a false "independent gates / stronger than p<0.05" stringency argument; corrected "L2 mechanically fixes
realism" (the modeled fill is size-independent touch-to-mid, so depth modeling enlarges impact rather than
clearing the half-spread) → routing now names passive/limit execution + tighter-spread universe + entry-latency
as the causally-matched levers; acknowledged reduced statistical power (a clean C2 GO is provisional pending a
confirmation holdout before paper). GPT should treat all of these as **claims to re-attack**, not settled.

**Rev 2 (2026-07-02) additions to attack:** the packet now contains a measured pre-run feasibility section
computed from the snooped fix-A baseline (entry-leg floor / late-day-drop re-composition), a max-cap
sample-size caveat, and a substrate-search budget. These were produced by the project's own review loop —
attack them too: is the 120m-survivor subset of 30m trades a fair proxy for C2's entry population; is
"measured on snooped data" itself free of design-contamination risk; is the p99-diagnostic + p95-margin
interpretation of the max cap sound; and is a 2-substrate budget the right K?

## The experiment in brief (what GPT is reviewing)

- **Strategy shape unchanged:** `relative_strength.long_only_proxy_v1` (rank → top-2 equal-notional long,
  no-overlap per symbol, both legs under one v2 artifact). Frozen feature/model block; `logit-mom-v1`
  coefficients reused **verbatim** for the new horizons (no per-window fit).
- **Universe held constant:** the M7c 10 names (`AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AVGO, COST, NFLX`),
  hash-bound, to isolate the horizon variable.
- **Grid (interval `1m`, config-only):** **C2 = `1m`/`120m`** is the SOLE decision-carrying config, run on the
  fresh holdout (single-use); **C1 = `1m`/`60m`** is a descriptive monotonicity diagnostic only, run on the
  snooped `2026-03-10 → 2026-04-08` window (zero decision authority).
- **Pinned M7 criteria UNCHANGED** (`paper_phase_criteria.py`): ≥20 sessions / ≥30 trades / ≥5 traded sessions,
  PF ≥ 1.10, max-drawdown ≤ 1.50%, worst-day ≤ 0.75%, p95 realism gap ≤ 15, max single-fill divergence ≤ 50,
  positive net PnL, positive active PnL vs **both** benchmarks (`exposure_matched_midbar_v1` in
  `paper_phase_criteria.py`; `universe_equal_weight_long_v1` in the writer at `backtest_historical.py:2347`),
  positive avg-trade-bps, zero quality-breach counts; plus the breadth/concentration rule.
- **Decision rule:** GO iff C2 clears the full pinned matrix + breadth on the fresh holdout. Margin-to-floor/cap is
  exported as a diagnostic, not an extra undefined gate; even a clean pass is provisional pending a confirmation
  holdout. Anything short = NULL.
- **Stop routing on a C2 null:** route by which gate family fails — (A) edge-fail → documented STOP of the L1
  1-min cross-sectional RS program; (B) edge-pass-but-realism-fail → DIAGNOSTIC PARTIAL routing the *next*
  separately-predeclared substrate spend only if Robin separately selects it (passive execution / tighter universe /
  entry-latency / L2), never an auto-GO or auto-spend; (C) both pass → provisional GO. No 4th cell, no in-grid
  re-test, no third family — all forbidden.

## Files / code the packet depends on (GPT should verify the harness-fit claims)

- `docs/superpowers/specs/2026-06-26-M7d-longer-horizon-research-packet.md` — the artifact under review.
- `scripts/agent/backtest_historical.py` — `_realism_gap_bps`/`_gap_summary` (1573-1587), the fills (1544-1546),
  `_entry_bar_end` +1m (740-741), the horizon validation (1813-1815), the equal-weight writer gate (2347), the
  interval validators (414-415, 526-527), plus the 2026-07-02 fixes: decision-bar FD-2 eligibility in the
  cross-sectional loop (~1859-1871) and `_require_config_derived_rules_hash` (~636-646), called by both writers.
- `scripts/agent/signal_snapshot.py` — `horizon_gate` + `session_horizon_crosses_close` (160-178).
- `scripts/agent/signal_config.py` — horizons/coefficients validation; the `1m`-only interval freeze (117-119).
- `scripts/agent/bar_series.py` — `resample_midbars` non-`1m` hard-reject (114-115).
- `scripts/agent/paper_phase_criteria.py` — pinned floors (single-benchmark active gate at 173-178).
- Predecessor: `docs/superpowers/specs/2026-06-13-M7c-relative-strength-research-packet.md`; null:
  `reports/m7_historical_runs/2026-03-10-clean-rs-v1/summary.json` (git-ignored, reproducible).

## Acting on GPT's findings (protocol for the fresh context)

1. **Triage:** classify each finding real vs misread (verify against the actual code/packet text; separate
   FACT / ASSUMPTION / OPINION). This is a DESIGN review — most findings will be about methodology (snooping,
   multiplicity, causal honesty, decision-rule loopholes) or harness-fit accuracy, not runtime bugs.
2. **The pivotal contingency — "this experiment is not worth running."** If GPT argues convincingly that the
   honest P(GO) is so realism-cap-dominated that the cheap horizon probe cannot inform the next decision, OR
   that a realism-matched lever (passive execution / tighter universe / entry-latency) should be predeclared
   *instead*, surface that to Robin as a routing question BEFORE committing to the run — it may redirect the
   substrate sequence.
3. **Methodology blockers** (a surviving data-snooping path, a multiple-comparisons escape hatch, a gate
   silently weakened, a causal claim still overstated in either direction) are must-fix before the packet is
   marked reviewed.
4. **Harness-fit errors:** if any code citation in the packet is wrong (line, mechanism, or a claimed
   "config-only" change that actually needs code), fix the packet to match the code; re-verify against HEAD.
5. **Fix discipline:** revise the packet text only (this is predeclaration — there is nothing to build or run).
   Do NOT flip run gates; do NOT write a production artifact; no orders; no credentialed pull. Keep authoring
   and review as separate passes.
6. **Process guardrails (learned the hard way):** dispatched build/fix subagents must NOT run git mutations;
   review/verify subagents tend to edit files despite READ-ONLY prompts — git-audit the tree after any agent
   run, revert non-authored edits, re-derive against pristine HEAD.
7. **If GPT findings are fixed and no methodology blocker remains:** keep the packet marked GPT-reviewed, then it
   waits on Robin's route decision, Robin's separate go, and the fresh holdout before any code/run loop.

## Open decisions (unchanged by this review until findings land)

- **Run the M7d horizon probe at all** vs route straight to a realism-matched lever — Robin's call, informed by
  this review (the packet is honest that P(GO) is low and realism-cap-dominated).
- **Branch for any future M7d code loop:** a fresh branch off `main` is recommended (the packet says to state
  this explicitly before starting).

---

## Appendix — the exact prompt sent to GPT

````
You are a senior quant + experiment-design reviewer performing an ADVERSARIAL, evidence-based review of a PREDECLARATION (a pre-registered experiment design) for a real autonomous US-equities trading agent. You have full read access to the repository and its history. Read the actual packet AND the code it cites before making any claim — do not review from these notes alone; they only orient you. Cite `file:line` for every finding. Separate FACTS (verifiable in the code/packet) from ASSUMPTIONS from OPINIONS. This is a READ-ONLY review: propose changes, do not make them.

## Why this review matters (stakes)

This system will eventually gate real capital. The artifact under review is NOT code or a run — it is a **predeclaration packet** that pre-commits, BEFORE any new PnL is computed, the experiment design, the universe, the windows, the GO/NULL decision rule, and the routing-on-null for the next "substrate" experiment. Its entire purpose is to make the result un-cherry-pickable. A predeclaration with a data-snooping path, a multiple-comparisons escape hatch, a silently-weakened gate, or an incorrect causal/harness claim would let a future null (or GO) be rationalized after the fact — which is exactly the failure mode the project's two-family search-budget stop rule exists to prevent. **Your job is to try to break the design's integrity and its claims, hard.**

## Context (what the program is and where it is)

- Autonomous, **paper-first, fail-closed** US-equities agent. Hard posture: NO real-money orders; run gates (`config/agent_rules.json: enabled`, `paper_trading.enabled`; `config/risk_rules.json: live_trading.enabled`) are committed `false`; live needs two-key arming. Tests make no network calls / no credential reads. Authoritative design: `docs/superpowers/specs/2026-06-08-stocks-agent-design.md`; project state + safety rules: `CLAUDE.md`, `PLAN.md`.
- History: two predeclared strategy families on the L1 1-minute BBO substrate have nulled — momentum (family 1) and relative-strength (family 2; the M7c phase-1 long-only proxy nulled broadly on a clean window: net −$839.68, PF 0.55, active −$120.65 vs the pinned exposure-matched benchmark, AND realism caps blown p95 29.8>15 / max 97.5>50). The predeclared two-family stop rule then forces a **substrate decision** rather than a third same-substrate family. The chosen (cheapest) substrate axis is a **longer holding horizon on the same L1 data**.
- Repo is on branch `main` (the M7 stack was just fast-forward-merged); offline suite 1777 tests green via `python3 -m unittest discover -s tests -p 'test_*.py' -t .`.

## The artifact to review

`docs/superpowers/specs/2026-06-26-M7d-longer-horizon-research-packet.md` — read it in full. It predeclares: strategy shape unchanged (`relative_strength.long_only_proxy_v1`, top-2 equal-notional long); universe held constant (the M7c 10 mega-caps, hash-bound, to isolate horizon); a 2-cell grid at interval `1m` (C2 = `1m`/`120m` is the SOLE decision-carrying config on a FRESH fixed-20-session post-2026-06-13 holdout; C1 = `1m`/`60m` is a descriptive monotonicity diagnostic on the already-snooped `2026-03-10 → 2026-04-08` window, zero decision authority); the pinned M7 criteria reproduced UNCHANGED; a GO/NULL decision rule; and a gate-family-based routing-on-null. Coarser decision bars are explicitly deferred.

This packet was already hardened by an internal multi-agent design+critique pass that found and fixed 2 blockers + 8 majors. The two most consequential, which you should RE-ATTACK rather than trust: (1) an earlier draft wrongly claimed the realism gap is "horizon-invariant" — corrected because the per-trade gap ≈ entry_half_spread + exit_half_spread and the exit leg is read from `exit_bar = decision + horizon` (horizon-dependent); (2) a contradiction between the family-wise rule and the stop rule that opened a recursive in-grid horizon-retest loophole — closed. Other applied fixes: dropped a `15m` coarser-bar cell (deferred), restricted the fresh holdout to C2 only, struck a false "independent gates" stringency argument, corrected a "L2 mechanically fixes realism" claim (the modeled fill is size-independent touch-to-mid), and made a clean GO provisional pending a confirmation holdout. Verify each fix actually holds in the current text.

## Code to verify the packet's claims against (read these; do not trust the packet's citations)

- `scripts/agent/backtest_historical.py` — the modeled fills `entry_fill = entry.ask` / `exit_fill = exit_bar.bid` (~1544-1546); `_realism_gap_bps` / `_gap_summary` (~1573-1587); `_entry_bar_end` hardcoded +1 minute (~740-741); horizon membership validation (~1813-1815); the `universe_equal_weight_long_v1` writer gate (~2347); the interval validators (~414-415, ~526-527); the 2026-07-02 fixes (decision-bar FD-2 eligibility in the cross-sectional loop ~1859-1871; `_require_config_derived_rules_hash` ~636-646 called by both writers).
- `scripts/agent/signal_snapshot.py` — `horizon_gate` and the `session_horizon_crosses_close` drop (~160-178).
- `scripts/agent/signal_config.py` — horizons/coefficients validation; the interval `1m`-only freeze (~117-119); `rules_hash` over the whole config.
- `scripts/agent/bar_series.py` — `resample_midbars` non-`1m` hard-reject (~114-115).
- `scripts/agent/paper_phase_criteria.py` — pinned floors; confirm the active-PnL gate there covers ONLY the exposure-matched benchmark (~173-178) and the second benchmark is gated in the writer.

## Review dimensions (be exhaustive; hunt, don't skim)

**A. Data-snooping / anti-lookahead (highest priority).**
- Is the fresh-holdout discipline airtight? The packet bars the snooped windows (2026-03-10→04-08, 04-09→05-08, 05-11→06-09) from the verdict AND the routing/funding decision, fixes the fresh window at exactly the first 20 sessions strictly after 2026-06-13, and makes C2 the only config to touch it (single-use). Find any residual path by which already-seen data, or the C1 snooped-window read, can leak into the GO/NULL verdict or the route-A/B/C decision. Is the "no in-grid horizon was backtested before predeclaration" attestation sufficient, or is `120m` still a sn-oopable researcher choice?

**B. Multiple comparisons / search-budget integrity.**
- Is the grid genuinely minimal and the family-wise rule a true "only C2 decides," with no "best-of" or recursive re-test escape hatch? Does the stop rule's "Explicitly NOT permitted" list actually foreclose every way to keep walking the horizon or spin up a 3rd same-substrate attempt? Is the deferral of `15m` clean, or does it pre-stage a future p-hack?

**C. Causal honesty (both directions).**
- The packet must not claim a longer horizon WILL fix the edge, nor that it CANNOT move the realism caps. Verify the realism-gap derivation (gap ≈ entry+exit half-spread; entry horizon-invariant, exit horizon-dependent) is correct and the hedging is symmetric. Attack the routing-B mechanism claim: is it true that depth/MBP-10 alone does NOT reduce a size-independent touch-to-mid gap (so passive/limit execution + tighter spreads are the causally-matched levers)? Is the stated "P(GO) low, realism-cap-dominated" framing internally consistent with the residual-mass channel the packet names?

**D. Harness-fit accuracy.**
- Are the "config-only, runnable today" claims for C1/C2 correct (interval stays `1m`; only `signal.horizons` + a verbatim coefficient block + `manifest.execution.horizon` change; +1-minute entry latency == +1 bar at `1m`)? Are the deferred coarser-bar change-list and the "resampler hard-rejects non-`1m`" claim accurate? Does a 120m hold + no-overlap + late-day `session_horizon_crosses_close` drops plausibly still clear `min_trades=30` / `min_traded_sessions=5` on 20 sessions — or is the experiment likely under-powered to the point of being uninformative?

**E. Criteria integrity.**
- Confirm NO pinned criterion is relaxed, scaled, or re-indexed by the packet, that the dual-benchmark requirement is correctly sourced (one gate in `paper_phase_criteria.py`, one in the writer), and that the zero-quality-breach gate is unambiguously inside the GO conjunction.

**F. Is this the right next experiment at all?**
- Given the honest framing, is the cheap horizon probe worth running before a realism-matched lever, or should the program predeclare passive-execution / tighter-universe / entry-latency instead? Is the gate-family routing the correct way to spend the next substrate dollar?

**G. Rev-2 additions (2026-07-02) — attack these specifically.**
- The MEASURED pre-run feasibility: from the snooped fix-A baseline the packet claims the horizon-invariant
  entry-leg realism floor is p95 ≈ 14.1 bps in the 120m-survivor population (94% of the 15 cap; survivor
  combined gap p95 ≈ 31.7; max(entry_half) 85.8 > 50) and concludes a C2 GO is structurally improbable on
  realism, so the run's value is the route-A-vs-B edge read. Verify the derivation and the subset construction
  (is the 120m-survivor subset of 30m trades a fair proxy for C2's entry population?); attack whether measuring
  on snooped data contaminates the design.
- The max-cap sample-size caveat: the raw max order statistic loosens mechanically at C2's ~90–350 trades vs
  the baseline's 1147; the packet answers with "p95 must also clear with margin" + a p99 diagnostic, with NO
  threshold change. Is that interpretation sound, or should the gate itself be re-specified BEFORE the run?
- The substrate-search budget (`PLAN.md`): at most 2 substrate experiments on this family line (M7d = 1 of 2),
  then a documented program STOP absent a fresh mandate. Is K=2 defensible and stated tightly enough to prevent
  substrate-level p-hacking?
- Given the measured floor, the pivotal contingency sharpens: should the horizon probe run at all, or should a
  realism-matched lever (passive/limit execution, tighter-spread universe, entry-latency) be predeclared
  INSTEAD? Answer explicitly.

## Output format

For each finding: `[SEVERITY: blocker|high|medium|low] [DIMENSION A–G] title — packet section and/or file:line — evidence (what the packet/code says) — why it's a real problem for a predeclaration — concrete fix`. Then close with an explicit verdict on each dimension and an overall: **APPROVE (mark reviewed) / CHANGES-REQUIRED (list blockers) / RECONSIDER-EXPERIMENT (route elsewhere first)**. Be specific and conservative: only real, actionable findings grounded in the actual code/packet. If a concern is already correctly mitigated by the applied fixes, don't re-raise it. If the design is sound, say so plainly — a clean bill of health is a valid result, but only after a genuine adversarial attempt to break it.
````
