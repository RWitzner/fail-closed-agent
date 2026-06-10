"""M5 §N — file-driven replay feed + replay clock (FD-M5-4).

``ReplayQuoteFeed`` reads an M1 recorder ``events.jsonl`` via
``recorder.persistence.replay_stream`` (hash-verified; the truncated-tail rule and
``JournalCorruption`` semantics are inherited verbatim) and drives the observe-mode
tick loop deterministically from RECORDED time:

- Quote rows (schema ∈ ``QUOTE_SCHEMAS`` = {"tbbo", "bbo-1s"}) map to
  ``agent.quote_quality.QuoteSnapshot`` — field map ``bid_px/ask_px/bid_sz/ask_sz``
  → ``bid/ask/bid_sz/ask_sz``, provenance carried VERBATIM, ``seen_at_ms`` = the
  ``ReplayClock`` offset of the row's ``ts_recv_utc``.
- Depth rows (schema ``"mbp-10"``) rebuild the ``DepthEvent`` via
  ``recorder.event_row.from_row``, fold it through ``EquityBookState.apply``
  (replace-on-apply), and shape an ``agent.execution_realism.DepthSnapshot`` whose
  ``book_hash`` is the RECORDED ``derived_book_hash`` — re-derived via
  ``recorder.book_hash.book_hash`` and cross-checked (the M1 dual-hash replay/
  reconcile posture: a mismatch is corruption → ``ValueError``; a ``null`` recorded
  hash falls back to the re-derived one). Exposed via a ``DepthView`` — used by
  TESTS ONLY in M5 (FD-M5-6: observe/paper leave the seam ``None``).
- ``symbols`` filter = file symbols ∩ the given set (FD-M5-4).

TIMESTAMP PARSE DISCIPLINE (frozen — EX-5/SAFETY-F2): ``ReplayClock`` and the
events.jsonl → ``QuoteSnapshot`` field map parse ALL timestamps via the
``agent.bar_series._parse_utc`` chokepoint — NO local ``strptime``/
``fromisoformat`` — because the repo provably mixes whole-second and ``.%f``
ISO-8601 surface forms (the shipped M3-02 bug class) and every downstream
staleness/latency/requote ms computation keys off these offsets.

Documented build resolutions (none contradicts a frozen pin; report-listed):

1. **Cadence injection:** §N freezes ``run(self, *, on_tick, on_bar_complete)``,
   so the cadence rides the CTOR — keyword-only ``refresh_cadence_ms`` (strict
   JSON-int > 0; bool rejected), default 1000 = the committed
   ``signal.refresh_cadence_ms``.
2. **Stream start = the first DELIVERED (post-symbol-filter) event's
   ``ts_recv_utc``;** ``now_ms() == 0`` before any delivery and at the first
   delivered event. Offsets are relative timing for staleness/latency math; the
   base is the delivered stream's own origin.
3. **First tick is due at exactly ``refresh_cadence_ms``** of recorded time (no
   tick at offset 0 — no cadence interval has elapsed yet).
4. **Delivery order per event:** advance clock → update view → fire every newly
   completed bar (``on_bar_complete``) → at most one ``on_tick`` (RC-5), so a
   tick always sees the bars completed at-or-before its ``now_ms``.
5. **RC-5 catch-up:** after a delivered event at offset ``t`` with ``t >=
   next_due``, ONE ``on_tick`` fires and ``next_due`` jumps to the next cadence
   boundary STRICTLY AFTER ``t`` — a recorded gap spanning several boundaries
   fires once, and ``now_ms`` strictly increases between consecutive ticks
   (equal-``ts_recv_utc`` batches can never double-fire).
6. **Bars are preloaded ONCE** (at construction) via
   ``agent.bar_series.resample_midbars`` into a ``MidBarSeriesReader`` — SAFE per
   §N because every feature/label read is as-of/watermark-gated (M3 FD-2). A bar
   "completes" in recorded time when BOTH its ``bucket_end_utc`` and its
   ``watermark_utc`` are <= the clock's recorded instant; completion order is
   ``(max(bucket_end, watermark), bucket_end, symbol, instrument_id)``. A bar
   whose completion never arrives inside the recorded stream NEVER fires
   (honest: the bucket never completed in recorded time).
7. **Per-(symbol, instrument_id) bar series** uses that identity's FIRST-SEEN
   quote ``(dataset, schema)``; rows of another quote schema for the same
   identity are deterministically skipped by the resampler (REPO-F10) — the
   ``MidBarSeriesReader`` forbids duplicate series keys.
8. **``data_pin``** uses the M3 §I frozen format
   ``f"{dataset}:{schema}:{interval}:{source_id}"`` with
   ``source_id = f"replay:{path.name}"``.
9. **``run()`` is single-shot** (second call → ``ValueError``): re-delivery would
   rewind the handed-out clock/views mid-flight.
10. **A ``ts_recv_utc`` regression** (recorded receipt order violated) →
    ``ValueError`` (fail-loud; the journal is receipt-ordered by construction).
11. **``DepthSnapshot`` is imported from ``agent.execution_realism``** — §3's
    import row for this module omits it (contract erratum, report-noted), but
    duplicating the frozen dataclass would violate the one-home discipline;
    ``execution_realism`` is itself in the §3 pure family, so the import keeps
    this module pure (no broker/preflight/kill/arming reachability).

Import discipline (§3): stdlib, ``agent.quote_quality``, ``agent.bar_series``,
``agent.execution_realism`` (erratum, above), ``recorder.persistence`` /
``event_row`` / ``book_state`` / ``book_hash``. This module replays and labels;
it never submits.
"""
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from agent.bar_series import (
    MidBar,
    MidBarSeriesReader,
    _decimal_field,
    _parse_utc,
    resample_midbars,
)
from agent.execution_realism import DepthSnapshot
from agent.quote_quality import QuoteSnapshot
from recorder.book_hash import book_hash
from recorder.book_state import EquityBookState
from recorder.event_row import from_row
from recorder.persistence import replay_stream

__all__ = [
    "BAR_INTERVAL",
    "DEPTH_SCHEMA",
    "QUOTE_SCHEMAS",
    "ReplayClock",
    "ReplayQuoteFeed",
]

QUOTE_SCHEMAS = frozenset({"tbbo", "bbo-1s"})   # §N frozen quote-row schema set
DEPTH_SCHEMA = "mbp-10"
BAR_INTERVAL = "1m"                              # FD-1: the only interval built
DEFAULT_REFRESH_CADENCE_MS = 1000                # committed signal.refresh_cadence_ms

_MS_PER_DAY = 86_400_000
_MS_PER_SECOND = 1_000


class ReplayClock:
    """now_ms = ms offset of the LAST DELIVERED event's ``ts_recv_utc`` from
    stream start (the first delivered event's receipt). FakeClock-compatible
    surface (``now_ms()``); only the owning ``ReplayQuoteFeed`` advances it.

    'Awaiting the latency budget' under this clock = delivering more recorded
    events until the budget has elapsed in RECORDED time — deterministic, no
    sleeps. ALL parsing goes through ``bar_series._parse_utc`` (EX-5).
    """

    def __init__(self) -> None:
        self._start: Optional[datetime] = None
        self._now_instant: Optional[datetime] = None
        self._now_ms = 0

    def now_ms(self) -> int:
        return self._now_ms

    # -- feed-private mechanics (same-module use only) --

    def _advance_to(self, ts_recv_utc: str) -> int:
        """Advance to a delivered event's receipt instant; return its ms offset."""
        instant = _parse_utc(ts_recv_utc)   # the ONE chokepoint (EX-5)
        if self._start is None:
            self._start = instant
        delta = instant - self._start
        offset = (delta.days * _MS_PER_DAY
                  + delta.seconds * _MS_PER_SECOND
                  + delta.microseconds // 1000)
        if offset < self._now_ms:
            raise ValueError(
                "replay stream violates receipt order: ts_recv_utc "
                f"{ts_recv_utc!r} is {self._now_ms - offset} ms before the "
                "last delivered event (recorded journals are receipt-ordered)")
        self._now_ms = offset
        self._now_instant = instant
        return offset


class _LatestQuoteView:
    """Latest-seen QuoteSnapshot per (symbol, instrument_id) — structurally
    satisfies the M3 ``QuoteView`` Protocol (calibration_probe.py:95-96)."""

    def __init__(self) -> None:
        self._latest: Dict[Tuple[str, int], QuoteSnapshot] = {}

    def latest(self, symbol: str, instrument_id: int) -> Optional[QuoteSnapshot]:
        return self._latest.get((symbol, instrument_id))

    def _put(self, snapshot: QuoteSnapshot) -> None:
        self._latest[(snapshot.symbol, snapshot.instrument_id)] = snapshot


class _LatestDepthView:
    """Latest-seen DepthSnapshot per (symbol, instrument_id) — structurally
    satisfies ``agent.execution_realism.DepthView``. TESTS ONLY in M5 (FD-M5-6)."""

    def __init__(self) -> None:
        self._latest: Dict[Tuple[str, int], DepthSnapshot] = {}

    def latest_book(self, symbol: str, instrument_id: int) -> Optional[DepthSnapshot]:
        return self._latest.get((symbol, instrument_id))

    def _put(self, snapshot: DepthSnapshot) -> None:
        self._latest[(snapshot.symbol, snapshot.instrument_id)] = snapshot


def _required(row: dict, key: str):
    if key not in row or row[key] is None:
        raise ValueError(f"recorder row missing required field {key!r}")
    return row[key]


class ReplayQuoteFeed:
    """File-driven feed over one M1 recorder ``events.jsonl`` (§N)."""

    def __init__(self, path, *, symbols: Optional[Sequence[str]] = None,
                 refresh_cadence_ms: int = DEFAULT_REFRESH_CADENCE_MS) -> None:
        if isinstance(refresh_cadence_ms, bool) or not isinstance(refresh_cadence_ms, int):
            raise ValueError(
                f"refresh_cadence_ms must be an int (got {refresh_cadence_ms!r})")
        if refresh_cadence_ms <= 0:
            raise ValueError(
                f"refresh_cadence_ms must be > 0 (got {refresh_cadence_ms})")
        self._path = Path(path)
        self._cadence_ms = refresh_cadence_ms

        rows = replay_stream(self._path)   # hash-verified; truncated tail dropped;
        #                                    complete corrupt line -> JournalCorruption
        deliverable = [row for row in rows
                       if row.get("schema") in QUOTE_SCHEMAS
                       or row.get("schema") == DEPTH_SCHEMA]
        file_symbols = {row["symbol"] for row in deliverable}
        if symbols is None:
            active = file_symbols
        else:
            active = file_symbols & {str(s) for s in symbols}   # FD-M5-4 intersection
        self._symbols: Tuple[str, ...] = tuple(sorted(active))
        self._events: List[dict] = [row for row in deliverable
                                    if row["symbol"] in active]

        self._clock = ReplayClock()
        self._quote_view = _LatestQuoteView()
        self._depth_view = _LatestDepthView()
        self._books: Dict[Tuple[str, int], EquityBookState] = {}
        self._ran = False

        # Bars: resampled ONCE here (resolution 6); reads stay as-of gated (FD-2).
        self._bar_reader, self._pending_bars = self._preload_bars()

    # -- accessors (§N) --

    def clock(self) -> ReplayClock:
        return self._clock

    def quote_view(self) -> _LatestQuoteView:
        return self._quote_view

    def depth_view(self) -> _LatestDepthView:
        return self._depth_view

    def bar_reader(self) -> MidBarSeriesReader:
        """The preloaded as-of/watermark-gated label series (orchestrator seam)."""
        return self._bar_reader

    def symbols(self) -> Tuple[str, ...]:
        """The active set: file symbols ∩ the ctor filter (FD-M5-4), sorted."""
        return self._symbols

    # -- the drive loop (§N) --

    def run(self, *, on_tick: Callable[..., None],
            on_bar_complete: Callable[[MidBar], None]) -> None:
        """Deliver every recorded event in receipt order.

        ``on_bar_complete(bar)`` fires in recorded-time completion order;
        ``on_tick(now_ms=...)`` fires per ``refresh_cadence_ms`` of RECORDED time
        under the RC-5 catch-up rule (at most ONE per delivered-event batch;
        ``now_ms`` strictly increases between consecutive ticks). Single-shot.
        """
        if self._ran:
            raise ValueError("ReplayQuoteFeed.run() is single-shot: re-delivery "
                             "would rewind the handed-out clock/views")
        self._ran = True

        pending = list(self._pending_bars)   # already completion-ordered
        next_idx = 0
        next_due = self._cadence_ms          # resolution 3: first tick at cadence
        last_tick_now: Optional[int] = None

        for row in self._events:
            offset = self._clock._advance_to(_required(row, "ts_recv_utc"))

            if row["schema"] in QUOTE_SCHEMAS:
                self._quote_view._put(self._quote_snapshot(row, offset))
            else:
                self._depth_view._put(self._depth_snapshot(row, offset))

            # Newly completed bars first (resolution 4) ...
            now_instant = self._clock._now_instant
            while (next_idx < len(pending)
                   and pending[next_idx][0] <= now_instant):
                on_bar_complete(pending[next_idx][3])
                next_idx += 1

            # ... then at most ONE tick for this batch (RC-5).
            if offset >= next_due and (last_tick_now is None or offset > last_tick_now):
                on_tick(now_ms=offset)
                last_tick_now = offset
                next_due = (offset // self._cadence_ms + 1) * self._cadence_ms

    # -- row shaping --

    def _quote_snapshot(self, row: dict, seen_at_ms: int) -> QuoteSnapshot:
        """§N field map: recorder flat row -> QuoteSnapshot, provenance verbatim."""
        return QuoteSnapshot(
            symbol=_required(row, "symbol"),
            instrument_id=_required(row, "instrument_id"),
            bid=_decimal_field(row, "bid_px"),
            ask=_decimal_field(row, "ask_px"),
            bid_sz=_decimal_field(row, "bid_sz"),
            ask_sz=_decimal_field(row, "ask_sz"),
            ts_event_utc=_required(row, "ts_event_utc"),
            ts_recv_utc=_required(row, "ts_recv_utc"),
            seen_at_ms=seen_at_ms,
            reconnect_epoch=_required(row, "reconnect_epoch"),
            vendor_seq=row.get("vendor_seq"),
            dataset=_required(row, "dataset"),
            schema=_required(row, "schema"),
        )

    def _depth_snapshot(self, row: dict, seen_at_ms: int) -> DepthSnapshot:
        """mbp-10 row -> EquityBookState.apply -> DepthSnapshot (recorded hash)."""
        event = from_row(row)
        key = (event.provenance.symbol, event.provenance.instrument_id)
        book = self._books.get(key)
        if book is None:
            book = EquityBookState(key[0], key[1])
            self._books[key] = book
        book.apply(event)
        snapshot = book.snapshot()
        derived = book_hash(snapshot)
        recorded = row.get("derived_book_hash")
        if recorded is None:
            recorded = derived
        elif recorded != derived:
            raise ValueError(
                f"depth row book_hash mismatch for {key}: recorded "
                f"{recorded!r} != re-derived {derived!r} (dual-hash reconcile)")
        return DepthSnapshot(
            symbol=key[0],
            instrument_id=key[1],
            bids=tuple((level.px, level.sz) for level in snapshot.bids),
            asks=tuple((level.px, level.sz) for level in snapshot.asks),
            book_hash=recorded,
            seen_at_ms=seen_at_ms,
            reconnect_epoch=event.provenance.reconnect_epoch,
            dataset=event.provenance.dataset,
            schema=DEPTH_SCHEMA,
        )

    # -- bar preload (resolutions 6-8) --

    def _preload_bars(self):
        groups: Dict[Tuple[str, int], List[dict]] = {}
        for row in self._events:
            if row["schema"] not in QUOTE_SCHEMAS:
                continue
            key = (row["symbol"], row["instrument_id"])
            groups.setdefault(key, []).append(row)

        all_bars: List[MidBar] = []
        all_missing: list = []
        for (symbol, instrument_id), group_rows in groups.items():
            first = group_rows[0]
            data_pin = (f"{first['dataset']}:{first['schema']}:{BAR_INTERVAL}"
                        f":replay:{self._path.name}")
            bars, missing = resample_midbars(
                group_rows, symbol=symbol, instrument_id=instrument_id,
                interval=BAR_INTERVAL, dataset=first["dataset"],
                schema=first["schema"], data_pin=data_pin)
            all_bars.extend(bars)
            all_missing.extend(missing)

        reader = MidBarSeriesReader(all_bars, all_missing)

        # Completion order: eligible at max(bucket_end, watermark) (FD-2 both-gated);
        # ties broken by (bucket_end, symbol, instrument_id) for determinism.
        pending = []
        for bar in all_bars:
            end = _parse_utc(bar.bucket_end_utc)
            watermark = _parse_utc(bar.watermark_utc)
            pending.append((max(end, watermark), end, (bar.symbol, bar.instrument_id), bar))
        pending.sort(key=lambda item: (item[0], item[1], item[2]))
        return reader, tuple(pending)
