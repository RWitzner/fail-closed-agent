"""EquityBookState L2 ladder + TradeTape (contract §C; tests §N).

The book is the ONLY input to book_hash, so its determinism is load-bearing.
mbp-10 is a FULL snapshot: apply() REPLACES the ladder (replace-on-apply). A
crossed/locked book is recorded and surfaced via snapshot().crossed — NEVER
silently normalized (the recorder turns that flag into a data_quality_alert).
Pure / no IO / no clock: identical event sequences => identical state.
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path

from recorder.book_state import BookSnapshot, BookStateError, EquityBookState, TradeTape
from recorder.event import DepthEvent, DepthLevel, Provenance, QuoteEvent, TradeEvent, parse

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "databento"


def _load_jsonl(name):
    path = FIXTURES / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _parse_depth(record):
    return parse(
        record,
        dataset=record["dataset"],
        schema=record["schema"],
        reconnect_epoch=0,
        ts_recv_utc="2026-06-09T13:30:00.000999Z",
    )


def _prov(*, symbol="AAPL", instrument_id=1001, vendor_seq=2001,
          ts_event="2026-06-09T13:30:00.500000Z", reconnect_epoch=0):
    return Provenance(
        dataset="<DEPTH_DATASET>",
        schema="mbp-10",
        instrument_id=instrument_id,
        symbol=symbol,
        vendor_seq=vendor_seq,
        ts_event_utc=ts_event,
        ts_recv_utc="2026-06-09T13:30:00.000999Z",
        reconnect_epoch=reconnect_epoch,
    )


def _depth(bids, asks, **prov_kwargs):
    """bids/asks are lists of (px_str, sz_str, ct) tuples."""
    return DepthEvent(
        provenance=_prov(**prov_kwargs),
        bids=tuple(DepthLevel(px=Decimal(p), sz=Decimal(s), ct=c) for p, s, c in bids),
        asks=tuple(DepthLevel(px=Decimal(p), sz=Decimal(s), ct=c) for p, s, c in asks),
    )


class TestApplyAndSnapshot(unittest.TestCase):
    def test_snapshot_identity_from_event(self):
        state = EquityBookState("AAPL", 1001)
        state.apply(_depth([("201.1500", "300", 3)], [("201.1600", "200", 2)]))
        snap = state.snapshot()
        self.assertIsInstance(snap, BookSnapshot)
        self.assertEqual(snap.symbol, "AAPL")
        self.assertEqual(snap.instrument_id, 1001)

    def test_apply_is_replace_on_snapshot(self):
        """mbp-10 is a full snapshot: a second apply REPLACES, never merges."""
        state = EquityBookState("AAPL", 1001)
        state.apply(_depth(
            [("201.1500", "300", 3), ("201.1400", "200", 2)],
            [("201.1600", "200", 2), ("201.1700", "400", 4)],
        ))
        state.apply(_depth(
            [("201.1500", "250", 2)],
            [("201.1600", "200", 2)],
            vendor_seq=2002,
        ))
        snap = state.snapshot()
        self.assertEqual([(l.px, l.sz) for l in snap.bids], [(Decimal("201.1500"), Decimal("250"))])
        self.assertEqual([(l.px, l.sz) for l in snap.asks], [(Decimal("201.1600"), Decimal("200"))])

    def test_snapshot_bids_sorted_desc_asks_asc_best_first(self):
        state = EquityBookState("AAPL", 1001)
        # Feed in NON-canonical vendor order to prove snapshot sorts.
        state.apply(_depth(
            [("201.1400", "200", 2), ("201.1500", "300", 3)],
            [("201.1700", "400", 4), ("201.1600", "200", 2)],
        ))
        snap = state.snapshot()
        self.assertEqual([l.px for l in snap.bids], [Decimal("201.1500"), Decimal("201.1400")])
        self.assertEqual([l.px for l in snap.asks], [Decimal("201.1600"), Decimal("201.1700")])

    def test_best_bid_and_best_ask(self):
        state = EquityBookState("AAPL", 1001)
        state.apply(_depth(
            [("201.1500", "300", 3), ("201.1400", "200", 2)],
            [("201.1600", "200", 2), ("201.1700", "400", 4)],
        ))
        self.assertEqual(state.best_bid(), (Decimal("201.1500"), Decimal("300")))
        self.assertEqual(state.best_ask(), (Decimal("201.1600"), Decimal("200")))

    def test_best_of_empty_book_is_none(self):
        state = EquityBookState("AAPL", 1001)
        self.assertIsNone(state.best_bid())
        self.assertIsNone(state.best_ask())
        snap = state.snapshot()
        self.assertEqual(snap.bids, ())
        self.assertEqual(snap.asks, ())
        self.assertFalse(snap.crossed)

    def test_deterministic_identical_sequences_identical_snapshot(self):
        events = [
            _depth([("201.1500", "300", 3)], [("201.1600", "200", 2)]),
            _depth([("201.1500", "250", 2)], [("201.1600", "200", 2)], vendor_seq=2002),
        ]
        a = EquityBookState("AAPL", 1001)
        b = EquityBookState("AAPL", 1001)
        for ev in events:
            a.apply(ev)
            b.apply(ev)
        self.assertEqual(a.snapshot(), b.snapshot())


class TestCrossedBook(unittest.TestCase):
    def test_crossed_book_flagged_not_corrected(self):
        """best_bid >= best_ask is RECORDED via snapshot().crossed, not silently fixed."""
        state = EquityBookState("AAPL", 1001)
        state.apply(_depth([("201.1700", "100", 1)], [("201.1600", "100", 1)]))
        snap = state.snapshot()
        self.assertTrue(snap.crossed)
        # The ladder is preserved as received — NOT auto-corrected away.
        self.assertEqual(state.best_bid(), (Decimal("201.1700"), Decimal("100")))
        self.assertEqual(state.best_ask(), (Decimal("201.1600"), Decimal("100")))

    def test_locked_book_is_crossed(self):
        """best_bid == best_ask (locked) also trips the crossed flag (>=)."""
        state = EquityBookState("AAPL", 1001)
        state.apply(_depth([("201.1600", "100", 1)], [("201.1600", "100", 1)]))
        self.assertTrue(state.snapshot().crossed)

    def test_uncrossed_book_not_flagged(self):
        state = EquityBookState("AAPL", 1001)
        state.apply(_depth([("201.1500", "100", 1)], [("201.1600", "100", 1)]))
        self.assertFalse(state.snapshot().crossed)

    def test_one_sided_book_not_crossed(self):
        state = EquityBookState("AAPL", 1001)
        state.apply(_depth([("201.1500", "100", 1)], []))
        self.assertFalse(state.snapshot().crossed)


class TestApplyQuote(unittest.TestCase):
    def test_apply_quote_builds_one_level_ladder(self):
        state = EquityBookState("AAPL", 1001)
        quote = QuoteEvent(
            provenance=Provenance(
                dataset="EQUS.MINI", schema="tbbo", instrument_id=1001, symbol="AAPL",
                vendor_seq=1001, ts_event_utc="2026-06-09T13:30:00.100000Z",
                ts_recv_utc="2026-06-09T13:30:00.100200Z", reconnect_epoch=0,
            ),
            bid_px=Decimal("201.1500"), bid_sz=Decimal("300"),
            ask_px=Decimal("201.1600"), ask_sz=Decimal("200"),
        )
        state.apply_quote(quote)
        self.assertEqual(state.best_bid(), (Decimal("201.1500"), Decimal("300")))
        self.assertEqual(state.best_ask(), (Decimal("201.1600"), Decimal("200")))
        snap = state.snapshot()
        self.assertEqual(len(snap.bids), 1)
        self.assertEqual(len(snap.asks), 1)
        self.assertFalse(snap.crossed)


class TestLadderInvariants(unittest.TestCase):
    def test_more_than_10_levels_raises(self):
        state = EquityBookState("AAPL", 1001)
        bids = [(f"201.{1500 - i:04d}", "100", 1) for i in range(11)]
        with self.assertRaises(BookStateError):
            state.apply(_depth(bids, [("201.1600", "100", 1)]))

    def test_apply_quote_wrong_symbol_raises(self):
        state = EquityBookState("AAPL", 1001)
        ev = _depth([("201.1500", "100", 1)], [("201.1600", "100", 1)], symbol="MSFT")
        with self.assertRaises(BookStateError):
            state.apply(ev)

    def test_apply_wrong_instrument_id_raises(self):
        state = EquityBookState("AAPL", 1001)
        ev = _depth([("201.1500", "100", 1)], [("201.1600", "100", 1)], instrument_id=9999)
        with self.assertRaises(BookStateError):
            state.apply(ev)


class TestFixtureDriven(unittest.TestCase):
    def test_apply_mbp10_fixture_rows(self):
        records = _load_jsonl("mbp10_depth_sample.jsonl")
        state = EquityBookState("AAPL", 1001)
        for record in records:
            state.apply(_parse_depth(record))
        # Final row 2002: bids [201.1500/250, 201.1400/500], asks [201.1600/200].
        snap = state.snapshot()
        self.assertEqual(
            [(l.px, l.sz) for l in snap.bids],
            [(Decimal("201.1500"), Decimal("250")), (Decimal("201.1400"), Decimal("500"))],
        )
        self.assertEqual([(l.px, l.sz) for l in snap.asks], [(Decimal("201.1600"), Decimal("200"))])
        self.assertFalse(snap.crossed)


class TestTradeTape(unittest.TestCase):
    def _trade(self, price, size, *, vendor_seq=3001):
        return TradeEvent(
            provenance=Provenance(
                dataset="EQUS.MINI", schema="trades", instrument_id=1001, symbol="AAPL",
                vendor_seq=vendor_seq, ts_event_utc="2026-06-09T13:30:00.000000Z",
                ts_recv_utc="2026-06-09T13:30:00.000100Z", reconnect_epoch=0,
            ),
            price=Decimal(price), size=Decimal(size), side="B",
        )

    def test_record_and_last(self):
        tape = TradeTape("AAPL")
        self.assertIsNone(tape.last())
        t1 = self._trade("201.5000", "100", vendor_seq=3001)
        t2 = self._trade("201.6000", "150", vendor_seq=3002)
        tape.record(t1)
        tape.record(t2)
        self.assertEqual(tape.last(), t2)

    def test_recent_returns_newest_last_bounded(self):
        tape = TradeTape("AAPL")
        trades = [self._trade("201.5000", "100", vendor_seq=3000 + i) for i in range(5)]
        for t in trades:
            tape.record(t)
        self.assertEqual(tape.recent(3), tuple(trades[-3:]))
        self.assertEqual(tape.recent(99), tuple(trades))

    def test_ring_drops_oldest_at_maxlen(self):
        tape = TradeTape("AAPL", maxlen=2)
        trades = [self._trade("201.5000", "100", vendor_seq=3000 + i) for i in range(3)]
        for t in trades:
            tape.record(t)
        self.assertEqual(tape.recent(99), tuple(trades[-2:]))
        self.assertEqual(tape.last(), trades[-1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
