# M7 Broader Historical Artifact Failure Review

Date: 2026-06-13

## Reviewed Run

- Strategy: `directional.momentum_v1`
- Input tier: `EQUS.MINI:bbo-1m`
- Window: `2026-05-11T13:30:00` to `2026-06-09T20:00:00`
- Rules hash: `a4298880ef6136f69a627c62ebc002d9c2c85f7d1e7ae5f3d5f3e96647c06bf6`
- Builder base: `93f5bbf82063141ca501fef1cab7d2b262fe0ce0` plus causal-time input hardening
- Symbols: `AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AVGO, COST, NFLX`
- Contract path: `agent m7-historical-artifact --input-manifest-json ...`
- Reviewable local evidence summary:
  `reports/m7_historical_runs/2026-06-13-broader-bbo1m-v2-causal/summary.json` (gitignored; raw/normalized
  licensed data is not committed)

Each symbol used its own manifest JSON. The manifest hash recomputed over the manifest body and the
`quote_rows_sha256` hash recomputed over the normalized JSONL rows. No raw licensed rows are committed.

The first broader pass (`2026-06-13-broader-bbo1m-v1`) exposed impossible rows where `ts_recv_utc < ts_event_utc`
and the event timestamp landed in year 2554. That pass is not approval evidence. The historical input contract now
rejects receive-before-event rows before snapshot/backtest assembly, and the reviewable v2-causal run recomputed
every manifest hash/data pin after dropping those impossible rows.

## Verdict

The broader historical attempt did not pass M7 criteria. No artifact was written to production
`artifacts/backtests/`; the directory remains `.gitkeep` only. Paper edge-validation must not start.

The run satisfies the intended "broader universe" branch of the next-action decision, but the first strategy did
not show cost-adjusted historical edge on any reviewed symbol in this sample.

Failed gate across all symbols:

- `metrics.pass`
- `positive_net_pnl`
- `positive_active_pnl`
- `profit_factor_min`
- `avg_trade_bps_positive`

Additional symbol-specific failures included `p95_realism_gap_bps_max`, `max_single_fill_divergence_bps`, and for
`COST`, `min_sessions`.

## Metrics

| Symbol | Valid rows | Causal drops | Trades | Net execution-realistic PnL | Active PnL | Profit factor | Avg trade bps |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| AAPL | 9958 | 42 | 1171 | -549.910000 | -251.575000 | 0.344377 | -5.141027 |
| MSFT | 10445 | 96 | 1025 | -856.780000 | -658.610000 | 0.206274 | -9.906893 |
| NVDA | 14948 | 33 | 1258 | -545.320000 | -201.400000 | 0.539612 | -4.942977 |
| AMZN | 9480 | 57 | 1149 | -439.270000 | -212.030000 | 0.404420 | -4.706998 |
| META | 9556 | 63 | 848 | -1045.670000 | -725.505000 | 0.064771 | -20.139305 |
| GOOGL | 9904 | 37 | 1103 | -631.520000 | -327.510000 | 0.316936 | -7.456952 |
| TSLA | 12018 | 53 | 1139 | -1007.980000 | -659.290000 | 0.303862 | -10.417311 |
| AVGO | 10351 | 79 | 1006 | -1458.380000 | -1129.080000 | 0.177543 | -16.944863 |
| COST | 8253 | 24 | 152 | -444.650000 | -348.030000 | 0.049507 | -30.190545 |
| NFLX | 8627 | 63 | 1072 | -326.180000 | -58.685000 | 0.544969 | -3.147759 |

## Verification

- Old v1 AAPL manifest is rejected by the hardened CLI:
  `m7-historical-artifact usage error: quote row 9959 ts_recv_utc must be >= ts_event_utc`.
- The v2-causal normalized quote files contain zero receive-before-event rows.
- `artifacts/backtests/` contains only `.gitkeep`.
- `verify_artifact("directional.momentum_v1", rules_hash, data_pin, artifacts_dir="artifacts/backtests")`
  returned `missing` for every v2-causal reviewed symbol data pin.
- Targeted block passed:
  `python3 -m unittest tests.agent.test_m7_historical_artifact tests.agent.test_backtest_gate_m7 tests.agent.test_m7_paper_phase_criteria tests.agent.test_m7_s9_integration tests.agent.test_config_canary`
  ran 48 tests, OK.
- `python3 -m compileall scripts tests` passed.
- `git diff --check` passed.
- Full offline suite passed: `python3 -m unittest discover -s tests -p 'test_*.py' -t .` ran 1683 tests, OK.

## Follow-up Hardening

Future reviewed historical manifests must include a hash-bound `universe` block with `hypothesis_id`,
`selection_rule`, and ordered `symbols` including the artifact symbol. The v2 artifact provenance now carries
that universe hypothesis forward, so a future passing artifact cannot be cherry-picked from an unpinned broader
scan after the fact.

## Next Action

Do not tune thresholds or commit an artifact.

Next loop is strategy/universe hardening:

- revise `directional.momentum_v1` under a new reviewed strategy version, or
- predeclare a materially different universe/sample selection with a defensible hypothesis and rerun the
  historical artifact tier.

M8 remains blocked. Paper edge-validation remains blocked until a reviewed production artifact verifies `ok`.
