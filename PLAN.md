# PLAN.md — Stocks trading agent

## Active purpose

Build an autonomous US-equities trading agent, paper-first but live-like, reusing the Polymarket engineering
spine. Authoritative design: `docs/superpowers/specs/2026-06-08-stocks-agent-design.md`.

## Current status (2026-06-08)

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
- **M1 tier-1 (offline) BUILT + HARDENED (2026-06-09, uncommitted):** full data tier under `scripts/recorder/`
  (event/event_row/book_state/book_hash/persistence/replay/reconcile/recorder/status/bar_cache/entitlement) +
  `marketdata/databento.py`, TDD. **333 tests green**, no-network/no-creds clean, golden `book_hash`es stable.
  Two adversarial review rounds (each repro-gated) found **18 confirmed defects — all fixed TDD** (contract items
  C1–C9, D1–D9 appended to the frozen contract). HEAD is still `70115f8` (an agent's stray partial commit was
  reverted; M1 work is intentionally uncommitted until Robin commits).
- **Next step:** (optional) a 3rd convergence review round, then **commit** M1 tier-1, then **tier-2 (2a
  historical-verified)** — runnable now with the historical key (exact dataset codes, schema availability,
  one-symbol sample pull, sequence semantics, `mbp-10` snapshot-vs-delta). Tier-2 (2b) live stays deferred.

## Locked decisions

- **Broker:** Alpaca (paper and live share one API surface).
- **Market data:** Databento (live≡historical schema). `MarketDataTransport` abstraction keeps Polygon an
  alternate candidate. Datasets pinned per milestone (spec §5.1).
- **Fill model:** hybrid, broker-authoritative — Alpaca = position-of-record; Databento depth = execution-realism
  label (never overrides the ledger).
- **Universe:** curated single-name US large-cap (~20–50), bounded.
- **First strategy:** observe-only calibration probe → historical anti-lookahead backtest gate → directional.
- **Live posture:** live-capable from day one, live-money gate OFF behind two-key arming + M8 checklist.

## Roadmap (each milestone: its own spec → plan → review → verify; see spec §10)

- **M0** Skeleton + abstractions + safety spine (stdlib-only; canary, journal, gates, preflight tokens, kill
  switch, charter, dashboard sandbox). ✓ **done** — 129 tests, adversarially hardened.
- **M1** Data tier (Databento recorder + replay/reconcile + bar cache; dataset matrix pinned + entitlement-verified). ← next
- **M2** Market-state (session/halt/LULD/SSR + fail-closed corporate actions; market calendar + session gate).
- **M3** Signal + observe-only calibration probe.
- **M4** Risk core (`IntradayMarginModel` + locate/SSR + exposure caps + drawdown kill switch).
- **M5** Paper-exec hybrid (Alpaca paper + second-quote preflight + broker/modeled fill separation).
- **M6** Reconcile hardening (SOD/EOD broker reconciliation).
- **M7** Backtest gate (anti-lookahead) → first paper-eligible directional strategy.
- **M8** Live canary (only after realized edge; two-key arming + flatten-then-halt + go-live checklist).

## Open tracks / risks

- Data quality is the load-bearing risk; M1 is make-or-break.
- Regulatory regime in transition (FINRA 26-10 intraday margin canonical; brokers phase in to Oct 2027 — mirror
  Alpaca's actual enforcement).
- Calibration + backtest metric design specified in M3/M7.
