"""M2 §A — ScheduleProvider seam + pure ET schedule query (contract §A).

Exact analogue of ``DatabentoTransport`` (marketdata/databento.py:82-102): a
``@runtime_checkable`` Protocol with a pinned-fixture OFFLINE impl (stdlib
``zoneinfo`` only — NO heavy lib) and an ``exchange_calendars``-backed LIVE impl
whose ``import exchange_calendars`` lives ONLY inside its ``_build_calendar``
method, reached solely on the credentialed/live path.

OFFLINE PURITY (hard constraint, non-negotiable): there is NO module-scope
``import exchange_calendars`` anywhere here. ``exchange_calendars`` is NOT
installed; an unguarded top-level import would crash ``unittest discover`` at
COLLECTION and fail ALL tests on a bare checkout. The lib is lazy-imported at
exactly ONE line inside ``ExchangeCalendarsScheduleProvider._build_calendar``,
which raises ``NotImplementedError`` in M2 offline scope and is never reached
by an offline test.

TIME (frozen): ET market logic via stdlib ``zoneinfo`` (mirror
``recorder/bar_cache.py:26-27,31-43`` + its ``1d`` branch); every ``*_utc``
boundary is built ``datetime(y,m,d,H,M,tzinfo=ET).astimezone(UTC)`` so the RTH
close 16:00 ET lands on 20:00Z (EDT) / 21:00Z (EST) correctly across a DST flip.
Persist UTC.
"""
from dataclasses import dataclass
from datetime import datetime, time, timezone
from enum import Enum
from typing import Dict, Optional, Protocol, runtime_checkable
from zoneinfo import ZoneInfo  # STDLIB — DST-correct; NO exchange_calendars in the offline core

ET = ZoneInfo("America/New_York")  # reuse bar_cache.py:26 mechanism exactly
UTC = timezone.utc

EXCHANGE_CALENDARS_PIN = "4.13.2"  # provenance (empirically verified vs pandas 3.0.3 / py3.14)
CALENDAR_MIC = "XNYS"  # provenance

# Regular ET session boundaries. CODE CONSTANTS, NOT overlayable (§G):
PRE_OPEN_ET = time(4, 0)  # 04:00 ET pre-market open
RTH_OPEN_ET = time(9, 30)  # 09:30 ET
RTH_CLOSE_ET = time(16, 0)  # 16:00 ET
EARLY_CLOSE_ET = time(13, 0)  # 13:00 ET half-day close
POST_CLOSE_ET = time(20, 0)  # 20:00 ET post-market close (17:00 on half-days; fixture-supplied)


class SessionPhase(str, Enum):  # closed vocabulary (event.TRADE_SIDES pattern, event.py:33)
    PRE = "pre"
    RTH = "rth"  # CONTINUOUS session [09:30:00, 16:00:00) — owns the full window
    POST = "post"
    CLOSED = "closed"
    AUCTION = "auction"  # halt-RESUMPTION re-opening auction ONLY (vendor-signalled, §B); NOT a clock window
    UNKNOWN = "unknown"  # out-of-coverage date / failed lookup -> caller maps to NOT_TRADABLE (S7-spirit)


class CalendarError(ValueError):
    """Calendar query failure — fail-closed base (subclasses ValueError for broad catch)."""


class UnknownSessionDate(CalendarError):
    """A queried ET date is outside the provider's pinned coverage -> caller degrades to UNKNOWN/CLOSED."""


@dataclass(frozen=True)
class SessionSchedule:
    """Pure, immutable, hashable schedule for ONE ET session date. ``*_utc`` are UTC
    ISO-8601 (persist-UTC, spec §11); ``None`` where a window does not exist
    (holiday/weekend -> ``is_trading_day=False``, all windows ``None``). ``*_utc`` are
    built via construct-in-ET-then-``astimezone(UTC)`` (bar_cache.py ``1d`` branch) so the
    close lands at 20:00Z (EDT) / 21:00Z (EST) correctly across a DST flip."""

    session_date_et: str  # "YYYY-MM-DD" ET
    is_trading_day: bool  # False for weekend/holiday -> every window below is None
    is_early_close: bool  # True on a half-day (RTH close = 13:00 ET)
    pre_open_utc: Optional[str]
    rth_open_utc: Optional[str]
    rth_close_utc: Optional[str]  # 13:00 ET on a half-day, else 16:00 ET — DST-correct UTC
    post_close_utc: Optional[str]


@runtime_checkable
class ScheduleProvider(Protocol):
    """Injectable seam (mirrors marketdata.base.MarketDataTransport @runtime_checkable
    Protocol). OFFLINE = FixtureScheduleProvider; LIVE = ExchangeCalendarsScheduleProvider
    whose ``import exchange_calendars`` lives ONLY in its build path."""

    def schedule_for(self, session_date_et: str) -> SessionSchedule: ...
    def is_trading_day(self, session_date_et: str) -> bool: ...
    def calendar_pin(self) -> str: ...  # provenance string carried into status rows (§E)


# --- internal ET<->UTC boundary helpers (stdlib zoneinfo; bar_cache.py mechanism) ---


def _parse_utc(ts: str) -> datetime:
    """Parse an ISO-8601 UTC string to a tz-aware UTC datetime.

    Accepts a trailing ``Z`` OR a numeric offset like ``+00:00`` (the repo's own
    ``datetime.now(timezone.utc).isoformat()`` emits ``+00:00``); a naive (tz-less)
    string is rejected. Mirrors bar_cache._parse_utc (bar_cache.py:46-65) exactly.
    """
    raw = ts
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        raise ValueError(f"cannot parse UTC timestamp: {raw!r}")
    if dt.tzinfo is None:
        raise ValueError(f"timestamp is not timezone-aware: {raw!r}")
    return dt.astimezone(UTC)


def _et_boundary_to_utc_iso(session_date_et: str, et_clock: time) -> str:
    """Build a UTC ISO-8601 string for an ET wall-clock boundary on ``session_date_et``.

    Constructs ``datetime(y,m,d,H,M,tzinfo=ET)`` then ``.astimezone(UTC)`` (the
    bar_cache.py ``1d`` branch mechanism) so the offset is DST-correct per date
    (EDT vs EST) — NEVER a fixed UTC offset.

    DST-gap guard (DET-6): assert the ET boundary round-trips
    (``built.astimezone(UTC).astimezone(ET)`` reproduces the constructed wall-clock).
    A boundary that lands in the spring-forward skipped 02:00-03:00 ET hour does NOT
    round-trip (zoneinfo fold-shifts it forward by an hour), so this raises
    ``CalendarError`` rather than silently fold-shifting.
    """
    try:
        year, month, day = (int(part) for part in session_date_et.split("-"))
    except ValueError:
        raise CalendarError(f"malformed session_date_et: {session_date_et!r}")
    built_et = datetime(year, month, day, et_clock.hour, et_clock.minute, 0, tzinfo=ET)
    dt_utc = built_et.astimezone(UTC)
    # DST-gap round-trip guard: a skipped-hour boundary will not reproduce the
    # constructed ET wall-clock after a UTC round-trip.
    round_tripped = dt_utc.astimezone(ET)
    if (round_tripped.year, round_tripped.month, round_tripped.day,
            round_tripped.hour, round_tripped.minute) != (
            year, month, day, et_clock.hour, et_clock.minute):
        raise CalendarError(
            "ET boundary "
            f"{session_date_et} {et_clock.strftime('%H:%M')} falls in a DST-skipped "
            "hour (spring-forward gap) — fail-closed (DET-6)"
        )
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _is_weekend(session_date_et: str) -> bool:
    """True iff the ET calendar date is a Saturday or Sunday (weekday() 5 or 6).

    A weekend is structurally a non-trading day regardless of fixture coverage, so the
    facade can classify it CLOSED (never UNKNOWN, never "assume open") without a fixture
    lookup. Pure date arithmetic — no clock, no zoneinfo dependence (a date has no DST)."""
    try:
        year, month, day = (int(part) for part in session_date_et.split("-"))
    except ValueError:
        raise CalendarError(f"malformed session_date_et: {session_date_et!r}")
    from datetime import date

    return date(year, month, day).weekday() >= 5


def _parse_et_clock(value: str) -> time:
    """Parse an "HH:MM" ET wall-clock string from the fixture into a ``time``."""
    try:
        hour_str, minute_str = value.split(":")
        return time(int(hour_str), int(minute_str))
    except ValueError:
        raise CalendarError(f"malformed ET clock in fixture: {value!r}")


class FixtureScheduleProvider:
    """OFFLINE, stdlib-only. Loads a pinned calendar fixture (§H.1) + zoneinfo. Imports
    NO exchange_calendars (keeps test_no_network_no_creds green). A queried date NOT in
    the fixture raises ``UnknownSessionDate`` (fail-closed: never a silent 'assume open').

    DST-gap guard (DET-6): when constructing each ``*_utc`` the ET boundary must round-trip
    (``datetime(...,tzinfo=ET).astimezone(UTC).astimezone(ET) == constructed``); a malformed
    fixture boundary landing in the spring-forward skipped hour raises ``CalendarError`` so
    it cannot silently fold-shift.
    """

    def __init__(self, fixture: dict, *, pin: str) -> None:
        # Pure dict input; no client, no creds. Read the sessions map defensively.
        self._sessions: Dict[str, dict] = dict(fixture.get("sessions", {}))
        self._mic = fixture.get("mic", CALENDAR_MIC)
        self._fixture_pin = fixture.get("pin")
        self._pin = pin

    def schedule_for(self, session_date_et: str) -> SessionSchedule:
        raw = self._sessions.get(session_date_et)
        if raw is None:
            raise UnknownSessionDate(
                f"session date outside pinned fixture coverage: {session_date_et!r} "
                "(fail-closed: never assume open)"
            )
        is_trading_day = bool(raw["is_trading_day"])
        is_early_close = bool(raw["is_early_close"])
        if not is_trading_day:
            return SessionSchedule(
                session_date_et=session_date_et,
                is_trading_day=False,
                is_early_close=is_early_close,
                pre_open_utc=None,
                rth_open_utc=None,
                rth_close_utc=None,
                post_close_utc=None,
            )

        def _window(key: str) -> Optional[str]:
            value = raw.get(key)
            if value is None:
                return None
            return _et_boundary_to_utc_iso(session_date_et, _parse_et_clock(value))

        return SessionSchedule(
            session_date_et=session_date_et,
            is_trading_day=True,
            is_early_close=is_early_close,
            pre_open_utc=_window("pre_open_et"),
            rth_open_utc=_window("rth_open_et"),
            rth_close_utc=_window("rth_close_et"),
            post_close_utc=_window("post_close_et"),
        )

    def is_trading_day(self, session_date_et: str) -> bool:
        return self.schedule_for(session_date_et).is_trading_day

    def calendar_pin(self) -> str:  # e.g. "fixture:XNYS-2026-v1"
        return self._pin


class ExchangeCalendarsScheduleProvider:
    """LIVE/credentialed. The ONLY ``import exchange_calendars`` in M2 lives inside
    ``_build_calendar()``, reached solely on the live path — so importing this module
    offline pulls nothing heavy into ``sys.modules`` (mirrors
    ``DatabentoTransport._build_real_client``, databento.py:93-102)."""

    def __init__(self, *, mic: str = CALENDAR_MIC, pin: str = EXCHANGE_CALENDARS_PIN) -> None:
        self._mic = mic
        self._pin = pin

    def _build_calendar(self):  # pragma: no cover - live
        """Lazily ``import exchange_calendars``; build ``ec.get_calendar(mic)``. Raises
        ``NotImplementedError`` in M2 offline scope; never reached by an offline test."""
        raise NotImplementedError(
            "exchange_calendars-backed provider lands when the live path is wired"
        )

    def schedule_for(self, session_date_et: str) -> SessionSchedule:  # pragma: no cover - live
        self._build_calendar()
        raise NotImplementedError(
            "exchange_calendars-backed provider lands when the live path is wired"
        )

    def is_trading_day(self, session_date_et: str) -> bool:  # pragma: no cover - live
        self._build_calendar()
        raise NotImplementedError(
            "exchange_calendars-backed provider lands when the live path is wired"
        )

    def calendar_pin(self) -> str:
        return self._pin


class MarketCalendar:
    """Pure query facade over an injected ScheduleProvider. No clock, no IO beyond the
    provider; deterministic given the provider."""

    def __init__(self, provider: ScheduleProvider) -> None:
        self._provider = provider

    def session_date_for(self, ts_utc_iso: str) -> str:
        """UTC instant -> ET session date "YYYY-MM-DD" (reuses bar_cache.et_session_date
        semantics; accepts 'Z' and '+00:00', rejects naive)."""
        dt_utc = _parse_utc(ts_utc_iso)
        dt_et = dt_utc.astimezone(ET)
        return dt_et.strftime("%Y-%m-%d")

    def schedule_for(self, session_date_et: str) -> SessionSchedule:
        """M3 additive passthrough (contract rev2 BUILD-F2): delegate to the provider.
        Propagates ``UnknownSessionDate`` unchanged (fail-closed; the M3 horizon gate
        maps it to ``calendar_unknown``). No M2 behavior changes."""
        return self._provider.schedule_for(session_date_et)

    def calendar_pin(self) -> str:
        """M3 additive passthrough: the provider's provenance pin string."""
        return self._provider.calendar_pin()

    def phase_at(self, ts_utc_iso: str) -> SessionPhase:
        """PURE function of (instant, schedule). Convert ts_utc -> ET, classify against
        the schedule:
          - not is_trading_day -> CLOSED.
          - within [rth_open, rth_close) -> RTH.   (CONTINUOUS session owns the full
            window; the open/close cross is a point-in-time event, not a multi-minute
            suspension — MR-1.)
          - within [pre_open, rth_open) -> PRE.    within [rth_close, post_close) -> POST.
          - otherwise -> CLOSED.
        ``UnknownSessionDate`` -> caller (the TradabilityInputs builder) sets
        ``session_phase=UNKNOWN`` and reason 'calendar_unknown' (§B). DST-correct:
        comparison is on the parsed UTC instant against the schedule's ``*_utc`` fields.
        NOTE: ``SessionPhase.AUCTION`` is NEVER produced here — it is reserved for the
        halt-resumption re-opening auction the decider derives from vendor halt state (§B).
        """
        session_date_et = self.session_date_for(ts_utc_iso)
        # A calendar weekend (Sat/Sun) is STRUCTURALLY a non-trading day, derivable
        # from the ET date itself — classify CLOSED before consulting the fixture so a
        # weekend instant never raises UnknownSessionDate (§H.2 row 8 / §J
        # test_holiday_and_weekend_are_closed: "a Saturday -> CLOSED"). This is the
        # UnknownSessionDate docstring's "degrades to ... CLOSED" outcome and never
        # "assumes open"; a non-weekend weekday absent from coverage still raises.
        if _is_weekend(session_date_et):
            return SessionPhase.CLOSED
        schedule = self._provider.schedule_for(session_date_et)
        if not schedule.is_trading_day:
            return SessionPhase.CLOSED

        instant = _parse_utc(ts_utc_iso)
        pre_open = _parse_utc(schedule.pre_open_utc) if schedule.pre_open_utc else None
        rth_open = _parse_utc(schedule.rth_open_utc) if schedule.rth_open_utc else None
        rth_close = _parse_utc(schedule.rth_close_utc) if schedule.rth_close_utc else None
        post_close = _parse_utc(schedule.post_close_utc) if schedule.post_close_utc else None

        # CONTINUOUS RTH owns the full [rth_open, rth_close) window (MR-1).
        if rth_open is not None and rth_close is not None and rth_open <= instant < rth_close:
            return SessionPhase.RTH
        if pre_open is not None and rth_open is not None and pre_open <= instant < rth_open:
            return SessionPhase.PRE
        if rth_close is not None and post_close is not None and rth_close <= instant < post_close:
            return SessionPhase.POST
        return SessionPhase.CLOSED
