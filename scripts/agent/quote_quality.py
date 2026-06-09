"""M3 §A — quote-quality filters in bps. PURE function of a QuoteSnapshot + injected
monotonic now_ms. Warnings-as-data: returns a verdict, never raises on bad market
data (raises only on programming errors such as float inputs).

Frozen semantics (contract §A, rev2):
- ALL applicable reasons are collected, then sorted (funnel attribution stays honest).
- `mid`/`spread_bps` are computed iff both PRICE sides are present, positive, finite —
  a size-derived `quote_one_sided`/`quote_nonpositive` does NOT suppress them
  (rev2 BUILD-F3) so rejected quotes stay inspectable.
- `spread_too_wide` compares the QUANTIZED `spread_bps` field (the persisted value),
  strict `>` (rev2 MATH-Q8).
- Staleness is strict `>` on injected monotonic ms (mirrors market_state_cache.py:74-99).
- No wall clock, no I/O, no imports beyond stdlib.
"""
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Optional, Tuple

BPS_QUANTUM = Decimal("0.01")        # spread_bps reported at 2dp
MID_QUANTUM = Decimal("0.000001")    # FD-5; exact for (bid+ask)/2 of 4dp prices

# Pinned context for persisted-value arithmetic (harden round 1, M3-R4).
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class QuoteSnapshot:
    """One NBBO observation with full provenance (event_row.py:34-55 field names)."""

    symbol: str
    instrument_id: int
    bid: Optional[Decimal]           # None == side missing
    ask: Optional[Decimal]
    bid_sz: Optional[Decimal]
    ask_sz: Optional[Decimal]
    ts_event_utc: str
    ts_recv_utc: str
    seen_at_ms: int                  # injected monotonic receipt stamp
    reconnect_epoch: int
    vendor_seq: Optional[int]
    dataset: str
    schema: str


@dataclass(frozen=True)
class QuoteVerdict:
    ok: bool
    reasons: Tuple[str, ...]         # sorted; subset of the frozen vocabulary (§1)
    mid: Optional[Decimal]           # quantized MID_QUANTUM; None unless both prices positive & finite
    spread_bps: Optional[Decimal]    # (ask-bid)/mid * 10_000, quantized BPS_QUANTUM; None if mid is None
    age_ms: int                      # now_ms - seen_at_ms


def _reject_float(value, *, field: str):
    """A float in a price/size/budget slot is a PROGRAMMING error (S2 posture,
    mirrors serializer float-reject)."""
    if isinstance(value, float):
        raise ValueError(f"{field}: float not allowed; use Decimal")


def evaluate(q: QuoteSnapshot, *, now_ms: int,
             spread_bps_max: Decimal, staleness_ms_max: int) -> QuoteVerdict:
    for field in ("bid", "ask", "bid_sz", "ask_sz"):
        _reject_float(getattr(q, field), field=field)
    _reject_float(spread_bps_max, field="spread_bps_max")

    sides = (q.bid, q.ask, q.bid_sz, q.ask_sz)
    reasons = set()

    # one-sided: ANY of the four legs missing.
    if any(v is None for v in sides):
        reasons.add("quote_one_sided")

    # non-finite: any present leg that is not finite.
    nonfinite = any(v is not None and not v.is_finite() for v in sides)
    if nonfinite:
        reasons.add("quote_nonfinite")

    # non-positive: any present, finite leg <= 0.
    if any(v is not None and v.is_finite() and v <= 0 for v in sides):
        reasons.add("quote_nonpositive")

    # crossed / locked need both prices present + finite.
    prices_usable = (
        q.bid is not None and q.ask is not None
        and q.bid.is_finite() and q.ask.is_finite()
    )
    if prices_usable:
        if q.bid > q.ask:
            reasons.add("quote_crossed")
        elif q.bid == q.ask:
            reasons.add("quote_locked")

    age_ms = now_ms - q.seen_at_ms
    if age_ms > staleness_ms_max:  # strict '>': fresh at exactly the budget
        reasons.add("quote_stale")

    # mid/spread: computed iff both PRICE sides present, positive, finite (rev2 BUILD-F3).
    mid: Optional[Decimal] = None
    spread_bps: Optional[Decimal] = None
    if prices_usable and q.bid > 0 and q.ask > 0:
        with localcontext(_DECIMAL_CTX):
            mid = ((q.bid + q.ask) / 2).quantize(MID_QUANTUM, ROUND_HALF_EVEN)
            spread_bps = ((q.ask - q.bid) / mid * Decimal("10000")).quantize(
                BPS_QUANTUM, ROUND_HALF_EVEN
            )
        if spread_bps > spread_bps_max:  # quantized-field compare (rev2 MATH-Q8)
            reasons.add("spread_too_wide")

    return QuoteVerdict(
        ok=not reasons,
        reasons=tuple(sorted(reasons)),
        mid=mid,
        spread_bps=spread_bps,
        age_ms=age_ms,
    )
