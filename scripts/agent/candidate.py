"""M3 §E — `Candidate`/`Leg`: PURE types for M5+ (contract FD-12).

These carry NO order authority — orders remain preflight-token-gated (M5). NO M3
module instantiates `Candidate` (the probe's S1 test asserts emitted types).
Validation mirrors `OrderIntent` (broker/base.py): qty positive finite Decimal,
limit finite or None, side in a closed vocabulary, legs non-empty.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple

SIDES = frozenset({"buy", "sell"})


def _require_finite_decimal(value, *, field: str, allow_none: bool = False):
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{field}: required Decimal, got None")
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, Decimal):
        raise ValueError(f"{field}: must be a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{field}: non-finite Decimal not allowed: {value!r}")
    return value


@dataclass(frozen=True)
class Leg:
    symbol: str
    instrument_id: int
    side: str                        # ∈ {"buy","sell"}
    qty: Decimal                     # finite, > 0
    limit_price: Optional[Decimal]   # finite if present

    def __post_init__(self):
        if self.side not in SIDES:
            raise ValueError(f"side must be one of {sorted(SIDES)}, got {self.side!r}")
        qty = _require_finite_decimal(self.qty, field="qty")
        if qty <= 0:
            raise ValueError(f"qty must be > 0, got {qty}")
        _require_finite_decimal(self.limit_price, field="limit_price", allow_none=True)


@dataclass(frozen=True)
class Candidate:
    strategy_id: str
    legs: Tuple["Leg", ...]          # len >= 1; M3 single-leg N=1 is a convention, not a cap
    paper_eligible: bool
    score: Optional[Decimal]

    def __post_init__(self):
        if not self.legs:
            raise ValueError("Candidate requires at least one leg")
        for leg in self.legs:
            if not isinstance(leg, Leg):
                raise ValueError(f"legs must be Leg instances, got {type(leg).__name__}")
        _require_finite_decimal(self.score, field="score", allow_none=True)
