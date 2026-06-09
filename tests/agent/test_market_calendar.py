"""M2 §A — MarketCalendar / ScheduleProvider tests (contract §J test_market_calendar.py).

Offline, stdlib-only. Drives FixtureScheduleProvider off the pinned §H.1 fixture and
the §H.2 instants. Asserts:
  - phase classification (PRE/RTH/POST/CLOSED) per instant,
  - continuous RTH owns the full [09:30:00, 16:00:00) window (MR-1),
  - early-close half-day (13:00 ET) → 13:30 ET is POST,
  - holiday/weekend → CLOSED (fail-closed),
  - unknown date → UnknownSessionDate (never "assume open"),
  - DST EST-vs-EDT 1h delta on the persisted RTH-close UTC (the discriminating axis),
  - a fixture boundary in the spring-forward skipped hour → CalendarError (DET-6),
  - importing the module pulls NO exchange_calendars into sys.modules (offline purity),
  - the live provider's _build_calendar is NotImplementedError offline.
"""
import json
import sys
import unittest
from pathlib import Path

from agent.market_calendar import (
    CalendarError,
    ExchangeCalendarsScheduleProvider,
    FixtureScheduleProvider,
    MarketCalendar,
    SessionPhase,
    SessionSchedule,
    UnknownSessionDate,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "calendar"
_SCHEDULE_JSON = _FIXTURE_DIR / "nyse_2026_schedule.json"
_INSTANTS_JSONL = _FIXTURE_DIR / "session_instants.jsonl"


def _load_fixture() -> dict:
    return json.loads(_SCHEDULE_JSON.read_text())


def _make_calendar() -> MarketCalendar:
    fixture = _load_fixture()
    provider = FixtureScheduleProvider(fixture, pin="fixture:XNYS-2026-v1")
    return MarketCalendar(provider)


def _load_instants():
    rows = []
    for line in _INSTANTS_JSONL.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


class TestPhaseClassification(unittest.TestCase):
    def test_phase_pre_rth_post_closed(self):
        cal = _make_calendar()
        rows = _load_instants()
        self.assertTrue(rows)  # fixture is non-empty
        for row in rows:
            ts = row["ts_utc"]
            with self.subTest(ts=ts):
                self.assertEqual(cal.session_date_for(ts), row["expect_date"])
                self.assertEqual(cal.phase_at(ts).value, row["expect_phase"])


class TestContinuousRth(unittest.TestCase):
    def test_continuous_rth_owns_full_window(self):
        cal = _make_calendar()
        # 2026-06-15 EDT (UTC-4): 09:30 ET = 13:30Z, 15:59:59 ET = 19:59:59Z.
        self.assertEqual(cal.phase_at("2026-06-15T13:30:00.000000Z"), SessionPhase.RTH)
        self.assertEqual(cal.phase_at("2026-06-15T19:59:59.000000Z"), SessionPhase.RTH)
        # The open/close cross is a point-in-time event — NOT a multi-minute suspension.
        # AUCTION is never produced by the calendar.
        self.assertNotEqual(cal.phase_at("2026-06-15T13:30:00.000000Z"), SessionPhase.AUCTION)
        self.assertNotEqual(cal.phase_at("2026-06-15T19:59:59.000000Z"), SessionPhase.AUCTION)


class TestEarlyClose(unittest.TestCase):
    def test_early_close_half_day(self):
        cal = _make_calendar()
        # 2026-11-27 EST half-day: RTH close 13:00 ET = 18:00Z.
        # 12:59:59 ET = 17:59:59Z is still RTH; 13:30 ET = 18:30Z is POST.
        self.assertEqual(cal.phase_at("2026-11-27T17:59:59.000000Z"), SessionPhase.RTH)
        self.assertEqual(cal.phase_at("2026-11-27T18:30:00.000000Z"), SessionPhase.POST)
        sched = cal._provider.schedule_for("2026-11-27")
        self.assertTrue(sched.is_early_close)
        self.assertEqual(sched.rth_close_utc, "2026-11-27T18:00:00.000000Z")


class TestHolidayWeekend(unittest.TestCase):
    def test_holiday_and_weekend_are_closed(self):
        cal = _make_calendar()
        # Thanksgiving (holiday) and Christmas (holiday) → CLOSED all day.
        self.assertEqual(cal.phase_at("2026-11-26T15:00:00.000000Z"), SessionPhase.CLOSED)
        self.assertEqual(cal.phase_at("2026-12-25T15:00:00.000000Z"), SessionPhase.CLOSED)
        # A Saturday (2026-06-13) → CLOSED via the facade's structural weekend short-circuit
        # (§H.2 row 8 / §J: "a Saturday -> CLOSED"); it is NOT in the §H.1 fixture, yet
        # phase_at degrades to CLOSED, never UNKNOWN and never "assume open".
        self.assertEqual(cal.phase_at("2026-06-13T15:00:00.000000Z"), SessionPhase.CLOSED)
        # ...even at an instant that would be RTH on a trading day.
        self.assertEqual(cal.phase_at("2026-06-13T13:30:00.000000Z"), SessionPhase.CLOSED)
        sched_holiday = cal._provider.schedule_for("2026-11-26")
        self.assertFalse(sched_holiday.is_trading_day)
        self.assertIsNone(sched_holiday.rth_open_utc)
        self.assertIsNone(sched_holiday.rth_close_utc)


class TestUnknownDate(unittest.TestCase):
    def test_unknown_date_raises_unknown_session_date(self):
        cal = _make_calendar()
        provider = cal._provider
        # A date outside the pinned fixture coverage → fail-closed, never "assume open".
        with self.assertRaises(UnknownSessionDate):
            provider.schedule_for("2026-06-13")  # Saturday, not in fixture
        with self.assertRaises(UnknownSessionDate):
            provider.schedule_for("2030-01-01")
        # UnknownSessionDate is a CalendarError subclass (broad fail-closed catch).
        self.assertTrue(issubclass(UnknownSessionDate, CalendarError))


class TestDstEstVsEdt(unittest.TestCase):
    def test_dst_est_vs_edt_rth_close_offset(self):
        cal = _make_calendar()
        # The discriminating axis: 16:00 ET RTH close persists as 20:00Z (EDT) vs 21:00Z (EST).
        edt = cal._provider.schedule_for("2026-06-15")  # EDT
        est = cal._provider.schedule_for("2026-11-02")  # EST
        self.assertEqual(edt.rth_close_utc, "2026-06-15T20:00:00.000000Z")
        self.assertEqual(est.rth_close_utc, "2026-11-02T21:00:00.000000Z")
        # The 1h delta a fixed-offset bug would miss.
        self.assertNotEqual(edt.rth_close_utc[11:13], est.rth_close_utc[11:13])
        # The phase boundary respects the per-date offset:
        self.assertEqual(cal.phase_at("2026-06-15T19:59:59.000000Z"), SessionPhase.RTH)
        self.assertEqual(cal.phase_at("2026-06-15T20:00:00.000000Z"), SessionPhase.POST)
        self.assertEqual(cal.phase_at("2026-11-02T20:59:59.000000Z"), SessionPhase.RTH)
        self.assertEqual(cal.phase_at("2026-11-02T21:00:00.000000Z"), SessionPhase.POST)


class TestDstHalfDayEstClose(unittest.TestCase):
    def test_dst_half_day_est_close(self):
        cal = _make_calendar()
        # 2026-11-27 (EST) half-day 13:00 ET → 18:00Z.
        sched = cal._provider.schedule_for("2026-11-27")
        self.assertEqual(sched.rth_close_utc, "2026-11-27T18:00:00.000000Z")
        self.assertEqual(sched.post_close_utc, "2026-11-27T22:00:00.000000Z")


class TestSpringForwardGap(unittest.TestCase):
    def test_spring_forward_gap_boundary_rejected(self):
        # A malformed fixture boundary inside the 02:00-03:00 ET skipped hour (spring forward
        # 2026-03-08) must raise CalendarError rather than silently fold-shift (DET-6).
        fixture = _load_fixture()
        fixture["sessions"]["2026-03-08-broken"] = {
            "is_trading_day": True,
            "is_early_close": False,
            "pre_open_et": "04:00",
            "rth_open_et": "09:30",
            "rth_close_et": "16:00",
            "post_close_et": "20:00",
        }
        # Override 2026-03-08 to a (fictional) trading day whose pre-open lands in the gap.
        fixture["sessions"]["2026-03-08"] = {
            "is_trading_day": True,
            "is_early_close": False,
            "pre_open_et": "02:30",  # inside the spring-forward skipped hour on 2026-03-08
            "rth_open_et": "09:30",
            "rth_close_et": "16:00",
            "post_close_et": "20:00",
        }
        provider = FixtureScheduleProvider(fixture, pin="fixture:XNYS-2026-v1")
        with self.assertRaises(CalendarError):
            provider.schedule_for("2026-03-08")


class TestOfflinePurity(unittest.TestCase):
    def test_importing_module_pulls_no_exchange_calendars(self):
        import agent.market_calendar  # noqa: F401

        self.assertNotIn("exchange_calendars", sys.modules)

    def test_fixture_provider_imports_no_exchange_calendars(self):
        cal = _make_calendar()
        cal.phase_at("2026-06-15T13:30:00.000000Z")
        cal._provider.is_trading_day("2026-06-15")
        self.assertNotIn("exchange_calendars", sys.modules)

    def test_live_provider_build_is_notimplemented_offline(self):
        provider = ExchangeCalendarsScheduleProvider()
        self.assertNotIn("exchange_calendars", sys.modules)
        with self.assertRaises(NotImplementedError):
            provider._build_calendar()
        self.assertNotIn("exchange_calendars", sys.modules)
        # The provider still carries a provenance pin without building anything.
        self.assertEqual(provider.calendar_pin(), "4.13.2")


class TestScheduleProviderProtocol(unittest.TestCase):
    def test_fixture_provider_satisfies_protocol(self):
        from agent.market_calendar import ScheduleProvider

        provider = FixtureScheduleProvider(_load_fixture(), pin="fixture:XNYS-2026-v1")
        self.assertIsInstance(provider, ScheduleProvider)
        self.assertIsInstance(ExchangeCalendarsScheduleProvider(), ScheduleProvider)

    def test_calendar_pin_is_provenance_string(self):
        provider = FixtureScheduleProvider(_load_fixture(), pin="fixture:XNYS-2026-v1")
        self.assertEqual(provider.calendar_pin(), "fixture:XNYS-2026-v1")

    def test_session_schedule_is_frozen(self):
        cal = _make_calendar()
        sched = cal._provider.schedule_for("2026-06-15")
        self.assertIsInstance(sched, SessionSchedule)
        with self.assertRaises(Exception):
            sched.is_trading_day = False  # frozen dataclass

    def test_session_date_for_rejects_naive_timestamp(self):
        cal = _make_calendar()
        with self.assertRaises(ValueError):
            cal.session_date_for("2026-06-15T13:30:00")  # no tz → naive → rejected

    def test_session_date_for_accepts_numeric_offset(self):
        cal = _make_calendar()
        # +00:00 form (repo's own isoformat output) is accepted.
        self.assertEqual(
            cal.session_date_for("2026-06-15T13:30:00.000000+00:00"), "2026-06-15"
        )


if __name__ == "__main__":
    unittest.main()
