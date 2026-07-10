# Fundamentals Long-Horizon Research Packet (new research line)

- **Date:** 2026-07-10
- **Status:** DRAFT rev 1 — predeclared research/design packet, NOT yet adversarially reviewed, NOT
  run-authorized. Robin's mandate (2026-07-10, "GO på alle dine anbefalinger") covers **authoring + review of
  this packet only**. No data spend (even $0-rate-limited pulls at scale), no harness build, no credentialed
  pull, and no run is authorized by this document. Every numeric threshold in this draft is a **rev-1 proposal
  to be frozen at review** — pinned before any PnL is computed, exactly like the M7c/M7d discipline.
- **Relation to the intraday program:** this is the **fresh explicit mandate** contemplated by `PLAN.md`'s
  substrate-search budget ("any continuation — including a daily-horizon/EOD family line, which is a NEW
  substrate — requires a fresh explicit mandate from Robin"). It is a **separate research line** on a genuinely
  different substrate (daily cadence, point-in-time fundamentals, months-scale holds). It does **NOT** consume
  the intraday substrate budget (M7d remains substrate experiment 1 of 2 on that line), does not depend on
  M7d's outcome, and inherits the full safety spine unchanged (gates, S1, journal, artifact discipline, S9).
- **Hypothesis id:** `fund_longterm_v0_20260710`.
- **Benchmark (Robin-set):** S&P 500 **total return**.

## Review Disposition

- DRAFT rev 1, authored single-pass by Claude on Robin's mandate. Next step (inside the mandate): an
  adversarial multi-lens critique + a GPT review handoff mirroring the M7d process
  (`docs/superpowers/reviews/2026-06-26-M7d-predeclaration-gpt-review-handoff.md` is the template). Only after
  review + Robin's separate go does phase F0 (data substrate) get built — and F0 itself authorizes no strategy
  PnL.
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
are amortized over months instead of minutes (a 10-bps round trip against a monthly-rebalance book is ~1.2%/yr
of drag, versus the same 10 bps *per 30–120 minute trade* that killed the intraday families), (b) the decision
cadence (daily) makes fill realism a solvable modeling problem instead of the binding constraint, and (c) the
program's actual tooling advantage — disciplined PIT data engineering and, later, LLM-assisted research —
points away from latency competition entirely.

This packet is honest about what it is: a **new research line with real build cost** (the existing backtest
harness is hardcoded 1-minute; see Evidence Grounding), whose first two phases are deliberately cheap and
falsifiable: F0 proves the data substrate exists at acceptable quality for ~$0, F1 tests ONE frozen mechanical
strategy family against pinned criteria on a single-use holdout. The LLM-research component Robin is ultimately
interested in is **F2 and forward-only by construction** — it cannot be backtested honestly (see the trap
below) and therefore must earn its evidence in paper time, benchmarked against the F1 mechanical baseline.

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

### Trap 2 — survivorship + delisting bias (the reason F0 exists)

A long-horizon backtest over "today's large caps" is structurally inflated: today's membership is conditioned
on having survived and grown. **Binding contract:**

- The universe at each rebalance is computed **only from PIT-known data** (predeclared rule below): PIT shares
  outstanding (latest filing accepted before cutoff) × PIT price. No index-membership snapshots from today, no
  hand-curated ticker list.
- Names that later delist must **retain price history through delisting**, with a pinned delisting-return
  convention (final tradable price; if a terminal price is unavailable, a predeclared conservative haircut —
  rev-1 proposal: −30% on the last observed price — applied and counted in diagnostics).
- **This is exactly where $0 data sources are weakest.** F0's job is to measure it, and F0 has a hard
  data-adequacy gate: if the free sources cannot support a PIT universe with delisted-name coverage, the line
  **stops or pauses for an explicit Robin paid-data decision** — it does not proceed on a survivor-only panel
  and call the result an edge.

## Evidence Grounding

All claims repo- or doc-cited; re-verify against HEAD before acting.

- **The intraday harness cannot run this line (new build required, cost acknowledged).** `resample_midbars()`
  hard-rejects non-`1m` (`bar_series.py:114-115`), as do `signal_config.py:117-119` and the manifest validators
  (`backtest_historical.py:414-415`, `:526-527`); the M7d packet already pinned that coarser bars are a real
  un-hardcoding plus a strategy-shape confound. A daily-cadence line therefore needs its **own** decision
  harness and its **own** pinned criteria module — built new, under the same conventions (deterministic JSON,
  Decimal-as-string, hash-bound manifests, fail-closed writers, v2 artifact verifier with reviewed,
  tighten-only allow-set changes). The M7 intraday criteria (`paper_phase_criteria.py`) stay byte-for-byte
  untouched.
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
  - **Fundamentals: SEC EDGAR XBRL company facts** (`data.sec.gov` companyfacts/frames APIs; free; fair-access
    rate limit ~10 req/s with a User-Agent). Each fact carries `accn`, `filed`, form type, period — a true PIT
    binding at **day granularity**. Conservative usable-from rule pinned below. Shares outstanding
    (`dei:EntityCommonStockSharesOutstanding` and the XBRL equivalents) supports the PIT universe rule.
  - **Prices (daily): Alpaca market data** (existing credentials, free tier — IEX-derived; coverage depth and
    delisted-name coverage are OPEN questions F0 measures; known caveat: IEX-only prices ≠ consolidated SIP
    closes, divergence must be measured on a sample) and/or **Databento historical daily OHLCV** via the
    existing historical-only key (consolidated; small per-pull credit cost ⇒ **any** such pull needs Robin's
    go). A third independent source (e.g. Stooq) is used only as a cross-check, never as primary.
  - **Benchmark: `sp500_total_return_proxy_v1`** — SPY daily close-to-close with cash dividends reinvested at
    ex-date close (construction pinned in Benchmark Contracts; F0 cross-checks it against an independent
    published TR reference within a pinned tolerance).
- **Cost intuition stated as arithmetic, not vibes:** at 100% monthly one-way turnover ceiling (rev-1 cap
  proposal is lower) and a pinned 10 bps per side, worst-case cost drag ≈ 0.10% × 2 × 12 ≈ 2.4%/yr; at the
  expected ~30% monthly turnover it is ~0.7%/yr. The intraday families paid the same order of round-trip cost
  **per 30–120-minute trade**. This is the whole reason the horizon move changes the cost regime.
- **Realism posture differs by construction:** fills are modeled at the **next session's official close** with
  a pinned cost haircut — no touch-quote realism gap, no L1 book dependence. The realism risk moves into data
  integrity (PIT correctness, CA adjustment, delisting handling), which is exactly what F0 gates.

## Non-Authorization

Do not do any of the following from this packet alone:

- flip any committed gate or touch `config/risk_rules.json` / `config/agent_rules.json`,
- build the F0 data layer or the daily harness (review + Robin's separate go first),
- make any paid or credentialed data pull (Databento daily included), or any bulk EDGAR crawl beyond the
  handful of spot-checks needed for review itself,
- write to `artifacts/backtests/`, start paper/M8 work, or wire any strategy into `paper_session`,
- relax, scale, or re-index any existing pinned criterion, or edit `paper_phase_criteria.py`,
- add factors, windows, universes, or cadences beyond what review freezes,
- let any LLM output touch a historical-window feature, signal, or universe decision (Trap 1).

## Phase structure (each phase gated; later phases build nothing early)

**F0 — Data substrate + PIT contract + calibration (no strategy PnL).** Deliverables: the pinned-source data
layer (EDGAR fundamentals + one primary price source), hash-bound input manifests binding
`(cik, accn, filed, tag, unit, period)` per fundamentals row and per-symbol price-series digests; the PIT
usable-from rule implemented fail-closed; CA/split handling cross-checked on a sample; the delisting-coverage
measurement; the benchmark TR proxy built + cross-checked; a **calibration report** (coverage by year, PIT lag
distributions, cross-source price divergence, universe-rule dry run producing membership series WITHOUT any
return computation). **F0 gate (all must hold, else stop/pause for a Robin data decision):** ≥ 60 monthly
rebalance dates of dev-window coverage with ≥ 80 eligible names each; delisted-name price coverage measured and
either adequate or convention-bounded with counted haircuts; cross-source close divergence within the pinned
tolerance on the sample; benchmark proxy within tolerance of the independent TR reference.

**F1 — ONE frozen mechanical cross-sectional family, backtested (the only backtest in this line).** Strategy id
`fundamentals.xs_value_quality_v1` (shape below). Dev window first; the single-use holdout only after the
family is frozen. Produces a reviewed v2 artifact under the NEW committed daily-line criteria; only a verifying
`ok` artifact can unlock any paper step, per S9, and even then paper entry needs the runbook's remaining steps
and Robin's go.

**F2 — LLM research overlay, FORWARD-ONLY, conditional.** Requires an F1 GO (the overlay needs a validated
mechanical baseline to beat) OR Robin's separate explicit decision to run it as its own family against the
benchmark alone. Own predeclared packet either way. Never a rescue path for an F1 null, and never backtested.

## Predeclared PIT rule (fundamentals usable-from)

A fundamentals fact with filing date `filed` (day granularity from EDGAR) is usable **from the open of the
second trading session after `filed`** (i.e. decision dates must satisfy `decision_date ≥ filed + 1 trading
day`, and the fill happens the session after the decision). This over-conservative one-day embargo absorbs
acceptance-time-of-day ambiguity (EDGAR acceptance can be after-hours) without needing per-filing acceptance
timestamps. Amendments (10-K/A etc.) supersede only from their own `filed` date; originals stay binding before
that. The rule is enforced fail-closed in the data layer (a fact without a parseable `filed` is dropped and
counted), and the manifest hash-binds it.

## Predeclared Universe (PIT-computable rule, no snapshots)

At each monthly rebalance decision date `D` (last trading session of the month):

1. Candidate set = all US common stocks (CIK-mapped, primary listing, share class deduplicated by predeclared
   preference: highest ADV class) with (a) a PIT-usable annual or quarterly filing aged ≤ 400 calendar days at
   `D`, (b) ≥ 252 trading days of price history at `D`, (c) 20-day ADV ≥ $5M (rev-1 proposal).
2. Rank by PIT market cap = PIT shares outstanding × close(`D`); **universe = top 100**.
3. **Minimum-eligibility rule:** if < 80 names qualify at `D`, the rebalance is SKIPPED (positions held), the
   skip counted — mirroring the M7c ≥-8-of-10 rule.

ETFs, ADRs, REITs, and financials are NOT excluded in rev 1 (each exclusion is a researcher degree of freedom;
review may prune this with justification, then it freezes). The universe series itself is an F0 deliverable
(dry run), so its stability is inspected **before** any return is attached to it.

## Strategy Shape (F1 — frozen at review, before any PnL)

`fundamentals.xs_value_quality_v1` — long-only, monthly, equal-weight, hand-set weights, no fitting:

- **Factors (rev-1 proposal, pre-2010-literature-grounded, to freeze at review):** earnings yield
  (trailing-12M net income / market cap), ROE (trailing-12M net income / latest book equity), and net share
  issuance (negative 12-month change in PIT shares outstanding, i.e. buybacks score high). Cross-sectional
  z-score each within the universe at `D` (winsorized at ±3), composite = equal-weight mean of the three.
  **No optimization, no per-window fit, no retraining — ever.**
- **Portfolio:** long the **top 20** names equal-notional; no shorts, no leverage, no locate/SSR surface.
- **Rebalance:** decisions at close of `D` on PIT-usable data; fills at the **next session's official close**
  with the pinned cost haircut (rev-1: 10 bps per side on traded notional); dividends credited on ex-date and
  reinvested at that day's close (TR accounting only — nothing conditions on a dividend before its ex-date).
- **Turnover cap as a diagnostic, not a knob:** expected ~20–40%/month one-way; measured and reported, never
  tuned against.

## Predeclared Windows

- **Dev window:** all F0-adequate history from the earliest date passing the F0 coverage gate through
  **2022-12-31** (must contain ≥ 60 monthly rebalances, else the F0 gate already failed).
- **Embargo:** 2023-01-01 → 2023-06-30 — never used for anything (breaks trailing-feature overlap between dev
  and holdout).
- **Holdout (single-use, decision-carrying):** **2023-07-01 → 2026-06-30** (36 monthly rebalances), untouched
  until the family is frozen post-review; consumed by ONE run; no metric from it may motivate a re-test, a new
  factor, or a window change. Any later family needs a brand-new never-inspected window (which, on a monthly
  cadence, means real elapsed time — acknowledged: this line's iteration clock is slow BY DESIGN).
- **Attestation (pin before any F1 run):** no configuration of this family — these factors, this universe rule,
  this cadence — has been backtested on ANY window prior to this predeclaration; the factor list was selected
  from pre-2010 public literature and cost/coverage reasoning only.

## Benchmark Contracts (both gated, mirroring the dual-benchmark discipline)

- **`sp500_total_return_proxy_v1`** (Robin's benchmark): SPY close-to-close with dividends reinvested at
  ex-date close, source-pinned in F0 and cross-checked against an independent published TR reference (pinned
  tolerance, rev-1: ±0.15%/yr cumulative divergence on the overlap sample).
- **`universe_equal_weight_tr_v1`** (family benchmark, the equal-weight analogue): equal-notional TR basket of
  the FULL eligible universe at each rebalance, same fills (next-session close), same cost haircut, same
  dividend accounting, same delisting conventions as the strategy.

Active return must be strictly positive vs **BOTH** on the holdout. The equal-weight leg is what separates
"stock selection works" from "the market went up".

## Metrics And Acceptance Gates (rev-1 proposals — FROZEN AT REVIEW, before any run)

New pinned criteria module for the daily line (the M7 intraday module is untouched). GO is the full
conjunction on the single-use holdout:

1. `rebalance_count >= 36` and `avg_eligible_universe >= 80`
2. net TR (after pinned costs) `> 0`
3. active net TR `> 0` vs `sp500_total_return_proxy_v1` **AND** vs `universe_equal_weight_tr_v1`
4. **cost-stress gate:** gate 3 still holds with the pinned per-side cost DOUBLED (20 bps)
5. `max_drawdown_pct <= 0.35` absolute AND `<= 1.25 ×` the S&P TR proxy's drawdown over the same window
6. breadth: no single name > 30% of positive active PnL; ≥ 15 of 20 slots filled at ≥ 90% of rebalances
7. delisting-convention exposure: haircut-convention names ≤ 5% of total gross exposure over the holdout
   (else the result is data-artifact-suspect and NULLs regardless of PnL)
8. **zero** quality breaches (the five existing counters carry over: reconcile drift, S1 canary, live submit,
   artifact mismatch, unhandled exception)

Diagnostics (never gates): information ratio vs both benchmarks, monthly hit rate, turnover, factor-leg
attribution, per-year actives, dev-vs-holdout consistency read.

**Statistical honesty, predeclared:** 36 monthly observations is a WEAK sample — a conjunctive pass is
supporting evidence, not proof, and (mirroring M7d) even a clean F1 GO is **PROVISIONAL**: it routes to
paper-phase forward confirmation under the pinned paper criteria, not to any live step. The dev-window result
is descriptive context only; the holdout alone decides.

## Stop Rules / Search Budget (this line's own budget, predeclared now)

- At most **two** predeclared strategy families on this substrate before a **documented stop** of the line
  (this packet's F1 composite = family 1). The F2 LLM-forward overlay is conditional on an F1 GO and is NOT a
  rescue path for an F1 null; running F2 despite an F1 null is possible only as Robin's separate explicit
  decision with its own packet.
- An F0 data-adequacy failure is a **data conclusion, not a strategy conclusion**: the line pauses for an
  explicit Robin paid-data decision (e.g. a survivorship-bias-free commercial panel) or stops. No backtest on
  a knowingly survivor-biased panel, ever.
- On an F1 NULL: export the full per-gate table + factor-leg attribution + turnover/cost decomposition
  (staged-only, gitignored `reports/`), route per the family budget. No factor swaps, no threshold moves, no
  holdout re-use.
- This line does not touch the intraday budget in either direction: an M7d GO does not rescue this line's
  nulls, and vice versa.

## Verification Before Handoff

Before this packet is acted on (review pass, then any build):

- Repo on `main`, clean tree; run gates `false`; `artifacts/backtests/` = `.gitkeep` only; offline suite green
  (1997 tests at drafting time).
- This packet has passed an adversarial multi-lens critique + GPT review, revisions applied, and Robin has
  given a **separate explicit go for F0** (scoped: $0 sources only; any Databento daily pull is its own ask).
- The F0 build, when authorized, is TDD offline-first (fixtures; zero network in tests), with the live EDGAR/
  price pulls behind the same UNVERIFIED-fail-closed seam discipline as every other credentialed path in this
  repo, and rate-limit-respecting clients (EDGAR fair-access) — no bulk crawl before the seam is verified.
- Nothing in F0/F1 writes to `artifacts/backtests/` until a reviewed artifact verifies `ok` under the NEW
  committed daily-line criteria module — which must itself land as a reviewed commit before the holdout run.
