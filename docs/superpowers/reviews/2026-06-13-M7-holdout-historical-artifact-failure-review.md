# M7 Holdout Historical Artifact Failure Review

Date: 2026-06-13

## Scope

- Strategy: `directional.momentum_v1`
- Input tier: `EQUS.MINI:bbo-1m`
- Window: `2026-04-09T13:30:00` to `2026-05-08T20:00:00`
- Rules hash: `a4298880ef6136f69a627c62ebc002d9c2c85f7d1e7ae5f3d5f3e96647c06bf6`
- Builder commit: `45c62fda157fadbfbb21bf7fa27eb124da9a0cd8`
- Symbols: `AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AVGO, COST, NFLX`
- Contract path: `agent.backtest_historical.write_m7_historical_artifact`
- Reviewable local evidence summary:
  `reports/m7_historical_runs/2026-06-13-holdout-bbo1m-v1/summary.json` (gitignored; raw/normalized licensed
  data is not committed)

The holdout rerun used the same broader large-cap/Nasdaq-leaning universe as the prior broader failure review,
but a non-overlapping earlier historical window. Each manifest included the hash-bound `universe` block introduced
by the strategy/universe hardening pass. No raw licensed rows are committed.

## Verdict

The holdout historical attempt did not pass M7 criteria. No artifact was written to production
`artifacts/backtests/`; the directory remains `.gitkeep` only. Paper edge-validation must not start.

Every reviewed symbol failed the required positive return gates. Most also failed one or both realism-gap gates.
This confirms the current blocker is strategy edge, not only the first reviewed sample period.

## Metrics

| Symbol | Valid rows | Trades | Net execution-realistic PnL | Active PnL | Profit factor | Avg trade bps | P95 gap bps | Max fill gap bps |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| AAPL | 10547 | 1286 | -482.300000 | -261.860000 | 0.392523 | -4.592064 | 15.165476 | 119.677150 |
| AMZN | 10165 | 1328 | -468.170000 | -334.060000 | 0.471907 | -4.138787 | 14.686444 | 136.092289 |
| AVGO | 9424 | 1165 | -1434.270000 | -1118.350000 | 0.134919 | -15.235490 | 41.681188 | 124.383947 |
| COST | 8548 | 140 | -496.380000 | -396.615000 | 0.001328 | -35.809488 | 84.332494 | 460.807126 |
| GOOGL | 9585 | 1324 | -564.170000 | -394.405000 | 0.373312 | -5.544256 | 19.852012 | 55.692973 |
| META | 9766 | 959 | -932.650000 | -766.005000 | 0.104599 | -14.984139 | 37.119525 | 63.241107 |
| MSFT | 10847 | 1269 | -782.780000 | -723.570000 | 0.278969 | -7.505781 | 19.866266 | 91.976828 |
| NFLX | 9529 | 1218 | -297.380000 | -132.850000 | 0.577747 | -2.586230 | 3.256445 | 126.387176 |
| NVDA | 13435 | 1415 | -264.520000 | -149.300000 | 0.713037 | -2.094101 | 5.046682 | 64.670182 |
| TSLA | 12690 | 1264 | -1196.210000 | -906.890000 | 0.252079 | -12.372290 | 23.400008 | 63.451777 |

## Verification

- `reports/m7_historical_runs/2026-06-13-holdout-bbo1m-v1/summary.json` records `passing_symbols: []`.
- `artifacts/backtests/` contains only `.gitkeep`.
- The holdout quote files, manifests, and staging artifacts are under gitignored `reports/`; no raw licensed data
  is committed.

## Next Action

Do not start M8 or paper edge-validation. Continue strategy/universe hardening with a new predeclared hypothesis
and require a reviewed historical artifact to verify `ok` before any paper-phase run.
