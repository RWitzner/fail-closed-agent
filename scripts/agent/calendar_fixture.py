"""Session-fixture generator + hand-table cross-check (M7d operational prerequisite).

Produces a committed, reviewable calendar fixture in the exact shape
``FixtureScheduleProvider`` consumes (``{"mic","pin","sessions":{date:{...}}}``,
ET wall-clock strings) from ANY ``ScheduleProvider`` — offline tests drive it from
a fixture-backed provider; the real generation run drives it from the implemented
``ExchangeCalendarsScheduleProvider`` under ``.venv`` (the pinned lib is the
cross-check instrument the M7d packet requires).

Fail-closed posture:
- weekdays MUST resolve via the provider (``UnknownSessionDate`` propagates — a
  coverage gap is never silently "closed");
- weekends are classified non-trading STRUCTURALLY (no provider query), mirroring
  ``MarketCalendar.phase_at``'s weekend short-circuit, so generated fixtures are
  self-contained over contiguous ranges;
- ``cross_check_fixture`` compares the generated fixture against a HAND-AUTHORED
  holiday/half-day table and returns divergence strings (empty == pass). Every
  divergence is a human-review item, including "surprises" like a non-16:00 close
  on a day the table calls normal.

No wall clock, no randomness, no network: generation is byte-stable for a given
provider + range, so the committed fixture is reproducible.
"""
import json
from datetime import date, timedelta
from typing import Iterable, List, Mapping, Optional, Tuple

from agent.market_calendar import (
    ET,
    CalendarError,
    ScheduleProvider,
    SessionSchedule,
    _is_weekend,
    _parse_utc,
    _require_session_date,
)

# Hand-authored XNYS 2026 H2 expectations (published NYSE holiday calendar).
# July 4 2026 is a SATURDAY: observed holiday Friday 2026-07-03, and there is NO
# July-2 early close under that rule. Half-days close 13:00 ET.
XNYS_2026_H2_EXPECTATIONS = {
    "holidays": (
        "2026-06-19",  # Juneteenth National Independence Day (Friday)
        "2026-07-03",  # Independence Day observed (July 4 = Saturday)
        "2026-09-07",  # Labor Day (Monday)
        "2026-11-26",  # Thanksgiving Day (Thursday)
        "2026-12-25",  # Christmas Day (Friday)
    ),
    "half_days": (
        "2026-11-27",  # day after Thanksgiving
        "2026-12-24",  # Christmas Eve (Thursday)
    ),
}


def _et_clock(utc_iso: Optional[str]) -> Optional[str]:
    """UTC ISO boundary -> "HH:MM" ET wall clock (None passes through)."""
    if utc_iso is None:
        return None
    return _parse_utc(utc_iso).astimezone(ET).strftime("%H:%M")


def _date_range(start_date_et: str, end_date_et: str) -> Iterable[str]:
    start = date.fromisoformat(_require_session_date(start_date_et))
    end = date.fromisoformat(_require_session_date(end_date_et))
    if end < start:
        raise CalendarError(
            f"end date {end_date_et!r} precedes start date {start_date_et!r}")
    current = start
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def _session_entry(schedule: SessionSchedule) -> dict:
    if not schedule.is_trading_day:
        return {"is_trading_day": False, "is_early_close": False}
    entry = {
        "is_trading_day": True,
        "is_early_close": bool(schedule.is_early_close),
        "pre_open_et": _et_clock(schedule.pre_open_utc),
        "rth_open_et": _et_clock(schedule.rth_open_utc),
        "rth_close_et": _et_clock(schedule.rth_close_utc),
        "post_close_et": _et_clock(schedule.post_close_utc),
    }
    missing = [key for key, value in entry.items() if value is None]
    if missing:
        raise CalendarError(
            f"trading day {schedule.session_date_et} is missing windows "
            f"{missing} — refusing to emit a partial session (fail-closed)")
    return entry


def generate_session_fixture(provider: ScheduleProvider, *,
                             start_date_et: str, end_date_et: str,
                             mic: str, pin: str,
                             provenance: Optional[Mapping[str, str]] = None) -> dict:
    """Every calendar date in [start, end] becomes a sessions entry. Weekends are
    structural non-trading days (no provider query); weekday coverage gaps
    propagate as ``UnknownSessionDate`` (fail-closed)."""
    sessions = {}
    for session_date in _date_range(start_date_et, end_date_et):
        if _is_weekend(session_date):
            sessions[session_date] = {"is_trading_day": False,
                                      "is_early_close": False}
            continue
        sessions[session_date] = _session_entry(
            provider.schedule_for(session_date))
    fixture = {
        "mic": mic,
        "pin": pin,
        "coverage": {"start_date_et": start_date_et,
                     "end_date_et": end_date_et},
        "sessions": sessions,
    }
    if provenance:
        fixture["provenance"] = dict(provenance)
    return fixture


def cross_check_fixture(fixture: Mapping, *,
                        holidays: Iterable[str] = (),
                        half_days: Iterable[str] = ()) -> List[str]:
    """Compare a generated fixture against the hand-authored expectations.

    Returns divergence strings (empty == pass). Both directions are checked:
    the table's claims must hold in the fixture, AND the fixture may not
    contain surprises the table does not know about (an unexpected weekday
    closure, an unexpected early close, or a non-standard full-day close).
    """
    divergences: List[str] = []
    sessions = fixture.get("sessions")
    if not isinstance(sessions, Mapping) or not sessions:
        return ["fixture has no sessions map"]
    expected_holidays = frozenset(holidays)
    expected_half_days = frozenset(half_days)

    for session_date in expected_holidays | expected_half_days:
        if session_date not in sessions:
            divergences.append(
                f"expected date {session_date} missing from fixture coverage")

    for session_date in sorted(sessions):
        entry = sessions[session_date]
        trading = bool(entry.get("is_trading_day"))
        early = bool(entry.get("is_early_close"))
        weekend = _is_weekend(session_date)
        if session_date in expected_holidays:
            if trading:
                divergences.append(
                    f"{session_date}: hand table says HOLIDAY, fixture says "
                    "trading day")
            continue
        if weekend:
            if trading:
                divergences.append(
                    f"{session_date}: weekend marked as a trading day")
            continue
        if not trading:
            divergences.append(
                f"{session_date}: non-trading WEEKDAY not in the hand table "
                "(unexpected closure)")
            continue
        if session_date in expected_half_days:
            if not early:
                divergences.append(
                    f"{session_date}: hand table says HALF-DAY, fixture says "
                    "normal close")
            elif entry.get("rth_close_et") != "13:00":
                divergences.append(
                    f"{session_date}: half-day close is "
                    f"{entry.get('rth_close_et')!r}, expected '13:00'")
            continue
        if early:
            divergences.append(
                f"{session_date}: unexpected early close (not in hand table)")
        elif entry.get("rth_close_et") != "16:00":
            divergences.append(
                f"{session_date}: full-day close is "
                f"{entry.get('rth_close_et')!r}, expected '16:00'")
        if entry.get("rth_open_et") != "09:30":
            divergences.append(
                f"{session_date}: open is {entry.get('rth_open_et')!r}, "
                "expected '09:30'")
    return divergences


def write_fixture_json(fixture: Mapping, path) -> str:
    """Deterministic canonical-JSON write (repo convention); returns the text."""
    text = json.dumps(fixture, sort_keys=True, indent=2) + "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return text


def _main(argv: Optional[Tuple[str, ...]] = None) -> int:  # pragma: no cover - thin CLI
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate + cross-check a session fixture from the pinned "
                    "exchange_calendars provider (run under .venv).")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--mic", default="XNYS")
    parser.add_argument("--pin", required=True,
                        help='fixture pin, e.g. "fixture:XNYS-2026H2-v1"')
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    from agent.market_calendar import ExchangeCalendarsScheduleProvider

    provider = ExchangeCalendarsScheduleProvider(mic=args.mic)
    fixture = generate_session_fixture(
        provider, start_date_et=args.start, end_date_et=args.end,
        mic=args.mic, pin=args.pin,
        provenance={
            "generated_by": "agent.calendar_fixture",
            "source": f"exchange_calendars=={provider.calendar_pin()}",
            "cross_check": "XNYS_2026_H2_EXPECTATIONS hand table "
                           "(published NYSE holiday calendar)",
        })
    divergences = cross_check_fixture(
        fixture,
        holidays=XNYS_2026_H2_EXPECTATIONS["holidays"],
        half_days=XNYS_2026_H2_EXPECTATIONS["half_days"])
    if divergences:
        for divergence in divergences:
            print(f"DIVERGENCE: {divergence}")
        return 1
    write_fixture_json(fixture, args.output)
    trading = sum(1 for entry in fixture["sessions"].values()
                  if entry["is_trading_day"])
    print(f"wrote {args.output}: {len(fixture['sessions'])} dates, "
          f"{trading} trading days, cross-check clean")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
