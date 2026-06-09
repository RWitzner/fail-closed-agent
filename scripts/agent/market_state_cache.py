"""M2 §C — freshness-gated NON-BLOCKING tradability cache (degrade-to-SAFE).

Per-``(symbol, instrument_id)`` ``Verdict`` cache with an EXPLICIT TTL driven by the
INJECTED ms clock (``FakeClock`` offline — tests/lib/fakes.py:83-94) — the SAME
ms-clock seam ``HeartbeatMonitor`` uses (recorder/status.py:162). It is a PURE
function of explicit injected inputs: no wall clock, no hidden state, no network.

Fail-closed bindings (spec §5 Tier-2): a STALE or MISSING entry degrades to the
MOST-RESTRICTIVE ``safe_default_verdict`` immediately — ``get`` NEVER computes or
refreshes inline and NEVER blocks; it NEVER serves a stale "tradable". A stored
verdict whose ``instrument_id`` != the requested one (ticker reuse / stale
definition) is a MISS -> safe default (MED-7). Refresh is a separate, caller-driven
step; ``refresh_set`` unions open-position symbols so a held symbol is ALWAYS
re-evaluated even if it left the candidate universe.

The freshness boundary is strict ``>`` (fresh at exactly ``ttl_ms``, stale at
``ttl_ms+1``) mirroring ``HeartbeatMonitor.check`` (status.py:178). ``ttl_ms`` is a
CODE CONSTANT with a ctor-only TEST override that can only SHORTEN (tighten), never
lengthen, freshness (HIGH-3 never-loosen clamp; a larger TTL == staler == less safe;
§G ``min()``-merge would not protect it).
"""
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

from agent.market_state import (
    HaltState,
    LuldState,
    SessionState,
    SsrState,
    Tradability,
    Verdict,
)

DEFAULT_FRESHNESS_TTL_MS = 2000  # CODE CONSTANT (overlay-as-config is a min()-merge trap — §G); ctor override for tests only


@dataclass(frozen=True)
class CacheEntry:
    verdict: Verdict
    refreshed_at_ms: int  # monotonic ms (from injected clock) when this verdict was computed


class MarketStateCache:
    """Freshness-gated, NON-BLOCKING per-symbol tradability cache. ``clock`` is the
    injected ms clock (FakeClock offline) — the SAME seam HeartbeatMonitor uses. No
    new clock abstraction.

    get() NEVER computes/refreshes inline and NEVER blocks: a stale OR missing entry
    returns safe_default_verdict() immediately (degrade-to-safe). Refresh is a
    separate, caller-driven step."""

    def __init__(self, *, clock, ttl_ms: int = DEFAULT_FRESHNESS_TTL_MS) -> None:
        """NEVER-LOOSEN CLAMP (HIGH-3, test-only override): ttl_ms exists only for
        tests; __init__ RAISES ValueError unless ttl_ms <= DEFAULT_FRESHNESS_TTL_MS —
        a caller may only SHORTEN (tighten) freshness, never lengthen it (a larger
        TTL == staler == less safe). The API-level mirror of the §G code-constant
        stance; a ctor param is not a loophole around a code constant."""
        if ttl_ms > DEFAULT_FRESHNESS_TTL_MS:
            raise ValueError(
                "ttl_ms may only SHORTEN freshness (HIGH-3 never-loosen clamp): "
                f"{ttl_ms} > DEFAULT_FRESHNESS_TTL_MS={DEFAULT_FRESHNESS_TTL_MS}"
            )
        self._clock = clock
        self._ttl_ms = int(ttl_ms)
        # keyed on symbol; the stored CacheEntry carries the instrument_id, and get()
        # rejects a mismatch as a MISS (MED-7).
        self._entries: Dict[str, CacheEntry] = {}

    def put(self, verdict: Verdict, *, now_ms: Optional[int] = None) -> None:
        """Store stamped at now_ms (defaults to clock.now_ms())."""
        stamp = self._clock.now_ms() if now_ms is None else int(now_ms)
        self._entries[verdict.symbol] = CacheEntry(verdict=verdict, refreshed_at_ms=stamp)

    def get(
        self,
        symbol: str,
        instrument_id: int,
        session_date_et: str,
        *,
        now_ms: Optional[int] = None,
    ) -> Verdict:
        """Return the cached Verdict IFF the stored entry is keyed to the SAME
        (symbol, instrument_id) AND (now_ms - refreshed_at_ms) <= ttl_ms (fresh at
        exactly ttl_ms, stale at ttl_ms+1 — mirrors HeartbeatMonitor.check strict
        '>' at status.py:178). A stored verdict whose instrument_id != the requested
        one (ticker reused for a new instrument / stale definition) is a MISS ->
        safe_default_verdict (MED-7). Otherwise, and for a missing/stale entry,
        return safe_default_verdict(...). NON-BLOCKING — no inline refresh."""
        now = self._clock.now_ms() if now_ms is None else int(now_ms)
        entry = self._entries.get(symbol)
        if entry is None:
            return self.safe_default_verdict(symbol, instrument_id, session_date_et)
        # MED-7: instrument_id mismatch -> MISS (never serve a prior instrument's verdict).
        if entry.verdict.instrument_id != instrument_id:
            return self.safe_default_verdict(symbol, instrument_id, session_date_et)
        # strict '>' staleness boundary (status.py:178).
        if now - entry.refreshed_at_ms > self._ttl_ms:
            return self.safe_default_verdict(symbol, instrument_id, session_date_et)
        return entry.verdict

    @staticmethod
    def safe_default_verdict(symbol: str, instrument_id: int, session_date_et: str) -> Verdict:
        """The MOST-RESTRICTIVE verdict — EVERY enum field pinned to its
        most-restrictive member (S7-7): session_state=SessionState.UNKNOWN (the
        honest 'we don't know' state; NOT HALTED, which would assert a halt we have
        not observed — LOW-1), tradability=Tradability.NOT_TRADABLE,
        halt=HaltState.UNKNOWN, luld=LuldState.UNKNOWN, ssr=SsrState.UNKNOWN,
        two_sided_nbbo=False, short_allowed=False, ca_blackout=True,
        reasons=('cache_stale_safe_default',). Returned on any
        stale/missing/refresh-failure (spec §5 Tier-2 'degrades to safe default when
        stale')."""
        return Verdict(
            symbol=symbol,
            instrument_id=instrument_id,
            session_state=SessionState.UNKNOWN,
            tradability=Tradability.NOT_TRADABLE,
            halt=HaltState.UNKNOWN,
            luld=LuldState.UNKNOWN,
            ssr=SsrState.UNKNOWN,
            two_sided_nbbo=False,
            short_allowed=False,
            reasons=("cache_stale_safe_default",),
            ca_blackout=True,
            session_date_et=session_date_et,
        )

    def is_fresh(self, symbol: str, *, now_ms: Optional[int] = None) -> bool:
        now = self._clock.now_ms() if now_ms is None else int(now_ms)
        entry = self._entries.get(symbol)
        if entry is None:
            return False
        return now - entry.refreshed_at_ms <= self._ttl_ms

    def refresh_set(
        self,
        *,
        candidate_symbols: Iterable[str],
        open_position_symbols: Iterable[str],
    ) -> Tuple[str, ...]:
        """UNION of candidate_symbols and currently-held (open-position) symbols,
        deduped, SORTED for determinism. A held symbol is ALWAYS in the refresh set
        even if it left the candidate universe (spec §5 Tier-2 'union open-position
        symbols into the refresh set')."""
        return tuple(sorted(set(candidate_symbols) | set(open_position_symbols)))

    def stale_symbols(
        self, symbols: Iterable[str], *, now_ms: Optional[int] = None
    ) -> Tuple[str, ...]:
        """Symbols whose entry is missing or older than ttl_ms (mirrors
        HeartbeatMonitor.stale_symbols shape)."""
        now = self._clock.now_ms() if now_ms is None else int(now_ms)
        return tuple(sym for sym in symbols if not self.is_fresh(sym, now_ms=now))
