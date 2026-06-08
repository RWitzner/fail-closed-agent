"""Pre-submit execution preflight: non-forgeable, single-use tokens (spec §5 Tier 6).

`Broker.submit_order()` is structurally unreachable without a valid preflight token,
minted only here. Tokens are split by intent:

- `OpenPreflightToken` — opening / position-increasing orders. **Reject-all in M0**
  (the full preflight — epoch/session/halt/freshness/tick/can_open + run gates —
  lands in M5). Disabled means reject-all, never `ok=True`.
- `ReduceOnlyPreflightToken` — mintable only for an existing held position + a
  position-decreasing order on the kill-switch / halt / close path, and is NOT
  gated off by the open run-gates, so flatten can always reduce exposure.

Tokens cannot be constructed directly (a module-private mint key is required) and
are single-use.
"""
from decimal import Decimal

_MINT = object()  # module-private; callers cannot obtain it, so tokens are non-forgeable


class PreflightRejected(Exception):
    """A token was requested but the preflight gates refuse to mint it."""


class PreflightForgery(Exception):
    """A token was constructed directly, tampered with, reused, or is the wrong kind."""


class PreflightToken:
    __slots__ = ("symbol", "intent_id", "_key", "_consumed")

    def __init__(self, *, key, symbol, intent_id):
        if key is not _MINT:
            raise PreflightForgery("preflight tokens cannot be constructed directly")
        self.symbol = symbol
        self.intent_id = intent_id
        self._key = key
        self._consumed = False


class OpenPreflightToken(PreflightToken):
    """Authorizes a single opening / position-increasing order."""


class ReduceOnlyPreflightToken(PreflightToken):
    """Authorizes a single position-decreasing (reduce-only) order."""


def is_authentic(token) -> bool:
    return (
        isinstance(token, PreflightToken)
        and getattr(token, "_key", None) is _MINT
        and token._consumed is False
    )


def consume(token) -> None:
    if not is_authentic(token):
        raise PreflightForgery("token is forged, reused, or invalid")
    token._consumed = True


def mint_open_token(config, intent) -> OpenPreflightToken:
    """M0: reject-all. The real open preflight (gates + market checks) lands in M5."""
    raise PreflightRejected(
        "open preflight is not implemented until M5; the committed config opens nothing"
    )


def mint_reduce_only_token(position, intent) -> ReduceOnlyPreflightToken:
    """Mint only for an existing held position + a position-decreasing order."""
    qty = getattr(position, "qty", None) if position is not None else None
    if qty is None or Decimal(qty) <= 0:
        raise PreflightRejected("reduce-only requires an existing held position")
    if not getattr(intent, "is_reducing", False):
        raise PreflightRejected("reduce-only requires a position-decreasing order")
    return ReduceOnlyPreflightToken(key=_MINT, symbol=intent.symbol, intent_id=intent.intent_id)
