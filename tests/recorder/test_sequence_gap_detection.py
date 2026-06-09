"""recorder.py sequence/gap/heartbeat detection — S4 inputs (contract §N).

These cases drive the recorder loop's anomaly detection: a real vendor_seq gap
fires a `data_quality_alert` (and keeps running), a reset-to-zero after a
reconnect is NOT a gap, the NONE policy never fires (composite feed), heartbeat
staleness is flagged via the injected FakeClock, and reconnect_epoch is bumped on
each disconnect and stamped on every subsequent parsed event.

§N cases bound here (S4 inputs):
  - test_gap_fires_on_injected_seq_jump        — flaky_transport_gap.jsonl 2001->2004
  - test_seq_reset_to_zero_is_not_a_gap        — equs_mini_sequence_zero_sample.jsonl
  - test_gap_detection_disabled_when_policy_none — policy=NONE never fires
  - test_heartbeat_stale_after_timeout         — staleness via FakeClock
  - test_reconnect_epoch_stamped_on_events     — epoch bumps + on each event

Offline, deterministic: Fake/Flaky transport + injected FakeClock + injected
async sleep (no real sleep, no network, no wall clock).
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from recorder.event import parse
from recorder.persistence import (
    STREAM_DATA_QUALITY,
    EventWriter,
    replay_stream,
)
from recorder.recorder import BackoffPolicy, Recorder, RecorderStats
from recorder.status import HeartbeatMonitor, SequencePolicy, SequenceTracker
from tests.lib.fakes import FakeClock, FakeTransport, FlakyTransport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "databento"

DATASET = "<DEPTH_DATASET>"
SCHEMA = "mbp-10"
SYMBOL = "AAPL"


def _load_frames(name):
    """Load a fixture file as a list of parsed JSON dicts (DATA + _control rows)."""
    path = FIXTURES / name
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


class _RecordingSleep:
    """Injected async sleep that records its delays and never really sleeps.

    Also advances the injected FakeClock by the requested delay so the recorder's
    disconnect-duration accounting is deterministic without a wall clock.
    """

    def __init__(self, clock=None):
        self.delays = []
        self._clock = clock

    async def __call__(self, delay_ms):
        self.delays.append(delay_ms)
        if self._clock is not None:
            self._clock.advance(delay_ms)


class _Tmp(unittest.TestCase):
    """Fresh temp dir per test so the path-keyed journal seq registry does not
    bleed state across tests (mirrors test_persistence.py:_Tmp)."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _events_path(self):
        return self.root / "events.jsonl"

    def _alerts_path(self):
        return self.root / "data_quality_alerts.jsonl"

    def _writers(self, *, run_id="run-1", clock=None):
        stamp = (lambda: "2026-06-09T13:30:00.000000Z") if clock is None else clock
        events = EventWriter(self._events_path(), run_id=run_id, clock=stamp)
        alerts = EventWriter(self._alerts_path(), run_id=run_id, clock=stamp)
        return events, alerts


class TestSequenceGapFires(_Tmp):
    def test_gap_fires_on_injected_seq_jump(self):
        # flaky_transport_gap.jsonl: 2001 -> (disconnect/reconnect) -> 2004.
        # The MONOTONIC tracker observes the post-reconnect 2004 and reports a gap.
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        transport = FlakyTransport(_load_frames("flaky_transport_gap.jsonl"))
        events_w, alerts_w = self._writers()
        rec = Recorder(
            transport, events_w,
            dataset=DATASET, schema=SCHEMA, symbols=(SYMBOL,),
            clock=clock, sleep=sleep,
            backoff=BackoffPolicy(),
            sequence_tracker=SequenceTracker(SYMBOL, policy=SequencePolicy.MONOTONIC),
            heartbeat=HeartbeatMonitor(timeout_ms=30000, clock=clock),
            alert_writer=alerts_w,
        )
        stats = asyncio.run(rec.run(max_events=2))
        self.assertIsInstance(stats, RecorderStats)

        alerts = replay_stream(self._alerts_path())
        gap_alerts = [a for a in alerts if a.get("cause") == "sequence_gap"]
        self.assertEqual(len(gap_alerts), 1, f"expected exactly one gap alert, got {alerts}")
        gap = gap_alerts[0]
        self.assertEqual(gap["symbol"], SYMBOL)
        self.assertEqual(gap["event_type"], STREAM_DATA_QUALITY)
        # gap_size: expected 2002, got 2004 -> 2 missing (carried in `detail`).
        self.assertEqual(gap["detail"]["gap_size"], 2)
        self.assertEqual(gap["detail"]["expected_seq"], 2002)
        self.assertEqual(gap["detail"]["got_seq"], 2004)

    def test_gap_does_not_stop_the_loop(self):
        # A gap is logged, not fatal: both events are still written.
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        transport = FlakyTransport(_load_frames("flaky_transport_gap.jsonl"))
        events_w, alerts_w = self._writers()
        rec = Recorder(
            transport, events_w,
            dataset=DATASET, schema=SCHEMA, symbols=(SYMBOL,),
            clock=clock, sleep=sleep,
            sequence_tracker=SequenceTracker(SYMBOL, policy=SequencePolicy.MONOTONIC),
            heartbeat=HeartbeatMonitor(timeout_ms=30000, clock=clock),
            alert_writer=alerts_w,
        )
        stats = asyncio.run(rec.run(max_events=2))
        self.assertEqual(stats.events_written, 2)
        rows = replay_stream(self._events_path())
        self.assertEqual([r["vendor_seq"] for r in rows], [2001, 2004])


class TestSeqResetIsNotAGap(_Tmp):
    def test_seq_reset_to_zero_is_not_a_gap(self):
        # equs_mini_sequence_zero_sample.jsonl: 1050 then vendor_seq 0.
        # A reset-to-zero is a reconnect/epoch marker, NOT a gap.
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        frames = _load_frames("equs_mini_sequence_zero_sample.jsonl")
        transport = FakeTransport([json.dumps(f).encode("utf-8") for f in frames])
        events_w, alerts_w = self._writers()
        rec = Recorder(
            transport, events_w,
            dataset="EQUS.MINI", schema="tbbo", symbols=(SYMBOL,),
            clock=clock, sleep=sleep,
            sequence_tracker=SequenceTracker(SYMBOL, policy=SequencePolicy.MONOTONIC),
            heartbeat=HeartbeatMonitor(timeout_ms=30000, clock=clock),
            alert_writer=alerts_w,
        )
        asyncio.run(rec.run(max_events=2))
        alerts = replay_stream(self._alerts_path())
        causes = [a.get("cause") for a in alerts]
        self.assertNotIn("sequence_gap", causes,
                         f"reset-to-zero must NOT fire a gap; alerts={alerts}")
        # reset_to_zero IS surfaced as its own alert (not silent).
        self.assertIn("reset_to_zero", causes)


class TestOutOfOrderIsNotANegativeGap(_Tmp):
    def test_out_of_order_fires_out_of_order_alert_not_a_gap(self):
        # out_of_order_seq_sample.jsonl: vendor_seq 2001 then 1999 (backward).
        # C1: a backward jump is kind='out_of_order' -> cause='out_of_order',
        # NOT a 'sequence_gap' with a negative gap_size.
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        frames = _load_frames("out_of_order_seq_sample.jsonl")
        transport = FakeTransport([json.dumps(f).encode("utf-8") for f in frames])
        events_w, alerts_w = self._writers()
        rec = Recorder(
            transport, events_w,
            dataset=DATASET, schema=SCHEMA, symbols=(SYMBOL,),
            clock=clock, sleep=sleep,
            sequence_tracker=SequenceTracker(SYMBOL, policy=SequencePolicy.MONOTONIC),
            heartbeat=HeartbeatMonitor(timeout_ms=30000, clock=clock),
            alert_writer=alerts_w,
        )
        asyncio.run(rec.run(max_events=2))
        alerts = replay_stream(self._alerts_path())
        causes = [a.get("cause") for a in alerts]
        self.assertNotIn("sequence_gap", causes,
                         f"backward seq must NOT be a gap; alerts={alerts}")
        ooo = [a for a in alerts if a.get("cause") == "out_of_order"]
        self.assertEqual(len(ooo), 1, f"expected one out_of_order alert; got {alerts}")
        self.assertEqual(ooo[0]["symbol"], SYMBOL)
        self.assertEqual(ooo[0]["event_type"], STREAM_DATA_QUALITY)
        self.assertEqual(ooo[0]["detail"]["got_seq"], 1999)
        # No negative gap_size anywhere (C1 invariant).
        for a in alerts:
            gs = a.get("detail", {}).get("gap_size") if isinstance(a.get("detail"), dict) else None
            if gs is not None:
                self.assertGreater(gs, 0, "gap_size must never be negative")


class TestNullVendorSeqDoesNotCrash(_Tmp):
    def test_null_vendor_seq_keeps_loop_running_and_alerts(self):
        # D2 (R2#2): a malformed/null vendor_seq mid-stream under MONOTONIC must
        # NOT raise a TypeError out of run(). The loop CONTINUES, both events are
        # written, and a data_quality_alert surfaces the null seq.
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        frames = [
            {"dataset": "EQUS.MINI", "schema": "tbbo", "instrument_id": 1001,
             "symbol": SYMBOL, "vendor_seq": 5,
             "ts_event": "2026-06-09T13:30:00.000000Z",
             "bid_px": "201.2000", "bid_sz": "100",
             "ask_px": "201.2100", "ask_sz": "100"},
            {"dataset": "EQUS.MINI", "schema": "tbbo", "instrument_id": 1001,
             "symbol": SYMBOL, "vendor_seq": None,
             "ts_event": "2026-06-09T13:30:01.000000Z",
             "bid_px": "201.2000", "bid_sz": "100",
             "ask_px": "201.2200", "ask_sz": "120"},
        ]
        transport = FakeTransport([json.dumps(f).encode("utf-8") for f in frames])
        events_w, alerts_w = self._writers()
        rec = Recorder(
            transport, events_w,
            dataset="EQUS.MINI", schema="tbbo", symbols=(SYMBOL,),
            clock=clock, sleep=sleep,
            sequence_tracker=SequenceTracker(SYMBOL, policy=SequencePolicy.MONOTONIC),
            heartbeat=HeartbeatMonitor(timeout_ms=30000, clock=clock),
            alert_writer=alerts_w,
        )
        # Must not raise (was a TypeError out of run() before D2).
        stats = asyncio.run(rec.run(max_events=2))
        self.assertEqual(stats.events_written, 2, "loop must keep running past the null seq")
        alerts = replay_stream(self._alerts_path())
        causes = [a.get("cause") for a in alerts]
        self.assertTrue(any(c in ("sequence_gap", "malformed_seq") for c in causes),
                        f"a null vendor_seq must surface a data_quality_alert; got {alerts}")


class TestMalformedFrameDoesNotKillIngest(_Tmp):
    def _run_three_frame_stream(self, bad_frame):
        # Helper: good / <bad_frame> / good. parse() raises a fail-closed-family
        # error on the middle frame; E3 requires run() to catch it for that SINGLE
        # frame, emit a malformed_record alert, and CONTINUE so the two good frames
        # are still recorded.
        good_a = {"dataset": "EQUS.MINI", "schema": "tbbo", "instrument_id": 1001,
                  "symbol": SYMBOL, "vendor_seq": 5,
                  "ts_event": "2026-06-09T13:30:00.000000Z",
                  "bid_px": "201.2000", "bid_sz": "100",
                  "ask_px": "201.2100", "ask_sz": "100"}
        good_b = {"dataset": "EQUS.MINI", "schema": "tbbo", "instrument_id": 1001,
                  "symbol": SYMBOL, "vendor_seq": 7,
                  "ts_event": "2026-06-09T13:30:02.000000Z",
                  "bid_px": "201.2000", "bid_sz": "100",
                  "ask_px": "201.2200", "ask_sz": "120"}
        frames = [good_a, bad_frame, good_b]
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        transport = FakeTransport([json.dumps(f).encode("utf-8") for f in frames])
        events_w, alerts_w = self._writers()
        rec = Recorder(
            transport, events_w,
            dataset="EQUS.MINI", schema="tbbo", symbols=(SYMBOL,),
            clock=clock, sleep=sleep,
            sequence_tracker=SequenceTracker(SYMBOL, policy=SequencePolicy.MONOTONIC),
            heartbeat=HeartbeatMonitor(timeout_ms=30000, clock=clock),
            alert_writer=alerts_w,
        )
        # Must NOT raise out of run() (pre-E3 the parse error propagated and stopped
        # recording for the whole universe). max_events=2 == the two GOOD frames, so
        # run() returns the moment the second good event is written (mirrors the D2
        # null-seq test) — the malformed frame in between is skipped, not counted.
        stats = asyncio.run(rec.run(max_events=2))
        return stats, replay_stream(self._events_path()), replay_stream(self._alerts_path())

    def test_malformed_vendor_seq_frame_does_not_stop_ingest(self):
        # E3 (R3#3, MAJOR): a non-null malformed vendor_seq (vendor_seq='BAD') makes
        # parse() raise MalformedRecord. Pre-E3 it propagated out of run() and
        # stopped recording for ALL symbols. E3: catch it for the single frame, emit
        # a malformed_record alert, and continue -> the two good frames survive.
        bad = {"dataset": "EQUS.MINI", "schema": "tbbo", "instrument_id": 1001,
               "symbol": SYMBOL, "vendor_seq": "BAD",
               "ts_event": "2026-06-09T13:30:01.000000Z",
               "bid_px": "201.2000", "bid_sz": "100",
               "ask_px": "201.2100", "ask_sz": "100"}
        stats, rows, alerts = self._run_three_frame_stream(bad)
        self.assertEqual(stats.events_written, 2,
                         "the two good frames must survive one malformed frame")
        self.assertEqual([r["vendor_seq"] for r in rows], [5, 7])
        malformed = [a for a in alerts if a.get("cause") == "malformed_record"]
        self.assertEqual(len(malformed), 1,
                         f"one malformed_record alert expected; got {alerts}")
        self.assertEqual(malformed[0]["event_type"], STREAM_DATA_QUALITY)

    def test_malformed_price_frame_does_not_stop_ingest(self):
        # E3 (R3#3, MAJOR): a malformed price (sub-penny '0.00005' -> PrecisionLoss)
        # in the middle frame must likewise NOT kill ingest: a malformed_record
        # alert is written and the two good frames survive.
        bad = {"dataset": "EQUS.MINI", "schema": "tbbo", "instrument_id": 1001,
               "symbol": SYMBOL, "vendor_seq": 6,
               "ts_event": "2026-06-09T13:30:01.000000Z",
               "bid_px": "0.00005", "bid_sz": "100",
               "ask_px": "201.2100", "ask_sz": "100"}
        stats, rows, alerts = self._run_three_frame_stream(bad)
        self.assertEqual(stats.events_written, 2,
                         "the two good frames must survive a malformed-price frame")
        self.assertEqual([r["vendor_seq"] for r in rows], [5, 7])
        malformed = [a for a in alerts if a.get("cause") == "malformed_record"]
        self.assertEqual(len(malformed), 1,
                         f"one malformed_record alert expected; got {alerts}")

    def test_book_state_error_frame_does_not_stop_ingest(self):
        # F1 (R4#1, MAJOR): a frame that PARSES fine but mismatches the symbol's
        # book instrument_id (symbol recycling / corporate-action remap — a real
        # vendor condition since equity symbol strings are not stable ids) raises
        # BookStateError in EquityBookState.apply_quote/_check_identity — OUTSIDE the
        # E3 parse-only try. Pre-F1 it propagated out of run() and killed ingest for
        # the WHOLE universe. F1: catch BookStateError for the SINGLE frame, emit a
        # loud book_state_error alert (carrying symbol+instrument_id), and CONTINUE so
        # the two well-formed frames (iid=1001) survive.
        #
        # 3-frame single-symbol AAPL stream: iid=1001, iid=9999, iid=1001. Frame 1
        # creates the per-symbol book at iid=1001; frame 2 (same symbol, iid=9999)
        # hits the existing book and trips _check_identity; frame 3 (iid=1001) is fine.
        good_a = {"dataset": "EQUS.MINI", "schema": "tbbo", "instrument_id": 1001,
                  "symbol": SYMBOL, "vendor_seq": 5,
                  "ts_event": "2026-06-09T13:30:00.000000Z",
                  "bid_px": "201.2000", "bid_sz": "100",
                  "ask_px": "201.2100", "ask_sz": "100"}
        mismatched = {"dataset": "EQUS.MINI", "schema": "tbbo", "instrument_id": 9999,
                      "symbol": SYMBOL, "vendor_seq": 6,
                      "ts_event": "2026-06-09T13:30:01.000000Z",
                      "bid_px": "201.2000", "bid_sz": "100",
                      "ask_px": "201.2100", "ask_sz": "100"}
        good_b = {"dataset": "EQUS.MINI", "schema": "tbbo", "instrument_id": 1001,
                  "symbol": SYMBOL, "vendor_seq": 7,
                  "ts_event": "2026-06-09T13:30:02.000000Z",
                  "bid_px": "201.2000", "bid_sz": "100",
                  "ask_px": "201.2200", "ask_sz": "120"}
        frames = [good_a, mismatched, good_b]
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        transport = FakeTransport([json.dumps(f).encode("utf-8") for f in frames])
        events_w, alerts_w = self._writers()
        rec = Recorder(
            transport, events_w,
            dataset="EQUS.MINI", schema="tbbo", symbols=(SYMBOL,),
            clock=clock, sleep=sleep,
            sequence_tracker=SequenceTracker(SYMBOL, policy=SequencePolicy.MONOTONIC),
            heartbeat=HeartbeatMonitor(timeout_ms=30000, clock=clock),
            alert_writer=alerts_w,
        )
        # Must NOT raise out of run() (pre-F1 the BookStateError propagated and
        # stopped recording for the whole universe). max_events=2 == the two GOOD
        # frames, so run() returns the moment the second good event is written.
        stats = asyncio.run(rec.run(max_events=2))
        self.assertEqual(stats.events_written, 2,
                         "the two good frames must survive one book-state-error frame")
        rows = replay_stream(self._events_path())
        self.assertEqual([r["vendor_seq"] for r in rows], [5, 7])
        alerts = replay_stream(self._alerts_path())
        bse = [a for a in alerts if a.get("cause") == "book_state_error"]
        self.assertEqual(len(bse), 1,
                         f"exactly one book_state_error alert expected; got {alerts}")
        self.assertEqual(bse[0]["event_type"], STREAM_DATA_QUALITY)
        # The alert carries symbol + the offending instrument_id (F1 binding).
        self.assertEqual(bse[0]["symbol"], SYMBOL)
        self.assertEqual(bse[0]["detail"]["instrument_id"], 9999)
        # No sequence_gap: the mismatched frame's seq (6) was never observed by the
        # tracker (it died before _process_event), so 5 -> 7 IS a gap... actually 6
        # is missing so 5->7 reports a gap. Assert the gap is the only seq anomaly
        # and book_state_error is recorded distinctly.
        self.assertNotIn("malformed_record", [a.get("cause") for a in alerts],
                         "a valid-parse frame must NOT be a malformed_record")


class TestSeqTrackerAdvancesOnlyForPersistedFrames(_Tmp):
    def test_book_state_error_frame_does_not_advance_seq_tracker(self):
        # G1 (R5#1, MAJOR): the per-symbol SequenceTracker baseline must advance
        # ONLY for frames that are actually PERSISTED. Pre-G1, _process_event ran
        # _detect_sequence(ev) BEFORE _maybe_book_hash(ev); a frame that raises
        # BookStateError (the F1 path) had ALREADY advanced the tracker baseline yet
        # was never written -> tracker/journal divergence (the next seq's gap is
        # swallowed, gap_size wrong, S3 invariant broken).
        #
        # Single-symbol AAPL stream: iid=1 seq=10, iid=2 seq=11 (BookStateError),
        # iid=1 seq=12. Frame 1 mints the book at iid=1 (baseline -> 10); frame 2
        # (same symbol, iid=2) trips _check_identity -> BookStateError BEFORE persist,
        # so the baseline MUST stay 10; frame 3 (iid=1, seq=12) is therefore a gap
        # (expected 11, got 12 -> gap_size 1), NOT swallowed.
        def quote(iid, seq):
            return {"dataset": "EQUS.MINI", "schema": "tbbo", "instrument_id": iid,
                    "symbol": SYMBOL, "vendor_seq": seq,
                    "ts_event": "2026-06-09T13:30:00.000000Z",
                    "bid_px": "201.2000", "bid_sz": "100",
                    "ask_px": "201.2100", "ask_sz": "100"}
        frames = [quote(1, 10), quote(2, 11), quote(1, 12)]
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        transport = FakeTransport([json.dumps(f).encode("utf-8") for f in frames])
        events_w, alerts_w = self._writers()
        rec = Recorder(
            transport, events_w,
            dataset="EQUS.MINI", schema="tbbo", symbols=(SYMBOL,),
            clock=clock, sleep=sleep,
            sequence_tracker=SequenceTracker(SYMBOL, policy=SequencePolicy.MONOTONIC),
            heartbeat=HeartbeatMonitor(timeout_ms=30000, clock=clock),
            alert_writer=alerts_w,
        )
        # max_events=2 == the two GOOD (persisted) frames (10 and 12); the thrown
        # frame in between is skipped, not counted (mirrors the F1 test).
        stats = asyncio.run(rec.run(max_events=2))
        self.assertEqual(stats.events_written, 2,
                         "the two well-formed frames must survive the thrown frame")
        rows = replay_stream(self._events_path())
        # Only the persisted frames hit the journal: seq 10 then seq 12.
        self.assertEqual([r["vendor_seq"] for r in rows], [10, 12])

        alerts = replay_stream(self._alerts_path())
        # The thrown frame is surfaced as book_state_error (F1), NOT swallowed.
        bse = [a for a in alerts if a.get("cause") == "book_state_error"]
        self.assertEqual(len(bse), 1, f"one book_state_error expected; got {alerts}")
        self.assertEqual(bse[0]["detail"]["instrument_id"], 2)
        # G1: because the thrown frame (seq 11) did NOT advance the tracker, the
        # persisted seq 12 IS a gap (expected 11, got 12 -> gap_size 1). Pre-G1 the
        # baseline was advanced to 11 by the never-persisted frame, so 12 was
        # expected exactly and the gap was swallowed (this is the RED assertion).
        gap_alerts = [a for a in alerts if a.get("cause") == "sequence_gap"]
        self.assertEqual(len(gap_alerts), 1,
                         f"the persisted seq 12 must be a gap (baseline stayed 10); got {alerts}")
        gap = gap_alerts[0]
        self.assertEqual(gap["symbol"], SYMBOL)
        self.assertEqual(gap["detail"]["expected_seq"], 11)
        self.assertEqual(gap["detail"]["got_seq"], 12)
        self.assertEqual(gap["detail"]["gap_size"], 1)


class TestPolicyNoneNeverGaps(_Tmp):
    def test_gap_detection_disabled_when_policy_none(self):
        # policy=NONE (composite feed): a 2001->2004 jump must NOT fire a gap.
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        transport = FlakyTransport(_load_frames("flaky_transport_gap.jsonl"))
        events_w, alerts_w = self._writers()
        rec = Recorder(
            transport, events_w,
            dataset=DATASET, schema=SCHEMA, symbols=(SYMBOL,),
            clock=clock, sleep=sleep,
            sequence_tracker=SequenceTracker(SYMBOL, policy=SequencePolicy.NONE),
            heartbeat=HeartbeatMonitor(timeout_ms=30000, clock=clock),
            alert_writer=alerts_w,
        )
        asyncio.run(rec.run(max_events=2))
        alerts = replay_stream(self._alerts_path())
        self.assertNotIn("sequence_gap", [a.get("cause") for a in alerts])


class TestPerSymbolSequenceTracking(_Tmp):
    def test_interleaved_two_symbols_gap_in_B_not_masked_by_A(self):
        # D6 (R2#6, MAJOR): with a SINGLE shared tracker, an interleaved A/B stream
        # cross-contaminates (B's seqs masked by A's). Per-symbol tracking detects a
        # gap in B independently and attributes the alert to the OBSERVED symbol (B).
        #
        # Interleave: A=10, B=20, A=11, B=25 (B gap 21->25 -> missing 4), A=12.
        # A is perfectly consecutive (10,11,12). Only B has a gap.
        def quote(symbol, iid, seq):
            return {"dataset": "EQUS.MINI", "schema": "tbbo", "instrument_id": iid,
                    "symbol": symbol, "vendor_seq": seq,
                    "ts_event": "2026-06-09T13:30:00.000000Z",
                    "bid_px": "201.2000", "bid_sz": "100",
                    "ask_px": "201.2100", "ask_sz": "100"}
        frames = [
            quote("AAA", 1, 10),
            quote("BBB", 2, 20),
            quote("AAA", 1, 11),
            quote("BBB", 2, 25),   # gap in BBB: expected 21, got 25 -> gap_size 4
            quote("AAA", 1, 12),
        ]
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        transport = FakeTransport([json.dumps(f).encode("utf-8") for f in frames])
        events_w, alerts_w = self._writers()
        rec = Recorder(
            transport, events_w,
            dataset="EQUS.MINI", schema="tbbo", symbols=("AAA", "BBB"),
            clock=clock, sleep=sleep,
            sequence_tracker=SequenceTracker("AAA", policy=SequencePolicy.MONOTONIC),
            heartbeat=HeartbeatMonitor(timeout_ms=30000, clock=clock),
            alert_writer=alerts_w,
        )
        asyncio.run(rec.run(max_events=5))
        alerts = replay_stream(self._alerts_path())
        gap_alerts = [a for a in alerts if a.get("cause") == "sequence_gap"]
        self.assertEqual(len(gap_alerts), 1,
                         f"exactly one gap (in BBB) expected, none for AAA; got {alerts}")
        gap = gap_alerts[0]
        self.assertEqual(gap["symbol"], "BBB",
                         "the gap alert must attribute the OBSERVED symbol (BBB), not AAA")
        self.assertEqual(gap["detail"]["expected_seq"], 21)
        self.assertEqual(gap["detail"]["got_seq"], 25)
        self.assertEqual(gap["detail"]["gap_size"], 4)


class TestHeartbeatStale(_Tmp):
    def test_heartbeat_stale_after_timeout(self):
        # A symbol seen once, then a long silence (clock advances past timeout)
        # flags staleness -> a heartbeat_timeout alert row.
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        # One data frame, then a disconnect that holds long enough for the
        # heartbeat to go stale on reconnect.
        frames = _load_frames("flaky_transport_gap.jsonl")
        transport = FlakyTransport(frames)
        events_w, alerts_w = self._writers()
        rec = Recorder(
            transport, events_w,
            dataset=DATASET, schema=SCHEMA, symbols=(SYMBOL,),
            clock=clock, sleep=sleep,
            # tiny heartbeat timeout so the disconnect backoff sleep crosses it.
            backoff=BackoffPolicy(base_ms=5000, alert_after_ms=10**9),
            sequence_tracker=SequenceTracker(SYMBOL, policy=SequencePolicy.MONOTONIC),
            heartbeat=HeartbeatMonitor(timeout_ms=1000, clock=clock),
            alert_writer=alerts_w,
        )
        asyncio.run(rec.run(max_events=2))
        alerts = replay_stream(self._alerts_path())
        self.assertIn("heartbeat_timeout", [a.get("cause") for a in alerts],
                      f"expected a heartbeat_timeout alert; got {alerts}")


class _AdvancingClock:
    """Injected ms clock that advances by a fixed step on every now_ms() read.

    Deterministic + wall-clock-free: lets a connected (no-disconnect) stream make
    an earlier-seen symbol go quiet/stale as later events advance the clock — the
    D9 connected-quiet heartbeat scenario.
    """

    def __init__(self, start_ms=0, step_ms=0):
        self._ms = int(start_ms)
        self._step = int(step_ms)

    def now_ms(self):
        v = self._ms
        self._ms += self._step
        return v

    def advance(self, ms):
        self._ms += int(ms)
        return self._ms


class TestConnectedQuietHeartbeat(_Tmp):
    def test_quiet_symbol_goes_stale_on_connected_stream(self):
        # D9 (R2#9): a symbol that stops updating WHILE the connection is healthy
        # (no disconnect) must surface a heartbeat_timeout alert. AAA is seen once,
        # then BBB keeps producing while the clock advances past the timeout, so
        # AAA goes stale mid-stream. No reconnect involved.
        def quote(symbol, iid, seq):
            return {"dataset": "EQUS.MINI", "schema": "tbbo", "instrument_id": iid,
                    "symbol": symbol, "vendor_seq": seq,
                    "ts_event": "2026-06-09T13:30:00.000000Z",
                    "bid_px": "201.2000", "bid_sz": "100",
                    "ask_px": "201.2100", "ask_sz": "100"}
        # AAA seen once at the start; then five BBB events advance the clock.
        frames = [quote("AAA", 1, 1)] + [quote("BBB", 2, 100 + i) for i in range(5)]
        clock = _AdvancingClock(start_ms=0, step_ms=1000)  # +1000 ms per now_ms read
        sleep = _RecordingSleep(clock)
        transport = FakeTransport([json.dumps(f).encode("utf-8") for f in frames])
        events_w, alerts_w = self._writers()
        rec = Recorder(
            transport, events_w,
            dataset="EQUS.MINI", schema="tbbo", symbols=("AAA", "BBB"),
            clock=clock, sleep=sleep,
            sequence_tracker=SequenceTracker("AAA", policy=SequencePolicy.MONOTONIC),
            heartbeat=HeartbeatMonitor(timeout_ms=2000, clock=clock),
            alert_writer=alerts_w,
        )
        asyncio.run(rec.run(max_events=6))
        alerts = replay_stream(self._alerts_path())
        hb = [a for a in alerts if a.get("cause") == "heartbeat_timeout"
              and a.get("symbol") == "AAA"]
        self.assertGreaterEqual(
            len(hb), 1,
            f"a quiet symbol on a CONNECTED stream must surface heartbeat_timeout; got {alerts}")


class TestReconnectEpochStamped(_Tmp):
    def test_reconnect_epoch_stamped_on_events(self):
        # The pre-disconnect event carries epoch 0; the post-reconnect event
        # carries epoch 1 (bumped by the disconnect).
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        transport = FlakyTransport(_load_frames("flaky_transport_gap.jsonl"))
        events_w, alerts_w = self._writers()
        rec = Recorder(
            transport, events_w,
            dataset=DATASET, schema=SCHEMA, symbols=(SYMBOL,),
            clock=clock, sleep=sleep,
            sequence_tracker=SequenceTracker(SYMBOL, policy=SequencePolicy.MONOTONIC),
            heartbeat=HeartbeatMonitor(timeout_ms=30000, clock=clock),
            alert_writer=alerts_w,
        )
        asyncio.run(rec.run(max_events=2))
        self.assertEqual(rec.reconnect_epoch, 1)
        rows = replay_stream(self._events_path())
        self.assertEqual(len(rows), 2)
        # Persisted reconnect_epoch rides inside the flat row (from provenance).
        self.assertEqual(rows[0]["reconnect_epoch"], 0)
        self.assertEqual(rows[1]["reconnect_epoch"], 1)


if __name__ == "__main__":
    unittest.main()
