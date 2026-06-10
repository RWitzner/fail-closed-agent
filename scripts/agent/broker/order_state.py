"""M5 §F — the ONE order-payload parser chokepoint (FD-M5-16/17/18).

Every broker order payload — fixture-shaped or live SDK-converted — passes through
`parse_order_payload`; fill accounting passes through `fill_delta`. Posture is the
M4 §B account parser's (risk/account_state.py): parsing is CONSTRUCTIVE — a
defective payload never becomes a `BrokerOrder`, it becomes `OrderInvalid`; the
read path NEVER raises on payload data. An out-of-vocab `source` is a CALLER bug
and raises `ExecError` (§2.4 posture: out-of-vocab anywhere is FATAL, never
coerced).

`OrderInvalid.reason` string conventions (M4 §B forms, frozen):
  "missing_field:<k>", "float_typed:<k>", "bool_typed:<k>", "non_finite:<k>",
  "unparseable:<k>", "invalid_type:<k>", "negative:<k>"   — per-slot defects
  "nonpositive_avg_price"      — filled_avg_price <= 0 when present (EX-10)
  "avg_price_without_fill"     — filled_avg_price present with filled_qty == 0
  "filled_qty_regression"      — cur.filled_qty < prev.filled_qty
  "nonpositive_delta_cost"     — delta_qty > 0 with delta_cost <= 0 (EX-10)
  "missing_field:filled_avg_price" — filled_qty > 0 with no avg price (the
      missing_field form reused: the slot is REQUIRED once shares have filled)

`fill_id` is NOT produced here — `exec_ledger.record_broker_fill` is its ONE home
(§P.3; M5C-T7). The FD-M5-18 terminal Sigma-consistency check is the watcher's
duty over the deltas this module emits.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional, Protocol, Union, runtime_checkable

from agent.exec_reasons import ExecError, FILL_SOURCES, ORDER_STATES, TERMINAL_STATES
from agent.serializer import BrokerUSD

# FROZEN; total over the 16 verified Alpaca status strings (§0.2 A4). Any string
# not in the map -> "unknown". unknown is NEVER terminal, never filled: the watcher
# journals order_state_alert + best-effort cancel + keeps polling (FD-M5-16).
ALPACA_STATUS_MAP = {
    "new": "accepted", "accepted": "accepted", "pending_new": "accepted",
    "partially_filled": "partially_filled", "filled": "filled",
    "canceled": "canceled", "pending_cancel": "pending_cancel",
    "expired": "expired", "rejected": "rejected", "done_for_day": "done_for_day",
    "replaced": "unknown", "pending_replace": "unknown", "suspended": "unknown",
    "accepted_for_bidding": "unknown", "stopped": "unknown", "calculated": "unknown",
}

# Structural invariants of the frozen map against the §2.4 vocabularies — a drifted
# exec_reasons would make this module lie, so it refuses to import instead.
if not set(ALPACA_STATUS_MAP.values()) <= ORDER_STATES:
    raise ExecError("ALPACA_STATUS_MAP targets must be a subset of ORDER_STATES")
if "unknown" in TERMINAL_STATES or "unknown" not in ORDER_STATES:
    raise ExecError("'unknown' must be a non-terminal member of ORDER_STATES")

# PDT rejection detection (FD-M4-15 wiring reserved for M5). Pinned VERBATIM from
# the M4 home (risk/pdt_compat.py:18-19 — order_state may import only stdlib /
# serializer / exec_reasons, so the set is mirrored, not imported; parity is
# test-asserted). FINALIZED AT M5-2a against the real paper API's rejection text.
PDT_REJECTION_CODES = frozenset({40310100})   # Alpaca's documented PDT-protection code
PDT_REJECTION_MARKERS = ("pattern day trad", "day trading buying power", "day-trade")
                                              # lowercase substring match (frozen)

_REQUIRED_KEYS = ("id", "client_order_id", "status", "symbol", "side",
                  "qty", "filled_qty")
_IDENTITY_KEYS = frozenset({"id", "client_order_id", "status", "symbol", "side"})
_ZERO = Decimal("0")


@dataclass(frozen=True)
class BrokerOrder:
    """One parsed broker order snapshot (cumulative aggregates, REST polling)."""

    broker_order_id: str
    client_order_id: str               # = our deterministic order_id (FD-M5-7)
    symbol: str
    side: str
    state: str                         # in ORDER_STATES (mapped, fail-closed)
    raw_status: str                    # the Alpaca string, provenance
    qty: Decimal
    filled_qty: Decimal
    filled_avg_price: Optional[Decimal]
    limit_price: Optional[Decimal]
    ts_broker_utc: Optional[str]       # updated_at|filled_at|canceled_at best available
    source: str                        # in FILL_SOURCES


@dataclass(frozen=True)
class OrderInvalid:
    """A defective payload/snapshot pair as DATA (S2 at the broker seam): alert +
    fail-closed handling upstream, never a constructed order, never a fill row."""

    reason: str
    raw: Mapping


@dataclass(frozen=True)
class BrokerRejection:
    """An HTTP-level broker rejection as DATA (FD-M5-17: terminal for that
    client_order_id). Build via `classify_rejection` so `pdt_marker_matched` is
    derived in exactly one place."""

    http_status: Optional[int]
    code: Optional[int]
    message: str
    pdt_marker_matched: bool   # code in PDT_REJECTION_CODES OR a frozen marker
                               # substring, case-insensitive (M4 FD-M4-15 set;
                               # finalized at M5-2a)


def classify_rejection(*, http_status: Optional[int], code: Optional[int],
                       message) -> BrokerRejection:
    """Total over weird payloads (the pdt_compat._is_pdt_rejection posture): a
    match failure is False, never an exception — detection must not be able to
    break the reject path."""
    try:
        matched = bool(code is not None and not isinstance(code, bool)
                       and code in PDT_REJECTION_CODES)
        if not matched:
            lowered = str(message or "").lower()
            matched = any(marker in lowered for marker in PDT_REJECTION_MARKERS)
    except Exception:  # noqa: BLE001 — detection must be total (FD-M4-15)
        matched = False
    return BrokerRejection(http_status=http_status, code=code,
                           message=str(message or ""),
                           pdt_marker_matched=matched)


def _parse_money_value(value, *, field: str):
    """(Decimal, None) or (None, reason). Mirrors risk/account_state.py:62-80
    byte-for-byte in posture: bool/float/non-Decimal-non-str rejected by TYPE,
    Decimal(str(v)) canonicalization, non-finite rejected."""
    if isinstance(value, bool):
        return None, f"bool_typed:{field}"
    if isinstance(value, float):
        return None, f"float_typed:{field}"
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return None, f"unparseable:{field}"
    else:
        return None, f"invalid_type:{field}"
    if not parsed.is_finite():
        return None, f"non_finite:{field}"
    return Decimal(str(parsed)), None


def parse_order_payload(payload: Mapping, *,
                        source: str) -> Union[BrokerOrder, OrderInvalid]:
    """The ONE parser for raw broker order dicts (fixture and live alike, §G).

    NEVER raises on payload data — every defect returns `OrderInvalid` (EX-10;
    the M4 §B posture). `source` out of FILL_SOURCES raises `ExecError`: which
    seam produced the payload is the CALLER's code, not broker data.
    """
    if source not in FILL_SOURCES:
        raise ExecError(f"out-of-vocabulary fill source: {source!r}")

    def invalid(reason: str) -> OrderInvalid:
        return OrderInvalid(reason=reason, raw=payload)

    for key in _REQUIRED_KEYS:
        if key not in payload or payload[key] is None:
            return invalid(f"missing_field:{key}")
        if key in _IDENTITY_KEYS and not isinstance(payload[key], str):
            return invalid(f"invalid_type:{key}")

    qty, reason = _parse_money_value(payload["qty"], field="qty")
    if reason is not None:
        return invalid(reason)
    if qty < 0:
        return invalid("negative:qty")

    filled_qty, reason = _parse_money_value(payload["filled_qty"], field="filled_qty")
    if reason is not None:
        return invalid(reason)
    if filled_qty < 0:
        return invalid("negative:filled_qty")

    filled_avg_price: Optional[Decimal] = None
    if payload.get("filled_avg_price") is not None:
        filled_avg_price, reason = _parse_money_value(
            payload["filled_avg_price"], field="filled_avg_price")
        if reason is not None:
            return invalid(reason)
        if filled_avg_price <= 0:
            return invalid("nonpositive_avg_price")   # EX-10

    limit_price: Optional[Decimal] = None
    if payload.get("limit_price") is not None:
        limit_price, reason = _parse_money_value(
            payload["limit_price"], field="limit_price")
        if reason is not None:
            return invalid(reason)

    # Provenance stamp, best available (never compared, never parsed as time here).
    ts_broker_utc: Optional[str] = None
    for stamp_key in ("updated_at", "filled_at", "canceled_at"):
        stamp = payload.get(stamp_key)
        if isinstance(stamp, str) and stamp:
            ts_broker_utc = stamp
            break

    status = payload["status"]
    return BrokerOrder(
        broker_order_id=payload["id"],
        client_order_id=payload["client_order_id"],
        symbol=payload["symbol"],
        side=payload["side"],
        state=ALPACA_STATUS_MAP.get(status, "unknown"),
        raw_status=status,
        qty=qty,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        limit_price=limit_price,
        ts_broker_utc=ts_broker_utc,
        source=source,
    )


@dataclass(frozen=True)
class FillDelta:
    """One emitted fill increment, EXACT integrated notional (FD-M5-18)."""

    delta_qty: Decimal
    delta_cost_usd: BrokerUSD          # cur qty x avg - prev qty x avg, EXACT
    cum_filled_qty: Decimal
    filled_avg_price_after: Decimal


def _snapshot_defect(order: BrokerOrder) -> Optional[str]:
    """Cross-field fill-consistency defects on ONE snapshot (parse owns per-slot
    typing; this owns avg-vs-qty coherence)."""
    if order.filled_qty < 0:
        return "negative:filled_qty"
    if order.filled_avg_price is not None:
        if order.filled_qty == 0:
            return "avg_price_without_fill"
        if order.filled_avg_price <= 0:
            return "nonpositive_avg_price"
    elif order.filled_qty > 0:
        return "missing_field:filled_avg_price"
    return None


def fill_delta(prev: Optional[BrokerOrder],
               cur: BrokerOrder) -> Union[Optional[FillDelta], OrderInvalid]:
    """FD-M5-18: the exact fill increment between two polled snapshots.

    PURE over exactly the two snapshots it is given. `prev` is the CALLER's
    cursor and is FROZEN as the `BrokerOrder` snapshot of the last EMITTED
    `FillDelta` (`None` before the first): when `filled_qty` is unchanged —
    including a broker avg-price-only correction at the same `filled_qty` —
    this returns `None` and the caller MUST NOT advance `prev` (FD-M5-18/EX-6).
    That is the only cursor rule under which the emitted `delta_cost_usd` values
    telescope exactly to the broker's final `filled_qty x filled_avg_price`; an
    avg correction is then folded into the NEXT emitted delta, never dropped.

    delta_qty  = cur.filled_qty - prev.filled_qty          (prev terms 0 if None)
    delta_cost = cur.filled_qty x cur.filled_avg_price
                 - prev.filled_qty x prev.filled_avg_price  -- EXACT Decimal,
                 NEVER delta_qty x avg (provably wrong under avg drift).

    OrderInvalid (alert + fail-closed, no fill row; EX-10):
      "filled_qty_regression"          cur.filled_qty < prev.filled_qty
      "avg_price_without_fill"         avg present with filled_qty == 0
      "nonpositive_avg_price"          avg <= 0 on either snapshot
      "missing_field:filled_avg_price" filled_qty > 0 with avg None
      "nonpositive_delta_cost"         delta_qty > 0 with delta_cost <= 0
                                       (broker avg-correction noise must never
                                       journal a <=0-cost buy fill)

    `fill_id` is NOT produced here — exec_ledger.record_broker_fill is its ONE
    home (§P.3; M5C-T7).
    """
    raw = {"prev": prev, "cur": cur}
    if prev is not None:
        defect = _snapshot_defect(prev)
        if defect is not None:
            return OrderInvalid(reason=defect, raw=raw)
    defect = _snapshot_defect(cur)
    if defect is not None:
        return OrderInvalid(reason=defect, raw=raw)

    prev_qty = prev.filled_qty if prev is not None else _ZERO
    if cur.filled_qty < prev_qty:
        return OrderInvalid(reason="filled_qty_regression", raw=raw)
    if cur.filled_qty == prev_qty:
        return None   # unchanged (incl. avg-only correction) — caller keeps prev

    prev_cost = (prev.filled_qty * prev.filled_avg_price
                 if prev is not None and prev.filled_qty > 0 else _ZERO)
    delta_qty = cur.filled_qty - prev_qty
    delta_cost = cur.filled_qty * cur.filled_avg_price - prev_cost
    if delta_cost <= 0:
        return OrderInvalid(reason="nonpositive_delta_cost", raw=raw)   # EX-10
    return FillDelta(
        delta_qty=delta_qty,
        delta_cost_usd=BrokerUSD(delta_cost),
        cum_filled_qty=cur.filled_qty,
        filled_avg_price_after=cur.filled_avg_price,
    )


@runtime_checkable
class OrderApi(Protocol):
    """The order-wire seam: RAW wire dicts in and out (the M4 AccountReadProvider
    precedent, LD8) — parsing has ONE chokepoint above. Implemented by the real
    SDK adapter and by tests' ScriptedOrderApi alike."""

    def submit(self, payload: Mapping) -> Mapping: ...        # POST /v2/orders

    def get_by_client_order_id(self, client_order_id: str) -> Mapping: ...

    def cancel(self, broker_order_id: str) -> Mapping: ...    # DELETE /v2/orders/{id}

    def get_account(self) -> Mapping: ...                     # GET /v2/account

    def list_positions(self) -> list: ...                     # GET /v2/positions

    def list_open_orders(self) -> list: ...                   # GET /v2/orders?status=open
