# PLAN.md — Stocks trading agent

## Active purpose

Build an autonomous US-equities trading agent, paper-first but live-like, reusing the Polymarket engineering
spine. Authoritative design: `docs/superpowers/specs/2026-06-08-stocks-agent-design.md`.

## Current status (2026-06-08)

- Design spec written, externally reviewed (twice) + internally reviewed (5-lens adversarial workflow), and
  reconciled to **build-ready for M0**. All external facts verified against primary sources.
- M0 + M1 implementation plans written and reconciled under `docs/superpowers/plans/`.
- Charter files (AGENTS.md, CLAUDE.md, PLAN.md, MEMORY.md) seeded.
- No agent code yet. Git history on `main`: `6ee1019` (spec) → `7f280a9` (external review) → `84ace6b` (5-lens
  reconcile) → `fa4bd3a` (review round 2) → charter files.
- **Next step:** implement **M0** (skeleton + abstractions + safety tests), all gates OFF.

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

- **M0** Skeleton + abstractions + safety spine (stdlib-only; canary, journal, gates, preflight stubs, kill
  switch, charter, dashboard sandbox). ← next
- **M1** Data tier (Databento recorder + replay/reconcile + bar cache; dataset matrix pinned + entitlement-verified).
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
