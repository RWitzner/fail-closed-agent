# M7b Momentum V2 Failure Review

Date: 2026-06-13

## Scope

- Strategy: `directional.momentum_v2`
- Code path: `agent.backtest_historical.write_m7_historical_artifact(strategy_id="directional.momentum_v2")`
- Criteria: unchanged M7 pinned paper/M8 artifact criteria
- Production artifact write: none

M7b added a stricter strategy version rather than tuning thresholds. `directional.momentum_v2` requires aligned
positive short/medium momentum, EMA and SMA trend confirmation, bounded positive `z_ret_21`, positive realized
volatility, and a spread cap before emitting a long-only BUY candidate. The historical artifact builder now accepts
an explicit strategy id and writes/verifies `<strategy_id>.json`, while v1 remains compatible.

## Verdict

`directional.momentum_v2` did not pass M7 criteria. No artifact was written to production `artifacts/backtests/`,
and paper edge-validation must not start.

The valid reviewed holdout replay failed every symbol. The strategy reduced candidate/trade counts materially versus
v1, but the cost-adjusted net PnL stayed negative across the holdout universe. Two symbols (`NFLX`, `NVDA`) had
positive active PnL, but still failed positive net PnL, profit factor, and average-trade-bps gates.

The broader v2-causal manifests are no longer valid review inputs because they predate the required hash-bound
`universe` block. A diagnostic replay with reconstructed temp manifests over the previously documented broader
universe also failed every symbol; that diagnostic is not approval evidence.

## Holdout Metrics

Run: `2026-06-13-holdout-bbo1m-v1`

| Symbol | Candidates | Trades | Net execution-realistic PnL | Active PnL | Profit factor | Avg trade bps | P95 gap bps | Max gap bps |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| AAPL | 296 | 175 | -57.040000 | -14.455000 | 0.495087 | -3.968816 | 16.122460 | 31.186873 |
| AMZN | 363 | 232 | -46.740000 | -22.695000 | 0.664200 | -2.365605 | 10.968778 | 17.071665 |
| AVGO | 108 | 42 | -43.840000 | -26.340000 | 0.139380 | -13.017249 | 38.526648 | 40.225066 |
| COST | 3 | 2 | -7.190000 | -3.850000 | 0.000000 | -36.052027 | 40.257084 | 40.257084 |
| GOOGL | 240 | 133 | -47.220000 | -38.390000 | 0.509708 | -4.513967 | 20.551850 | 37.499053 |
| META | 50 | 19 | -26.310000 | -12.185000 | 0.006795 | -21.108700 | 52.373281 | 52.373281 |
| MSFT | 217 | 103 | -73.500000 | -47.700000 | 0.252897 | -8.696002 | 21.691974 | 91.976828 |
| NFLX | 339 | 235 | -50.800000 | 15.075000 | 0.621235 | -2.291282 | 3.779902 | 5.958184 |
| NVDA | 585 | 389 | -44.940000 | 47.945000 | 0.824522 | -1.294819 | 3.988513 | 7.340527 |
| TSLA | 173 | 98 | -11.480000 | -39.570000 | 0.863121 | -1.530178 | 17.399786 | 27.219619 |

## Diagnostic Broader Replay

Run: `2026-06-13-broader-bbo1m-v2-causal+universe-reconstructed-diagnostic`

This replay used temp manifests with the previously documented broader universe inserted and newly recomputed
manifest hashes/data pins. It is useful for diagnosing v2 behavior but is not reviewed approval evidence because
the manifest was reconstructed after the original broader run.

| Symbol | Candidates | Trades | Net execution-realistic PnL | Active PnL | Profit factor | Avg trade bps | P95 gap bps | Max gap bps |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| AAPL | 258 | 164 | -60.050000 | 3.985000 | 0.474582 | -4.007540 | 11.677197 | 18.338675 |
| AMZN | 305 | 191 | -40.010000 | -9.705000 | 0.613691 | -2.589352 | 9.944385 | 30.062599 |
| AVGO | 63 | 35 | -60.340000 | -26.550000 | 0.239092 | -19.996951 | 34.603286 | 54.081038 |
| COST | 5 | 1 | -0.830000 | -0.650000 | 0.000000 | -8.302948 | 7.502663 | 7.502663 |
| GOOGL | 203 | 112 | -68.360000 | -26.460000 | 0.279966 | -7.946342 | 17.863523 | 39.382047 |
| META | 37 | 17 | -26.170000 | -12.875000 | 0.029663 | -25.112946 | 37.916842 | 37.916842 |
| MSFT | 116 | 56 | -64.240000 | -20.930000 | 0.075417 | -13.557923 | 25.369330 | 30.932391 |
| NFLX | 257 | 195 | -35.710000 | 10.470000 | 0.720448 | -1.892158 | 3.429943 | 11.456722 |
| NVDA | 457 | 314 | -121.760000 | 46.900000 | 0.561067 | -4.416681 | 7.266249 | 21.177939 |
| TSLA | 240 | 147 | -106.760000 | -19.860000 | 0.409579 | -8.544338 | 18.538394 | 49.820789 |

## Verification

- RED test run failed before implementation on missing `MomentumV2Strategy`, missing `strategy_for_id`, missing
  historical `strategy_id`, and missing CLI `--strategy-id`.
- Targeted GREEN run passed:
  `python3 -m unittest tests.agent.test_directional_momentum_m7 tests.agent.test_m7_historical_artifact tests.agent.test_backtest_gate_m7`
  ran 30 tests, OK.
- `python3 -m compileall scripts tests` passed.
- `git diff --check` passed.
- Full offline suite passed: `python3 -m unittest discover -s tests -p 'test_*.py' -t .` ran 1688 tests, OK.
- `artifacts/backtests/` contains only `.gitkeep`.
- Holdout replay returned `criteria_passed=False` for every symbol and `verify_status=missing` because no passing
  temp artifact was written.
- The broader original manifests were rejected by the current manifest contract because `input_manifest.universe`
  is missing.

## Next Action

Do not start paper edge-validation or M8. The next strategy loop should move beyond a long-only same-bar momentum
filter and test a different predeclared hypothesis, for example mean-reversion after overextension, longer holding
horizons, or a no-trade conclusion for this L1-only one-minute setup.
