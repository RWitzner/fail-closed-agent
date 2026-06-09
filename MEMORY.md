# MEMORY.md — Stocks trading agent (stable facts)

Stable profile + project facts. Ephemeral status lives in `PLAN.md`; the design is in
`docs/superpowers/specs/2026-06-08-stocks-agent-design.md`.

## Project

- Autonomous **US-equities** trading agent, **paper-first but live-like**, reusing the Polymarket spine
  (`<sibling-workspace>`). ~60–70 % of that spine transfers; the prediction-market alpha + the live
  data tier are rebuilt.

## Milestone outcomes (durable)

- **M0 (skeleton + safety spine) — done + adversarially hardened.** An adversarial review of the M0 code found
  and fixed 4 bugs test-first: journal crash-recovery (truncate dangling tail on reopen), kill-switch freeze on
  short/zero positions (per-position isolation + finally-halt), reduce-only validation (validate against the
  position's sign/size/symbol, not the caller's flag), and token single-use (mint-side nonce registry, copy/pickle
  hostile). A second code-review round hardened further: token **authority now lives in an immutable module
  registry keyed by nonce** (not on the token object), so a token built directly with the private mint key has no
  authorization and cannot open; journal `replay` drops only a truly truncated tail (no final newline) and treats
  a newline-terminated bad row as fatal; per-stream `seq`/lock is path-keyed (shared across writer instances);
  `OrderIntent` rejects non-finite/non-Decimal qty; `make_server` binds 127.0.0.1 only; `BrokerBase` makes the
  preflight chokepoint non-bypassable. 129 deterministic, stdlib-only tests. Lesson: put authority in a
  server-side registry, not in client-held objects — and the reduce-only path (the one path that can submit) must
  never trust caller-asserted intent.

- **M2 (market-state) — done + hardened.** Built pure market calendar/session gate, halt/LULD/SSR tradability
  decider, status ledger, session-aware liveness seam, market-state cache, config provenance strings, and
  fail-closed corporate actions. M2 emits reads/ledger rows only: no order submits, no preflight-token minting,
  no network/credential use in tests, no module-scope `exchange_calendars`, committed gates OFF. Final hardening
  added symbol/status/NBBO identity checks and CA identifier/provenance canonicalization so blank durable IDs,
  mismatched persisted provenance, mismatched fetcher/source boundaries, and whitespace-mirrored `source_ca_id`s fail
  closed. 532 deterministic tests.

## Hard rules (never cross without explicit, separately-approved instruction)

- No real-money orders. `live_trading.enabled = false`; run gates (`enabled`, `paper_trading.enabled`) = `false`
  on the committed config.
- Live capital requires **two-key arming** (committed flag + uncommitted runtime secret) + the M8 go-live
  checklist.
- Broker = position-of-record; modeled fill never overrides the broker ledger.
- Committed-config canary (S1): no opening/position-increasing order is ever submitted.
- Secrets in `.secrets/`, never committed; tests do no network I/O.

## Locked decisions

- Broker **Alpaca**; market data **Databento** (`MarketDataTransport`, Polygon = alternate candidate); **hybrid
  broker-authoritative** fill model; curated **single-name US large-cap** universe; **observe-only calibration
  probe** first, then a **backtest gate** before any paper-eligible strategy.

## Verified external facts (2026-06-08)

- **FINRA Notice 26-10:** intraday margin (amended Rule 4210) **replaces** the PDT day-trade-count + $25k
  minimum; effective 2026-06-04, phase-in to 2027-10-20. Use `IntradayMarginModel` as canonical; legacy PDT is
  compat-only and must mirror Alpaca's actual enforcement during phase-in.
- **Databento EQUS.MINI** is L1 top-of-book (no MBP-10, no `status` schema); L2 `mbp-10` and L3 `mbo` need other
  datasets — pinned per milestone.
- **Alpaca** paper and live share one API; paper does **not** simulate dividends/CA → corporate actions are
  fail-closed (cross-validated, ex-date blackout).

## Conventions

- Determinism: `json.dumps(sort_keys, separators)`, Decimal-as-string, correlation IDs + monotonic `seq`.
- Money: `BrokerUSD` (ledger) vs `ModeledUSD` (strategy eval) — never conflate.
- Time: market logic in ET; persist UTC.

## Communication

Robin prefers short, direct **Danish**; evidence over vibes; facts / assumptions / opinions kept distinct.
