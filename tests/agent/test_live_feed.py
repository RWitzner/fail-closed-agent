"""agent.marketdata.live_feed — the wall-clock live feed (M1 tier-2b seam).

Offline + deterministic: a scripted source generator advances a fake time
machine before each yield, so wall-clock ticks, bar-close grace, stop-at and
max-runtime are all exact — no threads, no sleeps, no network. Asserts:
  - quote delivery updates the view with delivery-time seen_at_ms,
  - ticks fire on wall cadence THROUGH heartbeats (quiet market),
  - a bar fires only after bucket_end + grace (no in-progress leak = no
    lookahead), exactly once, and the reader serves it,
  - late rows for fired buckets / receipt regressions / off-universe symbols
    are dropped + counted (fail-closed per record),
  - stop_at_utc and max_runtime_ms end the run,
  - recorded events ROUND-TRIP: the EventWriter journal replays through
    ReplayQuoteFeed to the same latest quote and the same bar,
  - the credentialed databento source is fail-closed UNVERIFIED by default and
    the module imports no databento,
  - an observe-mode Orchestrator composes over the live feed end-to-end.
"""
import json
import sys
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.bar_series import MidBar, _parse_utc
from agent.marketdata.live_feed import (
    LiveClock,
    LiveQuoteFeed,
    databento_live_source,
)
from agent.marketdata.replay_feed import ReplayQuoteFeed
from recorder.event import Provenance, QuoteEvent
from recorder.persistence import EventWriter

_REPO_ROOT = Path(__file__).resolve().parents[2]
_H2_FIXTURE = (_REPO_ROOT / "tests" / "fixtures" / "calendar"
               / "xnys_sessions_2026H2_v1.json")

_START = "2026-07-06T13:30:00.000000Z"   # a Monday RTH open (EDT)


class _TimeMachine:
    """One fake time source driving BOTH the monotonic clock and wall UTC."""

    def __init__(self, start_utc: str = _START) -> None:
        self._mono = 0.0
        self._base = _parse_utc(start_utc)

    def monotonic(self) -> float:
        return self._mono

    def utc_now(self):
        return self._base + timedelta(seconds=self._mono)

    def utc_iso(self) -> str:
        return self.utc_now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def advance(self, seconds: float) -> None:
        self._mono += seconds


def _quote(tm: _TimeMachine, symbol: str = "AAPL", *, instrument_id: int = 1001,
           bid: str = "100.00", ask: str = "100.02",
           ts_event_utc=None, ts_recv_utc=None) -> QuoteEvent:
    now_iso = tm.utc_iso()
    prov = Provenance(
        dataset="EQUS.MINI", schema="bbo-1s", instrument_id=instrument_id,
        symbol=symbol, vendor_seq=None,
        ts_event_utc=ts_event_utc or now_iso,
        ts_recv_utc=ts_recv_utc or now_iso,
        reconnect_epoch=0)
    return QuoteEvent(provenance=prov, bid_px=Decimal(bid),
                      bid_sz=Decimal("100"), ask_px=Decimal(ask),
                      ask_sz=Decimal("100"))


def _scripted(tm: _TimeMachine, steps):
    """Generator: advance the time machine, then yield the item (None = heartbeat)."""
    for advance_s, item in steps:
        tm.advance(advance_s)
        yield item() if callable(item) else item


def _feed(tm: _TimeMachine, steps, *, symbols=("AAPL",), stop_at=None,
          **kwargs) -> LiveQuoteFeed:
    return LiveQuoteFeed(
        record_source=_scripted(tm, steps),
        symbols=symbols,
        dataset="EQUS.MINI",
        schema="bbo-1s",
        stop_at_utc=stop_at or "2026-07-06T20:00:00.000000Z",
        clock=LiveClock(monotonic_fn=tm.monotonic),
        utc_now_fn=tm.utc_now,
        **kwargs)


def _run(feed: LiveQuoteFeed):
    ticks, bars = [], []
    feed.run(on_tick=lambda *, now_ms: ticks.append(now_ms),
             on_bar_complete=bars.append)
    return ticks, bars


class TestDeliveryAndTicks(unittest.TestCase):
    def test_quotes_update_view_with_delivery_seen_at_ms(self):
        tm = _TimeMachine()
        steps = [(10.0, lambda: _quote(tm, bid="100.00", ask="100.02")),
                 (5.0, lambda: _quote(tm, bid="100.10", ask="100.12"))]
        feed = _feed(tm, steps)
        _run(feed)
        latest = feed.quote_view().latest("AAPL", 1001)
        self.assertEqual(latest.bid, Decimal("100.10"))
        self.assertEqual(latest.seen_at_ms, 15_000)
        self.assertEqual(latest.schema, "bbo-1s")

    def test_ticks_fire_on_wall_cadence_through_heartbeats(self):
        tm = _TimeMachine()
        steps = [(0.4, None)] * 10   # 4s of quiet market
        ticks, _ = _run(_feed(tm, steps))
        self.assertGreaterEqual(len(ticks), 3)
        self.assertEqual(ticks, sorted(set(ticks)))   # strictly increasing

    def test_off_universe_and_regression_dropped_and_counted(self):
        tm = _TimeMachine()
        early_recv = "2026-07-06T13:30:01.000000Z"

        def regressed():
            return _quote(tm, ts_event_utc=early_recv, ts_recv_utc=early_recv)

        steps = [
            (10.0, lambda: _quote(tm)),
            (1.0, lambda: _quote(tm, symbol="TSLA", instrument_id=2001)),
            (1.0, regressed),   # ts_recv before the first delivered quote
        ]
        feed = _feed(tm, steps)
        _run(feed)
        counts = feed.data_quality_counts()
        self.assertEqual(counts.get("off_universe_symbol"), 1)
        self.assertEqual(counts.get("receipt_order_regression"), 1)


class TestBarCompletion(unittest.TestCase):
    def test_bar_fires_only_after_grace_and_only_once(self):
        tm = _TimeMachine()
        steps = [
            (10.0, lambda: _quote(tm, bid="100.00", ask="100.02")),  # 13:30:10
            (40.0, lambda: _quote(tm, bid="100.20", ask="100.22")),  # 13:30:50
            (10.5, None),   # 13:31:00.5 < end+grace(1.5s) -> NOT fired yet
        ]
        feed = _feed(tm, steps)
        fired_at_heartbeat = []

        def on_bar(bar):
            fired_at_heartbeat.append((bar.bucket_end_utc, tm.utc_iso()))

        feed.run(on_tick=lambda *, now_ms: None, on_bar_complete=on_bar)
        # the run's FINAL pass ran at 13:31:00.5 — still inside grace, so the
        # 13:30 bucket must NOT have fired at all.
        self.assertEqual(fired_at_heartbeat, [])

        tm2 = _TimeMachine()
        steps2 = [
            (10.0, lambda: _quote(tm2, bid="100.00", ask="100.02")),
            (40.0, lambda: _quote(tm2, bid="100.20", ask="100.22")),
            (12.0, None),    # 13:31:02 >= 13:31:00 + 1.5s -> fires
            (1.0, None),     # no double fire
        ]
        feed2 = _feed(tm2, steps2)
        _, bars = _run(feed2)
        self.assertEqual([bar.bucket_end_utc for bar in bars],
                         ["2026-07-06T13:31:00.000000Z"])
        served = feed2.bar_reader().get(
            "AAPL", 1001, "2026-07-06T13:31:00.000000Z")
        self.assertIsInstance(served, MidBar)

    def test_in_progress_minute_never_leaks(self):
        tm = _TimeMachine()
        steps = [
            (10.0, lambda: _quote(tm)),          # 13:30:10 (bucket 13:30)
            (55.0, lambda: _quote(tm)),          # 13:31:05 (bucket 13:31 open)
            (1.0, None),                         # 13:31:06: 13:30 fired, 13:31 NOT
        ]
        _, bars = _run(_feed(tm, steps))
        self.assertEqual([bar.bucket_end_utc for bar in bars],
                         ["2026-07-06T13:31:00.000000Z"])

    def test_vendor_clock_skew_cannot_disorder_bar_firing(self):
        # A late row for bucket A carries a vendor ts_recv AHEAD of our wall
        # clock (skew), pushing A's watermark past the moment bucket B becomes
        # eligible. Bars must STILL fire in bucket order per key (B waits for
        # A) — out-of-order on_bar_complete would corrupt rolling feature
        # state and an out-of-order reader rebuild would crash the session.
        tm = _TimeMachine()

        def skewed_late_row():
            # event in bucket A [13:30,13:31); vendor recv 8s ahead of wall
            recv = (tm.utc_now() + timedelta(seconds=8)).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ")
            return _quote(tm, ts_event_utc="2026-07-06T13:30:59.000000Z",
                          ts_recv_utc=recv)

        steps = [
            (10.0, lambda: _quote(tm)),      # 13:30:10 (bucket A)
            (75.0, lambda: _quote(tm)),      # 13:31:25 (bucket B)
            (30.0, skewed_late_row),         # wall 13:31:55; recv 13:32:03
            (8.0, None),                     # 13:32:03: B end+grace passed,
                                             # A watermark NOT yet -> neither
            (4.0, None),                     # 13:32:07: A settles -> A then B
            (1.0, None),
        ]
        _, bars = _run(_feed(tm, steps))
        self.assertEqual([bar.bucket_end_utc for bar in bars],
                         ["2026-07-06T13:31:00.000000Z",
                          "2026-07-06T13:32:00.000000Z"])

    def test_absurd_future_receipt_dropped_so_key_cannot_wedge(self):
        tm = _TimeMachine()

        def far_future_recv():
            recv = (tm.utc_now() + timedelta(seconds=120)).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ")
            return _quote(tm, ts_event_utc=tm.utc_iso(), ts_recv_utc=recv)

        steps = [
            (10.0, lambda: _quote(tm)),      # 13:30:10 (bucket A)
            (5.0, far_future_recv),          # recv 2 min ahead -> DROPPED
            (60.0, lambda: _quote(tm)),      # 13:31:15 (bucket B opens)
            (3.0, None),                     # A fires despite the bad row
        ]
        feed = _feed(tm, steps)
        _, bars = _run(feed)
        self.assertEqual([bar.bucket_end_utc for bar in bars],
                         ["2026-07-06T13:31:00.000000Z"])
        self.assertEqual(
            feed.data_quality_counts().get("future_receipt_dropped"), 1)

    def test_late_row_for_fired_bucket_dropped_and_counted(self):
        tm = _TimeMachine()
        late_ts = "2026-07-06T13:30:30.000000Z"

        def late_row():
            return _quote(tm, ts_event_utc=late_ts, bid="999.00", ask="999.02")

        steps = [
            (10.0, lambda: _quote(tm, bid="100.00", ask="100.02")),
            (52.0, None),          # 13:31:02 -> 13:30 bucket fires
            (1.0, late_row),       # ts_event inside the FIRED bucket -> dropped
        ]
        feed = _feed(tm, steps)
        _, bars = _run(feed)
        self.assertEqual(len(bars), 1)
        self.assertEqual(feed.data_quality_counts().get("late_row_dropped"), 1)
        served = feed.bar_reader().get(
            "AAPL", 1001, "2026-07-06T13:31:00.000000Z")
        self.assertIsInstance(served, MidBar)
        self.assertNotEqual(served.mid, Decimal("999.01"))


class TestTermination(unittest.TestCase):
    def test_stop_at_utc_ends_run(self):
        tm = _TimeMachine()
        steps = [(30.0, None)] * 100   # would run 50 minutes
        ticks, _ = _run(_feed(tm, steps, stop_at="2026-07-06T13:35:00.000000Z"))
        self.assertLessEqual(tm.monotonic(), 5 * 60 + 30)

    def test_max_runtime_backstop(self):
        tm = _TimeMachine()
        steps = [(30.0, None)] * 100
        feed = _feed(tm, steps, max_runtime_ms=120_000)
        _run(feed)
        self.assertLessEqual(tm.monotonic(), 150 + 30)
        self.assertEqual(feed.data_quality_counts().get("max_runtime_stop"), 1)

    def test_run_is_single_shot(self):
        tm = _TimeMachine()
        feed = _feed(tm, [(1.0, None)])
        _run(feed)
        with self.assertRaises(ValueError):
            _run(feed)


class TestRecordingRoundTrip(unittest.TestCase):
    def test_recorded_events_replay_to_same_quote_and_bar(self):
        tm = _TimeMachine()
        with TemporaryDirectory() as tmp:
            events_path = Path(tmp) / "events.jsonl"
            writer = EventWriter(events_path, "run-live-test",
                                 clock=lambda: "2026-07-06T13:30:00.000000Z")
            steps = [
                (10.0, lambda: _quote(tm, bid="100.00", ask="100.02")),
                (40.0, lambda: _quote(tm, bid="100.20", ask="100.22")),
                # a quote AFTER the bucket end so the RECORDED stream also
                # contains the bucket's completion instant (replay fires bars
                # only from recorded time; live fires them from wall time).
                (15.0, lambda: _quote(tm, bid="100.24", ask="100.26")),
                (1.0, None),
            ]
            feed = _feed(tm, steps, event_writer=writer)
            _, live_bars = _run(feed)
            self.assertEqual(len(live_bars), 1)

            replay = ReplayQuoteFeed(events_path)
            replay_bars = []
            replay.run(on_tick=lambda *, now_ms: None,
                       on_bar_complete=replay_bars.append)
            live_latest = feed.quote_view().latest("AAPL", 1001)
            replay_latest = replay.quote_view().latest("AAPL", 1001)
            self.assertEqual(replay_latest.bid, live_latest.bid)
            self.assertEqual(replay_latest.ask, live_latest.ask)
            self.assertEqual(replay_latest.ts_recv_utc, live_latest.ts_recv_utc)
            self.assertEqual(
                [bar.bucket_end_utc for bar in replay_bars],
                [bar.bucket_end_utc for bar in live_bars])
            self.assertEqual(replay_bars[0].mid, live_bars[0].mid)
            self.assertEqual(replay_bars[0].watermark_utc,
                             live_bars[0].watermark_utc)


class TestLiveSourceFailClosed(unittest.TestCase):
    def test_unverified_by_default_and_no_databento_import(self):
        with self.assertRaises(NotImplementedError):
            databento_live_source(dataset="EQUS.MINI", schema="bbo-1s",
                                  symbols=("AAPL",))
        self.assertNotIn("databento", sys.modules)

    def test_module_import_is_pure(self):
        import agent.marketdata.live_feed  # noqa: F401

        self.assertNotIn("databento", sys.modules)


class TestObserveComposition(unittest.TestCase):
    def test_orchestrator_observe_mode_runs_over_live_feed(self):
        from agent import config as agent_config
        from agent import orchestrator as orch_mod

        tm = _TimeMachine()
        steps = [(10.0, lambda: _quote(tm, bid="100.00", ask="100.02"))]
        steps += [(5.0, lambda: _quote(tm, bid="100.02", ask="100.04"))] * 30
        feed = _feed(tm, steps)
        config = {
            "agent_rules": agent_config.load(
                _REPO_ROOT / "config" / "agent_rules.json"),
            "risk_rules": agent_config.load(
                _REPO_ROOT / "config" / "risk_rules.json"),
        }
        fixture = json.loads(_H2_FIXTURE.read_text(encoding="utf-8"))
        from agent.market_calendar import FixtureScheduleProvider

        with TemporaryDirectory() as tmp:
            orch = orch_mod.Orchestrator(
                journal_dir=Path(tmp),
                run_id="run-live-observe-test",
                clock=feed.clock(),
                quote_view=feed.quote_view(),
                bar_reader=feed.bar_reader(),
                calendar_provider=FixtureScheduleProvider(
                    fixture, pin=fixture["pin"]),
                config=config,
            )
            try:
                self.assertEqual(orch.mode, "observe")
                self.assertIsNone(orch.broker)
                orch.run_with_feed(feed)
            finally:
                orch.close()
            decisions = (Path(tmp) / "decisions.jsonl")
            self.assertTrue(decisions.exists())


if __name__ == "__main__":
    unittest.main()
