"""M5 §C — Reg-NMS Rule 612 / Alpaca A2 tick grid + the frozen marketable-limit cap.

PURE Decimal math under quote_quality's pinned context discipline
(`Context(prec=28, ROUND_HALF_EVEN)`, quote_quality.py:23 precedent); floats raise (S2).
This module prices and labels; it never submits (§3 import discipline: stdlib +
agent.quote_quality types + exec_reasons only).

Frozen semantics (contract §C, rev 2):
- Tick grid: price >= $1.00 -> $0.01; 0 < price < $1.00 -> $0.0001 (the Alpaca
  42210000 sub-penny mirror). The cap crossing the $1.00 boundary uses
  `tick_for(raw)` of the RAW value.
- BUY open cap: `raw = ask_B * (1 + slippage_cap_bps/10000)`, quantized ROUND_DOWN —
  directed rounding TOWARD the budget, never past it; `capped_limit = min(cap,
  strategy_limit)`; marketable iff `capped_limit >= ask_B` (boundary-equal IS
  marketable). SELL mirrors: ROUND_UP, `max(cap, strategy_limit)`, `<= bid_B`.
- `adverse_move_bps`: buy = `(ask_B - ask_A)/ask_A * 10000` quantized BPS_QUANTUM
  ROUND_HALF_EVEN; sell mirrors on bids sign-flipped so adverse is positive.
  `latency_lost_edge` iff `adverse_move_bps > slippage_cap_bps` — STRICT, on the
  QUANTIZED value: THE single authoritative form (EX-1; the journaled
  `CapResult.adverse_move_bps` and the reject reason can never disagree).
- `unpriceable_candidate` iff `strategy_limit <= 0` (FD-M4-16 mirror) or the needed
  quote-B side is missing/non-finite/<=0 (belt-and-braces; stage 8 owns quotes).
  `invalid_tick` iff `strategy_limit` is not on its own grid.

Resolutions documented (not contradicted by §C):
- An unusable quote-A touch only makes the adverse move unmeasurable
  (`adverse_move_bps = 0.00`, no latency fire) — §C's "iff" closes the
  `unpriceable_candidate` condition over strategy_limit and quote-B only.
- When `unpriceable_candidate`/`invalid_tick` fires, `capped_limit` is None: a
  submittable on-grid limit is not derivable for that candidate (`CapResult`
  freezes capped_limit as ON-grid), and `not_marketable` is only emitted for a
  COMPUTED cap that fails the cross test.
- A non-finite `strategy_limit` raises ExecError: Candidate/OrderIntent guarantee
  finite-or-None, so NaN/Inf here is malformed collaborator input, not data.
"""
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, localcontext
from typing import Optional, Tuple

from agent.exec_reasons import ExecError
from agent.quote_quality import BPS_QUANTUM, QuoteSnapshot

# Pinned context for derived values (quote_quality.py:23 precedent; §S row "cap math").
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)

_ONE_DOLLAR = Decimal("1.00")
_TICK_DOLLAR = Decimal("0.01")
_TICK_SUB_DOLLAR = Decimal("0.0001")
_ONE = Decimal("1")
_TEN_THOUSAND = Decimal("10000")
_ZERO_BPS = Decimal("0.00")          # BPS_QUANTUM-quantized zero
_SIDES = ("buy", "sell")
_QUOTE_FIELDS = ("bid", "ask", "bid_sz", "ask_sz")


def _reject_float(value, *, field: str) -> None:
    """A float in a price/size/budget slot is a PROGRAMMING error (S2 posture,
    mirrors quote_quality/serializer float-reject)."""
    if isinstance(value, float):
        raise ExecError(f"{field}: float not allowed; use Decimal (S2)")


def _reject_quote_floats(quote: QuoteSnapshot, *, name: str) -> None:
    for field in _QUOTE_FIELDS:
        _reject_float(getattr(quote, field), field=f"{name}.{field}")


def _require_side(side: str) -> None:
    if side not in _SIDES:
        raise ExecError(f"side must be one of {_SIDES}, got {side!r}")


def _usable(price: Optional[Decimal]) -> bool:
    """A quote touch is usable iff present, Decimal, finite, > 0."""
    return isinstance(price, Decimal) and price.is_finite() and price > 0


def tick_for(price: Decimal) -> Decimal:
    """Reg-NMS Rule 612 / Alpaca A2 grid: >= $1.00 -> $0.01; 0 < p < $1.00 -> $0.0001.

    price <= 0 / non-finite / non-Decimal -> ExecError (callers pre-validate;
    a bug, not data)."""
    _reject_float(price, field="price")
    if not isinstance(price, Decimal):
        raise ExecError(f"tick_for: price must be Decimal, got {type(price).__name__}")
    if not price.is_finite() or price <= 0:
        raise ExecError(f"tick_for: price must be positive and finite, got {price}")
    return _TICK_DOLLAR if price >= _ONE_DOLLAR else _TICK_SUB_DOLLAR


def on_tick_grid(price: Decimal) -> bool:
    """Exact Decimal grid membership: price % tick_for(price) == 0."""
    tick = tick_for(price)
    with localcontext(_DECIMAL_CTX):
        return (price % tick) == 0


@dataclass(frozen=True)
class CapResult:
    capped_limit: Optional[Decimal]   # ON the tick grid; None iff not derivable
    marketable: bool
    adverse_move_bps: Decimal         # signed, quote-A touch -> quote-B touch, BPS_QUANTUM
    reasons: Tuple[str, ...]          # sorted; subset of {unpriceable_candidate,
                                      #   invalid_tick, not_marketable, latency_lost_edge}


def marketable_limit_cap(*, side: str, quote_a: QuoteSnapshot, quote_b: QuoteSnapshot,
                         slippage_cap_bps: Decimal,
                         strategy_limit: Optional[Decimal]) -> CapResult:
    """The frozen open-path cap formula (§C). Stage-10 consumer; PURE."""
    _require_side(side)
    _reject_quote_floats(quote_a, name="quote_a")
    _reject_quote_floats(quote_b, name="quote_b")
    _reject_float(slippage_cap_bps, field="slippage_cap_bps")
    _reject_float(strategy_limit, field="strategy_limit")
    if not isinstance(slippage_cap_bps, Decimal) or not slippage_cap_bps.is_finite():
        raise ExecError("slippage_cap_bps must be a finite Decimal")

    buy = side == "buy"
    touch_a = quote_a.ask if buy else quote_a.bid
    touch_b = quote_b.ask if buy else quote_b.bid

    reasons = set()

    # Strategy-limit validation (FD-M4-16 mirror + Reg-NMS grid).
    limit_valid = True
    if strategy_limit is not None:
        if not isinstance(strategy_limit, Decimal) or not strategy_limit.is_finite():
            raise ExecError("strategy_limit must be a finite Decimal or None")
        if strategy_limit <= 0:
            reasons.add("unpriceable_candidate")
            limit_valid = False
        elif not on_tick_grid(strategy_limit):
            reasons.add("invalid_tick")
            limit_valid = False

    # Quote-B needed side (belt-and-braces; normally stage 8 owns it).
    b_usable = _usable(touch_b)
    if not b_usable:
        reasons.add("unpriceable_candidate")

    # Adverse move A->B on the side's touch; quantized value is THE authoritative
    # latency_lost_edge comparison form (EX-1). Unmeasurable -> 0.00, never fires.
    adverse_move_bps = _ZERO_BPS
    if b_usable and _usable(touch_a):
        with localcontext(_DECIMAL_CTX):
            if buy:
                raw_bps = (touch_b - touch_a) / touch_a * _TEN_THOUSAND
            else:  # mirror on bids, sign flipped so adverse (bid fell) is positive
                raw_bps = (touch_a - touch_b) / touch_a * _TEN_THOUSAND
            adverse_move_bps = raw_bps.quantize(BPS_QUANTUM, ROUND_HALF_EVEN)
        if adverse_move_bps > slippage_cap_bps:  # STRICT, on the QUANTIZED value
            reasons.add("latency_lost_edge")

    # Cap derivation + marketability (only when an on-grid limit is derivable).
    capped_limit: Optional[Decimal] = None
    marketable = False
    if b_usable and limit_valid:
        with localcontext(_DECIMAL_CTX):
            if buy:
                raw = touch_b * (_ONE + slippage_cap_bps / _TEN_THOUSAND)
            else:
                raw = touch_b * (_ONE - slippage_cap_bps / _TEN_THOUSAND)
            # tick_for(raw) of the RAW value decides the grid at the $1.00 boundary
            # (frozen); directed rounding TOWARD the budget, never past it.
            cap = raw.quantize(tick_for(raw), ROUND_DOWN if buy else ROUND_UP)
        if strategy_limit is None:
            capped_limit = cap
        else:
            capped_limit = min(cap, strategy_limit) if buy else max(cap, strategy_limit)
        # Boundary-equal IS marketable (frozen).
        marketable = (capped_limit >= touch_b) if buy else (capped_limit <= touch_b)
        if not marketable:
            reasons.add("not_marketable")  # M5 never rests orders

    return CapResult(
        capped_limit=capped_limit,
        marketable=marketable,
        adverse_move_bps=adverse_move_bps,
        reasons=tuple(sorted(reasons)),
    )


def reduce_cap(*, side: str, quote: QuoteSnapshot, cap_bps: Decimal) -> Optional[Decimal]:
    """Close/flatten pricing helper (FD-M5-1/26): sell -> bid*(1 - bps/1e4) quantized
    ROUND_DOWN to grid; buy-to-cover -> ask*(1 + bps/1e4) quantized ROUND_UP; the
    needed side missing/non-finite/<=0 -> None (caller maps to no_price_for_cap —
    a deferral retried next tick, never a gate).

    FROZEN RATIONALE (EX-7): reduce-path caps round AWAY from the touch (up to one
    tick PAST the bps budget) so quantization can never under-price — and thus never
    impede — a reduce. This is the DELIBERATE INVERSE of marketable_limit_cap's
    budget-respect rounding (open path); do NOT "harmonize" them — tightening a
    flatten cap is the dangerous direction. Test-pinned: sell, bid=1.02, bps=25
    => 1.01 (§R 2)."""
    _require_side(side)
    _reject_quote_floats(quote, name="quote")
    _reject_float(cap_bps, field="cap_bps")
    if not isinstance(cap_bps, Decimal) or not cap_bps.is_finite():
        raise ExecError("cap_bps must be a finite Decimal")

    sell = side == "sell"
    touch = quote.bid if sell else quote.ask
    if not _usable(touch):
        return None
    with localcontext(_DECIMAL_CTX):
        if sell:
            raw = touch * (_ONE - cap_bps / _TEN_THOUSAND)
        else:
            raw = touch * (_ONE + cap_bps / _TEN_THOUSAND)
        if not raw.is_finite() or raw <= 0:
            return None  # cannot price (e.g. bps >= 10000); isolated failure, retried
        capped = raw.quantize(tick_for(raw), ROUND_DOWN if sell else ROUND_UP)
    if capped <= 0:
        return None  # grid bottom edge: a zero limit is unsubmittable; defer, never gate
    return capped
