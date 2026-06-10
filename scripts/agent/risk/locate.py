"""M4 §H — short-locate seam (FD-M4-1). M4 ships ONLY the Protocol + the deny-all
implementation. can_open does NOT call this in M4 (shorts are rejected structurally at
ladder stage 6); the module exists so the short-side milestone is an additive contract
change, not a new wire."""
from decimal import Decimal
from typing import Protocol, Tuple, runtime_checkable


@runtime_checkable
class LocateCheck(Protocol):
    def locate(self, symbol: str, qty: Decimal) -> Tuple[bool, str]: ...


class DenyAllLocate:
    """The only M4 implementation: every locate fails closed."""

    def locate(self, symbol: str, qty: Decimal) -> Tuple[bool, str]:
        return (False, "short_side_disabled")
