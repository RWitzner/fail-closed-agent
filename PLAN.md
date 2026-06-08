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
- Git history on `main`: spec/review/reconcile docs → charter files → `4230c8f` (M0 feat) → `5e45bf4` (M0 harden).
- **Next step:** implement **M1** (data tier) — but first pin the Databento dataset/schema matrix + provision
  credentials (see the M1 plan's two acceptance tiers).

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
