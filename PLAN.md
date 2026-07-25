# PLAN.md — Stocks trading agent

> **Frozen 2026-07-25 at public release.** This is the internal build log, published as-is because the
> chronology *is* part of the evidence: it shows criteria being pinned before runs, families being nulled on
> those criteria, and a written stop rule being applied rather than argued around. It is a working document,
> not documentation — it uses milestone codes (M0–M8), safety-invariant codes (S1–S10), frozen-decision ids
> (FD-xx) and local branch names, and its "current status" section stops on 2026-07-10. Nothing after that date
> was run. For the finished picture read `README.md`, `docs/ARCHITECTURE.md` and `docs/RESULTS.md`.

## Active purpose

Build an autonomous US-equities trading agent, paper-first but live-like, reusing the engineering spine of a
separate prediction-market research workspace (not published; see `docs/method/`). Authoritative design:
`docs/superpowers/specs/2026-06-08-stocks-agent-design.md`.

## Current status (2026-07-10)

- **2026-07-09 correctness wave + 2026-07-10 paper-live-loop wave — CONSOLIDATED ON MAIN, 1988 tests green,
  gates untouched, S1 canary green.** The 2026-07-09 wave (Codex/GPT: terminal-fill flatten gating, owner-safe
  run lock, replay credential-inertness, semantic artifact re-verify, drill terminal evidence, journal
  integrity) was independently reviewed (7 agents; task5's blindness-guard variant was BUGGY and dropped) and
  merged. The 2026-07-10 wave (Robin's goal: live view + get going without waiting on M7d) closed the verifier
  v1/equal-weight bypasses, added incomplete-report + honest exit codes on feed exceptions, the `read_new()`
  journal delta API (O(day²) copy removed), the paper_report completeness fields + restart-safe naming, **the
  Track B status data plane (`agent.marketdata.status_plane`, closes P0-1; Alpaca source
  UNVERIFIED-fail-closed pending the Track D drill)**, **live-feed bounded reconnect with epoch bump (closes
  P0-3b)**, **`agent.paper_autorun`** (daily supervisor + launchd template), **`agent.paper_phase_report`**
  (weekly pinned-criteria aggregator; missing evidence never zero-filled), and **the local live view
  `python3 -m dashboard`** — verified end-to-end (replay-observe 74 ticks/exit 0/zero orders; synthetic session
  with the full order lifecycle; cross-process live updates). Remaining before ARMED autonomous paper: runbook
  steps A–E + Track D drills + the S9 decision (`docs/superpowers/specs/2026-07-10-paper-canary-decision-memo.md`).
  Observe-mode needs only steps A/B. F2 (episode-scoped flatten client id) is a documented pre-arming item.

- **Robin's GO on all open decisions (2026-07-10 ~11:00 DK):** (1) **S9 = Option 1 (observe-mode now)** —
  decided in the memo itself; Option 2 (unvalidated canary) explicitly NOT taken now, revisit at the earliest
  after the M7d result; first live observe session starts the same afternoon IF the 15:32 `verify_alpaca_feed`
  report is green. (2) **M7d run-AUTHORIZED**: Robin chose M7d over routing straight to a realism-matched lever
  AND gave the separate go — the only remaining gate is the fresh holdout completing ~2026-07-14 (earliest run
  ~2026-07-15 when the last session's historical data is available). Expected outcome per the measured
  feasibility: NULL on realism caps; the run's value is the route-A-vs-B edge read. (3) **Data ladder stays $0
  (Alpaca IEX)**: no $99 SIP / $199 Databento spend until a strategy validates; the 15:32 report's
  statuses/lulds counts feed that decision. (4) **Fundamentals long-horizon mandate granted for PACKET DRAFTING
  ONLY** (the fresh explicit mandate the substrate rule requires, scoped to authoring+review of a predeclared
  packet — see `docs/superpowers/specs/2026-07-10-fundamentals-longterm-research-packet.md`, now DRAFT rev 3
  after TWO same-day adversarial passes: the 5-lens review (rev 2) and the GPT review (gpt-5.6-sol/ultra,
  read-only Codex; verdict RECONSIDER-EXPERIMENT + 5 confirmed blockers, all applied — the historical window
  is now a REJECTION-only screen, a bounded F−1 procurement phase precedes any F0 build, and the design is
  locked by a committed design digest; full verdict in
  `docs/superpowers/reviews/2026-07-10-fundamentals-predeclaration-gpt-review-handoff.md`). NEXT = Robin's
  routing choice: F−1 go / straight-to-paid-data decision / park the line; F−1, F0, and any data spend each
  need their own separate go). (5) Hygiene: the three stale superpowers worktrees removed;
  Alpaca key regeneration remains Robin's own to-do.

- Design spec + M0/M1 plans written, externally reviewed (twice) + internally reviewed (5-lens adversarial
  workflow), reconciled. All external facts verified against primary sources.
- **M0 built, hardened, and green (129 tests, deterministic).** TDD throughout; an adversarial review of the
  M0 code found 4 bugs, and a second (code) review round found more (token forgery via the private mint key,
  journal dropping a corrupt complete tail, per-stream seq being per-writer, float qty reaching submit) — all
  fixed test-first and re-probed closed. Token authority now lives in an immutable registry, not the token
  object. Stdlib-only; no network; committed gates OFF.
- Git history on `main`: spec/review/reconcile docs → charter files → `ac83b73` (M0 feat) → `933caf6` (M0 harden)
  → `df42d83` (M0 harden round 2, 129 tests).
- **M1 tier-1 contract FROZEN + verified (2026-06-09):** `docs/superpowers/specs/2026-06-09-M1-tier1-contract.md`
  (designed via 3-architect panel → synthesis → critic → revision → independent re-critique = READY-TO-BUILD).
  It pins every module API, the L2 `book_hash` encoding, fixture schemas, and the test→safety-invariant map.
- **Databento access (2026-06-09):** provisioned key is **historical-only**; live realtime is a separate paid
  subscription (not provisioned). Since live ≡ historical schema, the whole stack builds + verifies on historical
  now; the live feed is deferred. M1 tier-2 therefore splits: **(2a) historical-verified** (runnable now) vs
  **(2b) live-verified** (deferred blocker on the subscription).
- **M1 tier-1 (offline) BUILT + HARDENED + COMMITTED (`5723e4d`):** full data tier under `scripts/recorder/`
  (event/event_row/book_state/book_hash/persistence/replay/reconcile/recorder/status/bar_cache/entitlement) +
  `marketdata/databento.py`, TDD. **366 tests green**, no-network/no-creds clean, golden `book_hash`es stable.
  Six adversarial review rounds (each repro-gated) found **23 confirmed defects — all fixed TDD** (contract items
  C1–C9, D1–D9, E1–E3, F1–F2, G1–G4 appended to the frozen contract; round 6 clean).
- **M1 tier-2 (2a) historical-verified DONE + COMMITTED (`2b72143`, **378 tests**):** credentialed verifier ran
  vs the live Databento Historical API. Verified: L1 = `EQUS.MINI` (tbbo/bbo-1s/bbo-1m/trades/ohlcv-1s/ohlcv-1m/
  definition; NO mbp-10, NO status); L2 depth = `XNAS.ITCH` (mbp-10 = REPLACE-per-record, full post-event top-10
  book; `DBEQ.BASIC` rejected — sparse 1-level; `XNAS.ITCH` single-venue Nasdaq scope noted); status →
  broker/calendar downgrade. `access=historical`, `live_subscription=pending`. Artifact (gitignored/reproducible):
  `reports/databento_entitlements/verified_matrix.json`. **Scope of the reproducible tool** (`verify_databento_entitlements.py
  --live`): it reproduces schema/range/cost availability **+ entitlement-by-sample-pull** (a tiny
  `timeseries.get_range` per available cell: ≥1 record + a decode sanity = int 1e-9 fixed-point→Decimal, no float;
  `mbp-10` carries the 10-level/REPLACE structure) — recording a **redacted summary only** (counts/flags, never raw
  licensed data). A **full decode through the project parser/`book_state`** (byte-level book reconstruction + hash)
  remains a **tracked tier-2b / live item**, not a tier-2a claim.
- **M1 tier-2 (2b) live-verified: DEFERRED** — blocked on unprovisioned paid live realtime subscription.
- **M2 market-state DONE on branch `m2-market-state` (532 tests green):** pure market calendar/session gate,
  halt/LULD/SSR tradability decider, status ledger, session-aware liveness seam, market-state cache, config
  provenance strings, and fail-closed corporate actions. M2 submits no orders, mints no preflight tokens, imports no
  heavy SDK at module scope, and preserves committed run/live gates OFF. Hardening fixed identity/provenance risks:
  symbol/status/NBBO mismatches now fail closed; blank FIGI/CUSIP/source CA IDs, mismatched fetcher/source
  boundaries, and mirrored whitespace IDs cannot manufacture durable identity or fake source independence.
- **M3 signal + observe-only calibration probe DONE on branch `m3-signal` (700 tests green):** contract
  designed → 4-lens critic pass (52 findings applied) → independent re-critique (READY-TO-BUILD) → TDD build
  (quote-quality filters, mid-bar label series with watermark anti-lookahead, feature engine, signal snapshot
  gates, stable-logistic forecaster, as-of climatology reference, resolver with future-receipt deferral,
  calibration report with funnel identities + committed byte-identical golden) → 4-lens adversarial code
  review (22 agents, repro-gated: 17 confirmed findings = 9 unique defects, incl. 1 blocker
  climatology double-ingest) → all fixed TDD. S1 holds: the probe opens nothing, imports no order-capable
  module (AST + subprocess guards), and journals `paper_eligible=false` on every row.
- **M4 risk core DONE on branch `m3-signal` (896 tests green):** contract synthesized from a 3-architect
  panel around 8 locked decisions → 4-lens critic pass (50 findings, 2 blockers — all applied, READY-TO-BUILD
  rev 2) → TDD build (`scripts/agent/risk/`: 34-reason vocabulary, 13-rung two-phase `can_open` ladder,
  `IntradayMarginModel` per FINRA 26-10 incl. bd5/bd15 windows + max-merged per-date deficits + minor latch +
  90-day freeze, mirror-only `LegacyPdtCompatMode`, exposure caps with poisoned aggregates, loss limits,
  `RiskKillSwitch` delegating flatten to the M0 actuator, `journal/risk.jsonl` ledger + byte-exact rehydrate)
  → 4-lens adversarial review (13 agents, repro-gated: 8 confirmed defects — 2 majors in the freeze
  mechanics — all fixed TDD; see the contract's §R harden log). Committed `risk_rules.json` caps stay 0;
  every `can_open` on the committed config terminates at `run_gates` (S1 extended).
- **M5 paper-exec: BUILT — all 16 §R test files green (1513 tests, 2026-06-10); adversarial review
  rounds NOT yet run.** Contract path: 3-architect panel → draft → FULL 4-lens critic pass (48
  findings incl. 2 blockers) → rev 2 with all 48 applied (§V log) → independent re-critique (all 48
  verified + 5 RC defects, applied) → READY-TO-BUILD (`690434c`). Findings archive:
  `docs/superpowers/reviews/2026-06-10-M5-contract-critic-findings.json`. Build in 6 TDD waves
  (`b791f3d`→`a78a744`): wave 1 vocab/config/pricing/order-state (1040), wave 2 preflight ladder +
  TOCTOU consume + realism/fees + synthetic/gate (1251), wave 3 exec_ledger + alpaca/fake/flatten
  brokers (1358), wave 4 paper_book + replay_feed + run_lock/secrets (1446), wave 5 orchestrator +
  CLI + exec_fixtures (1466), wave 6 E2Es + committed goldens + kill drill + canary/purity (1513).
  S1 holds: committed config + absent run-gates file ⇒ zero submits over a full orchestrator run;
  kill-flatten works in every composition (the M5C-1 blocker pin). Build errata logged in wave-5/6
  commit messages (notable: M4 `leg_cap_notional` requires explicit strategy limits — M7 decision
  item; 2 §3 import-row omissions whitelisted in the AST guard).
  **HARDENED (`a11a947`, 1520 tests): 5-lens repro-gated adversarial review (safety/execution/
  determinism/conformance/lifecycle) — determinism + execution math clean; 6 defects fixed TDD:
  LC-2 (high — kill-with-close-in-flight crash + double-sell risk → retire close task, single
  flatten of record), LC-1/SF-1 (major — over-qty exit crashed the loop → journaled refusal),
  EC-1 (dead §J/EX-9 sell-fee path → close now feeds a sell-side modeled fill; goldens
  regenerated), SF-2 (FlattenUnpriced token leak → injected void_token), CC-1 (live-path
  `secret`→`secret_key`), DJ (fill_delta pinned-context hardening). M5 DONE.**
  **Plan confirmed (2026-06-10): finish the remaining milestones M6→M7, then a full autonomous paper
  edge-validation phase before any M8 step** (see "Edge before live" under Locked decisions). Merge-to-main
  **DONE 2026-06-26** (FF of `codex/m7-backtest-gate` into `main`; see the current-state bullet below).
- **M6 DONE on branch `m6-reconcile` (2026-06-13, 1639 tests green; separate W6 re-review clean).**
  - **Contract FROZEN rev 6 READY-TO-BUILD, committed `8ef8a51`:**
    `docs/superpowers/specs/2026-06-10-M6-reconcile-contract.md` (1556 lines). Path: 3-architect panel
    (correctness/safety/integration) → synthesis (20 disagreements resolved in §1) → 5-lens critic pass
    (48 raw → 34 canonical M6C, 3 blockers) → FOUR independent re-critique rounds (RC-1…RC-14) to
    convergence: round 5 verified all 47 prior resolutions complete (unverified=[]) + 1 minor (RC-14
    type pin) applied in rev 6. Archive: `docs/superpowers/reviews/2026-06-10-M6-contract-critic-findings.json`
    (48 findings, all applied, none rejected). Key catches pre-build: cash-latch fail-open window through
    cash-skipped passes (RC-8: re-journal blocks BOTH clear paths), un-constructible §G patch target
    (RC-12: pin = `agent.broker.alpaca.AlpacaPaperBroker`), PROBE_FAILED missing from deferral set (RC-9),
    exit-code precedence completed=false ⇒ 3 over 1 (RC-13). Design core: pure engine
    `scripts/agent/broker_reconcile.py` + `ReconcileLedger` on new stream `journal/reconcile_alerts.jsonl`;
    broker = truth via explicit `position_adjust` rows; journaled rehydratable drift latch → existing
    `portfolio_unreconciled` can_open reason; passes broker-read-only (no tokens/orders, safe gates OFF);
    exit codes 0/1/2/3; §T items ruled: websocket fills / activities granularity / retry-replace+concurrency /
    journal rotation OUT with owners; SOD/EOD job + PaperBook adjust fold + kill-residual REPORTING in.
  - **W1 DONE, committed `7feb110` (suite 1574 = 1520 + 54):** vocab/dataclasses/typed boundary +
    ReconcileLedger facade (§B.1a validation, v-first/rules_hash-last) + replay + latch fold (FD-M6-6,
    M6C-22, RC-8 residue lifecycle, §B.1b zero state). Builder errata in module docstrings.
  - **W2 DONE, committed `44686f6` (suite 1590 = 1574 + 16):** pure diff core in
    `scripts/agent/broker_reconcile.py` plus §I file-1 cases 2–20/24–26 in
    `tests/agent/test_broker_reconcile.py`. Covers `diff_positions` union/absence/short semantics,
    LIFO adjustment planning, cost tolerance + re-anchoring, deferral/immediate no-plan paths,
    `diff_cash` exact telescope + fill-id dedupe, `resolve_order_probe` table branches, and
    `identity_note` cent boundary. Verification: targeted broker-reconcile test file green, full
    suite green (`python3 -m unittest discover -s tests -p 'test_*.py' -t .`), `git diff --check`
    clean, and W2 diff audited to exactly the two W2 files.
  - **W3 DONE (suite 1599 = 1590 + 9):** positions-stream `EVT_POSITION_ADJUST` +
    `record_position_adjust` in `scripts/agent/exec_ledger.py`, and `PaperBook.rehydrate` /
    `apply_position_adjust` in `scripts/agent/paper_book.py`. Covers ledger field-set/id/lineage/qty
    validation, no order-lifecycle kwargs, qty/cost/combined/cost-only/zero adjust folds, prev-state
    verification with Decimal value equality, close-after-adjust brick trap, unchanged fill watermark,
    bad seeded rows, modeled-lineage preservation, and write-ahead no-commit on ledger refusal.
    Verification: targeted exec-ledger + paper-book tests green, reconcile/ledger/book regression green,
    and full suite green (`python3 -m unittest discover -s tests -p 'test_*.py' -t .`).
  - **W4 DONE (suite 1611 = 1599 + 12):** orchestrator-owned reconcile stream wiring, pure
    reconcile rehydrate, exported `run_reconcile`, portfolio drift-latch stamping, paper-only EOD
    hook, order-session expiry, kill-residual flatten probes, durable-id seeding/missing notes, and
    same-tick `BrokerAdjustDetector` freeze → status row → immediate reconcile path. Verification:
    targeted orchestrator matrix green, reconcile/ledger/book/canary/offline/golden regression block
    green, and full suite green (`python3 -m unittest discover -s tests -p 'test_*.py' -t .`).
  - **W5 DONE (suite 1636 = 1611 + 25):** `agent reconcile` CLI, `--rebaseline-cash`,
    exit-code mapping (0 clean / 1 drift-latched / 2 usage-lock / 3 cannot-reconcile),
    `_cmd_paper` SOD mapping, RunLockHeld wrappers, journal-corruption mapping, runbook,
    committed-config reconcile canary, offline/import purity guards, and observe/synthetic
    golden-stability checks proving no automatic `reconcile_alerts.jsonl` stream. Verification:
    W5 targeted block green, affected regression block green, and full suite green
    (`python3 -m unittest discover -s tests -p 'test_*.py' -t .`).
  - **W6 INITIAL REVIEW FIXES APPLIED + COMMITTED (`8e1f4db`, suite 1639 = 1636 + 3):** fixed the three
    repro-gated review blockers: in-process drift-latch clear now respects dirty journal windows; durable
    detector seeding now happens after `position_adjust` at the reconciled point; multi-lot allocation now feeds
    `diff_positions` newest-first by `position_open` stream `seq`, not `position_id`. Verification:
    new RED→GREEN regressions, affected M6 block green, compile green, `git diff --check` clean, and full
    suite green (`python3 -m unittest discover -s tests -p 'test_*.py' -t .`).
  - **W6 RE-REVIEW / CLOSEOUT CLEAN (2026-06-13):** separate read-only re-review over the fixed diff found
    0 critical/high/medium/low issues. It specifically re-checked dirty-window latch clearing, post-adjust
    durable seeding, and newest-first lot ordering by `position_open` stream `seq`; `git diff --check`, targeted
    regressions, compile, and the full 1639-test suite were green. M6 is closed on the branch; next is M7.
- **M7 REVIEW-HARDENED OFFLINE CLOSEOUT on branch `codex/m7-backtest-gate` (2026-06-13, 1683 tests green after
  historical-review hardening):**
  anti-lookahead backtest primitives, v2 artifact verifier/metrics, artifact cache key hardening, deterministic
  temp-only fixture artifact builder CLI, paper-phase criteria evaluator/runbook, and first real strategy
  `directional.momentum_v1` are built. Review hardening fixed the M7 seams that mattered before closeout:
  thresholds cannot be self-relaxed inside artifacts, benchmark PnL/risk realism metrics are explicit inputs
  rather than hardcoded pass values, delayed receipt of the decision bucket cannot count as quote B, and fixture
  artifacts cannot be written to production `artifacts/backtests/`. S9 integration is pinned: missing/mismatched
  artifacts reject before broker submit; valid v2 fixture artifacts can pass only under permissive temp-dir test
  fixtures; committed config plus valid artifact still submits zero orders. Production `artifacts/backtests/` still
  contains no reviewed strategy artifact, so the real strategy remains unable to open from the committed/default
  artifact directory. Historical reviewed artifact tier is explicitly deferred until a separate credentialed data
  run/review produces and approves it.
- **Historical artifact tier attempt + review hardening (2026-06-13):** added a separate normalized-quote
  historical artifact CLI (`agent m7-historical-artifact`) that keeps the fixture builder quarantined. The reviewed
  write path now requires a manifest JSON whose hash recomputes over the manifest body and normalized quote-row
  hash; the `data_pin` must end in that manifest hash; quote sizes, calendar windows, CA blackouts, latency,
  slippage, fee model, pricing model, and realism-gap model are pinned in artifact provenance. Production writes
  require `--allow-reviewed-artifact` and must target the exact `artifacts/backtests` directory, not a nested path.
  A credentialed Databento Historical pull for AAPL `EQUS.MINI:bbo-1m` over `2026-05-11T13:30:00` →
  `2026-06-09T20:00:00` produced manifest hash
  `57ada7d610b1f01d0ce3acb2682492492ee0e2c42018b82f3bd0919bccd308c5` (10,000 valid rows after dropping 78
  one-sided/UNDEF rows; instrument_id `38`). Review result: **failed M7 criteria**; gross and net modeled PnL were
  negative, so no artifact was written or committed. Production `artifacts/backtests/` remains `.gitkeep` only,
  and paper edge-validation remains blocked until a reviewed artifact verifies `ok`. Failure review:
  `docs/superpowers/reviews/2026-06-13-M7-historical-artifact-failure-review.md`.
- **Broader historical artifact attempt + causal-time input hardening (2026-06-13):** reran the hardened
  `--input-manifest-json` path over a predeclared large-cap/Nasdaq-leaning universe
  (`AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,AVGO,COST,NFLX`) with `EQUS.MINI:bbo-1m`, the same rules hash
  `a4298880ef6136f69a627c62ebc002d9c2c85f7d1e7ae5f3d5f3e96647c06bf6`, and the same window
  `2026-05-11T13:30:00` → `2026-06-09T20:00:00`. The first broader evidence pass exposed impossible
  receive-before-event quote rows in the normalized input, so the historical manifest contract now rejects
  `ts_recv_utc < ts_event_utc` and a new pinned v2-causal run recomputed every manifest hash/data pin after
  dropping those rows. Every symbol still failed M7 criteria; all had negative execution-realistic net PnL and
  negative active PnL, so no staging or production artifact verified `ok` and no production artifact was written.
  Production `artifacts/backtests/` remains `.gitkeep` only; `verify_artifact` against the reviewed triples
  returns `missing`. Paper edge-validation remains blocked. Next loop is strategy/universe hardening, not M8.
  Failure review:
  `docs/superpowers/reviews/2026-06-13-M7-broader-historical-artifact-failure-review.md`.
- **Strategy/universe manifest hardening (2026-06-13):** the historical reviewed-artifact manifest now requires a
  predeclared `universe` block (`hypothesis_id`, `selection_rule`, ordered `symbols` including the artifact
  symbol), and v2 artifact provenance carries that block forward. This keeps future strategy/universe reruns
  hash-bound to the reviewed hypothesis instead of allowing post-run symbol cherry-picking. No thresholds changed,
  no production artifact was written, and M8/paper edge-validation remain blocked.
- **Holdout historical artifact attempt (2026-06-13):** reran the same broader universe over an earlier,
  non-overlapping `EQUS.MINI:bbo-1m` window (`2026-04-09T13:30:00` → `2026-05-08T20:00:00`) with hash-bound
  universe manifests. Every symbol again failed M7 criteria; all had negative execution-realistic net PnL and
  negative active PnL, and most also breached one or both realism-gap gates. No staging artifact verified `ok`,
  no production artifact was written, and `artifacts/backtests/` remains `.gitkeep` only. Failure review:
  `docs/superpowers/reviews/2026-06-13-M7-holdout-historical-artifact-failure-review.md`.
- **M7b strategy hardening attempt (2026-06-13, 1688 tests green):** added `directional.momentum_v2` plus an explicit historical
  `strategy_id` selector so artifact payloads/filenames verify against the selected strategy version while v1 stays
  compatible. V2 keeps criteria unchanged and tries stricter trend/spread/edge gating; the valid holdout replay
  still failed every symbol with negative execution-realistic net PnL. The older broader manifests are no longer
  valid reviewed inputs because they predate the required hash-bound `universe` block; a diagnostic reconstructed
  broader replay also failed every symbol. No production artifact was written, `artifacts/backtests/` remains
  `.gitkeep` only, and paper/M8 remain blocked. Failure review:
  `docs/superpowers/reviews/2026-06-13-M7b-momentum-v2-failure-review.md`.
- **M7b diagnostic family closeout (2026-06-13, 1693 tests green):** added bounded historical trade/skip diagnostic exports plus a
  cross-symbol index for failed reviewed-artifact attempts. The holdout v2 diagnostic index
  (`reports/m7_historical_runs/2026-06-13-holdout-bbo1m-v2-diagnostics/index.json`) showed aggregate
  net execution-realistic PnL -409.060000, active PnL -142.165000, 0/10 symbols net-positive, and no row
  truncation. The only simple net-positive gap bucket was tiny, holdout-derived, failed symbol breadth, and became
  negative excluding NVDA. Closeout decision: do not implement an M7c gap/adverse-selection gate; close the current
  L1/BBO 1-minute long-only momentum family for M7. No production artifact was written, `artifacts/backtests/`
  remains `.gitkeep` only, and paper/M8 remain blocked. Closeout review:
  `docs/superpowers/reviews/2026-06-13-M7b-diagnostic-family-closeout-review.md`.
- **M7c predeclared relative-strength research packet (2026-06-13, rev 2 proxy-first):** wrote
  `docs/superpowers/specs/2026-06-13-M7c-relative-strength-research-packet.md` before any code or reviewed
  artifact loop. A direction review revised the original draft (which named true market-neutral the preferred
  phase-1 target) to **proxy-first**: phase 1 is `relative_strength.long_only_proxy_v1`, a long-only
  cross-sectional gating probe that reuses the existing single-leg fill/exit machinery and adds only a
  cross-sectional ranking/decision harness (no short-side, locate/SSR, multi-leg-preflight, or basket work), to
  answer "is there cross-sectional residual signal on this substrate at all?" before any short-side build. Phase 2
  (`relative_strength.market_neutral_v1`, long-short, multi-leg, short-side locate/borrow/SSR, basket metrics, the
  second benchmark) is conditional on a predeclared go/no-go: it is built only if the phase-1 artifact verifies
  `ok`. A broad phase-1 null routes to a **substrate** decision (longer horizon, L2/MBP-10 depth, or a wider
  liquidity-screened universe), not a sixth strategy family. The rev-1 approval does not carry forward to the
  revised sequencing; rev 2 was re-reviewed before commit. It fixes the ordered 10-symbol universe and clean-window
  rule, never labels phase 1 market-neutral, and does not authorize threshold relaxation, production artifact
  writes, paper edge-validation, M8, or gate flips.
- **M7c phase-1 proxy build (2026-06-13, 1702 tests green, commit `7aa628e`):** built the phase-1 gating probe
  `relative_strength.long_only_proxy_v1` (`scripts/agent/strategies/relative_strength.py`) — a pure cross-sectional
  ranking over the valid decision set at one decision instant (rs_score with the packet weights), top-2
  equal-notional whole-share long-only BUY candidates, `do_nothing` below 8 valid symbols, deterministic
  (rs_score desc → predeclared universe order → symbol). Added the `universe_equal_weight_long_v1` benchmark id to
  `backtest_metrics` (additional metric/provenance; pinned verifier benchmark unchanged) and registered the new
  module in the wall-3 strategies AST guard. Contract:
  `docs/superpowers/specs/2026-06-13-M7c-phase1-proxy-contract.md`; RED→GREEN tests:
  `tests/agent/test_relative_strength_proxy_m7c.py` (9). Reviewed APPROVE (contract-conformance + adversarial
  lenses); positional score keying + symbol tie-break hardening applied. **Still pending before any edge verdict:**
  (1) the multi-symbol same-timestamp historical-runner harness wiring + `universe_equal_weight_long_v1`
  attribution + one-artifact aggregation, (2) the credentialed clean-window run + Phase Gate go/no-go. No
  production artifact written; `artifacts/backtests/` remains `.gitkeep`; paper/M8 blocked; run gates and pinned
  criteria unchanged.
- **M7c phase-1 multi-symbol harness wired (2026-06-26, 1715 tests green, uncommitted on `codex/m7-backtest-gate`):**
  built `run_historical_cross_sectional_backtest` + `HistoricalCrossSectionalResult`
  (`scripts/agent/backtest_historical.py`) — the one genuinely new piece phase 1 needed (packet step 2 / FD-P1-7).
  It aligns decision timestamps across the predeclared universe; per instant assembles every symbol's point-in-time
  `SignalSnapshot` (per-symbol `FeatureEngine` sharing one clock → consistent `now_ms`), ranks via the proxy,
  selects the top-2 long, and reuses `_simulate_historical_long_trade` unchanged per leg. 30-bar horizon (skips the
  decision if it crosses RTH close), entry = decision+1m with M7 quote-B latency, no-overlap per symbol (a held name
  is suppressed until its exit bar), both legs aggregated under ONE artifact (FD-P1-10; the pinned verifier benchmark
  stays `exposure_matched_midbar_v1`), plus the `universe_equal_weight_long_v1` benchmark + active PnL + diagnostics
  (exclusion/valid-set counts, overlap-suppressed, per-symbol leg counts, benchmark fill/skip). **Benchmark
  normalization decision (Robin, exposure-matched):** the equal-weight-long basket sizes off the strategy's ACTUAL
  opened (whole-share-floored) notional split equally across valid symbols — NOT a nominal `TOP_N`×`PAPER_NOTIONAL` —
  so whole-share flooring does not systematically over-fund the benchmark and drag active PnL. RED→GREEN TDD:
  `tests/agent/test_m7c_cross_sectional_runner.py` (13). Read-only adversarial review passed (adopted: benchmark
  fill/skip counters + qty-floor-to-0 and sparse-symbol edge tests; verified as misreads: the no-overlap inequality,
  the ET-date schedule source, and the fractional benchmark-leg qty). `_market_state`/`_schedule` refactored to
  blackout-set / session-window helpers with the single-symbol path behavior preserved. Committed `1465566`.
- **M7c phase-1 step-5 manifest + cross-sectional artifact writer + CLI (2026-06-26, 1740 tests green, committed
  `6ceb4c0`):** built the production-artifact-writer plumbing on top of the harness (packet step 5).
  `validate_historical_cross_sectional_manifest` binds ONE hash-bound multi-symbol manifest — predeclared universe
  block + per-symbol `{instrument_id,row_count,quote_rows_sha256}` data binding; universe symbols == symbols-block
  keys == quote-row keys exactly; per-symbol `data_pin` is DERIVED from `manifest_hash:symbol` (never stored → no
  circular hash); `horizon` is hash-bound + validated (not silently defaulted). `write_m7_historical_cross_sectional_artifact`
  runs the harness over the manifest, aggregates both long legs into ONE `(strategy_id,rules_hash,data_pin)` v2
  artifact (FD-P1-10), and carries the `universe_equal_weight_long_v1` attribution + a canonical-JSON breadth/leg
  diagnostics string in provenance (FD-P1-9). Same fail-closed write guard as single-symbol (exact `artifacts/backtests`
  dir + `--allow-reviewed-artifact`); only `relative_strength.long_only_proxy_v1` accepted; no broker/preflight surface.
  New CLI `m7-historical-cross-sectional-artifact` (`--symbol-quotes SYMBOL=path` repeated). `verify_artifact` extended
  the OPTIONAL provenance allow-set ONLY (equal-weight keys, `horizon`, diagnostics blob) — pinned benchmark, floors,
  required keys, and metric schema unchanged. Refactor: extracted `_parse_calendar_block`/`_parse_blackout_dates`/
  `_parse_execution_block`/`_guard_production_artifact_dir`, shared by both writers (single-symbol behavior preserved).
  RED→GREEN TDD: `tests/agent/test_m7c_cross_sectional_artifact.py` (25). Adversarial read-only review (5 dimensions,
  each independently verified) returned `changes_required`; both must-fixes applied (horizon hash-binding/pass-through;
  bool-as-int / duplicate-symbol / empty-symbols-block / horizon tamper tests). Deferred nice-to-haves: enforce that the
  manifest `calendar.sessions` covers every session date in the quote rows (the harness falls back to 13:30–20:00 for
  missing dates; tz-derivation from UTC rows is cleaner to add against the real backfill manifest). Committed `6ceb4c0`.
- **M7c historical backfill + cross-sectional input-manifest builder (2026-06-26, 1761 tests green, committed
  `0e4848b`):** new module `scripts/agent/historical_backfill.py` — the data-production half of the credentialed run.
  PURE + offline-complete: `normalize_quote_event(s)` (recorder `QuoteEvent` → canonical quote row),
  `derive_session_windows` / `instrument_ids_from_rows`, `build_cross_sectional_input_manifest` (emits a manifest the
  step-5 validator accepts; per-symbol `quote_rows_sha256` + body `manifest_hash` use the SAME primitives the validator
  recomputes with — the **build→validate→write→run round-trip is pinned** against the real harness),
  `cross_sectional_data_pin`, `write_quote_rows_jsonl`. LIVE seam (tier-2b): `pull_normalized_window` orchestration is
  offline-tested through an injected `quote_event_source`; the real `databento` `timeseries.get_range` + DBN `bbo-1m`
  decode (`_dbn_bbo1m_record_to_event_dict`) is lazily imported, **fails closed**, and is flagged tier-2b-UNVERIFIED
  (`NotImplementedError`) until verified against the live record layout. No offline path imports `databento` or reads
  `.secrets`. Adversarial review (4 dims, each verified) → `changes_required`; applied: custom-sessions coverage guard +
  zero-row guard in the pure builder; fail-closed live adapter (None/float/bool/non-positive rejects, `vendor_seq`+ISO
  `ts_recv` per the recorder `parse` contract, no silent `ts_recv` fallback). RED→GREEN:
  `tests/agent/test_m7c_historical_backfill.py` (21).
  **Window (Robin):** first chose a FORWARD window after 2026-06-13, then — since only ~10 sessions had completed by
  2026-06-26 (a complete 20+-session forward window is not available until ~mid-July) — pivoted to the packet's
  PREFERRED clean window `2026-03-10→2026-04-08` (past → available now, clean → no prior RS metrics).
- **M7c phase-1 credentialed clean-window run — broad NULL / NO-GO (2026-06-26; tool fix committed `68f0538`; run
  staged-only):** wired + live-verified the credentialed `bbo-1m` pull (`_live_quote_event_source` → real `databento`
  `timeseries.get_range` → `_dbn_bbo1m_record_to_event_dict` verified against the live `EQUS.MINI` `BBOMsg`: flat
  `*_00` top-of-book, int 1e-9 prices, UNDEF price/timestamp drops, RTH filter; cost ≈ $0.03). Pulled the clean
  window (10 symbols, 21 sessions, ~8.2k bars/symbol), built the hash-bound cross-sectional manifest, ran the writer
  **staged** under gitignored `reports/m7_historical_runs/2026-03-10-clean-rs-v1/` (NOT `artifacts/backtests/`).
  **Result:** 1144 trades / 21 traded sessions; net `-$839.68`; profit factor `0.55`; avg `-8.78` bps; active
  `-$120.65` vs the pinned `exposure_matched_midbar_v1` (active `+$405.64` vs `universe_equal_weight_long_v1` only
  because that basket lost more); realism gaps `p95 29.8` / `max 97.5` bps exceed the `15`/`50` caps. Breadth was
  broad (10 symbols traded, max 12.3% of gross legs / 16.8% of net-positive PnL — **no concentration breach**), so the
  null is broad rather than a single-symbol artifact, and the realism-gap failures are a real execution-quality signal
  on this L1-1min configuration. This nulls ONE tested configuration and does NOT causally isolate the substrate from
  the horizon, universe, or strategy shape; the NO-GO/STOP routing rests on the predeclared two-family rule, not on
  causal proof. **DECISION (predeclared Phase Gate + the two-family search-budget stop rule): momentum
  = family 1 (nulled); relative-strength = family 2 (now nulled on a clean window) → route to a SUBSTRATE decision
  (longer decision/holding horizon, L2/MBP-10 depth-aware fill tier, or wider liquidity-screened universe). NOT the
  phase-2 short-side build; NOT a third same-substrate family.** No production artifact written; `artifacts/backtests/`
  remains `.gitkeep`; paper/M8 blocked; run gates and pinned criteria unchanged.
- **STOP decision (Robin, 2026-06-26):** rather than pursue a substrate variation (L2 fill / longer horizon / wider
  universe), Robin elected an **explicit STOP on the autonomous edge search** — the packet's sanctioned Phase-Gate
  outcome after two same-substrate families (momentum, relative-strength) nulled. No third family / substrate build is
  in flight. The autonomous strategy-search loop is paused. **Open items for the step-back reassessment:** the
  long-pending **merge-to-main** decision (RESOLVED 2026-06-26 — FF-merged into `main`) and a scope/ambition review
  of whether/when to resume the edge hunt. Nothing should be built
  toward a new strategy family without an explicit restart from Robin.
- **GPT review of the NULL/STOP returned + acted on; restart toward a longer/coarser-horizon substrate (Robin,
  2026-06-26):** the GPT adversarial review came back and was independently re-verified (5-agent read-only workflow +
  direct read; worktree audited clean). **STOP HELD — no false-null:** the NULL is driven by active PnL vs the pinned
  `exposure_matched_midbar_v1` benchmark (`-$120.65`) plus blown realism caps, and GPT's own RTH-leak falsification
  (dropping the 102 event-outside-session rows) still failed hard. All four findings confirmed in code; none blocked
  the offline merge (code+docs only — no production artifact). **Four fixes APPLIED + TDD-tested (1773 tests green):**
  (B) the cross-sectional writer now also gates positive active PnL vs the `universe_equal_weight_long_v1` benchmark —
  the Phase Gate requires positive active vs BOTH benchmarks and only the exposure-matched leg was gated (equal-weight
  rode in provenance, ungated); (A) `_within_rth` AND `_validate_quote_row_quality` now require the `ts_event`
  bucketing key — not just `ts_recv` — to fall inside the pinned session window, dropping stale pre-open books that
  would otherwise bucket as RTH-tradable; (C) softened the causal "substrate proven" overclaim in `CLAUDE.md` + the
  staged `summary.json` (the NO-GO/STOP rests on the predeclared two-family rule, not on causal proof); (D)
  `build_cross_sectional_input_manifest` now fails closed unless `sessions` is pinned from the market calendar
  (`pin_sessions_from_provider`, half-day/DST-aware) or `allow_derived_sessions=True` for an offline fixture, and the
  validator rejects inverted windows. **Decision:** restart toward the sanctioned substrate step = a **longer/coarser
  decision/holding horizon on the same L1 data** (cheapest, reuses the harness, attacks both the edge failure and the
  realism-cap failures), NOT L2/MBP-10 (heaviest build) and NOT wider-universe-first. **Next:** predeclare the horizon
  experiment (coarser bars + longer holding, the pinned M7 criteria unchanged) before any run; the credentialed
  pull/run is gated on Robin's separate go. **Merge-to-main DONE 2026-06-26: `codex/m7-backtest-gate`
  fast-forward-merged into `main` (`main` = `ba1b554` = M7 tip; was M2 `e19118f`; all safety gates verified closed
  first; no remote, locally reversible).**
- **M7d predeclaration committed + full-project review applied (2026-06-26 → 2026-07-02, 1777 tests at that checkpoint;
  current suite 1988 after the 2026-07-09 correctness wave + the 2026-07-10 paper-live-loop wave):**
  the longer-horizon packet + GPT-review handoff were committed `676bf3e`
  (`docs/superpowers/specs/2026-06-26-M7d-longer-horizon-research-packet.md`, C2 = `1m`/`120m` sole decider on a
  fresh fixed-20-session post-2026-06-13 holdout; C1 = `1m`/`60m` descriptive on the snooped window; pinned
  criteria unchanged; both benchmarks gated). A 2026-07-02 full-project review (4 read-only deep-review agents:
  backtest core / backfill / safety / methodology + an own-run reproduction; tree git-audited clean) found **no
  blockers** — the safety checklist passed 12/12 in code (token chokepoint non-bypassable, S1 canaries real
  end-to-end, two-key arming, no committed secrets; one pre-live MINOR for the M8 checklist: the loss/drawdown
  auto-kill is SKIPPED on a non-"fresh" account read) — and three M7d-relevant fixes, applied TDD (+4 tests):
  (1) the cross-sectional runner now requires the DECISION bar to be FD-2-eligible at decision time
  (`read_eligible_midbar`; late-receipt bars are excluded from the ranked set as `decision_bar_*` exclusion
  counts — ranked set == tradable set; byte-identical on the clean-window data, where `ts_recv` is the minute
  boundary); (2) both reviewed-artifact writers now fail closed unless `rules_hash` equals the config-DERIVED
  hash for the `agent_rules_path` the harness runs (an operator label can no longer decouple the artifact triple
  from the rules — the M7d horizon config edit changes the derived hash, and the writer will enforce the new one);
  (3) `build_cross_sectional_input_manifest` requires an explicit `horizon` (silent `30m` default removed). The
  review also established: the staged `2026-03-10-clean-rs-v1` quotes predate fix A and no longer pass the HEAD
  validator (102 `ts_event`-outside-session rows), and the fix-A-compliant rerun — rows re-filtered, manifest
  rebuilt via the committed builder (`90866e90…`) — gives **1147 trades, net −$858.01, active −$111.51 vs
  `exposure_matched_midbar_v1`, p95 29.949, max 97.484**: the NULL is robust under the current contract and this
  is the like-for-like baseline for M7d. A measured pre-run feasibility decomposition (snooped window only; per
  trade gap ≈ entry_half + exit_half) put the horizon-invariant ENTRY-leg realism floor at **p95 ≈ 14.13 bps in
  the 120m-survivor population (94% of the 15-bps cap; survivor combined gap p95 31.67; max(entry_half) 85.8 >
  the 50 cap alone)** — an M7d C2 GO is structurally improbable on the realism caps, so the run's decision value
  is the EDGE read (route A vs B). The packet was revised to **rev 2** accordingly (measured feasibility +
  fix-A baseline in Evidence Grounding, max-cap order-statistic caveat + p99 diagnostic, pinned
  latency/slippage numerics, pinned C1/C2 run order, GO-path lead-time note, calendar-fixture + committed-driver
  operational prerequisites) and the handoff's GPT prompt gained a dimension G attacking the rev-2 additions.
  GPT review (2026-07-02) returned changes-required on two methodology items plus a strategic WATCH; rev 3 applied
  the must-fixes by removing the undefined "comfortable margin" gate, making route-B decomposition
  mechanism-classification only (no automatic second substrate spend), and reconciling current HEAD operational
  state. STATUS = GPT-reviewed rev 3, **run-AUTHORIZED 2026-07-10** (Robin chose M7d over routing straight to a
  realism-matched lever and gave the separate go); the only remaining gate is the fresh holdout completing
  ~2026-07-14 (earliest run ~2026-07-15). Paper/M8 blocked; run gates and pinned criteria unchanged.

- **Paper-operational readiness build (2026-07-02, 1846 tests green; commits `e037751`…`1a696a2`):** closed every
  buildable gap between "spine done" and "an autonomous paper session can run", without flipping anything
  (committed gates untouched; S1 canaries green throughout). (1) Kill-switch bounded blindness: >120s of
  continuous non-fresh account reads WITH open positions now trips the new `account_blind_cap` cause
  (flatten-then-halt, no account numbers consumed — FD-M4-3 preserved; code-constant per FD-M4-22; the
  2026-07-02 review MINOR closed). (2) `ExchangeCalendarsScheduleProvider` IMPLEMENTED (lazy import,
  4.13.2 version-pin fail-closed, coverage/identity checks); `exchange_calendars==4.13.2` +
  `alpaca-py==0.43.5` pinned + installed in `.venv` (offline suite stays stdlib-only). (3) New
  `agent.calendar_fixture` generator + hand-table cross-check; committed cross-checked fixture
  `tests/fixtures/calendar/xnys_sessions_2026H2_v1.json` (2026-06-15→12-31, 139 trading days; the fresh
  M7d holdout = exactly 20 sessions ending 2026-07-14); the cross-check caught + fixed a REAL defect in the
  hand-authored margin fixture (2026-07-02 wrongly a half-day — July 4 2026 is a Saturday). (4) The M7d
  packet's LAST two operational prerequisites DONE: §9 calendar (above) and §10 the committed
  `agent.m7_run_driver` (pin sessions fail-closed BEFORE any credentialed spend → pull → session-pinned
  row filter (`filter_rows_to_pinned_sessions`, half-day/DST-correct) → build/validate from the staged
  bytes on disk → staged-only write guard (never production) → summary.json with the packet's full
  per-gate table + NULL diagnostics incl. p99). (5) M1 tier-2b live seam BUILT fail-closed:
  `LiveQuoteFeed` (wall-clock feed with record-then-act EventWriter journaling — live sessions replay
  byte-identically through `ReplayQuoteFeed`; strict in-order no-lookahead bar firing with
  grace+watermark+vendor-skew guards; per-record fail-closed drops; stop/max-runtime backstops) +
  `databento_live_source` (bbo-1s pinned, UNVERIFIED-fail-closed until the paid live subscription; the
  one-session verification flag mirrors the historical pull's posture). (6) The paper session day-runner
  `agent.paper_session` (SOD reconcile → the frozen tick loop with its in-loop session-edge EOD → new
  idempotent `ensure_eod_reconcile` fallback → daily report; exit codes 0/1/2/3/4; strategy registry
  wires momentum v1/v2 and REFUSES the cross-sectional RS proxy pending a predeclared scan-adapter) +
  `agent.paper_report` (journal roll-up: broker-vs-modeled PnL split, rejects, kill, reconcile, feed data
  quality) — the replay rehearsal is verified end-to-end (committed observe fixture: 74 ticks, report,
  exit 0, zero orders). (7) `agent.verify_alpaca_paper`: credentialed read-only account verifier +
  gated non-marketable order drill, redacted artifact; the FD-M5-5 one-SDK-import-site wall extended to
  exactly the two `_build_real_client` sites. (8) The capstone runbook
  `docs/runbooks/paper-go-live-checklist.md`: the ordered Robin-only steps A–E (Alpaca paper account +
  verifier; the PAID Databento live realtime subscription + tier-2b verification; the S9 reviewed
  artifact via M7d incl. the RS adapter caveat; the REVIEWED paper-phase caps commit — overlays are
  tighten-only so the envelope is git-visible by design; runtime arming via `.secrets/run_gates.json`)
  + daily operation + drills. **Remaining before autonomous paper: exactly those five Robin-gated
  steps — no unbuilt code stands in the path** (the only conditional build left is the cross-sectional
  scan-adapter IF the GO'ed strategy is the RS proxy).

## Locked decisions

- **Broker:** Alpaca (paper and live share one API surface).
- **Market data:** Databento (live≡historical schema). `MarketDataTransport` abstraction keeps Polygon an
  alternate candidate. Datasets verified (M1 tier-2 2a): L1 = `EQUS.MINI`, L2 depth = `XNAS.ITCH` (mbp-10,
  REPLACE-per-record, full post-event top-10 book; single-venue Nasdaq scope noted).
- **Fill model:** hybrid, broker-authoritative — Alpaca = position-of-record; Databento depth = execution-realism
  label (never overrides the ledger).
- **Universe:** curated single-name US large-cap (~20–50), bounded.
- **First strategy:** observe-only calibration probe → historical anti-lookahead backtest gate → directional.
- **Live posture:** live-capable from day one, live-money gate OFF behind two-key arming + M8 checklist.
- **Edge before live (locked 2026-06-10, updated 2026-06-13):** after M7, paper edge-validation can start only
  after a reviewed historical artifact verifies `ok` for the current `(strategy_id, rules_hash, data_pin)`. M8 is
  considered only if that paper phase shows realized edge against the pinned criteria. Failed historical artifacts
  route to strategy/universe hardening; no go-live on vibes. Spine construction ends with M6/M7: full
  contract/review depth stays mandatory on anything touching orders or money; no further infrastructure
  extensions beyond the roadmap.

## Roadmap (each milestone: its own spec → plan → review → verify; see spec §10)

- **M0** Skeleton + abstractions + safety spine (stdlib-only; canary, journal, gates, preflight tokens, kill
  switch, charter, dashboard sandbox). ✓ **done** — 129 tests, adversarially hardened.
- **M1** Data tier (Databento recorder + replay/reconcile + bar cache; dataset matrix pinned + entitlement-verified).
  ✓ **done** — tier-1 offline (`5723e4d`, 366 tests, 23 bugs fixed across 6 review rounds) + tier-2 (2a)
  historical-verified (`2b72143`, 378 tests; L1=`EQUS.MINI`, L2=`XNAS.ITCH` mbp-10 confirmed); tier-2 (2b)
  live-verified deferred (live subscription not provisioned).
- **M2** Market-state (session/halt/LULD/SSR + fail-closed corporate actions; market calendar + session gate).
  ✓ **done on `m2-market-state`** — 532 tests; pure tradability READ only; no order submit/preflight-token mint;
  fail-closed CA validation; no module-scope `exchange_calendars`; gates OFF.
- **M3** Signal + observe-only calibration probe. ✓ **done on `m3-signal`** — 700 tests; observe-only
  (`paper_eligible=false` ledger-enforced); S1/S2/S3/S4/S6 test-mapped; calibration report + golden.
- **M4** Risk core (`IntradayMarginModel` + locate/SSR + exposure caps + drawdown kill switch).
  ✓ **done on `m3-signal`** — 896 tests; long-only (locate = deny-all stub per spec §14); broker buying
  power = ground truth; committed caps 0; S1/S8/S10 + R9 byte-exact rehydrate test-mapped.
- **M5** Paper-exec hybrid (Alpaca paper + second-quote preflight + broker/modeled fill separation).
  ✓ **done on `m3-signal`** (`a11a947`, 1520 tests) — contract rev 2 (48+5 findings) → 6 TDD waves →
  5-lens adversarial review (6 defects fixed). S1/S8 hold; committed gates OFF. (Built on `m3-signal`; later
  folded into `main` via the 2026-06-26 FF merge of `codex/m7-backtest-gate`.)
- **M6** Reconcile hardening (SOD/EOD broker reconciliation). ✓ **done on `m6-reconcile`** —
  contract frozen rev 6 (`8ef8a51`), W1 built (`7feb110`, 1574 tests), W2 built (`44686f6`,
  1590 tests), W3 built (1599 tests), W4 built (1611 tests), W5 built (1636 tests);
  W6 initial review blockers fixed+committed (`8e1f4db`, 1639 tests); separate W6 re-review clean; gates OFF.
- **M7** Backtest gate (anti-lookahead) → first paper-eligible directional strategy. ✓ **review-hardened offline
  closeout on `codex/m7-backtest-gate`** (1683 tests after historical-review hardening): v2 artifact gate, backtest engine,
  `directional.momentum_v1`, temp-only fixture builder CLI, criteria runbook, and S9 integration are green.
  Reviewed historical production artifact has not passed, so default committed artifact state remains fail-closed.
- **Strategy/universe hardening loop** (current post-M7 gate): start a new predeclared strategy/universe family
  after the M7b no-trade closeout; do not continue with a gap/adverse-selection M7c on the current 1-minute
  long-only momentum family. The active family is the M7c relative-strength packet, proxy-first: build and review
  `relative_strength.long_only_proxy_v1` before any short-side/market-neutral work. Only a reviewed artifact
  verifying `ok` can unlock paper edge-validation.
- **Search-budget stop rule** (bounds the hardening loop so "start a new family" cannot recurse indefinitely):
  the strategy search is allowed at most **two** distinct predeclared (strategy, universe) families on a fixed
  data substrate (the L1 `EQUS.MINI` 1-minute top-of-book mega-cap substrate) before a substrate-level decision is
  forced. The failed momentum family was family 1; the M7c relative-strength family (proxy then conditional
  neutral) is family 2. If family 2 produces a broad reviewed null, the next decision is NOT a third same-substrate
  family — it is an explicit substrate decision (a longer decision/holding horizon, an L2/MBP-10 depth-aware fill
  tier, or a wider liquidity-screened universe) or a documented stop. No-edge on a fixed substrate is a substrate
  conclusion, not an invitation to keep reskinning the strategy.
- **Substrate-search budget (predeclared 2026-07-02):** the substrate axis itself is budgeted, mirroring the
  two-family rule one level up: at most **two** predeclared substrate experiments on the current relative-strength
  family line before a **documented program STOP** of the autonomous edge search. The M7d longer-horizon packet is
  substrate experiment **1 of 2**. If M7d's C2 nulls on the edge gates (route A), the packet's documented STOP
  applies directly and the remaining slot is NOT auto-spent. If C2 passes edge but fails realism (route B), the
  single remaining slot may fund ONE realism-matched lever (passive/limit execution, tighter-spread universe, or
  entry-latency — informed, not auto-selected, by the predeclared realism decomposition), as its own packet +
  review + separate go.
  A second substrate null ends the program: any continuation — including a daily-horizon/EOD family line, which
  is a NEW substrate — requires a fresh explicit mandate from Robin, not a routing rule. **(2026-07-10: Robin
  granted exactly such a fresh mandate for a long-horizon fundamentals line, scoped to PACKET DRAFTING + review
  only — `docs/superpowers/specs/2026-07-10-fundamentals-longterm-research-packet.md`; it is a separate research
  line, does NOT consume the intraday substrate budget, and any data spend/credentialed run needs its own go.)**
- **Paper edge-validation phase** (post-passing-artifact, pre-M8): full autonomous paper run measured against the
  pre-pinned criteria — this run produces the "realized edge" evidence M8 requires.
- **M8** Live canary (only after realized edge; two-key arming + flatten-then-halt + go-live checklist).

## Open tracks / risks

- Data quality remains load-bearing; M1 data + M2 market-state are in place, but live realtime validation is still deferred.
- Regulatory regime in transition (FINRA 26-10 intraday margin canonical; brokers phase in to Oct 2027 — mirror
  Alpaca's actual enforcement).
- Calibration + backtest metric design specified in M3/M7.
