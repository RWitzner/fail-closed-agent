# Fundamentals predeclaration GPT review handoff (2026-07-10)

**Purpose.** A GPT adversarial review is requested on a **predeclaration packet** (a pre-registered research
design), NOT on code or a run/verdict. The packet —
`docs/superpowers/specs/2026-07-10-fundamentals-longterm-research-packet.md` (DRAFT rev 2) — predeclares a
**new research line** on a genuinely new substrate: daily cadence, point-in-time fundamentals, months-scale
holds, benchmarked against S&P 500 total return. It is the "fresh explicit mandate" line contemplated by
`PLAN.md`'s substrate rule and does NOT consume the intraday substrate budget (M7d remains 1 of 2 there).
This document lets a FRESH context act on GPT's findings without re-deriving state. The exact prompt sent to
GPT is embedded verbatim in the appendix.

> Robin routes reviews through GPT. The most consequential question for a *predeclaration* is methodological:
> is the experiment honestly falsifiable, free of data-snooping and multiple-comparisons escape hatches, and
> are its data-feasibility and harness-fit claims correct — BEFORE any new PnL is computed or any data dollar
> is spent. A predeclaration's whole value is that it pre-commits the verdict and the routing so results
> cannot be cherry-picked. This line adds a trap no intraday packet had: the packet's own AUTHORS (an LLM
> trained through ~2026, and Robin) already know how the 2023–2026 holdout played out.

## Review outcome

**PENDING** — populated after GPT returns (verdict per dimension + overall APPROVE / CHANGES-REQUIRED /
RECONSIDER-EXPERIMENT, each finding, and the rev-3 resolution).

## What this packet is (and is NOT)

- A pre-registered design for phases F0 (data substrate, no strategy PnL) → F1 (ONE frozen mechanical family,
  the line's only backtest, single-use historical holdout 2023-07-01→2026-06-30) → F2 (LLM overlay,
  FORWARD-ONLY, conditional, never backtested).
- **Nothing is authorized by the packet or by this review:** no F0 build, no credentialed/paid pull, no bulk
  EDGAR beyond a hard cap of ≤ 20 logged spot-check requests, no gate flip, no artifact write, no paper step.
  After GPT review + revisions, F0 still needs Robin's separate explicit go ($0 sources only; any Databento
  daily pull is its own ask).
- The safety spine is inherited unchanged: run gates committed `false`, S9 (no paper opens without a reviewed
  `ok` artifact under the line's OWN criteria), `artifacts/backtests/` = `.gitkeep`, strategy registry
  fail-closed. The M7 intraday criteria module and verifier stay byte-for-byte untouched — the daily line
  gets its OWN criteria module + verifier (a NEW metric space, not a "tighten-only" change; rev 1 misstated
  this and rev 2 corrects it).
- The packet's numeric thresholds are **rev-2 proposals to be frozen at review** — GPT should stress-test the
  numbers, not treat them as settled.

## Repo state (as of this handoff)

- Branch **`main`**, HEAD `d0cc87f` (rev-1 packet committed `ed557ce`; the two later commits are today's
  Alpaca-IEX feed verification fixes, unrelated to this line).
- Offline suite: **2000 tests green** — `python3 -m unittest discover -s tests -p 'test_*.py' -t .` (`-t .`
  required).
- Run gates committed `false` (`config/agent_rules.json: enabled`, `paper_trading.enabled`;
  `config/risk_rules.json: live_trading.enabled`); `artifacts/backtests/` holds only `.gitkeep`;
  `.secrets/databento.json` = historical-only key; EDGAR needs no credential (fair-access rate limits apply).
- Context for the strategic pivot: both intraday families nulled clean on the L1 1-minute substrate; the
  measured entry-leg realism floor makes an M7d GO structurally improbable; Robin's 2026-07-10 GO started
  this separate fundamentals line scoped to **packet drafting + review only**.

## How this packet was authored (so GPT can attack the process, not just the artifact)

- **Rev 1** was a SINGLE-PASS draft by Claude on Robin's mandate — no multi-agent hardening preceded it (do
  not assume M7d's 13-agent provenance here).
- **Rev 2 (same day)** applied the FIRST adversarial pass: five parallel READ-ONLY review lenses —
  PIT/lookahead, $0-data feasibility (EDGAR/Alpaca/Databento/Stooq mechanics), statistics/criteria/benchmarks,
  strategy/economic design, governance/repo-consistency. They returned **8 blockers + ~23 majors + ~10
  minors** (deduplicated); the orchestrator independently re-verified every finding against the packet text
  before applying it. GPT is therefore the SECOND adversarial pass and should RE-ATTACK the applied fixes
  rather than trust them. The most consequential, in brief:
  1. The core PIT usable-from rule was self-contradictory by one trading session ("second session after
     filed" vs "filed + 1 trading day") → replaced by one canonical form.
  2. The CIK↔ticker identity map was a silent survivorship re-import (the only free SEC map is a
     current-state snapshot; delisted names unmappable, recycled tickers mis-attributed) → CIK-first
     enumeration + vintaged mapping + an F0 gate on MAP MEMBERSHIP of delisted names.
  3. The 20-day "$5M ADV" screen had no volume basis; on IEX-only volume (~2–3% of tape) it was a ~30–50×
     tighter, non-stationary filter → consolidated basis pinned, F0 measures the ratio.
  4. EDGAR `companyfacts` vs `frames` were conflated; frames returns latest-filed (restated) values →
     frames barred from any PIT feature/universe value.
  5. Quality-leg ROE sign-inverts on negative book equity (the HD/MCD buyback cohort) and fights the
     issuance factor → quality leg replaced by ROA (better pre-2010 grounding: Haugen–Baker 1996, Piotroski
     2000).
  6. The shares series was not bound to be split-adjusted; the 2020–2024 mega-cap split wave would read as
     massive fake issuance AND corrupt the market-cap rank → split-consistent shares pinned for both.
  7. No per-observation consistency gate existed (the M7 module's PF/avg-trade floors had no analog; one
     lumpy month could carry a cumulative GO) → new gate 9: monthly-active hit ≥ 20/36 AND monthly-active
     PF ≥ 1.15 (numbers deliberately proposals — stress them).
  8. Gates were prose with no artifact-metric binding; the cost-stress gate was unverifiable from a stored
     artifact → a verifiability contract: pinned metric-key schema + in-run stress fields.
  Applied majors include: a binding raw-close/no-back-adjusted-series contract; the CA/dividend source made
  a first-class F0 deliverable+gate; the embargo rationale corrected (6 months cannot break a 12-month
  trailing overlap; the attestation is the operative control); the author-hindsight residual CONCEDED
  honestly (historical holdout = no forward firewall, unlike M7d); SPY-proxy marked on the strategy's
  fill-lagged intervals + pinned ^SP500TR reference with an expense-drag-centered asymmetric tolerance + the
  proxy-vs-index concession stated; TTM stitching/tag precedence pinned; multi-class total-company cap;
  financials/REITs excluded (SIC 6000–6799, Fama-French-grounded) and the ETF/ADR sentence corrected to
  structural exclusion; MAD-winsorize-then-z order pinned; the M7c-parity strategy-hypothesis hash-binding
  block added to the F1 artifact contract; gates 1/5/6/7 re-specified to be computable exactly as written;
  the equal-weight benchmark pays its OWN turnover; gate-3 leg routing (selection vs weighting) predeclared;
  near-free-gates / effective-N / single-regime honesty added; the daily paper-phase criteria declared a
  separate future module; the family-2 slot made explicitly non-auto-authorized; the F0
  expected-pause-for-paid-data branch stated as the LIKELY outcome on pure-$0 sources.
- Process guardrails held: the lens agents were structurally read-only (no Edit/Write tools) and the
  post-run `git status` audit was clean; authoring and review were separate passes.

## The design in brief (what GPT is reviewing)

- **F0 (no PnL):** EDGAR companyfacts (+ SEC bulk files) fundamentals; one primary daily price source
  (Alpaca IEX free tier, history ~2016+, or Databento daily behind Robin's go); a CA/dividend source; a PIT
  identity map (CIK-first, vintaged); hash-bound manifests; a calibration report; a hard adequacy gate whose
  failure PAUSES the line for a Robin paid-data decision (expected branch, stated as such).
- **F1:** `fundamentals.xs_value_quality_v1` — long-only, monthly, top-20-of-top-100-by-PIT-cap
  (financials/REITs excluded), equal-notional; factors = earnings yield (Basu 1977), ROA (Haugen–Baker 1996 /
  Piotroski 2000), net share issuance on split-adjusted shares (Ikenberry et al. 1995, Pontiff–Woodgate
  2008); MAD-winsorize-then-z, equal-weight composite, zero fitting; decisions at month-end close on
  PIT-usable data (canonical usable-from rule: `D ≥ F + 1` trading session where `F` = first session on/after
  `filed`); fills next session's close + 10 bps/side; dev through 2022-12-31, embargo H1-2023, single-use
  holdout 2023-07-01→2026-06-30 (36 month-ends, terminal mark 2026-06-30).
- **Gates (conjunctive, frozen at review):** coverage (36 scheduled / ≤ 2 skips / ≥ 80 eligible);
  net TR > 0; active > 0 vs BOTH `sp500_total_return_proxy_v1` (fill-lagged SPY TR) and
  `universe_equal_weight_tr_v1` (own-turnover-costed); cost-stress ×2; daily-NAV drawdown ≤ 0.35 absolute AND
  ≤ 1.25× the equal-weight benchmark's; concentration ≤ 30% of net-positive contributions + fill-integrity;
  delisting exposure ≤ 5% position-dollar-days; zero quality breaches (structural counters documented
  N/A-zero); monthly consistency (≥ 20/36 positive monthly actives, monthly-active PF ≥ 1.15);
  failure-delisting haircut stress (−30%→−60%). All gates bind to a pinned artifact metric-key schema;
  stress fields are emitted in-run.
- **Stop rules:** ≤ 2 families on this line (family 2 NOT predeclared, NOT auto-authorized); F2 never a
  rescue path; F0 inadequacy = data conclusion → pause/stop; no cross-contamination with the intraday budget
  in either direction.

## Files / docs the packet depends on (GPT should verify the claims)

- `docs/superpowers/specs/2026-07-10-fundamentals-longterm-research-packet.md` — the artifact under review
  (rev 2).
- Discipline precedents: `docs/superpowers/specs/2026-06-13-M7c-relative-strength-research-packet.md`,
  `docs/superpowers/specs/2026-06-26-M7d-longer-horizon-research-packet.md`, and
  `docs/superpowers/reviews/2026-06-26-M7d-predeclaration-gpt-review-handoff.md` (this document's template).
- Harness-fit claims to verify at HEAD: `scripts/agent/bar_series.py:117-118` (non-`1m` hard-reject — note
  rev 1 cited 114-115, a docstring; rev 2 corrected it), `scripts/agent/signal_config.py:117-119`,
  `scripts/agent/backtest_historical.py:414-415` + `:526-527` (interval validators),
  `scripts/agent/backtest_gate.py` (`_V2_METRIC_KEYS` exact-match schema, `_V2_RISK_KEYS` intraday realism
  keys, the pinned benchmark ids — the basis for the "own new verifier, NOT tighten-only" posture),
  `scripts/agent/paper_phase_criteria.py` (must remain untouched; its PF/avg-trade hard gates are the
  consistency standard gate 9 restores an analog of), `scripts/agent/paper_session.py` (`_CROSS_SECTIONAL_IDS`
  fail-closed registry refusal precedent), `config/agent_rules.json` + `config/risk_rules.json` (gates
  `false`), `PLAN.md` (substrate budget + mandate scope), `CLAUDE.md`.

## Acting on GPT's findings (protocol for the fresh context)

1. **Triage:** classify each finding real vs misread against the actual packet/repo text; separate FACT /
   ASSUMPTION / OPINION. This is a DESIGN review — expect methodology findings (leakage, multiplicity,
   decision-rule loopholes, data-feasibility errors), not runtime bugs.
2. **The pivotal contingency — "this line is not worth starting (or not at $0)."** If GPT argues convincingly
   that the $0 data plan cannot support an honest PIT panel (identity map / delisted coverage / CA source)
   and the line should EITHER start from a paid survivorship-bias-free panel decision OR not start, surface
   that to Robin as a routing question BEFORE any F0 go — the packet already predeclares the
   pause-for-paid-data branch as likely.
3. **Methodology blockers** (a surviving leakage path — including any under-conceded author-hindsight
   channel, a multiple-comparisons escape hatch, a gate silently weakened or unverifiable from the artifact,
   a benchmark mis-specified) are must-fix before the packet is marked reviewed.
4. **Data-mechanics errors:** if any EDGAR/Alpaca/Databento/Stooq claim is wrong, fix the packet to match
   reality; re-verify the F0 gate still covers the corrected risk.
5. **Fix discipline:** revise the packet text only (predeclaration — nothing to build or run). Do NOT flip
   gates; no artifact writes; no orders; no credentialed pull; EDGAR spot-checks stay within the packet's
   ≤ 20-request cap. Keep authoring and review as separate passes.
6. **Process guardrails (learned the hard way):** dispatched subagents must NOT run git mutations;
   review/verify subagents tend to edit files despite READ-ONLY prompts — git-audit the tree after any agent
   run, revert non-authored edits, re-derive against pristine HEAD.
7. **Exit:** if GPT findings are fixed and no methodology blocker remains, mark the packet GPT-reviewed; it
   then waits on Robin's separate F0 go ($0-scope) — and any Databento/paid pull remains its own ask.

## Open decisions (unchanged by this review until findings land)

- **Robin's F0 go** (after review): build the $0 data layer, or route straight to the paid-data decision the
  packet predicts, or park the line.
- **Priority vs the intraday track:** M7d's authorized run (holdout complete ~2026-07-14) and the paper
  observe-track are independent of this line; nothing here blocks or is blocked by them.
- **Branch for any future F0 code loop:** a fresh branch off `main`, stated explicitly before starting.

---

## Appendix — the exact prompt sent to GPT

````
You are a senior quant + experiment-design reviewer performing an ADVERSARIAL, evidence-based review of a PREDECLARATION (a pre-registered research design) for a real autonomous US-equities trading program. You have full read access to the repository and its history. Read the actual packet AND the files it cites before making any claim — do not review from these notes alone. Cite `file:line` for every finding. Separate FACTS (verifiable in the packet/repo) from ASSUMPTIONS from OPINIONS. This is a READ-ONLY review: propose changes, do not make them.

## Why this review matters (stakes)

This system will eventually gate real capital. The artifact under review is NOT code or a run — it is a predeclaration packet for a NEW research line (daily-cadence, point-in-time fundamentals, months-scale holds) that pre-commits, BEFORE any PnL is computed on this substrate, the data contracts, the universe rule, the strategy shape, the windows, the acceptance gates, and the stop rules. Its entire purpose is to make any future result un-cherry-pickable. A predeclaration with a leakage path, an escape hatch, an unverifiable gate, or a false data-feasibility claim would let a future null (or GO) be rationalized after the fact. Your job is to try to break the design's integrity and its claims, hard.

Two traps are load-bearing and unique to this line — attack them first:
1. LLM future-knowledge: the packet bans LLM output from every backtested feature and makes the LLM overlay (F2) forward-only — but the packet's own AUTHORS (an LLM trained through ~2026, and the operator) already know how the 2023–2026 holdout played out. Rev 2 concedes this residual explicitly (historical holdout = NO forward firewall, unlike the intraday M7d packet's genuinely-forward window) and claims it is bounded by pre-2010-literature factor grounding + no-change-after-first-read + PROVISIONAL routing to forward paper time. Is that concession honest and sufficient, or is the bound illusory (e.g. the factor TRIO and the window boundaries are still hindsight-bearing choices among many pre-2010 candidates)?
2. Survivorship: the packet claims a PIT universe from EDGAR (CIK-first enumeration, vintaged CIK↔ticker mapping, delisted names retained, F0 gating on identity-map membership AND price coverage, delisting conventions with a failure-haircut stress). Find any residual survivor path — enumeration, mapping, price panel, CA/dividend stream, benchmark construction.

## Context (what the program is and where it is)

- Autonomous, paper-first, fail-closed US-equities agent. Hard posture: NO real-money orders; run gates committed false; live needs two-key arming; tests make no network calls / no credential reads. Authoritative design: docs/superpowers/specs/2026-06-08-stocks-agent-design.md; state + safety rules: CLAUDE.md, PLAN.md.
- History: two intraday families nulled clean on the L1 1-minute substrate (momentum; relative strength — M7c broad null on a clean window), the two-family stop rule fired, and the sanctioned intraday substrate experiment (M7d, longer horizon) is authorized and awaiting its fresh holdout (~2026-07-14). THIS packet is a SEPARATE line under a fresh mandate scoped to drafting + review only; it consumes no intraday budget and is authorized to build/run NOTHING yet.
- Repo on `main`, HEAD d0cc87f, offline suite 2000 tests green via `python3 -m unittest discover -s tests -p 'test_*.py' -t .`.

## The artifact to review

docs/superpowers/specs/2026-07-10-fundamentals-longterm-research-packet.md — DRAFT rev 2. Read it in full. Structure: two binding traps (LLM future-knowledge with F2 forward-only + the conceded author-hindsight residual; survivorship with identity-map + delisting contracts); F0 data-substrate phase with a hard adequacy gate whose paid-data-pause branch is declared the EXPECTED outcome on $0 sources; F1 = ONE frozen mechanical family (fundamentals.xs_value_quality_v1: long-only monthly top-20-of-top-100 by equal-weight composite of earnings yield, ROA, split-adjusted net issuance; financials/REITs excluded SIC 6000-6799; MAD-winsorize-then-z; zero fitting), single-use historical holdout 2023-07-01→2026-06-30 with a pinned terminal mark; ten conjunctive acceptance gates bound to a pinned artifact metric-key schema (incl. dual-benchmark actives on a fill-lagged basis, a ×2 cost stress, a −60% failure-delisting stress, and a monthly-consistency gate); dual benchmarks (SPY-TR proxy with a pinned ^SP500TR cross-check tolerance centered on the known expense drag + an own-turnover-costed equal-weight universe TR); stop rules (≤ 2 families, family 2 not predeclared and not auto-authorized, F2 never a rescue path).

This packet was already hardened by ONE internal adversarial pass (5 read-only lenses; 8 blockers + ~23 majors applied in rev 2 — the packet's Revision History lists them). You are the SECOND adversarial pass: RE-ATTACK the applied fixes rather than trusting them, and hunt for what the first pass missed.

## Files to verify claims against (read these; do not trust the packet's citations)

- scripts/agent/bar_series.py:117-118 (non-1m hard-reject), scripts/agent/signal_config.py:117-119, scripts/agent/backtest_historical.py:414-415 and :526-527 — the "intraday harness cannot run this line" claim.
- scripts/agent/backtest_gate.py — _V2_METRIC_KEYS (exact-match), _V2_RISK_KEYS (intraday realism keys), pinned benchmark ids — the packet's "own NEW verifier, NOT a tighten-only change" posture.
- scripts/agent/paper_phase_criteria.py — the intraday criteria module the packet promises to leave untouched, whose PF ≥ 1.10 / avg_trade_bps > 0 hard gates motivated the new monthly-consistency gate 9.
- scripts/agent/paper_session.py — the fail-closed strategy-registry refusal precedent.
- config/agent_rules.json, config/risk_rules.json — gates false.
- PLAN.md (substrate-search budget + mandate scope), CLAUDE.md (posture), the M7c/M7d packets + the M7d GPT handoff (the discipline precedents this line claims parity with).

## Review dimensions (be exhaustive; hunt, don't skim)

**A. Leakage / PIT integrity (highest priority).** The canonical usable-from rule (D ≥ F+1 with F = first session on/after `filed`; decisions at close, fills next close) — any residual ambiguity (amendments, non-trading-day filed, acceptance-time edge cases)? The raw-close/no-back-adjusted-series contract — complete? The dividend/ex-date discipline, the split-adjusted shares series, the TTM stitching vintage rule — attack each. Is anything in the universe rule, factor computation, or benchmark construction conditioned on information not knowable at D?

**B. Survivorship / data feasibility at $0.** The CIK-first enumeration + vintaged mapping plan — actually constructible from EDGAR submissions data at $0, or hand-waved? Is the F0 gate (map membership + price coverage + CA coverage + close-proxy divergence + ≥60 months × ≥80 names) sufficient and honestly ordered? Are the Alpaca-IEX limitations (history ~2016+, IEX-only volume and closes, no official auction close, delisted coverage) correctly stated and correctly gated? Is the consolidated-ADV basis pinning workable? Is the declared "pause for paid data is the EXPECTED branch" framing honest or a soft escape hatch?

**C. Statistics / gates / multiple comparisons.** 36 autocorrelated monthly observations in a single post-2016 regime — is the packet's effective-N and near-free-gates honesty adequate? Gate 9's numbers (20/36, PF 1.15) — right order, or still gameable/lumpy-month-passable? The verifiability contract (metric-key schema, in-run stress fields) — does it actually make every gate checkable from a stored artifact? Any residual escape hatch in the stop rules / family budget / F2 conditionality? Is the gate-3 leg-routing paragraph (selection vs weighting) clean, or does it pre-stage a post-hoc rationalization?

**D. Benchmark honesty.** The fill-lagged SPY-TR proxy + drag-centered asymmetric tolerance [−25, −2] bps/yr vs ^SP500TR + the explicit proxy-vs-index concession — sound? The equal-weight benchmark paying its OWN 10 bps/side turnover — correctly specified and unbiased? The shared delisting conventions — do they really cancel in the equal-weight active leg?

**E. Strategy-shape integrity.** ROA-for-ROE (negative-book-equity fix) — right call, correctly grounded pre-2010? Split-adjusted issuance + total-company multi-class cap — complete? Financials/REITs exclusion (SIC 6000-6799) — literature-grounded predeclaration or a hindsight-bearing choice? MAD-winsorize-then-z — sound? Anything in the factor trio that a 2026-knowledge author would plausibly have chosen BECAUSE of 2023-2026 outcomes, beyond what the concession covers?

**F. Governance / harness-fit.** Every repo citation correct at HEAD? The own-new-verifier posture vs backtest_gate.py reality? The M7c-parity hypothesis hash-binding block — sufficient to prevent post-run factor/weight/universe/window swaps? The EDGAR ≤ 20-request review cap and SEC-bulk-file sanctioning — coherent with the mandate scope? The daily paper-phase-criteria out-of-scope declaration — honest about the years-scale forward confirmation clock?

**G. Is this line worth starting at all — and at $0?** Given the conceded author-hindsight residual on a fully historical holdout, the expected F0 pause, and the years-scale forward-confirmation clock: is F1's historical backtest evidence worth its build cost, or should the line EITHER go straight to a Robin paid-data decision, OR skip to a forward-only design (F1 run as a paper-time forward test), OR not start? Answer explicitly — this is the decision Robin needs informed.

## Output format

For each finding: `[SEVERITY: blocker|high|medium|low] [DIMENSION A–G] title — packet section and/or file:line — evidence (what the packet/repo says) — why it's a real problem for a predeclaration — concrete fix`. Then close with an explicit verdict per dimension and an overall: **APPROVE (mark reviewed) / CHANGES-REQUIRED (list blockers) / RECONSIDER-EXPERIMENT (route elsewhere first)**. Be specific and conservative: only real, actionable findings grounded in the actual packet/repo. If a concern is already correctly mitigated in rev 2, don't re-raise it — but verify the mitigation text actually says what this handoff claims. If the design is sound, say so plainly — a clean bill of health is a valid result, but only after a genuine adversarial attempt to break it.
````
