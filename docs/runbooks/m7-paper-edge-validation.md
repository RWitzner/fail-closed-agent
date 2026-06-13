# M7 Paper Edge Validation Runbook

M7 only makes `directional.momentum_v1` paper-eligible after the reviewed v2
artifact gate passes. M8 remains blocked until the paper phase produces all
evidence below and the reviewed artifact still verifies against the current
`(strategy_id, rules_hash, data_pin)`.

## Historical Artifact Gate

Build the reviewed historical artifact before starting paper validation:

```bash
PYTHONPATH=scripts python3 -m agent m7-historical-artifact \
  --quotes-jsonl <normalized-historical-quotes.jsonl> \
  --input-manifest-json <historical-input-manifest.json> \
  --artifacts-dir artifacts/backtests \
  --symbol <SYMBOL> \
  --instrument-id <DATABENTO_INSTRUMENT_ID> \
  --dataset <DATASET> \
  --schema <SCHEMA> \
  --rules-hash <CURRENT_ASSEMBLED_RULES_HASH> \
  --data-pin <DATASET>:<SCHEMA>:1m:historical:<MANIFEST_HASH> \
  --created-utc <PINNED_CREATED_UTC> \
  --builder-git-commit <CURRENT_COMMIT> \
  --allow-reviewed-artifact
```

The manifest is part of the reviewed input, not operator prose. It must be a
canonical JSON object whose `manifest_hash` recomputes from the body and whose
`quote_rows_sha256` recomputes over the normalized JSONL rows. Required manifest
contents include the dataset/schema/interval/symbol/instrument id, row count,
source window, normalizer id, drop counts/reasons, calendar session windows,
corporate-action blackout dates, a predeclared universe hypothesis
(`hypothesis_id`, `selection_rule`, and ordered `symbols` including the artifact
symbol), latency budget, slippage cap, fee model, pricing model, and
realism-gap model. The `data_pin` must equal
`<DATASET>:<SCHEMA>:1m:historical:<manifest_hash>`.

`EQUS.MINI:bbo-1m` normalized rows are acceptable only as an explicitly declared
historical L1 quote tier with this manifest binding. A passing artifact must not
come from ad hoc JSONL or a free-form manifest hash; if the source tier changes
to `tbbo`, produce a new manifest and data pin.

If this exits with `criteria_failed=...`, do not commit an artifact and do not
start paper edge-validation. Production `artifacts/backtests/` must remain
fail-closed until `verify_artifact(strategy_id, rules_hash, data_pin)` returns
`ok` for the reviewed triple. Reviewed writes target the exact
`artifacts/backtests` directory; nested paths under it are refused.

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
