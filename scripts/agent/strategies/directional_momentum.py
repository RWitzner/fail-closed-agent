"""M7 directional momentum strategy.

The strategy is intentionally pure: it reads only the assembled SignalSnapshot in
ScanContext and emits Candidate data. It imports no broker, preflight, journal, or
clock surface.
"""
from decimal import Context, Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_EVEN
from decimal import localcontext
from typing import Optional, Sequence

from agent.candidate import Candidate, Leg
from agent.order_pricing import tick_for
from agent.strategy import ScanContext

STRATEGY_ID_V1 = "directional.momentum_v1"
STRATEGY_ID_V2 = "directional.momentum_v2"
STRATEGY_ID = STRATEGY_ID_V1
PAPER_NOTIONAL_USD = Decimal("1000")
EDGE_BUFFER_BPS = Decimal("2.000000")
V2_EDGE_BUFFER_BPS = Decimal("5.000000")
V2_MIN_Z_RET_21 = Decimal("0.15000000")
V2_MAX_Z_RET_21 = Decimal("2.50000000")
V2_MAX_SPREAD_BPS = Decimal("5.000000")
BPS_QUANTUM = Decimal("0.000001")
_ONE = Decimal("1")
_TEN_THOUSAND = Decimal("10000")
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


def _feature_decimal(ctx: ScanContext, name: str) -> Optional[Decimal]:
    raw = ctx.snapshot.feature.features.get(name)
    if not isinstance(raw, str):
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return value


def _quote_mid(ctx: ScanContext) -> Optional[Decimal]:
    if not ctx.snapshot.quote_verdict.ok:
        return None
    mid = ctx.snapshot.quote_verdict.mid
    if not isinstance(mid, Decimal) or not mid.is_finite() or mid <= 0:
        return None
    return mid


def _quote_spread_bps(ctx: ScanContext) -> Optional[Decimal]:
    spread = ctx.snapshot.quote_verdict.spread_bps
    if not isinstance(spread, Decimal) or not spread.is_finite() or spread < 0:
        return None
    return spread


def _whole_share_qty(mid: Decimal) -> Decimal:
    with localcontext(_DECIMAL_CTX):
        return (PAPER_NOTIONAL_USD / mid).to_integral_value(rounding=ROUND_DOWN)


def _buy_limit(mid: Decimal, edge_bps: Decimal) -> Decimal:
    with localcontext(_DECIMAL_CTX):
        raw = mid * (_ONE + edge_bps / _TEN_THOUSAND)
        return raw.quantize(tick_for(raw), rounding=ROUND_DOWN)


class MomentumV1Strategy:
    """First non-synthetic directional strategy, eligible only for paper gates."""

    strategy_id = STRATEGY_ID
    synthetic = False

    def scan(self, ctx: ScanContext) -> Sequence[Candidate]:
        feature = ctx.snapshot.feature
        if not feature.available:
            return ()

        momentum_9 = _feature_decimal(ctx, "momentum_9")
        momentum_21 = _feature_decimal(ctx, "momentum_21")
        z_ret_21 = _feature_decimal(ctx, "z_ret_21")
        realized_vol_21 = _feature_decimal(ctx, "realized_vol_21")
        if any(value is None for value in (
                momentum_9, momentum_21, z_ret_21, realized_vol_21)):
            return ()
        if (momentum_9 <= 0 or momentum_21 <= 0
                or z_ret_21 < 0 or realized_vol_21 <= 0):
            return ()

        mid = _quote_mid(ctx)
        if mid is None:
            return ()

        with localcontext(_DECIMAL_CTX):
            raw_edge_bps = min(momentum_9, momentum_21) * _TEN_THOUSAND
            edge_bps = (raw_edge_bps - EDGE_BUFFER_BPS).quantize(
                BPS_QUANTUM, rounding=ROUND_HALF_EVEN)
        if edge_bps <= 0:
            return ()

        qty = _whole_share_qty(mid)
        if qty < 1:
            return ()

        return (Candidate(
            strategy_id=self.strategy_id,
            legs=(Leg(
                symbol=ctx.snapshot.symbol,
                instrument_id=ctx.snapshot.instrument_id,
                side="buy",
                qty=qty,
                limit_price=_buy_limit(mid, edge_bps),
            ),),
            paper_eligible=True,
            score=edge_bps,
        ),)


class MomentumV2Strategy:
    """Stricter M7b directional strategy for reviewed historical replay."""

    strategy_id = STRATEGY_ID_V2
    synthetic = False

    def scan(self, ctx: ScanContext) -> Sequence[Candidate]:
        feature = ctx.snapshot.feature
        if not feature.available:
            return ()

        momentum_9 = _feature_decimal(ctx, "momentum_9")
        momentum_21 = _feature_decimal(ctx, "momentum_21")
        ema_gap_9_21 = _feature_decimal(ctx, "ema_gap_9_21")
        sma_gap_21_50 = _feature_decimal(ctx, "sma_gap_21_50")
        z_ret_21 = _feature_decimal(ctx, "z_ret_21")
        realized_vol_21 = _feature_decimal(ctx, "realized_vol_21")
        aligned = (momentum_9, momentum_21, ema_gap_9_21, sma_gap_21_50)
        if any(value is None for value in (
                *aligned, z_ret_21, realized_vol_21)):
            return ()
        if any(value <= 0 for value in aligned):
            return ()
        if (z_ret_21 < V2_MIN_Z_RET_21
                or z_ret_21 > V2_MAX_Z_RET_21
                or realized_vol_21 <= 0):
            return ()

        spread_bps = _quote_spread_bps(ctx)
        if spread_bps is None or spread_bps > V2_MAX_SPREAD_BPS:
            return ()
        mid = _quote_mid(ctx)
        if mid is None:
            return ()

        with localcontext(_DECIMAL_CTX):
            raw_edge_bps = min(aligned) * _TEN_THOUSAND
            edge_bps = (raw_edge_bps - V2_EDGE_BUFFER_BPS).quantize(
                BPS_QUANTUM, rounding=ROUND_HALF_EVEN)
        if edge_bps <= 0:
            return ()

        qty = _whole_share_qty(mid)
        if qty < 1:
            return ()

        return (Candidate(
            strategy_id=self.strategy_id,
            legs=(Leg(
                symbol=ctx.snapshot.symbol,
                instrument_id=ctx.snapshot.instrument_id,
                side="buy",
                qty=qty,
                limit_price=_buy_limit(mid, edge_bps),
            ),),
            paper_eligible=True,
            score=edge_bps,
        ),)


def strategy_for_id(strategy_id: str):
    if strategy_id == STRATEGY_ID_V1:
        return MomentumV1Strategy()
    if strategy_id == STRATEGY_ID_V2:
        return MomentumV2Strategy()
    raise ValueError(f"unknown strategy_id: {strategy_id!r}")
