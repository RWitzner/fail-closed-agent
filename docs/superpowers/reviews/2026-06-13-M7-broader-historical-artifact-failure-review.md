# M7 Broader Historical Artifact Failure Review

Date: 2026-06-13

## Reviewed Run

- Strategy: `directional.momentum_v1`
- Input tier: `EQUS.MINI:bbo-1m`
- Window: `2026-05-11T13:30:00` to `2026-06-09T20:00:00`
- Rules hash: `a4298880ef6136f69a627c62ebc002d9c2c85f7d1e7ae5f3d5f3e96647c06bf6`
- Builder commit: `184516a3c593efadccf4803d4503b62203afb369`
- Symbols: `AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AVGO, COST, NFLX`
- Contract path: `agent m7-historical-artifact --input-manifest-json ...`
- Local evidence summary: `reports/m7_historical_runs/2026-06-13-broader-bbo1m-v1/summary.json` (gitignored;
  raw/normalized licensed data is not committed)

Each symbol used its own manifest JSON. The manifest hash recomputed over the manifest body and the
`quote_rows_sha256` hash recomputed over the normalized JSONL rows. No raw licensed rows are committed.

## Verdict

The broader historical attempt did not pass M7 criteria. No artifact was written to production
`artifacts/backtests/`; the directory remains `.gitkeep` only. Paper edge-validation must not start.

The run did satisfy the intended "broader universe" branch of the next-action decision, but the first strategy did
not show cost-adjusted historical edge on any reviewed symbol in this sample.

Failed gate across all symbols:

- `metrics.pass`
- `positive_net_pnl`
- `positive_active_pnl`
- `profit_factor_min`
- `avg_trade_bps_positive`

## Metrics

| Symbol | Valid rows | Trades | Net execution-realistic PnL | Active PnL | Profit factor | Avg trade bps |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| AAPL | 10000 | 1171 | -549.910000 | -251.575000 | 0.344377 | -5.141027 |
| MSFT | 10541 | 1025 | -856.780000 | -658.610000 | 0.206274 | -9.906893 |
| NVDA | 14981 | 1258 | -545.320000 | -201.400000 | 0.539612 | -4.942977 |
| AMZN | 9537 | 1149 | -439.270000 | -212.030000 | 0.404420 | -4.706998 |
| META | 9619 | 848 | -1045.670000 | -725.505000 | 0.064771 | -20.139305 |
| GOOGL | 9941 | 1103 | -631.520000 | -327.510000 | 0.316936 | -7.456952 |
| TSLA | 12071 | 1139 | -1007.980000 | -659.290000 | 0.303862 | -10.417311 |
| AVGO | 10430 | 1006 | -1458.380000 | -1129.080000 | 0.177543 | -16.944863 |
| COST | 8277 | 152 | -444.650000 | -348.030000 | 0.049507 | -30.190545 |
| NFLX | 8690 | 1072 | -326.180000 | -58.685000 | 0.544969 | -3.147759 |

## Verification

- `artifacts/backtests/` contains only `.gitkeep`.
- `verify_artifact("directional.momentum_v1", rules_hash, data_pin, artifacts_dir="artifacts/backtests")`
  returned `missing` for every reviewed symbol data pin.
- `git diff --check` passed.
- Full offline suite passed: `python3 -m unittest discover -s tests -p 'test_*.py' -t .` ran 1679 tests, OK.

## Next Action

Do not tune thresholds or commit an artifact.

Next loop is strategy/universe hardening:

- revise `directional.momentum_v1` under a new reviewed strategy version, or
- predeclare a materially different universe/sample selection with a defensible hypothesis and rerun the
  historical artifact tier.

M8 remains blocked. Paper edge-validation remains blocked until a reviewed production artifact verifies `ok`.
