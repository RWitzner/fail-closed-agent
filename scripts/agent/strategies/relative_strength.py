"""M7c phase-1 long-only cross-sectional relative-strength proxy.

Pure: reads only assembled SignalSnapshots and emits Candidate data. Imports no
broker, preflight, orchestrator, journal, or clock surface and performs no I/O.

The proxy ranks the valid decision set cross-sectionally at a single decision
instant and buys the top N strongest names with equal per-leg notional. It builds
no short side and no multi-leg basket (those are phase 2). Contract:
docs/superpowers/specs/2026-06-13-M7c-phase1-proxy-contract.md.
"""
from dataclasses import dataclass
from decimal import (
    Context,
    Decimal,
    InvalidOperation,
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    localcontext,
)
from typing import List, Optional, Sequence, Tuple, Union

from agent.candidate import Candidate, Leg
from agent.market_state import Tradability
from agent.order_pricing import tick_for

STRATEGY_ID = "relative_strength.long_only_proxy_v1"
TOP_N = 2
MIN_VALID_SYMBOLS = 8
PAPER_NOTIONAL_USD = Decimal("1000")
EDGE_BUFFER_BPS = Decimal("5.000000")

# Ordered scored features and their rs_score weights (packet "Feature Set"):
#   rs_score = rank(momentum_21) + 0.50*rank(ema_gap_9_21) + 0.50*rank(sma_gap_21_50)
#              + 0.25*rank(rsi14_centered) - 0.25*rank(realized_vol_21)
SCORE_WEIGHTS = (
    ("momentum_21", Decimal("1.00")),
    ("ema_gap_9_21", Decimal("0.50")),
    ("sma_gap_21_50", Decimal("0.50")),
    ("rsi14_centered", Decimal("0.25")),
    ("realized_vol_21", Decimal("-0.25")),
)
_SCORED_FEATURES = tuple(name for name, _ in SCORE_WEIGHTS)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_TEN_THOUSAND = Decimal("10000")
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class RankedSymbol:
    symbol: str
    instrument_id: int
    rs_score: Decimal
    mid: Decimal


@dataclass(frozen=True)
class Exclusion:
    symbol: str
    reason: str


@dataclass(frozen=True)
class CrossSectionalRanking:
    ranked: Tuple[RankedSymbol, ...]
    excluded: Tuple[Exclusion, ...]
    valid_count: int


@dataclass(frozen=True)
class _ValidSymbol:
    symbol: str
    instrument_id: int
    mid: Decimal
    features: Tuple[Decimal, ...]  # aligned with _SCORED_FEATURES


def _feature_decimal(features, name: str) -> Optional[Decimal]:
    raw = features.get(name) if isinstance(features, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return value


def _classify(snapshot) -> Union[_ValidSymbol, Exclusion]:
    """Return a _ValidSymbol for an eligible decision-set member, else Exclusion."""
    symbol = snapshot.symbol
    feature = snapshot.feature
    if not getattr(feature, "available", False):
        return Exclusion(symbol=symbol, reason="features_unavailable")
    values = []
    for name in _SCORED_FEATURES:
        value = _feature_decimal(feature.features, name)
        if value is None:
            return Exclusion(symbol=symbol, reason="features_unavailable")
        values.append(value)

    verdict = snapshot.quote_verdict
    if not getattr(verdict, "ok", False):
        return Exclusion(symbol=symbol, reason="quote_not_ok")
    mid = verdict.mid
    if not isinstance(mid, Decimal) or not mid.is_finite() or mid <= 0:
        return Exclusion(symbol=symbol, reason="quote_not_ok")
    spread = verdict.spread_bps
    if not isinstance(spread, Decimal) or not spread.is_finite() or spread < 0:
        return Exclusion(symbol=symbol, reason="invalid_spread")

    market_state = snapshot.market_state
    if (getattr(market_state, "tradability", None) != Tradability.TRADABLE
            or not getattr(market_state, "two_sided_nbbo", False)):
        return Exclusion(symbol=symbol, reason="market_state_not_tradable")

    return _ValidSymbol(
        symbol=symbol,
        instrument_id=snapshot.instrument_id,
        mid=mid,
        features=tuple(values),
    )


def _average_ranks(values: Sequence[Decimal]) -> List[Decimal]:
    """Ascending ranks in [0, n-1]; tied values share their average rank.

    Equal feature vectors therefore produce equal rs_scores (a true tie), which the
    final selection breaks by predeclared universe order — never by future returns.
    """
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [_ZERO] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = Decimal(sum(range(i, j + 1))) / Decimal(j - i + 1)
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def rank_decision_set(snapshots: Sequence[object], *,
                      universe_order: Sequence[str]) -> CrossSectionalRanking:
    """Cross-sectionally rank the valid decision set at one decision instant."""
    order_index = {sym: i for i, sym in enumerate(universe_order)}
    valid: List[_ValidSymbol] = []
    excluded: List[Exclusion] = []
    for snapshot in snapshots:
        result = _classify(snapshot)
        if isinstance(result, Exclusion):
            excluded.append(result)
        else:
            valid.append(result)

    # Scores are keyed POSITIONALLY (not by symbol name) so a duplicated symbol in
    # the decision set cannot collapse two rows into one corrupted score.
    scores = [_ZERO] * len(valid)
    for feature_index, (_name, weight) in enumerate(SCORE_WEIGHTS):
        column = [symbol.features[feature_index] for symbol in valid]
        ranks = _average_ranks(column)
        for position, rank in enumerate(ranks):
            scores[position] = scores[position] + weight * rank

    # Fully deterministic order: rs_score desc, then predeclared universe order,
    # then symbol string as a final stable key (deterministic even for symbols
    # absent from universe_order, which all share the same fallback index).
    order = sorted(
        range(len(valid)),
        key=lambda pos: (
            -scores[pos],
            order_index.get(valid[pos].symbol, len(order_index)),
            valid[pos].symbol,
        ),
    )
    ranked = tuple(
        RankedSymbol(
            symbol=valid[pos].symbol,
            instrument_id=valid[pos].instrument_id,
            rs_score=scores[pos],
            mid=valid[pos].mid,
        )
        for pos in order
    )
    return CrossSectionalRanking(
        ranked=ranked, excluded=tuple(excluded), valid_count=len(valid))


def _whole_share_qty(mid: Decimal) -> Decimal:
    with localcontext(_DECIMAL_CTX):
        return (PAPER_NOTIONAL_USD / mid).to_integral_value(rounding=ROUND_DOWN)


def _buy_limit(mid: Decimal, edge_bps: Decimal) -> Decimal:
    with localcontext(_DECIMAL_CTX):
        raw = mid * (_ONE + edge_bps / _TEN_THOUSAND)
        return raw.quantize(tick_for(raw), rounding=ROUND_DOWN)


class RelativeStrengthLongOnlyProxyV1:
    """Phase-1 gating probe: long the top-N cross-sectional names, no short side."""

    strategy_id = STRATEGY_ID
    synthetic = False

    def decide(self, *, snapshots: Sequence[object],
               universe_order: Sequence[str], now_ms: int) -> Sequence[Candidate]:
        ranking = rank_decision_set(snapshots, universe_order=universe_order)
        if ranking.valid_count < MIN_VALID_SYMBOLS:
            return ()
        candidates = []
        for ranked in ranking.ranked[:TOP_N]:
            qty = _whole_share_qty(ranked.mid)
            if qty < 1:
                continue
            candidates.append(Candidate(
                strategy_id=STRATEGY_ID,
                legs=(Leg(
                    symbol=ranked.symbol,
                    instrument_id=ranked.instrument_id,
                    side="buy",
                    qty=qty,
                    limit_price=_buy_limit(ranked.mid, EDGE_BUFFER_BPS),
                ),),
                paper_eligible=True,
                score=ranked.rs_score,
            ))
        return tuple(candidates)


def strategy_for_id(strategy_id: str) -> RelativeStrengthLongOnlyProxyV1:
    if strategy_id == STRATEGY_ID:
        return RelativeStrengthLongOnlyProxyV1()
    raise ValueError(f"unknown strategy_id: {strategy_id!r}")
