# M7b Diagnostic Family Closeout Review

Date: 2026-06-13

## Scope

- Strategy family: L1/BBO 1-minute same-bar or short-horizon long-only momentum.
- Current strategy id: `directional.momentum_v2`.
- Diagnostic source:
  `reports/m7_historical_runs/2026-06-13-holdout-bbo1m-v2-diagnostics/index.json`.
- Source run: `2026-06-13-holdout-bbo1m-v1`.
- Holdout window: `2026-04-09_to_2026-05-08_bbo1m_holdout`.
- Rules hash: `a4298880ef6136f69a627c62ebc002d9c2c85f7d1e7ae5f3d5f3e96647c06bf6`.
- Production artifact write: none.

This review closes the current momentum-family diagnostic loop. It does not approve paper trading, live trading,
threshold relaxation, symbol cherry-picking, or a production artifact write.

## Verdict

Do not implement an M7c gap/adverse-selection gate on the current `directional.momentum_v2` family.

The bounded diagnostics explain part of the loss pattern, but they do not provide a robust production-track
hypothesis. The current L1/BBO 1-minute long-only momentum family is closed for the M7 track as a no-trade
conclusion. Future research must restart as a new predeclared strategy/universe family before any reviewed
artifact loop.

Paper edge-validation and M8 remain blocked because production `artifacts/backtests/` contains only `.gitkeep` and
no reviewed artifact verifies `ok`.

## Diagnostic Evidence

The diagnostic index is a local/exported analysis artifact, not approval evidence. It is complete for the bounded
trade/skip rows used here: `trade_rows_available=1428`, `skip_rows_available=907`,
`any_trade_rows_truncated=false`, and `any_skip_rows_truncated=false`.

The index reconciliation is intentionally not treated as artifact proof. It reports
`status=not_checked_strategy_mismatch` because the source summary is v1 while the diagnostics are v2.

| Diagnostic slice | Trades | Net execution-realistic PnL | Active PnL | Symbol breadth | Read |
| --- | ---: | ---: | ---: | --- | --- |
| Full v2 diagnostic | 1428 | -409.060000 | -142.165000 | 0/10 net-positive symbols; 2/10 active-positive symbols | Fails the pinned M7 edge requirement. |
| Gap q1 | 286 | 5.680000 | 23.690000 | 3/6 net-positive symbols; 6/6 active-positive symbols | Too small and too narrow to promote. |
| Gap <= 5 bps | 1084 | -141.850000 | 35.760000 | 9 symbols | Broad low-gap regime still loses net. |
| Entry-spread q1 | 286 | -51.240000 | -18.655000 | 0/7 net-positive symbols | Simple spread filtering fails. |
| Entry-spread q1+q2 | 572 | -91.120000 | 6.520000 | 0 net-positive symbols | Low spread does not isolate net edge. |
| Adverse-entry-move q1 | 286 | -52.700000 | 186.055000 | 2/9 net-positive symbols; 8/9 active-positive symbols | Explains benchmark-relative behavior, not tradable net edge. |

Concentration also argues against promotion. NVDA alone is 27.2409% of trades, and the top two symbols are 43.6975%
of trades. In the only net-positive simple gap bucket, NVDA contributes 13.090000 of the bucket's total 5.680000 net
PnL; excluding NVDA, gap q1 is approximately -7.410000.

Gap q1 by symbol:

| Symbol | Trades | Net execution-realistic PnL | Active PnL |
| --- | ---: | ---: | ---: |
| AAPL | 24 | -3.630000 | 2.595000 |
| AMZN | 27 | 2.580000 | 2.485000 |
| GOOGL | 8 | 1.780000 | 0.630000 |
| MSFT | 4 | -3.100000 | 0.750000 |
| NFLX | 80 | -5.040000 | 4.090000 |
| NVDA | 143 | 13.090000 | 13.140000 |

## Why This Is Not M7c-Ready

- The base family is negative after costs: -409.060000 net execution-realistic PnL and -142.165000 active PnL.
- No symbol has positive aggregate net PnL under the current v2 diagnostic.
- The only simple net-positive gap quintile is holdout-selected, tiny at 5.680000 total net over 286 trades, and
  becomes negative without NVDA.
- Entry-spread filtering is not a rescue path; q1 and q1+q2 are both net negative.
- Adverse-entry-move filtering has positive active PnL in the best bucket but remains net negative, so it is an
  explanation of exposure/benchmark behavior rather than an entry rule.
- `sample_window_id` is null. The failed holdout diagnostic must not become the threshold source for a new reviewed
  artifact.
- The current family already failed AAPL-only v1, broader v1, holdout v1, and holdout/diagnostic v2 attempts.

## Stop Rule

Stop the current L1/BBO 1-minute same-bar or short-horizon long-only momentum family for M7.

Do not:

- run a reviewed M7c artifact for `directional.momentum_v2` plus a gap/adverse-selection gate,
- relax the pinned M7 thresholds,
- select only NFLX/NVDA or another favorable subset from the failed holdout,
- treat diagnostics as production artifact approval,
- start paper edge-validation or M8.

## Allowed Next Work

The next branch, if research continues, is a new predeclared strategy/universe design pass. The best clean break is
market-neutral or relative-strength research because the evidence does not support broad long-only short-horizon
directional alpha. Longer-horizon continuation or mean reversion can be considered only after a new hypothesis
packet defines sample/holdout windows, ordered universe, feature set, concentration limits, and artifact acceptance
rules before looking at reviewed holdout PnL.

No raw quote rows were copied into this review.
