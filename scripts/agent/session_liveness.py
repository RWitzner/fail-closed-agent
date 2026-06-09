"""M2 session-aware liveness predicate (contract §F).

The recorder's ``heartbeat_timeout`` alert is session-UNAWARE: a symbol that stops
quoting during a legitimately closed session would otherwise raise a false
``data_quality_alert``. ``SessionLiveness`` is the injectable predicate the recorder
consults BEFORE emitting ``heartbeat_timeout``; a closed/unknown session suppresses
the alert (a closed-session quiet is NOT an error -- §F frozen posture, mirroring
``status.py``'s "every anomaly is DATA, never an exception").

Offline-pure: this module imports the M2 calendar TYPES only -- no
``exchange_calendars``, no broker/data SDK, no socket. The recorder imports the
``SessionLiveness`` TYPE only; it never constructs a calendar. ``SequenceTracker``
is untouched (session-awareness applies only to the heartbeat/quiet path; the seq
path is ``policy=NONE`` for EQUS.MINI, ``status.py:77``).
"""
from typing import Callable, Protocol, runtime_checkable

from agent.market_calendar import CalendarError, MarketCalendar, SessionPhase

# Phases at which a symbol is EXPECTED to be quoting; a quiet gap in one of these is
# a real data-quality signal (alert fires). CLOSED / UNKNOWN -> not expected live.
_LIVE_PHASES = frozenset({SessionPhase.PRE, SessionPhase.RTH, SessionPhase.POST})


@runtime_checkable
class SessionLiveness(Protocol):
    """Injectable predicate seam (mirrors ``MarketDataTransport`` @runtime_checkable
    Protocol). The recorder guards both ``heartbeat_timeout`` emit sites with
    ``expected_live(symbol, now_ms)``; ``liveness is None`` reproduces byte-identical
    M1 behavior (no suppression)."""

    def expected_live(self, symbol: str, now_ms: int) -> bool:
        """True iff ``symbol`` is EXPECTED to be quoting at this ET instant (calendar
        phase in {PRE, RTH, POST} on a trading day; not CLOSED/UNKNOWN). PURE; the
        calendar and the now_ms -> UTC-ISO derivation are injected."""
        ...


class CalendarLiveness:
    """``expected_live`` = ``phase_at(clock_to_utc_iso(now_ms)) in {PRE, RTH, POST}``.

    A CLOSED session, or any calendar lookup failure (``UnknownSessionDate`` /
    ``CalendarError``), yields ``False`` -> the recorder SUPPRESSES the
    ``heartbeat_timeout`` (§F: a legitimately closed/unknown session is not a feed
    fault). PURE: the calendar and the ms->UTC-ISO conversion are injected, so there
    is no clock, IO, or wall-time here.
    """

    def __init__(self, *, calendar: MarketCalendar, clock_to_utc_iso: Callable[[int], str]) -> None:
        self._calendar = calendar
        self._clock_to_utc_iso = clock_to_utc_iso

    def expected_live(self, symbol: str, now_ms: int) -> bool:
        try:
            phase = self._calendar.phase_at(self._clock_to_utc_iso(now_ms))
        except CalendarError:
            # UnknownSessionDate is a CalendarError subclass; an uncovered/unknown
            # session is not "expected live" -> suppress (fail toward NOT-alerting a
            # legitimately quiet symbol, per the §F frozen posture).
            return False
        return phase in _LIVE_PHASES
