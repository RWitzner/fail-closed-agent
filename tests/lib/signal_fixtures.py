"""M3 §K — programmatic fixture builders for the signal tier.

Pure, seeded (explicit LCG — no `random`, no wall clock); rows are reproducible
byte-for-byte. Quote rows are PERSISTED-row-shaped (event_row.py flat field names,
Decimal-strings) — the same shape `journal.replay` returns for the M1 events stream.
"""
import json
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

DATASET = "EQUS.MINI"
SCHEMA = "tbbo"
DATA_PIN_V1 = "EQUS.MINI:tbbo:1m:fixture:signal-aapl-v1"

CALENDAR_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "calendar" / "nyse_2026_schedule.json"
)


def load_calendar_fixture() -> dict:
    return json.loads(CALENDAR_FIXTURE_PATH.read_text(encoding="utf-8"))


def calendar_fixture_pin() -> str:
    """The committed M2 calendar fixture's own pin string (golden-report calendar_pin)."""
    return load_calendar_fixture()["pin"]


class _Lcg:
    """Tiny deterministic LCG (numerical recipes constants)."""

    def __init__(self, seed: int):
        self._state = seed & 0xFFFFFFFF

    def next(self) -> int:
        self._state = (1664525 * self._state + 1013904223) & 0xFFFFFFFF
        return self._state


def _utc_str(dt: datetime, *, whole_second_form: bool = False) -> str:
    dt = dt.astimezone(UTC)
    if whole_second_form:
        # The recorder's _ms_to_iso_utc form at whole seconds (no fractional part) —
        # exercises the §0 mixed-ISO-form discipline (rev2 SAFETY-F2).
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _quote_row(*, symbol, instrument_id, ts_event: datetime, ts_recv: datetime,
               bid: Decimal, ask: Decimal, vendor_seq: int,
               whole_second_recv: bool = False) -> dict:
    return {
        "schema": SCHEMA,
        "dataset": DATASET,
        "instrument_id": instrument_id,
        "symbol": symbol,
        "vendor_seq": vendor_seq,
        "ts_event_utc": _utc_str(ts_event),
        "ts_recv_utc": _utc_str(ts_recv, whole_second_form=whole_second_recv),
        "reconnect_epoch": 0,
        "bid_px": str(bid),
        "bid_sz": "300",
        "ask_px": str(ask),
        "ask_sz": "200",
    }


def quotes_session(*, symbol="AAPL", instrument_id=1001, session_date="2026-06-15",
                   start_et="09:30", minutes=75, seed=1234,
                   start_mid=Decimal("200.0000"),
                   include_special_rows=True, gap_minutes=()) -> list:
    """1-per-minute valid tbbo persisted rows; deterministic LCG price path.

    Each minute m gets one valid quote at second :30 of the minute, received 100ms
    later. `gap_minutes` (ints, 0-based from session start) emit NO quote at all
    (halt analog). With `include_special_rows`, minute 35 also gets a crossed quote,
    36 a locked one, 37 a zero-bid one (10:05/10:06/10:07 for a 09:30 start) — extra
    rows; the valid row still exists. Minutes 2 and 3 use the recorder's
    whole-second ts_recv form (mixed-ISO discipline).
    """
    year, month, day = (int(x) for x in session_date.split("-"))
    hh, mm = (int(x) for x in start_et.split(":"))
    session_start = datetime(year, month, day, hh, mm, 0, tzinfo=ET)

    lcg = _Lcg(seed)
    rows = []
    mid = start_mid
    vendor_seq = 1000
    for minute in range(minutes):
        if minute in gap_minutes:
            continue
        # deterministic walk: -4..+5 ticks of 0.01
        step = (lcg.next() % 10) - 4
        mid = mid + Decimal(step) * Decimal("0.0100")
        if mid < Decimal("1"):
            mid = Decimal("1.0000")
        bid = mid - Decimal("0.0100")
        ask = mid + Decimal("0.0100")
        ts_event = session_start + timedelta(minutes=minute, seconds=30)
        ts_recv = ts_event + timedelta(milliseconds=100)
        whole_second = minute in (2, 3)
        if whole_second:
            ts_recv = ts_event.replace(second=31, microsecond=0)
        rows.append(_quote_row(
            symbol=symbol, instrument_id=instrument_id,
            ts_event=ts_event, ts_recv=ts_recv, bid=bid, ask=ask,
            vendor_seq=vendor_seq, whole_second_recv=whole_second))
        vendor_seq += 1

        if include_special_rows and minute == 35:   # crossed
            rows.append(_quote_row(
                symbol=symbol, instrument_id=instrument_id,
                ts_event=ts_event + timedelta(seconds=10),
                ts_recv=ts_event + timedelta(seconds=10, milliseconds=100),
                bid=ask + Decimal("0.0200"), ask=bid, vendor_seq=vendor_seq))
            vendor_seq += 1
        if include_special_rows and minute == 36:   # locked
            rows.append(_quote_row(
                symbol=symbol, instrument_id=instrument_id,
                ts_event=ts_event + timedelta(seconds=10),
                ts_recv=ts_event + timedelta(seconds=10, milliseconds=100),
                bid=mid, ask=mid, vendor_seq=vendor_seq))
            vendor_seq += 1
        if include_special_rows and minute == 37:   # zero-bid
            rows.append(_quote_row(
                symbol=symbol, instrument_id=instrument_id,
                ts_event=ts_event + timedelta(seconds=10),
                ts_recv=ts_event + timedelta(seconds=10, milliseconds=100),
                bid=Decimal("0.0000"), ask=ask, vendor_seq=vendor_seq))
            vendor_seq += 1
    return rows


def quotes_session_v1(symbol="AAPL", instrument_id=1001) -> list:
    """The §K primary fixture: 75 minutes on 2026-06-15 (covered REGULAR day)."""
    return quotes_session(symbol=symbol, instrument_id=instrument_id)


def future_receipt_quote(*, symbol="AAPL", instrument_id=1001) -> dict:
    """ts_event inside bucket 10:14->10:15 ET; ts_recv 10:21:00Z-equivalent (ET) —
    the S3 leakage probe (whole-second recv form on purpose)."""
    ts_event = datetime(2026, 6, 15, 10, 14, 45, tzinfo=ET)
    ts_recv = datetime(2026, 6, 15, 10, 21, 0, tzinfo=ET)
    return _quote_row(symbol=symbol, instrument_id=instrument_id,
                      ts_event=ts_event, ts_recv=ts_recv,
                      bid=Decimal("250.0000"), ask=Decimal("250.0200"),
                      vendor_seq=9999, whole_second_recv=True)


def gap_session_v1(*, symbol="AAPL", instrument_id=1001) -> list:
    """quotes_session_v1 but minutes 60-69 (10:30-10:39 ET) have no quotes."""
    return quotes_session(symbol=symbol, instrument_id=instrument_id,
                          gap_minutes=tuple(range(60, 70)))


def zero_variance_session(*, symbol="AAPL", instrument_id=1001, minutes=55) -> list:
    """Constant-mid session => z=0, vol=0, rsi14=50 (both-zero Wilder guard)."""
    rows = quotes_session(symbol=symbol, instrument_id=instrument_id,
                          minutes=minutes, include_special_rows=False)
    for row in rows:
        row["bid_px"] = "99.9900"
        row["ask_px"] = "100.0100"
    return rows


def zero_reference_brier_samples() -> list:
    """(p, outcome, p_ref) triples where the reference is PERFECT => BS_ref == 0."""
    return [
        (Decimal("0.700000"), 1, Decimal("1.000000")),
        (Decimal("0.300000"), 0, Decimal("0.000000")),
    ]
