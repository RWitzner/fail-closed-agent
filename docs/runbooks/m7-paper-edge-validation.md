# M7 Paper Edge Validation Runbook

M7 only makes `directional.momentum_v1` paper-eligible after the reviewed v2
artifact gate passes. M8 remains blocked until the paper phase produces all
evidence below and the reviewed artifact still verifies against the current
`(strategy_id, rules_hash, data_pin)`.

## Required Sample

- At least 20 full RTH sessions.
- At least 30 opened-and-closed paper trades.
- At least 5 traded sessions with one or more closed trades.
- Zero unresolved broker reconciliation drift at SOD/EOD.

## Required Returns

- `net_execution_realistic_pnl_usd > 0`.
- `active_pnl_usd > 0` versus `exposure_matched_midbar_v1`.
- Profit factor >= 1.10 on execution-realistic closed trades.
- Average trade after fees > 0 bps.

## Required Risk And Realism

- Maximum drawdown <= 1.50% of allocated paper notional.
- Worst single-session loss <= 0.75% of allocated paper notional.
- P95 broker-vs-modeled realism gap <= 15 bps.
- No single fill divergence > 50 bps unless excluded by documented
  data-quality rules.

## Required Safety Evidence

- Zero S1 canary breaches.
- Zero live-broker submissions.
- Zero artifact hash/key mismatches during the paper phase.
- Zero unhandled orchestrator-loop exceptions.
- Every data-quality exclusion is journaled with a machine-readable reason.

Failure of any item blocks M8. Passing every item is necessary but not sufficient
for live trading: M8 still requires the separate two-key arming, broker dry-run,
kill-switch drill, risk caps, and explicit runbook approval.
