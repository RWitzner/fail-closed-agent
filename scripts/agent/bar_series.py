"""M3 §B — explicit mid-bar label series (design §3.3, contract rev2).

M1's OHLCV ``Bar`` is NOT a BBO label (bar_cache.py:65-79 has no bid/ask/mid) — this
module owns the ONLY label read used by ``calibration.resolve``. The FD-2 watermark
predicate lives here: a mid bar is *eligible at* ``as_of`` iff
``bucket_end_utc <= as_of`` AND ``watermark_utc <= as_of`` — BOTH compared as PARSED
UTC instants, never as raw strings (rev2 SAFETY-F2: the repo mixes ISO-8601 surface
forms, and lexicographic compare leaks in the eligible direction).

Bucketing mirrors ``bar_cache.py``'s ET-boundary flooring rule ([start,end) in ET
wall-clock via zoneinfo, persisted UTC) — reimplemented per contract (the bar_cache
helpers are private). Every minted timestamp uses the canonical surface form
``strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"``.
"""
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Dict, Iterable, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

# Pinned context for persisted-value arithmetic (caller-mutable ambient context
# must never change persisted bytes — harden round 1, M3-R4).
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

MID_QUANTUM = Decimal("0.000001")

# MissingBar reason vocabulary (closed; §B rev2 defines all four).
MISSING_BAR_REASONS = frozenset({
    "no_quotes_in_bucket", "invalid_quotes_only", "future_receipt", "out_of_series",
})

_QUOTE_FIELDS = ("bid_px", "bid_sz", "ask_px", "ask_sz")


@dataclass(frozen=True)
class MidBar:
    symbol: str
    instrument_id: int
    interval: str                  # "1m" (FD-1; only value built in M3)
    bucket_start_utc: str          # ET-boundary bucket open, UTC ISO (canonical form)
    bucket_end_utc: str            # exclusive close — the t0/tH key
    session_date_et: str
    bid: Decimal                   # last valid quote of the bucket
    ask: Decimal
    mid: Decimal                   # (bid+ask)/2 quantized MID_QUANTUM; finite, > 0
    watermark_utc: str             # FD-2: ts_recv_utc of the contributing quote (S3 predicate)
    source_dataset: str            # e.g. "EQUS.MINI"
    source_schema: str             # input quote schema, e.g. "tbbo" (FD-1 rev2: input ONLY)
    data_pin: str                  # §I data-pin format
    quote_provenance: dict         # {ts_event_utc, ts_recv_utc, reconnect_epoch, vendor_seq}


@dataclass(frozen=True)
class MissingBar:
    symbol: str
    instrument_id: int
    interval: str
    bucket_end_utc: str
    reason: str                    # ∈ MISSING_BAR_REASONS


def _parse_utc(ts: str) -> datetime:
    """ISO-8601 UTC -> aware datetime. Accepts 'Z' or numeric offsets, rejects naive
    (mirrors bar_cache._parse_utc; SAFETY-F2 parse-then-compare chokepoint)."""
    raw = ts
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        raise ValueError(f"cannot parse UTC timestamp: {raw!r}")
    if dt.tzinfo is None:
        raise ValueError(f"timestamp is not timezone-aware: {raw!r}")
    return dt.astimezone(UTC)


def _canonical_utc(dt: datetime) -> str:
    """The ONE canonical minted surface form (§0 rev2)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _floor_minute_et(dt_utc: datetime) -> datetime:
    """Floor a UTC instant to its 1-minute ET wall-clock bucket (bar_cache rule)."""
    dt_et = dt_utc.astimezone(ET)
    return dt_et.replace(second=0, microsecond=0)


def _decimal_field(row: dict, key: str) -> Decimal:
    if key not in row:
        raise ValueError(f"quote row missing required field {key!r} (M1 persists all four)")
    raw = row[key]
    if raw is None or isinstance(raw, float):
        raise ValueError(f"quote row field {key!r} is {raw!r}; M1 rows carry Decimal-strings")
    if isinstance(raw, Decimal):
        return raw   # value-identical to Decimal(str(raw)); hot on the live path
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        raise ValueError(f"quote row field {key!r} unparseable as Decimal: {raw!r}")


def resample_midbars(quote_rows: Iterable[dict], *, symbol: str, instrument_id: int,
                     interval: str = "1m", dataset: str, schema: str, data_pin: str
                     ) -> Tuple[List[MidBar], List[MissingBar]]:
    """Deterministic mid-bar resampler over persisted quote rows.

    Returns ``(bars, missing)`` — ``missing`` carries one
    ``MissingBar(reason="invalid_quotes_only")`` per bucket that had quotes but no
    VALID one (rev2 BUILD-F1: the reader cannot reconstruct this from bars). Rows
    whose ``(schema, symbol, instrument_id)`` do not match the kwargs are
    deterministically SKIPPED (rev2 REPO-F10). Truly empty buckets yield nothing.
    """
    if interval != "1m":
        raise ValueError(f"only interval '1m' is built in M3 (FD-1), got {interval!r}")

    # Buckets are keyed by the UTC INSTANT of the ET-floored bucket start — never by
    # the ET wall-clock datetime: PEP 495 ignores `fold` in aware-datetime equality,
    # so the fall-back hour's two 01:xx EDT/EST buckets would otherwise COLLIDE into
    # one bar (harden round 1, M3-EDGE-1). The paired ET datetime is kept only for
    # session_date_et.
    seen_any: Dict[datetime, datetime] = {}   # utc_start -> floored ET (for session date)
    best_valid: Dict[datetime, tuple] = {}

    for index, row in enumerate(quote_rows):
        if not isinstance(row, dict):
            raise ValueError(f"quote row must be a dict (got {type(row).__name__})")
        if (row.get("schema") != schema or row.get("symbol") != symbol
                or row.get("instrument_id") != instrument_id):
            continue  # mixed-stream row: filtered, never an error (REPO-F10)
        ts_event = row.get("ts_event_utc")
        ts_recv = row.get("ts_recv_utc")
        if not isinstance(ts_event, str) or not isinstance(ts_recv, str):
            raise ValueError("quote row missing ts_event_utc/ts_recv_utc")
        event_instant = _parse_utc(ts_event)
        floored = _floor_minute_et(event_instant)
        utc_start = floored.astimezone(UTC)   # unambiguous instant key (M3-EDGE-1)

        bid = _decimal_field(row, "bid_px")
        bid_sz = _decimal_field(row, "bid_sz")
        ask = _decimal_field(row, "ask_px")
        ask_sz = _decimal_field(row, "ask_sz")

        seen_any[utc_start] = floored
        # Validity for the LABEL series: prices finite, > 0, not crossed (locked OK).
        valid = (bid.is_finite() and ask.is_finite() and bid > 0 and ask > 0
                 and bid <= ask)
        if not valid:
            continue
        vendor_seq = row.get("vendor_seq")
        seq_key = vendor_seq if isinstance(vendor_seq, int) else -1
        sort_key = (event_instant, seq_key, index)
        current = best_valid.get(utc_start)
        if current is None or sort_key >= current[0]:
            best_valid[utc_start] = (sort_key, {
                "bid": bid, "bid_sz": bid_sz, "ask": ask, "ask_sz": ask_sz,
                "ts_event_utc": ts_event, "ts_recv_utc": ts_recv,
                "reconnect_epoch": row.get("reconnect_epoch"),
                "vendor_seq": vendor_seq,
            })

    bars: List[MidBar] = []
    missing: List[MissingBar] = []
    for start_utc in sorted(seen_any):
        floored = seen_any[start_utc]
        end_utc = start_utc + timedelta(minutes=1)
        bucket_end = _canonical_utc(end_utc)
        chosen = best_valid.get(start_utc)
        if chosen is None:
            missing.append(MissingBar(
                symbol=symbol, instrument_id=instrument_id, interval=interval,
                bucket_end_utc=bucket_end, reason="invalid_quotes_only",
            ))
            continue
        _, q = chosen
        with localcontext(_DECIMAL_CTX):
            # (bid+ask)/2 of 4dp prices is at most 5dp ⊂ the 1e-6 grid — round-trip
            # ENFORCED (§L; harden round 1, M3-R1-003).
            mid_raw = (q["bid"] + q["ask"]) / 2
            mid = mid_raw.quantize(MID_QUANTUM, ROUND_HALF_EVEN)
            if mid != mid_raw:
                raise ValueError(
                    f"mid {mid_raw} does not round-trip through {MID_QUANTUM} "
                    "(precision loss — logic error)")
        bars.append(MidBar(
            symbol=symbol, instrument_id=instrument_id, interval=interval,
            bucket_start_utc=_canonical_utc(start_utc),
            bucket_end_utc=bucket_end,
            session_date_et=floored.strftime("%Y-%m-%d"),
            bid=q["bid"], ask=q["ask"], mid=mid,
            watermark_utc=q["ts_recv_utc"],
            source_dataset=dataset, source_schema=schema, data_pin=data_pin,
            quote_provenance={
                "ts_event_utc": q["ts_event_utc"],
                "ts_recv_utc": q["ts_recv_utc"],
                "bid_sz": q["bid_sz"],
                "ask_sz": q["ask_sz"],
                "reconnect_epoch": q["reconnect_epoch"],
                "vendor_seq": q["vendor_seq"],
            },
        ))
    return bars, missing


class MidBarSeriesReader:
    """Read seam over (bars ∪ missing). All lookups key on the PARSED bucket_end
    instant so mixed ISO-8601 surface forms resolve to the same record (SAFETY-F2).

    ``get()`` resolution for an absent record (rev2 BUILD-F1): per
    ``(symbol, instrument_id)`` the coverage is ``[min, max]`` over all records'
    bucket_end; inside coverage -> ``no_quotes_in_bucket``; outside coverage or an
    unknown series key -> ``out_of_series``.
    """

    def __init__(self, bars: Iterable[MidBar],
                 missing: Iterable[MissingBar] = ()) -> None:
        self._bars: Dict[tuple, Dict[datetime, MidBar]] = {}
        self._missing: Dict[tuple, Dict[datetime, MissingBar]] = {}

        def _admit(record, table, last_end):
            # Ordering is enforced per (symbol, instrument_id) WITHIN each input
            # stream (bars and missing are each sorted resampler outputs); duplicates
            # are rejected across both tables below.
            key = (record.symbol, record.instrument_id)
            end = _parse_utc(record.bucket_end_utc)
            prev = last_end.get(key)
            if prev is not None and end <= prev:
                raise ValueError(
                    "bar series must be strictly increasing per (symbol, instrument_id): "
                    f"{record.bucket_end_utc} after {prev.isoformat()} for {key}"
                )
            last_end[key] = end
            table.setdefault(key, {})[end] = record

        bars_last: Dict[tuple, datetime] = {}
        missing_last: Dict[tuple, datetime] = {}
        for bar in bars:
            _admit(bar, self._bars, bars_last)
        for miss in missing:
            if miss.reason not in MISSING_BAR_REASONS:
                raise ValueError(f"out-of-vocabulary MissingBar reason: {miss.reason!r}")
            _admit(miss, self._missing, missing_last)
        for key, table in self._missing.items():
            overlap = set(table) & set(self._bars.get(key, {}))
            if overlap:
                raise ValueError(
                    f"bucket present as BOTH bar and missing for {key}: "
                    f"{sorted(dt.isoformat() for dt in overlap)}"
                )

        self._coverage: Dict[tuple, Tuple[datetime, datetime]] = {}
        for key in set(self._bars) | set(self._missing):
            ends = list(self._bars.get(key, {})) + list(self._missing.get(key, {}))
            self._coverage[key] = (min(ends), max(ends))

        # Per-key ascending (ends, watermarks, bars) parallel arrays, parsed
        # ONCE at construction: eligible_history is called per (decision ×
        # symbol) by the historical runners, and a per-call sort + per-bar
        # watermark parse is O(B²) per symbol. The reader is immutable.
        self._eligible_index: Dict[tuple, Tuple[list, list, list]] = {}
        for key, table in self._bars.items():
            ends = sorted(table)
            self._eligible_index[key] = (
                ends,
                [_parse_utc(table[end].watermark_utc) for end in ends],
                [table[end] for end in ends],
            )

    def get(self, symbol: str, instrument_id: int, bucket_end_utc: str,
            *, as_of_utc: Optional[str] = None) -> Union[MidBar, MissingBar]:
        key = (symbol, instrument_id)
        end = _parse_utc(bucket_end_utc)

        def _missing_bar(reason: str) -> MissingBar:
            return MissingBar(symbol=symbol, instrument_id=instrument_id,
                              interval="1m", bucket_end_utc=bucket_end_utc,
                              reason=reason)

        miss = self._missing.get(key, {}).get(end)
        if miss is not None:
            return miss
        bar = self._bars.get(key, {}).get(end)
        if bar is None:
            coverage = self._coverage.get(key)
            if coverage is None or not (coverage[0] <= end <= coverage[1]):
                return _missing_bar("out_of_series")
            return _missing_bar("no_quotes_in_bucket")
        if as_of_utc is not None:
            as_of = _parse_utc(as_of_utc)
            if _parse_utc(bar.bucket_end_utc) > as_of or _parse_utc(bar.watermark_utc) > as_of:
                return _missing_bar("future_receipt")
        return bar

    def latest_eligible(self, symbol: str, instrument_id: int,
                        *, as_of_utc: str) -> Optional[MidBar]:
        history = self.eligible_history(symbol, instrument_id,
                                        as_of_utc=as_of_utc, max_bars=1)
        return history[-1] if history else None

    def eligible_history(self, symbol: str, instrument_id: int,
                         *, as_of_utc: str, max_bars: int) -> Tuple[MidBar, ...]:
        """The last <= max_bars FD-2-eligible bars, ascending by bucket_end.

        Served from the construction-time index: bisect the ``end <= as_of``
        cut, then walk BACKWARDS collecting bars whose watermark is eligible
        until ``max_bars`` — the last-K of the filtered ascending list is
        exactly the first K found walking backwards, reversed."""
        index = self._eligible_index.get((symbol, instrument_id))
        if index is None or max_bars <= 0:
            return ()
        as_of = _parse_utc(as_of_utc)
        ends, watermarks, bars = index
        cut = bisect_right(ends, as_of)
        picked: List[MidBar] = []
        for i in range(cut - 1, -1, -1):
            if watermarks[i] <= as_of:
                picked.append(bars[i])
                if len(picked) == max_bars:
                    break
        picked.reverse()
        return tuple(picked)
