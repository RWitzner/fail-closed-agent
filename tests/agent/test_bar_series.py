"""M3 §M.2 — MidBar resampler + MidBarSeriesReader (§B). [S3]

The S3 core lives here: the FD-2 watermark eligibility predicate, the resolver-facing
MissingBar reasons (via the rev2 BUILD-F1 `missing` seam), and the rev2 SAFETY-F2
mixed-ISO-form discipline (parse-then-compare; never lexicographic).
"""
import unittest
from decimal import Decimal

from agent.bar_series import MidBar, MissingBar, MidBarSeriesReader, resample_midbars

DATASET = "EQUS.MINI"
SCHEMA = "tbbo"
PIN = "EQUS.MINI:tbbo:1m:fixture:test-v1"


def _row(ts_event, ts_recv, bid="100.0000", ask="100.0200", bid_sz="100", ask_sz="100",
         *, symbol="AAPL", instrument_id=1001, schema=SCHEMA, vendor_seq=None):
    return {
        "schema": schema,
        "dataset": DATASET,
        "instrument_id": instrument_id,
        "symbol": symbol,
        "vendor_seq": vendor_seq,
        "ts_event_utc": ts_event,
        "ts_recv_utc": ts_recv,
        "reconnect_epoch": 0,
        "bid_px": bid,
        "bid_sz": bid_sz,
        "ask_px": ask,
        "ask_sz": ask_sz,
    }


def _resample(rows, **kw):
    kwargs = dict(symbol="AAPL", instrument_id=1001, interval="1m",
                  dataset=DATASET, schema=SCHEMA, data_pin=PIN)
    kwargs.update(kw)
    return resample_midbars(rows, **kwargs)


class TestBucketing(unittest.TestCase):
    def test_summer_et_boundary(self):
        # EDT (UTC-4): 09:30 ET == 13:30 UTC on 2026-06-15.
        bars, missing = _resample([
            _row("2026-06-15T13:30:01.000000Z", "2026-06-15T13:30:01.050000Z"),
        ])
        self.assertEqual(missing, [])
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        self.assertEqual(bar.bucket_start_utc, "2026-06-15T13:30:00.000000Z")
        self.assertEqual(bar.bucket_end_utc, "2026-06-15T13:31:00.000000Z")
        self.assertEqual(bar.session_date_et, "2026-06-15")

    def test_winter_et_boundary(self):
        # EST (UTC-5): 09:30 ET == 14:30 UTC on 2026-12-09 (DST-correct bucketing).
        bars, _ = _resample([
            _row("2026-12-09T14:30:30.000000Z", "2026-12-09T14:30:30.100000Z"),
        ])
        self.assertEqual(bars[0].bucket_start_utc, "2026-12-09T14:30:00.000000Z")
        self.assertEqual(bars[0].bucket_end_utc, "2026-12-09T14:31:00.000000Z")
        self.assertEqual(bars[0].session_date_et, "2026-12-09")

    def test_event_time_buckets_receipt_time_only_watermarks(self):
        bars, _ = _resample([
            _row("2026-06-15T13:30:59.000000Z", "2026-06-15T13:35:00.000000Z"),
        ])
        self.assertEqual(bars[0].bucket_end_utc, "2026-06-15T13:31:00.000000Z")
        self.assertEqual(bars[0].watermark_utc, "2026-06-15T13:35:00.000000Z")

    def test_unsupported_interval_raises(self):
        with self.assertRaises(ValueError):
            _resample([], interval="5m")

    def test_dst_fall_back_fold_yields_two_distinct_buckets(self):
        # harden round 1, M3-EDGE-1: 2026-11-01 01:30 EDT (05:30Z) and 01:30 EST
        # (06:30Z) share the ET wall-clock but are DIFFERENT instants — PEP 495
        # ignores `fold` in aware-datetime equality, so ET-keyed buckets would
        # collide. UTC-instant keys must keep them apart.
        bars, missing = _resample([
            _row("2026-11-01T05:30:10.000000Z", "2026-11-01T05:30:10.100000Z",
                 bid="100.0000", ask="100.0200"),
            _row("2026-11-01T06:30:10.000000Z", "2026-11-01T06:30:10.100000Z",
                 bid="200.0000", ask="200.0200"),
        ])
        self.assertEqual(missing, [])
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].bucket_start_utc, "2026-11-01T05:30:00.000000Z")
        self.assertEqual(bars[1].bucket_start_utc, "2026-11-01T06:30:00.000000Z")
        self.assertEqual(bars[0].bid, Decimal("100.0000"))
        self.assertEqual(bars[1].bid, Decimal("200.0000"))
        self.assertEqual(bars[0].session_date_et, "2026-11-01")
        self.assertEqual(bars[1].session_date_et, "2026-11-01")


class TestValidityAndSelection(unittest.TestCase):
    def test_locked_quote_is_a_valid_label(self):
        bars, missing = _resample([
            _row("2026-06-15T13:30:01.000000Z", "2026-06-15T13:30:01.000000Z",
                 bid="100.0000", ask="100.0000"),
        ])
        self.assertEqual(missing, [])
        self.assertEqual(bars[0].mid, Decimal("100.000000"))

    def test_crossed_and_zero_are_invalid_quotes_only(self):
        bars, missing = _resample([
            _row("2026-06-15T13:30:01.000000Z", "2026-06-15T13:30:01.000000Z",
                 bid="100.0200", ask="100.0000"),                       # crossed
            _row("2026-06-15T13:30:02.000000Z", "2026-06-15T13:30:02.000000Z",
                 bid="0.0000"),                                          # zero bid
        ])
        self.assertEqual(bars, [])
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].reason, "invalid_quotes_only")
        self.assertEqual(missing[0].bucket_end_utc, "2026-06-15T13:31:00.000000Z")

    def test_last_valid_quote_wins_with_vendor_seq_tiebreak(self):
        bars, _ = _resample([
            _row("2026-06-15T13:30:05.000000Z", "2026-06-15T13:30:05.000000Z",
                 bid="100.0000", ask="100.0200", vendor_seq=7),
            _row("2026-06-15T13:30:05.000000Z", "2026-06-15T13:30:05.100000Z",
                 bid="101.0000", ask="101.0200", vendor_seq=9),         # same event ts, higher seq
            _row("2026-06-15T13:30:04.000000Z", "2026-06-15T13:30:06.000000Z",
                 bid="102.0000", ask="102.0200", vendor_seq=11),        # earlier event ts
        ])
        bar = bars[0]
        self.assertEqual(bar.bid, Decimal("101.0000"))
        self.assertEqual(bar.watermark_utc, "2026-06-15T13:30:05.100000Z")
        self.assertEqual(bar.quote_provenance["vendor_seq"], 9)

    def test_input_order_breaks_full_ties(self):
        bars, _ = _resample([
            _row("2026-06-15T13:30:05.000000Z", "2026-06-15T13:30:05.000000Z",
                 bid="100.0000", ask="100.0200"),
            _row("2026-06-15T13:30:05.000000Z", "2026-06-15T13:30:05.000000Z",
                 bid="103.0000", ask="103.0200"),
        ])
        self.assertEqual(bars[0].bid, Decimal("103.0000"))

    def test_mixed_stream_rows_filtered(self):
        bars, missing = _resample([
            _row("2026-06-15T13:30:01.000000Z", "2026-06-15T13:30:01.000000Z"),
            _row("2026-06-15T13:30:02.000000Z", "2026-06-15T13:30:02.000000Z",
                 symbol="MSFT", instrument_id=2002, bid="999.0000", ask="999.0200"),
            _row("2026-06-15T13:30:03.000000Z", "2026-06-15T13:30:03.000000Z",
                 schema="trades", bid="1.0000", ask="1.0200"),
        ])
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].bid, Decimal("100.0000"))

    def test_missing_px_field_raises(self):
        row = _row("2026-06-15T13:30:01.000000Z", "2026-06-15T13:30:01.000000Z")
        del row["bid_px"]
        with self.assertRaises(ValueError):
            _resample([row])

    def test_none_side_raises(self):
        with self.assertRaises(ValueError):
            _resample([_row("2026-06-15T13:30:01.000000Z",
                            "2026-06-15T13:30:01.000000Z", bid=None)])

    def test_unparseable_decimal_raises(self):
        with self.assertRaises(ValueError):
            _resample([_row("2026-06-15T13:30:01.000000Z",
                            "2026-06-15T13:30:01.000000Z", bid="garbage")])


def _bar(bucket_start, bucket_end, watermark, *, symbol="AAPL", instrument_id=1001,
         bid="100.0000", ask="100.0200"):
    bid_d, ask_d = Decimal(bid), Decimal(ask)
    return MidBar(
        symbol=symbol, instrument_id=instrument_id, interval="1m",
        bucket_start_utc=bucket_start, bucket_end_utc=bucket_end,
        session_date_et="2026-06-15", bid=bid_d, ask=ask_d,
        mid=((bid_d + ask_d) / 2).quantize(Decimal("0.000001")),
        watermark_utc=watermark, source_dataset=DATASET, source_schema=SCHEMA,
        data_pin=PIN,
        quote_provenance={"ts_event_utc": bucket_start, "ts_recv_utc": watermark,
                          "reconnect_epoch": 0, "vendor_seq": None},
    )


class TestReader(unittest.TestCase):
    def setUp(self):
        self.bars = [
            _bar("2026-06-15T13:30:00.000000Z", "2026-06-15T13:31:00.000000Z",
                 "2026-06-15T13:30:59.000000Z"),
            _bar("2026-06-15T13:31:00.000000Z", "2026-06-15T13:32:00.000000Z",
                 "2026-06-15T13:31:58.000000Z"),
            # 13:34->13:35 bucket: future-received (watermark long after bucket end);
            # 13:33 is `missing` (invalid_quotes_only) and 13:34 is a true gap.
            _bar("2026-06-15T13:34:00.000000Z", "2026-06-15T13:35:00.000000Z",
                 "2026-06-15T13:41:00.000000Z"),
        ]
        self.missing = [
            MissingBar(symbol="AAPL", instrument_id=1001, interval="1m",
                       bucket_end_utc="2026-06-15T13:33:00.000000Z",
                       reason="invalid_quotes_only"),
        ]
        self.reader = MidBarSeriesReader(self.bars, self.missing)

    def test_get_present_bar(self):
        bar = self.reader.get("AAPL", 1001, "2026-06-15T13:31:00.000000Z")
        self.assertIsInstance(bar, MidBar)

    def test_get_invalid_quotes_only(self):
        result = self.reader.get("AAPL", 1001, "2026-06-15T13:33:00.000000Z")
        self.assertIsInstance(result, MissingBar)
        self.assertEqual(result.reason, "invalid_quotes_only")

    def test_get_no_quotes_in_bucket_inside_coverage(self):
        result = self.reader.get("AAPL", 1001, "2026-06-15T13:34:00.000000Z")
        self.assertIsInstance(result, MissingBar)
        self.assertEqual(result.reason, "no_quotes_in_bucket")

    def test_get_out_of_series_outside_coverage(self):
        before = self.reader.get("AAPL", 1001, "2026-06-15T13:00:00.000000Z")
        after = self.reader.get("AAPL", 1001, "2026-06-15T15:00:00.000000Z")
        unknown = self.reader.get("MSFT", 2002, "2026-06-15T13:31:00.000000Z")
        for result in (before, after, unknown):
            self.assertIsInstance(result, MissingBar)
            self.assertEqual(result.reason, "out_of_series")

    def test_s3_future_receipt(self):
        # bucket_end 13:35 <= as_of 13:40 BUT watermark 13:41 > as_of -> future_receipt.
        result = self.reader.get("AAPL", 1001, "2026-06-15T13:35:00.000000Z",
                                 as_of_utc="2026-06-15T13:40:00.000000Z")
        self.assertIsInstance(result, MissingBar)
        self.assertEqual(result.reason, "future_receipt")
        # at 13:41 (>= watermark) it becomes eligible.
        bar = self.reader.get("AAPL", 1001, "2026-06-15T13:35:00.000000Z",
                              as_of_utc="2026-06-15T13:41:00.000000Z")
        self.assertIsInstance(bar, MidBar)

    def test_s3_in_progress_bucket_is_future_receipt(self):
        result = self.reader.get("AAPL", 1001, "2026-06-15T13:32:00.000000Z",
                                 as_of_utc="2026-06-15T13:31:30.000000Z")
        # absent bucket: still classified by record absence first
        self.assertIsInstance(result, MissingBar)
        bar = self.reader.get("AAPL", 1001, "2026-06-15T13:31:00.000000Z",
                              as_of_utc="2026-06-15T13:30:30.000000Z")
        self.assertIsInstance(bar, MissingBar)
        self.assertEqual(bar.reason, "future_receipt")

    def test_mixed_iso_forms_compare_as_instants(self):
        # rev2 SAFETY-F2: watermark with fractional seconds vs as_of WITHOUT them.
        # Lexicographic would pass ('.' < 'Z'); parsed-instant must reject.
        bars = [_bar("2026-06-15T13:30:00.000000Z", "2026-06-15T13:31:00.000000Z",
                     "2026-06-15T13:31:00.500000Z")]
        reader = MidBarSeriesReader(bars)
        result = reader.get("AAPL", 1001, "2026-06-15T13:31:00.000000Z",
                            as_of_utc="2026-06-15T13:31:00Z")  # whole-second form
        self.assertIsInstance(result, MissingBar)
        self.assertEqual(result.reason, "future_receipt")
        # equal instants in different surface forms compare equal:
        bar = reader.get("AAPL", 1001, "2026-06-15T13:31:00Z",
                         as_of_utc="2026-06-15T13:31:00.500000Z")
        self.assertIsInstance(bar, MidBar)

    def test_duplicate_bucket_raises(self):
        with self.assertRaises(ValueError):
            MidBarSeriesReader(self.bars + [self.bars[-1]])

    def test_out_of_order_per_key_raises(self):
        with self.assertRaises(ValueError):
            MidBarSeriesReader(list(reversed(self.bars)))

    def test_cross_key_interleaving_permitted(self):
        other = _bar("2026-06-15T13:29:00.000000Z", "2026-06-15T13:30:00.000000Z",
                     "2026-06-15T13:29:59.000000Z", symbol="MSFT", instrument_id=2002)
        MidBarSeriesReader([self.bars[0], other, self.bars[1], self.bars[2]])

    def test_latest_eligible_and_history(self):
        as_of = "2026-06-15T13:35:00.000000Z"
        latest = self.reader.latest_eligible("AAPL", 1001, as_of_utc=as_of)
        # 13:34 bar has watermark 13:41 -> ineligible; latest eligible ends 13:32.
        self.assertEqual(latest.bucket_end_utc, "2026-06-15T13:32:00.000000Z")
        history = self.reader.eligible_history("AAPL", 1001, as_of_utc=as_of, max_bars=10)
        self.assertEqual([b.bucket_end_utc for b in history],
                         ["2026-06-15T13:31:00.000000Z", "2026-06-15T13:32:00.000000Z"])
        capped = self.reader.eligible_history("AAPL", 1001, as_of_utc=as_of, max_bars=1)
        self.assertEqual([b.bucket_end_utc for b in capped],
                         ["2026-06-15T13:32:00.000000Z"])

    def test_resampler_feeds_reader_end_to_end(self):
        rows = [
            _row("2026-06-15T13:30:01.000000Z", "2026-06-15T13:30:01.000000Z"),
            _row("2026-06-15T13:31:05.000000Z", "2026-06-15T13:31:05.000000Z",
                 bid="100.0200", ask="100.0000"),  # crossed only -> invalid bucket
        ]
        bars, missing = _resample(rows)
        reader = MidBarSeriesReader(bars, missing)
        self.assertIsInstance(
            reader.get("AAPL", 1001, "2026-06-15T13:31:00.000000Z"), MidBar)
        result = reader.get("AAPL", 1001, "2026-06-15T13:32:00.000000Z")
        self.assertEqual(result.reason, "invalid_quotes_only")


class TestEligibleHistoryIndexIdentity(unittest.TestCase):
    """eligible_history is served from a per-key precomputed ascending index
    (bisect the end<=as_of cut, walk backwards under the watermark predicate):
    the historical runners call it per (decision × symbol) and a per-call sort
    + per-bar timestamp parse is O(B²) per symbol. This pins exact identity
    against the naive §B definition over a grid of as_of instants, max_bars
    values, and keys (incl. late/skewed watermarks and an unknown key)."""

    def test_matches_naive_definition_over_grid(self):
        from agent.bar_series import _parse_utc

        rows = []
        # buckets 13:30..13:37; every second bucket's watermark lands LATE
        # (two minutes after its bucket) so eligibility ≠ bucket order.
        for i in range(8):
            ts_event = f"2026-06-15T13:3{i}:10.000000Z"
            if i % 2 == 1 and i + 2 <= 9:
                ts_recv = f"2026-06-15T13:3{i + 2}:30.000000Z"
            else:
                ts_recv = f"2026-06-15T13:3{i}:20.000000Z"
            rows.append(_row(ts_event, ts_recv))
        msft_rows = [_row("2026-06-15T13:31:05.000000Z",
                          "2026-06-15T13:31:06.000000Z",
                          symbol="MSFT", instrument_id=2002)]
        bars_a, missing_a = _resample(rows)
        bars_m, _ = _resample(msft_rows, symbol="MSFT", instrument_id=2002)
        all_bars = list(bars_a) + list(bars_m)
        reader = MidBarSeriesReader(all_bars, missing_a)

        def naive(symbol, instrument_id, as_of_utc, max_bars):
            as_of = _parse_utc(as_of_utc)
            per = [b for b in all_bars
                   if (b.symbol, b.instrument_id) == (symbol, instrument_id)]
            eligible = [
                b for b in sorted(per,
                                  key=lambda b: _parse_utc(b.bucket_end_utc))
                if _parse_utc(b.bucket_end_utc) <= as_of
                and _parse_utc(b.watermark_utc) <= as_of]
            if max_bars <= 0:
                return ()
            return tuple(eligible[-max_bars:])

        instants = [f"2026-06-15T13:{mm}:{ss:02d}.000000Z"
                    for mm in range(29, 42) for ss in (0, 15, 45)]
        for as_of in instants:
            for max_bars in (0, 1, 2, 3, 51):
                for symbol, instrument_id in (("AAPL", 1001), ("MSFT", 2002),
                                              ("NVDA", 3003)):
                    self.assertEqual(
                        reader.eligible_history(symbol, instrument_id,
                                                as_of_utc=as_of,
                                                max_bars=max_bars),
                        naive(symbol, instrument_id, as_of, max_bars),
                        msg=f"{symbol} as_of={as_of} max_bars={max_bars}")


if __name__ == "__main__":
    unittest.main()
