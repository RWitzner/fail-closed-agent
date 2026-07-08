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

from agent.journal import (
    IncrementalJournalReader,
    JournalCorruption,
    JournalWriter,
    replay,
)


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


class TestCrashRecovery(_Tmp):
    def test_reopen_repairs_truncated_tail_then_appends_replay_clean(self):
        w = JournalWriter(self.path, run_id="run-1")
        w.append("decision", {"symbol": "AAPL"})
        # Crash mid-write: a partial, unterminated line is left behind.
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write('{"event_type":"decision","symbol":"PARTIA')
        # Restart: a new writer must repair the dangling tail so future appends
        # land on a record boundary instead of concatenating onto garbage.
        w2 = JournalWriter(self.path, run_id="run-2")
        w2.append("decision", {"symbol": "MSFT"})
        rows = replay(self.path)
        self.assertEqual([r["symbol"] for r in rows], ["AAPL", "MSFT"])

    def test_seq_resumes_across_reopen(self):
        w = JournalWriter(self.path, run_id="run-1")
        w.append("decision", {"i": 0})
        w.append("decision", {"i": 1})
        w2 = JournalWriter(self.path, run_id="run-2")
        row = w2.append("decision", {"i": 2})
        self.assertEqual(row["seq"], 3)
        self.assertEqual(row["run_id"], "run-2")


class TestTailIntegrity(_Tmp):
    def test_corrupt_complete_trailing_line_with_newline_raises(self):
        # A trailing line that IS newline-terminated is a complete record; a bad
        # hash on it is corruption, not a partial write — it must raise (H1).
        w = JournalWriter(self.path, run_id="run-1")
        w.append("decision", {"symbol": "AAPL"})
        w.append("decision", {"symbol": "MSFT"})
        lines = self.path.read_text().rstrip("\n").split("\n")
        row = json.loads(lines[-1])
        row["symbol"] = "TAMPERED"  # body changed -> stored hash is now stale
        lines[-1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        self.path.write_text("\n".join(lines) + "\n")  # newline-terminated => complete
        with self.assertRaises(JournalCorruption):
            replay(self.path)


class TestSharedStreamSeq(_Tmp):
    def test_two_writers_same_path_share_monotonic_seq(self):
        # Both writers opened before either appends — they must still produce a
        # single monotonic per-stream seq, not [1, 1] (H2).
        w1 = JournalWriter(self.path, run_id="run-a")
        w2 = JournalWriter(self.path, run_id="run-b")
        s1 = w1.append("decision", {"x": 1})["seq"]
        s2 = w2.append("decision", {"x": 2})["seq"]
        self.assertEqual(sorted([s1, s2]), [1, 2])
        rows = replay(self.path)
        self.assertEqual(sorted(r["seq"] for r in rows), [1, 2])


class TestTimestamp(_Tmp):
    def test_rows_carry_ts_utc(self):
        w = JournalWriter(self.path, run_id="run-1")
        row = w.append("decision", {"symbol": "AAPL"})
        self.assertIn("ts_utc", row)
        self.assertIsInstance(row["ts_utc"], str)

    def test_ts_utc_clock_is_injectable(self):
        ticks = iter(["2026-06-08T00:00:00Z", "2026-06-08T00:00:01Z"])
        w = JournalWriter(self.path, run_id="run-1", clock=lambda: next(ticks))
        self.assertEqual(w.append("decision", {"i": 0})["ts_utc"], "2026-06-08T00:00:00Z")
        self.assertEqual(w.append("decision", {"i": 1})["ts_utc"], "2026-06-08T00:00:01Z")


class TestIncrementalJournalReader(_Tmp):
    """read() must equal replay(path) at every point — it exists so the tick
    loop stops re-reading + re-hashing the whole decisions file per bar batch
    (O(day²) over a session)."""

    def test_matches_full_replay_across_interleaved_appends(self):
        reader = IncrementalJournalReader(self.path)
        self.assertEqual(reader.read(), [])          # missing file
        w = JournalWriter(self.path, run_id="run-1",
                          clock=lambda: "2026-07-06T00:00:00Z")
        w.append("evt", {"n": 1})
        self.assertEqual(reader.read(), replay(self.path))
        w.append("evt", {"n": 2})
        w.append("evt", {"n": 3})
        self.assertEqual(reader.read(), replay(self.path))
        self.assertEqual([r["n"] for r in reader.read()], [1, 2, 3])
        # the returned list is a copy: caller mutation cannot poison the cache
        rows = reader.read()
        rows.append({"poison": True})
        self.assertEqual([r["n"] for r in reader.read()], [1, 2, 3])

    def test_complete_corrupt_row_raises_and_keeps_raising(self):
        w = JournalWriter(self.path, run_id="run-1",
                          clock=lambda: "2026-07-06T00:00:00Z")
        w.append("evt", {"n": 1})
        reader = IncrementalJournalReader(self.path)
        self.assertEqual(len(reader.read()), 1)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("garbage-complete-line\n")
        with self.assertRaises(JournalCorruption):
            reader.read()
        with self.assertRaises(JournalCorruption):
            reader.read()   # offset was NOT committed past the corruption

    def test_truncated_tail_pending_until_completed(self):
        w = JournalWriter(self.path, run_id="run-1",
                          clock=lambda: "2026-07-06T00:00:00Z")
        w.append("evt", {"n": 1})
        # craft row 2's exact bytes in a scratch stream, then append them split
        scratch = self.path.with_name("scratch.jsonl")
        w2 = JournalWriter(scratch, run_id="run-1",
                           clock=lambda: "2026-07-06T00:00:00Z")
        w2.append("evt", {"n": 1})
        w2.append("evt", {"n": 2})
        row2_bytes = scratch.read_bytes().splitlines(keepends=True)[1]
        reader = IncrementalJournalReader(self.path)
        self.assertEqual(len(reader.read()), 1)
        with open(self.path, "ab") as fh:
            fh.write(row2_bytes[:20])                # partial line, no newline
        self.assertEqual(len(reader.read()), 1)      # pending, like replay()
        with open(self.path, "ab") as fh:
            fh.write(row2_bytes[20:])                # completed
        self.assertEqual(reader.read(), replay(self.path))
        self.assertEqual([r["n"] for r in reader.read()], [1, 2])

    def test_shrunk_file_falls_back_to_full_reread(self):
        w = JournalWriter(self.path, run_id="run-1",
                          clock=lambda: "2026-07-06T00:00:00Z")
        w.append("evt", {"n": 1})
        first_row_bytes = self.path.read_bytes()
        w.append("evt", {"n": 2})
        reader = IncrementalJournalReader(self.path)
        self.assertEqual(len(reader.read()), 2)
        self.path.write_bytes(first_row_bytes)        # external shrink
        self.assertEqual(reader.read(), replay(self.path))
        self.assertEqual([r["n"] for r in reader.read()], [1])


if __name__ == "__main__":
    unittest.main()
