"""M2 §F — session-aware gap-detection seam (a SCOPED recorder.py edit, no duplication).

The recorder's ``heartbeat_timeout`` alert is session-UNAWARE (``HeartbeatMonitor`` is a
pure ms timer, ``status.py:178``). M2 injects an optional ``SessionLiveness`` predicate that
the recorder consults at BOTH ``_emit_alert(cause="heartbeat_timeout", ...)`` sites
(``recorder.py`` ``_check_connected_quiet`` and ``_reconnect``): a legitimately closed/unknown
session SUPPRESSES the false heartbeat_timeout. ``liveness is None`` reproduces byte-identical
M1 behavior, and the SequenceTracker / seq path is UNTOUCHED.

§J cases:
  - test_heartbeat_alarm_suppressed_when_session_closed — closed liveness -> 0 alerts at BOTH sites
  - test_heartbeat_alarm_fires_when_session_open        — open liveness  -> alert fires
  - test_liveness_none_is_byte_identical_to_m1          — None -> unchanged M1 behavior
  - test_seq_path_unaffected_by_session                 — liveness never gates the seq path
  - test_calendar_liveness_predicate                    — the real CalendarLiveness over the fixture calendar
"""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent.market_calendar import FixtureScheduleProvider, MarketCalendar
from agent.session_liveness import CalendarLiveness, SessionLiveness
from recorder.persistence import EventWriter, replay_stream
from recorder.recorder import BackoffPolicy, Recorder
from recorder.status import HeartbeatMonitor, SequencePolicy, SequenceTracker
from tests.lib.fakes import FakeClock, FakeTransport, FlakyTransport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "databento"
CAL_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "calendar" / "nyse_2026_schedule.json"

DATASET = "<DEPTH_DATASET>"
SCHEMA = "mbp-10"
SYMBOL = "AAPL"
TIMEOUT_MS = 5000


def _load_frames(name):
    path = FIXTURES / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class _FixedLiveness:
    """A test double that satisfies the SessionLiveness Protocol with a fixed answer."""

    def __init__(self, live):
        self._live = live
        self.calls = []

    def expected_live(self, symbol, now_ms):
        self.calls.append((symbol, now_ms))
        return self._live


class _RecordingSleep:
    def __init__(self, clock):
        self.delays = []
        self._clock = clock

    async def __call__(self, delay_ms):
        self.delays.append(delay_ms)
        self._clock.advance(delay_ms)


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _alerts_path(self):
        return self.root / "data_quality_alerts.jsonl"

    def _recorder(self, *, clock, liveness=None, transport=None, sleep=None,
                  policy=SequencePolicy.NONE, heartbeat_timeout_ms=TIMEOUT_MS,
                  backoff=None):
        stamp = lambda: "2026-06-09T13:30:00.000000Z"
        events_w = EventWriter(self.root / "events.jsonl", run_id="run-1", clock=stamp)
        alerts_w = EventWriter(self._alerts_path(), run_id="run-1", clock=stamp)
        return Recorder(
            transport if transport is not None else FakeTransport([]),
            events_w,
            dataset=DATASET, schema=SCHEMA, symbols=(SYMBOL,),
            clock=clock, sleep=sleep if sleep is not None else _RecordingSleep(clock),
            backoff=backoff if backoff is not None else BackoffPolicy(base_ms=250, alert_after_ms=10**9),
            sequence_tracker=SequenceTracker(SYMBOL, policy=policy),
            heartbeat=HeartbeatMonitor(timeout_ms=heartbeat_timeout_ms, clock=clock),
            alert_writer=alerts_w,
            liveness=liveness,
        )

    def _heartbeat_causes(self):
        return [a.get("cause") for a in replay_stream(self._alerts_path())
                if a.get("cause") == "heartbeat_timeout"]

    def _prime_stale(self, rec, clock):
        """Make SYMBOL stale: touch at t=0, advance past the timeout."""
        rec._heartbeat.touch(SYMBOL, 0)
        clock.advance(TIMEOUT_MS + 1000)


class TestSessionAwareSuppression(_Tmp):
    def test_heartbeat_alarm_suppressed_when_session_closed(self):
        # SITE 1 (_check_connected_quiet, recorder.py): closed session -> no alert.
        clock = FakeClock(start_ms=0)
        liveness = _FixedLiveness(False)
        rec = self._recorder(clock=clock, liveness=liveness)
        self._prime_stale(rec, clock)
        rec._check_connected_quiet(clock.now_ms())
        self.assertEqual(self._heartbeat_causes(), [],
                         "closed session must suppress heartbeat_timeout at the connected-quiet site")
        self.assertTrue(liveness.calls, "liveness predicate must be consulted")

        # SITE 2 (_reconnect, recorder.py): closed session -> no alert across the gap.
        clock2 = FakeClock(start_ms=0)
        rec2 = self._recorder(clock=clock2, liveness=_FixedLiveness(False))
        rec2._heartbeat.touch(SYMBOL, 0)
        clock2.advance(TIMEOUT_MS + 1000)
        rec2._disconnect_origin_ms = clock2.now_ms()
        asyncio.run(rec2._reconnect())
        self.assertEqual(self._heartbeat_causes(), [],
                         "closed session must suppress heartbeat_timeout at the reconnect site too")

    def test_suppressed_symbol_not_marked_alerted_so_it_can_alert_on_reopen(self):
        # A suppressed symbol must NOT enter _stale_alerted, so once the session
        # re-opens a still-quiet symbol DOES alert (no permanent silencing).
        clock = FakeClock(start_ms=0)
        live = _FixedLiveness(False)
        rec = self._recorder(clock=clock, liveness=live)
        self._prime_stale(rec, clock)
        rec._check_connected_quiet(clock.now_ms())
        self.assertEqual(self._heartbeat_causes(), [])
        self.assertNotIn(SYMBOL, rec._stale_alerted)
        # Session re-opens: flip liveness to live; the same quiet symbol now alerts.
        rec._liveness = _FixedLiveness(True)
        rec._check_connected_quiet(clock.now_ms())
        self.assertEqual(self._heartbeat_causes(), ["heartbeat_timeout"])


class TestSessionAwareFiring(_Tmp):
    def test_heartbeat_alarm_fires_when_session_open(self):
        clock = FakeClock(start_ms=0)
        rec = self._recorder(clock=clock, liveness=_FixedLiveness(True))
        self._prime_stale(rec, clock)
        rec._check_connected_quiet(clock.now_ms())
        self.assertEqual(self._heartbeat_causes(), ["heartbeat_timeout"],
                         "an open session must NOT suppress a real heartbeat_timeout")

    def test_liveness_none_is_byte_identical_to_m1(self):
        clock = FakeClock(start_ms=0)
        rec = self._recorder(clock=clock, liveness=None)
        self._prime_stale(rec, clock)
        rec._check_connected_quiet(clock.now_ms())
        causes = self._heartbeat_causes()
        self.assertEqual(causes, ["heartbeat_timeout"],
                         "liveness=None must behave exactly as M1 (alert fires)")
        rows = [a for a in replay_stream(self._alerts_path()) if a.get("cause") == "heartbeat_timeout"]
        self.assertEqual(rows[0]["symbol"], SYMBOL)


class TestSeqPathUnaffected(_Tmp):
    def test_seq_path_unaffected_by_session(self):
        # The seq path must be identical regardless of the session liveness: a closed
        # liveness gates ONLY heartbeat_timeout, never sequence detection. Run the SAME
        # gap fixture once with liveness=None and once with a closed liveness; the
        # non-heartbeat alerts must be identical. Heartbeat timeout is huge so the
        # heartbeat path stays silent and the seq path is isolated.
        def run_with(liveness):
            clock = FakeClock(start_ms=0)
            transport = FlakyTransport(_load_frames("flaky_transport_gap.jsonl"))
            sleep = _RecordingSleep(clock)
            rec = self._recorder(clock=clock, liveness=liveness, transport=transport,
                                 sleep=sleep, policy=SequencePolicy.MONOTONIC,
                                 heartbeat_timeout_ms=10**9)
            asyncio.run(rec.run(max_events=2))
            causes = sorted(a.get("cause") for a in replay_stream(self._alerts_path()))
            self._dir.cleanup()
            self._dir = tempfile.TemporaryDirectory()
            self.root = Path(self._dir.name)
            return causes

        none_causes = run_with(None)
        closed_causes = run_with(_FixedLiveness(False))
        self.assertEqual(none_causes, closed_causes,
                         "session liveness must NOT change the sequence-detection alerts")
        self.assertNotIn("heartbeat_timeout", closed_causes)
        self.assertTrue(any("gap" in c for c in closed_causes),
                        f"the seq path must still surface the injected gap; got {closed_causes}")

    def test_none_policy_never_gaps_regardless_of_session(self):
        # EQUS.MINI is policy=NONE; the seq path never fires, session-aware or not.
        tracker = SequenceTracker(SYMBOL, policy=SequencePolicy.NONE)

        class _Ev:
            def __init__(self, s):
                self.provenance = type("P", (), {"vendor_seq": s})()

        self.assertIsNone(tracker.observe(_Ev(1001)))
        self.assertIsNone(tracker.observe(_Ev(1005)))  # a jump that MONOTONIC would flag


class TestCalendarLiveness(_Tmp):
    def _calendar(self):
        fixture = json.loads(CAL_FIXTURE.read_text(encoding="utf-8"))
        return MarketCalendar(FixtureScheduleProvider(fixture, pin=fixture["pin"]))

    @staticmethod
    def _epoch_ms(y, m, d, hh, mm):
        return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)

    @staticmethod
    def _conv(now_ms):
        return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def test_calendar_liveness_predicate(self):
        live = CalendarLiveness(calendar=self._calendar(), clock_to_utc_iso=self._conv)
        self.assertIsInstance(live, SessionLiveness)
        # 2026-06-15 13:30Z == 09:30 ET (EDT) -> RTH -> expected_live True.
        self.assertTrue(live.expected_live(SYMBOL, self._epoch_ms(2026, 6, 15, 13, 30)))
        # 2026-06-13 is a Saturday -> CLOSED -> not expected live (suppress).
        self.assertFalse(live.expected_live(SYMBOL, self._epoch_ms(2026, 6, 13, 15, 0)))
        # A date outside the fixture coverage -> UnknownSessionDate -> False (fail-closed suppress).
        self.assertFalse(live.expected_live(SYMBOL, self._epoch_ms(2026, 1, 5, 15, 0)))


if __name__ == "__main__":
    unittest.main()
