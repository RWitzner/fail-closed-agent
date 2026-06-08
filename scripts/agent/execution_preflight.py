"""Pre-submit execution preflight: non-forgeable, single-use tokens (spec §5 Tier 6).

`Broker.submit_order()` is reachable only with a token minted here. Tokens are
split by intent:

- `OpenPreflightToken` — opening / position-increasing orders. **Reject-all in M0**
  (the full preflight — epoch/session/halt/freshness/tick/can_open + run gates —
  lands in M5). Disabled means reject-all, never `ok=True`.
- `ReduceOnlyPreflightToken` — minted only for an **existing held position + a
  genuinely position-decreasing order** (validated against the position's sign,
  size, and symbol — never the caller's self-asserted flag), so flatten can always
  reduce exposure but can never flip a position or open a new one.

Single-use is enforced by a mint-side nonce registry (so a shallow copy or a
hand-forged token is not authentic), and tokens refuse to be copied/pickled.
"""
from decimal import Decimal

_MINT = object()  # module-private; callers cannot obtain it, so tokens cannot be constructed directly
_live_nonces = set()  # nonces of issued, un-consumed tokens — the single-use registry


class PreflightRejected(Exception):
    """A token was requested but the preflight gates refuse to mint it."""


class PreflightForgery(Exception):
    """A token was constructed/copied directly, reused, tampered with, or is the wrong kind."""


class PreflightToken:
    __slots__ = ("symbol", "intent_id", "_nonce")

    def __init__(self, *, key, symbol, intent_id):
        if key is not _MINT:
            raise PreflightForgery("preflight tokens cannot be constructed directly")
        self.symbol = symbol
        self.intent_id = intent_id
        nonce = object()
        self._nonce = nonce
        _live_nonces.add(nonce)

    def __copy__(self):
        raise PreflightForgery("preflight tokens are single-use; copying is forbidden")

    def __deepcopy__(self, memo):
        raise PreflightForgery("preflight tokens are single-use; copying is forbidden")

    def __reduce__(self):
        raise PreflightForgery("preflight tokens cannot be pickled")


class OpenPreflightToken(PreflightToken):
    """Authorizes a single opening / position-increasing order."""


class ReduceOnlyPreflightToken(PreflightToken):
    """Authorizes a single position-decreasing order, bound to a specific side + qty."""

    __slots__ = ("side", "qty")


def is_authentic(token) -> bool:
    return isinstance(token, PreflightToken) and getattr(token, "_nonce", None) in _live_nonces


def consume(token) -> None:
    if not is_authentic(token):
        raise PreflightForgery("token is forged, reused, or invalid")
    _live_nonces.discard(token._nonce)


def mint_open_token(config, intent) -> OpenPreflightToken:
    """M0: reject-all. The real open preflight (gates + market checks) lands in M5."""
    raise PreflightRejected(
        "open preflight is not implemented until M5; the committed config opens nothing"
    )


def mint_reduce_only_token(position, intent) -> ReduceOnlyPreflightToken:
    """Mint only for a genuinely position-decreasing order against the held position."""
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
    token = ReduceOnlyPreflightToken(key=_MINT, symbol=position.symbol, intent_id=intent.intent_id)
    token.side = required_side
    token.qty = order_qty
    return token
