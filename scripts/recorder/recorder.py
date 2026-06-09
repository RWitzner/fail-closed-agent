"""Single-writer ingest loop + always-on reconnect (contract §F).

The recorder is the live spine of tier-1: it drives
``transport.stream(symbols)`` -> ``event.parse`` (INJECTING the receipt stamp) ->
``EquityBookState.apply`` -> ``book_hash`` -> ``EventWriter.write_event``, while
running sequence/heartbeat/gap detection (S4 inputs) on the side.

Liveness (spec §5 tier1, NON-NEGOTIABLE):
  * Reconnect is ALWAYS-ON with UNBOUNDED attempts and an exponential backoff whose
    DELAY is capped (``min(cap_ms, base_ms*factor**n)``). The ported 5-attempt cap is
    STRUCTURALLY ABSENT — ``BackoffPolicy`` has no ``max_attempts`` field.
  * A disconnect surfaces as ``TransportDisconnected`` raised by the (Flaky)transport;
    the recorder CATCHES it, bumps ``reconnect_epoch``, sleeps the injected backoff,
    re-calls ``stream()`` — it NEVER returns silently on a disconnect.
  * A disconnect held longer than ``alert_after_ms`` writes a ``prolonged_disconnect``
    ``data_quality_alert`` ROW (alerts are DATA, not exceptions). A detected sequence
    gap / reset-to-zero / heartbeat-timeout / crossed-book likewise writes an alert
    row and keeps running.
  * D3 (contract): the disconnect instant is captured ONCE per outage so a
    multi-attempt outage's CUMULATIVE downtime is measured against a STABLE origin.
  * D9 (contract): connected-but-quiet heartbeat IS wired — a symbol that goes
    quiet (> timeout_ms) WHILE the connection is healthy surfaces a
    ``heartbeat_timeout`` alert (not only at reconnect time), deterministic offline
    via the injected clock + HeartbeatMonitor.
  * D6 (contract): the recorder holds one SequenceTracker PER SYMBOL (lazily minted
    from the injected prototype's policy) so an interleaved multi-symbol stream is
    never cross-contaminated.
  * D2 (contract): a null/malformed vendor_seq surfaces a ``malformed_seq`` alert
    and the loop CONTINUES — never a TypeError out of ``run()``.
  * E3 (R3#3): one malformed frame must NOT kill ingest for the universe — the
    per-frame parse fail-closed family (``MalformedRecord`` / ``NonFinitePrice`` /
    ``PrecisionLoss``) is caught for a SINGLE frame, surfaced as a loud
    ``malformed_record`` ``data_quality_alert``, then the loop CONTINUES. A real
    disconnect and any unknown exception are NOT caught here. (Completes D2 for the
    non-null malformed case, e.g. ``vendor_seq='BAD'`` or a malformed price.)
  * F1 (R4#1): the per-frame containment spans the FULL per-frame body, not just
    ``parse()``. A frame that parses fine but raises ``BookStateError`` in the
    apply/persist path (a mismatched ``instrument_id`` from symbol recycling / a
    corporate-action remap — equity symbol strings are not stable ids) is caught
    for the SINGLE frame, surfaced as a loud ``book_state_error``
    ``data_quality_alert`` (carrying ``symbol`` + the offending ``instrument_id``),
    then the loop CONTINUES. ``TransportDisconnected`` still routes to reconnect;
    ALL OTHER exceptions still propagate (fail-fast on programmer error).
  * E1 (R3#1): ``prolonged_disconnect`` is ONE-SHOT per outage (``_prolonged_alerted``
    dedup, mirroring ``_stale_alerted``), cleared on a successful receive.

Determinism / purity (offline):
  * Fed a Fake/Flaky transport + an injected ms clock (``FakeClock``) + an injected
    async ``sleep`` (no real wall-clock sleep, no network, no credential read).
  * Every persisted row / hash flows through ``agent.serializer`` via ``EventWriter``
    (which wraps ``JournalWriter``) — there is exactly ONE canonicalization path.
  * ``reconnect_epoch`` is stamped on every parsed event's ``Provenance`` (S4 input)
    and rides INSIDE the flat persisted row.
  * The recorder SHARES the injected ``run_id`` namespace (via the injected writers);
    it does NOT mint its own, so events + data_quality_alerts correlate (S6).

``ts_recv_utc``: the receipt stamp is derived from the injected ms clock
(``clock.now_ms()`` -> UTC ISO-8601) so it is deterministic and wall-clock-free
offline. Integer-ms arithmetic only — no float ever enters the pipeline.

The disconnect signal type is imported from ``tests.lib.fakes`` lazily inside the
loop so this production module has no test-package import at module scope; the
real transport raises the SAME ``TransportDisconnected`` type (frozen in §F2).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from recorder.book_hash import book_hash
from recorder.book_state import BookStateError, EquityBookState
from recorder.event import (
    DepthEvent,
    MalformedRecord,
    NonFinitePrice,
    PrecisionLoss,
    QuoteEvent,
    parse,
)
from recorder.persistence import STREAM_DATA_QUALITY
from recorder.status import make_data_quality_alert

# E3 (R3#3): the parse fail-closed family. A SINGLE malformed frame raising one of
# these MUST NOT stop ingest for the whole universe — the recorder records a loud
# ``malformed_record`` alert and continues. ``PrecisionLoss`` subclasses
# ``MalformedRecord`` (so it is already covered) but is listed for explicitness.
_PARSE_FAIL_CLOSED = (MalformedRecord, NonFinitePrice, PrecisionLoss)

# F1 (R4#1): the per-frame recoverable family now spans the FULL per-frame body, not
# just ``parse()``. E3 wrapped only the parse step, but a frame that parses fine can
# still raise ``BookStateError`` in the apply/persist path (e.g. a mismatched
# instrument_id from symbol recycling / a corporate-action remap — a real vendor
# condition, equity symbol strings are not stable ids). That raised OUTSIDE the
# parse-only try and killed ingest for the whole universe. So the recoverable family
# is {parse family} ∪ {BookStateError}: any of these for a SINGLE frame -> a loud
# data_quality_alert (``malformed_record`` for the parse family, ``book_state_error``
# for ``BookStateError`` carrying symbol+instrument_id) then CONTINUE.
# ``TransportDisconnected`` still routes to reconnect; ALL OTHER exceptions still
# propagate (fail-fast on programmer error — no bare ``except``). The two causes are
# caught in distinct clauses (below) so each alert carries the metadata in scope:
# the parse family has no parsed event yet; ``BookStateError`` carries the parsed
# event's provenance.

__all__ = ["BackoffPolicy", "RecorderStats", "Recorder", "TransportDisconnected"]

_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)


class TransportDisconnected(Exception):
    """Recoverable disconnect signal (frozen in §F2). A transport raises this to tell
    the recorder loop to bump ``reconnect_epoch``, sleep the backoff, and re-stream.

    Defined here as the canonical production type; ``tests.lib.fakes.FlakyTransport``
    raises ITS ``TransportDisconnected`` — the loop catches by NAME-equivalence via a
    structural check (``type(exc).__name__ == "TransportDisconnected"``) so a fault
    injected by either definition is handled identically. Never a fatal/silent exit.
    """


def _is_disconnect(exc: BaseException) -> bool:
    """True for any ``TransportDisconnected`` (this module's or the fakes' copy)."""
    return type(exc).__name__ == "TransportDisconnected"


@dataclass(frozen=True)
class BackoffPolicy:
    base_ms: int = 250
    factor: int = 2
    cap_ms: int = 30000          # backoff DELAY cap (spec §5: unbounded ATTEMPTS, capped delay)
    alert_after_ms: int = 60000  # sustained-disconnect threshold -> data_quality_alert
    # DELIBERATELY no `max_attempts` field: the ported 5-attempt cap is structurally
    # absent (spec §5 tier1 "new"). Reconnect attempts are unbounded.


@dataclass(frozen=True)
class RecorderStats:
    events_written: int
    alerts_emitted: int
    reconnects: int
    final_book_hashes: dict       # {symbol -> book_hash}


def _ms_to_iso_utc(now_ms: int) -> str:
    """Integer-ms epoch -> ISO-8601 UTC string (float-free, deterministic).

    Uses ``timedelta(milliseconds=int)`` so no float ever enters the pipeline.
    """
    dt = _EPOCH_UTC + timedelta(milliseconds=int(now_ms))
    return dt.isoformat().replace("+00:00", "Z")


class Recorder:
    """Drives the ingest loop with always-on reconnect (see module docstring)."""

    def __init__(
        self,
        transport,                       # MarketDataTransport (Fake/Flaky offline)
        writer,                          # EventWriter for the `events` stream
        *,
        dataset: str,
        schema: str,
        symbols,
        clock,                           # injected ms clock (FakeClock offline)
        sleep,                           # injected async sleep(delay_ms) (no real sleep)
        backoff: BackoffPolicy = BackoffPolicy(),
        sequence_tracker,               # SequenceTracker
        heartbeat,                      # HeartbeatMonitor
        alert_writer=None,              # EventWriter for the `data_quality_alerts` stream
        liveness=None,                  # M2 §F: optional SessionLiveness predicate; None == M1 behavior
    ) -> None:
        self._transport = transport
        self._writer = writer
        self._dataset = dataset
        self._schema = schema
        self._symbols = tuple(symbols)
        self._clock = clock
        self._sleep = sleep
        self._backoff = backoff
        # D6 (R2#6): per-symbol sequence tracking. The injected ``sequence_tracker``
        # is the PROTOTYPE — its policy is reused to lazily mint one tracker per
        # OBSERVED symbol (like ``_book_for``), so a gap in one symbol never masks
        # or cross-contaminates another in the 20-50 name universe. The prototype
        # itself is reused for its own symbol so a single-symbol stream is unchanged.
        self._tracker_proto = sequence_tracker
        self._trackers = {sequence_tracker._symbol: sequence_tracker}
        self._heartbeat = heartbeat
        # Alerts go to a SEPARATE stream/file (one EventWriter == one resolved path,
        # §E). Fall back to the events writer only if no alert writer was injected.
        self._alert_writer = alert_writer if alert_writer is not None else writer
        # M2 §F (OP-1): optional session-aware liveness predicate. When None, the two
        # heartbeat_timeout emit sites behave EXACTLY as M1 (byte-identical). When set,
        # a symbol not EXPECTED to be quoting (legitimately closed/unknown session)
        # suppresses the false heartbeat_timeout. The seq path is UNTOUCHED.
        self._liveness = liveness
        self._reconnect_epoch = 0
        # D3 (R2#3): the disconnect instant is captured ONCE per outage (the first
        # except), so down_ms is measured against a STABLE origin across every
        # reconnect attempt. Reset to None only on a successful receive.
        self._disconnect_origin_ms = None
        # E1 (R3#1): prolonged_disconnect is ONE-SHOT per outage. D3's stable origin
        # makes down_ms exceed alert_after_ms on EVERY attempt of one sustained
        # outage, which (pre-E1) re-fired the alert each attempt. This dedup flag
        # (mirroring _stale_alerted) emits the alert at most once per outage; it is
        # cleared on a successful receive next to the disconnect-origin reset.
        self._prolonged_alerted = False
        # D9 (R2#9): symbols already alerted stale this quiet-episode, so a quiet
        # symbol fires heartbeat_timeout ONCE (not per subsequent event); cleared
        # when the symbol is touched again.
        self._stale_alerted = set()
        self._books = {}                 # symbol -> EquityBookState
        self._final_book_hashes = {}     # symbol -> last derived book_hash
        self._events_written = 0
        self._alerts_emitted = 0
        self._reconnects = 0

    @property
    def reconnect_epoch(self) -> int:
        return self._reconnect_epoch

    @staticmethod
    def _backoff_delay_ms(backoff: BackoffPolicy, attempt: int) -> int:
        """delay = min(cap_ms, base_ms * factor**attempt). Unbounded attempts, capped delay."""
        return min(backoff.cap_ms, backoff.base_ms * (backoff.factor ** attempt))

    def _book_for(self, symbol: str, instrument_id: int) -> EquityBookState:
        book = self._books.get(symbol)
        if book is None:
            book = EquityBookState(symbol, instrument_id)
            self._books[symbol] = book
        return book

    def _tracker_for(self, symbol: str):
        """D6 (R2#6): the per-symbol SequenceTracker (lazily minted from the
        prototype's policy, like ``_book_for``). Each symbol gets an independent
        seq baseline so an interleaved multi-symbol stream is not cross-contaminated.
        """
        tracker = self._trackers.get(symbol)
        if tracker is None:
            tracker = type(self._tracker_proto)(symbol, policy=self._tracker_proto._policy)
            self._trackers[symbol] = tracker
        return tracker

    def _emit_alert(self, **kwargs) -> None:
        """Write one data_quality_alert ROW to the alerts stream (NEVER a silent exit)."""
        body = make_data_quality_alert(reconnect_epoch=self._reconnect_epoch, **kwargs)
        self._alert_writer.record(STREAM_DATA_QUALITY, body)
        self._alerts_emitted += 1

    def _maybe_book_hash(self, ev) -> Optional[str]:
        """Update per-symbol book state and return the derived book_hash for the events
        that mutate the ladder (depth + quote); None otherwise. A crossed book surfaces
        as a ``crossed_book`` alert (recorded, not silently normalized)."""
        prov = ev.provenance
        if isinstance(ev, DepthEvent):
            book = self._book_for(prov.symbol, prov.instrument_id)
            book.apply(ev)
        elif isinstance(ev, QuoteEvent):
            book = self._book_for(prov.symbol, prov.instrument_id)
            book.apply_quote(ev)
        else:
            return None
        snap = book.snapshot()
        bh = book_hash(snap)
        self._final_book_hashes[prov.symbol] = bh
        if snap.crossed:
            self._emit_alert(cause="crossed_book", symbol=prov.symbol)
        return bh

    def _detect_sequence(self, ev) -> None:
        """Sequence anomaly detection -> alert row (S4 input). Non-fatal.

        C1 taxonomy: a forward gap, a backward out-of-order, and a duplicate are
        DISTINCT fail-closed S4 signals — each surfaced, none swallowed.

        D6 (R2#6): route ``observe`` to the per-symbol tracker for the OBSERVED
        event's symbol; the alert always attributes ``report.symbol`` (the observed
        symbol). D2 (R2#2): a null/malformed vendor_seq surfaces a ``malformed_seq``
        alert and keeps the loop running (never a TypeError out of ``run()``).
        """
        report = self._tracker_for(ev.provenance.symbol).observe(ev)
        if report is None:
            return
        if report.kind == "gap":
            self._emit_alert(
                cause="sequence_gap", symbol=report.symbol,
                detail={
                    "expected_seq": report.expected_seq,
                    "got_seq": report.got_seq,
                    "gap_size": report.gap_size,
                },
            )
        elif report.kind in ("out_of_order", "duplicate"):
            self._emit_alert(
                cause=report.kind, symbol=report.symbol,
                detail={
                    "expected_seq": report.expected_seq,
                    "got_seq": report.got_seq,
                },
            )
        elif report.kind == "malformed_seq":
            self._emit_alert(
                cause="malformed_seq", symbol=report.symbol,
                detail={
                    "expected_seq": report.expected_seq,
                    "got_seq": report.got_seq,
                },
            )
        elif report.kind == "reset_to_zero":
            self._emit_alert(cause="reset_to_zero", symbol=report.symbol)

    def _process_event(self, ev) -> None:
        """Per-event pipeline: heartbeat touch, book hash, sequence detection, persist.

        G1 (R5#1): the per-symbol SequenceTracker baseline advances ONLY for frames
        that are actually PERSISTED. ``_detect_sequence(ev)`` therefore runs AFTER
        ``_maybe_book_hash(ev)`` SUCCEEDS and before ``write_event`` — a frame that
        raises ``BookStateError`` in the book-hash step (the F1 path) is contained by
        the caller (loud ``book_state_error`` alert + continue) WITHOUT ever advancing
        the tracker, so the next persisted seq's gap is detected against the true
        baseline (no tracker/journal divergence, no swallowed gap, S3 intact).
        """
        prov = ev.provenance
        now_ms = self._clock.now_ms()
        self._heartbeat.touch(prov.symbol, now_ms)
        # The touched symbol is fresh again -> eligible to alert on a future quiet.
        self._stale_alerted.discard(prov.symbol)
        self._check_connected_quiet(now_ms)
        bh = self._maybe_book_hash(ev)
        # Advance the seq tracker ONLY now that the frame is persist-bound (G1).
        self._detect_sequence(ev)
        self._writer.write_event(ev, derived_book_hash=bh)
        self._events_written += 1

    def _check_connected_quiet(self, now_ms: int) -> None:
        """D9 (R2#9): connected-but-quiet heartbeat. While the connection is healthy,
        a symbol that stops updating (quiet > timeout_ms) MUST surface a
        ``heartbeat_timeout`` alert — staleness is not only a reconnect-time signal.
        Each quiet symbol alerts once per quiet-episode (reset when it is touched).
        Deterministic offline: driven by the injected clock + HeartbeatMonitor.
        """
        for symbol in self._heartbeat.stale_symbols(now_ms):
            if symbol in self._stale_alerted:
                continue
            if self._liveness is not None and not self._liveness.expected_live(symbol, now_ms):
                # M2 §F: legitimately closed/unknown session -> suppress the false
                # heartbeat_timeout. Do NOT mark _stale_alerted, so the symbol can
                # still alert if it stays quiet once the session re-opens.
                continue
            self._stale_alerted.add(symbol)
            self._emit_alert(cause="heartbeat_timeout", symbol=symbol)

    async def _reconnect(self) -> None:
        """Always-on reconnect: bump epoch, sleep the capped backoff, then emit a
        ``prolonged_disconnect`` alert if the outage exceeded ``alert_after_ms``, and a
        ``heartbeat_timeout`` alert for any symbol gone stale across the gap.

        D3 (R2#3): ``down_ms`` is measured against ``self._disconnect_origin_ms`` — the
        instant captured ONCE at the first except of this outage — so a multi-attempt
        outage whose CUMULATIVE downtime exceeds ``alert_after_ms`` alerts even when no
        single attempt's backoff does. The origin is reset only on a successful receive.
        """
        self._reconnect_epoch += 1
        self._reconnects += 1
        # attempt index is (reconnects - 1): first reconnect uses base_ms * factor**0.
        delay = self._backoff_delay_ms(self._backoff, self._reconnects - 1)
        await self._sleep(delay)
        now_ms = self._clock.now_ms()
        down_ms = now_ms - self._disconnect_origin_ms
        # E1 (R3#1): emit prolonged_disconnect AT MOST ONCE per outage. Once this
        # outage has crossed alert_after_ms the stable origin keeps down_ms above
        # the threshold on every later attempt; the dedup flag suppresses the
        # re-fire. Cleared on a successful receive so the next outage alerts afresh.
        if down_ms > self._backoff.alert_after_ms and not self._prolonged_alerted:
            self._prolonged_alerted = True
            self._emit_alert(cause="prolonged_disconnect", down_ms=down_ms)
        # Heartbeat may have gone stale during the outage (S4 input). Dedup against
        # the connected-quiet path (D9) so one quiet-episode alerts once.
        for symbol in self._heartbeat.stale_symbols(now_ms):
            if symbol in self._stale_alerted:
                continue
            if self._liveness is not None and not self._liveness.expected_live(symbol, now_ms):
                # M2 §F: legitimately closed/unknown session -> suppress the false
                # heartbeat_timeout. Do NOT mark _stale_alerted, so the symbol can
                # still alert if it stays quiet once the session re-opens.
                continue
            self._stale_alerted.add(symbol)
            self._emit_alert(cause="heartbeat_timeout", symbol=symbol)

    async def run(self, max_events: Optional[int] = None) -> RecorderStats:
        """Main loop. Returns only on explicit stop (``max_events`` reached) or a clean
        end-of-stream; a disconnect reconnects (never exits silently)."""
        while True:
            produced = 0
            try:
                async for frame in self._transport.stream(self._symbols):
                    # A frame arrived over the wire -> the segment is alive, even if
                    # this one frame is malformed. Count it as produced so an
                    # all-malformed segment still ends cleanly (no infinite re-stream).
                    produced += 1
                    # D3 (R2#3): a successful receive ends the outage -> reset the
                    # stable disconnect origin so the NEXT outage is timed afresh.
                    # E1 (R3#1): clear the one-shot prolonged_disconnect flag here
                    # too, so the next outage can alert again.
                    self._disconnect_origin_ms = None
                    self._prolonged_alerted = False
                    # E3 (R3#3) + F1 (R4#1): one bad frame must NOT kill ingest for
                    # the universe. The per-frame recoverable family now spans the
                    # WHOLE per-frame body (parse + process/apply/persist), not just
                    # parse(). A real disconnect (TransportDisconnected from stream())
                    # and any unknown exception are NOT caught here and still
                    # propagate (fail-fast on programmer error — no bare except).
                    #
                    # The parse family is caught first (no parsed event yet -> the
                    # alert carries only the error reason). A BookStateError fires
                    # later in _process_event from a VALID-parse frame whose
                    # instrument_id mismatches the symbol's book; that alert carries
                    # symbol+instrument_id from the parsed event's provenance.
                    try:
                        ev = parse(
                            frame,
                            dataset=self._dataset,
                            schema=self._schema,
                            reconnect_epoch=self._reconnect_epoch,
                            ts_recv_utc=_ms_to_iso_utc(self._clock.now_ms()),
                        )
                    except _PARSE_FAIL_CLOSED as exc:
                        self._emit_alert(
                            cause="malformed_record",
                            detail={"error": type(exc).__name__, "reason": str(exc)},
                        )
                        continue
                    try:
                        self._process_event(ev)
                    except BookStateError as exc:
                        # F1 (R4#1): a valid-parse frame whose instrument_id mismatches
                        # the symbol's book -> loud book_state_error alert (carrying
                        # symbol+instrument_id from the parsed event's provenance) and
                        # CONTINUE; ingest for the rest of the universe survives.
                        prov = ev.provenance
                        self._emit_alert(
                            cause="book_state_error",
                            symbol=prov.symbol,
                            detail={
                                "error": type(exc).__name__,
                                "reason": str(exc),
                                "instrument_id": prov.instrument_id,
                            },
                        )
                        continue
                    if max_events is not None and self._events_written >= max_events:
                        return self._stats()
            except Exception as exc:
                if not _is_disconnect(exc):
                    raise
                # Recoverable disconnect: reconnect (unbounded), then re-stream.
                # D3: capture the disconnect instant ONCE per outage (only when not
                # already disconnected) so down_ms accrues across attempts.
                if self._disconnect_origin_ms is None:
                    self._disconnect_origin_ms = self._clock.now_ms()
                await self._reconnect()
                continue
            # Stream segment ended WITHOUT a disconnect. If it produced nothing, the
            # transport is exhausted -> clean stop (avoid an infinite re-stream).
            if produced == 0:
                return self._stats()

    def _stats(self) -> RecorderStats:
        return RecorderStats(
            events_written=self._events_written,
            alerts_emitted=self._alerts_emitted,
            reconnects=self._reconnects,
            final_book_hashes=dict(self._final_book_hashes),
        )
