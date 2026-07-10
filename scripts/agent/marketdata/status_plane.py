"""Track B — the per-symbol status data plane (halt / LULD / SSR).

The quote feed (``EQUS.MINI``) has NO status schema (recorder/status.py), so a
paper session needs a SEPARATE fail-closed status plane before any open is
possible: without a composed ``status_provider`` the §M.3 step-2 builder falls
back to ``StatusFlags(UNKNOWN)`` and the tradability decider blocks every open
(the P0-1 posture — safe, but permanently untradable).

This module supplies the plane per the 2026-07-09 autonomous-paper-trader
design (§Track B), fail-closed at every edge:

- no observation for a symbol ⇒ ``None`` ⇒ UNKNOWN ⇒ NOT_TRADABLE;
- a stale or disconnected STREAM blocks every symbol (silence is meaningful
  only on a live stream: halts arrive as events, so per-symbol silence after a
  baseline is healthy, stream-level silence is not);
- after a disconnect every symbol must be RE-observed (epoch bump) — states
  can change while the stream is down;
- conflicting same-instant observations resolve to the MOST RESTRICTIVE state;
- a malformed event drops its symbol to UNKNOWN (an unreliable plane must not
  keep vouching for the symbol) and the drop is journalable;
- LULD bands decay: a band older than ``band_max_age_ms`` degrades the LULD
  field to UNKNOWN (restrictive) while halt/SSR keep their event semantics;
- the calendar NEVER substitutes for symbol status (this module never reads
  the calendar; the replay/rehearsal windows provider lives in the
  orchestrator and is composed only where nothing can open by construction);
- every accepted transition is recorded and drainable for journaling with
  source + both timestamps.

The credentialed Alpaca ``statuses``/``lulds`` adapter is UNVERIFIED and
REFUSES to run (same tier-2b posture as ``databento_live_source``): the Track D
status-coverage drill must pin the real channel payloads first, then the flag
flips in a reviewed commit. Offline, the pump/normalization contract is pinned
via an injected scripted source.
"""
import threading
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, Iterable, List, Mapping, Optional

from agent.bar_series import _parse_utc
from agent.market_state import (
    HaltReason,
    HaltState,
    LuldBand,
    LuldState,
    LuldTier,
    SsrState,
    StatusFlags,
)

# Restrictiveness ranks for same-instant conflict resolution (higher wins).
_HALT_RANK = {
    HaltState.NONE: 0,
    HaltState.RESUMING: 1,
    HaltState.PAUSED_LULD: 2,
    HaltState.HALTED: 3,
}
_LULD_RANK = {
    LuldState.NORMAL: 0,
    LuldState.LIMIT: 1,
    LuldState.PAUSED: 2,
}
_SSR_RANK = {
    SsrState.INACTIVE: 0,
    SsrState.ACTIVE: 1,
}

DEFAULT_STREAM_STALE_MS = 15_000
DEFAULT_BAND_MAX_AGE_MS = 300_000


def _band_from_payload(payload) -> Optional[LuldBand]:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise ValueError("luld_band must be a mapping or None")
    try:
        return LuldBand(
            reference_px=Decimal(str(payload["reference_px"])),
            lower_px=Decimal(str(payload["lower_px"])),
            upper_px=Decimal(str(payload["upper_px"])),
            tier=LuldTier(payload["tier"]),
            doubled=bool(payload["doubled"]),
        )
    except (KeyError, InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"malformed luld_band: {exc}") from exc


class _FieldState:
    __slots__ = ("value", "extra", "ts_event_utc", "seen_at_ms")

    def __init__(self, value, extra, ts_event_utc: str, seen_at_ms: int):
        self.value = value
        self.extra = extra
        self.ts_event_utc = ts_event_utc
        self.seen_at_ms = seen_at_ms


class LiveStatusProvider:
    """Latest-state, fail-closed status provider over normalized events.

    Normalized event contract (vendor adapters produce this):
        {"symbol": str, "ts_event_utc": iso-utc, "source": str,
         # any subset of the three field groups:
         "halt": HaltState value, "halt_reason": HaltReason value,
         "luld": LuldState value, "luld_band": mapping | None,
         "ssr": SsrState value}

    A BASELINE is simply an event carrying all three groups — the live adapter
    must emit one per subscribed symbol on every (re)connect ack, because after
    a disconnect the provider refuses to vouch for any symbol until it has been
    re-observed (epoch bump).

    The callable form is the pinned orchestrator seam:
    ``provider(symbol, ts_utc) -> Optional[StatusFlags]``.
    """

    def __init__(self, clock, *,
                 stream_stale_ms: int = DEFAULT_STREAM_STALE_MS,
                 band_max_age_ms: int = DEFAULT_BAND_MAX_AGE_MS) -> None:
        self._clock = clock
        self._stream_stale_ms = int(stream_stale_ms)
        self._band_max_age_ms = int(band_max_age_ms)
        self._lock = threading.Lock()
        self._connected = False
        self._epoch = 0
        self._last_alive_ms: Optional[int] = None
        # symbol -> {"epoch": int, "source": str,
        #            "halt"|"luld"|"ssr": _FieldState}
        self._symbols: Dict[str, dict] = {}
        self._transitions: List[dict] = []

    # -- ingest side (pump thread) --------------------------------------------

    def ingest(self, event: Mapping) -> None:
        now_ms = self._clock.now_ms()
        with self._lock:
            self._connected = True
            self._last_alive_ms = now_ms
            try:
                self._ingest_locked(event, now_ms)
            except (KeyError, ValueError, TypeError) as exc:
                symbol = (event.get("symbol")
                          if isinstance(event, Mapping) else None)
                if isinstance(symbol, str) and symbol:
                    self._symbols.pop(symbol, None)
                self._transitions.append({
                    "symbol": symbol if isinstance(symbol, str) else "?",
                    "field": "malformed",
                    "from": None, "to": "unknown",
                    "ts_event_utc": (event.get("ts_event_utc")
                                     if isinstance(event, Mapping) else None),
                    "seen_at_ms": now_ms,
                    "source": (event.get("source")
                               if isinstance(event, Mapping) else None),
                    "detail": str(exc),
                })

    def _ingest_locked(self, event: Mapping, now_ms: int) -> None:
        symbol = event["symbol"]
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("event symbol must be a non-empty string")
        ts_event = event["ts_event_utc"]
        instant = _parse_utc(ts_event)      # raises on malformed timestamps
        source = event["source"]
        if not isinstance(source, str) or not source:
            raise ValueError("event source must be a non-empty string")

        record = self._symbols.get(symbol)
        if record is None or record["epoch"] != self._epoch:
            record = {"epoch": self._epoch, "source": source}
            self._symbols[symbol] = record
        record["source"] = source

        if "halt" in event:
            halt = HaltState(event["halt"])
            reason = HaltReason(event["halt_reason"])
            self._overlay(record, symbol, "halt", halt, reason,
                          _HALT_RANK, ts_event, instant, now_ms, source)
        if "luld" in event:
            luld = LuldState(event["luld"])
            band = _band_from_payload(event.get("luld_band"))
            self._overlay(record, symbol, "luld", luld, band,
                          _LULD_RANK, ts_event, instant, now_ms, source)
        if "ssr" in event:
            ssr = SsrState(event["ssr"])
            self._overlay(record, symbol, "ssr", ssr, None,
                          _SSR_RANK, ts_event, instant, now_ms, source)

    def _overlay(self, record, symbol, field, value, extra, rank,
                 ts_event, instant, now_ms, source) -> None:
        current: Optional[_FieldState] = record.get(field)
        if current is not None:
            current_instant = _parse_utc(current.ts_event_utc)
            if instant < current_instant:
                return                       # late out-of-order delivery
            if instant == current_instant and rank[value] <= rank[current.value]:
                # same-instant conflict: most restrictive wins; equal or less
                # restrictive duplicates are no-ops (but a fresher band on an
                # equal-restrictiveness luld update is still an overlay).
                if not (field == "luld" and extra is not None
                        and rank[value] == rank[current.value]):
                    return
        previous = current.value if current is not None else None
        record[field] = _FieldState(value, extra, ts_event, now_ms)
        if previous != value:
            self._transitions.append({
                "symbol": symbol, "field": field,
                "from": previous.value if previous is not None else None,
                "to": value.value,
                "ts_event_utc": ts_event, "seen_at_ms": now_ms,
                "source": source,
            })

    def mark_disconnected(self, *, reason: str = "disconnected") -> None:
        with self._lock:
            was_connected = self._connected
            self._connected = False
            self._epoch += 1
            self._transitions.append({
                "symbol": "*", "field": "stream",
                "from": "connected" if was_connected else "disconnected",
                "to": "disconnected",
                "ts_event_utc": None,
                "seen_at_ms": self._clock.now_ms(),
                "source": None, "detail": reason,
            })

    def drain_transitions(self) -> List[dict]:
        with self._lock:
            drained, self._transitions = self._transitions, []
            return drained

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    # -- lookup side (tick loop) -----------------------------------------------

    def __call__(self, symbol: str, ts_utc: str) -> Optional[StatusFlags]:
        now_ms = self._clock.now_ms()
        with self._lock:
            if not self._connected or self._last_alive_ms is None:
                return None
            if now_ms - self._last_alive_ms > self._stream_stale_ms:
                return None                  # stream stale ⇒ silence means nothing
            record = self._symbols.get(symbol)
            if record is None or record["epoch"] != self._epoch:
                return None                  # never observed since (re)connect
            halt: Optional[_FieldState] = record.get("halt")
            luld: Optional[_FieldState] = record.get("luld")
            ssr: Optional[_FieldState] = record.get("ssr")

            luld_state = LuldState.UNKNOWN
            luld_band = None
            if luld is not None:
                luld_state = luld.value
                luld_band = luld.extra
                if (luld_band is not None
                        and now_ms - luld.seen_at_ms > self._band_max_age_ms):
                    # decayed band: refuse to vouch for the LULD field
                    luld_state = LuldState.UNKNOWN
                    luld_band = None

            return StatusFlags(
                symbol=symbol,
                halt=halt.value if halt is not None else HaltState.UNKNOWN,
                halt_reason=(halt.extra if halt is not None
                             else HaltReason.UNKNOWN),
                luld=luld_state,
                luld_band=luld_band,
                ssr=ssr.value if ssr is not None else SsrState.UNKNOWN,
                prior_close=None,
                source=record["source"],
            )


def pump_status_events(source: Iterable[Mapping],
                       provider: LiveStatusProvider) -> None:
    """Drive a normalized-event source into the provider; NEVER raises out.

    Runs on a daemon thread in the live composition. Exhaustion or any source
    exception marks the provider disconnected (⇒ every symbol UNKNOWN ⇒ opens
    blocked) — the fail-closed direction, not a crash."""
    try:
        for event in source:
            provider.ingest(event)
    except Exception as exc:  # noqa: BLE001 — fail-closed, never crash the pump
        provider.mark_disconnected(reason=f"source_error: {exc!r}")
        return
    provider.mark_disconnected(reason="source_exhausted")


def alpaca_status_source(*, symbols,
                         credentials_path=None,
                         allow_unverified_status: bool = False,
                         stream_factory: Optional[Callable] = None):
    """The credentialed Alpaca ``statuses``/``lulds`` source — UNVERIFIED.

    Fail-closed until the Track D status-coverage drill pins the REAL channel
    payloads (subscription ack, per-symbol baseline, halt/LULD/SSR message
    shapes) against the live account — the same one-session verification
    posture ``databento_live_source`` followed for bbo-1s. Until then this
    seam refuses to run; the paper composition degrades to a DISCONNECTED
    provider (opens blocked), never to a calendar substitute.

    ``stream_factory`` is the offline test seam: an injected callable
    returning an iterable of NORMALIZED events (the pump + provider contract
    is pinned offline; only the vendor client construction awaits the drill).
    """
    if stream_factory is not None:
        return stream_factory()
    raise NotImplementedError(
        "the Alpaca statuses/lulds status stream is UNVERIFIED: run the "
        "Track D status-coverage drill against the real paper account to pin "
        "the channel payloads, then flip this seam in a reviewed commit "
        "(allow_unverified_status is reserved for that verification session)"
        + ("" if allow_unverified_status else
           " — and it was not requested (allow_unverified_status=False)"))
