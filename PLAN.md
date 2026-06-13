# PLAN.md — Stocks trading agent

## Active purpose

Build an autonomous US-equities trading agent, paper-first but live-like, reusing the Polymarket engineering
spine. Authoritative design: `docs/superpowers/specs/2026-06-08-stocks-agent-design.md`.

## Current status (2026-06-09)

- Design spec + M0/M1 plans written, externally reviewed (twice) + internally reviewed (5-lens adversarial
  workflow), reconciled. All external facts verified against primary sources.
- **M0 built, hardened, and green (129 tests, deterministic).** TDD throughout; an adversarial review of the
  M0 code found 4 bugs, and a second (code) review round found more (token forgery via the private mint key,
  journal dropping a corrupt complete tail, per-stream seq being per-writer, float qty reaching submit) — all
  fixed test-first and re-probed closed. Token authority now lives in an immutable registry, not the token
  object. Stdlib-only; no network; committed gates OFF.
- Git history on `main`: spec/review/reconcile docs → charter files → `4230c8f` (M0 feat) → `5e45bf4` (M0 harden)
  → `70115f8` (M0 harden round 2, 129 tests).
- **M1 tier-1 contract FROZEN + verified (2026-06-09):** `docs/superpowers/specs/2026-06-09-M1-tier1-contract.md`
  (designed via 3-architect panel → synthesis → critic → revision → independent re-critique = READY-TO-BUILD).
  It pins every module API, the L2 `book_hash` encoding, fixture schemas, and the test→safety-invariant map.
- **Databento access (2026-06-09):** provisioned key is **historical-only**; live realtime is a separate paid
  subscription (not provisioned). Since live ≡ historical schema, the whole stack builds + verifies on historical
  now; the live feed is deferred. M1 tier-2 therefore splits: **(2a) historical-verified** (runnable now) vs
  **(2b) live-verified** (deferred blocker on the subscription).
- **M1 tier-1 (offline) BUILT + HARDENED + COMMITTED (`5f9ef97`):** full data tier under `scripts/recorder/`
  (event/event_row/book_state/book_hash/persistence/replay/reconcile/recorder/status/bar_cache/entitlement) +
  `marketdata/databento.py`, TDD. **366 tests green**, no-network/no-creds clean, golden `book_hash`es stable.
  Six adversarial review rounds (each repro-gated) found **23 confirmed defects — all fixed TDD** (contract items
  C1–C9, D1–D9, E1–E3, F1–F2, G1–G4 appended to the frozen contract; round 6 clean).
- **M1 tier-2 (2a) historical-verified DONE + COMMITTED (`3f9d7b2`, **378 tests**):** credentialed verifier ran
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
  verified + 5 RC defects, applied) → READY-TO-BUILD (`73d3e29`). Findings archive:
  `docs/superpowers/reviews/2026-06-10-M5-contract-critic-findings.json`. Build in 6 TDD waves
  (`72fc789`→`0616c3a`): wave 1 vocab/config/pricing/order-state (1040), wave 2 preflight ladder +
  TOCTOU consume + realism/fees + synthetic/gate (1251), wave 3 exec_ledger + alpaca/fake/flatten
  brokers (1358), wave 4 paper_book + replay_feed + run_lock/secrets (1446), wave 5 orchestrator +
  CLI + exec_fixtures (1466), wave 6 E2Es + committed goldens + kill drill + canary/purity (1513).
  S1 holds: committed config + absent run-gates file ⇒ zero submits over a full orchestrator run;
  kill-flatten works in every composition (the M5C-1 blocker pin). Build errata logged in wave-5/6
  commit messages (notable: M4 `leg_cap_notional` requires explicit strategy limits — M7 decision
  item; 2 §3 import-row omissions whitelisted in the AST guard).
  **HARDENED (`ad14cf6`, 1520 tests): 5-lens repro-gated adversarial review (safety/execution/
  determinism/conformance/lifecycle) — determinism + execution math clean; 6 defects fixed TDD:
  LC-2 (high — kill-with-close-in-flight crash + double-sell risk → retire close task, single
  flatten of record), LC-1/SF-1 (major — over-qty exit crashed the loop → journaled refusal),
  EC-1 (dead §J/EX-9 sell-fee path → close now feeds a sell-side modeled fill; goldens
  regenerated), SF-2 (FlattenUnpriced token leak → injected void_token), CC-1 (live-path
  `secret`→`secret_key`), DJ (fill_delta pinned-context hardening). M5 DONE.**
  **Plan confirmed (2026-06-10): finish the remaining milestones M6→M7, then a full autonomous paper
  edge-validation phase before any M8 step** (see "Edge before live" under Locked decisions). Merge-to-main
  decision still open (nothing merged yet; branches stack m2-market-state→m3-signal→m6-reconcile).
- **M6 IN PROGRESS on branch `m6-reconcile` (2026-06-13).**
  - **Contract FROZEN rev 6 READY-TO-BUILD, committed `1e96d5d`:**
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
  - **W1 DONE, committed `2c2b6ed` (suite 1574 = 1520 + 54):** vocab/dataclasses/typed boundary +
    ReconcileLedger facade (§B.1a validation, v-first/rules_hash-last) + replay + latch fold (FD-M6-6,
    M6C-22, RC-8 residue lifecycle, §B.1b zero state). Builder errata in module docstrings.
  - **W2 DONE, committed `436f7b0` (suite 1590 = 1574 + 16):** pure diff core in
    `scripts/agent/broker_reconcile.py` plus §I file-1 cases 2–20/24–26 in
    `tests/agent/test_broker_reconcile.py`. Covers `diff_positions` union/absence/short semantics,
    LIFO adjustment planning, cost tolerance + re-anchoring, deferral/immediate no-plan paths,
    `diff_cash` exact telescope + fill-id dedupe, `resolve_order_probe` table branches, and
    `identity_note` cent boundary. Verification: targeted broker-reconcile test file green, full
    suite green (`python3 -m unittest discover -s tests -p 'test_*.py' -t .`), `git diff --check`
    clean, and W2 diff audited to exactly the two W2 files.
  - **Next:** W3 (PaperBook fold) → W4 (orchestrator wiring) → W5 (CLI/canary/purity, suite
    ≈1683) → W6 (multi-lens repro-gated adversarial review, separate authoring/review). Each wave:
    build agent (NO git) → own suite run + git-audit → commit.

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
- **Edge before live (locked 2026-06-10):** after M7, run a **full autonomous paper phase**; M8 is considered
  only if that phase shows realized edge against success criteria **pinned in advance** (to be fixed in the M7
  contract — e.g. positive modeled PnL after realistic costs over a minimum number of trading days, a
  realism-gap ceiling between broker fills and the Databento-depth label, a drawdown bound). The paper phase is
  the evidence gate — no go-live on vibes. Spine construction ends with M6/M7: full contract/review depth stays
  mandatory on anything touching orders or money; no further infrastructure extensions beyond the roadmap.

## Roadmap (each milestone: its own spec → plan → review → verify; see spec §10)

- **M0** Skeleton + abstractions + safety spine (stdlib-only; canary, journal, gates, preflight tokens, kill
  switch, charter, dashboard sandbox). ✓ **done** — 129 tests, adversarially hardened.
- **M1** Data tier (Databento recorder + replay/reconcile + bar cache; dataset matrix pinned + entitlement-verified).
  ✓ **done** — tier-1 offline (`5f9ef97`, 366 tests, 23 bugs fixed across 6 review rounds) + tier-2 (2a)
  historical-verified (`3f9d7b2`, 378 tests; L1=`EQUS.MINI`, L2=`XNAS.ITCH` mbp-10 confirmed); tier-2 (2b)
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
  ✓ **done on `m3-signal`** (`ad14cf6`, 1520 tests) — contract rev 2 (48+5 findings) → 6 TDD waves →
  5-lens adversarial review (6 defects fixed). S1/S8 hold; committed gates OFF; nothing merged to main.
- **M6** Reconcile hardening (SOD/EOD broker reconciliation). ← **in progress on `m6-reconcile`** —
  contract frozen rev 6 (`1e96d5d`), W1 built (`2c2b6ed`, 1574 tests), W2 built (`436f7b0`,
  1590 tests); W3–W6 remain (see status above).
- **M7** Backtest gate (anti-lookahead) → first paper-eligible directional strategy. The M7 contract must pin
  the paper-phase success criteria (see "Edge before live" locked decision).
- **Paper edge-validation phase** (post-M7, pre-M8): full autonomous paper run measured against the pre-pinned
  criteria — this run produces the "realized edge" evidence M8 requires.
- **M8** Live canary (only after realized edge; two-key arming + flatten-then-halt + go-live checklist).

## Open tracks / risks

- Data quality remains load-bearing; M1 data + M2 market-state are in place, but live realtime validation is still deferred.
- Regulatory regime in transition (FINRA 26-10 intraday margin canonical; brokers phase in to Oct 2027 — mirror
  Alpaca's actual enforcement).
- Calibration + backtest metric design specified in M3/M7.
