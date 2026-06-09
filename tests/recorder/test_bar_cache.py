"""Tests for recorder.bar_cache (contract §J, §N).

S2 re-verify + DST correctness.
"""
import json
import random
import unittest
from decimal import Decimal
from pathlib import Path

from agent.serializer import dumps as serializer_dumps
from recorder.bar_cache import (
    Bar,
    EmptyWindowVWAP,
    VWAP_QUANTUM,
    _vwap,
    et_session_date,
    resample,
)
from recorder.event import Provenance, TradeEvent

FIXTURES_BARS = Path(__file__).resolve().parents[1] / "fixtures" / "bars"
FIXTURES_DB = Path(__file__).resolve().parents[1] / "fixtures" / "databento"


def _make_trade(symbol, ts_event, price, size, vendor_seq=1, side="B"):
    prov = Provenance(
        dataset="EQUS.MINI",
        schema="trades",
        instrument_id=1001,
        symbol=symbol,
        vendor_seq=vendor_seq,
        ts_event_utc=ts_event,
        ts_recv_utc=ts_event,
        reconnect_epoch=0,
    )
    return TradeEvent(
        provenance=prov,
        price=Decimal(price),
        size=Decimal(size),
        side=side,
    )


def _load_trade_events(path):
    events = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        prov = Provenance(
            dataset=r["dataset"],
            schema=r["schema"],
            instrument_id=r["instrument_id"],
            symbol=r["symbol"],
            vendor_seq=r.get("vendor_seq"),
            ts_event_utc=r["ts_event"],
            ts_recv_utc=r["ts_event"],
            reconnect_epoch=0,
        )
        events.append(TradeEvent(
            provenance=prov,
            price=Decimal(r["price"]),
            size=Decimal(r["size"]),
            side=r["side"],
        ))
    return events


class TestEmptyWindowVWAP(unittest.TestCase):
    """S2: empty-window VWAP raises EmptyWindowVWAP; no NaN/Inf row ever emitted."""

    def test_empty_window_vwap_rejects_naninf(self):
        """D7 (R2#7): the internal VWAP computation RAISES EmptyWindowVWAP when called
        with zero volume/count — the guard is LIVE, not dead. (NaN=0/0, Inf=x/0 impossible.)
        """
        # Zero volume AND zero count -> 0/0 would be NaN; must RAISE instead.
        with self.assertRaises(EmptyWindowVWAP):
            _vwap(Decimal("0"), Decimal("0"), count=0)
        # Zero volume with nonzero px_vol_sum -> x/0 would be Inf; must RAISE instead.
        with self.assertRaises(EmptyWindowVWAP):
            _vwap(Decimal("201.5"), Decimal("0"), count=0)

    def test_empty_window_skipped_by_resample(self):
        """resample SKIPS zero-volume buckets entirely (no fabricated bar)."""
        self.assertEqual(resample([], interval="1m"), [])

    def test_serializer_rejects_nan_decimal(self):
        """Binds serializer.py:42 — dumps raises on non-finite Decimal."""
        with self.assertRaises((ValueError, TypeError)):
            serializer_dumps({"vwap": Decimal("nan")})

    def test_vwap_is_decimal_quantized_half_even(self):
        """vwap is Decimal at VWAP_QUANTUM (4dp), finite, when window non-empty."""
        ev = _make_trade("AAPL", "2026-06-09T13:30:00.000000Z", "201.5000", "100")
        bars = resample([ev], interval="1m")
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        self.assertIsInstance(bar.vwap, Decimal)
        self.assertTrue(bar.vwap.is_finite())
        # VWAP_QUANTUM is 4dp
        self.assertEqual(VWAP_QUANTUM, Decimal("0.0001"))
        # vwap should be quantized to 4dp
        self.assertEqual(bar.vwap, bar.vwap.quantize(VWAP_QUANTUM))

    def test_empty_buckets_are_skipped_not_zero_volume_rows(self):
        """No fabricated zero-volume bars emitted for gaps between events."""
        ev1 = _make_trade("AAPL", "2026-06-09T13:30:00.000000Z", "201.5000", "100", vendor_seq=1)
        ev2 = _make_trade("AAPL", "2026-06-09T13:32:00.000000Z", "202.0000", "50", vendor_seq=2)
        # 13:30 and 13:32 — 13:31 minute bucket is empty
        bars = resample([ev1, ev2], interval="1m")
        self.assertEqual(len(bars), 2)
        for bar in bars:
            self.assertGreater(bar.volume, Decimal("0"))
            self.assertGreater(bar.trade_count, 0)


class TestEtSessionDate(unittest.TestCase):
    """DST / §11 correctness."""

    def test_et_session_date_edt_event(self):
        """2026-06-09T20:00:00Z -> ET date 2026-06-09 (EDT, UTC-4)."""
        self.assertEqual(et_session_date("2026-06-09T20:00:00Z"), "2026-06-09")

    def test_et_session_date_est_event(self):
        """2026-12-09T21:00:00Z -> ET date 2026-12-09 (EST, UTC-5)."""
        self.assertEqual(et_session_date("2026-12-09T21:00:00Z"), "2026-12-09")

    def test_et_session_date_just_before_midnight_edt(self):
        """2026-06-09T23:59:00Z = 19:59 ET (EDT) -> session date 2026-06-09."""
        self.assertEqual(et_session_date("2026-06-09T23:59:00Z"), "2026-06-09")

    def test_et_session_date_just_after_midnight_utc_edt(self):
        """2026-06-10T00:30:00Z = 20:30 ET previous day (EDT) -> session date 2026-06-09."""
        self.assertEqual(et_session_date("2026-06-10T00:30:00Z"), "2026-06-09")

    def test_dst_boundary_fixture_assigns_correct_sessions(self):
        """dst_boundary_events.jsonl: both trades land on correct ET session dates across EDT->EST flip."""
        events = _load_trade_events(FIXTURES_BARS / "dst_boundary_events.jsonl")
        self.assertEqual(len(events), 2)

        # 2026-10-30T19:59:30Z = 15:59:30 ET (EDT, UTC-4) -> session date 2026-10-30
        date0 = et_session_date(events[0].provenance.ts_event_utc)
        self.assertEqual(date0, "2026-10-30")

        # 2026-11-02T14:30:00Z = 09:30 ET (EST, UTC-5) -> session date 2026-11-02
        date1 = et_session_date(events[1].provenance.ts_event_utc)
        self.assertEqual(date1, "2026-11-02")


class TestBucketBoundaries(unittest.TestCase):
    """§11 UTC persistence of bucket boundaries; no 25h day on fall-back."""

    def test_bucket_boundaries_persisted_utc(self):
        """bucket_start_utc and bucket_end_utc are correct on EDT side."""
        ev = _make_trade("AAPL", "2026-06-09T13:30:00.000000Z", "201.5000", "100")
        bars = resample([ev], interval="1m")
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        # 13:30 UTC = 09:30 ET (EDT, UTC-4) -> minute bucket 09:30-09:31 ET
        # = 13:30-13:31 UTC
        self.assertIn("13:30", bar.bucket_start_utc)
        self.assertIn("13:31", bar.bucket_end_utc)

    def test_bucket_boundaries_est_side(self):
        """bucket_start_utc correct on EST side (UTC-5 vs UTC-4 EDT)."""
        # 2026-12-09T14:30:00Z = 09:30 ET (EST, UTC-5) -> bucket 09:30-09:31 ET = 14:30-14:31 UTC
        ev = _make_trade("AAPL", "2026-12-09T14:30:00.000000Z", "201.5000", "100")
        bars = resample([ev], interval="1m")
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        self.assertIn("14:30", bar.bucket_start_utc)
        self.assertIn("14:31", bar.bucket_end_utc)

    def test_session_date_et_is_in_bar(self):
        """Bar.session_date_et is set to the ET calendar date."""
        ev = _make_trade("AAPL", "2026-06-09T13:30:00.000000Z", "201.5000", "100")
        bars = resample([ev], interval="1m")
        self.assertEqual(bars[0].session_date_et, "2026-06-09")


class TestResampleDeterminism(unittest.TestCase):
    """Determinism: shuffled input -> identical bars."""

    def test_resample_deterministic_on_shuffled_input(self):
        """Shuffled event order produces identical bars (input sorted defensively)."""
        events = [
            _make_trade("AAPL", "2026-06-09T13:30:01.000000Z", "201.5000", "100", vendor_seq=1),
            _make_trade("AAPL", "2026-06-09T13:30:05.000000Z", "201.6000", "200", vendor_seq=2),
            _make_trade("AAPL", "2026-06-09T13:30:30.000000Z", "201.4000", "50", vendor_seq=3),
        ]
        bars_ordered = resample(events, interval="1m")

        shuffled = events[:]
        random.shuffle(shuffled)
        bars_shuffled = resample(shuffled, interval="1m")

        self.assertEqual(bars_ordered, bars_shuffled)

    def test_vwap_computed_correctly(self):
        """VWAP = sum(px*sz) / sum(sz), quantized VWAP_QUANTUM ROUND_HALF_EVEN."""
        # 100 @ 200.0000 + 200 @ 201.0000 = 20000 + 40200 = 60200 / 300 = 200.6667 -> 200.6667
        ev1 = _make_trade("AAPL", "2026-06-09T13:30:01.000000Z", "200.0000", "100", vendor_seq=1)
        ev2 = _make_trade("AAPL", "2026-06-09T13:30:02.000000Z", "201.0000", "200", vendor_seq=2)
        bars = resample([ev1, ev2], interval="1m")
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        expected_vwap = (Decimal("200.0000") * 100 + Decimal("201.0000") * 200) / 300
        expected_vwap = expected_vwap.quantize(VWAP_QUANTUM)
        self.assertEqual(bar.vwap, expected_vwap)

    def test_ohlc_correct(self):
        """OHLC computed correctly across trades in a bucket."""
        ev1 = _make_trade("AAPL", "2026-06-09T13:30:01.000000Z", "201.0000", "100", vendor_seq=1)
        ev2 = _make_trade("AAPL", "2026-06-09T13:30:02.000000Z", "202.0000", "100", vendor_seq=2)
        ev3 = _make_trade("AAPL", "2026-06-09T13:30:03.000000Z", "200.5000", "100", vendor_seq=3)
        bars = resample([ev1, ev2, ev3], interval="1m")
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        self.assertEqual(bar.open, Decimal("201.0000"))
        self.assertEqual(bar.high, Decimal("202.0000"))
        self.assertEqual(bar.low, Decimal("200.5000"))
        self.assertEqual(bar.close, Decimal("200.5000"))
        self.assertEqual(bar.volume, Decimal("300"))
        self.assertEqual(bar.trade_count, 3)


class TestOneDayBucketEndDST(unittest.TestCase):
    """C4 (#1/#7): '1d' bucket_end must advance to the TRUE next ET midnight in ET
    wall-clock (then -> UTC), not add a fixed 24h UTC timedelta. There is otherwise
    ZERO '1d' coverage. A fixed-24h-UTC bug is 1h short on fall-back and 1h long on
    spring-forward.
    """

    def test_1d_bucket_end_fall_back_day(self):
        """2026-11-01 (EST begins): next ET midnight = 2026-11-02T00:00 ET = 05:00:00Z.

        A fixed-24h-UTC bug would emit 04:00:00Z (1h short).
        """
        # 2026-11-01T14:30:00Z = 10:30 ET (still EDT, UTC-4, before the 02:00 flip is moot)
        ev = _make_trade("AAPL", "2026-11-01T14:30:00.000000Z", "201.5000", "100")
        bars = resample([ev], interval="1d")
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        # Bucket OPEN = 2026-11-01T00:00 ET = 04:00:00Z (EDT, UTC-4)
        self.assertEqual(bar.bucket_start_utc, "2026-11-01T04:00:00.000000Z")
        # Bucket CLOSE = next ET midnight 2026-11-02T00:00 ET = 05:00:00Z (EST, UTC-5)
        self.assertEqual(bar.bucket_end_utc, "2026-11-02T05:00:00.000000Z")
        self.assertEqual(bar.session_date_et, "2026-11-01")

    def test_1d_bucket_end_spring_forward_day(self):
        """2026-03-08 (EDT begins): next ET midnight = 2026-03-09T00:00 ET = 04:00:00Z.

        A fixed-24h-UTC bug would emit 05:00:00Z (1h long).
        """
        # 2026-03-08T14:30:00Z = 09:30 ET (already EDT, the 02:00->03:00 jump precedes 09:30)
        ev = _make_trade("AAPL", "2026-03-08T14:30:00.000000Z", "201.5000", "100")
        bars = resample([ev], interval="1d")
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        # Bucket OPEN = 2026-03-08T00:00 ET = 05:00:00Z (still EST at midnight, UTC-5)
        self.assertEqual(bar.bucket_start_utc, "2026-03-08T05:00:00.000000Z")
        # Bucket CLOSE = next ET midnight 2026-03-09T00:00 ET = 04:00:00Z (EDT, UTC-4)
        self.assertEqual(bar.bucket_end_utc, "2026-03-09T04:00:00.000000Z")
        self.assertEqual(bar.session_date_et, "2026-03-08")

    def test_1d_bucket_end_normal_day_control(self):
        """Control: a non-transition summer day is a clean 24h span (both ends EDT)."""
        # 2026-06-15T14:30:00Z = 10:30 ET (EDT, UTC-4)
        ev = _make_trade("AAPL", "2026-06-15T14:30:00.000000Z", "201.5000", "100")
        bars = resample([ev], interval="1d")
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        self.assertEqual(bar.bucket_start_utc, "2026-06-15T04:00:00.000000Z")
        self.assertEqual(bar.bucket_end_utc, "2026-06-16T04:00:00.000000Z")
        self.assertEqual(bar.session_date_et, "2026-06-15")


class TestParseUtcOffsetForms(unittest.TestCase):
    """C5 (#4): _parse_utc must accept '+00:00' (numeric offset) ISO-8601, not only
    a literal 'Z' suffix — the repo's own datetime.now(timezone.utc).isoformat()
    emits '+00:00'.
    """

    def test_plus_offset_resamples_identically_to_z(self):
        """A row written with '+00:00' resamples to the SAME bar as the 'Z' form."""
        ev_z = _make_trade("AAPL", "2026-06-09T13:30:00.000000Z", "201.5000", "100")
        ev_offset = _make_trade("AAPL", "2026-06-09T13:30:00.000000+00:00", "201.5000", "100")
        bars_z = resample([ev_z], interval="1m")
        bars_offset = resample([ev_offset], interval="1m")
        self.assertEqual(len(bars_z), 1)
        self.assertEqual(len(bars_offset), 1)
        self.assertEqual(bars_offset[0], bars_z[0])

    def test_et_session_date_accepts_plus_offset(self):
        """et_session_date accepts a '+00:00' UTC instant identically to 'Z'."""
        self.assertEqual(
            et_session_date("2026-06-09T20:00:00+00:00"),
            et_session_date("2026-06-09T20:00:00Z"),
        )

    def test_non_utc_offset_maps_to_correct_instant(self):
        """A non-UTC offset (e.g. -04:00) is normalized to the correct UTC instant."""
        # 2026-06-09T09:30:00-04:00 == 2026-06-09T13:30:00Z
        ev_local = _make_trade("AAPL", "2026-06-09T09:30:00.000000-04:00", "201.5000", "100")
        ev_z = _make_trade("AAPL", "2026-06-09T13:30:00.000000Z", "201.5000", "100")
        self.assertEqual(
            resample([ev_local], interval="1m")[0],
            resample([ev_z], interval="1m")[0],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
