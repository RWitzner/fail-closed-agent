# M7 Historical Artifact Failure Review

Date: 2026-06-13

## Reviewed Run

- Strategy: `directional.momentum_v1`
- Input: AAPL `EQUS.MINI:bbo-1m`
- Window: `2026-05-11T13:30:00` to `2026-06-09T20:00:00`
- Instrument id: `38`
- Normalized rows: `10000`
- Dropped before review input: `78` one-sided or `UNDEF_PRICE` rows
- Input manifest hash: `57ada7d610b1f01d0ce3acb2682492492ee0e2c42018b82f3bd0919bccd308c5`
- Rules hash: `a4298880ef6136f69a627c62ebc002d9c2c85f7d1e7ae5f3d5f3e96647c06bf6`

Raw licensed rows were not committed. The reviewed evidence is the manifest hash
and the deterministic artifact-builder output.

## Verdict

The historical artifact did not pass the M7 criteria, so no artifact was written
to `artifacts/backtests/` and paper edge-validation must not start.

Post-review hardening note: this failed run predates the manifest-bound
historical builder hardening. The rejection remains valid because gross modeled
PnL and net execution-realistic PnL were both negative, but the old active-PnL
number used the pre-hardening benchmark proxy and must not be used as a template
for approving a future artifact. Future reviewed attempts must use
`--input-manifest-json`, a data pin ending in the recomputed manifest hash,
quote-A/quote-B realistic fills, M5 sell-fee assumptions, measured realism gaps,
and the exact production-write guard documented in
`docs/runbooks/m7-paper-edge-validation.md`.

Failed criteria:

- `metrics.pass`
- `positive_net_pnl`
- `positive_active_pnl`
- `profit_factor_min`
- `avg_trade_bps_positive`

## Metrics

- Bars: `9114`
- Candidates: `1428`
- Trades: `1389`
- Skips: `14`
- Sessions: `21`
- Traded sessions: `21`
- Gross modeled PnL: `-58.860000`
- Fees: `138.900000`
- Net execution-realistic PnL: `-197.760000`
- Benchmark PnL: `-29.430000`
- Active PnL: `-168.330000`
- Profit factor: `0.713794`
- Average trade: `-1.558644` bps
- Wins/losses/flat: `602 / 787 / 0`

Risk and safety did not cause the failure:

- Max drawdown pct allocated: `0.001997` versus threshold `0.015000`
- Worst day pct allocated: `0.000346` versus threshold `0.007500`
- S1 canary breaches: `0`
- Live broker submits: `0`
- Artifact mismatches: `0`
- Unhandled exceptions: `0`

## Root Cause

This is not a sample-size failure. The run meets the M7 minimum sample gates
(`21` sessions, `1389` trades, `21` traded sessions).

The artifact failed because `directional.momentum_v1` showed no historical edge
on this pinned AAPL sample:

- The strategy lost money before fees: gross modeled PnL was `-58.860000`.
- Fees added another `-138.900000`, turning a weak gross result into a clearly
  negative execution-realistic result.
- The strategy also underperformed the pre-hardening benchmark proxy: active PnL
  was `-168.330000`. Future reviews must recompute active PnL with the hardened
  quote-A/quote-B benchmark model before approving any passing artifact.
- Losing trades outnumbered winning trades (`787` losses versus `602` wins).

The practical blocker is therefore strategy/input selection and cost-adjusted edge,
not the v2 artifact verifier, production write guard, or paper gate plumbing.

## Next Action

Do not tune thresholds or commit this artifact.

Next acceptable attempts are:

- run the hardened reviewed historical tier on a pre-declared broader universe
  or a different pinned symbol/sample, or
- revise `directional.momentum_v1` under a new reviewed strategy version and rerun
  the historical artifact tier from a new pinned manifest.

M8 remains blocked. Paper edge-validation remains blocked until
`verify_artifact("directional.momentum_v1", rules_hash, data_pin)` returns `ok`
for a reviewed production artifact.
