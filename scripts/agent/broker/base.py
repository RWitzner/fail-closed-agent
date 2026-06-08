"""Broker abstraction (spec §5 Tier 6). `submit_order` requires a preflight token.

The broker is the position-of-record. `OrderIntent` is the explicit order intent;
`require_token` is the structural chokepoint every concrete `submit_order` calls
first — it validates the token is authentic, of the right kind for the intent
(opening vs reduce-only), matches the symbol, and consumes it (single-use).
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Protocol, runtime_checkable

from agent.execution_preflight import (
    OpenPreflightToken,
    PreflightForgery,
    ReduceOnlyPreflightToken,
    consume,
    is_authentic,
)


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


def require_token(intent: OrderIntent, token) -> None:
    """Validate + consume the preflight token for this intent, or raise PreflightForgery."""
    if not is_authentic(token):
        raise PreflightForgery("submit_order requires a valid, unconsumed preflight token")
    expected = ReduceOnlyPreflightToken if intent.is_reducing else OpenPreflightToken
    if not isinstance(token, expected):
        raise PreflightForgery(
            f"intent requires a {expected.__name__}, got {type(token).__name__}"
        )
    if token.symbol != intent.symbol:
        raise PreflightForgery("token/intent symbol mismatch")
    consume(token)


@runtime_checkable
class Broker(Protocol):
    """Position-of-record. `submit_order` must require a preflight token (use `require_token`)."""

    def submit_order(self, intent: OrderIntent, token) -> object: ...

    def positions(self) -> object: ...

    def account(self) -> object: ...
