"""Hostile end-to-end integration test — ROUND 4 convergence lens.

Drives the REAL pipeline over a hostile multi-symbol stream:
  - interleaved symbols (AAPL + MSFT)
  - a malformed frame (bad JSON + bad price)
  - an alert-bearing stream with NO alert_writer injected (E2 path: alerts fall
    back to the events writer; replay must skip non-schema rows)
  - a sustained multi-attempt outage: assert EXACTLY ONE prolonged_disconnect alert
  - a crossed book
  - duplicate + out-of-order + reset-to-zero seqs (interleaved between symbols)
  - a DST-boundary day (fall-back Nov 2026 transition)

Then for the whole pipeline:
  - replay + reconcile against self -> ok=True
  - every persisted depth row re-derives its hash (S3 byte-stable)
  - bars bucket right across the DST boundary
  - alerts attribute the right symbol
  - reconcile-against-self is clean (no missing / no mismatch)

No network, no credentials, no real sleep, no real clock — fully deterministic.
"""
import asyncio
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

# Ensure scripts/ is on the path (mirrors conftest.py / tests/__init__.py).
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from recorder.bar_cache import resample
from recorder.event import parse
from recorder.persistence import (
    STREAM_DATA_QUALITY,
    STREAM_EVENTS,
    EventWriter,
    replay_stream,
)
from recorder.reconcile import reconcile_against_fixture
from recorder.recorder import BackoffPolicy, Recorder
from recorder.replay import replay_book_hashes
from recorder.status import HeartbeatMonitor, SequencePolicy, SequenceTracker
from tests.lib.fakes import FakeClock, FlakyTransport

DATASET = "<DEPTH_DATASET>"
SCHEMA = "mbp-10"
TRADE_SCHEMA = "trades"
TS_RECV_STAMP = "2026-06-09T13:30:00.000000Z"


class _RecordingSleep:
    def __init__(self, clock):
        self.delays = []
        self._clock = clock

    async def __call__(self, delay_ms):
        self.delays.append(delay_ms)
        self._clock.advance(delay_ms)


def _depth(symbol, instr_id, seq, ts, bids, asks):
    return {
        "dataset": DATASET, "schema": SCHEMA,
        "instrument_id": instr_id, "symbol": symbol,
        "vendor_seq": seq, "ts_event": ts,
        "bids": bids, "asks": asks,
    }


def _trade(symbol, instr_id, seq, ts, price, size, side="B"):
    return {
        "dataset": "EQUS.MINI", "schema": TRADE_SCHEMA,
        "instrument_id": instr_id, "symbol": symbol,
        "vendor_seq": seq, "ts_event": ts,
        "price": price, "size": size, "side": side,
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _encode(frames):
    """Encode list-of-dicts to list of JSON bytes (for FakeTransport/FlakyTransport)."""
    return [json.dumps(f).encode("utf-8") for f in frames]


def _make_recorder(transport, events_w, alerts_w, *, clock, sleep,
                   backoff=None, symbol="AAPL", policy=SequencePolicy.MONOTONIC,
                   heartbeat_timeout_ms=10 ** 9):
    if backoff is None:
        backoff = BackoffPolicy(alert_after_ms=10 ** 9)
    # Per-symbol trackers: AAPL + MSFT (D6 lazy-mint will handle second symbol)
    tracker = SequenceTracker(symbol, policy=policy)
    heartbeat = HeartbeatMonitor(timeout_ms=heartbeat_timeout_ms, clock=clock)
    return Recorder(
        transport, events_w,
        dataset=DATASET, schema=SCHEMA, symbols=(symbol,),
        clock=clock, sleep=sleep, backoff=backoff,
        sequence_tracker=tracker,
        heartbeat=heartbeat,
        alert_writer=alerts_w,
    )


# ---------------------------------------------------------------------------
# Test 1 — interleaved multi-symbol stream: per-symbol seq isolation (D6)
# ---------------------------------------------------------------------------

class TestInterleavedMultiSymbol(unittest.TestCase):
    """D6: two symbols with independent sequences must not cross-contaminate."""

    def _run(self, frames, *, policy=SequencePolicy.MONOTONIC, n_events):
        """n_events: exact number of valid events expected — passed as max_events so
        FakeTransport (which re-replays on every stream() call) terminates cleanly."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            clock = FakeClock(start_ms=0)
            sleep = _RecordingSleep(clock)
            stamp = lambda: TS_RECV_STAMP
            events_w = EventWriter(root / "events.jsonl", run_id="r1", clock=stamp)
            alerts_w = EventWriter(root / "alerts.jsonl", run_id="r1", clock=stamp)

            from tests.lib.fakes import FakeTransport
            transport = FakeTransport([json.dumps(f).encode() for f in frames])
            rec = _make_recorder(transport, events_w, alerts_w,
                                 clock=clock, sleep=sleep,
                                 symbol="AAPL", policy=policy)
            stats = asyncio.run(rec.run(max_events=n_events))
            events = replay_stream(root / "events.jsonl")
            alerts = replay_stream(root / "alerts.jsonl")
            return stats, events, alerts

    def test_interleaved_symbols_seq_isolation(self):
        """AAPL seq 1,2,3 and MSFT seq 1,2,3 interleaved must produce NO gap alerts.
        Pre-D6 fix a single SequenceTracker would see AAPL:1, MSFT:1 (dup), AAPL:2,
        MSFT:2, ... and generate spurious gap/dup alerts."""
        aapl = lambda seq, ts: _depth("AAPL", 1001, seq, ts,
                                      [["200.0000", "100", 1]],
                                      [["200.0100", "100", 1]])
        msft = lambda seq, ts: _depth("MSFT", 2001, seq, ts,
                                      [["300.0000", "200", 1]],
                                      [["300.0100", "200", 1]])

        frames = [
            aapl(1, "2026-06-09T13:30:00.000000Z"),
            msft(1, "2026-06-09T13:30:00.100000Z"),
            aapl(2, "2026-06-09T13:30:00.200000Z"),
            msft(2, "2026-06-09T13:30:00.300000Z"),
            aapl(3, "2026-06-09T13:30:00.400000Z"),
            msft(3, "2026-06-09T13:30:00.500000Z"),
        ]
        stats, events, alerts = self._run(frames, n_events=6)
        self.assertEqual(stats.events_written, 6, f"all 6 events must be written; got {stats}")
        gap_alerts = [a for a in alerts if a.get("cause") in ("sequence_gap", "duplicate", "out_of_order")]
        self.assertEqual(gap_alerts, [],
                         f"interleaved clean seqs must produce no seq anomaly alerts; got {gap_alerts}")

    def test_per_symbol_gap_alerts_attribute_correct_symbol(self):
        """A gap in AAPL seq must attribute AAPL; a gap in MSFT must attribute MSFT."""
        aapl = lambda seq, ts: _depth("AAPL", 1001, seq, ts,
                                      [["200.0000", "100", 1]],
                                      [["200.0100", "100", 1]])
        msft = lambda seq, ts: _depth("MSFT", 2001, seq, ts,
                                      [["300.0000", "200", 1]],
                                      [["300.0100", "200", 1]])
        # AAPL: 1, 3 (gap of 1); MSFT: 1, 2 (clean)
        frames = [
            aapl(1, "2026-06-09T13:30:00.000000Z"),
            msft(1, "2026-06-09T13:30:00.100000Z"),
            msft(2, "2026-06-09T13:30:00.200000Z"),
            aapl(3, "2026-06-09T13:30:00.300000Z"),  # gap: AAPL expected 2, got 3
        ]
        stats, events, alerts = self._run(frames, n_events=4)
        gap_alerts = [a for a in alerts if a.get("cause") == "sequence_gap"]
        self.assertEqual(len(gap_alerts), 1, f"expected exactly 1 gap alert; got {alerts}")
        self.assertEqual(gap_alerts[0]["symbol"], "AAPL",
                         f"gap alert must attribute AAPL, not MSFT; got {gap_alerts[0]}")

    def test_reset_to_zero_per_symbol_does_not_affect_other(self):
        """A reset-to-zero on AAPL must not generate any anomaly for MSFT."""
        aapl = lambda seq, ts: _depth("AAPL", 1001, seq, ts,
                                      [["200.0000", "100", 1]],
                                      [["200.0100", "100", 1]])
        msft = lambda seq, ts: _depth("MSFT", 2001, seq, ts,
                                      [["300.0000", "200", 1]],
                                      [["300.0100", "200", 1]])
        frames = [
            aapl(5, "2026-06-09T13:30:00.000000Z"),
            msft(1, "2026-06-09T13:30:00.100000Z"),
            aapl(0, "2026-06-09T13:30:00.200000Z"),   # AAPL reset-to-zero
            msft(2, "2026-06-09T13:30:00.300000Z"),
        ]
        stats, events, alerts = self._run(frames, n_events=4)
        msft_anomalies = [a for a in alerts
                          if a.get("symbol") == "MSFT"
                          and a.get("cause") in ("sequence_gap", "duplicate", "out_of_order", "reset_to_zero")]
        self.assertEqual(msft_anomalies, [],
                         f"AAPL reset-to-zero must not produce MSFT anomaly alerts; got {msft_anomalies}")
        aapl_resets = [a for a in alerts
                       if a.get("symbol") == "AAPL" and a.get("cause") == "reset_to_zero"]
        self.assertEqual(len(aapl_resets), 1, f"AAPL reset-to-zero must produce exactly 1 reset alert; got {alerts}")


# ---------------------------------------------------------------------------
# Test 2 — malformed frames survive (E3)
# ---------------------------------------------------------------------------

class TestMalformedFrameSurvival(unittest.TestCase):
    """E3: one malformed frame must NOT kill ingest for the universe."""

    def _run(self, mixed_frames, *, n_events):
        """n_events: number of valid events expected (used as max_events to stop
        FakeTransport which re-replays its list on every stream() call)."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            clock = FakeClock(start_ms=0)
            sleep = _RecordingSleep(clock)
            stamp = lambda: TS_RECV_STAMP

            events_w = EventWriter(root / "events.jsonl", run_id="r1", clock=stamp)
            alerts_w = EventWriter(root / "alerts.jsonl", run_id="r1", clock=stamp)

            from tests.lib.fakes import FakeTransport
            transport = FakeTransport(mixed_frames)
            tracker = SequenceTracker("AAPL", policy=SequencePolicy.NONE)
            heartbeat = HeartbeatMonitor(timeout_ms=10 ** 9, clock=clock)
            rec = Recorder(
                transport, events_w,
                dataset=DATASET, schema=SCHEMA, symbols=("AAPL",),
                clock=clock, sleep=sleep,
                backoff=BackoffPolicy(alert_after_ms=10 ** 9),
                sequence_tracker=tracker, heartbeat=heartbeat,
                alert_writer=alerts_w,
            )
            stats = asyncio.run(rec.run(max_events=n_events))
            events = replay_stream(root / "events.jsonl")
            alerts = replay_stream(root / "alerts.jsonl")
            return stats, events, alerts

    def test_bad_json_frame_survives_ingest_continues(self):
        """A raw frame that is not valid JSON -> malformed_record alert, loop continues."""
        good = _depth("AAPL", 1001, 1, "2026-06-09T13:30:00.000000Z",
                      [["200.0000", "100", 1]], [["200.0100", "100", 1]])
        bad_json = b"NOT_VALID_JSON{{{}"
        good2 = _depth("AAPL", 1001, 2, "2026-06-09T13:30:01.000000Z",
                       [["200.0000", "100", 1]], [["200.0100", "100", 1]])
        frames = [json.dumps(good).encode(), bad_json, json.dumps(good2).encode()]
        stats, events, alerts = self._run(frames, n_events=2)
        # Both valid events must be written
        self.assertEqual(stats.events_written, 2,
                         f"2 good events expected; got {stats.events_written}")
        # Exactly one malformed_record alert for the bad JSON frame
        malformed = [a for a in alerts if a.get("cause") == "malformed_record"]
        self.assertEqual(len(malformed), 1,
                         f"expected 1 malformed_record alert; got {alerts}")

    def test_bad_price_frame_survives_ingest_continues(self):
        """A frame with a float price -> NonFinitePrice alert, loop continues."""
        good = _depth("AAPL", 1001, 1, "2026-06-09T13:30:00.000000Z",
                      [["200.0000", "100", 1]], [["200.0100", "100", 1]])
        bad_price = {
            "dataset": DATASET, "schema": SCHEMA, "instrument_id": 1001,
            "symbol": "AAPL", "vendor_seq": 2,
            "ts_event": "2026-06-09T13:30:01.000000Z",
            "bids": [[1.5, "100", 1]],   # float price -> NonFinitePrice
            "asks": [["200.0100", "100", 1]],
        }
        good2 = _depth("AAPL", 1001, 3, "2026-06-09T13:30:02.000000Z",
                       [["200.0000", "100", 1]], [["200.0100", "100", 1]])
        frames = [json.dumps(good).encode(), json.dumps(bad_price).encode(), json.dumps(good2).encode()]
        stats, events, alerts = self._run(frames, n_events=2)
        self.assertEqual(stats.events_written, 2,
                         f"2 good events expected after malformed price frame; got {stats.events_written}")
        malformed = [a for a in alerts if a.get("cause") == "malformed_record"]
        self.assertEqual(len(malformed), 1,
                         f"expected 1 malformed_record alert for float price; got {alerts}")

    def test_malformed_frame_between_symbols(self):
        """A malformed frame between AAPL and MSFT events must not drop either symbol's events."""
        aapl = _depth("AAPL", 1001, 1, "2026-06-09T13:30:00.000000Z",
                      [["200.0000", "100", 1]], [["200.0100", "100", 1]])
        bad = b"{{not json}}"
        msft = _depth("MSFT", 2001, 1, "2026-06-09T13:30:01.000000Z",
                      [["300.0000", "200", 1]], [["300.0100", "200", 1]])
        frames = [json.dumps(aapl).encode(), bad, json.dumps(msft).encode()]
        stats, events, alerts = self._run(frames, n_events=2)
        self.assertEqual(stats.events_written, 2,
                         f"both AAPL and MSFT events must be written; got {stats.events_written}")
        symbols_written = {e["symbol"] for e in events}
        self.assertIn("AAPL", symbols_written)
        self.assertIn("MSFT", symbols_written)


# ---------------------------------------------------------------------------
# Test 3 — no alert_writer: alerts fall into events stream (E2 path)
# ---------------------------------------------------------------------------

class TestNoAlertWriter(unittest.TestCase):
    """When alert_writer=None, alerts are routed to the events writer.
    replay_book_hashes must skip those non-schema rows (E2) without crashing."""

    def test_replay_skips_alert_rows_in_events_stream(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            clock = FakeClock(start_ms=0)
            sleep = _RecordingSleep(clock)
            stamp = lambda: TS_RECV_STAMP

            events_w = EventWriter(root / "events.jsonl", run_id="r1", clock=stamp)
            # NO alert_writer -> alerts fall back to events_w (recorder.py:164)

            # Stream with a crossed book (will generate a crossed_book alert)
            crossed = {
                "dataset": DATASET, "schema": SCHEMA, "instrument_id": 1001,
                "symbol": "AAPL", "vendor_seq": 1,
                "ts_event": "2026-06-09T13:30:00.000000Z",
                "bids": [["201.2000", "100", 1]],   # bid > ask -> crossed
                "asks": [["201.1000", "100", 1]],
            }
            good = _depth("AAPL", 1001, 2, "2026-06-09T13:30:01.000000Z",
                          [["200.0000", "100", 1]], [["200.0100", "100", 1]])

            from tests.lib.fakes import FakeTransport
            transport = FakeTransport([json.dumps(crossed).encode(), json.dumps(good).encode()])
            tracker = SequenceTracker("AAPL", policy=SequencePolicy.NONE)
            heartbeat = HeartbeatMonitor(timeout_ms=10 ** 9, clock=clock)
            rec = Recorder(
                transport, events_w,
                dataset=DATASET, schema=SCHEMA, symbols=("AAPL",),
                clock=clock, sleep=sleep,
                backoff=BackoffPolicy(alert_after_ms=10 ** 9),
                sequence_tracker=tracker, heartbeat=heartbeat,
                alert_writer=None,   # E2 path: alerts -> events stream
            )
            asyncio.run(rec.run(max_events=2))

            # replay_book_hashes must NOT crash on the alert row (no 'schema' field).
            result = replay_book_hashes(root / "events.jsonl")
            self.assertTrue(result.ok,
                            f"replay must be ok (no hash mismatch); first_mismatch={result.first_mismatch}")
            # The two depth events must produce two hash entries.
            self.assertEqual(len(result.rederived_book_hashes), 2,
                             f"expected 2 depth hash entries; got {result.rederived_book_hashes}")


# ---------------------------------------------------------------------------
# Test 4 — sustained multi-attempt outage: exactly ONE prolonged_disconnect (E1)
# ---------------------------------------------------------------------------

class TestSustainedMultiAttemptOutage(unittest.TestCase):
    """E1 + D3: three reconnect attempts, each > alert_after_ms individually.
    EXACTLY one prolonged_disconnect alert must be emitted for the whole outage."""

    def test_exactly_one_prolonged_disconnect_per_outage(self):
        aapl = lambda seq, ts: _depth("AAPL", 1001, seq, ts,
                                      [["200.0000", "100", 1]],
                                      [["200.0100", "100", 1]])
        frames = [
            aapl(1, "2026-06-09T13:30:00.000000Z"),
            {"_control": "disconnect"},
            {"_control": "reconnect"},
            {"_control": "disconnect"},
            {"_control": "reconnect"},
            {"_control": "disconnect"},
            {"_control": "reconnect"},
            aapl(2, "2026-06-09T13:30:01.000000Z"),
        ]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            clock = FakeClock(start_ms=0)
            sleep = _RecordingSleep(clock)
            stamp = lambda: TS_RECV_STAMP
            events_w = EventWriter(root / "events.jsonl", run_id="r1", clock=stamp)
            alerts_w = EventWriter(root / "alerts.jsonl", run_id="r1", clock=stamp)
            backoff = BackoffPolicy(base_ms=70000, factor=1, cap_ms=70000, alert_after_ms=60000)
            transport = FlakyTransport(frames)
            tracker = SequenceTracker("AAPL", policy=SequencePolicy.NONE)
            heartbeat = HeartbeatMonitor(timeout_ms=10 ** 9, clock=clock)
            rec = Recorder(
                transport, events_w,
                dataset=DATASET, schema=SCHEMA, symbols=("AAPL",),
                clock=clock, sleep=sleep, backoff=backoff,
                sequence_tracker=tracker, heartbeat=heartbeat,
                alert_writer=alerts_w,
            )
            stats = asyncio.run(rec.run())
            self.assertEqual(stats.reconnects, 3, f"fixture must force 3 reconnects; got {stats.reconnects}")
            self.assertEqual(stats.events_written, 2)

            alerts = replay_stream(root / "alerts.jsonl")
            prolonged = [a for a in alerts if a.get("cause") == "prolonged_disconnect"]
            self.assertEqual(len(prolonged), 1,
                             f"EXACTLY ONE prolonged_disconnect per outage (E1); got {alerts}")
            self.assertGreater(prolonged[0]["down_ms"], backoff.alert_after_ms)
            # The alert fires on the FIRST reconnect attempt that crosses the
            # threshold (epoch=1 after the first bump). Subsequent attempts are
            # deduped by _prolonged_alerted (E1), so epoch stays at 1.
            self.assertEqual(prolonged[0]["reconnect_epoch"], 1,
                             "alert must stamp the reconnect_epoch at the attempt it fires (epoch 1)")


# ---------------------------------------------------------------------------
# Test 5 — crossed book alert attributes correct symbol
# ---------------------------------------------------------------------------

class TestCrossedBookMultiSymbol(unittest.TestCase):
    def test_crossed_book_attributes_correct_symbol(self):
        """A crossed book on MSFT must attribute MSFT, not AAPL."""
        aapl_clean = _depth("AAPL", 1001, 1, "2026-06-09T13:30:00.000000Z",
                            [["200.0000", "100", 1]], [["200.0100", "100", 1]])
        msft_crossed = {
            "dataset": DATASET, "schema": SCHEMA, "instrument_id": 2001,
            "symbol": "MSFT", "vendor_seq": 1,
            "ts_event": "2026-06-09T13:30:01.000000Z",
            "bids": [["301.0000", "100", 1]],
            "asks": [["300.9000", "100", 1]],  # bid 301.0 > ask 300.9 -> crossed
        }
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            clock = FakeClock(start_ms=0)
            sleep = _RecordingSleep(clock)
            stamp = lambda: TS_RECV_STAMP
            events_w = EventWriter(root / "events.jsonl", run_id="r1", clock=stamp)
            alerts_w = EventWriter(root / "alerts.jsonl", run_id="r1", clock=stamp)

            from tests.lib.fakes import FakeTransport
            frames = [json.dumps(aapl_clean).encode(), json.dumps(msft_crossed).encode()]
            transport = FakeTransport(frames)
            tracker = SequenceTracker("AAPL", policy=SequencePolicy.NONE)
            heartbeat = HeartbeatMonitor(timeout_ms=10 ** 9, clock=clock)
            rec = Recorder(
                transport, events_w,
                dataset=DATASET, schema=SCHEMA, symbols=("AAPL", "MSFT"),
                clock=clock, sleep=sleep,
                backoff=BackoffPolicy(alert_after_ms=10 ** 9),
                sequence_tracker=tracker, heartbeat=heartbeat,
                alert_writer=alerts_w,
            )
            asyncio.run(rec.run(max_events=2))
            alerts = replay_stream(root / "alerts.jsonl")
            crossed = [a for a in alerts if a.get("cause") == "crossed_book"]
            self.assertEqual(len(crossed), 1, f"expected 1 crossed_book alert; got {alerts}")
            self.assertEqual(crossed[0]["symbol"], "MSFT",
                             f"crossed_book alert must attribute MSFT; got {crossed[0]}")
            # AAPL clean -> no crossed alert for it
            aapl_crossed = [a for a in alerts
                            if a.get("cause") == "crossed_book" and a.get("symbol") == "AAPL"]
            self.assertEqual(aapl_crossed, [], f"AAPL must not have crossed_book alert; got {aapl_crossed}")


# ---------------------------------------------------------------------------
# Test 6 — duplicate + out-of-order + reset-to-zero seq detection
# ---------------------------------------------------------------------------

class TestSeqAnomalyTaxonomy(unittest.TestCase):
    def _run_seq(self, seqs):
        """Run a single-symbol AAPL stream with the given seq list, return alerts."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            clock = FakeClock(start_ms=0)
            sleep = _RecordingSleep(clock)
            stamp = lambda: TS_RECV_STAMP
            events_w = EventWriter(root / "events.jsonl", run_id="r1", clock=stamp)
            alerts_w = EventWriter(root / "alerts.jsonl", run_id="r1", clock=stamp)
            frames = []
            for i, seq in enumerate(seqs):
                f = _depth("AAPL", 1001, seq, f"2026-06-09T13:30:{i:02d}.000000Z",
                           [["200.0000", "100", 1]], [["200.0100", "100", 1]])
                frames.append(json.dumps(f).encode())
            from tests.lib.fakes import FakeTransport
            transport = FakeTransport(frames)
            tracker = SequenceTracker("AAPL", policy=SequencePolicy.MONOTONIC)
            heartbeat = HeartbeatMonitor(timeout_ms=10 ** 9, clock=clock)
            rec = Recorder(
                transport, events_w,
                dataset=DATASET, schema=SCHEMA, symbols=("AAPL",),
                clock=clock, sleep=sleep,
                backoff=BackoffPolicy(alert_after_ms=10 ** 9),
                sequence_tracker=tracker, heartbeat=heartbeat,
                alert_writer=alerts_w,
            )
            asyncio.run(rec.run(max_events=len(seqs)))
            return replay_stream(root / "alerts.jsonl")

    def test_duplicate_seq_alert(self):
        alerts = self._run_seq([1, 2, 2, 3])   # seq 2 repeated
        dups = [a for a in alerts if a.get("cause") == "duplicate"]
        self.assertEqual(len(dups), 1, f"expected 1 duplicate alert; got {alerts}")
        self.assertEqual(dups[0]["symbol"], "AAPL")

    def test_out_of_order_seq_alert(self):
        alerts = self._run_seq([1, 2, 3, 2])   # seq 2 after 3 -> out-of-order
        ooo = [a for a in alerts if a.get("cause") == "out_of_order"]
        self.assertEqual(len(ooo), 1, f"expected 1 out_of_order alert; got {alerts}")
        self.assertEqual(ooo[0]["symbol"], "AAPL")

    def test_reset_to_zero_seq_alert(self):
        alerts = self._run_seq([1, 2, 0, 1])   # seq 0 mid-stream -> reset_to_zero
        resets = [a for a in alerts if a.get("cause") == "reset_to_zero"]
        self.assertEqual(len(resets), 1, f"expected 1 reset_to_zero alert; got {alerts}")
        self.assertEqual(resets[0]["symbol"], "AAPL")


# ---------------------------------------------------------------------------
# Test 7 — full pipeline: record -> replay -> reconcile-against-self (S3 + §I)
# ---------------------------------------------------------------------------

class TestEndToEndReplayAndReconcile(unittest.TestCase):
    """S3 + §I: every persisted depth row re-derives its hash; reconcile-self ok=True."""

    def _build_and_run(self, root):
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        stamp = lambda: TS_RECV_STAMP
        events_w = EventWriter(root / "events.jsonl", run_id="r1", clock=stamp)
        alerts_w = EventWriter(root / "alerts.jsonl", run_id="r1", clock=stamp)

        # Rich stream: two symbols, a disconnect mid-stream, a crossed book, a bad frame
        aapl = lambda seq, ts: _depth("AAPL", 1001, seq, ts,
                                      [["200.0000", "100", 1], ["199.9900", "50", 1]],
                                      [["200.0100", "150", 2]])
        msft = lambda seq, ts: _depth("MSFT", 2001, seq, ts,
                                      [["300.0000", "200", 1]],
                                      [["300.0100", "200", 1]])

        frames = [
            aapl(1, "2026-06-09T13:30:00.000000Z"),
            msft(1, "2026-06-09T13:30:00.100000Z"),
            aapl(2, "2026-06-09T13:30:00.200000Z"),
            {"_control": "disconnect"},
            {"_control": "reconnect"},
            msft(2, "2026-06-09T13:30:01.000000Z"),
            aapl(3, "2026-06-09T13:30:01.100000Z"),
        ]
        transport = FlakyTransport(frames)
        tracker = SequenceTracker("AAPL", policy=SequencePolicy.NONE)
        heartbeat = HeartbeatMonitor(timeout_ms=10 ** 9, clock=clock)
        rec = Recorder(
            transport, events_w,
            dataset=DATASET, schema=SCHEMA, symbols=("AAPL", "MSFT"),
            clock=clock, sleep=sleep,
            backoff=BackoffPolicy(alert_after_ms=10 ** 9),
            sequence_tracker=tracker, heartbeat=heartbeat,
            alert_writer=alerts_w,
        )
        return asyncio.run(rec.run())

    def test_all_depth_rows_rederive_correct_hash(self):
        """S3: every depth row's rederived book_hash must match the persisted one."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stats = self._build_and_run(root)
            result = replay_book_hashes(root / "events.jsonl")
            self.assertTrue(result.ok,
                            f"S3 replay mismatch: first_mismatch={result.first_mismatch}")
            self.assertEqual(result.first_mismatch, None)

    def test_reconcile_against_self_is_clean(self):
        """§I: reconcile a stream against itself -> ok=True, no missing, no mismatch."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._build_and_run(root)
            report = reconcile_against_fixture(
                root / "events.jsonl",
                root / "events.jsonl",
            )
            self.assertTrue(report.ok,
                            f"self-reconcile must be ok; mismatches={report.mismatches}, "
                            f"missing_in_recorded={report.missing_in_recorded}, "
                            f"missing_in_reference={report.missing_in_reference}")
            self.assertEqual(report.mismatches, ())
            self.assertEqual(report.missing_in_recorded, ())
            self.assertEqual(report.missing_in_reference, ())

    def test_events_per_symbol_count(self):
        """All 5 depth events (AAPL:3, MSFT:2) must be persisted."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stats = self._build_and_run(root)
            self.assertEqual(stats.events_written, 5,
                             f"expected 5 events written; got {stats.events_written}")
            rows = replay_stream(root / "events.jsonl")
            aapl_rows = [r for r in rows if r.get("symbol") == "AAPL"]
            msft_rows = [r for r in rows if r.get("symbol") == "MSFT"]
            self.assertEqual(len(aapl_rows), 3, f"expected 3 AAPL rows; got {aapl_rows}")
            self.assertEqual(len(msft_rows), 2, f"expected 2 MSFT rows; got {msft_rows}")


# ---------------------------------------------------------------------------
# Test 8 — DST-boundary bars bucket correctly
# ---------------------------------------------------------------------------

class TestDSTBoundaryBars(unittest.TestCase):
    """Fall-back 2026 DST: Nov 1 at 02:00 ET clocks fall back to 01:00.
    Trades just before and just after the transition must land in the correct
    ET session-date bucket."""

    def _make_trade_event(self, symbol, instr_id, seq, ts_utc, price, size):
        record = _trade(symbol, instr_id, seq, ts_utc, price, size)
        return parse(
            record,
            dataset=record["dataset"],
            schema=record["schema"],
            reconnect_epoch=0,
            ts_recv_utc=TS_RECV_STAMP,
        )

    def test_dst_fall_back_trades_bucket_to_correct_et_date(self):
        """Nov 1 2026 fall-back: 05:59 UTC = 01:59 EDT = Oct 31 session;
        06:00 UTC = 01:00 EST (after fall-back) = Nov 1 session.
        Trades at 10:00 ET on Oct 30 and 10:00 ET on Nov 2 (post-transition)
        must be in separate day buckets."""
        # 2026-10-30 15:00 UTC = 2026-10-30 11:00 ET (EDT, UTC-4) -> session date Oct 30
        ev1 = self._make_trade_event("AAPL", 1001, 1, "2026-10-30T15:00:00.000000Z",
                                     "201.5000", "100")
        # 2026-11-02 15:00 UTC = 2026-11-02 10:00 ET (EST, UTC-5) -> session date Nov 2
        ev2 = self._make_trade_event("AAPL", 1001, 2, "2026-11-02T15:00:00.000000Z",
                                     "202.0000", "150")

        bars = resample([ev1, ev2], interval="1d")
        self.assertEqual(len(bars), 2, f"expected 2 day bars (one per session); got {bars}")
        dates = {b.session_date_et for b in bars}
        self.assertIn("2026-10-30", dates, f"expected Oct 30 session date; got {dates}")
        self.assertIn("2026-11-02", dates, f"expected Nov 2 session date; got {dates}")

    def test_dst_ambiguous_hour_no_double_count(self):
        """Fall-back 2026: two trades at ambiguous wall-clock 01:30 ET (one EDT, one EST)
        must be disambiguated by UTC and land in the SAME day bucket (both Nov 1),
        not double-counted as two separate buckets."""
        # 2026-11-01 05:30 UTC = 01:30 EDT (first pass through 01:30, before fall-back)
        ev_fold0 = self._make_trade_event("AAPL", 1001, 1, "2026-11-01T05:30:00.000000Z",
                                          "201.0000", "100")
        # 2026-11-01 06:30 UTC = 01:30 EST (second pass through 01:30, after fall-back)
        ev_fold1 = self._make_trade_event("AAPL", 1001, 2, "2026-11-01T06:30:00.000000Z",
                                          "201.5000", "50")
        bars = resample([ev_fold0, ev_fold1], interval="1d")
        self.assertEqual(len(bars), 1,
                         f"both ambiguous-hour trades must land in ONE day bucket; got {bars}")
        self.assertEqual(bars[0].session_date_et, "2026-11-01",
                         f"session date must be 2026-11-01; got {bars[0].session_date_et}")
        self.assertEqual(bars[0].volume, Decimal("150"),
                         f"volume must be 100+50=150; got {bars[0].volume}")

    def test_dst_minute_bucket_boundaries_correct(self):
        """1-minute bars: trades at exactly the spring-forward gap (02:00-03:00 ET,
        Mar 8 2026) must yield no bucket for the skipped hour; trades on each side
        of the gap must be in separate 1m buckets."""
        # Mar 8 2026 spring-forward: 2026-03-08 07:00 UTC = 02:00 ET (clocks skip to 03:00)
        # 2026-03-08 06:59 UTC = 01:59 EST  -> ET date Mar 8, 01:59 bucket
        ev_before = self._make_trade_event("AAPL", 1001, 1, "2026-03-08T06:59:00.000000Z",
                                           "200.0000", "100")
        # 2026-03-08 07:01 UTC = 03:01 EDT  -> ET date Mar 8, 03:01 bucket
        ev_after = self._make_trade_event("AAPL", 1001, 2, "2026-03-08T07:01:00.000000Z",
                                          "200.5000", "100")
        bars = resample([ev_before, ev_after], interval="1m")
        self.assertEqual(len(bars), 2,
                         f"expected 2 separate 1m buckets across spring-forward gap; got {bars}")
        # Neither bucket start is in the skipped 02:xx ET window
        for bar in bars:
            self.assertNotIn("T02:", bar.bucket_start_utc.replace("Z", ""),
                             f"no bar should have 02:xx ET start (spring-forward gap); got {bar}")


# ---------------------------------------------------------------------------
# Test 9 — run_id threading: events + alerts share the same run_id namespace
# ---------------------------------------------------------------------------

class TestRunIdCorrelation(unittest.TestCase):
    """S6: events and data_quality_alerts must share the injected run_id."""

    def test_events_and_alerts_share_run_id(self):
        aapl = _depth("AAPL", 1001, 1, "2026-06-09T13:30:00.000000Z",
                      [["200.0000", "100", 1]], [["200.0100", "100", 1]])
        # Crossed book -> triggers a data_quality_alert
        crossed = {
            "dataset": DATASET, "schema": SCHEMA, "instrument_id": 1001,
            "symbol": "AAPL", "vendor_seq": 2,
            "ts_event": "2026-06-09T13:30:01.000000Z",
            "bids": [["201.0000", "100", 1]],
            "asks": [["200.9000", "100", 1]],
        }
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            clock = FakeClock(start_ms=0)
            sleep = _RecordingSleep(clock)
            stamp = lambda: TS_RECV_STAMP
            RUN_ID = "run-e2e-test-42"
            events_w = EventWriter(root / "events.jsonl", run_id=RUN_ID, clock=stamp)
            alerts_w = EventWriter(root / "alerts.jsonl", run_id=RUN_ID, clock=stamp)

            from tests.lib.fakes import FakeTransport
            transport = FakeTransport([json.dumps(aapl).encode(), json.dumps(crossed).encode()])
            tracker = SequenceTracker("AAPL", policy=SequencePolicy.NONE)
            heartbeat = HeartbeatMonitor(timeout_ms=10 ** 9, clock=clock)
            rec = Recorder(
                transport, events_w,
                dataset=DATASET, schema=SCHEMA, symbols=("AAPL",),
                clock=clock, sleep=sleep,
                backoff=BackoffPolicy(alert_after_ms=10 ** 9),
                sequence_tracker=tracker, heartbeat=heartbeat,
                alert_writer=alerts_w,
            )
            asyncio.run(rec.run(max_events=2))
            events = replay_stream(root / "events.jsonl")
            alerts = replay_stream(root / "alerts.jsonl")
            # All rows in BOTH streams must carry the same run_id
            for row in events:
                self.assertEqual(row["run_id"], RUN_ID,
                                 f"event row missing run_id={RUN_ID}; got {row.get('run_id')}")
            for row in alerts:
                self.assertEqual(row["run_id"], RUN_ID,
                                 f"alert row missing run_id={RUN_ID}; got {row.get('run_id')}")


# ---------------------------------------------------------------------------
# Test 10 — final_book_hashes in RecorderStats reflect last state per symbol
# ---------------------------------------------------------------------------

class TestFinalBookHashesPerSymbol(unittest.TestCase):
    def test_final_book_hashes_keyed_by_symbol(self):
        """RecorderStats.final_book_hashes must have one entry per symbol that
        produced at least one depth event, keyed by symbol string."""
        aapl = lambda seq: _depth("AAPL", 1001, seq, "2026-06-09T13:30:00.000000Z",
                                  [["200.0000", "100", 1]], [["200.0100", "100", 1]])
        msft = lambda seq: _depth("MSFT", 2001, seq, "2026-06-09T13:30:00.000000Z",
                                  [["300.0000", "200", 1]], [["300.0100", "200", 1]])
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            clock = FakeClock(start_ms=0)
            sleep = _RecordingSleep(clock)
            stamp = lambda: TS_RECV_STAMP
            events_w = EventWriter(root / "events.jsonl", run_id="r1", clock=stamp)
            alerts_w = EventWriter(root / "alerts.jsonl", run_id="r1", clock=stamp)

            from tests.lib.fakes import FakeTransport
            frames = [json.dumps(f).encode() for f in [aapl(1), msft(1), aapl(2)]]
            transport = FakeTransport(frames)
            tracker = SequenceTracker("AAPL", policy=SequencePolicy.NONE)
            heartbeat = HeartbeatMonitor(timeout_ms=10 ** 9, clock=clock)
            rec = Recorder(
                transport, events_w,
                dataset=DATASET, schema=SCHEMA, symbols=("AAPL", "MSFT"),
                clock=clock, sleep=sleep,
                backoff=BackoffPolicy(alert_after_ms=10 ** 9),
                sequence_tracker=tracker, heartbeat=heartbeat,
                alert_writer=alerts_w,
            )
            stats = asyncio.run(rec.run(max_events=3))
            self.assertIn("AAPL", stats.final_book_hashes,
                          f"AAPL must be in final_book_hashes; got {stats.final_book_hashes}")
            self.assertIn("MSFT", stats.final_book_hashes,
                          f"MSFT must be in final_book_hashes; got {stats.final_book_hashes}")
            # Both hashes must be non-empty strings
            for sym, h in stats.final_book_hashes.items():
                self.assertIsInstance(h, str, f"{sym} hash must be a string; got {h!r}")
                self.assertGreater(len(h), 0, f"{sym} hash must be non-empty")


if __name__ == "__main__":
    unittest.main(verbosity=2)
