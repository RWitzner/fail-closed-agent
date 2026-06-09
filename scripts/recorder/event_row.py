"""Flat persistence <-> replay seam for parsed events (contract §B2; BLOCKER 2).

The ``*Event`` dataclasses are NESTED (a ``Provenance`` sub-object + Decimal/tuple
payloads), but ``agent.journal.JournalWriter.append`` takes a FLAT dict. S3 requires
the replay-re-derived ``book_hash`` to byte-match record-time, which is only possible
if persistence and replay agree on the EXACT flat field names. This module is that
single source of truth: ``persistence.write_event`` and ``replay.py`` both bind to it.

No journal-reserved key is ever produced (``event_type``/``run_id``/``seq``/``hash``/
``decision_id``/``order_id``/``ts_utc``). The vendor sequence is ``vendor_seq`` — NOT
``seq`` (BLOCKER 1). Decimals are passed AS Decimal; the serializer renders them as
strings. ``from_row(to_row(ev)) == ev`` for every event type.
"""
from decimal import Decimal
from typing import Optional

from recorder.event import (
    BarEvent,
    DefinitionEvent,
    DepthEvent,
    DepthLevel,
    MalformedRecord,
    PRICE_QUANTUM,
    Provenance,
    QuoteEvent,
    SCHEMA_REGISTRY,
    SIZE_QUANTUM,
    TradeEvent,
    UnknownSchema,
    _quantize_checked,
)

# Common provenance prefix flattened onto EVERY row (§B2). No journal-owned key.
_PROV_FIELDS = (
    "schema",
    "dataset",
    "instrument_id",
    "symbol",
    "vendor_seq",
    "ts_event_utc",
    "ts_recv_utc",
    "reconnect_epoch",
)


def _prov_prefix(prov: Provenance) -> dict:
    return {
        "schema": prov.schema,
        "dataset": prov.dataset,
        "instrument_id": prov.instrument_id,
        "symbol": prov.symbol,
        "vendor_seq": prov.vendor_seq,  # int or None -> null in the serialized row
        "ts_event_utc": prov.ts_event_utc,
        "ts_recv_utc": prov.ts_recv_utc,
        "reconnect_epoch": prov.reconnect_epoch,
    }


def _level_to_row(level: DepthLevel) -> list:
    return [str(level.px), str(level.sz), level.ct]


def _level_from_row(raw, *, side: str) -> DepthLevel:
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise MalformedRecord(f"{side} level must be [px_str, sz_str, ct] (got {raw!r})")
    px_str, sz_str, ct = raw
    if isinstance(ct, bool) or not isinstance(ct, int):
        raise MalformedRecord(f"{side} level count must be an int (got {ct!r})")
    # C3: route rebuilt levels through the SAME quantize chokepoint as parse so a
    # hand-authored/non-canonical reference row ('300.0', '201.15') rebuilds to a
    # canonical DepthLevel and reconciles clean (defense-in-depth).
    px = _quantize_checked(px_str, PRICE_QUANTUM, field=f"{side}.px")
    sz = _quantize_checked(sz_str, SIZE_QUANTUM, field=f"{side}.sz")
    return DepthLevel(px=px, sz=sz, ct=ct)


def to_row(event, *, derived_book_hash: Optional[str] = None) -> dict:
    """Flatten any ``*Event`` into the flat dict defined in §B2.

    Decimals stay Decimal (the serializer renders them). No journal-reserved key
    is produced. For a DepthEvent, ``derived_book_hash`` is included (None if not
    yet computed).
    """
    prov = event.provenance
    row = _prov_prefix(prov)
    if isinstance(event, QuoteEvent):
        row.update(
            bid_px=event.bid_px, bid_sz=event.bid_sz,
            ask_px=event.ask_px, ask_sz=event.ask_sz,
        )
    elif isinstance(event, TradeEvent):
        row.update(price=event.price, size=event.size, side=event.side)
    elif isinstance(event, BarEvent):
        row.update(
            open=event.open, high=event.high, low=event.low,
            close=event.close, volume=event.volume,
        )
    elif isinstance(event, DepthEvent):
        row.update(
            bids=[_level_to_row(level) for level in event.bids],
            asks=[_level_to_row(level) for level in event.asks],
            derived_book_hash=derived_book_hash,
        )
    elif isinstance(event, DefinitionEvent):
        row.update(mic=event.mic, raw_symbol=event.raw_symbol)
    else:  # pragma: no cover - defensive; SCHEMA_REGISTRY exhausts the known types
        raise MalformedRecord(f"unknown event type {type(event).__name__}")
    return row


def _require(row: dict, key: str):
    if key not in row:
        raise MalformedRecord(f"missing required flat field {key!r}")
    return row[key]


def from_row(row: dict) -> object:
    """Inverse of ``to_row``: rebuild the exact ``*Event`` from a flat row.

    ``schema`` selects the dataclass via SCHEMA_REGISTRY; Decimal fields are rebuilt
    via ``Decimal(str(value))``; depth levels are rebuilt from ``[px_str, sz_str, ct]``.
    Unknown schema -> UnknownSchema; missing flat field -> MalformedRecord.
    """
    if not isinstance(row, dict):
        raise MalformedRecord(f"row must be a dict (got {type(row).__name__})")
    schema = _require(row, "schema")
    event_type = SCHEMA_REGISTRY.get(schema)
    if event_type is None:
        raise UnknownSchema(f"schema {schema!r} not in SCHEMA_REGISTRY")

    prov = Provenance(
        dataset=_require(row, "dataset"),
        schema=schema,
        instrument_id=_require(row, "instrument_id"),
        symbol=_require(row, "symbol"),
        vendor_seq=row.get("vendor_seq"),
        ts_event_utc=_require(row, "ts_event_utc"),
        ts_recv_utc=_require(row, "ts_recv_utc"),
        reconnect_epoch=_require(row, "reconnect_epoch"),
    )

    if event_type is QuoteEvent:
        return QuoteEvent(
            provenance=prov,
            bid_px=Decimal(str(_require(row, "bid_px"))),
            bid_sz=Decimal(str(_require(row, "bid_sz"))),
            ask_px=Decimal(str(_require(row, "ask_px"))),
            ask_sz=Decimal(str(_require(row, "ask_sz"))),
        )
    if event_type is TradeEvent:
        return TradeEvent(
            provenance=prov,
            price=Decimal(str(_require(row, "price"))),
            size=Decimal(str(_require(row, "size"))),
            side=_require(row, "side"),
        )
    if event_type is BarEvent:
        return BarEvent(
            provenance=prov,
            open=Decimal(str(_require(row, "open"))),
            high=Decimal(str(_require(row, "high"))),
            low=Decimal(str(_require(row, "low"))),
            close=Decimal(str(_require(row, "close"))),
            volume=Decimal(str(_require(row, "volume"))),
        )
    if event_type is DepthEvent:
        return DepthEvent(
            provenance=prov,
            bids=tuple(_level_from_row(level, side="bids") for level in _require(row, "bids")),
            asks=tuple(_level_from_row(level, side="asks") for level in _require(row, "asks")),
        )
    if event_type is DefinitionEvent:
        return DefinitionEvent(
            provenance=prov,
            mic=_require(row, "mic"),
            raw_symbol=_require(row, "raw_symbol"),
        )
    raise UnknownSchema(f"schema {schema!r} has no row builder")  # pragma: no cover
