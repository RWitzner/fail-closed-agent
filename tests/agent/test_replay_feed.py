"""M5 §N / §R 16 (replay parts) — ReplayQuoteFeed + ReplayClock.

Covers the replay_feed cases of §R 16: field mapping events.jsonl -> QuoteSnapshot
byte-exact; ReplayClock determinism; THE MIXED-FORM CASE (whole-second + .%f
``ts_recv_utc`` => identical offsets — the ``bar_series._parse_utc`` chokepoint,
EX-5); RC-5 sparse-stream catch-up (ONE on_tick per gap, strictly-increasing
now_ms); symbol intersection (FD-M5-4); depth rows -> DepthSnapshot with the
recorded book_hash; bar preload == incremental resample (anti-lookahead preserved
by as-of reads); truncated-tail tolerated / corrupt complete line fatal (S3,
inherited from replay_stream).

The observe E2E case of §R 16 is a LATER wave (test_observe_e2e.py) and is NOT
here. Fixture files are built with the REAL ``recorder.persistence.EventWriter``
(hash-stamped journal envelope) + the REAL ``recorder.event.parse`` chokepoint —
never hand-rolled JSON.
"""
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agent.bar_series import MidBar, MissingBar, resample_midbars
from agent.execution_realism import DepthView
from agent.marketdata import replay_feed as replay_feed_module
from agent.marketdata.replay_feed import ReplayQuoteFeed
from agent.quote_quality import QuoteSnapshot
from agent.strategies.calibration_probe import QuoteView
from recorder.book_hash import book_hash
from recorder.book_state import EquityBookState
from recorder.event import parse
from recorder.persistence import EventWriter, JournalCorruption, replay_stream

DATASET = "EQUS.MINI"
SYMBOL = "AAPL"
IID = 1001
WRITER_TS = "2026-06-10T00:00:00+00:00"   # injected row clock (deterministic)


def _quote_record(*, symbol=SYMBOL, iid=IID, vendor_seq, ts_event, schema="tbbo",
                  bid="201.1500", bid_sz="300", ask="201.1600", ask_sz="200"):
    return {"dataset": DATASET, "schema": schema, "instrument_id": iid,
            "symbol": symbol, "vendor_seq": vendor_seq, "ts_event": ts_event,
            "bid_px": bid, "bid_sz": bid_sz, "ask_px": ask, "ask_sz": ask_sz}


def _trade_record(*, vendor_seq, ts_event):
    return {"dataset": DATASET, "schema": "trades", "instrument_id": IID,
            "symbol": SYMBOL, "vendor_seq": vendor_seq, "ts_event": ts_event,
            "price": "201.5000", "size": "100", "side": "B"}


def _depth_record(*, vendor_seq, ts_event):
    return {"dataset": DATASET, "schema": "mbp-10", "instrument_id": IID,
            "symbol": SYMBOL, "vendor_seq": vendor_seq, "ts_event": ts_event,
            "bids": [["201.1500", "300", 2], ["201.1400", "500", 4]],
            "asks": [["201.1600", "200", 1], ["201.1700", "400", 3]]}


def _write_stream(path, items, *, run_id="run-replay-test",
                  depth_hash_override=None):
    """Build a REAL recorder events.jsonl: parse() -> EventWriter.write_event.

    ``items`` = sequence of (record, ts_recv_utc[, reconnect_epoch]) tuples.
    Depth rows get derived_book_hash from the REAL book pipeline (apply ->
    snapshot -> book_hash) unless ``depth_hash_override`` is given.
    """
    writer = EventWriter(path, run_id=run_id, clock=lambda: WRITER_TS)
    books = {}
    rows = []
    for item in items:
        record, ts_recv = item[0], item[1]
        epoch = item[2] if len(item) > 2 else 0
        ev = parse(record, dataset=record["dataset"], schema=record["schema"],
                   reconnect_epoch=epoch, ts_recv_utc=ts_recv)
        if record["schema"] == "mbp-10":
            key = (ev.provenance.symbol, ev.provenance.instrument_id)
            book = books.setdefault(key, EquityBookState(*key))
            book.apply(ev)
            derived = (depth_hash_override if depth_hash_override is not None
                       else book_hash(book.snapshot()))
            rows.append(writer.write_event(ev, derived_book_hash=derived))
        else:
            rows.append(writer.write_event(ev))
    return rows


def _collecting_callbacks():
    ticks, bars = [], []
    return ticks, bars, (lambda now_ms: ticks.append(now_ms)), bars.append


class _Tmp(unittest.TestCase):
    """Fresh temp dir per test (the path-keyed journal seq registry must not
    bleed across tests — mirrors tests/recorder/test_persistence.py:_Tmp)."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.path = self.root / "events.jsonl"

    def tearDown(self):
        self._dir.cleanup()


class TestQuoteFieldMapping(_Tmp):
    def test_quote_row_maps_byte_exact_to_quote_snapshot(self):
        # tbbo + bbo-1s both map; a trades row is skipped (never delivered,
        # never advances the clock); provenance rides VERBATIM.
        _write_stream(self.path, [
            (_quote_record(vendor_seq=11, ts_event="2026-06-09T13:30:00.100000Z"),
             "2026-06-09T13:30:00.200000Z"),
            (_trade_record(vendor_seq=12, ts_event="2026-06-09T13:30:00.300000Z"),
             "2026-06-09T13:30:00.900000Z"),
            (_quote_record(vendor_seq=13, ts_event="2026-06-09T13:30:01.100000Z",
                           schema="bbo-1s", bid="201.2000", bid_sz="250",
                           ask="201.2100", ask_sz="150"),
             "2026-06-09T13:30:01.450000Z", 3),
        ])
        feed = ReplayQuoteFeed(self.path)
        _, _, on_tick, on_bar = _collecting_callbacks()
        feed.run(on_tick=on_tick, on_bar_complete=on_bar)

        expected = QuoteSnapshot(
            symbol=SYMBOL, instrument_id=IID,
            bid=Decimal("201.2000"), ask=Decimal("201.2100"),
            bid_sz=Decimal("250"), ask_sz=Decimal("150"),
            ts_event_utc="2026-06-09T13:30:01.100000Z",
            ts_recv_utc="2026-06-09T13:30:01.450000Z",
            seen_at_ms=1250,                 # offset of ts_recv from stream start
            reconnect_epoch=3, vendor_seq=13,
            dataset=DATASET, schema="bbo-1s",
        )
        self.assertEqual(feed.quote_view().latest(SYMBOL, IID), expected)
        # The trade row did not advance the clock; the last DELIVERED event did.
        self.assertEqual(feed.clock().now_ms(), 1250)

    def test_quote_view_satisfies_m3_quote_view_protocol(self):
        _write_stream(self.path, [
            (_quote_record(vendor_seq=1, ts_event="2026-06-09T13:30:00.000000Z"),
             "2026-06-09T13:30:00.100000Z"),
        ])
        feed = ReplayQuoteFeed(self.path)
        self.assertIsInstance(feed.quote_view(), QuoteView)
        self.assertIsNone(feed.quote_view().latest("MSFT", 2002))


class TestReplayClock(_Tmp):
    def _items(self):
        return [
            (_quote_record(vendor_seq=1, ts_event="2026-06-09T13:30:00.000000Z"),
             "2026-06-09T13:30:00.000000Z"),
            (_quote_record(vendor_seq=2, ts_event="2026-06-09T13:30:00.700000Z"),
             "2026-06-09T13:30:00.750000Z"),
            (_quote_record(vendor_seq=3, ts_event="2026-06-09T13:30:02.000000Z"),
             "2026-06-09T13:30:02.250000Z"),
        ]

    def test_clock_is_zero_before_first_delivery(self):
        _write_stream(self.path, self._items())
        feed = ReplayQuoteFeed(self.path)
        self.assertEqual(feed.clock().now_ms(), 0)

    def test_clock_offsets_deterministic_across_instances(self):
        _write_stream(self.path, self._items())
        results = []
        for _ in range(2):
            feed = ReplayQuoteFeed(self.path, refresh_cadence_ms=1)
            ticks, _, on_tick, on_bar = _collecting_callbacks()
            feed.run(on_tick=on_tick, on_bar_complete=on_bar)
            results.append((tuple(ticks), feed.clock().now_ms(),
                            feed.quote_view().latest(SYMBOL, IID)))
        self.assertEqual(results[0], results[1])
        # cadence=1: every delivered event past offset 0 fires exactly one tick.
        self.assertEqual(results[0][0], (750, 2250))
        self.assertEqual(results[0][1], 2250)

    def test_receipt_order_regression_raises(self):
        _write_stream(self.path, [
            (_quote_record(vendor_seq=1, ts_event="2026-06-09T13:30:01.000000Z"),
             "2026-06-09T13:30:01.000000Z"),
            (_quote_record(vendor_seq=2, ts_event="2026-06-09T13:30:00.000000Z"),
             "2026-06-09T13:30:00.500000Z"),   # recv BEFORE the previous row
        ])
        feed = ReplayQuoteFeed(self.path)
        _, _, on_tick, on_bar = _collecting_callbacks()
        with self.assertRaises(ValueError):
            feed.run(on_tick=on_tick, on_bar_complete=on_bar)


class TestMixedFormOffsets(_Tmp):
    """EX-5: the repo provably mixes whole-second and .%f ISO forms; ALL parsing
    must ride bar_series._parse_utc, so offsets are identical either way."""

    EVENTS = ["2026-06-09T13:30:00.000000Z", "2026-06-09T13:30:00.300000Z",
              "2026-06-09T13:30:00.900000Z", "2026-06-09T13:30:01.600000Z"]
    RECV_MIXED = ["2026-06-09T13:30:00Z",          # whole-second #1
                  "2026-06-09T13:30:00.400000Z",
                  "2026-06-09T13:30:01Z",          # whole-second #2 (EX-5: >= 2)
                  "2026-06-09T13:30:01.750000Z"]
    RECV_CANON = ["2026-06-09T13:30:00.000000Z", "2026-06-09T13:30:00.400000Z",
                  "2026-06-09T13:30:01.000000Z", "2026-06-09T13:30:01.750000Z"]

    def _run(self, path, recvs):
        _write_stream(path, [
            (_quote_record(vendor_seq=i + 1, ts_event=self.EVENTS[i]), recvs[i])
            for i in range(len(recvs))
        ])
        feed = ReplayQuoteFeed(path, refresh_cadence_ms=1)
        ticks, _, on_tick, on_bar = _collecting_callbacks()
        feed.run(on_tick=on_tick, on_bar_complete=on_bar)
        latest = feed.quote_view().latest(SYMBOL, IID)
        return tuple(ticks), feed.clock().now_ms(), latest.seen_at_ms

    def test_mixed_form_offsets_identical_to_all_fractional_equivalent(self):
        mixed = self._run(self.root / "mixed.jsonl", self.RECV_MIXED)
        canon = self._run(self.root / "canon.jsonl", self.RECV_CANON)
        self.assertEqual(mixed, canon)
        self.assertEqual(mixed[0], (400, 1000, 1750))   # per-event offsets
        self.assertEqual(mixed[1], 1750)
        self.assertEqual(mixed[2], 1750)

    def test_module_has_no_local_timestamp_parser(self):
        # Structural EX-5 pin: NO local strptime/fromisoformat CODE — every parse
        # goes through the bar_series._parse_utc chokepoint (AST scan; docstring
        # prose naming the forbidden tokens is fine, code references are not).
        import ast
        source = Path(replay_feed_module.__file__).read_text(encoding="utf-8")
        forbidden = {"strptime", "fromisoformat"}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, forbidden)
            elif isinstance(node, ast.Name):
                self.assertNotIn(node.id, forbidden)


class TestCatchUpRule(_Tmp):
    """RC-5: at most ONE on_tick per delivered-event batch; now_ms strictly
    increases between consecutive ticks (per-tick close ids key on it, §M.7)."""

    def test_gap_spanning_multiple_boundaries_fires_once(self):
        recvs = ["2026-06-09T13:30:00.000000Z",   # offset 0
                 "2026-06-09T13:30:00.500000Z",   # 500
                 "2026-06-09T13:30:01.000000Z",   # 1000 -> tick
                 "2026-06-09T13:30:01.000000Z",   # 1000 again (same batch time)
                 "2026-06-09T13:30:05.500000Z",   # 5500: spans 2000..5000 -> ONE tick
                 "2026-06-09T13:30:06.500000Z"]   # 6500 -> tick
        events = ["2026-06-09T13:30:00.000000Z", "2026-06-09T13:30:00.400000Z",
                  "2026-06-09T13:30:00.900000Z", "2026-06-09T13:30:00.950000Z",
                  "2026-06-09T13:30:05.400000Z", "2026-06-09T13:30:06.400000Z"]
        _write_stream(self.path, [
            (_quote_record(vendor_seq=i + 1, ts_event=events[i]), recvs[i])
            for i in range(len(recvs))
        ])
        feed = ReplayQuoteFeed(self.path, refresh_cadence_ms=1000)
        clock = feed.clock()
        ticks = []

        def on_tick(now_ms):
            self.assertEqual(now_ms, clock.now_ms())   # tick sees the clock's now
            ticks.append(now_ms)

        feed.run(on_tick=on_tick, on_bar_complete=lambda bar: None)
        self.assertEqual(ticks, [1000, 5500, 6500])
        # Strictly increasing — two ticks never share a now_ms.
        self.assertTrue(all(b > a for a, b in zip(ticks, ticks[1:])))


class TestSymbolIntersection(_Tmp):
    def _items(self):
        msft = lambda seq, ev: _quote_record(symbol="MSFT", iid=2002,
                                             vendor_seq=seq, ts_event=ev)
        return [
            (_quote_record(vendor_seq=1, ts_event="2026-06-09T13:30:00.000000Z"),
             "2026-06-09T13:30:00.000000Z"),
            (msft(2, "2026-06-09T13:30:00.400000Z"), "2026-06-09T13:30:00.500000Z"),
            (_quote_record(vendor_seq=3, ts_event="2026-06-09T13:30:01.000000Z"),
             "2026-06-09T13:30:01.000000Z"),
            (msft(4, "2026-06-09T13:30:02.400000Z"), "2026-06-09T13:30:02.500000Z"),
        ]

    def test_filter_is_file_symbols_intersect_given_set(self):
        _write_stream(self.path, self._items())
        feed = ReplayQuoteFeed(self.path, symbols=["MSFT", "TSLA"],
                               refresh_cadence_ms=1000)
        self.assertEqual(feed.symbols(), ("MSFT",))   # TSLA not in file
        ticks, _, on_tick, on_bar = _collecting_callbacks()
        feed.run(on_tick=on_tick, on_bar_complete=on_bar)
        # AAPL rows were never delivered; the clock base is the first MSFT row.
        self.assertIsNone(feed.quote_view().latest(SYMBOL, IID))
        msft = feed.quote_view().latest("MSFT", 2002)
        self.assertEqual(msft.seen_at_ms, 2000)
        self.assertEqual(feed.clock().now_ms(), 2000)
        self.assertEqual(ticks, [2000])

    def test_symbols_none_delivers_every_file_symbol(self):
        _write_stream(self.path, self._items())
        feed = ReplayQuoteFeed(self.path)
        self.assertEqual(feed.symbols(), ("AAPL", "MSFT"))
        _, _, on_tick, on_bar = _collecting_callbacks()
        feed.run(on_tick=on_tick, on_bar_complete=on_bar)
        self.assertIsNotNone(feed.quote_view().latest(SYMBOL, IID))
        self.assertEqual(feed.clock().now_ms(), 2500)


class TestDepthRows(_Tmp):
    def _write_depth(self, **kwargs):
        return _write_stream(self.path, [
            (_quote_record(vendor_seq=1, ts_event="2026-06-09T13:30:00.000000Z"),
             "2026-06-09T13:30:00.000000Z"),
            (_depth_record(vendor_seq=2, ts_event="2026-06-09T13:30:00.200000Z"),
             "2026-06-09T13:30:00.300000Z"),
        ], **kwargs)

    def test_depth_row_builds_snapshot_with_recorded_book_hash(self):
        rows = self._write_depth()
        recorded = rows[1]["derived_book_hash"]
        feed = ReplayQuoteFeed(self.path)
        _, _, on_tick, on_bar = _collecting_callbacks()
        feed.run(on_tick=on_tick, on_bar_complete=on_bar)
        snap = feed.depth_view().latest_book(SYMBOL, IID)
        self.assertIsNotNone(snap)
        self.assertEqual(snap.book_hash, recorded)   # the RECORDED hash, verbatim
        self.assertEqual(snap.bids, ((Decimal("201.1500"), Decimal("300")),
                                     (Decimal("201.1400"), Decimal("500"))))
        self.assertEqual(snap.asks, ((Decimal("201.1600"), Decimal("200")),
                                     (Decimal("201.1700"), Decimal("400"))))
        self.assertEqual(snap.seen_at_ms, 300)
        self.assertEqual(snap.schema, "mbp-10")
        self.assertEqual(snap.dataset, DATASET)
        self.assertEqual(snap.reconnect_epoch, 0)

    def test_tampered_recorded_book_hash_raises(self):
        # Internally-consistent journal, but the recorded derived_book_hash does
        # not match the re-derived ladder hash -> fail-loud (dual-hash posture).
        self._write_depth(depth_hash_override="0" * 64)
        feed = ReplayQuoteFeed(self.path)
        _, _, on_tick, on_bar = _collecting_callbacks()
        with self.assertRaises(ValueError):
            feed.run(on_tick=on_tick, on_bar_complete=on_bar)

    def test_null_recorded_hash_falls_back_to_rederived(self):
        # A row whose derived_book_hash persisted as null (the writer's default)
        # gets the re-derived hash — recorded-when-present, derived-when-absent.
        path2 = self.root / "events_null_hash.jsonl"
        writer = EventWriter(path2, run_id="run-replay-test",
                             clock=lambda: WRITER_TS)
        record = _depth_record(vendor_seq=2,
                               ts_event="2026-06-09T13:30:00.200000Z")
        ev = parse(record, dataset=DATASET, schema="mbp-10", reconnect_epoch=0,
                   ts_recv_utc="2026-06-09T13:30:00.300000Z")
        writer.write_event(ev)   # derived_book_hash persists as null
        book = EquityBookState(SYMBOL, IID)
        book.apply(ev)
        expected_hash = book_hash(book.snapshot())

        feed = ReplayQuoteFeed(path2)
        _, _, on_tick, on_bar = _collecting_callbacks()
        feed.run(on_tick=on_tick, on_bar_complete=on_bar)
        self.assertEqual(feed.depth_view().latest_book(SYMBOL, IID).book_hash,
                         expected_hash)

    def test_depth_view_satisfies_depth_view_protocol(self):
        self._write_depth()
        feed = ReplayQuoteFeed(self.path)
        self.assertIsInstance(feed.depth_view(), DepthView)
        self.assertIsNone(feed.depth_view().latest_book("MSFT", 2002))


class TestBarPreload(_Tmp):
    """§N: bars preloaded ONCE via resample_midbars; equivalence with a direct
    resample; FD-2 as-of reads cannot see the future (the M3 watermark gate)."""

    ITEMS = [
        ("2026-06-09T13:30:10.000000Z", "2026-06-09T13:30:10.500000Z",
         "201.0000", "201.0200"),
        ("2026-06-09T13:30:50.000000Z", "2026-06-09T13:30:50.500000Z",
         "201.1000", "201.1200"),
        ("2026-06-09T13:31:20.000000Z", "2026-06-09T13:31:20.500000Z",
         "201.2000", "201.2200"),
        ("2026-06-09T13:32:40.000000Z", "2026-06-09T13:32:40.500000Z",
         "201.3000", "201.3200"),
        ("2026-06-09T13:33:30.000000Z", "2026-06-09T13:33:30.500000Z",
         "201.4000", "201.4200"),
    ]

    def _write(self):
        _write_stream(self.path, [
            (_quote_record(vendor_seq=i + 1, ts_event=ev, bid=bid, ask=ask),
             recv)
            for i, (ev, recv, bid, ask) in enumerate(self.ITEMS)
        ])

    def _direct_bars(self):
        data_pin = f"{DATASET}:tbbo:1m:replay:{self.path.name}"
        return resample_midbars(
            replay_stream(self.path), symbol=SYMBOL, instrument_id=IID,
            interval="1m", dataset=DATASET, schema="tbbo", data_pin=data_pin)

    def test_preload_equals_direct_resample_and_fires_in_recorded_order(self):
        self._write()
        expected_bars, expected_missing = self._direct_bars()
        self.assertEqual(len(expected_bars), 4)   # 13:31, 13:32, 13:33, 13:34 ends
        self.assertEqual(expected_missing, [])

        feed = ReplayQuoteFeed(self.path, refresh_cadence_ms=60_000)
        _, fired, on_tick, on_bar = _collecting_callbacks()
        feed.run(on_tick=on_tick, on_bar_complete=on_bar)
        # Only buckets whose end AND watermark passed in recorded time complete;
        # the 13:33 bucket's end (13:34) postdates the last recorded receipt.
        self.assertEqual(fired, expected_bars[:3])
        ends = [bar.bucket_end_utc for bar in fired]
        self.assertEqual(ends, sorted(ends))      # recorded-time order
        for bar in fired:
            self.assertIsInstance(bar, MidBar)

    def test_as_of_reads_cannot_see_the_future(self):
        self._write()
        expected_bars, _ = self._direct_bars()
        bar_a, bar_b = expected_bars[0], expected_bars[1]
        feed = ReplayQuoteFeed(self.path)
        reader = feed.bar_reader()

        # A later bucket is future_receipt at an earlier as-of (FD-2).
        got = reader.get(SYMBOL, IID, bar_b.bucket_end_utc,
                         as_of_utc=bar_a.bucket_end_utc)
        self.assertIsInstance(got, MissingBar)
        self.assertEqual(got.reason, "future_receipt")
        # An in-progress bucket is future_receipt — whole-second as_of surface
        # form on purpose (SAFETY-F2: parse-then-compare, never lexicographic).
        got = reader.get(SYMBOL, IID, bar_a.bucket_end_utc,
                         as_of_utc="2026-06-09T13:30:30Z")
        self.assertIsInstance(got, MissingBar)
        self.assertEqual(got.reason, "future_receipt")
        # At its own end the bar is eligible (watermark already passed).
        self.assertEqual(
            reader.get(SYMBOL, IID, bar_a.bucket_end_utc,
                       as_of_utc=bar_a.bucket_end_utc), bar_a)
        self.assertEqual(
            reader.eligible_history(SYMBOL, IID, as_of_utc=bar_b.bucket_end_utc,
                                    max_bars=10),
            (bar_a, bar_b))

    def test_resample_runs_exactly_once_for_the_file(self):
        self._write()
        calls = []
        real = replay_feed_module.resample_midbars

        def counting(*args, **kwargs):
            calls.append(kwargs.get("symbol"))
            return real(*args, **kwargs)

        replay_feed_module.resample_midbars = counting
        try:
            feed = ReplayQuoteFeed(self.path)
            _, _, on_tick, on_bar = _collecting_callbacks()
            feed.run(on_tick=on_tick, on_bar_complete=on_bar)
        finally:
            replay_feed_module.resample_midbars = real
        self.assertEqual(calls, [SYMBOL])   # ONCE: at construction, never in run


class TestTailSemantics(_Tmp):
    """S3, inherited verbatim from replay_stream: a truncated (no-newline) tail
    is dropped; a complete corrupt line is fatal JournalCorruption."""

    def _write(self):
        _write_stream(self.path, [
            (_quote_record(vendor_seq=i + 1,
                           ts_event=f"2026-06-09T13:30:0{i}.000000Z"),
             f"2026-06-09T13:30:0{i}.500000Z")
            for i in range(3)
        ])

    def test_truncated_tail_tolerated(self):
        self._write()
        with open(self.path, "ab") as fh:
            fh.write(b'{"event_type":"events","schema":"tbbo","trunc')   # no \n
        feed = ReplayQuoteFeed(self.path)
        _, _, on_tick, on_bar = _collecting_callbacks()
        feed.run(on_tick=on_tick, on_bar_complete=on_bar)
        self.assertEqual(feed.clock().now_ms(), 2000)   # all 3 complete rows
        self.assertIsNotNone(feed.quote_view().latest(SYMBOL, IID))

    def test_corrupt_complete_line_raises_journal_corruption(self):
        self._write()
        lines = self.path.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(lines[1])
        tampered["bid_px"] = "999.9999"                  # hash no longer matches
        lines[1] = json.dumps(tampered)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(JournalCorruption):
            ReplayQuoteFeed(self.path)


class TestRunMechanics(_Tmp):
    def _write_one(self):
        _write_stream(self.path, [
            (_quote_record(vendor_seq=1, ts_event="2026-06-09T13:30:00.000000Z"),
             "2026-06-09T13:30:00.000000Z"),
        ])

    def test_run_is_single_shot(self):
        self._write_one()
        feed = ReplayQuoteFeed(self.path)
        _, _, on_tick, on_bar = _collecting_callbacks()
        feed.run(on_tick=on_tick, on_bar_complete=on_bar)
        with self.assertRaises(ValueError):
            feed.run(on_tick=on_tick, on_bar_complete=on_bar)

    def test_cadence_must_be_a_strict_positive_int(self):
        self._write_one()
        for bad in (0, -5, True, "1000", 1000.0):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ReplayQuoteFeed(self.path, refresh_cadence_ms=bad)

    def test_empty_or_quote_free_file_runs_quietly(self):
        # Only a trades row: nothing deliverable, no ticks, clock stays 0.
        _write_stream(self.path, [
            (_trade_record(vendor_seq=1, ts_event="2026-06-09T13:30:00.000000Z"),
             "2026-06-09T13:30:00.100000Z"),
        ])
        feed = ReplayQuoteFeed(self.path)
        ticks, fired, on_tick, on_bar = _collecting_callbacks()
        feed.run(on_tick=on_tick, on_bar_complete=on_bar)
        self.assertEqual((ticks, fired), ([], []))
        self.assertEqual(feed.clock().now_ms(), 0)
        self.assertEqual(feed.symbols(), ())


if __name__ == "__main__":
    unittest.main()
