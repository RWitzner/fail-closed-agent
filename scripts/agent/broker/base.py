"""Broker abstraction (spec §5 Tier 6). `submit_order` requires a preflight token.

`OrderIntent` validates its own money fields (finite positive Decimal qty, finite
Decimal/None limit_price) so a float can never reach the broker. `require_token`
is the chokepoint: it looks up the token's **immutable, registry-stored**
authorization and validates the intent against it (a reduce-only authorization
binds side+qty), then consumes it. `BrokerBase` makes the chokepoint
non-bypassable: a concrete broker implements `_place`, never `submit_order`.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Protocol, runtime_checkable

from agent.execution_preflight import PreflightForgery, authorization_of, consume


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str  # "buy" | "sell"
    qty: Decimal
    order_type: str = "marketable_limit"
    tif: str = "day"
    limit_price: Optional[Decimal] = None
    is_reducing: bool = False  # True => position-decreasing (reduce-only / close)
    intent_id: str = ""

    def __post_init__(self):
        if not isinstance(self.qty, Decimal) or not self.qty.is_finite() or self.qty <= 0:
            raise ValueError("OrderIntent.qty must be a positive finite Decimal")
        if self.limit_price is not None and (
            not isinstance(self.limit_price, Decimal) or not self.limit_price.is_finite()
        ):
            raise ValueError("OrderIntent.limit_price must be a finite Decimal or None")


def require_token(intent: OrderIntent, token) -> None:
    """Validate the intent against the token's stored authorization, then consume it.

    Validation precedes consumption, so a failed check leaves the token usable for
    its correct intent.
    """
    auth = authorization_of(token)
    if auth is None:
        raise PreflightForgery("submit_order requires a valid, unconsumed preflight token")
    if intent.is_reducing:
        if auth.kind != "reduce_only":
            raise PreflightForgery("reduce-only intent requires a reduce-only authorization")
        if auth.symbol != intent.symbol or auth.side != intent.side or auth.qty != intent.qty:
            raise PreflightForgery("intent does not match the stored reduce-only authorization")
    else:
        if auth.kind != "open":
            raise PreflightForgery("opening intent requires an open authorization")
        if auth.symbol != intent.symbol:
            raise PreflightForgery("token/intent symbol mismatch")
    consume(token)


@runtime_checkable
class Broker(Protocol):
    """Position-of-record. `submit_order` must enforce `require_token` (use `BrokerBase`)."""

    def submit_order(self, intent: OrderIntent, token) -> object: ...

    def positions(self) -> object: ...

    def account(self) -> object: ...


class BrokerBase:
    """Non-bypassable gateway: `submit_order` enforces the preflight chokepoint, then
    delegates the placement to `_place`. Concrete brokers implement `_place` and must
    NOT override `submit_order`."""

    def submit_order(self, intent: OrderIntent, token):
        require_token(intent, token)
        return self._place(intent)

    def _place(self, intent: OrderIntent):
        raise NotImplementedError

    def positions(self):
        raise NotImplementedError

    def account(self):
        raise NotImplementedError
