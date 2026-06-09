"""recorder.py always-on reconnect + sustained-disconnect alert — S4 / liveness (§N).

The recorder MUST NOT silently terminate on a disconnect: it catches
TransportDisconnected, bumps reconnect_epoch, sleeps the injected (capped) backoff,
re-calls stream(), and — if the disconnect was held longer than alert_after_ms
(measured on the injected FakeClock) — writes a `prolonged_disconnect`
data_quality_alert ROW (alerts are DATA, not exceptions; spec §5 tier1).

§N cases bound here (S4 input / liveness):
  - test_sustained_disconnect_emits_data_quality_alert — held > alert_after_ms -> alert row
  - test_recorder_does_not_exit_silently_on_disconnect — catches + reconnects, never returns silently
  - test_backoff_is_capped                              — delay = min(cap, base*factor**n); no max_attempts
  - test_reconnect_increments_epoch                     — epoch bumps per reconnect, stamped on events
  - test_no_real_sleep_offline                          — injected sleep recorded; wall clock untouched

Offline, deterministic: FlakyTransport + injected FakeClock + injected async sleep.
"""
import asyncio
import dataclasses
import json
import tempfile
import time
import unittest
from pathlib import Path

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
    path = FIXTURES / name
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


class _RecordingSleep:
    """Injected async sleep: records delays, never really sleeps, advances the
    injected FakeClock so disconnect-duration accounting is deterministic."""

    def __init__(self, clock=None):
        self.delays = []
        self._clock = clock

    async def __call__(self, delay_ms):
        self.delays.append(delay_ms)
        if self._clock is not None:
            self._clock.advance(delay_ms)


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _events_path(self):
        return self.root / "events.jsonl"

    def _alerts_path(self):
        return self.root / "data_quality_alerts.jsonl"

    def _writers(self, *, run_id="run-1"):
        stamp = lambda: "2026-06-09T13:30:00.000000Z"
        events = EventWriter(self._events_path(), run_id=run_id, clock=stamp)
        alerts = EventWriter(self._alerts_path(), run_id=run_id, clock=stamp)
        return events, alerts

    def _recorder(self, *, clock, sleep, backoff, policy=SequencePolicy.MONOTONIC,
                  heartbeat_timeout_ms=30000):
        transport = FlakyTransport(_load_frames("flaky_transport_gap.jsonl"))
        events_w, alerts_w = self._writers()
        return Recorder(
            transport, events_w,
            dataset=DATASET, schema=SCHEMA, symbols=(SYMBOL,),
            clock=clock, sleep=sleep, backoff=backoff,
            sequence_tracker=SequenceTracker(SYMBOL, policy=policy),
            heartbeat=HeartbeatMonitor(timeout_ms=heartbeat_timeout_ms, clock=clock),
            alert_writer=alerts_w,
        )


class TestSustainedDisconnectAlert(_Tmp):
    def test_sustained_disconnect_emits_data_quality_alert(self):
        # Backoff base 70_000 ms > alert_after_ms 60_000 -> the single backoff sleep
        # advances the FakeClock past the prolonged-disconnect threshold, so a
        # `prolonged_disconnect` data_quality_alert row is written.
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        backoff = BackoffPolicy(base_ms=70000, cap_ms=120000, alert_after_ms=60000)
        rec = self._recorder(clock=clock, sleep=sleep, backoff=backoff)
        asyncio.run(rec.run(max_events=2))

        alerts = replay_stream(self._alerts_path())
        prolonged = [a for a in alerts if a.get("cause") == "prolonged_disconnect"]
        self.assertEqual(len(prolonged), 1, f"expected one prolonged_disconnect alert; got {alerts}")
        alert = prolonged[0]
        self.assertEqual(alert["event_type"], STREAM_DATA_QUALITY)
        self.assertGreater(alert["down_ms"], backoff.alert_after_ms)
        self.assertEqual(alert["reconnect_epoch"], 1)

    def test_short_disconnect_does_not_alert(self):
        # Backoff base 100 ms << alert_after_ms 60_000 -> NO prolonged_disconnect alert.
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        backoff = BackoffPolicy(base_ms=100, cap_ms=30000, alert_after_ms=60000)
        rec = self._recorder(clock=clock, sleep=sleep, backoff=backoff)
        asyncio.run(rec.run(max_events=2))
        alerts = replay_stream(self._alerts_path())
        self.assertNotIn("prolonged_disconnect", [a.get("cause") for a in alerts])


class TestMultiAttemptProlongedDisconnect(_Tmp):
    def test_cumulative_downtime_across_attempts_emits_alert(self):
        # D3 (R2#3, MAJOR): a disconnect that requires MULTIPLE reconnect attempts,
        # where NO single attempt's backoff exceeds alert_after_ms but the CUMULATIVE
        # downtime does, MUST emit at least one prolonged_disconnect alert. Measured
        # from a STABLE origin captured ONCE at the first disconnect.
        #
        # Three disconnects in a row, each backoff = 25_000 ms (< alert_after_ms
        # 60_000), cumulative = 75_000 ms (> 60_000). Pre-D3 this emitted 0 alerts.
        def depth(seq):
            return {"dataset": DATASET, "schema": SCHEMA, "instrument_id": 1001,
                    "symbol": SYMBOL, "vendor_seq": seq,
                    "ts_event": "2026-06-09T13:31:00.000000Z",
                    "bids": [["201.3000", "100", 1]], "asks": [["201.3100", "100", 1]]}
        frames = [
            depth(3001),
            {"_control": "disconnect", "after_seq": 3001},
            {"_control": "reconnect"},
            {"_control": "disconnect", "after_seq": 3001},
            {"_control": "reconnect"},
            {"_control": "disconnect", "after_seq": 3001},
            {"_control": "reconnect"},
            depth(3002),
        ]
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        # base 25_000, factor 1 -> every attempt sleeps 25_000 (<60_000); 3 attempts
        # -> cumulative 75_000 > 60_000.
        backoff = BackoffPolicy(base_ms=25000, factor=1, cap_ms=25000, alert_after_ms=60000)
        transport = FlakyTransport(frames)
        events_w, alerts_w = self._writers()
        rec = Recorder(
            transport, events_w,
            dataset=DATASET, schema=SCHEMA, symbols=(SYMBOL,),
            clock=clock, sleep=sleep, backoff=backoff,
            sequence_tracker=SequenceTracker(SYMBOL, policy=SequencePolicy.NONE),
            heartbeat=HeartbeatMonitor(timeout_ms=10**9, clock=clock),
            alert_writer=alerts_w,
        )
        stats = asyncio.run(rec.run(max_events=2))
        self.assertEqual(stats.reconnects, 3, "fixture forces three reconnect attempts")
        alerts = replay_stream(self._alerts_path())
        prolonged = [a for a in alerts if a.get("cause") == "prolonged_disconnect"]
        # E1 (R3#1, MAJOR): a single outage emits EXACTLY ONE prolonged_disconnect.
        # Here the threshold is crossed only on the final attempt; the dedicated
        # E1 regression below forces the multi-crossing re-fire scenario.
        self.assertEqual(
            len(prolonged), 1,
            f"cumulative downtime > alert_after_ms must emit exactly one "
            f"prolonged_disconnect; got {alerts}")
        self.assertGreater(prolonged[-1]["down_ms"], backoff.alert_after_ms)

    def test_prolonged_disconnect_is_one_shot_per_outage(self):
        # E1 (R3#1, MAJOR): prolonged_disconnect must be ONE-SHOT per outage. The
        # D3 origin-stabilisation measures down_ms from a STABLE origin, so once a
        # sustained outage crosses alert_after_ms, EVERY subsequent reconnect
        # attempt of the SAME outage also exceeds it. Pre-E1 the recorder re-fired
        # the alert on each attempt (a 10-min outage -> ~one row per attempt). With
        # the dedup flag the alert is written AT MOST ONCE per outage; the flag
        # clears on a successful receive so the NEXT outage can alert again.
        #
        # One outage, three reconnect attempts, each backoff = 70_000 ms (>
        # alert_after_ms 60_000). So all three attempts cross the threshold. Pre-E1
        # this emitted 3 prolonged_disconnect rows; post-E1 it emits exactly 1.
        def depth(seq):
            return {"dataset": DATASET, "schema": SCHEMA, "instrument_id": 1001,
                    "symbol": SYMBOL, "vendor_seq": seq,
                    "ts_event": "2026-06-09T13:31:00.000000Z",
                    "bids": [["201.3000", "100", 1]], "asks": [["201.3100", "100", 1]]}
        frames = [
            depth(3001),
            {"_control": "disconnect", "after_seq": 3001},
            {"_control": "reconnect"},
            {"_control": "disconnect", "after_seq": 3001},
            {"_control": "reconnect"},
            {"_control": "disconnect", "after_seq": 3001},
            {"_control": "reconnect"},
            depth(3002),
        ]
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        # base 70_000, factor 1 -> every attempt sleeps 70_000 (> 60_000), so EVERY
        # attempt's cumulative down_ms exceeds alert_after_ms (the re-fire trigger).
        backoff = BackoffPolicy(base_ms=70000, factor=1, cap_ms=70000, alert_after_ms=60000)
        transport = FlakyTransport(frames)
        events_w, alerts_w = self._writers()
        rec = Recorder(
            transport, events_w,
            dataset=DATASET, schema=SCHEMA, symbols=(SYMBOL,),
            clock=clock, sleep=sleep, backoff=backoff,
            sequence_tracker=SequenceTracker(SYMBOL, policy=SequencePolicy.NONE),
            heartbeat=HeartbeatMonitor(timeout_ms=10**9, clock=clock),
            alert_writer=alerts_w,
        )
        stats = asyncio.run(rec.run(max_events=2))
        self.assertEqual(stats.reconnects, 3, "fixture forces three reconnect attempts")
        alerts = replay_stream(self._alerts_path())
        prolonged = [a for a in alerts if a.get("cause") == "prolonged_disconnect"]
        self.assertEqual(
            len(prolonged), 1,
            f"one sustained outage crossing alert_after_ms on every attempt must "
            f"emit EXACTLY ONE prolonged_disconnect (one-shot per outage); got {alerts}")


class TestNoSilentExit(_Tmp):
    def test_recorder_does_not_exit_silently_on_disconnect(self):
        # The loop catches TransportDisconnected, reconnects, and writes BOTH the
        # pre- and post-disconnect events. A silent exit would write only the first.
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        backoff = BackoffPolicy(base_ms=250, cap_ms=30000, alert_after_ms=60000)
        rec = self._recorder(clock=clock, sleep=sleep, backoff=backoff)
        stats = asyncio.run(rec.run(max_events=2))
        self.assertIsInstance(stats, RecorderStats)
        self.assertEqual(stats.events_written, 2)
        self.assertEqual(stats.reconnects, 1)
        rows = replay_stream(self._events_path())
        self.assertEqual([r["vendor_seq"] for r in rows], [2001, 2004])


class TestBackoffCapped(_Tmp):
    def test_backoff_is_capped(self):
        # delay = min(cap_ms, base_ms * factor**n). With base 1000, factor 2, cap 1500,
        # the first reconnect delay must be capped at 1500, not 1000*2**0=1000... here
        # n=0 gives 1000 (<=cap), so assert the formula and the cap directly.
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        backoff = BackoffPolicy(base_ms=1000, factor=2, cap_ms=1500, alert_after_ms=10**9)
        rec = self._recorder(clock=clock, sleep=sleep, backoff=backoff)
        asyncio.run(rec.run(max_events=2))
        # Exactly one disconnect in the fixture -> one recorded backoff delay.
        self.assertEqual(len(sleep.delays), 1)
        # n=0: min(1500, 1000*2**0) = 1000.
        self.assertEqual(sleep.delays[0], 1000)

    def test_backoff_policy_has_no_max_attempts(self):
        # The ported 5-attempt cap is STRUCTURALLY ABSENT (spec §5 tier1).
        field_names = {f.name for f in dataclasses.fields(BackoffPolicy)}
        self.assertNotIn("max_attempts", field_names)

    def test_backoff_delay_formula_caps_high_attempts(self):
        # White-box: the recorder's delay helper applies min(cap, base*factor**n).
        backoff = BackoffPolicy(base_ms=250, factor=2, cap_ms=30000)
        delays = [Recorder._backoff_delay_ms(backoff, n) for n in range(0, 12)]
        self.assertEqual(delays[0], 250)
        self.assertEqual(delays[1], 500)
        self.assertTrue(all(d <= 30000 for d in delays))
        self.assertEqual(delays[-1], 30000)  # large n saturates at the cap


class TestReconnectEpoch(_Tmp):
    def test_reconnect_increments_epoch(self):
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        backoff = BackoffPolicy(base_ms=250, alert_after_ms=10**9)
        rec = self._recorder(clock=clock, sleep=sleep, backoff=backoff)
        self.assertEqual(rec.reconnect_epoch, 0)
        asyncio.run(rec.run(max_events=2))
        self.assertEqual(rec.reconnect_epoch, 1)
        rows = replay_stream(self._events_path())
        self.assertEqual(rows[0]["reconnect_epoch"], 0)
        self.assertEqual(rows[1]["reconnect_epoch"], 1)


class TestNoRealSleep(_Tmp):
    def test_no_real_sleep_offline(self):
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        backoff = BackoffPolicy(base_ms=5000, alert_after_ms=10**9)
        rec = self._recorder(clock=clock, sleep=sleep, backoff=backoff)
        wall_before = time.monotonic()
        asyncio.run(rec.run(max_events=2))
        wall_after = time.monotonic()
        # The injected sleep recorded the (large) backoff delay...
        self.assertEqual(sleep.delays, [5000])
        # ...but no real wall-clock time elapsed (no real sleep).
        self.assertLess(wall_after - wall_before, 2.0)


class TestCrossedBookAlert(_Tmp):
    def test_crossed_book_emits_exactly_one_alert(self):
        # C7: a single crossed mbp-10 frame (best_bid 201.20 >= best_ask 201.10)
        # surfaces as exactly one cause='crossed_book' alert row (recorded, not
        # silently normalized). This path previously had ZERO coverage.
        clock = FakeClock(start_ms=0)
        sleep = _RecordingSleep(clock)
        frames = _load_frames("crossed_book_sample.jsonl")
        transport = FakeTransport([json.dumps(f).encode("utf-8") for f in frames])
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
        asyncio.run(rec.run(max_events=1))

        alerts = replay_stream(self._alerts_path())
        crossed = [a for a in alerts if a.get("cause") == "crossed_book"]
        self.assertEqual(len(crossed), 1,
                         f"expected exactly one crossed_book alert; got {alerts}")
        self.assertEqual(crossed[0]["symbol"], SYMBOL)
        self.assertEqual(crossed[0]["event_type"], STREAM_DATA_QUALITY)


if __name__ == "__main__":
    unittest.main()
