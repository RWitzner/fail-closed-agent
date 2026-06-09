"""Heartbeat + sequence/gap detection — S4 inputs (contract §G).

EXPLICIT DOWNGRADE (non-negotiable, recorded here):
  EQUS.MINI has NO ``status`` schema (verified 2026-06-09 entitlement matrix,
  contract §K).  Therefore halt/LULD/SSR status is NOT sourced from the feed.
  The PRIMARY source for halt/LULD/SSR status is the broker (Alpaca) +
  ``exchange_calendars``, owned by M2.

  ``EQUS_MINI_STATUS_DOWNGRADE`` is the canonical written record of this fact.
  There is no silent fallback — the absence of a status schema is WRITTEN, not
  assumed (spec §14 fail-closed; contract §K downgrade note).

Raises: none (returns data / Optional).

S4-inputs binding: ``GapReport`` + ``reconnect_epoch`` + heartbeat-staleness
are the freshness/epoch/gap inputs M5's execution preflight consumes.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# EXPLICIT DOWNGRADE — this is the entire reason this module exists in Wave 2.
#
# EQUS.MINI has no "status" schema (confirmed: list_schemas_response.json does
# not include "status" under "EQUS.MINI").  Halt/LULD/SSR status therefore
# comes from: broker (Alpaca, M2) + exchange_calendars (M2).  Any code that
# needs market-status information MUST wait for M2; it MUST NOT silently fall
# back to guessing from price action or omitting the check.
# ---------------------------------------------------------------------------
EQUS_MINI_STATUS_DOWNGRADE: str = (
    "EQUS.MINI has no 'status' schema: halt/LULD/SSR status primary source is "
    "broker (Alpaca) + exchange_calendars (M2). No silent fallback."
)


class SequencePolicy(str, Enum):
    MONOTONIC = "monotonic"  # vendor_seq must be prev+1; a jump => gap
    NONE = "none"            # dataset has no meaningful per-venue seq (EQUS.MINI composite) => never a gap


@dataclass(frozen=True)
class GapReport:
    symbol: str
    expected_seq: Optional[int]   # expected vendor_seq
    got_seq: Optional[int]        # observed vendor_seq
    gap_size: Optional[int]       # positive for kind='gap'; None otherwise (C1: never negative)
    kind: str                     # "gap" | "duplicate" | "out_of_order" | "reset_to_zero" | "malformed_seq" | "heartbeat_timeout"


class SequenceTracker:
    """Per-symbol monotonic-seq watcher keyed to the dataset's sequencing semantics.

    Observes ``ev.provenance.vendor_seq`` (BLOCKER 1 rename).

    ``policy=NONE``  -> never reports a 'gap' (composite feed; vendor_seq null/0)
                        -> NO false alerts.
    ``policy=MONOTONIC`` against ``expected = last + 1`` (C1 anomaly taxonomy):
        * ``got > expected`` -> kind='gap', gap_size = got - expected (POSITIVE;
                               count of missing messages).
        * ``got == last``    -> kind='duplicate', gap_size=None (a repeat).
        * ``got < last``     -> kind='out_of_order', gap_size=None (a backward
                               jump; a NEGATIVE gap_size is forbidden — C1).
    A vendor_seq RESET to 0 mid-stream is kind='reset_to_zero' (a reconnect/
    epoch marker, NOT a gap; expected_seq=None — C9).
    A null/malformed vendor_seq under MONOTONIC is kind='malformed_seq' (D2:
    surfaced as a data-quality signal, never a TypeError out of observe()).
    """

    def __init__(self, symbol: str, *, policy: SequencePolicy) -> None:
        self._symbol = symbol
        self._policy = policy
        self._last: Optional[int] = None  # last observed vendor_seq

    def observe(self, ev) -> Optional[GapReport]:
        """Return a GapReport on an anomaly, None on clean / policy-suppressed."""
        if self._policy is SequencePolicy.NONE:
            return None

        got = ev.provenance.vendor_seq

        # D2 (R2#2): a null/malformed vendor_seq under MONOTONIC must NOT crash the
        # comparison chain below (None < int raises TypeError in Py3). Surface it as
        # a data-quality signal and keep the baseline so the loop continues. Do NOT
        # advance _last (a null seq carries no position).
        if got is None:
            return GapReport(
                symbol=self._symbol,
                expected_seq=(self._last + 1) if self._last is not None else None,
                got_seq=None,
                gap_size=None,
                kind="malformed_seq",
            )

        # First observation: establish baseline, nothing to compare.
        if self._last is None:
            self._last = got
            return None

        # vendor_seq reset to 0: reconnect/epoch marker, not a gap.
        if got == 0:
            self._last = got
            return GapReport(
                symbol=self._symbol,
                expected_seq=None,  # reset has no meaningful expected continuation
                got_seq=0,
                gap_size=None,
                kind="reset_to_zero",
            )

        # Duplicate: got == last (a repeat, not a forward/backward move). C1.
        if got == self._last:
            return GapReport(
                symbol=self._symbol,
                expected_seq=self._last + 1,
                got_seq=got,
                gap_size=None,
                kind="duplicate",
            )

        # Out-of-order: got < last (a backward jump). gap_size is None — a
        # negative gap_size is forbidden (C1). Do NOT advance the baseline; the
        # last valid in-order seq remains the reference point.
        if got < self._last:
            return GapReport(
                symbol=self._symbol,
                expected_seq=self._last + 1,
                got_seq=got,
                gap_size=None,
                kind="out_of_order",
            )

        # Consecutive: no anomaly.
        expected = self._last + 1
        if got == expected:
            self._last = got
            return None

        # Forward gap: got > expected. gap_size = count of missing seqs (POSITIVE).
        gap_size = got - expected
        self._last = got
        return GapReport(
            symbol=self._symbol,
            expected_seq=expected,
            got_seq=got,
            gap_size=gap_size,
            kind="gap",
        )


class HeartbeatMonitor:
    """Injected-clock freshness watcher.

    ``quiet > timeout_ms`` -> GapReport(kind='heartbeat_timeout').
    Produces the freshness/epoch/gap INPUTS the M5 execution_preflight consumes
    (S4 inputs only; full S4 in M5).

    The clock is injected (``FakeClock`` offline) so the suite is deterministic
    and wall-clock-free.
    """

    def __init__(self, *, timeout_ms: int, clock) -> None:
        self._timeout_ms = timeout_ms
        self._clock = clock
        self._last_seen: Dict[str, int] = {}  # symbol -> ms timestamp of last touch

    def touch(self, symbol: str, now_ms: int) -> None:
        """Record a heartbeat / event receipt for ``symbol`` at ``now_ms``."""
        self._last_seen[symbol] = now_ms

    def check(self, symbol: str, now_ms: int) -> Optional[GapReport]:
        """Return a GapReport(kind='heartbeat_timeout') if ``symbol`` is stale,
        else None.  A symbol that has never been touched is not checked (returns
        None) — the monitor only tracks symbols it has seen at least once."""
        last = self._last_seen.get(symbol)
        if last is None:
            return None
        if now_ms - last > self._timeout_ms:
            return GapReport(
                symbol=symbol,
                expected_seq=None,
                got_seq=None,
                gap_size=None,
                kind="heartbeat_timeout",
            )
        return None

    def stale_symbols(self, now_ms: int) -> tuple:
        """Return a tuple of symbol strings whose last-seen timestamp is more than
        ``timeout_ms`` ago (i.e., those for which ``check`` would fire)."""
        return tuple(
            sym for sym, last in self._last_seen.items()
            if now_ms - last > self._timeout_ms
        )


def make_data_quality_alert(
    *,
    cause: str,
    symbol=None,
    detail=None,
    down_ms=None,
    reconnect_epoch: int = 0,
) -> dict:
    """Build the data_quality_alert row body.

    ``cause`` enumerated:
    'sequence_gap' | 'duplicate' | 'out_of_order' | 'reset_to_zero' |
    'malformed_seq' | 'heartbeat_timeout' | 'prolonged_disconnect' | 'crossed_book'.

    Written via EventWriter to the data_quality_alerts stream (sharing the
    agent run_id) — NEVER a silent exit.  No float values; safe to pass through
    ``agent.serializer.dumps``.
    """
    row: dict = {
        "cause": cause,
        "reconnect_epoch": reconnect_epoch,
    }
    if symbol is not None:
        row["symbol"] = symbol
    if detail is not None:
        row["detail"] = detail
    if down_ms is not None:
        row["down_ms"] = down_ms
    return row
