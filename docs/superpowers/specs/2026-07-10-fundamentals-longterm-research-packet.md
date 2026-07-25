# Fundamentals Long-Horizon Research Packet (new research line)

- **Date:** 2026-07-10 (rev 3 same day)
- **Status:** DRAFT rev 3 — predeclared research/design packet, **GPT-reviewed** (gpt-5.6-sol via Codex CLI
  0.144.1, ultra reasoning, read-only sandbox, 2026-07-10 evening). GPT verdict: **RECONSIDER-EXPERIMENT**
  overall + CHANGES-REQUIRED on five blockers if the line is kept. All five blockers and the high/medium
  findings are APPLIED in this revision (see Revision History), including two that corrected rev-2's own
  fixes. **The line's next step is Robin's ROUTING CHOICE, not a build:** (a) give a bounded F−1
  data-procurement review its go, (b) route straight to a paid-data decision, or (c) park the line. No F0
  build, no data spend, no credentialed pull, and no run is authorized by this document. Robin's mandate
  (2026-07-10) covers **authoring + review only**. Every numeric threshold remains a **proposal to be frozen
  at review** — pinned before any PnL is computed, exactly like the M7c/M7d discipline.
- **Relation to the intraday program:** this is the **fresh explicit mandate** contemplated by `PLAN.md`'s
  substrate-search budget ("any continuation — including a daily-horizon/EOD family line, which is a NEW
  substrate — requires a fresh explicit mandate from Robin"). It is a **separate research line** on a genuinely
  different substrate (daily cadence, point-in-time fundamentals, months-scale holds). It does **NOT** consume
  the intraday substrate budget (M7d remains substrate experiment 1 of 2 on that line), does not depend on
  M7d's outcome, and inherits the full safety spine unchanged (gates, S1, journal, artifact discipline, S9).
- **Hypothesis id:** `fund_longterm_v0_20260710`.
- **Benchmark (Robin-set):** S&P 500 **total return** (gated via the tradable SPY proxy — see the explicit
  concession in Benchmark Contracts).

## Review Disposition

- DRAFT rev 1 was authored single-pass by Claude on Robin's mandate. Rev 2 (same day) applied the first
  adversarial pass: five parallel read-only review lenses (PIT/lookahead, $0-data feasibility,
  statistics/criteria/benchmarks, strategy/economic design, governance/repo-consistency) — 8 blockers +
  ~23 majors, each independently re-verified before application. **Rev 3 (same day) applies the second
  adversarial pass: the GPT review** (handoff + verdict:
  `docs/superpowers/reviews/2026-07-10-fundamentals-predeclaration-gpt-review-handoff.md`). GPT's five
  blockers, all confirmed on triage: (1) a fully-historical window cannot become confirmatory through
  post-outcome preregistration → the window is RECLASSIFIED as a rejection/engineering screen; (2) the
  promised $0 PIT security master does not exist in EDGAR's data model → a named, verifiable security master
  is now a hard prerequisite (the F−1 phase); (3) the F0 gates were discretionary and could shape the screen
  universe after names were visible → F0 split into F0A (freeze) / F0B (one fail-closed materialization);
  (4) the declared 36-observation calendar was mathematically impossible (the June-2026 decision fills AFTER
  the terminal mark — a rev-2 error) → explicit initialization-decision calendar; (5) the artifact hash was
  tamper-evident but not an external preregistration lock → committed design-digest contract. Two further
  rev-2 claims were corrected: the delisting-convention "cancels in the active leg" claim (false — identical
  conventions ≠ identical weights) and gate 9's numbers (a verified counterexample passed with 98.1% of
  positive active from one month).
- **Process guardrails (carried from M7c/M7d, learned the hard way):** dispatched review/critique agents are
  READ-ONLY — no git mutations, no file edits (including to this packet); the tree is git-audited after every
  agent run and non-authored edits reverted; authoring and review stay separate passes. (Both review passes
  ran read-only — the rev-2 lens agents structurally so, the GPT review in a read-only Codex sandbox — and
  both post-run audits were clean.)
- This packet predeclares the phase structure, the anti-lookahead contracts (including the **LLM
  future-knowledge trap**, unique to this line), the universe rule, the windows, the benchmark contracts, and
  the stop rules **before** any strategy PnL exists on this substrate.
- No gate is flipped, no threshold relaxed, no artifact written. `artifacts/backtests/` remains
  `.gitkeep`-only; the committed run gates stay `false`.

## Purpose

Two intraday families (momentum, relative-strength) nulled cleanly on the L1 1-minute substrate, and the
measured entry-leg realism floor makes an M7d GO structurally improbable — the binding intraday constraint is
per-trade half-spread friction at short holds. The strategic read (2026-07-10, Robin-endorsed): the realistic
long-term expectancy for this program lives at a **longer horizon on fundamentals**, where (a) round-trip costs
are amortized over months instead of minutes — at the pinned 10 bps **per side** (20 bps round trip) and the
expected ~30% monthly one-way turnover, the cost drag is ~0.7%/yr (~2.4%/yr at the 100% turnover ceiling),
versus the same order of round-trip cost paid *per 30–120-minute trade* that killed the intraday families —
(b) the decision cadence (daily) makes fill realism a solvable modeling problem instead of the binding
constraint, and (c) the program's actual tooling advantage — disciplined PIT data engineering and, later,
LLM-assisted research — points away from latency competition entirely.

**What the historical test can and cannot prove (GPT-forced honesty, rev 3):** because the candidate test
window (2023-07 → 2026-06) is fully historical and its outcomes are known to the packet's authors, the
historical F1 run is a **rejection/engineering screen, not edge evidence**: a NULL parks or stops the line;
a pass means only "do not kill — begin forward evidence collection." **The only promotion-carrying evidence
this line can ever produce is forward** (paper-time accrued after this predeclaration date), benchmarked under
its own separately predeclared paper criteria. That forward clock runs in YEARS for a monthly strategy —
acknowledged, and the reason the line must be cheap until it earns otherwise.

**Sequencing (GPT-forced, rev 3):** the expensive part of F0 is NOT the first spend. A bounded **F−1
procurement review** (no PnL, no build, time- and request-capped) first establishes whether a survivor-complete
PIT security master + CA/delisting source exists at $0 or names its price — because security IDENTITY, not
historical bars, is the binding constraint. Robin decides procure / $0-proceed / park BEFORE any F0
engineering.

This packet does not authorize paper trading, live trading, production artifact writes, threshold relaxation,
or any credentialed/paid data pull.

## The two traps this line lives or dies on (predeclared contracts)

### Trap 1 — LLM future-knowledge leakage (the reason F2 is forward-only)

Any LLM whose training data postdates a historical window **knows the outcomes** of that window: which
companies compounded, which blew up, which "cheap" stocks were value traps, the 2020 crash, the AI-capex boom.
An LLM asked to "assess this company as of 2019" cannot be trusted to firewall that knowledge, and no prompt
discipline makes the leak measurable or bounded. Therefore, **binding contract**:

- **No LLM output may enter any backtested feature, signal, ranking, parameter, or universe decision** on any
  historical window — F1 is 100% mechanical from PIT data.
- LLM-assisted research (F2) operates **forward-only**: decisions made at wall-clock time T may use an LLM
  whose knowledge cutoff ≤ T, on inputs journaled at T. Its evidence accrues in real elapsed paper time and is
  benchmarked against the F1 mechanical baseline AND the S&P 500 TR benchmark.
- The build/review agents (Claude/GPT) obviously author code; the ban targets **model judgments about specific
  companies/periods entering the strategy path**, plus the ordinary snooping ban (no strategy-shape iteration
  informed by peeking at window outcomes). F1's factor definitions must be justified from **pre-2010 public
  literature** (one pinned citation per factor), and the exact factor list + weights are frozen at review,
  before any PnL is computed — the same frozen-coefficients discipline as `logit-mom-v1`.
- **The residual author-hindsight leak, and its structural consequence (GPT-sharpened, rev 3):** the test
  window is HISTORICAL — its outcomes are known to the authors (an LLM trained through ~2026, and Robin), so
  unlike M7d's genuinely-forward window there is **no forward firewall**, and no attestation can make the
  original factor/window SELECTION hindsight-free (the trio, the equal weights, the top-20/top-100 shape, the
  SIC exclusion are choices among many pre-2010-defensible alternatives that a 2026-knowing author could
  favor without ever running PnL). Pre-2010 citations and no-change-after-first-read bound *subsequent
  iteration*, not the *original selection*. **The consequence is structural, not cosmetic: the historical
  window carries REJECTION authority only** (see Windows) — it can kill the family, it cannot promote it.
  Promotion-carrying evidence is forward-only.

### Trap 2 — survivorship + delisting bias (the reason F−1/F0 exist)

A long-horizon backtest over "today's large caps" is structurally inflated: today's membership is conditioned
on having survived and grown. **Binding contract:**

- The universe at each rebalance is computed **only from PIT-known data** (predeclared rule below): PIT shares
  outstanding (latest filing accepted before cutoff) × PIT price. No index-membership snapshots from today, no
  hand-curated ticker list.
- **The identity map is itself a survivorship surface — and EDGAR alone cannot supply it (GPT-corrected,
  rev 3):** EDGAR filing indices carry filer-level facts (CIK, form, filed date), and the submissions API
  documents current/former *company names* — but a CIK is a FILER identity, not a security identity, and
  nothing in EDGAR provides effective-dated ticker/MIC/share-class validity intervals, primary-listing status,
  delisting effective dates/reasons, or successor mappings. Rev 2's "vintaged mapping from submissions
  formerNames + cross-checks" would have moved survivorship from the current ticker file into an unverifiable
  historical join, not closed it. **Binding requirement: a NAMED, verifiable PIT security master** — permanent
  security id ↔ CIK link, ticker/MIC/class validity intervals, primary-listing status, delisting effective
  date + reason, successor mapping — with an **independent delisted-name recall denominator** to gate against.
  EDGAR-derived enumeration (CIK-first, delisted CIKs retained) is an INPUT to that master, insufficient
  alone; `company_tickers.json` is BARRED as the primary join. If no adequate master exists at $0, the
  paid-data decision comes BEFORE any F0 build (the F−1 phase exists to establish exactly this).
- Names that later delist must **retain price history through delisting**, with a pinned delisting-return
  convention: the final tradable price where available; if a terminal price is unavailable, a predeclared
  conservative haircut (rev-3 proposal: −30% on the last observed price) applied and counted in diagnostics.
  **Booking + classification rules (PIT-safe):** the delisting return is booked at the last-observed-trade
  date; a name is classified "delisted" only per a PIT delisting/corporate-action record. If that record
  becomes available only later, the position is carried at last observed price with a `delisting_pending`
  flag and the haircut is applied at the RECORD's availability date, never retroactively re-booked into
  earlier months. A data gap is never inferred to be a delisting (fake losses) and a delisting is never
  dropped as a gap (survivorship). Diagnostics count M&A/cash-out delistings (where −30% is conservative —
  real outcomes are usually premiums) separately from failure/bankruptcy delistings (where −30% may be
  anti-conservative); the failure-haircut stress gate below exists for the second cohort.
- **The shared convention does NOT cancel in the active legs (rev-2 claim corrected):** the strategy and the
  equal-weight benchmark apply the SAME convention but hold DIFFERENT weights — a failure at 5% strategy
  weight vs 1% benchmark weight contributes ≈ −1.2% to active under a −30% haircut, and a benchmark-only
  failure mechanically flatters the strategy's active. The convention is applied symmetrically for fairness,
  and its base- and stress-case CONTRIBUTIONS are measured and reported per leg, per cohort, at actual
  weights. Identity recall, price-path coverage, and terminal-value uncertainty are gated separately (F0
  gates + gate 7 + gate 10) — the haircut bounds only unknown terminal value.
- **This is exactly where $0 data sources are weakest.** F−1 establishes whether the required security master
  and CA/delisting records exist at $0; F0 measures coverage quantitatively against pinned thresholds; and on
  inadequacy the line **stops or pauses for an explicit Robin paid-data decision** — it does not proceed on a
  survivor-only panel and call the result an edge.

## Evidence Grounding

All claims repo- or doc-cited; re-verify against HEAD before acting.

- **The intraday harness cannot run this line (new build required, cost acknowledged).** `resample_midbars()`
  hard-rejects non-`1m` (`bar_series.py:117-118`), as do `signal_config.py:117-119` and the manifest validators
  (`backtest_historical.py:414-415`, `:526-527`); the M7d packet already pinned that coarser bars are a real
  un-hardcoding plus a strategy-shape confound. A daily-cadence line therefore needs its **own** decision
  harness, its **own** pinned criteria module, AND its **own artifact verifier for a new metric space** — see
  the verifier posture below. The M7 intraday criteria (`paper_phase_criteria.py`) and the M7 verifier stay
  byte-for-byte untouched.
- **Verifier posture — own NEW verifier + fail-closed dispatch (GPT-extended, rev 3):** the M7 v2 verifier's
  metric schema is EXACT-MATCH (`backtest_gate.py` `_V2_METRIC_KEYS`), hard-codes intraday realism keys this
  line does not produce, and pins the intraday benchmark ids; the daily line's gates are a NEW metric space,
  so it gets its **own v2-style verifier + pinned-criteria module** landed as a reviewed commit before any
  screen run — NOT a "tighten-only" change to the M7 verifier. **Dispatch is itself a predeclared contract:**
  today the orchestrator imports and calls ONLY the intraday `verify_artifact`
  (`orchestrator.py:77`, `:2218`) — safe now (fundamentals artifacts fail closed), but any future
  implementation MUST land a **fail-closed verifier registry keyed on the exact strategy id**: intraday ids
  route byte-for-byte through the existing verifier; ONLY the exact fundamentals strategy id routes through
  the daily verifier; unknown or cross-routed ids are rejected. No unreviewed path to an S9 `ok`.
- **Preregistration lock — a committed design digest, not just a manifest hash (GPT blocker, rev 3):**
  hashing a builder-supplied manifest is tamper-EVIDENT but not a preregistration LOCK — a modified body
  re-hashes to a new, internally-consistent value. Binding contract: **before any dev-window PnL is
  computed**, ONE canonical design-body — exact factor formulas + XBRL context-selection pseudocode, source
  ids, PIT/CA/delisting rules, the universe rule, the fill/cost model (formula pinned below), both benchmark
  constructions, ALL gates with their full exact-match metric-key schema, and the explicit decision/fill/mark
  date arrays — is committed to the repo together with its expected digest as an EXTERNAL constant. The daily
  verifier exact-matches every artifact's carried design digest AND design-commit reference against that
  committed constant; TDD includes negative tests proving a re-hashed body with one changed field is
  REJECTED. The M7c universe-block binding is the precedent; this extends it from "carried in provenance" to
  "matched against a pre-run committed constant."
- **The safety spine transfers unchanged.** Run gates (`agent_rules.enabled`, `paper_trading.enabled`,
  `live_trading.enabled`) are `false`; `submit_order()` is reachable only via preflight tokens; S9 requires a
  passing reviewed artifact for any non-synthetic strategy open — a daily-line strategy gets **no paper opens**
  until a reviewed artifact under ITS OWN committed criteria verifies `ok`. `artifacts/backtests/` =
  `.gitkeep`. The `paper_session` strategy registry fail-closed-refuses unwired strategies (the RS-proxy
  refusal is the precedent); the daily line would need its own predeclared runner/adapter, later.
- **Mandate + budget provenance.** `PLAN.md` "Substrate-search budget": a daily-horizon/EOD line is a NEW
  substrate requiring a fresh explicit mandate — granted by Robin 2026-07-10, **scoped to packet drafting +
  review** (recorded in `PLAN.md` Current status and CLAUDE.md). The intraday budget is not consumed by this
  line.
- **$0 data candidates (to be pinned + measured in F−1/F0, not asserted here):**
  - **Fundamentals: SEC EDGAR XBRL — `companyfacts` ONLY for anything PIT.** Each companyfacts fact carries
    `accn`, `filed`, form type, period — a true PIT binding at day granularity ("usable" = max `filed` ≤ cutoff
    among facts for the target `(tag, unit, period)`). The `frames` API is **BARRED from any PIT
    feature/universe value** (frames returns the latest-filed value per entity-period — restatements silently
    overwrite as-originally-reported numbers); frames may be used only for non-decision discovery, labeled as
    such. Bulk mechanics: the SEC's own **`companyfacts.zip` bulk file (daily-refreshed) and the quarterly
    Financial Statement Data Sets are the SANCTIONED F0 backfill mechanism**; the ~10 req/s live API (with a
    descriptive User-Agent) is reserved for incremental PIT updates. "No bulk crawl" in this packet means no
    un-throttled scraping of filing documents — it does NOT bar the SEC-published bulk files.
  - **Prices (daily) — corrected upward by the GPT review (rev 3):** Alpaca's free tier restricts LIVE
    streaming to IEX, but **historical SIP queries are available on the free tier when the request window ends
    ≥ 15 minutes in the past** — so consolidated daily bars (incl. consolidated volume for the ADV screen, and
    daily closes that include the primary auction prints) are a legitimate $0 HISTORICAL candidate; F0
    verifies actual entitlement, how far back coverage reaches, whether the SIP daily-bar close matches the
    primary official close on a sample, and — the real question — **delisted/inactive-symbol coverage**, which
    remains the expected binding inadequacy. The rev-2 claim "IEX runs no closing auction" is softened: IEX
    operates auctions only for IEX-listed securities (currently none), so for NYSE/Nasdaq names an IEX last
    trade is still not the official close — but with SIP-historical available this matters less than rev 2
    assumed. **Databento historical daily OHLCV** stays the Robin-gated consolidated alternative (delisted
    names retained; usage-based cost over thousands of symbol-years is NOT necessarily small ⇒ any pull needs
    Robin's go). A third independent source (e.g. Stooq) is a cross-check only — and serves back-ADJUSTED
    closes whose retroactive factors embed future actions, so any cross-check must first un-adjust to the raw
    basis.
  - **Corporate actions + dividends (first-class F−1/F0 deliverable):** TR accounting for the strategy and
    BOTH benchmarks requires PIT-safe ex-dates + amounts for every universe name and SPY, and the split
    factors for the shares/price series. Candidate $0 sources (Alpaca corporate-actions endpoints;
    cross-checks against EDGAR-declared dividends) have UNKNOWN coverage — F−1 names the source, F0 measures
    coverage and PIT-correctness against pinned thresholds. No adequate $0 CA source ⇒ the same
    pause-for-paid-data branch.
  - **Benchmark: `sp500_total_return_proxy_v1`** — SPY daily total return on the strategy's fill-lagged
    intervals (construction pinned in Benchmark Contracts; reference + tolerance pinned there; the reference
    SERIES' vendor, snapshot hash, field mapping, date alignment, missing-day rule, and the
    cumulative-annualized divergence formula are frozen at F0A review as part of the design digest).
- **Price-adjustment contract (binding, closes the most common lookahead in equity backtests):** all signal
  and return computation uses **as-traded/raw closes**; split factors are applied only for splits with ex-date
  ≤ the point of use; dividends enter only as explicit ex-date events. **Vendor back-adjusted /
  "adjusted-close" series are FORBIDDEN as inputs to any signal, universe rule, or return.** Part of the
  hash-bound design digest.
- **Cost model (formula pinned, rev 3):** per-rebalance transaction cost =
  `cost_t = bps_per_side × Σ_names |target_weight − pretrade_weight| × NAV_t`, applied at fill; the initial
  entry pays full cost on 100% of deployed notional; the terminal mark trades nothing (no synthetic exit
  cost — the terminal position is marked, not sold; stated so the screen cannot harvest a cost-free exit);
  each benchmark pays the same formula on ITS OWN turnover. The cost-stress gate re-prices the same trades at
  the doubled rate in-run; a combined cost+delisting stress is exported as a diagnostic.
- **Cost intuition stated as arithmetic, not vibes:** at the 100% monthly one-way turnover ceiling and the
  pinned 10 bps per side, worst-case cost drag ≈ 0.10% × 2 × 12 ≈ 2.4%/yr; at the expected ~30% monthly
  turnover it is ~0.7%/yr (likely band 10–25% one-way for a monthly top-20-of-100 fundamentals book — the
  planning ceiling stays 40%). The intraday families paid the same order of round-trip cost **per
  30–120-minute trade**. This is the whole reason the horizon move changes the cost regime.
- **Realism posture differs by construction:** fills are modeled at the **next session's close on the pinned
  primary price source** with the pinned cost formula. F0 measures close-fidelity (SIP daily-bar close vs
  primary official close on a sample; close-proxy divergence if any non-SIP source is used) and the measured
  divergence becomes part of the F0 adequacy gate. The realism risk moves into data integrity (PIT
  correctness, CA adjustment, delisting handling, close fidelity), which is exactly what F−1/F0 gate.

## Non-Authorization

Do not do any of the following from this packet alone:

- flip any committed gate or touch `config/risk_rules.json` / `config/agent_rules.json`,
- start the F−1 procurement review, build the F0 data layer, or build the daily harness (each needs Robin's
  separate go; F−1 first),
- make any paid or credentialed data pull (Databento daily included), or any EDGAR access beyond a hard cap of
  **≤ 20 individual companyfacts requests, single-threaded, rate-limit-respecting, with a descriptive
  User-Agent, logged in the review notes** — anything larger (including the bulk-file download) is F0 build
  activity requiring Robin's separate go,
- write to `artifacts/backtests/`, start paper/M8 work, or wire any strategy into `paper_session`,
- relax, scale, or re-index any existing pinned criterion, or edit `paper_phase_criteria.py`,
- add factors, windows, universes, or cadences beyond what review freezes,
- let any LLM output touch a historical-window feature, signal, or universe decision (Trap 1).

## Phase structure (each phase gated; later phases build nothing early)

**F−1 — bounded data-procurement review (NEW in rev 3; no PnL, no build).** A strictly time- and
request-capped assessment (cap proposed at review; EDGAR portion stays inside the ≤ 20-request review cap
above unless Robin extends it) that answers ONE question: **does a survivor-complete PIT security master +
CA/delisting record source exist at $0, and if not, what does an adequate one cost?** Deliverables: the named
candidate master(s) with their field coverage (permanent id, CIK link, ticker/MIC/class validity, listing
status, delisting date+reason, successor mapping); the named CA/dividend source(s); the price-panel decision
input (Alpaca SIP-historical entitlement/coverage check vs Databento vs commercial); and an **independent
delisted-name recall denominator** (a reference cohort of known delistings in the window against which any
master's recall can be measured). Output = a short memo to Robin with the procure / $0-proceed / park
decision. **No F0 engineering before that decision** — security identity, not historical bars, is the binding
constraint, and the sunk-cost order must reflect it.

**F0 — data substrate + PIT contract + calibration (no strategy PnL), SPLIT to remove discretion (GPT
blocker, rev 3):**

- **F0A — calibration + freeze (dev-only data).** Build the pinned-source data layer and measure everything on
  the DEV window only: coverage by year including per-year eligible-name breadth; PIT lag distributions;
  cross-source and close-fidelity divergence; CA/dividend coverage + PIT-correctness; identity-map recall
  against the F−1 denominator; the ADV basis measurement; the universe-rule dry run (membership series, no
  returns). **At the end of F0A, a review FREEZES every number rev 2 left as words:** the reference cohort and
  recall/coverage bounds, allowed missing-session caps, CA-coverage thresholds, sample-selection rules,
  close-divergence tolerances, source/tag precedence, the ADV formula + basis + floor, all fallback caps, the
  benchmark-reference pinning (vendor, snapshot hash, field, date alignment, missing-day rule,
  divergence formula), and the full design digest (Evidence Grounding). Words like "adequate" and "within
  tolerance" have NO force — only the frozen numbers do.
- **F0B — one fail-closed materialization of the SCREEN window.** After the F0A freeze, the screen-window
  panel + universe series is materialized ONCE under the frozen rules. No threshold, precedence, or rule may
  change after any screen-window name is visible; a failure against the frozen F0 gates routes to the Robin
  data decision, never to a rule adjustment.

**F0 gate (all must hold at the frozen numbers, else stop/pause for a Robin data decision):** ≥ 60 monthly
rebalance dates of dev-window coverage with ≥ 80 eligible names each; identity-map recall of delisted names ≥
the frozen bound against the independent denominator; delisted-name price coverage measured and either
adequate or convention-bounded with counted haircuts; CA/dividend coverage adequate and PIT-correct on the
frozen sample rules; close-fidelity divergence within the frozen tolerance; benchmark proxy within the frozen
tolerance of the pinned reference. **Expected-branch honesty:** on $0 sources the most likely failure points
are the identity map and delisted coverage — hitting the pause-for-paid-data branch is a normal, predeclared
outcome, and F−1 exists so it is hit BEFORE the expensive engineering, not after.

**F1 — ONE frozen mechanical cross-sectional family, run ONCE on the retrospective screen.** Strategy id
`fundamentals.xs_value_quality_v1` (shape below). Dev window first (descriptive only); the single-use SCREEN
window only after the family + design digest are frozen. Produces a reviewed v2-style artifact under the NEW
committed daily-line criteria module + verifier (own metric schema, own benchmark ids, fail-closed dispatch
registry — see Evidence Grounding), hash-bound to the predeclared strategy-hypothesis block
`{hypothesis_id: fund_longterm_v0_20260710, universe_selection_rule, factor_list_with_weights, pit_rule_id,
window date arrays}` AND matched against the committed design digest. **Authority of the result (rev 3):** a
screen NULL parks/stops the line per the family budget; a screen PASS is `screen_passed` — explicitly NOT
`ok`-for-paper — and licenses only the forward step: drafting the daily-line PAPER criteria packet (its own
predeclaration + review). S9 semantics are unchanged: no paper open for this line until a reviewed artifact
under the daily line's own committed criteria verifies, and the packet defining those criteria will bind them
to FORWARD evidence, not to this screen.

**F2 — LLM research overlay, FORWARD-ONLY, conditional.** Requires an F1 screen-pass (the overlay needs a
surviving mechanical baseline to beat) OR Robin's separate explicit decision to run it as its own family
against the benchmark alone. Own predeclared packet either way. Never a rescue path for an F1 null, and never
backtested. **Budget accounting (rev 3):** any decision-carrying F2 test CONSUMES the second and final family
slot of this line's two-family budget.

## Predeclared PIT rule (fundamentals usable-from)

**Canonical form (one rule, no paraphrase):** map `filed` to `F = the first trading session on or after
filed`; the fact is usable at decision session `D` **iff `D ≥ F + 1` trading session** (decisions are taken at
the close of `D`; the resulting fill happens at the next session's close, i.e. no earlier than `F + 2`).
Worked example: `filed` Monday (trading day) ⇒ `F` = Monday ⇒ earliest decision = Tuesday close ⇒ fill
Wednesday close. `filed` Saturday ⇒ `F` = Monday ⇒ earliest decision = Tuesday close. This one-session embargo
absorbs acceptance-time-of-day ambiguity (EDGAR acceptance after 17:30 ET is dated the next business day, so
an `F`-dated filing is public well before the close of `F + 1`) without needing per-filing acceptance
timestamps. Amendments (10-K/A etc.) supersede only from their own `filed` date; originals stay binding before
that. The rule is enforced fail-closed in the data layer (a fact without a parseable `filed` is dropped and
counted), and the design digest binds the rule id.

## Predeclared Universe (PIT-computable rule, no snapshots)

At each monthly rebalance decision date `D` (last trading session of the month):

1. Candidate set = all US operating-company common stocks enumerated **CIK-first from EDGAR filing indices**,
   joined to price series through the F−1-named PIT security master (primary listing, share classes grouped
   by CIK/permanent id) with (a) a PIT-usable annual or quarterly filing aged ≤ 400 calendar days at `D`,
   (b) ≥ 252 trading days of price history at `D`, (c) 20-day average daily dollar volume ≥ the pinned floor.
   **ADV basis (frozen at F0A):** the floor is defined on CONSOLIDATED dollar volume ≈ $5M. If SIP-historical
   consolidated volume is available at $0 (see Evidence Grounding) it is the basis; only if F0A shows it is
   not does the frozen IEX-ratio fallback apply. No frozen basis, no screen.
2. **Exclusions (frozen rev 2, literature-grounded, decided before any PnL):** financial companies and REITs
   (SIC 6000–6799) are EXCLUDED — bank/insurer book leverage and REIT depreciation-distorted GAAP earnings are
   exactly the cohorts the pre-2010 value/quality literature excludes (Fama-French exclude financials), and
   they would otherwise concentrate in the factor tails. ETFs and secondary-listing ADRs are **structurally**
   excluded already (ETFs file N-1A/N-CEN with no us-gaap operating fundamentals and are not common stocks;
   the primary-listing requirement drops secondary ADR lines; 20-F/IFRS filers without the pinned us-gaap tags
   fail the PIT-usable-filing requirement and are counted, not silently dropped). Note under Trap 1: this
   exclusion, like every shape choice, sits under the author-hindsight concession.
3. Rank by PIT market cap. **Multi-class convention (pinned):** top-100 MEMBERSHIP is ranked by TOTAL company
   PIT cap = Σ over the CIK's share classes of (class PIT shares × class close(`D`)), using split-consistent
   share counts as of `D`; the tradable instrument is the single highest-ADV class. Where per-class share
   attribution is impossible from companyfacts (the class axis is flattened), the pinned fallback is total
   company shares × the primary class's close, flagged and counted; if even that is ambiguous the name is
   dropped and counted. **Universe = top 100.**
4. **Minimum-eligibility rule:** if < 80 names qualify at `D` (eligible-candidate count BEFORE the top-100
   truncation), the rebalance is SKIPPED (strategy positions held; the equal-weight benchmark holds too), the
   skip counted. If the INITIALIZATION decision (see Windows) skips, the screen starts in cash until the
   first executed rebalance, counted.

The universe series itself is an F0B deliverable, materialized once under frozen rules, so its stability is
inspected **before** any return is attached to it.

## Strategy Shape (F1 — frozen at review, before any PnL)

`fundamentals.xs_value_quality_v1` — long-only, monthly, equal-weight, hand-set weights, no fitting:

- **Factors (rev-3 proposal, pre-2010-literature-grounded, to freeze at review — one pinned citation per
  factor):**
  - **Earnings yield** = trailing-12M net income / total company market cap (Basu 1977). Negative earnings are
    RETAINED and rank monotonically low (no drop, no separate bucket).
  - **Quality = ROA** = trailing-12M net income / latest PIT total assets (Haugen–Baker 1996; Piotroski 2000).
    Replaces rev 1's ROE deliberately: book equity is NEGATIVE for many buyback-heavy mega-caps, where NI>0 /
    BE<0 sign-inverts the score and mechanically fights the issuance factor. Total assets are strictly
    positive. (If review overrides back to ROE, the frozen rule must be: BE ≤ 0 ⇒ the quality-leg z set to 0
    (neutral) and counted — but ROA is the recommended, better-grounded leg.)
  - **Net share issuance** = −(percentage 12-month change in split-adjusted total company PIT shares
    outstanding): `issuance_raw = shares_adj(D) / shares_adj(D − 12M) − 1`, factor value = `−issuance_raw`
    (buybacks score high; Ikenberry–Lakonishok–Vermaelen 1995, Daniel–Titman 2006, Pontiff–Woodgate 2008).
    **Endpoint + adjustment rules (pinned):** both endpoints are the company-aggregate split-consistent share
    count at their own PIT vintage; if the 12M-lookback endpoint lacks a PIT observation within the frozen
    staleness window, the leg is neutral (z = 0) and counted; merger/spin-off-driven share changes are NOT
    netted out in rev 3 (a predeclared simplification — flagged in diagnostics via the CA ledger, candidate
    refinement only for a future family, never adjusted post-hoc). The same split-consistency requirement
    applies to the market-cap rank.
- **TTM stitching + XBRL context binding (pinned, part of the frozen design digest):** trailing-12M net
  income = the sum of the 4 most recent NON-OVERLAPPING quarterly `us-gaap:NetIncomeLoss` durations, each
  independently PIT-usable at `D` at its own amendment-aware vintage, selected on the SAME fiscal chain (no
  mixing across a fiscal-year change), with Q4 derived as annual − 9-month-YTD where discrete Q4 is absent;
  fallback = the latest PIT-usable annual value; if neither is constructible the name is ineligible and
  counted. The manifest binds the FULL XBRL context per used fact — `(cik, accn, filed, tag, unit, period
  start, period end, fy, fp, form, duration)` — and the F0A freeze pins: deterministic selection among
  duplicate facts, 52/53-week-period handling, tag precedence + fallback order for net income, total assets,
  and shares outstanding (mismatches counted, never silently substituted), and the staleness windows.
- **Standardization (pinned order + degenerate cases):** for each factor at `D`, winsorize the RAW value
  cross-sectionally to [median − 3·MAD, median + 3·MAD], THEN compute the z-score
  (`z = (x − mean) / stdev` over the winsorized cross-section); if MAD = 0 or stdev = 0 the factor is neutral
  (z = 0 for all) that month and counted. Composite = equal-weight mean of the three z-scores; a name missing
  a leg uses the mean of its available legs with the miss counted; ties at the top-20 boundary break by
  ticker lexicographic order (deterministic). **No optimization, no per-window fit, no retraining — ever.**
- **Portfolio:** long the **top 20** names equal-notional; no shorts, no leverage, no locate/SSR surface.
  **Fill-shortfall rule (pinned):** if a selected name has no tradable next-session fill, its slot stays in
  CASH until the next rebalance (never implicit reweighting into the other 19), counted in gate 6's
  fill-integrity clause.
- **Rebalance:** decisions at close of `D` on PIT-usable data; fills at the **next session's close on the
  pinned primary source** with the pinned cost formula (10 bps per side, rev-3 proposal). **Corporate-action
  event ledger (pinned):** dividend entitlement follows positions held at the close of the session BEFORE the
  ex-date (ex-date-open buyers get nothing; the TR credit books at ex-date close and reinvests there); splits
  adjust quantity on ex-date with value-neutral bookkeeping; cash mergers pay the recorded consideration at
  the effective date; stock/spin-off consideration converts to the successor per the recorded ratio (fallback:
  cash-in-lieu at the first tradable price, counted); any CA event type without a pinned rule ⇒ fail-closed
  (position frozen at last price + flagged, run NULLs on gate 7's threshold if such names exceed the frozen
  cap). Nothing conditions on any CA before its PIT record date.
- **Turnover cap as a diagnostic, not a knob:** expected ~10–25%/month one-way (planning ceiling 40%);
  measured and reported, never tuned against.

## Predeclared Windows

- **Dev window:** all F0-adequate history from the earliest date passing the F0 coverage gate through
  **2022-12-31** (must contain ≥ 60 monthly rebalances, else the F0 gate already failed). Descriptive context
  only.
- **Embargo:** 2023-01-01 → 2023-06-30 — no outcome from it is ever evaluated. Honest purpose: it buffers the
  dev/screen seam against serial-correlation double-counting and marks the boundary before which no screen
  outcome was inspected; the ATTESTATION below (not the embargo) is the operative snooping control, and
  trailing INPUTS legitimately reach back through it (PIT-legal).
- **Retrospective screen (single-use, REJECTION authority only — reclassified in rev 3):** the window
  **2023-07-01 → 2026-06-30**. Because its outcomes are known to the packet's authors, it is NOT a holdout
  and carries NO promotion authority: a NULL parks/stops the line (family budget consumed); a pass =
  `screen_passed` = "do not kill — begin forward evidence collection" (see F1). It is consumed by ONE run
  under the frozen design digest; no metric from it may motivate a re-test, a new factor, or a window change.
  **Explicit calendar (rev-3 fix of an impossible rev-2 spec):**
  - **Decision dates (37):** the last trading sessions of 2023-06 through 2026-05, where the 2023-06-end
    decision is the INITIALIZATION decision (it consumes only PIT inputs at that date; no pre-2023-07 outcome
    is evaluated; if it skips under the eligibility rule the screen starts in cash), followed by 36 monthly
    rebalance decisions 2023-07-end … 2026-05-end... — correction: initialization (1) + rebalances 2023-07
    … 2026-05 (35) = **36 decisions total**.
  - **Fills (36):** each decision fills at the next session's close — 2023-07-first-session through
    2026-06-first-session.
  - **Terminal mark:** the final position is marked-to-market at the **2026-06-30 close** (nothing trades at
    the terminal; no synthetic exit cost).
  - **Observations (36):** 35 fill-to-fill monthly periods + 1 final fill-to-terminal period — all realized
    strictly inside 2023-07-01 → 2026-06-30. (Rev 2 pinned a 2026-06-end decision "marked at 2026-06-30",
    which is impossible — that decision's fill lands 2026-07-01, AFTER the mark. There is no 2026-06-end
    decision in rev 3.)
  - The exact date arrays (decisions, fills, marks, from the pinned calendar) are materialized into the design
    digest before any PnL.
- **Single-regime caveat (predeclared interpretation limit):** dev (~2017–2022 on $0 prices) and screen
  (2023–2026) both sit inside one post-2016 macro regime — no GFC-scale bear anywhere in the data, so gate 2
  and gate 5's absolute leg are never regime-tested here. A screen pass is ONE macro draw; a second,
  independent reason (beyond author hindsight and sample size) that the screen cannot promote.
- **Attestation (pin before any F1 run):** no configuration of this family — these factors, this universe
  rule, this cadence — has been backtested on ANY window prior to this predeclaration; the factor list was
  selected from pre-2010 public literature and cost/coverage reasoning only; and the residual author-hindsight
  concession in Trap 1 applies in full, which is WHY the screen carries rejection authority only.

## Benchmark Contracts (both gated; a third non-deciding decomposition benchmark added in rev 3)

- **`sp500_total_return_proxy_v1`** (Robin's benchmark, via the tradable proxy): SPY total return marked over
  the **identical fill-lagged intervals as the strategy** (the fill/mark date arrays above), with cash
  dividends reinvested at ex-date close under the same entitlement rule. **Reference + tolerance (frozen at
  F0A):** cross-checked against the S&P 500 TR INDEX (^SP500TR); SPY structurally trails the index by ≈ its
  ~9.45 bps/yr expense ratio (+ small historical cash-drag), so the acceptance band is CENTERED on that
  expected one-signed drag (rev-3 proposal: proxy − index ∈ [−25, −2] bps/yr, annualized from cumulative
  divergence over the overlap sample; exact formula, reference vendor/snapshot-hash/field/date-alignment/
  missing-day rules frozen at F0A). **Explicit concession:** gate 3's market leg is judged against the
  investable SPY proxy — a ~10–15 bps/yr EASIER bar than the uninvestable index TR; active-vs-index is
  exported as a diagnostic so the concession stays visible.
- **`universe_equal_weight_tr_v1`** (family benchmark, the equal-weight analogue): equal-notional TR basket of
  the FULL eligible universe at each rebalance, same fill-lagged marks, same dividend entitlement/accounting,
  same delisting conventions, and the SAME COST FORMULA on **its own** monthly rebalance turnover (membership
  churn + reweight-to-equal drift, on the benchmark's own traded notional) — neither charged the strategy's
  turnover nor cost-free. Per Trap 2: the shared delisting convention is symmetric but does NOT cancel in the
  active leg (different weights); per-leg contributions are measured and reported.
- **`universe_cap_weight_tr_v1` (NEW rev 3 — non-deciding decomposition benchmark):** the cap-weighted TR
  basket of the SAME eligible universe, same conventions/costs. It has NO gate authority; it exists so a
  mixed gate-3 outcome is decomposable instead of narratively over-interpreted: top20-vs-EW100 isolates
  SELECTION, EW100-vs-cap100 isolates WEIGHTING, cap100-vs-SPY isolates UNIVERSE/index effects.

Active return must be strictly positive vs **BOTH** gated benchmarks on the screen. **Routing on a mixed
result (corrected in rev 3 — no causal claim without the decomposition):** any pass/fail mix across the two
gated legs still NULLs the family (both are required), and the recorded cause is **"unresolved mixed
benchmark result"** together with the three-way decomposition above — successor-design routing (weighting vs
selection vs universe) may cite the decomposition, never the two-leg mix alone.

## Metrics And Acceptance Gates (rev-3 proposals — FROZEN AT REVIEW, before any run)

New pinned criteria module + verifier for the daily line (the M7 intraday module and verifier are untouched;
dispatch per the fail-closed registry in Evidence Grounding). A screen PASS is the full conjunction on the
single-use retrospective screen — and yields `screen_passed` (forward-step license), never a promotion:

1. `decision_count == 36` (the pinned initialization + 35 rebalance decisions, 2023-06-end … 2026-05-end,
   from the pinned calendar) AND `skipped_rebalance_count <= 2` AND every EXECUTED decision has
   `eligible_candidate_count >= 80` (counted BEFORE the top-100 truncation; a skipped month contributes no
   eligibility observation). Realized observations == 36 (35 fill-to-fill + 1 fill-to-terminal at the
   2026-06-30 close). A coverage failure here is a DATA-null (routes to the Robin data decision), distinct
   from an edge-null.
2. net TR (after pinned costs) `> 0`
3. active net TR `> 0` vs `sp500_total_return_proxy_v1` **AND** vs `universe_equal_weight_tr_v1` (both on the
   pinned fill-lagged basis)
4. **cost-stress gate:** gate 3 still holds with the pinned per-side cost DOUBLED (20 bps), re-priced in-run
   on the same trades (both gated benchmarks re-priced with their own doubled costs) and stored
5. **drawdown (pinned definition):** `max_drawdown_pct` = maximum peak-to-trough decline of the strategy's
   DAILY total-return NAV (positions marked at each session's close on the pinned source, dividends accrued
   per the entitlement rule; NAV starts at the initialization fill; terminal = the 2026-06-30 mark) `<= 0.35`
   absolute AND `<= 1.25 ×` the drawdown of `universe_equal_weight_tr_v1` on the identical daily-NAV basis
   (the structurally-matched comparator; the SPY-proxy drawdown is a diagnostic)
6. **breadth/concentration:** no single name accounts for > 30% of the sum of net-positive per-name PnL
   contributions (per-name NET PnL aggregated over the screen, matching the M7c/M7d concentration rule), AND
   ≥ 15 of the 20 selected names receive a tradable next-session fill at ≥ 90% of EXECUTED decisions
   (fill-integrity; unfilled slots sit in cash per the pinned rule; skipped months excluded from the
   denominator and counted separately)
7. **delisting-convention exposure (pinned as position-dollar-days):** Σ over haircut-convention positions of
   (position notional × trading-days held) ÷ the same sum over ALL positions `<= 0.05` across the screen —
   else the result is data-artifact-suspect and NULLs regardless of PnL. CA events with no pinned rule count
   toward the same 5% cap.
8. **zero** quality breaches — the five existing counters carry over (reconcile drift, S1 canary, live
   submit, artifact mismatch, unhandled exception), with the offline-structural ones (`s1_canary_breach_count`,
   `live_broker_submit_count` — no broker in this loop) documented as N/A-zero rather than silently "passing"
9. **monthly consistency + temporal concentration (rev-3 hardening — the rev-2 form was passable with 98.1%
   of positive active from ONE month, verified counterexample):** on the 36 realized monthly actives vs
   `universe_equal_weight_tr_v1`: (a) monthly active `> 0` in `>= 20 of 36` months; (b) monthly-active profit
   factor (Σ positive ÷ |Σ negative|) `>= 1.15`; (c) **no single month contributes > 40% of the gross
   positive monthly active sum**; (d) **total active `> 0` in ≥ 2 of the 3 screen years** (Y1 = 2023-07 →
   2024-06, Y2 = 2024-07 → 2025-06, Y3 = 2025-07 → 2026-06). (All four numbers are rev-3 proposals sized for
   36 noisy observations; review must stress-calibrate before freeze.)
10. **failure-delisting haircut stress:** gate 3 still holds with the failure/bankruptcy-cohort haircut
    deepened from −30% to −60% (M&A/cash-out delistings keep their known terminal prices), re-priced in-run
    and stored. Fill-reference stress (close vs VWAP/impact) stays an F0-measured DIAGNOSTIC.
11. **economic-materiality floor (NEW rev 3 — `> 0` is not a business case):** annualized net active TR
    `>= +1.0%/yr` vs BOTH gated benchmarks on the base case (rev-3 proposal — Robin approves the number at
    freeze; the stress gates 4/10 stay at `> 0`). A few-bps "pass" would have negative expected value against
    the build cost and the years-long forward-confirmation clock; this floor makes the screen's pass bar
    economically meaningful, not merely statistical.

**Verifiability contract:** every gate above must be checkable by the daily-line verifier from STORED artifact
metrics alone — no re-running a backtest at verify time. The criteria module pins the FULL exact-match
metric-key schema (gate → `metrics.<section>.<key>` → comparator → threshold — the complete key list, not an
ellipsis) inside the committed design digest, and the harness emits every stress/decomposition field in-run
(e.g. `metrics.cost_stress.active_tr_vs_spy_20bps`, `metrics.cost_stress.active_tr_vs_ew_20bps`,
`metrics.delisting_stress.active_tr_vs_spy_60pct`, `metrics.consistency.max_month_share_of_positive_active`,
`metrics.decomposition.ew100_vs_cap100_tr`). The verifier exact-matches the schema AND the design digest
against the pre-run committed constant (Evidence Grounding), with negative tests.

**Which gates actually bind here (predeclared honesty):** in a 2023–2026 long-only mega-cap window, gates 2,
5-absolute, and parts of 1/6/8 are expected near-free; the decision load sits on gates 3, 4, 9, 10, 11 and
the concentration clauses — and the two gate-3 legs are correlated, so "11 conjunctive gates" must NOT be
sold as 11 independent hurdles. The 36 monthly observations are autocorrelated and single-regime; the
effective independent sample is a handful of episodes. Ex-ante difficulty note for the SPY leg: 2023–2026 was
a cap-weight-growth-led regime in which equal-weight value books broadly lagged the index — the packet does
not adjust the bar for that; it just refuses to pretend the bar is easy.

Diagnostics (never gates): information ratio vs both gated benchmarks, monthly hit rate beyond gate 9's
floor, turnover, factor-leg attribution, per-year actives, dev-vs-screen consistency read, active-vs-^SP500TR
(index), the three-way benchmark decomposition, close-fidelity divergence, M&A-vs-failure delisting split
with per-leg contribution at actual weights, combined cost+delisting stress, fill-reference stress.

**Statistical honesty, predeclared:** 36 monthly observations is a WEAK sample, the window is known history
to the authors, and both facts are structural: the screen can only reject or license forward work. The
forward paper confirmation for a monthly strategy accrues over YEARS (≈ 2 years for ~20 rebalances) — a
`screen_passed` therefore gates only the DRAFTING of the daily-line paper-criteria packet (its own
predeclaration + review; the intraday `paper_phase_criteria.py` cannot evaluate a monthly close-fill book and
is untouched). The dev-window result is descriptive context only; the screen alone decides rejection.

## Stop Rules / Search Budget (this line's own budget, predeclared now)

- At most **two** predeclared strategy families on this substrate before a **documented stop** of the line
  (this packet's F1 composite = family 1). **Family 2, if ever pursued, is NOT predeclared here and is NOT
  auto-authorized by an F1 null:** it requires its own predeclared packet + review + Robin's separate go, on
  a window that is genuinely fresh for it, exactly like this one. An F1 null routes to the documented line
  stop by default. The F2 LLM-forward overlay is conditional on an F1 screen-pass and is NOT a rescue path
  for an F1 null; running F2 despite an F1 null is possible only as Robin's separate explicit decision with
  its own packet — **and any decision-carrying F2 test consumes the second and final family slot** (rev 3).
- An F−1/F0 data-adequacy failure is a **data conclusion, not a strategy conclusion**: the line pauses for an
  explicit Robin paid-data decision (e.g. a survivorship-bias-free commercial security master / CA panel) or
  stops. No backtest on a knowingly survivor-biased panel, ever. Per the sequencing rule, this decision point
  is reached in F−1 — BEFORE the expensive engineering — whenever the $0 sources fail the identity-master
  requirement.
- On an F1 NULL: export the full per-gate table + factor-leg attribution + turnover/cost decomposition + the
  three-way benchmark decomposition (staged-only, gitignored `reports/`), route per the family budget. No
  factor swaps, no threshold moves, no screen-window re-use.
- This line does not touch the intraday budget in either direction: an M7d GO does not rescue this line's
  nulls, and vice versa.

## Verification Before Any Next Step

- Repo on `main`, clean tree; run gates `false`; `artifacts/backtests/` = `.gitkeep` only; offline suite green
  (2000 tests at HEAD `c6f0da8` / rev-2 commit `b6dffa7`; pin the commit so the count stops being a moving
  target).
- This packet has passed the rev-2 multi-lens critique AND the rev-3 GPT review (verdict:
  RECONSIDER-EXPERIMENT + five blockers, all applied here). **The open item is Robin's ROUTING CHOICE:**
  (a) F−1 go (bounded, $0, no build), (b) straight to the paid-data decision, or (c) park the line. F0 build
  authorization can only follow an F−1 outcome + a separate Robin go.
- The F0 build, when (and if) authorized, is TDD offline-first (fixtures; zero network in tests), with the
  live EDGAR/price/CA pulls behind the same UNVERIFIED-fail-closed seam discipline as every other credentialed
  path in this repo, and rate-limit-respecting clients (EDGAR fair-access; bulk backfill via the SEC's
  published bulk files, not per-CIK crawls).
- Nothing in F0/F1 writes to `artifacts/backtests/` until a reviewed artifact verifies under the NEW committed
  daily-line criteria module + verifier + dispatch registry + design digest — all of which must land as
  reviewed commits before the screen run, with the digest committed before any dev PnL.

## Revision History

- **Rev 1 (2026-07-10, `87867fb`):** single-pass draft on Robin's mandate.
- **Rev 2 (2026-07-10, `b6dffa7`):** first adversarial pass — 5 read-only review lenses; 8 blockers + ~23
  majors + ~10 minors applied (canonical PIT rule; CIK-first identity mapping; ADV basis; frames barred; ROA
  for ROE; split-adjusted shares; monthly-consistency gate; metric-key verifiability; own-new-verifier
  posture; hypothesis hash-binding; author-hindsight concession; financials/REITs exclusion; benchmark
  fill-lag + tolerance; embargo rationale; computable gates 1/5/6/7; EW-benchmark own costs; near-free-gates
  honesty; family-2 non-auto-authorization; EDGAR request cap + SEC bulk files; expected-pause honesty;
  delisting booking rules + failure-haircut stress).
- **Rev 3 (2026-07-10, same day):** second adversarial pass — GPT review (gpt-5.6-sol, ultra, read-only
  Codex sandbox; verdict RECONSIDER-EXPERIMENT + 5 blockers, all confirmed on triage and applied):
  (1) the historical window RECLASSIFIED from "holdout" to a REJECTION-authority-only retrospective screen —
  a pass licenses forward evidence collection, never promotion; (2) the $0 identity plan corrected — EDGAR
  cannot supply an effective-dated security master; a NAMED master + independent delisted-recall denominator
  is now a hard prerequisite, established in a NEW bounded F−1 procurement phase that precedes any F0
  engineering (sunk-cost order fixed); (3) F0 split into F0A (dev-only calibration + numeric freeze of every
  formerly-discretionary threshold) and F0B (one fail-closed screen materialization — no rule changes after
  screen names are visible); (4) the impossible rev-2 calendar fixed — initialization decision at 2023-06-end,
  35 rebalances through 2026-05-end, fills next session, terminal mark 2026-06-30, exactly 36 realized
  observations, explicit date arrays in the design digest; (5) the artifact contract upgraded from
  tamper-evident hashing to a PREREGISTRATION LOCK — one canonical design-body + expected digest committed
  before any dev PnL, verifier exact-matches against the external constant with negative tests. Further
  rev-3 corrections: the delisting-convention "cancels in active" claim retracted (weights differ; per-leg
  contributions measured); gate 9 hardened with a max-month-share cap + year-breadth clause (the rev-2 form
  was passable with 98.1% single-month concentration — verified counterexample); NEW gate 11
  economic-materiality floor (≥ +1.0%/yr proposal); NEW non-deciding `universe_cap_weight_tr_v1`
  decomposition benchmark + "unresolved mixed benchmark result" routing (no post-hoc causal stories);
  fail-closed verifier DISPATCH registry predeclared (orchestrator currently imports only the intraday
  verifier); the CA/fill/dividend event-order ledger pinned (entitlement at prior close, non-retroactive
  delisting recognition, cash-slot rule for unfilled selections, unsupported-event fail-closed); factor/XBRL
  definitions made executable-frozen (issuance formula + endpoints, full context binding incl.
  start/end/fy/fp/form/duration, fiscal-chain + 52/53-week + duplicate-fact rules, MAD/stdev degenerate
  cases, tie-breaks); the Alpaca $0 price picture corrected UPWARD (free-tier HISTORICAL SIP with 15-minute
  delay is available — consolidated volume/closes are $0-feasible; identity/delisted/CA coverage remain the
  binding constraints; "IEX runs no closing auction" softened to listings-accurate form); SPY reference +
  cost accounting made reproducible (cost formula pinned incl. initial entry + terminal treatment + benchmark
  costs; reference pinning deferred to the F0A freeze as digest content); F2 explicitly consumes the second
  family slot.
