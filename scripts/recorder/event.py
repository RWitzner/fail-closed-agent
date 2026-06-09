"""Databento schema -> typed parsed event (contract §B; S2 born-from-vendor seam).

Pure parser: one raw frame (``bytes`` of one JSON object offline, or an
already-decoded ``dict``) -> a frozen typed event. No I/O, no clock, no float.

Every vendor price/size is converted via ``Decimal(str(x))`` (NEVER ``float()``)
and round-trip quantized at parse, so ``str(Decimal)`` is canonical by
construction for both prices (``PRICE_QUANTUM``, 4dp) and sizes (``SIZE_QUANTUM``,
whole shares). A float / NaN / Inf / None in a price-or-size slot, a sub-$1
sub-penny price that would not round-trip, a fractional share, a missing required
field, or an unknown schema all FAIL LOUD (spec §14 fail-closed) — never silently
coerced.

BLOCKER 1: the vendor sequence is ``Provenance.vendor_seq`` (renamed from ``seq``,
which collides with the journal's reserved monotonic ``seq``). It is ``None``
where the dataset has no meaningful per-venue sequence.
"""
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Dict, Optional, Tuple, Type

# Databento fixed-point note: int * 1e-9 USD (tier-2 re-verify; offline fixtures
# use Decimal-as-string). Offline we receive strings already.
PRICE_SCALE = Decimal("1e-9")
PRICE_QUANTUM = Decimal("0.0001")  # 4dp Reg-NMS sub-penny tick floor; quantize at parse
SIZE_QUANTUM = Decimal("1")        # MAJOR 3: whole shares; quantize so str(Decimal) is canonical

MAX_DEPTH_LEVELS = 10              # mbp-10 invariant: <=10 levels each side

# D4 (R2#4): trades `side` is a CLOSED vocabulary (vendor aggressor flag).
# "A"=ask-aggressor, "B"=bid-aggressor, "N"=none/unknown. Anything else FAILS LOUD.
TRADE_SIDES = frozenset({"A", "B", "N"})


class UnknownSchema(ValueError):
    """Schema not in SCHEMA_REGISTRY -> explicit reject (no silent fallback)."""


class MalformedRecord(ValueError):
    """Missing / extra-typed required field -> FATAL (fail-closed)."""


class NonFinitePrice(ValueError):
    """NaN/Inf/None (or any float) where a finite Decimal is required (S2)."""


class PrecisionLoss(MalformedRecord):
    """A price/size that does NOT round-trip through its quantum -> FATAL (MAJOR 4)."""


@dataclass(frozen=True)
class Provenance:
    dataset: str
    schema: str                   # typed schema tag; dispatch key (also keys SCHEMA_REGISTRY)
    instrument_id: int            # symbol identity (replaces token_ids)
    symbol: str
    vendor_seq: Optional[int]     # BLOCKER 1: vendor sequence; None where no per-venue seq
    ts_event_utc: str             # ISO-8601 UTC, derived from vendor ts_event
    ts_recv_utc: str              # recorder receipt stamp, ISO-8601 UTC
    reconnect_epoch: int          # stamped by recorder; 0 at first connect, bumped per reconnect


@dataclass(frozen=True)
class QuoteEvent:                 # tbbo / bbo-1s / bbo-1m / mbp-1 (L1 top-of-book NBBO)
    provenance: Provenance
    bid_px: Decimal
    bid_sz: Decimal
    ask_px: Decimal
    ask_sz: Decimal


@dataclass(frozen=True)
class TradeEvent:                 # trades
    provenance: Provenance
    price: Decimal
    size: Decimal
    side: str                     # "A" | "B" | "N" (vendor aggressor flag; "N" = none/unknown)


@dataclass(frozen=True)
class BarEvent:                   # ohlcv-1s / ohlcv-1m (vendor-emitted bars)
    provenance: Provenance
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class DepthLevel:
    px: Decimal                   # quantized PRICE_QUANTUM, ROUND_HALF_EVEN, round-trip checked
    sz: Decimal                   # quantized SIZE_QUANTUM (whole shares), >= 0, round-trip checked
    ct: int                       # order count at level; carried, but EXCLUDED from book_hash (§D)


@dataclass(frozen=True)
class DepthEvent:                 # mbp-10 (L2, <=10 levels each side; full snapshot)
    provenance: Provenance
    bids: Tuple[DepthLevel, ...]  # length <= 10, vendor order, index 0 = best/inside
    asks: Tuple[DepthLevel, ...]


@dataclass(frozen=True)
class DefinitionEvent:            # definitions (instrument_id <-> symbol/MIC identity)
    provenance: Provenance
    mic: str
    raw_symbol: str


# --- MAJOR 6: frozen schema -> Event dispatch registry (data, not prose) ----------
SCHEMA_REGISTRY: Dict[str, Type] = {
    "tbbo": QuoteEvent,           # PRIMARY NBBO source
    "bbo-1s": QuoteEvent,
    "bbo-1m": QuoteEvent,
    "mbp-1": QuoteEvent,
    "trades": TradeEvent,
    "ohlcv-1s": BarEvent,
    "ohlcv-1m": BarEvent,
    "mbp-10": DepthEvent,
    "definitions": DefinitionEvent,
}


def _quantize_checked(raw, quantum: Decimal, *, field: str) -> Decimal:
    """MAJOR 3 + MAJOR 4 single chokepoint.

    ``d = Decimal(str(raw))``; reject non-finite / None / float (NonFinitePrice);
    ``q = d.quantize(quantum, ROUND_HALF_EVEN)``; if ``q != d`` raise PrecisionLoss
    (the quantize silently changed the value — e.g. a sub-$1 sub-penny price zeroed,
    or a fractional share where whole shares are required). On success returns the
    canonical quantized Decimal whose ``str()`` is stable (``'300.0'``/``'300'`` ->
    ``Decimal('300')`` -> ``'300'``; ``'1.5'``/``'1.50'`` -> ``'1.5000'``).
    """
    if raw is None or isinstance(raw, float):
        raise NonFinitePrice(f"{field}: float/None not allowed (got {raw!r}); use Decimal-as-string")
    try:
        d = Decimal(str(raw))
    except InvalidOperation as exc:
        raise MalformedRecord(f"{field}: not a number ({raw!r})") from exc
    if not d.is_finite():
        raise NonFinitePrice(f"{field}: non-finite value not allowed ({raw!r})")
    q = d.quantize(quantum, ROUND_HALF_EVEN)
    if q != d:
        raise PrecisionLoss(f"{field}: {raw!r} does not round-trip through quantum {quantum}")
    return q


def _require(record: dict, key: str):
    if key not in record:
        raise MalformedRecord(f"missing required field {key!r}")
    return record[key]


def _int_field(record: dict, key: str) -> int:
    value = _require(record, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedRecord(f"{key!r} must be an int (got {value!r})")
    return value


def _str_field(record: dict, key: str) -> str:
    value = _require(record, key)
    if not isinstance(value, str):
        raise MalformedRecord(f"{key!r} must be a str (got {value!r})")
    return value


def _vendor_seq(record: dict) -> Optional[int]:
    value = record.get("vendor_seq")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedRecord(f"'vendor_seq' must be an int or null (got {value!r})")
    return value


def _provenance(record: dict, *, dataset: str, schema: str,
                reconnect_epoch: int, ts_recv_utc: str) -> Provenance:
    return Provenance(
        dataset=dataset,
        schema=schema,
        instrument_id=_int_field(record, "instrument_id"),
        symbol=_str_field(record, "symbol"),
        vendor_seq=_vendor_seq(record),
        ts_event_utc=_str_field(record, "ts_event"),
        ts_recv_utc=ts_recv_utc,
        reconnect_epoch=reconnect_epoch,
    )


def _depth_level(raw, *, side: str) -> DepthLevel:
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise MalformedRecord(f"{side} level must be [px, sz, ct] (got {raw!r})")
    px_raw, sz_raw, ct_raw = raw
    if isinstance(ct_raw, bool) or not isinstance(ct_raw, int):
        raise MalformedRecord(f"{side} level count must be an int (got {ct_raw!r})")
    px = _quantize_checked(px_raw, PRICE_QUANTUM, field=f"{side}.px")
    sz = _quantize_checked(sz_raw, SIZE_QUANTUM, field=f"{side}.sz")
    if sz < 0:
        raise MalformedRecord(f"{side} level size must be >= 0 (got {sz_raw!r})")
    return DepthLevel(px=px, sz=sz, ct=ct_raw)


def _depth_side(record: dict, key: str) -> Tuple[DepthLevel, ...]:
    raw = _require(record, key)
    if not isinstance(raw, (list, tuple)):
        raise MalformedRecord(f"{key!r} must be a list of levels (got {raw!r})")
    if len(raw) > MAX_DEPTH_LEVELS:
        raise MalformedRecord(f"{key!r} exceeds mbp-10 max {MAX_DEPTH_LEVELS} levels ({len(raw)})")
    return tuple(_depth_level(level, side=key) for level in raw)


def _build_quote(record, prov) -> QuoteEvent:
    return QuoteEvent(
        provenance=prov,
        bid_px=_quantize_checked(_require(record, "bid_px"), PRICE_QUANTUM, field="bid_px"),
        bid_sz=_quantize_checked(_require(record, "bid_sz"), SIZE_QUANTUM, field="bid_sz"),
        ask_px=_quantize_checked(_require(record, "ask_px"), PRICE_QUANTUM, field="ask_px"),
        ask_sz=_quantize_checked(_require(record, "ask_sz"), SIZE_QUANTUM, field="ask_sz"),
    )


def _trade_side(record: dict) -> str:
    # D4 (R2#4): closed vocabulary {A,B,N}; an unknown side is FATAL (fail-closed).
    side = _str_field(record, "side")
    if side not in TRADE_SIDES:
        raise MalformedRecord(
            f"'side' must be one of {sorted(TRADE_SIDES)} (got {side!r})"
        )
    return side


def _build_trade(record, prov) -> TradeEvent:
    return TradeEvent(
        provenance=prov,
        price=_quantize_checked(_require(record, "price"), PRICE_QUANTUM, field="price"),
        size=_quantize_checked(_require(record, "size"), SIZE_QUANTUM, field="size"),
        side=_trade_side(record),
    )


def _build_bar(record, prov) -> BarEvent:
    return BarEvent(
        provenance=prov,
        open=_quantize_checked(_require(record, "open"), PRICE_QUANTUM, field="open"),
        high=_quantize_checked(_require(record, "high"), PRICE_QUANTUM, field="high"),
        low=_quantize_checked(_require(record, "low"), PRICE_QUANTUM, field="low"),
        close=_quantize_checked(_require(record, "close"), PRICE_QUANTUM, field="close"),
        volume=_quantize_checked(_require(record, "volume"), SIZE_QUANTUM, field="volume"),
    )


def _build_depth(record, prov) -> DepthEvent:
    return DepthEvent(
        provenance=prov,
        bids=_depth_side(record, "bids"),
        asks=_depth_side(record, "asks"),
    )


def _build_definition(record, prov) -> DefinitionEvent:
    return DefinitionEvent(
        provenance=prov,
        mic=_str_field(record, "mic"),
        raw_symbol=_str_field(record, "raw_symbol"),
    )


_BUILDERS = {
    QuoteEvent: _build_quote,
    TradeEvent: _build_trade,
    BarEvent: _build_bar,
    DepthEvent: _build_depth,
    DefinitionEvent: _build_definition,
}


def parse(record, *, dataset: str, schema: str, reconnect_epoch: int, ts_recv_utc: str) -> object:
    """Dispatch on ``schema`` via SCHEMA_REGISTRY -> one of the *Event dataclasses.

    - schema not in SCHEMA_REGISTRY -> UnknownSchema (no silent reinterpretation).
    - missing/invalid required field -> MalformedRecord.
    - a float / NaN / Inf / None in a price/size slot -> NonFinitePrice.
    - a price/size that does NOT round-trip through its quantum -> PrecisionLoss (MAJOR 4).
    - >10 levels on a depth side -> MalformedRecord (mbp-10 invariant).

    Prices are quantized to PRICE_QUANTUM (ROUND_HALF_EVEN) and sizes to SIZE_QUANTUM
    so ``str(Decimal)`` is canonical for BOTH. NEVER returns a float anywhere.
    """
    if isinstance(record, (bytes, bytearray)):
        try:
            record = json.loads(record)
        except (json.JSONDecodeError, ValueError) as exc:
            raise MalformedRecord(f"raw frame is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise MalformedRecord(f"record must be a dict (got {type(record).__name__})")

    event_type = SCHEMA_REGISTRY.get(schema)
    if event_type is None:
        raise UnknownSchema(f"schema {schema!r} not in SCHEMA_REGISTRY")

    prov = _provenance(
        record, dataset=dataset, schema=schema,
        reconnect_epoch=reconnect_epoch, ts_recv_utc=ts_recv_utc,
    )
    return _BUILDERS[event_type](record, prov)
