# Fundamentals Long-Horizon Research Packet (new research line)

- **Date:** 2026-07-10 (rev 2 same day)
- **Status:** DRAFT rev 2 — predeclared research/design packet. Rev 1 was a single-pass draft; rev 2 applies a
  5-lens adversarial critique (PIT/lookahead, $0-data feasibility, statistics/criteria, strategy design,
  governance — 8 blockers + ~23 majors, every finding independently re-verified against the packet text before
  application; see Revision History). NOT yet GPT-reviewed, NOT run-authorized. Robin's mandate (2026-07-10,
  "GO på alle dine anbefalinger") covers **authoring + review of this packet only**. No data spend (even
  $0-rate-limited pulls at scale), no harness build, no credentialed pull, and no run is authorized by this
  document. Every numeric threshold in this draft is a **rev-2 proposal to be frozen at review** — pinned
  before any PnL is computed, exactly like the M7c/M7d discipline.
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
  statistics/criteria/benchmarks, strategy/economic design, governance/repo-consistency), findings deduplicated
  and each independently re-verified against the packet text by the orchestrator before application. Next step
  (inside the mandate): the GPT review handoff mirroring the M7d process
  (`docs/superpowers/reviews/2026-07-10-fundamentals-predeclaration-gpt-review-handoff.md`). Only after GPT
  review + Robin's separate go does phase F0 (data substrate) get built — and F0 itself authorizes no strategy
  PnL.
- **Process guardrails (carried from M7c/M7d, learned the hard way):** dispatched review/critique agents are
  READ-ONLY — no git mutations, no file edits (including to this packet); the tree is git-audited after every
  agent run and non-authored edits reverted; authoring and review stay separate passes. (The rev-2 lens agents
  were structurally read-only — no Edit/Write tools — and the post-run audit was clean.)
- This packet predeclares the phase structure, the anti-lookahead contracts (including the **LLM
  future-knowledge trap**, which is unique to this line), the universe rule, the windows, the benchmark
  contracts, and the stop rules **before** any strategy PnL exists on this substrate.
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

This packet is honest about what it is: a **new research line with real build cost** (the existing backtest
harness is hardcoded 1-minute; see Evidence Grounding), whose first two phases are deliberately cheap and
falsifiable: F0 proves the data substrate exists at acceptable quality for ~$0, F1 tests ONE frozen mechanical
strategy family against pinned criteria on a single-use holdout. **Honesty about F0's odds:** on the $0 price
substrate the ≥60-month dev-coverage gate has near-zero margin (see Evidence Grounding), so the F0 outcome
"pause for an explicit Robin paid-data decision" is an EXPECTED branch, not an exceptional one. The
LLM-research component Robin is ultimately interested in is **F2 and forward-only by construction** — it cannot
be backtested honestly (see the trap below) and therefore must earn its evidence in paper time, benchmarked
against the F1 mechanical baseline.

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
  literature** (value/quality/issuance factors long predate the dev window), and the exact factor list +
  weights are frozen at review, before any PnL is computed — the same frozen-coefficients discipline as
  `logit-mom-v1`.
- **The residual author-hindsight leak, conceded honestly:** this line's holdout is HISTORICAL (it ends
  2026-06-30, days before drafting), so unlike M7d's genuinely-forward fresh window there is **no forward
  firewall**. The no-prior-backtest attestation removes in-repo snooping, but it cannot remove the residual
  optimism that the factor SELECTION among pre-2010 candidates and the WINDOW BOUNDARIES were chosen by
  hindsight-bearing authors (an LLM trained through ~2026, and Robin). We do not claim this residual is zero or
  measurable. It is bounded structurally: one specific pre-2010 citation per factor pinned before any PnL; no
  factor/window change after the first holdout read; and the F1 holdout GO treated as strictly PROVISIONAL —
  routing only to FORWARD paper confirmation, which is the sole leak-free evidence this line can produce. That
  asymmetry versus M7d — not statistical power alone — is why forward paper time is the real test here.

### Trap 2 — survivorship + delisting bias (the reason F0 exists)

A long-horizon backtest over "today's large caps" is structurally inflated: today's membership is conditioned
on having survived and grown. **Binding contract:**

- The universe at each rebalance is computed **only from PIT-known data** (predeclared rule below): PIT shares
  outstanding (latest filing accepted before cutoff) × PIT price. No index-membership snapshots from today, no
  hand-curated ticker list.
- **The identity map is itself a survivorship surface (as dangerous as prices):** the only free SEC
  ticker↔CIK map (`company_tickers.json`) is a CURRENT-STATE snapshot — it omits delisted/deregistered names
  and carries only survivors' current tickers, and recycled tickers mis-attribute history across issuers.
  Building the fundamentals↔price join through it would re-import survivorship at the mapping layer while
  every price file looks complete. Binding rule: candidate enumeration is **CIK-first from EDGAR filing
  indices** (delisted CIKs retained by construction); the historical CIK↔ticker mapping uses vintaged
  sources (the submissions API's `formerNames`/ticker fields and predeclared cross-checks);
  `company_tickers.json` is BARRED as the primary join. F0 must gate on **map membership of delisted names**,
  not only on their price coverage.
- Names that later delist must **retain price history through delisting**, with a pinned delisting-return
  convention: the final tradable price where available; if a terminal price is unavailable, a predeclared
  conservative haircut (rev-2 proposal: −30% on the last observed price) applied and counted in diagnostics.
  **Booking + classification rules (PIT-safe):** the delisting return is booked at the last-observed-trade
  date, never retroactively; a name is classified "delisted" only per a PIT delisting/corporate-action record,
  NEVER inferred from forward price absence (a data gap misread as a delisting injects fake losses; a delisting
  misread as a gap re-imports survivorship). Diagnostics count M&A/cash-out delistings (where −30% is
  conservative — real outcomes are usually premiums) separately from failure/bankruptcy delistings (where −30%
  may be anti-conservative — real failures run far deeper); the failure-haircut stress gate below exists for
  the second cohort.
- **This is exactly where $0 data sources are weakest.** F0's job is to measure it, and F0 has a hard
  data-adequacy gate: if the free sources cannot support a PIT universe with delisted-name coverage — in the
  identity map AND the price panel — the line **stops or pauses for an explicit Robin paid-data decision**; it
  does not proceed on a survivor-only panel and call the result an edge.

## Evidence Grounding

All claims repo- or doc-cited; re-verify against HEAD before acting.

- **The intraday harness cannot run this line (new build required, cost acknowledged).** `resample_midbars()`
  hard-rejects non-`1m` (`bar_series.py:117-118`), as do `signal_config.py:117-119` and the manifest validators
  (`backtest_historical.py:414-415`, `:526-527`); the M7d packet already pinned that coarser bars are a real
  un-hardcoding plus a strategy-shape confound. A daily-cadence line therefore needs its **own** decision
  harness, its **own** pinned criteria module, AND its **own artifact verifier for a new metric space** — see
  the explicit verifier posture below. The M7 intraday criteria (`paper_phase_criteria.py`) and the M7 verifier
  stay byte-for-byte untouched.
- **Verifier posture (corrected from rev 1 — this is NOT "tighten-only"):** the M7 v2 verifier's metric schema
  is EXACT-MATCH (`backtest_gate.py` `_V2_METRIC_KEYS`), hard-codes intraday realism keys
  (`p95_realism_gap_bps`, `max_single_fill_divergence_bps`) this line does not produce, and pins the intraday
  benchmark ids. The daily line's gates are a NEW metric space (TR PnL, benchmark-relative drawdown, delisting
  exposure, cost-stress, monthly consistency — and no realism-gap metric). It therefore gets its **own
  v2-style verifier + pinned-criteria module** — own exact-match metric schema, own benchmark ids
  (`sp500_total_return_proxy_v1`, `universe_equal_weight_tr_v1`), own floors — landed as a reviewed commit
  before any holdout run. Calling that "a tighten-only allow-set change" (rev 1's wording) misapplied a
  load-bearing safety semantic; the M7 intraday verifier is not being widened, reused, or touched.
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
- **$0 data candidates (to be pinned + measured in F0, not asserted here):**
  - **Fundamentals: SEC EDGAR XBRL — `companyfacts` ONLY for anything PIT.** Each companyfacts fact carries
    `accn`, `filed`, form type, period — a true PIT binding at day granularity ("usable" = max `filed` ≤ cutoff
    among facts for the target `(tag, unit, period)`). The `frames` API is **BARRED from any PIT
    feature/universe value**: frames returns one value per entity-period — the latest-filed — so restatements
    silently overwrite as-originally-reported values (lookahead precisely on the names that later restated);
    frames may be used only for non-decision discovery, labeled as such. Bulk mechanics: the SEC's own
    **`companyfacts.zip` bulk file (daily-refreshed) and the quarterly Financial Statement Data Sets are the
    SANCTIONED F0 backfill mechanism** — one download is far gentler on EDGAR fair-access than thousands of
    per-CIK API calls; the ~10 req/s live API (with a descriptive User-Agent) is reserved for incremental PIT
    updates. "No bulk crawl" in this packet means no un-throttled scraping of filing documents — it does NOT
    bar the SEC-published bulk files.
  - **Prices (daily): Alpaca market data** (existing credentials, free tier — IEX-derived; history begins
    ~2016, which combined with the 252-day burn-in makes the earliest usable rebalance ~2017 and leaves the
    ≥60-month dev gate with near-zero margin; delisted-name coverage is likely the binding inadequacy; IEX-only
    prices ≠ consolidated SIP closes, and **IEX runs no closing auction, so an "official close" does not exist
    on this source** — see the fill-price contract below) and/or **Databento historical daily OHLCV** via the
    existing historical-only key (consolidated, delisted names retained; usage-based cost over thousands of
    symbol-years is NOT necessarily small ⇒ **any** such pull needs Robin's go). A third independent source
    (e.g. Stooq) is used only as a cross-check, never as primary — and Stooq serves back-ADJUSTED closes whose
    retroactive factors embed future actions, so any cross-check must first un-adjust to the raw basis.
  - **Corporate actions + dividends (a first-class F0 deliverable, unpinned in rev 1):** TR accounting for the
    strategy and BOTH benchmarks requires PIT-safe ex-dates + amounts for every universe name and SPY, and the
    split factors for the shares/price series. Candidate $0 sources (Alpaca corporate-actions endpoints;
    cross-checks against EDGAR-declared dividends) have UNKNOWN coverage — F0 measures coverage and
    PIT-correctness (ex-date not declaration-date conditioning; no retroactive-adjustment leak) and gates on
    them. No adequate $0 CA source ⇒ the same stop/pause-for-paid-data branch as prices.
  - **Benchmark: `sp500_total_return_proxy_v1`** — SPY daily close-to-close with cash dividends reinvested at
    ex-date close (construction pinned in Benchmark Contracts; F0 cross-checks it against the pinned reference
    there).
- **Price-adjustment contract (binding, closes the most common lookahead in equity backtests):** all signal
  and return computation uses **as-traded/raw closes**; split factors are applied only for splits with ex-date
  ≤ the point of use; dividends enter only as explicit ex-date events. **Vendor back-adjusted /
  "adjusted-close" series are FORBIDDEN as inputs to any signal, universe rule, or return** (a today-adjusted
  2019 close embeds 2019→today actions). This contract is part of the hash-bound F0 data layer.
- **Cost intuition stated as arithmetic, not vibes:** at the 100% monthly one-way turnover ceiling and the
  pinned 10 bps per side, worst-case cost drag ≈ 0.10% × 2 × 12 ≈ 2.4%/yr; at the expected ~30% monthly
  turnover it is ~0.7%/yr (the likely band is 10–25% one-way for a monthly top-20-of-100 fundamentals book —
  the 20–40% planning band is deliberately conservative; lower turnover only shrinks the drag). The intraday
  families paid the same order of round-trip cost **per 30–120-minute trade**. This is the whole reason the
  horizon move changes the cost regime.
- **Realism posture differs by construction:** fills are modeled at the **next session's close on the pinned
  primary price source** with a pinned cost haircut. On the $0 source that close is an IEX last-trade proxy,
  NOT the official auction close — so F0 must MEASURE the close-proxy-vs-official divergence on a sample
  (using any available consolidated reference) and the measured divergence becomes part of the F0 adequacy
  gate; if the proxy is materially biased, the honest fill price requires a consolidated source (a paid-branch
  trigger). The realism risk moves into data integrity (PIT correctness, CA adjustment, delisting handling,
  close-proxy fidelity), which is exactly what F0 gates.

## Non-Authorization

Do not do any of the following from this packet alone:

- flip any committed gate or touch `config/risk_rules.json` / `config/agent_rules.json`,
- build the F0 data layer or the daily harness (review + Robin's separate go first),
- make any paid or credentialed data pull (Databento daily included), or any EDGAR access beyond a hard cap of
  **≤ 20 individual companyfacts requests, single-threaded, rate-limit-respecting, with a descriptive
  User-Agent, logged in the review notes** — the spot-checks needed for review itself; anything larger
  (including the bulk-file download) is an F0 build activity requiring Robin's separate go,
- write to `artifacts/backtests/`, start paper/M8 work, or wire any strategy into `paper_session`,
- relax, scale, or re-index any existing pinned criterion, or edit `paper_phase_criteria.py`,
- add factors, windows, universes, or cadences beyond what review freezes,
- let any LLM output touch a historical-window feature, signal, or universe decision (Trap 1).

## Phase structure (each phase gated; later phases build nothing early)

**F0 — Data substrate + PIT contract + calibration (no strategy PnL).** Deliverables: the pinned-source data
layer (EDGAR companyfacts fundamentals + one primary price source + the CA/dividend source), hash-bound input
manifests binding `(cik, accn, filed, tag, unit, period)` per fundamentals row and per-symbol price-series
digests; the **PIT identity map** (CIK-first enumeration; vintaged CIK↔ticker history; delisted names present
and joinable); the PIT usable-from rule implemented fail-closed; the price-adjustment contract (raw closes +
PIT-dated CA events) enforced; CA/split/dividend coverage and PIT-correctness measured; the delisting-coverage
measurement (map membership AND price panel); the benchmark TR proxy built + cross-checked; the **ADV basis
measurement** (see Universe rule); and a **calibration report** (coverage by year INCLUDING per-year
eligible-name breadth, PIT lag distributions, cross-source price divergence, close-proxy-vs-official
divergence, universe-rule dry run producing membership series WITHOUT any return computation). **F0 gate (all
must hold, else stop/pause for a Robin data decision):** ≥ 60 monthly rebalance dates of dev-window coverage
with ≥ 80 eligible names each; delisted names present in the identity map and their price coverage measured
and either adequate or convention-bounded with counted haircuts; CA/dividend coverage adequate and
PIT-correct on the sample; cross-source close divergence and close-proxy-vs-official divergence within the
pinned tolerances on the sample; benchmark proxy within tolerance of the pinned TR reference. **Expected-branch
honesty:** on $0 sources the most likely failure points are the identity map, delisted price coverage, and the
≥60-month margin — hitting the pause-for-paid-data branch is a normal, predeclared outcome, not a failure of
the line's discipline.

**F1 — ONE frozen mechanical cross-sectional family, backtested (the only backtest in this line).** Strategy id
`fundamentals.xs_value_quality_v1` (shape below). Dev window first; the single-use holdout only after the
family is frozen. Produces a reviewed v2-style artifact under the NEW committed daily-line criteria module +
verifier (own metric schema and benchmark ids — see Evidence Grounding). **Hypothesis hash-binding (M7c
parity):** the F1 artifact's manifest hash-binds a predeclared strategy-hypothesis block —
`{hypothesis_id: fund_longterm_v0_20260710, universe_selection_rule, factor_list_with_weights, pit_rule_id,
dev/embargo/holdout window bounds}` — and the artifact provenance carries it forward (mirroring the M7c
universe-block hardening), so any post-run factor/weight/universe/window change breaks the hash and cannot be
silently cherry-picked. Only a verifying `ok` artifact can unlock any paper step, per S9, and even then paper
entry needs the runbook's remaining steps and Robin's go.

**F2 — LLM research overlay, FORWARD-ONLY, conditional.** Requires an F1 GO (the overlay needs a validated
mechanical baseline to beat) OR Robin's separate explicit decision to run it as its own family against the
benchmark alone. Own predeclared packet either way. Never a rescue path for an F1 null, and never backtested.

## Predeclared PIT rule (fundamentals usable-from)

**Canonical form (one rule, no paraphrase):** map `filed` to `F = the first trading session on or after
filed`; the fact is usable at decision session `D` **iff `D ≥ F + 1` trading session** (decisions are taken at
the close of `D`; the resulting fill happens at the next session's close, i.e. no earlier than `F + 2`).
Worked example: `filed` Monday (trading day) ⇒ `F` = Monday ⇒ earliest decision = Tuesday close ⇒ fill
Wednesday close. `filed` Saturday ⇒ `F` = Monday ⇒ earliest decision = Tuesday close. This one-session embargo
absorbs acceptance-time-of-day ambiguity (EDGAR acceptance after 17:30 ET is dated the next business day, so
an `F`-dated filing is public well before the close of `F + 1`) without needing per-filing acceptance
timestamps. (Rev 1 stated "the open of the second trading session after filed" and "filed + 1 trading day" as
if equivalent; they differ by one session — the canonical form above supersedes both phrasings.) Amendments
(10-K/A etc.) supersede only from their own `filed` date; originals stay binding before that. The rule is
enforced fail-closed in the data layer (a fact without a parseable `filed` is dropped and counted), and the
manifest hash-binds the rule id.

## Predeclared Universe (PIT-computable rule, no snapshots)

At each monthly rebalance decision date `D` (last trading session of the month):

1. Candidate set = all US operating-company common stocks enumerated **CIK-first from EDGAR filing indices**
   (primary listing, share classes grouped by CIK) with (a) a PIT-usable annual or quarterly filing aged ≤ 400
   calendar days at `D`, (b) ≥ 252 trading days of price history at `D`, (c) 20-day average daily dollar
   volume ≥ the pinned floor. **ADV basis (pinned at F0, frozen at review):** the floor is defined on
   CONSOLIDATED dollar volume ≈ $5M. The $0 primary source carries IEX-only volume (~2–3% of consolidated,
   symbol-specific and non-stationary), on which a naive $5M would be a ~30–50× tighter, drifting filter — so
   F0 must either source consolidated volume, or measure the IEX/consolidated ratio and re-derive an
   equivalent IEX-basis floor, and the chosen basis + number freeze at review. No basis, no screen.
2. **Exclusions (frozen rev 2, literature-grounded, decided before any PnL):** financial companies and REITs
   (SIC 6000–6799) are EXCLUDED — bank/insurer book leverage and REIT depreciation-distorted GAAP earnings are
   exactly the cohorts the pre-2010 value/quality literature excludes (Fama-French exclude financials), and
   they would otherwise concentrate in the factor tails. ETFs and secondary-listing ADRs are **structurally**
   excluded already (ETFs file N-1A/N-CEN with no us-gaap operating fundamentals and are not common stocks;
   the primary-listing requirement drops secondary ADR lines; 20-F/IFRS filers without the pinned us-gaap tags
   fail the PIT-usable-filing requirement and are counted, not silently dropped). Rev 1's "not excluded"
   sentence was misleading on ETFs/ADRs (they never could qualify) and is superseded by this rule.
3. Rank by PIT market cap. **Multi-class convention (pinned):** top-100 MEMBERSHIP is ranked by TOTAL company
   PIT cap = Σ over the CIK's share classes of (class PIT shares × class close(`D`)), using split-consistent
   share counts as of `D`; the tradable instrument is the single highest-ADV class. Where per-class share
   attribution is impossible from companyfacts (the class axis is flattened), the pinned fallback is total
   company shares × the primary class's close, flagged and counted; if even that is ambiguous the name is
   dropped and counted. **Universe = top 100.**
4. **Minimum-eligibility rule:** if < 80 names qualify at `D` (eligible-candidate count BEFORE the top-100
   truncation), the rebalance is SKIPPED (strategy positions held; the equal-weight benchmark holds too), the
   skip counted — mirroring the M7c ≥-8-of-10 rule.

The universe series itself is an F0 deliverable (dry run), so its stability is inspected **before** any return
is attached to it.

## Strategy Shape (F1 — frozen at review, before any PnL)

`fundamentals.xs_value_quality_v1` — long-only, monthly, equal-weight, hand-set weights, no fitting:

- **Factors (rev-2 proposal, pre-2010-literature-grounded, to freeze at review — one pinned citation per
  factor):**
  - **Earnings yield** = trailing-12M net income / total company market cap (Basu 1977). Negative earnings are
    RETAINED and rank monotonically low (no drop, no separate bucket) — the denominator is strictly positive,
    so the ratio is well-behaved.
  - **Quality = ROA** = trailing-12M net income / latest PIT total assets (Haugen–Baker 1996; Piotroski 2000
    uses ROA as its first signal). **This replaces rev 1's ROE deliberately:** book equity is NEGATIVE for
    many buyback-heavy mega-caps (HD, MCD, SBUX cohort), where NI>0 / BE<0 sign-inverts the score, runs to −∞
    as BE crosses zero, and mechanically fights the issuance factor on the same names. Total assets are
    strictly positive and monotonic. (If review overrides back to ROE, the frozen rule must be: BE ≤ 0 ⇒
    the quality-leg z set to 0 (neutral) and counted — but ROA is the recommended, better-grounded leg.)
  - **Net share issuance** = negative 12-month change in **split-adjusted** PIT shares outstanding (buybacks
    score high; Ikenberry–Lakonishok–Vermaelen 1995, Daniel–Titman 2006, Pontiff–Woodgate 2008). **The shares
    series MUST be split-adjusted with the same PIT split factors as prices (ex-date ≤ D)** — on raw
    `dei:EntityCommonStockSharesOutstanding` the 2020–2024 mega-cap split wave (AAPL 4:1, TSLA 5:1+3:1, GOOGL
    20:1, AMZN 20:1, NVDA 10:1) reads as massive fake issuance and floors exactly the largest names; reverse
    splits fake buybacks. The same split-consistency requirement applies to the market-cap rank (shares aged ≤
    400 days × post-split price is wrong by the split factor).
- **TTM stitching + tag precedence (pinned, part of the frozen factor definition):** trailing-12M net income =
  the sum of the 4 most recent non-overlapping quarterly `us-gaap:NetIncomeLoss` facts, each independently
  PIT-usable at `D` at its own amendment-aware vintage, with Q4 derived as annual − 9-month-YTD where discrete
  Q4 is absent; fallback = the latest PIT-usable annual 10-K value; if neither is constructible the name is
  ineligible and counted. Tag precedence for each input (net income; total assets; shares outstanding) is
  pinned in F0 before any PnL, with fallbacks ordered and mismatches counted, never silently substituted.
- **Standardization (pinned order):** for each factor at `D`, winsorize the RAW value cross-sectionally to
  [median − 3·MAD, median + 3·MAD], THEN compute the z-score; no re-clip or re-standardization after. (Rev 1's
  "z-score (winsorized at ±3)" was ambiguous, and clipping z AFTER computing σ on fat-tailed raw values lets a
  single outlier compress everyone else's scores.) Composite = equal-weight mean of the three z-scores.
  **No optimization, no per-window fit, no retraining — ever.**
- **Portfolio:** long the **top 20** names equal-notional; no shorts, no leverage, no locate/SSR surface.
- **Rebalance:** decisions at close of `D` on PIT-usable data; fills at the **next session's close on the
  pinned primary source** (see the close-proxy contract in Evidence Grounding) with the pinned cost haircut
  (rev-2: 10 bps per side on traded notional); dividends credited on ex-date and reinvested at that day's
  close (TR accounting only — nothing conditions on a dividend before its ex-date).
- **Turnover cap as a diagnostic, not a knob:** expected ~10–25%/month one-way (planning ceiling 40%);
  measured and reported, never tuned against.

## Predeclared Windows

- **Dev window:** all F0-adequate history from the earliest date passing the F0 coverage gate through
  **2022-12-31** (must contain ≥ 60 monthly rebalances, else the F0 gate already failed — and on $0 prices
  this margin is near-zero; see the expected-branch honesty note).
- **Embargo:** 2023-01-01 → 2023-06-30 — never used for any evaluation. **Honest purpose (rev 1's rationale
  corrected):** the embargo does NOT break trailing-INPUT overlap (12-month trailing features at the first
  holdout decision legitimately reach back into dev-window dates — reusing old PIT data in a later decision is
  PIT-legal, not leakage), and with a frozen, unfitted rule there is no learned parameter for it to firewall.
  It buffers the dev/holdout seam against serial-correlation double-counting and marks the boundary before
  which no holdout outcome was inspected; the ATTESTATION below is the operative snooping control.
- **Holdout (single-use, decision-carrying):** **2023-07-01 → 2026-06-30** — decision dates are the 36
  month-ends 2023-07 … 2026-06; untouched until the family is frozen post-review; consumed by ONE run; no
  metric from it may motivate a re-test, a new factor, or a window change. **Terminal mark (pinned):** the
  36th (2026-06) decision's position is marked-to-market at the 2026-06-30 close, so the run realizes exactly
  36 monthly return observations fully inside the window. Any later family needs a brand-new never-inspected
  window (which, on a monthly cadence, means real elapsed time — acknowledged: this line's iteration clock is
  slow BY DESIGN).
- **Single-regime caveat (predeclared interpretation limit):** dev (~2017–2022 on $0 prices) and holdout
  (2023–2026) both sit inside one post-2016 macro regime — no GFC-scale bear is in-sample or in-holdout, so
  gate 2 and gate 5's absolute leg are never regime-tested here. Even a clean conjunctive pass is ONE macro
  draw; this is a second, independent reason (beyond sample size) that any GO is provisional.
- **Attestation (pin before any F1 run):** no configuration of this family — these factors, this universe
  rule, this cadence — has been backtested on ANY window prior to this predeclaration; the factor list was
  selected from pre-2010 public literature and cost/coverage reasoning only; and the residual author-hindsight
  concession in Trap 1 applies in full (the authors know 2023–2026's outcomes; the structural bounds there —
  pinned citations, no post-read changes, PROVISIONAL routing — are the mitigation, not a claim of zero leak).

## Benchmark Contracts (both gated, mirroring the dual-benchmark discipline)

- **`sp500_total_return_proxy_v1`** (Robin's benchmark, via the tradable proxy): SPY total return marked over
  the **identical fill-lagged intervals as the strategy** (fill-close(D+1) → fill-close(next-D+1); rev 1's
  bare "close-to-close" would have shifted the proxy one session earlier than the book and biased the deciding
  active-return gate on 36 observations), with cash dividends reinvested at ex-date close. **Reference +
  tolerance (pinned):** cross-checked against the S&P 500 TR INDEX (^SP500TR); SPY structurally trails the
  index by ≈ its ~9.45 bps/yr expense ratio (+ small historical cash-drag), so the acceptance band is
  CENTERED on that expected one-signed drag (rev-2 proposal: proxy − index ∈ [−25, −2] bps/yr cumulative-
  annualized on the overlap sample) rather than a symmetric ±0.15%/yr that would mask construction bugs of
  the same size as the known drag. **Explicit concession:** gate 3's market leg is judged against the
  investable SPY proxy, which is a ~10–15 bps/yr EASIER bar than the uninvestable index TR; the active-vs-
  index figure is exported as a diagnostic so the concession stays visible.
- **`universe_equal_weight_tr_v1`** (family benchmark, the equal-weight analogue): equal-notional TR basket of
  the FULL eligible universe at each rebalance, same fill-lagged marks (next-session close on the same
  source), same dividend accounting, same delisting conventions as the strategy (the −30% haircut convention
  binds BOTH legs identically, so it cancels in the equal-weight active; it is N/A to the SPY ETF proxy).
  **Cost basis (pinned; rev 1 was silent):** the equal-weight benchmark pays the same 10 bps/side on **its
  own** monthly rebalance turnover — membership churn plus the reweight-to-equal drift trade, on the
  benchmark's own traded notional; it is neither charged the strategy's turnover nor treated as cost-free.

Active return must be strictly positive vs **BOTH** on the holdout. **What each leg isolates (routing on a
mixed result is predeclared):** the equal-weight leg is the clean SELECTION test (did the chosen 20 beat the
average of the 100 under identical conventions); the SPY leg additionally embeds the equal-weight-vs-cap-weight
WEIGHTING tilt. A holdout where the equal-weight leg passes but the SPY leg fails is recorded as "selection
works; the equal-weight structure lost to cap-weight in this regime" — a WEIGHTING conclusion that still
NULLs the family as specified (both legs are required), but routes any successor family design toward the
weighting scheme, not toward new factors. An equal-weight-leg fail is a selection null, full stop.

## Metrics And Acceptance Gates (rev-2 proposals — FROZEN AT REVIEW, before any run)

New pinned criteria module + verifier for the daily line (the M7 intraday module and verifier are untouched).
GO is the full conjunction on the single-use holdout:

1. `decision_rebalance_count == 36` (the pinned month-ends 2023-07 … 2026-06 from the pinned calendar) AND
   `skipped_rebalance_count <= 2` AND every EXECUTED rebalance has `eligible_candidate_count >= 80` (counted
   BEFORE the top-100 truncation; a skipped month contributes no eligibility observation). The first 35
   decisions realize full month-over-month returns; the 36th is marked-to-market at the 2026-06-30 close → 36
   monthly return observations. (Rev 1's `rebalance_count >= 36` was simultaneously tautological on the
   schedule and impossible under any skip; this form separates a coverage-null from an edge-null.)
2. net TR (after pinned costs) `> 0`
3. active net TR `> 0` vs `sp500_total_return_proxy_v1` **AND** vs `universe_equal_weight_tr_v1` (both on the
   fill-lagged basis above)
4. **cost-stress gate:** gate 3 still holds with the pinned per-side cost DOUBLED (20 bps), re-priced
   in-run and stored (see the metric-key schema note)
5. **drawdown (pinned definition):** `max_drawdown_pct` = maximum peak-to-trough decline of the strategy's
   DAILY total-return NAV (positions marked at each session's close on the pinned source, dividends accrued
   at ex-date; NAV starts at the first fill; the terminal position marks at 2026-06-30) `<= 0.35` absolute
   AND `<= 1.25 ×` the drawdown of `universe_equal_weight_tr_v1` on the identical daily-NAV basis (the
   structurally-matched equal-weight comparator — rev 1 compared against cap-weight SPY, which structurally
   draws down less than ANY equal-weight book in risk-off and would have failed the weighting scheme rather
   than the selection; the SPY-proxy drawdown is exported as a diagnostic)
6. **breadth/concentration:** no single name accounts for > 30% of the sum of net-positive per-name PnL
   contributions (per-name NET PnL aggregated over the holdout, matching the M7c/M7d concentration rule; rev
   1's "positive active PnL" was not per-name-decomposable against an index), AND ≥ 15 of the 20 selected
   names receive a tradable next-session fill at ≥ 90% of EXECUTED rebalances (a fill-integrity check —
   rev 1's "slots filled" phrasing was near-tautological on a top-100 universe; skipped months are excluded
   from the denominator and counted separately)
7. **delisting-convention exposure (pinned as position-dollar-days):** Σ over haircut-convention positions of
   (position notional × trading-days held) ÷ the same sum over ALL positions `<= 0.05` across the holdout —
   else the result is data-artifact-suspect and NULLs regardless of PnL (rev 1's "5% of total gross exposure"
   had three incompatible readings)
8. **zero** quality breaches — the five existing counters carry over (reconcile drift, S1 canary, live
   submit, artifact mismatch, unhandled exception), with the offline-structural ones (`s1_canary_breach_count`,
   `live_broker_submit_count` — no broker in this loop) documented as N/A-zero rather than silently "passing"
9. **monthly consistency (new in rev 2 — restores the per-observation floor the M7 module has via
   PF/avg-trade gates):** month-over-month active total return vs `universe_equal_weight_tr_v1` is `> 0` in
   `>= 20 of 36` months, AND the monthly-active profit factor (Σ positive monthly actives ÷ |Σ negative
   monthly actives|) is `>= 1.15` — both on the 36 realized monthly actives with the pinned terminal mark.
   Without this, one lumpy month could carry the entire cumulative GO on gates 2–4 while the strategy was
   noise in the other 35. (20/36 and 1.15 are rev-2 proposals sized for 36 noisy observations — deliberately
   below the intraday per-trade analogues; review/GPT should stress-calibrate both before freeze.)
10. **failure-delisting haircut stress:** gate 3 still holds with the failure/bankruptcy-cohort haircut
    deepened from −30% to −60% (M&A/cash-out delistings keep their known terminal prices). Fill-reference
    stress (close-proxy vs VWAP/impact) is an F0-measured DIAGNOSTIC, not a gate — no $0 source can price it
    honestly in-run.

**Verifiability contract (new in rev 2):** every gate above must be checkable by the daily-line verifier from
STORED artifact metrics alone — no re-running a backtest at verify time. The criteria module pins a metric-key
schema (gate → `metrics.<section>.<key>` → comparator → threshold) at review, and the harness emits the
stress-gate fields in-run (e.g. `metrics.cost_stress.active_tr_vs_spy_20bps`,
`metrics.cost_stress.active_tr_vs_ew_20bps`, `metrics.delisting_stress.active_tr_vs_spy_60pct`, …). Gate 4/10
without stored fields would be unverifiable prose — exactly what the M7d metric-key discipline exists to
prevent.

**Which gates actually bind here (predeclared honesty):** in a 2023–2026 long-only mega-cap window, gates 2,
5-absolute, and parts of 1/6/8 are expected to be near-free (a trending book clears them by construction);
the decision load sits on gates 3, 4, 9, 10 and the concentration clause of 6 — and the two legs of gate 3
are correlated, so "10 conjunctive gates" must NOT be sold as 10 independent hurdles. The 36 monthly
observations are autocorrelated and single-regime (see Windows), so the effective independent sample is a
handful of episodes, not 36. Ex-ante difficulty note for the SPY leg: 2023–2026 was a cap-weight-growth-led
regime in which equal-weight value books broadly lagged the index — the packet does not adjust the bar for
that (the gate is the gate), it just refuses to pretend the bar is easy.

Diagnostics (never gates): information ratio vs both benchmarks, monthly hit rate (beyond gate 9's floor),
turnover, factor-leg attribution, per-year actives, dev-vs-holdout consistency read, active-vs-^SP500TR
(index) comparison, close-proxy divergence, M&A-vs-failure delisting split, fill-reference stress.

**Statistical honesty, predeclared:** 36 monthly observations is a WEAK sample — a conjunctive pass is
supporting evidence, not proof, and (mirroring M7d) even a clean F1 GO is **PROVISIONAL**: it routes to
paper-phase forward confirmation, and for a MONTHLY strategy that forward confirmation accrues over YEARS
(≈ 2 years for ~20 rebalances), far slower than M7d's weeks — so an F1 GO gates only a paper START. The
intraday `paper_phase_criteria.py` CANNOT evaluate a monthly close-fill book (its floors are per-session and
realism-gap-denominated); the daily line's paper-phase criteria are a separately predeclared, separately
reviewed module, out of scope for this packet. The dev-window result is descriptive context only; the holdout
alone decides.

## Stop Rules / Search Budget (this line's own budget, predeclared now)

- At most **two** predeclared strategy families on this substrate before a **documented stop** of the line
  (this packet's F1 composite = family 1). **Family 2, if ever pursued, is NOT predeclared here and is NOT
  auto-authorized by an F1 null:** it requires its own predeclared packet + review + Robin's separate go, on
  a fresh never-inspected window (which on this cadence means real elapsed time), exactly like this one. An
  F1 null routes to the documented line stop by default, not to an automatic family-2 build. The F2
  LLM-forward overlay is conditional on an F1 GO and is NOT a rescue path for an F1 null; running F2 despite
  an F1 null is possible only as Robin's separate explicit decision with its own packet.
- An F0 data-adequacy failure is a **data conclusion, not a strategy conclusion**: the line pauses for an
  explicit Robin paid-data decision (e.g. a survivorship-bias-free commercial panel) or stops. No backtest on
  a knowingly survivor-biased panel, ever. (Per the expected-branch honesty above, this is the LIKELY first
  outcome on pure-$0 sources — reaching it is the discipline working, not failing.)
- On an F1 NULL: export the full per-gate table + factor-leg attribution + turnover/cost decomposition +
  the gate-3 leg decomposition (selection vs weighting, per the Benchmark Contracts routing) (staged-only,
  gitignored `reports/`), route per the family budget. No factor swaps, no threshold moves, no holdout
  re-use.
- This line does not touch the intraday budget in either direction: an M7d GO does not rescue this line's
  nulls, and vice versa.

## Verification Before Handoff

Before this packet is acted on (review pass, then any build):

- Repo on `main`, clean tree; run gates `false`; `artifacts/backtests/` = `.gitkeep` only; offline suite green
  (2000 tests at HEAD `d0cc87f`; 1997 at the rev-1 drafting commit `ed557ce` — pin the commit so the count
  stops being a moving target).
- This packet has passed the rev-2 adversarial multi-lens critique (done, this revision) + the GPT review,
  revisions applied, and Robin has given a **separate explicit go for F0** (scoped: $0 sources only; any
  Databento daily pull is its own ask).
- The F0 build, when authorized, is TDD offline-first (fixtures; zero network in tests), with the live EDGAR/
  price/CA pulls behind the same UNVERIFIED-fail-closed seam discipline as every other credentialed path in
  this repo, and rate-limit-respecting clients (EDGAR fair-access; bulk backfill via the SEC's published bulk
  files, not per-CIK crawls).
- Nothing in F0/F1 writes to `artifacts/backtests/` until a reviewed artifact verifies `ok` under the NEW
  committed daily-line criteria module + verifier — which must themselves land as reviewed commits before the
  holdout run.

## Revision History

- **Rev 1 (2026-07-10, `ed557ce`):** single-pass draft on Robin's mandate.
- **Rev 2 (2026-07-10):** first adversarial pass applied — 5 read-only review lenses; 8 blockers + ~23 majors
  + ~10 minors, deduplicated, each independently re-verified against the packet text before application.
  Blockers fixed: (1) the PIT usable-from rule was self-contradictory by one session → canonical single form;
  (2) the CIK↔ticker identity map was a survivorship re-import (current-snapshot join) → CIK-first
  enumeration + vintaged mapping + F0 map-membership gate; (3) the ADV screen had no volume basis and would
  be ~30–50× too tight on IEX volume → consolidated basis pinned, F0 measures; (4) companyfacts/frames were
  conflated → frames barred from PIT values; (5) ROE sign-inverts on negative book equity → quality leg is
  ROA; (6) the shares series was not split-adjusted → issuance factor AND market-cap rank now bind to
  split-consistent shares; (7) no per-observation consistency gate → gate 9 (monthly hit + monthly-active PF);
  (8) gates were prose, gate 4 unverifiable from a stored artifact → metric-key schema + in-run stress fields
  contract. Majors include: raw-close/no-back-adjusted-series contract; CA/dividend source as a first-class
  F0 deliverable+gate; the embargo rationale corrected; the author-hindsight residual conceded (historical
  holdout, no forward firewall); SPY-proxy fill-lag marking + pinned ^SP500TR reference with a drag-centered
  asymmetric tolerance + the proxy-vs-index concession made explicit; TTM stitching/tag precedence pinned;
  multi-class total-company cap; financials/REITs excluded (SIC 6000–6799, literature-grounded) and the
  ETF/ADR sentence corrected to structural exclusion; MAD-winsorize-then-z pinned; "tighten-only verifier"
  corrected to an own-new-verifier posture; the M7c-parity hypothesis-block hash-binding added; gate 1/5/6/7
  re-specified exactly; equal-weight benchmark pays its own turnover; gate-3 leg routing predeclared;
  near-free-gates / effective-N / single-regime honesty added; paper-phase criteria declared out-of-scope
  (new module required); family-2 slot explicitly non-auto-authorized; EDGAR spot-check hard cap (≤ 20
  requests) + SEC bulk files sanctioned for F0; expected-pause honesty for F0 on $0 sources; delisting
  booking/classification rules + M&A-vs-failure split + failure-haircut stress gate 10; citation fix
  (`bar_series.py:117-118`) and test-count refresh (2000 @ `d0cc87f`).
