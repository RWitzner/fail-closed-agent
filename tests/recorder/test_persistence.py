"""EventWriter persistence (contract §E; tests §N).

`persistence.EventWriter` WRAPS `agent.journal.JournalWriter` — it does NOT
re-implement the lock / seq / hash / tail loop. It persists EXACTLY the flat
`recorder.event_row.to_row(event)` shape (§B2), shares the INJECTED agent
`run_id` namespace across the events / data_quality_alerts / status streams (S6),
and uses `vendor_seq` (BLOCKER 1) so no flat field collides with the journal's
reserved monotonic `seq` (journal.py:21,113-114).

§N cases bound to persistence:
- write_event persists the to_row flat shape (incl. journal seq + hash).
- vendor_seq does NOT collide with journal _RESERVED (never raises at journal.py:114).
- a from_row(row) after read round-trips to the exact *Event.
- the events / data_quality_alerts / status streams share one injected run_id.
"""
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agent.journal import JournalCorruption
from recorder.event import (
    BarEvent,
    DefinitionEvent,
    DepthEvent,
    QuoteEvent,
    TradeEvent,
    parse,
)
from recorder.event_row import from_row, to_row
from recorder.persistence import (
    STREAM_DATA_QUALITY,
    STREAM_EVENTS,
    STREAM_STATUS,
    EventWriter,
    replay_stream,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "databento"

# A fixed receipt stamp so events are byte-stable offline (no wall clock).
TS_RECV = "2026-06-09T13:30:00.000999Z"


def _load_jsonl(name):
    path = FIXTURES / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _parse_record(record):
    return parse(
        record,
        dataset=record["dataset"],
        schema=record["schema"],
        reconnect_epoch=0,
        ts_recv_utc=TS_RECV,
    )


def _all_events():
    return [
        _parse_record(_load_jsonl("equs_mini_tbbo_sample.jsonl")[0]),
        _parse_record(_load_jsonl("mbp10_depth_sample.jsonl")[0]),
        _parse_record({
            "dataset": "EQUS.MINI", "schema": "trades", "instrument_id": 1001, "symbol": "AAPL",
            "vendor_seq": 4001, "ts_event": "2026-06-09T13:30:00.300000Z",
            "price": "201.5000", "size": "100", "side": "B",
        }),
        _parse_record({
            "dataset": "EQUS.MINI", "schema": "ohlcv-1m", "instrument_id": 1001, "symbol": "AAPL",
            "vendor_seq": 5001, "ts_event": "2026-06-09T13:31:00.000000Z",
            "open": "201.0000", "high": "202.0000", "low": "200.5000",
            "close": "201.7500", "volume": "12000",
        }),
        _parse_record({
            "dataset": "EQUS.MINI", "schema": "definitions", "instrument_id": 1001, "symbol": "AAPL",
            "vendor_seq": 6001, "ts_event": "2026-06-09T13:00:00.000000Z",
            "mic": "XNAS", "raw_symbol": "AAPL",
        }),
    ]


class _Tmp(unittest.TestCase):
    """Fresh temp dir per test so the path-keyed journal seq registry (journal.py:70)
    does not bleed state across tests (mirrors test_journal_replay.py:_Tmp)."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _events_path(self):
        return self.root / "events.jsonl"


_RESERVED = {"event_type", "run_id", "seq", "hash", "decision_id", "order_id", "ts_utc"}


class TestWriteEventPersistsToRowShape(_Tmp):
    def test_write_event_persists_to_row_flat_shape(self):
        w = EventWriter(self._events_path(), run_id="run-1")
        ev = _parse_record(_load_jsonl("equs_mini_tbbo_sample.jsonl")[0])
        row = w.write_event(ev)
        # The recorder facade returns the persisted row: journal-owned keys present.
        self.assertEqual(row["run_id"], "run-1")
        self.assertEqual(row["seq"], 1)
        self.assertEqual(row["event_type"], STREAM_EVENTS)
        self.assertIn("hash", row)
        # Every flat field from to_row(ev) is on the persisted row, unchanged.
        expected = to_row(ev)
        for key, value in expected.items():
            self.assertIn(key, row)
            self.assertEqual(row[key], value, f"flat field {key!r} mismatch")
        # vendor_seq present; the colliding `seq` carries the JOURNAL seq, not vendor's.
        self.assertEqual(row["vendor_seq"], 1001)
        self.assertEqual(row["seq"], 1)

    def test_depth_row_carries_derived_book_hash(self):
        w = EventWriter(self._events_path(), run_id="run-1")
        ev = _parse_record(_load_jsonl("mbp10_depth_sample.jsonl")[0])
        bh = "deadbeef" * 8  # a stand-in hash computed by the recorder (§E)
        row = w.write_event(ev, derived_book_hash=bh)
        self.assertEqual(row["derived_book_hash"], bh)
        self.assertIsInstance(row["bids"], list)
        self.assertIsInstance(row["asks"], list)

    def test_decimal_fields_persisted_as_strings(self):
        w = EventWriter(self._events_path(), run_id="run-1")
        ev = _parse_record(_load_jsonl("equs_mini_tbbo_sample.jsonl")[0])
        w.write_event(ev)
        # Read the raw on-disk line: Decimals must render as JSON strings (serializer).
        raw = json.loads(self._events_path().read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(raw["bid_px"], "201.1500")
        self.assertIsInstance(raw["bid_px"], str)


class TestVendorSeqDoesNotCollide(_Tmp):
    def test_vendor_seq_does_not_raise_reserved_collision(self):
        # BLOCKER 1: a vendor field named `vendor_seq` (NOT `seq`) never trips the
        # journal's _RESERVED guard (journal.py:113-114).
        w = EventWriter(self._events_path(), run_id="run-1")
        for ev in _all_events():
            row = w.write_event(ev)  # must NOT raise ValueError
            self.assertIn("vendor_seq", row)

    def test_to_row_produces_no_reserved_key(self):
        for ev in _all_events():
            self.assertEqual(_RESERVED & set(to_row(ev)), set())

    def test_journal_seq_is_monotonic_across_events(self):
        w = EventWriter(self._events_path(), run_id="run-1")
        seqs = [w.write_event(ev)["seq"] for ev in _all_events()]
        self.assertEqual(seqs, [1, 2, 3, 4, 5])


class TestRoundTripAfterRead(_Tmp):
    def test_from_row_after_replay_roundtrips_every_event_type(self):
        w = EventWriter(self._events_path(), run_id="run-1")
        events = _all_events()
        for ev in events:
            w.write_event(ev)
        rows = replay_stream(self._events_path())
        self.assertEqual(len(rows), len(events))
        for ev, row in zip(events, rows):
            rebuilt = from_row(row)
            self.assertEqual(rebuilt, ev)

    def test_replay_stream_hash_verifies(self):
        w = EventWriter(self._events_path(), run_id="run-1")
        w.write_event(_parse_record(_load_jsonl("equs_mini_tbbo_sample.jsonl")[0]))
        # Tamper a complete persisted line -> JournalCorruption (inherited from M0).
        path = self._events_path()
        line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        line["bid_px"] = "999.9999"  # hash no longer matches
        path.write_text(json.dumps(line) + "\n", encoding="utf-8")
        with self.assertRaises(JournalCorruption):
            replay_stream(path)


class TestSharedRunIdAcrossStreams(_Tmp):
    def test_events_alerts_status_share_injected_run_id(self):
        run_id = "agent-run-42"
        events = EventWriter(self.root / "events.jsonl", run_id=run_id)
        alerts = EventWriter(self.root / "data_quality_alerts.jsonl", run_id=run_id)
        status = EventWriter(self.root / "status.jsonl", run_id=run_id)

        ev_row = events.write_event(_parse_record(_load_jsonl("equs_mini_tbbo_sample.jsonl")[0]))
        alert_row = alerts.record(STREAM_DATA_QUALITY, {"cause": "sequence_gap", "symbol": "AAPL"})
        status_row = status.record(STREAM_STATUS, {"state": "connected", "symbol": "AAPL"})

        self.assertEqual(ev_row["run_id"], run_id)
        self.assertEqual(alert_row["run_id"], run_id)
        self.assertEqual(status_row["run_id"], run_id)
        # The recorder SHARES the injected namespace; it does not mint its own.
        self.assertEqual({ev_row["run_id"], alert_row["run_id"], status_row["run_id"]}, {run_id})

    def test_record_escape_hatch_persists_alert_fields(self):
        run_id = "agent-run-7"
        alerts = EventWriter(self.root / "data_quality_alerts.jsonl", run_id=run_id)
        row = alerts.record(STREAM_DATA_QUALITY, {"cause": "crossed_book", "symbol": "AAPL", "reconnect_epoch": 0})
        self.assertEqual(row["cause"], "crossed_book")
        self.assertEqual(row["symbol"], "AAPL")
        self.assertEqual(row["event_type"], STREAM_DATA_QUALITY)
        self.assertEqual(row["run_id"], run_id)
        rows = replay_stream(self.root / "data_quality_alerts.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cause"], "crossed_book")

    def test_decision_and_order_id_threaded_through(self):
        w = EventWriter(self._events_path(), run_id="run-1")
        ev = _parse_record(_load_jsonl("equs_mini_tbbo_sample.jsonl")[0])
        row = w.write_event(ev, decision_id="dec-9", order_id="ord-3")
        self.assertEqual(row["decision_id"], "dec-9")
        self.assertEqual(row["order_id"], "ord-3")


class TestInjectedClock(_Tmp):
    def test_injected_clock_stamps_ts_utc(self):
        stamps = iter(["2026-06-09T00:00:01Z", "2026-06-09T00:00:02Z"])
        w = EventWriter(self._events_path(), run_id="run-1", clock=lambda: next(stamps))
        row = w.write_event(_parse_record(_load_jsonl("equs_mini_tbbo_sample.jsonl")[0]))
        self.assertEqual(row["ts_utc"], "2026-06-09T00:00:01Z")


if __name__ == "__main__":
    unittest.main()
