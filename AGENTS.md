# AGENTS.md

Instructions for coding agents working in this repository.

**The authoritative agent instructions are in [`CLAUDE.md`](CLAUDE.md).** Read it before doing anything — it
carries the safety boundaries, which are not optional, plus the commands and conventions.

Short version, so nothing depends on a second file being loaded:

- This is an autonomous US-equities trading agent that has **never placed a trade**. It is paper-first and
  fail-closed: nothing opens by default.
- The three run gates — `config/risk_rules.json → live_trading.enabled`, `config/agent_rules.json → enabled`,
  and `config/agent_rules.json → paper_trading.enabled` — are `false` in the committed config and **must stay
  `false`**. Do not flip them.
- No real-money orders. Live capital requires two-key arming plus a go-live checklist; no single commit or
  process supplies both keys.
- The broker is the position-of-record. Secrets live in git-ignored `.secrets/` and are never committed.
- Never commit market data: recorded quotes, bars, journals and run reports are vendor-licensed and git-ignored.
- Tests: `python3 -m unittest discover -s tests -p 'test_*.py' -t .` (the `-t .` is required). Stdlib-only, no
  install, no network, no credential reads.

If a task implicitly requires breaching any of these, stop and ask.

## Orientation

| File | What it is |
|---|---|
| `README.md` | What the project is, what it found, and why an agent that never traded is the point |
| `docs/ARCHITECTURE.md` | The seven safety patterns, each with its implementing file and its test |
| `docs/RESULTS.md` | The two nulled strategy families — predeclared criteria, measurements, stop rule |
| `PLAN.md` | Build chronology and milestone status |
| `docs/superpowers/specs/` | Per-milestone contracts and the frozen design spec |
| `docs/superpowers/reviews/` | Adversarial review handoffs and failure reviews |
| `docs/runbooks/` | Operator runbooks, including the go-live checklist |
