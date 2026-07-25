> **Provenance.** This document is a translation from Danish of
> `docs/polymarket_bot_kritisk_vurdering.md`, written on 2026-05-14 in a **separate research workspace**
> (a prediction-market observe-only paper agent) that is **not part of this repository** and is not published.
> It is an adversarial review of that project's initial trading plan, written before its market-data recorder
> and arbitrage scanners were built. It is reproduced here unchanged in substance because it is evidence that
> the same predeclaration-and-kill-criteria method was applied in a different market. Nothing in it has been
> softened. Any file, script, or log it refers to belongs to that other workspace.

# Critical assessment of the Polymarket-only paper-trading bot

## 1. The most important structural insight you have overlooked

Your biggest mistake is not "Polymarket-only". It is that you treat **single-token edge** and
**portfolio/constraint edge** as the same object.

For candidates 1–3 the "edge" does not exist in a single Yes token. It exists in a **basket of legs** with a
particular payoff vector. The decision log as it stood — one row per token carrying `my_prob`, `entry_ask`,
`edge_pp` — is therefore structurally wrong for multi-outcome and cross-market arb. You need a
`bundle_decision_id` with legs, VWAP, common executable size, fee/slippage, resolution assumptions and payoff
matrix. Otherwise you are logging false edge.

The second hard point: a Polymarket "event" is not automatically a sportsbook future. The Polymarket
documentation describes a **market** as the fundamental binary unit with a condition ID and Yes/No token IDs;
some events group several such markets, but standard multi-outcome markets can be independent in terms of
liquidity and clearing. Only **negative risk** events create a technical/mechanical binding constraint, where a
No share in one outcome can be converted into Yes shares in the other outcomes via the Neg Risk Adapter.

Source: <https://docs.polymarket.com/concepts/markets-events>

Therefore `sum(Yes asks)` is only meaningful after this gate:

```text
same event is not enough
→ require negRisk=true or manually verified exhaustive/mutually exclusive resolution set
→ include “Other”/placeholders
→ same resolution rules/source
→ current CLOB book, not Gamma display price
```

And your rule "sum Yes-ask > 1.02, fade the favorite" is wrong. If `sum(Yes asks) < 1`, there may be a
long-bundle arb: buy one Yes in every outcome, if exactly one outcome resolves Yes. If `sum(Yes bids) > 1`,
there may be a short/split-sell arb. But `sum(Yes asks) > 1` primarily says that it is expensive to buy the
whole basket; it does not say which specific favorite is overpriced. Without an external probability, "fade the
favorite" is just an unfounded directional bet.

---

## 2. Assessment of your four edge candidates

### 1. Overround arbitrage in multi-outcome events

**Verdict:** usable as a scanner, but your current interpretation is too naive.

Overround on Polymarket is not the same as sportsbook overround. On standard events it is often just a UX
grouping of independent binary markets. On `negRisk` events there is a real mechanical relation, but you still
have to trade the right side: `sum(Yes asks) < 1` for a long bundle, or `sum(Yes bids) > 1` / No-bundle for a
short bundle. "Fade the favorite because the total is 1.02" is not an arb.

**Realistic edge magnitude:** for liquid sports futures I would assume that 2pp gross is almost entirely spread,
fee, inventory, resolution risk and capital lockup, until your own data proves otherwise. On long-dated futures,
2pp can easily be zero alpha. For short, clean, unambiguous negRisk events there may be small executable
dislocations, but they become size-limited.

**Frequency:** high enough for research, low enough that a simple cron is not sufficient. Broad Polymarket
research did in fact find realized arbitrage across market rebalancing and combinatorial arb, but much of the
profit has already been extracted by sophisticated actors. In that same analysis, sports were surprisingly
absent from the profit from NegRisk rebalancing, probably because the sports profits were smaller.

Source: <https://arxiv.org/html/2508.03474v1>

**Persistence mechanism:** retail flow, longshot/fan bias, capital lockup, resolution ambiguity, and the fact
that relationship/completeness is not always machine-readable. It is not "the market is stupid"; it is friction
plus semantic complexity. The simple version gets copied away.

---

### 2. Yes+No binary arbitrage

**Verdict:** as a live trading edge it is almost dead; as a stale-data detector it is useful.

Polymarket matches complementary Yes/No orders: a Yes bid at 0.60 can match a No bid at 0.40, and $1 of
collateral can be split into Yes+No; conversely, a full Yes+No set can be merged back into collateral. That
means a visible `Yes ask + No ask < 1` in an active, liquid CLOB is normally either stale data, a rounding/tick
illusion, or an extremely short-lived microstructure event.

Source: <https://docs.polymarket.com/concepts/prices-orderbook>

**Realistic edge magnitude:** often 0 after execution; maybe sub-pp to ~1pp gross in genuine transient cases,
but fee/slippage/latency eat it.

**Frequency:** in sports with high liquidity: rare and short. An NBA arbitrage analysis across 75M order-book
snapshots on 173 games found only 7 executable single-market in-game anomalies with a median duration of 3.6
seconds. A 15-minute cron loop does not catch that; it catches stale snapshots.

Source: <https://arxiv.org/abs/2605.00864>

**Persistence mechanism:** almost none. It exists only through latency, stale books, engine transitions or very
fast retail shocks.

---

### 3. Cross-market relationship inconsistencies

**Verdict:** the best of your four, but only if you build it small, strict and event-driven.

"Team X wins the Finals" ≤ "Team X wins the Conference" is a genuine mathematical constraint. It is better than
mean reversion because it does not require an external probability. But it is not automatically a "lock-in":
legs are not atomic across markets, odds can move between legs, and resolution rules can be different enough to
break the apparent relation.

**Realistic edge magnitude:** when it exists, gross can be 50–150 bps in sports microstructure; the NBA study
found 290 combinatorial episodes with a median return of 101 bps, but 76.9% were limited to an average of 14.8
shares of executable size. That is retail scale, not a scalable strategy.

Source: <https://arxiv.org/abs/2605.00864>

**Frequency:** on very liquid, obvious pairs it is rare and disappears quickly. On more obscure relationship
graphs it can occur more often, but then resolution/rules risk rises.

**Persistence mechanism:** semantic complexity. Bots can easily check "Finals ≤ Conference", but they cannot as
easily parse all the rules, exceptions, void/50-50 cases, postponed games, leagues, conference realignment,
tiebreakers, and "will X be named winner" vs "will X win on field".

---

### 4. Time-series mean reversion

**Verdict:** drop it as an autonomous entry strategy.

"Midpoint moved > Xpp without volume" is not edge. It can be quote cancellation, a stale midpoint, a
market-maker inventory reset, hidden news, low-depth top-of-book, or a displayed-price artifact. Polymarket
itself describes the displayed price as the midpoint, but if the spread is very wide the UI may show the last
traded price instead; you should therefore not build entry signals on the midpoint alone.

Source: <https://docs.polymarket.com/concepts/prices-orderbook>

**Realistic edge magnitude:** expected negative after spread/fees, unless you have separate proof that the move
is liquidity-only and mean-reverting.

**Frequency:** many false positives.

**Persistence mechanism:** none strong. It is a research feature, not a trading edge.

---

## 3. Recommended implementation order

**First: change the schema.** Keep token-level records as child legs, but add a portfolio-level decision object:

```text
bundle_id
strategy_type
constraint_type
legs[]
payoff_vector
assumed_resolution_graph
event_id
condition_ids
token_ids
negRisk
enableNegRisk
rules_hash
resolution_source
entry_book_timestamp
local_seen_at
book_hash
tick_size
fee_rate
VWAP_to_size
gross_surplus
net_surplus
capital_lock_days
complete_fill_required
```

**Then: build in this order.**

### 1. CLOB/WebSocket recorder, not trading bot

Your first bot should measure the duration and executable depth of anomalies. Polymarket has a public WebSocket
market channel with book snapshots, price changes, best bid/ask, trade executions and market lifecycle events;
use it as the primary data source. Gamma is discovery, not execution truth.

Source: <https://docs.polymarket.com/market-data/websocket/overview>

### 2. Binary parity scanner as a stale-data test

Scan `Yes ask + No ask < 1` and `Yes bid + No bid > 1`, but log only if the CLOB book timestamp/hash is fresh,
both legs have depth, and the opportunity survives at least two independent snapshots. I would not expect profit
here; I would use it to validate data quality and latency.

### 3. NegRisk multi-outcome bundle scanner

Only `negRisk=true`, complete outcome set, no unresolved placeholders, "Other" handled explicitly. Use full-book
VWAP for the common size. Scan both the long-bundle and the short/split-sell side. Drop "fade the favorite".

### 4. Small manual relationship graph

Start with 10–30 high-confidence constraints: Finals ≤ Conference, tournament winner ≤ reaches final/semifinal,
league winner ≤ playoff qualification, if rules truly match. Every relation must have a rules hash and manual
approval. No LLM-autogenerated graph in the trading loop.

### 5. CLV/calibration layer

Since you have chosen Polymarket-only, the "truth proxy" must not be sportsbook odds; use the Polymarket closing
line/pre-event close as the internal benchmark. PnL from final resolution takes a long time and has high
variance. CLV tells you faster whether your entries are better than the subsequent market.

---

## Sizing

Without an external probability, Kelly is meaningless for directional trades. For no-arb/constraint trades,
sizing must be depth-/risk-based:

- common executable size across all legs
- max capital lock
- max event exposure
- max unresolved positions
- complete bundle or no trade

For paper: use fixed notional per completed bundle and log unfilled/partial attempts separately. Kelly on a
made-up `my_prob` is false precision.

---

## Threshold

2pp after spread is not enough for directional Polymarket-only. For pure no-arb, 1.5–2pp net can be
research-worthy, but only after fees, VWAP, slippage, stale penalty and capital lock. For directional/no-model
signals I would require 5pp+ just for paper logging — not because 5pp guarantees edge, but because lower
thresholds drown in noise.

Taker fees and maker rebates are category-/market-dependent; fetch fee data per market via CLOB
metadata/fee-rate instead of hardcoding it.

Source: <https://docs.polymarket.com/trading/fees>

---

## Sample size

100 trades is only a pipeline sanity check. 500 completed, comparable decisions is the minimum for a CLV signal.
For PnL on 1–3pp edges you need to think 1,000+ resolved trades, and even more if trades are correlated around
the same leagues/events. For futures with 3–9 months of lockup, "15 decisions" is almost zero information.

---

## 4. The silent failure modes most likely to kill the project

### 1. Stale/unexecutable data masquerading as edge

If you use Gamma `outcomePrices`, event-level liquidity, or the midpoint as the execution price, the bot will
"find" edges that do not exist. The CLOB orderbook response has token-specific bids/asks, timestamp, hash, tick
size, min order size, `neg_risk`, and last trade price; those fields must be in the log for every decision.

Source: <https://docs.polymarket.com/api-reference/market-data/get-order-book>

### 2. Non-atomic multi-leg execution

Polymarket settlement is atomic for a matched trade, but your multi-leg arb across outcomes/markets is not
atomic as a bundle. Paper trading therefore overstates PnL if you simply snapshot all the asks and assume full
completion.

Use FOK/FAK semantics in the simulator: either the whole bundle can be filled at the logged VWAP, or decision =
no trade. Polymarket's order docs distinguish precisely between FOK, FAK, GTC/GTD and post-only; simulate that
realistically.

Source: <https://docs.polymarket.com/concepts/order-lifecycle>

### 3. Resolution/rules risk disguised as arbitrage

The market title is not the resolution rule. Rules, resolution source, end date and edge cases govern the
payout, and UMA disputes can lead to challenge periods, a DVM vote or rare 50/50 outcomes. If your bot does not
snapshot the rules text/hash and automatically exclude ambiguous/disputed/clarified markets, it will buy
"mathematical arb" that is not mathematical in the resolution world.

Source: <https://docs.polymarket.com/concepts/resolution>

### An extra practical killer: matching engine restarts

Polymarket documents weekly restart windows, `425 Too Early`, and the need for exponential backoff. All
opportunities during an outage/reconnect must be marked invalid, not as missed alpha.

Source: <https://docs.polymarket.com/trading/matching-engine>

---

## 5. What I would build in 10 days

I would not build an "autonomous trader" first. I would build a **Polymarket internal no-arb measurement
engine**.

### Ranking

### 1. Event-driven CLOB recorder + anomaly duration profiler

WebSocket ingest, local book reconstruction, trades, book hash, local latency, stale detection, fee/tick/min-size
enrichment. Without it you do not know whether your edges last 3 seconds or 30 minutes.

### 2. NegRisk bundle arb scanner

Strict exhaustive events only. Full-depth VWAP. Long-bundle when `sum(Yes asks) < 1 - threshold`;
short/split-side when executable bids imply surplus. No "fade favourite".

### 3. Manual cross-market constraint graph

Small graph, high quality. Encode relations as inequalities and payoff constraints. Start with NBA/tournament
relationships, but only where the rules are identical enough.

### 4. CLV/calibration module

For every candidate decision: mark to the Polymarket close, mark to 1h/6h/24h later, and mark to resolution.
Your first KPI should be CLV, not final PnL.

### 5. Maker/rebate simulator, not live maker

Polymarket has maker rebates funded by taker fees in eligible markets, but paper simulation of maker fills is
dangerous, because queue position and cancellation dynamics are hard. Log "would-post" orders, then check
whether trades actually occurred through your quoted price after your timestamp.

Source: <https://docs.polymarket.com/market-makers/maker-rebates>

---

## Brutal conclusion

A Polymarket-only bot can well be valuable, but not as a "sports edge bot" in the traditional sense. It can be a
**constraint-arb and market-microstructure auditor**.

If you require autonomous, no external probability, no human-in-the-loop, then the only rational signals are
those where Polymarket itself creates a mathematical relation. Everything else is just the Polymarket price
trying to predict the Polymarket price.
