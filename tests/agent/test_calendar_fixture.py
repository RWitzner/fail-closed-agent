"""agent.calendar_fixture — session-fixture generator + hand-table cross-check.

Offline, stdlib-only: the generator is driven from committed fixture-backed
providers (never the real exchange_calendars lib — that runs only in the
credentialed .venv generation step). Asserts:
  - generation shape over a contiguous range (trading/holiday/weekend entries),
  - ROUND-TRIP equality: FixtureScheduleProvider over the GENERATED fixture
    reproduces the source provider's SessionSchedules exactly,
  - half-day wall clocks (13:00 close, 17:00 post-close),
  - a weekday coverage gap propagates UnknownSessionDate (fail-closed),
  - cross-check passes on a clean range and flags every divergence class,
  - deterministic byte-stable JSON writes.
"""
import json
import unittest
from pathlib import Path

from agent.calendar_fixture import (
    XNYS_2026_H2_EXPECTATIONS,
    cross_check_fixture,
    generate_session_fixture,
    write_fixture_json,
)
from agent.market_calendar import (
    CalendarError,
    FixtureScheduleProvider,
    UnknownSessionDate,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "calendar"


def _provider(name: str, pin: str) -> FixtureScheduleProvider:
    fixture = json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return FixtureScheduleProvider(fixture, pin=pin)


def _margin_provider() -> FixtureScheduleProvider:
    return _provider("nyse_margin_window_v1.json", "fixture:XNYS-margin-window-v1")


class TestGeneration(unittest.TestCase):
    def test_generates_trading_holiday_and_weekend_entries(self):
        fixture = generate_session_fixture(
            _margin_provider(), start_date_et="2026-06-15",
            end_date_et="2026-06-22", mic="XNYS", pin="fixture:test-v1")
        sessions = fixture["sessions"]
        self.assertEqual(sorted(sessions), [
            "2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18",
            "2026-06-19", "2026-06-20", "2026-06-21", "2026-06-22"])
        self.assertEqual(sessions["2026-06-15"], {
            "is_trading_day": True, "is_early_close": False,
            "pre_open_et": "04:00", "rth_open_et": "09:30",
            "rth_close_et": "16:00", "post_close_et": "20:00"})
        # Juneteenth holiday + the weekend are non-trading.
        self.assertEqual(sessions["2026-06-19"]["is_trading_day"], False)
        self.assertEqual(sessions["2026-06-20"]["is_trading_day"], False)
        self.assertEqual(sessions["2026-06-21"]["is_trading_day"], False)
        self.assertEqual(fixture["mic"], "XNYS")
        self.assertEqual(fixture["pin"], "fixture:test-v1")
        self.assertEqual(fixture["coverage"], {
            "start_date_et": "2026-06-15", "end_date_et": "2026-06-22"})

    def test_round_trip_reproduces_source_schedules(self):
        source = _margin_provider()
        fixture = generate_session_fixture(
            source, start_date_et="2026-06-15", end_date_et="2026-07-14",
            mic="XNYS", pin="fixture:test-v1")
        regenerated = FixtureScheduleProvider(fixture, pin="fixture:test-v1")
        for session_date in sorted(fixture["sessions"]):
            with self.subTest(date=session_date):
                self.assertEqual(regenerated.schedule_for(session_date),
                                 source.schedule_for(session_date))

    def test_half_day_wall_clocks(self):
        fixture = generate_session_fixture(
            _provider("nyse_2026_schedule.json", "fixture:XNYS-2026-v1"),
            start_date_et="2026-11-27", end_date_et="2026-11-27",
            mic="XNYS", pin="fixture:test-v1")
        entry = fixture["sessions"]["2026-11-27"]
        self.assertTrue(entry["is_early_close"])
        self.assertEqual(entry["rth_close_et"], "13:00")
        self.assertEqual(entry["post_close_et"], "17:00")

    def test_weekday_coverage_gap_fails_closed(self):
        # nyse_2026_schedule.json covers 2026-06-15 but NOT 2026-06-16.
        with self.assertRaises(UnknownSessionDate):
            generate_session_fixture(
                _provider("nyse_2026_schedule.json", "fixture:XNYS-2026-v1"),
                start_date_et="2026-06-15", end_date_et="2026-06-16",
                mic="XNYS", pin="fixture:test-v1")

    def test_inverted_or_malformed_range_rejected(self):
        with self.assertRaises(CalendarError):
            generate_session_fixture(
                _margin_provider(), start_date_et="2026-06-22",
                end_date_et="2026-06-15", mic="XNYS", pin="p")
        with self.assertRaises(CalendarError):
            generate_session_fixture(
                _margin_provider(), start_date_et="garbage",
                end_date_et="2026-06-15", mic="XNYS", pin="p")

    def test_write_is_deterministic(self):
        import tempfile

        fixture = generate_session_fixture(
            _margin_provider(), start_date_et="2026-06-15",
            end_date_et="2026-06-19", mic="XNYS", pin="fixture:test-v1")
        with tempfile.TemporaryDirectory() as tmp:
            text_a = write_fixture_json(fixture, Path(tmp) / "a.json")
            text_b = write_fixture_json(fixture, Path(tmp) / "b.json")
            self.assertEqual(text_a, text_b)
            self.assertEqual(json.loads(text_a), fixture)


class TestCrossCheck(unittest.TestCase):
    def _june_fixture(self):
        return generate_session_fixture(
            _margin_provider(), start_date_et="2026-06-15",
            end_date_et="2026-06-26", mic="XNYS", pin="fixture:test-v1")

    def test_clean_range_passes(self):
        divergences = cross_check_fixture(
            self._june_fixture(), holidays=("2026-06-19",), half_days=())
        self.assertEqual(divergences, [])

    def test_hand_table_holiday_vs_fixture_trading_flags(self):
        divergences = cross_check_fixture(
            self._june_fixture(), holidays=("2026-06-18",), half_days=())
        self.assertTrue(any("2026-06-18" in d and "HOLIDAY" in d
                            for d in divergences))

    def test_unexpected_weekday_closure_flags(self):
        # 2026-06-19 closed in the fixture but NOT declared in the table.
        divergences = cross_check_fixture(
            self._june_fixture(), holidays=(), half_days=())
        self.assertTrue(any("2026-06-19" in d and "unexpected closure" in d
                            for d in divergences))

    def test_half_day_mismatches_flag_both_directions(self):
        fixture = self._june_fixture()
        # table claims a half-day the fixture shows as normal
        divergences = cross_check_fixture(
            fixture, holidays=("2026-06-19",), half_days=("2026-06-18",))
        self.assertTrue(any("2026-06-18" in d and "HALF-DAY" in d
                            for d in divergences))
        # fixture shows an early close the table does not declare
        fixture["sessions"]["2026-06-17"]["is_early_close"] = True
        fixture["sessions"]["2026-06-17"]["rth_close_et"] = "13:00"
        divergences = cross_check_fixture(
            fixture, holidays=("2026-06-19",), half_days=())
        self.assertTrue(any("2026-06-17" in d and "unexpected early close" in d
                            for d in divergences))

    def test_nonstandard_full_day_close_flags(self):
        fixture = self._june_fixture()
        fixture["sessions"]["2026-06-16"]["rth_close_et"] = "15:00"
        divergences = cross_check_fixture(
            fixture, holidays=("2026-06-19",), half_days=())
        self.assertTrue(any("2026-06-16" in d and "15:00" in d
                            for d in divergences))

    def test_expected_date_outside_coverage_flags(self):
        divergences = cross_check_fixture(
            self._june_fixture(),
            holidays=("2026-06-19", "2026-09-07"), half_days=())
        self.assertTrue(any("2026-09-07" in d and "missing" in d
                            for d in divergences))

    def test_hand_table_shape(self):
        # The committed hand table itself: H2 holidays + half-days as published.
        self.assertEqual(XNYS_2026_H2_EXPECTATIONS["holidays"], (
            "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26",
            "2026-12-25"))
        self.assertEqual(XNYS_2026_H2_EXPECTATIONS["half_days"], (
            "2026-11-27", "2026-12-24"))


if __name__ == "__main__":
    unittest.main()
