"""Deterministic ET-session-boundary resampler (contract §J; S2 re-verify + DST).

Offline, stdlib-only (``zoneinfo``). No ``exchange_calendars`` in the offline core —
that calendar awareness is M2. The resampler buckets by ET wall-clock date only and
does NOT know half-days / holidays.

DST rules (frozen):
- All bucketing is done in ET wall-clock via ``zoneinfo.ZoneInfo("America/New_York")``.
- Each UTC instant is converted to ET independently (``dt_utc.astimezone(ET)``),
  floored to the interval in ET, then converted back to UTC for ``bucket_start_utc``.
  We NEVER enumerate wall-clock minutes or assume a fixed UTC offset.
- Spring-forward 02:00–03:00 ET: the skipped hour yields no bucket (harmless for RTH).
- Fall-back ambiguous 01:00–02:00 ET: disambiguated by the underlying UTC instant
  (UTC is monotonic) — no double-count, no 25-hour day.

S2 empty-window VWAP: ``resample`` SKIPS zero-volume buckets; a forced empty-window
VWAP raises ``EmptyWindowVWAP`` instead of ``0/0=NaN`` or ``x/0=Inf``. As a second
wall, ``serializer.dumps`` rejects a non-finite Decimal, so even a regression cannot
persist a NaN VWAP.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

VWAP_QUANTUM = Decimal("0.0001")  # 4dp, matches book_hash/event price scale

_INTERVAL_DELTA: Dict[str, timedelta] = {
    "1s": timedelta(seconds=1),
    "1m": timedelta(minutes=1),
    "1d": timedelta(days=1),
}


class EmptyWindowVWAP(ValueError):
    """Raised if a caller tries to MATERIALIZE a VWAP for a zero-volume window (S2)."""


def _vwap(px_vol_sum: Decimal, volume: Decimal, *, count: int) -> Decimal:
    """Internal VWAP computation (S2 born-here chokepoint).

    D7 (R2#7): the empty-window guard is LIVE, not dead. A zero-volume / zero-count
    window would produce ``0/0 = NaN`` or ``x/0 = Inf``; this RAISES ``EmptyWindowVWAP``
    BEFORE the division so a non-finite VWAP can never be materialized. ``resample``
    skips empty buckets so this raise is unreachable on the normal path, but any caller
    that forces a bar on an empty window fails loud and legibly here.

    Returns the canonical Decimal VWAP quantized to ``VWAP_QUANTUM`` (ROUND_HALF_EVEN).
    """
    if volume == Decimal("0") or count == 0:
        raise EmptyWindowVWAP(
            f"empty-window VWAP (volume={volume}, count={count}) — no division by zero"
        )
    raw_vwap = px_vol_sum / volume
    vwap = raw_vwap.quantize(VWAP_QUANTUM, ROUND_HALF_EVEN)
    if not vwap.is_finite():  # belt-and-suspenders: must be impossible after the guard
        raise EmptyWindowVWAP("non-finite VWAP computed — logic error")
    return vwap


@dataclass(frozen=True)
class Bar:
    symbol: str
    interval: str                   # "1s" | "1m" | "1d"
    bucket_start_utc: str           # bucket OPEN, UTC ISO-8601 (ET-aligned boundary -> UTC)
    bucket_end_utc: str             # bucket CLOSE (exclusive), UTC ISO-8601
    session_date_et: str            # "YYYY-MM-DD" ET trading-session date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Optional[Decimal]         # None for empty window — NEVER NaN/Inf; VWAP_QUANTUM otherwise
    trade_count: int


def et_session_date(ts_utc_iso: str) -> str:
    """Map a UTC instant to its ET trading-session date.

    Converts UTC -> ET via zoneinfo (DST-correct), then takes the ET calendar date.
    e.g. 2026-06-09T20:00:00Z -> ET 2026-06-09 (EDT, UTC-4);
         2026-12-09T21:00:00Z -> ET 2026-12-09 (EST, UTC-5).

    The input is an UNAMBIGUOUS UTC instant, so the fall-back fold never affects
    assignment (UTC->ET is always unambiguous).
    """
    dt_utc = _parse_utc(ts_utc_iso)
    dt_et = dt_utc.astimezone(ET)
    return dt_et.strftime("%Y-%m-%d")


def _parse_utc(ts: str) -> datetime:
    """Parse an ISO-8601 UTC string to a timezone-aware UTC datetime.

    Accepts ANY tz-aware ISO-8601 UTC form — a trailing ``Z`` OR a numeric
    offset like ``+00:00`` (the repo's own ``datetime.now(timezone.utc).isoformat()``
    emits ``+00:00``). Normalizes a single trailing ``Z`` to ``+00:00`` then defers
    to ``datetime.fromisoformat``; a naive (tz-less) string is rejected. The result
    is normalized to UTC via ``.astimezone(UTC)`` so a non-UTC offset still lands on
    the correct instant (contract §J / C5).
    """
    raw = ts
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        raise ValueError(f"cannot parse UTC timestamp: {raw!r}")
    if dt.tzinfo is None:
        raise ValueError(f"timestamp is not timezone-aware: {raw!r}")
    return dt.astimezone(UTC)


def _floor_to_interval_et(dt_et: datetime, interval: str) -> datetime:
    """Floor a timezone-aware ET datetime to the start of its interval bucket.

    For 1s/1m: standard floor.
    For 1d: midnight ET on the same calendar date.
    """
    if interval == "1s":
        return dt_et.replace(microsecond=0)
    if interval == "1m":
        return dt_et.replace(second=0, microsecond=0)
    if interval == "1d":
        # Midnight ET on the same ET calendar date — fold=0 is safe coming from UTC.
        return dt_et.replace(hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"unsupported interval: {interval!r}")


def _bucket_start_utc_str(dt_et_floored: datetime) -> str:
    """Convert an ET-floored datetime back to UTC ISO-8601 string."""
    dt_utc = dt_et_floored.astimezone(UTC)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _bucket_end_utc_str(dt_et_floored: datetime, interval: str) -> str:
    """Compute the exclusive bucket end as a UTC ISO-8601 string.

    For 1s/1m: add the fixed UTC delta (never spans a DST jump at those scales).
    For 1d: advance one calendar day in ET wall-clock then convert to UTC so the
    result lands on the true next ET midnight regardless of DST (contract §J).
    """
    if interval == "1d":
        next_date = dt_et_floored.date() + timedelta(days=1)
        next_et = datetime(next_date.year, next_date.month, next_date.day,
                           0, 0, 0, tzinfo=ET)
        dt_utc = next_et.astimezone(UTC)
    else:
        delta = _INTERVAL_DELTA[interval]
        dt_utc = dt_et_floored.astimezone(UTC) + delta
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def resample(events: Iterable, *, interval: str) -> List[Bar]:
    """Deterministic resampler.

    Bucket boundaries computed in ET wall-clock (via zoneinfo) then persisted as
    UTC (bucket_start_utc / bucket_end_utc) + session_date_et (spec §11). For each
    NON-EMPTY bucket emit a Bar; SKIP empty buckets entirely (no fabricated
    zero-volume bar). vwap = (sum px*sz) / (sum sz) in Decimal, quantized
    VWAP_QUANTUM ROUND_HALF_EVEN. If a caller forces a bar on an empty window,
    raises EmptyWindowVWAP — NEVER produces Decimal('NaN') / Decimal('Infinity').

    Input sorted defensively by the PARSED ts_event_utc instant for determinism.
    Sorting by the raw STRING is WRONG when mixed ISO-8601 forms are present (a
    bare 'Z' with no fractional seconds vs '.NNNNNN'): '.' (0x2E) < 'Z' (0x5A), so
    a later fractional trade would string-sort BEFORE an earlier bare-'Z' trade in
    the same bucket, swapping open/close. Sorting by ``_parse_utc(...)`` (a true
    instant) is correct regardless of the surface form (H2).
    """
    if interval not in _INTERVAL_DELTA:
        raise ValueError(f"unsupported interval: {interval!r}")

    event_list = sorted(events, key=lambda e: _parse_utc(e.provenance.ts_event_utc))

    # Group by (symbol, bucket_key) where bucket_key is the ET-floored datetime.
    # Use an ordered dict (insertion order = temporal order after sort).
    buckets: Dict[Tuple[str, str], dict] = {}

    for ev in event_list:
        symbol = ev.provenance.symbol
        ts = ev.provenance.ts_event_utc
        price: Decimal = ev.price
        size: Decimal = ev.size

        dt_utc = _parse_utc(ts)
        dt_et = dt_utc.astimezone(ET)
        floored = _floor_to_interval_et(dt_et, interval)
        # Key: (symbol, bucket_start_utc string) — stable string so dict key is hashable.
        start_str = _bucket_start_utc_str(floored)
        key = (symbol, start_str)

        if key not in buckets:
            buckets[key] = {
                "symbol": symbol,
                "floored_et": floored,
                "start_str": start_str,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": Decimal("0"),
                "px_vol_sum": Decimal("0"),  # sum of price * size (for VWAP)
                "trade_count": 0,
            }

        b = buckets[key]
        if price > b["high"]:
            b["high"] = price
        if price < b["low"]:
            b["low"] = price
        b["close"] = price
        b["volume"] += size
        b["px_vol_sum"] += price * size
        b["trade_count"] += 1

    bars: List[Bar] = []
    for b in buckets.values():
        volume = b["volume"]
        if volume == Decimal("0"):
            # Zero-volume bucket: skip (never emit fabricated zero-volume bar).
            continue

        # Non-empty bucket: compute VWAP via the single S2 chokepoint (D7). The
        # zero-volume guard inside _vwap is unreachable here (we already skipped
        # empty buckets above) but stays LIVE for any forced-empty-window caller.
        vwap = _vwap(b["px_vol_sum"], volume, count=b["trade_count"])

        floored = b["floored_et"]
        start_str = b["start_str"]
        end_str = _bucket_end_utc_str(floored, interval)
        session_date = floored.strftime("%Y-%m-%d")

        bars.append(Bar(
            symbol=b["symbol"],
            interval=interval,
            bucket_start_utc=start_str,
            bucket_end_utc=end_str,
            session_date_et=session_date,
            open=b["open"],
            high=b["high"],
            low=b["low"],
            close=b["close"],
            volume=volume,
            vwap=vwap,
            trade_count=b["trade_count"],
        ))

    return bars
