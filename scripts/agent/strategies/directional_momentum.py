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

STRATEGY_ID = "directional.momentum_v1"
PAPER_NOTIONAL_USD = Decimal("1000")
EDGE_BUFFER_BPS = Decimal("2.000000")
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
