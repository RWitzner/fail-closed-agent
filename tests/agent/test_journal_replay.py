"""S3 (replay + partial-write) and S6 (correlation IDs + monotonic seq).

The journal is an append-only event-sourced stream. Every row carries `run_id`,
a per-stream monotonic `seq`, and a row hash. Replay re-reads the stream, verifies
hashes, and drops a single truncated trailing line (a crash mid-write) without
treating it as fatal — but a corrupt non-trailing line IS fatal.
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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

    def test_active_writer_rejects_valid_external_rewrite(self):
        writer = JournalWriter(
            self.path, run_id="run-1",
            clock=lambda: "2026-07-06T00:00:00Z")
        writer.append("evt", {"value": "AAAA"})
        replacement = self.path.with_name("replacement.jsonl")
        replacement_writer = JournalWriter(
            replacement, run_id="run-1",
            clock=lambda: "2026-07-06T00:00:00Z")
        replacement_writer.append("evt", {"value": "BBBB"})
        self.path.write_bytes(replacement.read_bytes())

        with self.assertRaises(JournalCorruption):
            writer.append("evt", {"value": "CCCC"})

        self.assertEqual([row["value"] for row in replay(self.path)], ["BBBB"])


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

    def test_reopen_retries_if_external_append_lands_during_replay(self):
        source = self.path.with_name("source.jsonl")
        source_writer = JournalWriter(
            source, run_id="source",
            clock=lambda: "2026-07-09T00:00:00Z")
        source_writer.append("evt", {"value": "one"})
        source_writer.append("evt", {"value": "external-two"})
        source_lines = source.read_bytes().splitlines(keepends=True)
        self.path.write_bytes(source_lines[0])
        real_loads = json.loads
        appended = False

        def append_after_first_parse(payload, *args, **kwargs):
            nonlocal appended
            row = real_loads(payload, *args, **kwargs)
            if not appended:
                with open(self.path, "ab") as fh:
                    fh.write(source_lines[1])
                appended = True
            return row

        with mock.patch(
                "agent.journal.json.loads",
                side_effect=append_after_first_parse):
            writer = JournalWriter(
                self.path, run_id="writer",
                clock=lambda: "2026-07-09T00:00:01Z")

        row = writer.append("evt", {"value": "writer-three"})
        rows = replay(self.path)
        self.assertTrue(appended)
        self.assertEqual(row["seq"], 3)
        self.assertEqual([item["seq"] for item in rows], [1, 2, 3])
        self.assertEqual(
            [item["value"] for item in rows],
            ["one", "external-two", "writer-three"])

    def test_tail_repair_never_overwrites_path_replacement(self):
        writer = JournalWriter(
            self.path, run_id="old",
            clock=lambda: "2026-07-09T00:00:00Z")
        writer.append("evt", {"value": "AAAA"})
        with open(self.path, "ab") as fh:
            fh.write(b'{"partial":')

        replacement = self.path.with_name("replacement.jsonl")
        replacement_writer = JournalWriter(
            replacement, run_id="new",
            clock=lambda: "2026-07-09T00:00:00Z")
        replacement_writer.append("evt", {"value": "BBBB"})
        real_write_bytes = Path.write_bytes
        real_ftruncate = os.ftruncate
        replaced = False

        def replace_once():
            nonlocal replaced
            if not replaced:
                os.replace(replacement, self.path)
                replaced = True

        def replace_before_path_write(path, data):
            if path == self.path:
                replace_once()
            return real_write_bytes(path, data)

        def replace_before_descriptor_truncate(fd, length):
            replace_once()
            return real_ftruncate(fd, length)

        with mock.patch(
                "pathlib.Path.write_bytes", autospec=True,
                side_effect=replace_before_path_write), mock.patch(
                    "agent.journal.os.ftruncate",
                    side_effect=replace_before_descriptor_truncate):
            reopened = JournalWriter(
                self.path, run_id="repair",
                clock=lambda: "2026-07-09T00:00:01Z")

        self.assertTrue(replaced)
        self.assertEqual(
            [item["value"] for item in replay(self.path)], ["BBBB"])
        row = reopened.append("evt", {"value": "CCCC"})
        self.assertEqual(row["seq"], 2)
        self.assertEqual(
            [item["value"] for item in replay(self.path)],
            ["BBBB", "CCCC"])

    def test_tail_repair_replays_post_truncate_descriptor_version(self):
        source = self.path.with_name("source.jsonl")
        source_writer = JournalWriter(
            source, run_id="source",
            clock=lambda: "2026-07-09T00:00:00Z")
        source_writer.append("evt", {"value": "AAAA"})
        source_writer.append("evt", {"value": "BBBB"})
        source_lines = source.read_bytes().splitlines(keepends=True)
        self.assertEqual(len(source_lines[0]), len(source_lines[1]))
        self.path.write_bytes(source_lines[0] + b'{"partial":')
        real_ftruncate = os.ftruncate
        rewritten = False

        def rewrite_prefix_then_truncate(fd, length):
            nonlocal rewritten
            with open(self.path, "r+b") as external:
                external.write(source_lines[1])
                external.flush()
            rewritten = True
            return real_ftruncate(fd, length)

        with mock.patch(
                "agent.journal.os.ftruncate",
                side_effect=rewrite_prefix_then_truncate):
            reopened = JournalWriter(
                self.path, run_id="repair",
                clock=lambda: "2026-07-09T00:00:01Z")

        row = reopened.append("evt", {"value": "CCCC"})
        rows = replay(self.path)
        self.assertTrue(rewritten)
        self.assertEqual(row["seq"], 3)
        self.assertEqual([item["seq"] for item in rows], [2, 3])
        self.assertEqual(
            [item["value"] for item in rows], ["BBBB", "CCCC"])


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

    def test_returned_nested_rows_cannot_mutate_reader_cache(self):
        writer = JournalWriter(
            self.path, run_id="run-1",
            clock=lambda: "2026-07-06T00:00:00Z")
        writer.append("evt", {"nested": {"value": 1}})
        reader = IncrementalJournalReader(self.path)
        rows = reader.read()
        rows[0]["nested"]["value"] = 999

        self.assertEqual(reader.read(), replay(self.path))

    def test_same_size_replacement_forces_integrity_recheck(self):
        writer = JournalWriter(
            self.path, run_id="run-1",
            clock=lambda: "2026-07-06T00:00:00Z")
        writer.append("evt", {"value": "AAAA"})
        reader = IncrementalJournalReader(self.path)
        reader.read()
        original = self.path.read_bytes()
        self.path.write_bytes(original.replace(b"AAAA", b"BBBB"))

        with self.assertRaises(JournalCorruption):
            reader.read()

    def test_same_size_prefix_mutation_with_partial_tail_is_not_hidden(self):
        writer = JournalWriter(
            self.path, run_id="run-1",
            clock=lambda: "2026-07-06T00:00:00Z")
        writer.append("evt", {"value": "AAAA"})
        reader = IncrementalJournalReader(self.path)
        reader.read()
        with open(self.path, "ab") as fh:
            fh.write(b'{"partial":')
        self.assertEqual(reader.read(), replay(self.path))

        original = self.path.read_bytes()
        self.path.write_bytes(original.replace(b"AAAA", b"BBBB"))

        with self.assertRaises(JournalCorruption):
            replay(self.path)
        with self.assertRaises(JournalCorruption):
            reader.read()

    def test_same_inode_truncate_and_larger_rewrite_forces_full_reread(self):
        writer = JournalWriter(
            self.path, run_id="run-1",
            clock=lambda: "2026-07-06T00:00:00Z")
        writer.append("evt", {"value": "AAAA"})
        reader = IncrementalJournalReader(self.path)
        reader.read()
        original_inode = self.path.stat().st_ino

        replacement = self.path.with_name("replacement.jsonl")
        replacement_writer = JournalWriter(
            replacement, run_id="run-1",
            clock=lambda: "2026-07-06T00:00:00Z")
        replacement_writer.append("evt", {"value": "BBBB"})
        replacement_writer.append("evt", {"value": "CCCC"})
        self.path.write_bytes(replacement.read_bytes())
        self.assertEqual(self.path.stat().st_ino, original_inode)

        self.assertEqual(reader.read(), replay(self.path))
        self.assertEqual([row["value"] for row in reader.read()], ["BBBB", "CCCC"])

    def test_path_replacement_between_metadata_and_open_never_yields_hybrid(self):
        writer = JournalWriter(
            self.path, run_id="run-1",
            clock=lambda: "2026-07-06T00:00:00Z")
        writer.append("evt", {"value": "AAAA"})
        reader = IncrementalJournalReader(self.path)
        reader.read()
        writer.append("evt", {"value": "DDDD"})

        replacement = self.path.with_name("replacement.jsonl")
        replacement_writer = JournalWriter(
            replacement, run_id="run-1",
            clock=lambda: "2026-07-06T00:00:00Z")
        replacement_writer.append("evt", {"value": "BBBB"})
        replacement_writer.append("evt", {"value": "CCCC"})
        real_open = open
        replaced = False

        def replace_before_open(path, *args, **kwargs):
            nonlocal replaced
            mode = args[0] if args else kwargs.get("mode", "r")
            if Path(path) == self.path and mode == "rb" and not replaced:
                os.replace(replacement, self.path)
                replaced = True
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=replace_before_open):
            rows = reader.read()

        self.assertTrue(replaced)
        self.assertEqual(rows, replay(self.path))
        self.assertEqual([row["value"] for row in rows], ["BBBB", "CCCC"])

    def test_path_replacement_during_descriptor_read_retries_stably(self):
        writer = JournalWriter(
            self.path, run_id="run-1",
            clock=lambda: "2026-07-06T00:00:00Z")
        writer.append("evt", {"value": "AAAA"})
        reader = IncrementalJournalReader(self.path)
        reader.read()
        writer.append("evt", {"value": "DDDD"})

        replacement = self.path.with_name("replacement.jsonl")
        replacement_writer = JournalWriter(
            replacement, run_id="run-1",
            clock=lambda: "2026-07-06T00:00:00Z")
        replacement_writer.append("evt", {"value": "BBBB"})
        replacement_writer.append("evt", {"value": "CCCC"})
        real_open = open
        replaced = False

        class ReplacingReader:
            def __init__(inner_self, fh):
                inner_self._fh = fh

            def __enter__(inner_self):
                inner_self._fh.__enter__()
                return inner_self

            def __exit__(inner_self, *args):
                return inner_self._fh.__exit__(*args)

            def __getattr__(inner_self, name):
                return getattr(inner_self._fh, name)

            def read(inner_self, *args):
                nonlocal replaced
                if not replaced:
                    os.replace(replacement, self.path)
                    replaced = True
                return inner_self._fh.read(*args)

        def replacing_read_open(path, *args, **kwargs):
            fh = real_open(path, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "r")
            if Path(path) == self.path and mode == "rb" and not replaced:
                return ReplacingReader(fh)
            return fh

        with mock.patch("builtins.open", side_effect=replacing_read_open):
            rows = reader.read()

        self.assertTrue(replaced)
        self.assertEqual(rows, replay(self.path))
        self.assertEqual([row["value"] for row in rows], ["BBBB", "CCCC"])

    def test_true_append_parses_only_new_rows(self):
        writer = JournalWriter(
            self.path, run_id="run-1",
            clock=lambda: "2026-07-06T00:00:00Z")
        writer.append("evt", {"n": 1})
        reader = IncrementalJournalReader(self.path)
        reader.read()
        old_size = self.path.stat().st_size
        writer.append("evt", {"n": 2})
        appended_size = self.path.stat().st_size - old_size
        real_open = open
        seeks = []
        read_sizes = []

        class TrackingReader:
            def __init__(self, fh):
                self._fh = fh

            def __enter__(self):
                self._fh.__enter__()
                return self

            def __exit__(self, *args):
                return self._fh.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self._fh, name)

            def seek(self, offset, whence=0):
                seeks.append((offset, whence))
                return self._fh.seek(offset, whence)

            def read(self, *args):
                data = self._fh.read(*args)
                read_sizes.append(len(data))
                return data

        def tracking_open(path, *args, **kwargs):
            fh = real_open(path, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "r")
            if Path(path) == self.path and mode == "rb":
                return TrackingReader(fh)
            return fh

        with mock.patch("builtins.open", side_effect=tracking_open):
            with mock.patch("agent.journal.json.loads", wraps=json.loads) as loads:
                rows = reader.read()

        self.assertEqual([row["n"] for row in rows], [1, 2])
        self.assertEqual(loads.call_count, 1)
        self.assertEqual(seeks, [(old_size, 0)])
        self.assertEqual(read_sizes, [appended_size])

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


class TestIncrementalReadNew(_Tmp):
    """read_new() is the tick loop's DELTA API: O(new rows) copying per call
    instead of read()'s full-list deepcopy (which reintroduced an O(day²)
    copy term). Contract: (snapshot_replaced, new_rows) — accumulate
    ``rows if replaced else acc + rows`` and the accumulation always equals
    replay(path). Integrity semantics identical to read()."""

    def _writer(self):
        return JournalWriter(self.path, run_id="run-1",
                             clock=lambda: "2026-07-06T00:00:00Z")

    def test_first_call_serves_everything_as_replaced(self):
        w = self._writer()
        w.append("evt", {"n": 1})
        w.append("evt", {"n": 2})
        reader = IncrementalJournalReader(self.path)

        replaced, rows = reader.read_new()

        self.assertTrue(replaced)
        self.assertEqual(rows, replay(self.path))

    def test_append_only_serves_delta_not_full_list(self):
        w = self._writer()
        for n in range(3):
            w.append("evt", {"n": n})
        reader = IncrementalJournalReader(self.path)
        reader.read_new()

        w.append("evt", {"n": 3})
        replaced, rows = reader.read_new()

        self.assertFalse(replaced)
        self.assertEqual([r["n"] for r in rows], [3])

    def test_unchanged_serves_empty_delta(self):
        w = self._writer()
        w.append("evt", {"n": 1})
        reader = IncrementalJournalReader(self.path)
        reader.read_new()

        replaced, rows = reader.read_new()

        self.assertFalse(replaced)
        self.assertEqual(rows, [])

    def test_external_shrink_serves_full_replacement(self):
        w = self._writer()
        w.append("evt", {"n": 1})
        first_row_bytes = self.path.read_bytes()
        w.append("evt", {"n": 2})
        reader = IncrementalJournalReader(self.path)
        acc = []
        replaced, rows = reader.read_new()
        acc = rows if replaced else acc + rows

        self.path.write_bytes(first_row_bytes)        # external shrink
        replaced, rows = reader.read_new()
        acc = rows if replaced else acc + rows

        self.assertTrue(replaced)
        self.assertEqual(acc, replay(self.path))
        self.assertEqual([r["n"] for r in acc], [1])

    def test_full_reparse_via_read_marks_next_read_new_replaced(self):
        w = self._writer()
        w.append("evt", {"n": 1})
        first_row_bytes = self.path.read_bytes()
        w.append("evt", {"n": 2})
        reader = IncrementalJournalReader(self.path)
        reader.read_new()                             # cursor at 2 rows

        self.path.write_bytes(first_row_bytes)        # external shrink
        self.assertEqual(len(reader.read()), 1)       # read() full-reparses

        replaced, rows = reader.read_new()
        self.assertTrue(replaced)
        self.assertEqual([r["n"] for r in rows], [1])

    def test_returned_delta_rows_are_copies(self):
        w = self._writer()
        w.append("evt", {"nested": {"value": 1}})
        reader = IncrementalJournalReader(self.path)

        _, rows = reader.read_new()
        rows[0]["nested"]["value"] = 999

        self.assertEqual(reader.read(), replay(self.path))

    def test_corruption_still_raises_from_read_new(self):
        w = self._writer()
        w.append("evt", {"value": "AAAA"})
        reader = IncrementalJournalReader(self.path)
        reader.read_new()

        original = self.path.read_bytes()
        self.path.write_bytes(original.replace(b"AAAA", b"BBBB"))

        with self.assertRaises(JournalCorruption):
            reader.read_new()


if __name__ == "__main__":
    unittest.main()
