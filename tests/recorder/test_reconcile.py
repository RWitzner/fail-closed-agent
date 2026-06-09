"""Dual-hash reconcile of a recorded stream against a reference (contract §I; spec §7).

Reconcile re-derives the per-row ``book_hash`` from a recorded depth stream AND from
a reference stream, keyed on ``(symbol, vendor_seq)``, and reports every divergence
without silently mutating either side (fail-closed). ``ok`` is True iff there is no
mismatch and nothing missing. The credentialed Databento-historical path is a tier-2
stub that raises ``NotImplementedError`` offline.

Cases:
- identical streams reconcile ok (matched == row count, no mismatch/missing).
- a differing hash for the same key is a mismatch (ok=False), never mutated.
- a key present on one side only is reported missing_in_reference / missing_in_recorded.
- the historical reconcile stub raises NotImplementedError offline (no-net).
"""
import json
import tempfile
import unittest
from pathlib import Path

from recorder.book_hash import book_hash
from recorder.book_state import EquityBookState
from recorder.event import parse
from recorder.persistence import EventWriter
from recorder.reconcile import (
    ReconcileReport,
    reconcile_against_fixture,
    reconcile_against_historical,
)
from recorder.status import make_data_quality_alert

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "databento"
DEPTH_FIXTURE = FIXTURES / "mbp10_depth_sample.jsonl"
TS_RECV = "2026-06-09T13:30:00.000999Z"


def _records():
    return [json.loads(line) for line in DEPTH_FIXTURE.read_text(encoding="utf-8").splitlines() if line]


def _parse(rec):
    return parse(rec, dataset=rec["dataset"], schema=rec["schema"],
                reconnect_epoch=0, ts_recv_utc=TS_RECV)


def _write_depth_stream(path, records, run_id):
    writer = EventWriter(path, run_id=run_id)
    for rec in records:
        ev = _parse(rec)
        state = EquityBookState(ev.provenance.symbol, ev.provenance.instrument_id)
        state.apply(ev)
        writer.write_event(ev, derived_book_hash=book_hash(state.snapshot()))


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()


class TestReconcileAgainstFixture(_Tmp):
    def test_identical_streams_reconcile_ok(self):
        _write_depth_stream(self.root / "recorded.jsonl", _records(), "rec")
        _write_depth_stream(self.root / "reference.jsonl", _records(), "ref")
        report = reconcile_against_fixture(self.root / "recorded.jsonl", self.root / "reference.jsonl")
        self.assertIsInstance(report, ReconcileReport)
        self.assertTrue(report.ok)
        self.assertEqual(report.matched, 2)
        self.assertEqual(report.mismatches, ())
        self.assertEqual(report.missing_in_recorded, ())
        self.assertEqual(report.missing_in_reference, ())

    def test_differing_hash_is_a_mismatch_not_mutated(self):
        # Reference persisted with a deliberately wrong book for vendor_seq 2001:
        # a different ladder -> a different re-derived hash -> a reported mismatch.
        _write_depth_stream(self.root / "recorded.jsonl", _records(), "rec")
        records = _records()
        records[0]["bids"] = [["199.9900", "100", 1]]  # different book, same key
        _write_depth_stream(self.root / "reference.jsonl", records, "ref")
        report = reconcile_against_fixture(self.root / "recorded.jsonl", self.root / "reference.jsonl")
        self.assertFalse(report.ok)
        self.assertEqual(len(report.mismatches), 1)
        m = report.mismatches[0]
        self.assertEqual(m["symbol"], "AAPL")
        self.assertEqual(m["vendor_seq"], 2001)
        self.assertNotEqual(m["recorded_hash"], m["reference_hash"])

    def test_missing_in_reference_reported(self):
        _write_depth_stream(self.root / "recorded.jsonl", _records(), "rec")
        _write_depth_stream(self.root / "reference.jsonl", _records()[:1], "ref")  # only vendor_seq 2001
        report = reconcile_against_fixture(self.root / "recorded.jsonl", self.root / "reference.jsonl")
        self.assertFalse(report.ok)
        self.assertEqual(report.matched, 1)
        self.assertEqual(len(report.missing_in_reference), 1)
        self.assertEqual(report.missing_in_reference[0]["vendor_seq"], 2002)
        self.assertEqual(report.missing_in_recorded, ())

    def test_missing_in_recorded_reported(self):
        _write_depth_stream(self.root / "recorded.jsonl", _records()[:1], "rec")  # only vendor_seq 2001
        _write_depth_stream(self.root / "reference.jsonl", _records(), "ref")
        report = reconcile_against_fixture(self.root / "recorded.jsonl", self.root / "reference.jsonl")
        self.assertFalse(report.ok)
        self.assertEqual(report.matched, 1)
        self.assertEqual(len(report.missing_in_recorded), 1)
        self.assertEqual(report.missing_in_recorded[0]["vendor_seq"], 2002)
        self.assertEqual(report.missing_in_reference, ())


class TestC6SingleStreamDivergenceEscalates(_Tmp):
    """C6 (finding #5): reconcile MUST escalate a single-stream replay hash divergence.

    A recorded stream whose persisted derived_book_hash is STALE/WRONG on a depth row
    makes replay_book_hashes(...).ok == False. reconcile_against_fixture must fold each
    side's .ok into its result as a HARD failure (previously reported ok=True with zero
    mismatches because only the re-derived hashes were compared, ignoring the persisted
    derived_book_hash divergence).
    """

    def _write_with_one_stale_hash(self, path, records, run_id):
        writer = EventWriter(path, run_id=run_id)
        for i, rec in enumerate(records):
            ev = _parse(rec)
            state = EquityBookState(ev.provenance.symbol, ev.provenance.instrument_id)
            state.apply(ev)
            bh = book_hash(state.snapshot())
            if i == 0:
                bh = "00" * 32  # stale/wrong persisted derived_book_hash on the first row
            writer.write_event(ev, derived_book_hash=bh)

    def test_recorded_side_stale_hash_makes_reconcile_not_ok(self):
        # Recorded side has a wrong persisted derived_book_hash on vendor_seq 2001;
        # reference side is clean. Re-derived hashes still MATCH (same books), but the
        # recorded stream fails its OWN replay -> reconcile must report NOT ok.
        self._write_with_one_stale_hash(self.root / "recorded.jsonl", _records(), "rec")
        _write_depth_stream(self.root / "reference.jsonl", _records(), "ref")
        report = reconcile_against_fixture(
            self.root / "recorded.jsonl", self.root / "reference.jsonl"
        )
        self.assertFalse(report.ok)


class TestE2ReconcileSkipsNonEventRows(_Tmp):
    """E2 (R3#2): reconcile inherits the replay skip-non-event-row fix.

    A stream mixing a depth row + a data_quality_alert row (no ``schema`` field, as
    ``make_data_quality_alert`` produces) routed into the events stream must reconcile
    cleanly: reconcile delegates to ``replay_book_hashes`` on each side, which now skips
    schema-less alert rows instead of crashing on ``from_row``.
    """

    def _write_depth_plus_alert(self, path, records, run_id):
        writer = EventWriter(path, run_id=run_id)
        for rec in records:
            ev = _parse(rec)
            state = EquityBookState(ev.provenance.symbol, ev.provenance.instrument_id)
            state.apply(ev)
            writer.write_event(ev, derived_book_hash=book_hash(state.snapshot()))
        alert = make_data_quality_alert(
            cause="crossed_book", symbol="AAPL", detail="bid>=ask", reconnect_epoch=0
        )
        self.assertNotIn("schema", alert)
        writer.record("data_quality_alert", alert)

    def test_streams_with_alert_rows_reconcile_clean(self):
        self._write_depth_plus_alert(self.root / "recorded.jsonl", _records(), "rec")
        self._write_depth_plus_alert(self.root / "reference.jsonl", _records(), "ref")
        # Before the fix replay_book_hashes crashed on the alert row, taking reconcile
        # down with it. Now both sides skip the alert and the depth rows reconcile.
        report = reconcile_against_fixture(
            self.root / "recorded.jsonl", self.root / "reference.jsonl"
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.matched, 2)
        self.assertEqual(report.mismatches, ())
        self.assertEqual(report.missing_in_recorded, ())
        self.assertEqual(report.missing_in_reference, ())


class TestHistoricalStub(unittest.TestCase):
    def test_reconcile_against_historical_is_notimplemented_offline(self):
        with self.assertRaises(NotImplementedError):
            reconcile_against_historical("recorded.jsonl", lambda: None)


if __name__ == "__main__":
    unittest.main()
