# M7c GPT review handoff (2026-06-26)

**Purpose.** A GPT adversarial review was requested on (1) the M7c phase-1 clean-window
**NULL** verdict, (2) the new M7c code, (3) the STOP decision, and (4) merge-to-main
readiness. This document lets a FRESH context act on GPT's findings without re-deriving
state. The exact prompt sent to GPT is embedded verbatim in the appendix. When the review
comes back, follow the "Acting on GPT's findings" protocol below.

> Robin routes reviews through GPT. This is a step-back review after the two-family STOP;
> the most consequential question is whether the NULL is real or a pipeline/measurement
> artifact (a false null would have wrongly triggered the STOP and killed a viable family).

## Repo state (as of this handoff)

- Branch: `codex/m7-backtest-gate`. HEAD = `214850c`. **47 commits ahead of `main`;
  nothing merged to main.** Stack: `m2-market-state → m3-signal → m6-reconcile →
  codex/m7-backtest-gate`.
- Offline suite: **1762 tests green** — `python3 -m unittest discover -s tests -p 'test_*.py' -t .`
  (the `-t .` is required; modules import as `agent.*` / `recorder.*` from `scripts/`).
- Run gates committed `false`; `artifacts/backtests/` holds only `.gitkeep`; no production
  artifact written. `.secrets/databento.json` = historical-only Databento key (works;
  full-window cost of the run below was ≈ $0.03).

### M7c commit list (review range `b637287..HEAD`)

```
214850c docs(m7c): record Robin's explicit STOP on the autonomous edge search
575b18a docs(m7c): record clean-window NULL / NO-GO -> substrate decision triggered
dd2f2bc fix(m7c): drop bbo-1m records with UNDEF matching-engine timestamp in live adapter
983f209 feat(m7c): wire credentialed bbo-1m live pull (verified against live BBOMsg) + RTH filter
5e1b0d3 docs(m7c): sync state to historical-backfill builder built + window/tier-2b plan
e45c2c0 feat(m7c): historical backfill + cross-sectional input-manifest builder (offline-complete)
98e88c8 docs(m7c): sync CLAUDE.md/PLAN.md to step-5 artifact plumbing built+committed
b9a8756 feat(m7c): step-5 multi-symbol manifest + cross-sectional artifact writer + CLI
869e20c feat(m7c): wire phase-1 multi-symbol cross-sectional backtest harness
fbadeac docs(m7c): sync CLAUDE.md/PLAN.md state to phase-1 proxy build
3a1a7f2 feat(m7c): phase-1 long-only relative-strength proxy (TDD, 1702 tests green)
b637287 docs(m7c): revise relative-strength packet to proxy-first (rev 2)
```

### Files under review

- `scripts/agent/strategies/relative_strength.py` — phase-1 proxy (rank → top-2 long).
- `scripts/agent/backtest_historical.py` — `run_historical_cross_sectional_backtest`,
  `validate_historical_cross_sectional_manifest`, `write_m7_historical_cross_sectional_artifact`,
  shared single-symbol path + extracted helpers.
- `scripts/agent/historical_backfill.py` — normalize / manifest builder / RTH filter /
  live pull seam (`_live_quote_event_source`, `_dbn_bbo1m_record_to_event_dict`, `_dbn_price`,
  `_ns_to_iso_utc`).
- `scripts/agent/backtest_metrics.py` — `build_v2_artifact_payload` + pinned-criteria `pass`.
- `scripts/agent/backtest_gate.py` — `verify_artifact` (S9 gate) + `_V2_PROVENANCE_OPTIONAL_KEYS`
  change.
- `scripts/agent/paper_phase_criteria.py` — pinned floors.
- `scripts/recorder/event.py` — DBN `parse` / `QuoteEvent` / quantization (int 1e-9).
- Tests: `tests/agent/test_m7c_{cross_sectional_runner,cross_sectional_artifact,historical_backfill}.py`,
  `test_relative_strength_proxy_m7c.py`, `tests/agent/test_m7_historical_artifact.py`.
- Predeclared: `docs/superpowers/specs/2026-06-13-M7c-relative-strength-research-packet.md`,
  `docs/superpowers/specs/2026-06-13-M7c-phase1-proxy-contract.md`.

## The run + verdict under review

Credentialed Databento `bbo-1m` pull, packet's preferred CLEAN window **2026-03-10 → 2026-04-08**
(21 sessions, full ordered universe `AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AVGO, COST, NFLX`),
scored **staged-only** (NOT `artifacts/backtests/`). Run data (git-ignored, local, reproducible):
`reports/m7_historical_runs/2026-03-10-clean-rs-v1/` (`summary.json`, `manifest.json`, `quotes/*.jsonl`).

- `rules_hash` = `7a51138a5448ff4aed3d5de9ec84b7fb17cd4d67a223d637d7e7e2a74decf5c6`
  (= `agent.config.rules_hash(load("config/agent_rules.json"))`).
- `data_pin` = `EQUS.MINI:bbo-1m:1m:historical:c6a585b970e7ef70358ff21a8d83182cc5476aefd9262bba66b5c8ca85d641cc`.
- 1144 trades / 21 traded sessions; **net −$839.68**; profit factor **0.55**; avg **−8.78 bps**.
- Active **−$120.65** vs pinned `exposure_matched_midbar_v1`; **+$405.64** vs `universe_equal_weight_long_v1`.
- Realism gaps: **p95 29.82 bps (cap 15)**, **max 97.48 bps (cap 50)** — both fail.
- Breadth broad: max 12.3% of gross legs / 16.8% of net-positive PnL (< 35% / 50% limits).
- Verdict: broad clean-window NULL ⇒ substrate (L1 1-min BBO) is the binding constraint ⇒
  two-family stop rule ⇒ Robin chose an **explicit STOP**.

### Reproducing the run (≈ $0.03, needs `.secrets/databento.json` + `.venv` with `databento==0.79.0`)

The building blocks are committed; the one-off driver lived in scratch (ephemeral). To
regenerate: pull → build manifest → score staged.

```python
import sys; sys.path.insert(0, "scripts")
from agent import config as agent_config
from agent.historical_backfill import (
    pull_normalized_window, build_cross_sectional_input_manifest, cross_sectional_data_pin)
from agent.backtest_historical import write_m7_historical_cross_sectional_artifact

UNIVERSE = ["AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","AVGO","COST","NFLX"]
rules_hash = agent_config.rules_hash(agent_config.load("config/agent_rules.json"))
rows = pull_normalized_window(dataset="EQUS.MINI", schema="bbo-1m", universe=UNIVERSE,
    start_utc="2026-03-10T13:30:00", end_utc="2026-04-08T20:01:00")          # RTH-filtered
manifest = build_cross_sectional_input_manifest(symbol_rows=rows, universe=UNIVERSE,
    dataset="EQUS.MINI", schema="bbo-1m",
    hypothesis_id="m7c_relative_strength_market_neutral_v0_20260613",
    selection_rule="Reuse the full ordered M7 broad universe before relative-strength metrics; "
                   "no symbol additions or removals from failed v1/v2 diagnostics.",
    calendar_pin="xnys-rth-regular-2026-03-10_2026-04-08-v1")
res = write_m7_historical_cross_sectional_artifact(
    artifacts_dir="reports/m7_historical_runs/2026-03-10-clean-rs-v1/staged",   # NEVER artifacts/backtests
    symbol_quote_rows=rows, rules_hash=rules_hash, data_pin=cross_sectional_data_pin(manifest),
    dataset="EQUS.MINI", schema="bbo-1m", created_utc="2026-06-26T20:00:00.000000Z",
    input_manifest=manifest, builder_git_commit="staged-clean-rs-v1", allow_reviewed_artifact=True)
# res.criteria.passed -> False; res.payload["metrics"] has the numbers above.
```

Run it with the sandbox disabled (network) + `.venv/bin/python`. Cost ≈ $0.03 (confirmed via
`client.metadata.get_cost`). Staged writer does NOT write when criteria fail (it returns the result).

## Acting on GPT's findings (protocol for the fresh context)

1. **Triage:** classify each finding real vs misread (verify against the actual code/data;
   GPT can misread the validator/serializer/recorder). Separate FACT / ASSUMPTION / OPINION.
2. **The pivotal contingency — a FALSE-NULL finding.** If GPT identifies a real bug/look-ahead/
   decode/latency/benchmark error that would make a strategy with edge *appear* to lose, this
   **reopens the STOP**: fix it TDD, re-run the full suite, **re-run the staged March pull**
   (recipe above, ≈ $0.03), and re-evaluate the Phase Gate before any STOP/substrate conclusion
   stands. Update `PLAN.md`/`CLAUDE.md`/memory if the verdict changes.
3. **Safety-critical findings** (artifact writer / `verify_artifact` / write guards / hash
   binding) are blockers — fix before anything else; never let a non-conforming artifact path
   survive.
4. **Fix discipline:** RED→GREEN TDD; full offline suite must stay green; keep authoring and
   review as separate passes. Do NOT flip run gates; do NOT write a production artifact to
   `artifacts/backtests/`; no orders.
5. **Process guardrails (learned the hard way):** dispatched build/fix subagents must NOT run
   git mutations; review/verify subagents tend to edit files despite READ-ONLY prompts —
   git-audit the tree after any agent run, revert non-authored edits, re-derive against
   pristine HEAD.
6. **If GPT confirms the null + clean bill of health:** the STOP stands; the open decision is
   then merge-to-main (47 commits, offline-complete) + a scope/ambition reassessment with Robin.

## Open decisions (unchanged by this review until findings land)

- **STOP (chosen 2026-06-26):** explicit stop on the autonomous edge search after two
  same-substrate families nulled. No substrate family (L2 fill / longer horizon / wider
  universe) in flight.
- **Merge-to-main (pending):** everything is on `codex/m7-backtest-gate`, nothing merged.

---

## Appendix — the exact prompt sent to GPT

````
You are a senior quant + systems reviewer performing an ADVERSARIAL, evidence-based review of a real autonomous US-equities trading agent. You have full read access to the repository and its history. Read the actual code and data before making any claim — do not review from these notes alone; they only orient you. Cite `file:line` for every finding. Separate FACTS (verifiable in the code/data) from ASSUMPTIONS from OPINIONS. This is a READ-ONLY review: propose changes, do not make them.

## Why this review matters (stakes)

This system will eventually gate real capital. Two things are on the line right now and I need you to pressure-test BOTH, hard:

1. **A go/no-go verdict.** A predeclared, long-only cross-sectional "relative-strength" strategy (M7c phase 1) was just backtested on a clean historical window and returned a **broad NULL (NO-GO)**. On the strength of that null, the project just made a **two-family STOP decision** (halt the autonomous strategy search). If that null is actually a *pipeline/measurement bug* rather than a true absence of edge, we are about to kill a potentially-viable strategy and stop for the wrong reason. **Your single most important job: try to prove the null is WRONG** — find the most plausible bug, look-ahead leak, decode error, latency/benchmark mistake, or fixture artifact that would make a strategy with real edge *appear* to lose money. If you cannot find one after genuine effort, say so explicitly and state your confidence.

2. **Safety-critical plumbing.** New code writes the "reviewed artifact" that the live system's S9 gate consults before allowing an open, and a verifier change touched that gate. A defect here could later let a non-conforming artifact pass (→ unsafe opens) or silently corrupt the provenance/hash binding. Treat the artifact-writer + verifier path as security-sensitive.

## What the system is (context)

- Autonomous, **paper-first, fail-closed** US-equities agent. Hard posture: NO real-money orders; run gates (`config/agent_rules.json: enabled`, `paper_trading.enabled`; `config/risk_rules.json: live_trading.enabled`) are committed `false`; live needs two-key arming. Tests make no network calls / no credential reads. Authoritative design: `docs/superpowers/specs/2026-06-08-stocks-agent-design.md`. Project state + safety rules: `CLAUDE.md`, `PLAN.md`.
- Data = Databento (historical-only key). Broker = Alpaca (paper). Everything is on branch `codex/m7-backtest-gate`, **47 commits ahead of `main`, nothing merged to main**. Offline test suite: **1762 tests green** via `python3 -m unittest discover -s tests -p 'test_*.py' -t .` (the `-t .` is required; imports are `agent.*` / `recorder.*` from `scripts/`).
- M7 = an anti-lookahead historical backtest gate + a v2 reviewed-artifact contract; a strategy is only "paper-eligible" once a reviewed artifact verifies `ok` against pinned criteria. M7c is the current strategy family (relative-strength), and its **packet/contract are predeclared** (pre-registered) so results can't be cherry-picked: `docs/superpowers/specs/2026-06-13-M7c-relative-strength-research-packet.md` (rev 2) and `docs/superpowers/specs/2026-06-13-M7c-phase1-proxy-contract.md`.

## Exactly what to review

The M7c body of work and the null verdict it produced. Commit range: `b637287..HEAD` (HEAD = `214850c`). Read these in full:

- `scripts/agent/strategies/relative_strength.py` — the phase-1 proxy: cross-sectional rank of the valid decision set, select top `TOP_N=2` long, equal `PAPER_NOTIONAL_USD=1000` notional, whole-share floor, one BUY `Candidate`/leg, no short side. `rs_score = rank(momentum_21) + 0.5*rank(ema_gap_9_21) + 0.5*rank(sma_gap_21_50) + 0.25*rank(rsi14_centered) - 0.25*rank(realized_vol_21)`.
- `scripts/agent/backtest_historical.py` — the multi-symbol harness `run_historical_cross_sectional_backtest` (same-timestamp assembly across the universe, top-2, 30-bar horizon, entry = decision+1m with M7 "quote-B" latency, no-overlap per symbol, both legs aggregated under ONE artifact, the `universe_equal_weight_long_v1` exposure-matched benchmark); `validate_historical_cross_sectional_manifest`; `write_m7_historical_cross_sectional_artifact`; the shared single-symbol path (`run_historical_backtest`, `_simulate_historical_long_trade`, `validate_historical_input_manifest`) and the extracted helpers (`_parse_calendar_block/_parse_blackout_dates/_parse_execution_block/_guard_production_artifact_dir`).
- `scripts/agent/historical_backfill.py` — credentialed backfill + cross-sectional input-manifest builder: `normalize_quote_event(s)`, `derive_session_windows`, `instrument_ids_from_rows`, `build_cross_sectional_input_manifest`, `cross_sectional_data_pin`, the RTH filter (`_within_rth`), and the live pull seam `pull_normalized_window` / `_live_quote_event_source` / `_dbn_bbo1m_record_to_event_dict` / `_dbn_price` / `_ns_to_iso_utc`.
- `scripts/agent/backtest_metrics.py` — `build_v2_artifact_payload` (sample/pnl/benchmark/risk/quality/thresholds, the pinned-criteria `pass`, the `exposure_matched_midbar_v1` benchmark).
- `scripts/agent/paper_phase_criteria.py` — pinned floors (min 20 sessions, 30 trades, profit_factor ≥ 1.10, positive net + active, drawdown/worst-day/realism-gap caps).
- `scripts/agent/backtest_gate.py` — `verify_artifact` (the S9 gate) and `_V2_PROVENANCE_OPTIONAL_KEYS` (this diff added equal-weight + `horizon` + a `cross_sectional_diagnostics` JSON-string key to the OPTIONAL allow-set; confirm the pinned benchmark method, floors, required keys, closed metric-block schemas, and the "every provenance value is a string except universe_symbols" rule are all UNWEAKENED).
- `scripts/recorder/event.py` — the DBN `parse` + `QuoteEvent`/`Provenance` + price/size quantization (`PRICE_QUANTUM=0.0001`, `SIZE_QUANTUM=1`, int 1e-9 fixed-point) that the live adapter feeds.
- Tests: `tests/agent/test_m7c_cross_sectional_runner.py`, `test_m7c_cross_sectional_artifact.py`, `test_m7c_historical_backfill.py`, `test_relative_strength_proxy_m7c.py`, `tests/agent/test_m7_historical_artifact.py`.

### The run + verdict to audit

A credentialed Databento `bbo-1m` pull was run on the packet's preferred clean window **2026-03-10 → 2026-04-08** (21 sessions, the full ordered universe `AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AVGO, COST, NFLX`), then scored **staged-only** (NOT written to `artifacts/backtests/`, which still holds only `.gitkeep`). The run lives under `reports/m7_historical_runs/2026-03-10-clean-rs-v1/` (`summary.json`, `manifest.json`, `quotes/*.jsonl`) — note `reports/` is git-ignored (licensed quotes are local-only but reproducible from the committed tool + the key in `.secrets/databento.json`). Reported result:

- 1144 trades over 21 traded sessions; **net execution-realistic PnL = −$839.68**; profit factor **0.55**; avg trade **−8.78 bps**.
- Active PnL **−$120.65** vs the pinned `exposure_matched_midbar_v1` benchmark; **+$405.64** vs `universe_equal_weight_long_v1` (interpreted as "the equal-weight long basket simply lost more").
- Realism gaps: **p95 = 29.82 bps (cap 15)**, **max single-fill divergence = 97.48 bps (cap 50)** — both fail.
- Breadth: 10 distinct symbols traded; max single-symbol share = 12.3% of gross legs / 16.8% of net-positive PnL (well under the packet's 35%/50% concentration limits) → claimed "broad, not a single-symbol artifact."
- Conclusion drawn: a clean-window broad null ⇒ the binding constraint is the **substrate** (L1 1-minute BBO), not the long-only shape ⇒ per the predeclared two-family search-budget stop rule (momentum = family 1 nulled; relative-strength = family 2 nulled), route to a substrate decision; I then chose an **explicit STOP**.

## Review dimensions (be exhaustive; hunt, don't skim)

**A. Is the NULL trustworthy? (highest priority — try to refute it)**
- Decode: `_dbn_bbo1m_record_to_event_dict` + `_dbn_price` (int 1e-9 → 4dp Decimal), flat `*_00` top-of-book, `UNDEF_PRICE`/`UNDEF_TIMESTAMP` (UINT64_MAX) drops, `ts_event`/`ts_recv` ns→ISO, `sequence`→`vendor_seq`. Could prices/sizes be mis-scaled, sides swapped, or stale carried-forward quotes leak in? Verify against the live `BBOMsg` semantics and `recorder.event.parse`.
- RTH filtering (`_within_rth`, ts_recv time-of-day in 13:30–20:00 UTC): correct for the window (US EDT after the 2026-03-08 DST switch; any early-close days in 2026-03-10…04-08; Good Friday 2026-04-03 is a full holiday)? Could it drop the wrong minutes or keep extended-hours bars the harness then treats as RTH (the historical market-state is RTH-always)?
- Look-ahead / latency: entry = decision_bar_end + 1m with "quote-B" latency; does any feature, ranking, fill, or exit read data at or after the decision instant? Is the 30-bar horizon / RTH-close skip correct? Does cross-session feature continuity (rolling windows spanning the overnight gap) bias anything?
- Ranking + selection + benchmark: is the top-2 selection and the exposure-matched `exposure_matched_midbar_v1` benchmark (`trade.benchmark_pnl_usd`) computed correctly, so the −$120.65 active number is real? Is whole-share flooring or the 5bps marketable-limit handling silently rejecting/biasing fills?
- Realism gap: is p95 29.8 / max 97.5 bps a genuine execution-realism signal on 1-min BBO, or an artifact of the modeled-fill-vs-raw-mid gap computation?
- Economic plausibility: −$839.68 / 1144 trades ≈ −0.73 USD/trade on ~$1000 notional ≈ −7.3 bps/trade. Is that consistent with mega-cap spread + 1-min latency, or anomalously large/small (which would hint at a bug)?
- Net: **What is the single most likely way this null is an artifact rather than a true no-edge result?** Give a concrete, testable hypothesis and how to falsify it.

**B. New-code correctness, safety, determinism**
- Manifest hash-binding round-trip: does `build_cross_sectional_input_manifest` ALWAYS produce a manifest `validate_historical_cross_sectional_manifest` accepts for the same rows (per-symbol `quote_rows_sha256` + body `manifest_hash` recompute identically; per-symbol `data_pin` derived as `…:{manifest_hash}:{symbol}`, never stored — confirm no circular dependency)? Any divergence after a JSONL disk round-trip via `load_quote_rows_jsonl`?
- Write guards / fail-closed: `_guard_production_artifact_dir` (exact `artifacts/backtests` dir only, never nested; reviewed-flag required; passing write to ANY dir requires the flag), strategy-id guard (only `relative_strength.long_only_proxy_v1`), and that the writer reaches NO broker/preflight/order surface. Can anything cause a write under the real committed `artifacts/backtests/`?
- Verifier change: did adding optional provenance keys weaken the S9 gate in any way? Could the `cross_sectional_diagnostics` JSON-string blob smuggle a failing metric past the gate?
- Determinism: serializer Decimal-strictness (no float), `row_hash`/`dumps(sort_keys)`, dict/set ordering. Any nondeterminism between runs over identical inputs.
- No-network/no-creds invariant: confirm importing the new modules pulls neither `databento` nor `.secrets` (the live import is lazy inside `_live_quote_event_source`); `tests/agent/test_no_network_no_creds.py` must stay valid.

**C. Decision logic**
- Is applying the predeclared two-family stop rule correct here, and is the "substrate, not shape" inference sound given the breadth + the realism-gap failures? Is there a cheap, in-scope variation (e.g. fixing a measurement issue from A) that should be tried before stopping? Is the explicit STOP defensible, or premature?

**D. Merge-to-main readiness**
- Is the branch coherent and safe to merge to `main` (gates off, no half-finished surface, no licensed data committed, tests green)? Flag anything that should be cleaned, finished, or split before a merge.

## Output format

For each finding: `[SEVERITY: blocker|high|medium|low] [DIMENSION A/B/C/D] title — file:line — evidence (what the code does) — why it's a real problem — concrete repro or falsification — recommended fix`. Then close with an explicit verdict on each of the four dimensions:
1. **Null trustworthy?** (yes / no / can't-tell + confidence + the most-likely-bug hypothesis from A).
2. **Code sound & safe?** (approve / changes-required + the blockers).
3. **STOP decision sound?** (sound / reconsider + why).
4. **Merge-to-main ready?** (yes / not-yet + what's needed).

Be specific and conservative: only real, actionable findings grounded in the actual code/data. If something is already correctly mitigated, don't raise it. If the work is sound, say so plainly — a clean bill of health is a valid result, but only after a genuine adversarial attempt to break it.
````
