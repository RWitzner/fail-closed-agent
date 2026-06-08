"""Pre-submit execution preflight: registry-authorized, single-use tokens (spec §5 Tier 6).

A token is an opaque capability handle. Its authority does NOT live on the token
object (which carries only an opaque nonce and cannot be constructed, copied, or
mutated into authority) — it lives in a module-private registry of **immutable**
authorizations, keyed by the token's nonce and created ONLY by the mint functions.

- `mint_open_token` — reject-all in M0 (full open preflight lands in M5). It never
  issues an authorization, so even a directly-constructed `OpenPreflightToken`
  (built by importing the private mint key) has no authorization and cannot open.
- `mint_reduce_only_token` — issues an authorization only for a genuinely
  position-decreasing order, validated against the held position's sign, size, and
  symbol (never the caller's self-asserted flag). The stored side+qty are
  re-checked at `require_token`, so mutating the token cannot rebind it.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

_MINT = object()  # module-private mint key
_authorizations = {}  # nonce(object) -> _Authorization (immutable); only mint_* writes here


@dataclass(frozen=True)
class _Authorization:
    kind: str  # "open" | "reduce_only"
    symbol: str
    side: Optional[str] = None
    qty: Optional[Decimal] = None


class PreflightRejected(Exception):
    """A token was requested but the preflight gates refuse to issue an authorization."""


class PreflightForgery(Exception):
    """A token was constructed/copied directly, reused, or has no/wrong authorization."""


class PreflightToken:
    __slots__ = ("_nonce",)

    def __init__(self, *, mint):
        if mint is not _MINT:
            raise PreflightForgery("preflight tokens cannot be constructed directly")
        self._nonce = object()

    def __copy__(self):
        raise PreflightForgery("preflight tokens are single-use; copying is forbidden")

    def __deepcopy__(self, memo):
        raise PreflightForgery("preflight tokens are single-use; copying is forbidden")

    def __reduce__(self):
        raise PreflightForgery("preflight tokens cannot be pickled")


class OpenPreflightToken(PreflightToken):
    """Handle for a single opening / position-increasing order."""


class ReduceOnlyPreflightToken(PreflightToken):
    """Handle for a single position-decreasing order."""


def _issue(token_cls, authorization: _Authorization):
    token = token_cls(mint=_MINT)
    _authorizations[token._nonce] = authorization
    return token


def authorization_of(token) -> Optional[_Authorization]:
    if not isinstance(token, PreflightToken):
        return None
    return _authorizations.get(getattr(token, "_nonce", None))


def is_authentic(token) -> bool:
    return authorization_of(token) is not None


def consume(token) -> _Authorization:
    auth = authorization_of(token)
    if auth is None:
        raise PreflightForgery("token is forged, reused, or invalid")
    del _authorizations[token._nonce]
    return auth


def mint_open_token(config, intent) -> OpenPreflightToken:
    """M0: reject-all. Never issues an authorization."""
    raise PreflightRejected(
        "open preflight is not implemented until M5; the committed config opens nothing"
    )


def mint_reduce_only_token(position, intent) -> ReduceOnlyPreflightToken:
    """Issue an authorization only for a genuinely position-decreasing order."""
    raw_qty = getattr(position, "qty", None) if position is not None else None
    if raw_qty is None:
        raise PreflightRejected("reduce-only requires an existing held position")
    held = Decimal(raw_qty)
    if held == 0:
        raise PreflightRejected("reduce-only requires a non-zero held position")
    if getattr(intent, "is_reducing", False) is not True:
        raise PreflightRejected("reduce-only requires an order flagged is_reducing")
    if intent.symbol != getattr(position, "symbol", None):
        raise PreflightRejected("reduce-only order symbol must match the held position")
    required_side = "sell" if held > 0 else "buy"  # long reduces by selling, short by buying
    if intent.side != required_side:
        raise PreflightRejected(f"reduce-only for this position requires side={required_side!r}")
    order_qty = Decimal(intent.qty)
    if order_qty <= 0 or order_qty > abs(held):
        raise PreflightRejected("reduce-only qty must be >0 and <= held size (may flatten, never flip)")
    return _issue(
        ReduceOnlyPreflightToken,
        _Authorization(kind="reduce_only", symbol=position.symbol, side=required_side, qty=order_qty),
    )
