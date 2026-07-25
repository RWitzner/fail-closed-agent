> **Provenance.** Written 2026-05-31 in a **separate research workspace** (a prediction-market observe-only
> paper agent) that is **not part of this repository** and is not published. These are the predeclared kill
> criteria for a closing-line-value measurement lab in sports markets — a different market from the equities
> work in this repository. Reproduced unchanged. Any file or script it refers to belongs to that other workspace.

# Sports CLV edge-lab kill criteria

Date: 2026-05-31

Boundary: observe-only measurement. No real money, no wallet, no signing, no live orders.

## Thesis under test

Polymarket 2-way sports moneylines may occasionally trade at executable asks below a devigged sharp reference line (Pinnacle via OddsPapi). The lab is allowed to find "no edge".

## Primary metric

Realized CLV for a shadow `would_open` row:

`sharp_close_fair_prob - poly_entry_ask`

The entry price is always the executable Polymarket ask, not midpoint.

## Kill rules

Kill or redesign the lab before any live-money discussion if, after at least 50 qualifying games over at least 14 calendar days:

- Median realized CLV is below +1.5 percentage points.
- Fewer than 10 qualifying 2-way sharp-covered games appear per week.
- Fewer than 5 `would_open` rows survive the pre-registered +1.5pp edge threshold.
- Resolution scoring is negative after at least 20 resolved `would_open` rows.
- More than 10% of rows are unmatchable or stale-source rows after fixture/team matching fixes.

## Non-goals

- No threshold lowering to create activity.
- No copy-trading whale fills.
- No live market-making or maker order loop.
- No 3-way/draw, exact-score, esports, or void-prone structures until a separate spec exists.

## Review cadence

Review the dashboard track record weekly. If the lab does not reach enough qualifying games, the correct conclusion is "insufficient ownable edge under current feed/universe", not a forced trade thesis.
