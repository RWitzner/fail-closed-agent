"""S3 (replay + partial-write) and S6 (correlation IDs + monotonic seq).

The journal is an append-only event-sourced stream. Every row carries `run_id`,
a per-stream monotonic `seq`, and a row hash. Replay re-reads the stream, verifies
hashes, and drops a single truncated trailing line (a crash mid-write) without
treating it as fatal — but a corrupt non-trailing line IS fatal.
"""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent.journal import JournalCorruption, JournalWriter, replay


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "stream.jsonl"

    def tearDown(self):
        self._dir.cleanup()


class TestAppendAndReplay(_Tmp):
    def test_append_returns_row_with_run_id_and_seq(self):
        w = JournalWriter(self.path, run_id="run-1")
        row = w.append("decision", {"symbol": "AAPL", "action": "do_nothing"})
        self.assertEqual(row["run_id"], "run-1")
        self.assertEqual(row["seq"], 1)
        self.assertEqual(row["event_type"], "decision")
        self.assertEqual(row["symbol"], "AAPL")
        self.assertIn("hash", row)

    def test_seq_is_monotonic(self):
        w = JournalWriter(self.path, run_id="run-1")
        seqs = [w.append("decision", {"i": i})["seq"] for i in range(5)]
        self.assertEqual(seqs, [1, 2, 3, 4, 5])

    def test_replay_returns_appended_rows(self):
        w = JournalWriter(self.path, run_id="run-1")
        w.append("decision", {"symbol": "AAPL"})
        w.append("decision", {"symbol": "MSFT"})
        rows = replay(self.path)
        self.assertEqual([r["symbol"] for r in rows], ["AAPL", "MSFT"])
        self.assertEqual([r["seq"] for r in rows], [1, 2])

    def test_optional_decision_and_order_ids_recorded(self):
        w = JournalWriter(self.path, run_id="run-1")
        row = w.append("order_submitted", {"symbol": "AAPL"}, decision_id="d1", order_id="o1")
        self.assertEqual(row["decision_id"], "d1")
        self.assertEqual(row["order_id"], "o1")

    def test_reserved_field_collision_rejected(self):
        w = JournalWriter(self.path, run_id="run-1")
        with self.assertRaises(ValueError):
            w.append("decision", {"seq": 99})

    def test_replay_empty_or_missing_file(self):
        self.assertEqual(replay(self.path), [])


class TestCorrelationAndMonotonicSeq(_Tmp):
    def test_rows_require_correlation_and_monotonic_seq(self):
        w = JournalWriter(self.path, run_id="run-7")
        for i in range(4):
            w.append("decision", {"i": i})
        rows = replay(self.path)
        self.assertTrue(all(r["run_id"] == "run-7" for r in rows))
        seqs = [r["seq"] for r in rows]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))


class TestPartialWriteAndCorruption(_Tmp):
    def test_replay_drops_truncated_trailing_line(self):
        w = JournalWriter(self.path, run_id="run-1")
        w.append("decision", {"symbol": "AAPL"})
        w.append("decision", {"symbol": "MSFT"})
        # Simulate a crash mid-write: append a partial (unterminated, invalid) line.
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write('{"event_type":"decision","symbol":"GOO')
        rows = replay(self.path)
        self.assertEqual([r["symbol"] for r in rows], ["AAPL", "MSFT"])

    def test_replay_raises_on_corrupt_non_trailing_line(self):
        w = JournalWriter(self.path, run_id="run-1")
        w.append("decision", {"symbol": "AAPL"})
        w.append("decision", {"symbol": "MSFT"})
        # Corrupt the FIRST line (not the trailing one) -> fatal.
        lines = self.path.read_text().split("\n")
        lines[0] = "{ this is not json"
        self.path.write_text("\n".join(lines))
        with self.assertRaises(JournalCorruption):
            replay(self.path)

    def test_replay_detects_tampered_hash_mid_stream(self):
        w = JournalWriter(self.path, run_id="run-1")
        w.append("decision", {"symbol": "AAPL"})
        w.append("decision", {"symbol": "MSFT"})
        lines = self.path.read_text().rstrip("\n").split("\n")
        row = json.loads(lines[0])
        row["symbol"] = "TAMPERED"  # body changed, hash now stale
        lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        self.path.write_text("\n".join(lines) + "\n")
        with self.assertRaises(JournalCorruption):
            replay(self.path)


class TestWriterLock(_Tmp):
    def test_concurrent_appends_are_serialized_with_no_torn_lines(self):
        w = JournalWriter(self.path, run_id="run-1")
        n_threads, per = 8, 25

        def worker():
            for _ in range(per):
                w.append("decision", {"x": 1})

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = replay(self.path)
        self.assertEqual(len(rows), n_threads * per)
        seqs = sorted(r["seq"] for r in rows)
        self.assertEqual(seqs, list(range(1, n_threads * per + 1)))


if __name__ == "__main__":
    unittest.main()
