"""M1 tier-2b seam — wall-clock live quote feed for the paper/observe session loop.

``LiveQuoteFeed`` implements the exact orchestrator feed surface ``ReplayQuoteFeed``
pins (``clock()/quote_view()/bar_reader()/run(on_tick,on_bar_complete)``) but driven
by WALL time and a live record source:

- ``record_source`` is any iterable yielding ``recorder.event.QuoteEvent`` items or
  ``None`` heartbeats (a heartbeat lets ticks/bar-closes fire through quiet markets).
  Offline tests inject a list; the credentialed source is ``databento_live_source``
  below — LAZY ``databento`` import, fail-closed ``NotImplementedError`` until the
  paid live subscription exists and the layout is verified (tier-2b-UNVERIFIED, the
  same posture the historical ``bbo-1m`` pull had before its credentialed
  verification).
- Every delivered quote is RECORDED first (optional ``EventWriter`` — the same flat
  row ``ReplayQuoteFeed`` replays), so a live session is deterministically
  re-playable afterward: record → replay is the M1 provenance discipline, not an
  afterthought.
- Bars: rows accumulate per ``(symbol, instrument_id)`` and are resampled through
  the SAME ``resample_midbars`` the replay feed uses. A bucket's bar fires only
  once wall time passes ``bucket_end + bar_close_grace_ms`` AND the bar's own
  FD-2 watermark — an in-progress minute can never leak (no lookahead); a late row
  for an already-fired bucket is dropped and counted (``late_row_dropped``), never
  silently folded.
- Fail-closed data quality: a per-key ``ts_recv_utc`` receipt-order regression is
  dropped + counted (``receipt_order_regression``), an off-universe symbol is
  dropped + counted (``off_universe_symbol``) — live streams degrade per record,
  they do not crash the session loop.
- ``run()`` is single-shot and ends at ``stop_at_utc`` (wall), on source
  exhaustion, or at ``max_runtime_ms`` (a hard backstop so an unattended session
  can never run away). A source that EXHAUSTS before ``stop_at_utc`` is a
  mid-session disconnect (the credentialed generator otherwise heartbeats
  through quiet markets until stop) and is counted ``source_exhausted_early``
  so the session runner can refuse to report a truncated day as clean.
- Bounded state: once a bucket fires (bar or missing), its rows are PRUNED —
  behavior-identical because ``resample_midbars`` buckets are independent and a
  late row for a fired bucket is already dropped at delivery. Without pruning a
  bbo-1s day re-resamples O(day²) rows and the loop falls behind by mid-day.

Import discipline: stdlib + ``agent.bar_series`` + ``agent.quote_quality`` +
``recorder.event``/``event_row`` types via duck use. No broker, no preflight, no
kill, no arming — this module delivers and labels; it never submits.
"""
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from agent.bar_series import (
    MidBar,
    MidBarSeriesReader,
    _parse_utc,
    resample_midbars,
)
from agent.quote_quality import QuoteSnapshot

__all__ = [
    "LIVE_QUOTE_SCHEMAS",
    "LiveClock",
    "LiveQuoteFeed",
    "ReconnectingQuoteSource",
    "databento_live_source",
]

LIVE_QUOTE_SCHEMAS = frozenset({"tbbo", "bbo-1s"})  # the replay QUOTE_SCHEMAS set
DEFAULT_REFRESH_CADENCE_MS = 1000
DEFAULT_BAR_CLOSE_GRACE_MS = 1500
BAR_INTERVAL = "1m"
# Vendor ts_recv may run slightly ahead of our wall clock (skew); beyond this
# bound a record is corrupt/unusable — buffering it would push the bucket's
# FD-2 watermark far into the future and wedge the key's bar firing.
MAX_RECV_AHEAD_MS = 30_000


class LiveClock:
    """Monotonic wall-clock ms since construction (never wall-date arithmetic —
    latency/staleness math must be immune to NTP steps). ``monotonic_fn`` is
    injectable for deterministic tests."""

    def __init__(self, *, monotonic_fn: Optional[Callable[[], float]] = None) -> None:
        self._monotonic_fn = monotonic_fn or _time.monotonic
        self._start = self._monotonic_fn()

    def now_ms(self) -> int:
        return int((self._monotonic_fn() - self._start) * 1000)


class _LatestQuoteView:
    """Latest-seen QuoteSnapshot per (symbol, instrument_id) — the same structural
    QuoteView the replay feed exposes."""

    def __init__(self) -> None:
        self._latest: Dict[Tuple[str, int], QuoteSnapshot] = {}

    def latest(self, symbol: str, instrument_id: int) -> Optional[QuoteSnapshot]:
        return self._latest.get((symbol, instrument_id))

    def _put(self, snapshot: QuoteSnapshot) -> None:
        self._latest[(snapshot.symbol, snapshot.instrument_id)] = snapshot


class _LiveBarReader:
    """Mutable delegate exposing the MidBarSeriesReader read API over the bars
    fired so far. The inner reader is rebuilt on each bar-close batch (hundreds of
    bars/day — trivially cheap) so consumers hold ONE stable object reference."""

    def __init__(self) -> None:
        self._inner = MidBarSeriesReader((), ())

    def _rebuild(self, bars, missing) -> None:
        self._inner = MidBarSeriesReader(bars, missing)

    def get(self, symbol: str, instrument_id: int, bucket_end_utc: str,
            *, as_of_utc: Optional[str] = None):
        return self._inner.get(symbol, instrument_id, bucket_end_utc,
                               as_of_utc=as_of_utc)

    def latest_eligible(self, symbol: str, instrument_id: int, *, as_of_utc: str):
        return self._inner.latest_eligible(symbol, instrument_id,
                                           as_of_utc=as_of_utc)

    def eligible_history(self, symbol: str, instrument_id: int, *,
                         as_of_utc: str, max_bars: int):
        return self._inner.eligible_history(symbol, instrument_id,
                                            as_of_utc=as_of_utc,
                                            max_bars=max_bars)


def _utc_now_default() -> datetime:
    return datetime.now(timezone.utc)


class LiveQuoteFeed:
    """Wall-clock live feed over an injected record source (see module docstring)."""

    def __init__(self, *, record_source: Iterable,
                 symbols: Sequence[str],
                 dataset: str,
                 schema: str,
                 stop_at_utc: str,
                 clock: Optional[LiveClock] = None,
                 utc_now_fn: Optional[Callable[[], datetime]] = None,
                 refresh_cadence_ms: int = DEFAULT_REFRESH_CADENCE_MS,
                 bar_close_grace_ms: int = DEFAULT_BAR_CLOSE_GRACE_MS,
                 event_writer=None,
                 max_runtime_ms: Optional[int] = None) -> None:
        if schema not in LIVE_QUOTE_SCHEMAS:
            raise ValueError(
                f"live feed schema must be one of {sorted(LIVE_QUOTE_SCHEMAS)}, "
                f"got {schema!r}")
        for name, value in (("refresh_cadence_ms", refresh_cadence_ms),
                            ("bar_close_grace_ms", bar_close_grace_ms)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive int, got {value!r}")
        if max_runtime_ms is not None and (
                isinstance(max_runtime_ms, bool)
                or not isinstance(max_runtime_ms, int) or max_runtime_ms <= 0):
            raise ValueError(
                f"max_runtime_ms must be a positive int or None, got {max_runtime_ms!r}")
        self._source = record_source
        self._symbols: Tuple[str, ...] = tuple(sorted({str(s) for s in symbols}))
        if not self._symbols:
            raise ValueError("live feed requires a non-empty symbols set")
        self._dataset = dataset
        self._schema = schema
        self._stop_at = _parse_utc(stop_at_utc)
        self._clock = clock or LiveClock()
        self._utc_now_fn = utc_now_fn or _utc_now_default
        self._cadence_ms = refresh_cadence_ms
        self._grace = timedelta(milliseconds=bar_close_grace_ms)
        self._writer = event_writer
        self._max_runtime_ms = max_runtime_ms

        self._quote_view = _LatestQuoteView()
        self._bar_reader = _LiveBarReader()
        self._rows: Dict[Tuple[str, int], List[dict]] = {}
        self._last_recv: Dict[Tuple[str, int], datetime] = {}
        self._fired_bars: Dict[Tuple[str, int], List[MidBar]] = {}
        self._fired_missing: Dict[Tuple[str, int], list] = {}
        self._fired_ends: Dict[Tuple[str, int], set] = {}
        # Running max of fired bucket ends per key (firing is strict in-order,
        # so this is the last fired end): the per-record late-row check and the
        # prune horizon read this instead of max()-ing the set per record.
        self._fired_max: Dict[Tuple[str, int], datetime] = {}
        self._quality_counts: Dict[str, int] = {}
        self._ran = False

    # -- accessors (the frozen feed surface) --

    def clock(self) -> LiveClock:
        return self._clock

    def quote_view(self) -> _LatestQuoteView:
        return self._quote_view

    def bar_reader(self) -> _LiveBarReader:
        return self._bar_reader

    def symbols(self) -> Tuple[str, ...]:
        return self._symbols

    def data_quality_counts(self) -> Dict[str, int]:
        """Per-reason dropped-record counts (fail-closed per record, journaled by
        the session runner — a live stream degrades loudly, never silently).
        A reconnecting source's stats are folded in so the daily report shows
        disconnect/reconnect churn alongside the drop counts."""
        counts = dict(self._quality_counts)
        stats = getattr(self._source, "stats", None)
        if isinstance(stats, dict):
            for key, value in stats.items():
                if value:
                    counts[f"source_{key}"] = (
                        counts.get(f"source_{key}", 0) + int(value))
        return counts

    # -- drive loop --

    def run(self, *, on_tick: Callable[..., None],
            on_bar_complete: Callable[[MidBar], None]) -> None:
        if self._ran:
            raise ValueError("LiveQuoteFeed.run() is single-shot")
        self._ran = True
        next_due = self._cadence_ms
        started_ms = self._clock.now_ms()

        for item in self._source:
            wall_utc = self._utc_now_fn()
            if wall_utc >= self._stop_at:
                break
            now_ms = self._clock.now_ms()
            if (self._max_runtime_ms is not None
                    and now_ms - started_ms > self._max_runtime_ms):
                self._count("max_runtime_stop")
                break

            if item is not None:
                self._deliver(item, now_ms, wall_utc)

            self._complete_due_bars(wall_utc, on_bar_complete)

            if now_ms >= next_due:
                on_tick(now_ms=now_ms)
                next_due = (now_ms // self._cadence_ms + 1) * self._cadence_ms
        else:
            # the loop ended by SOURCE EXHAUSTION (no break): before stop_at
            # that is a disconnect, not a clean session end — count it so the
            # runner/report can see the truncation.
            if self._utc_now_fn() < self._stop_at:
                self._count("source_exhausted_early")

        # final bar-close pass so a bucket completed by the clock (but not yet by
        # a subsequent event) still fires before shutdown/EOD accounting.
        self._complete_due_bars(self._utc_now_fn(), on_bar_complete)

    # -- delivery --

    def _count(self, reason: str) -> None:
        self._quality_counts[reason] = self._quality_counts.get(reason, 0) + 1

    def _deliver(self, event, now_ms: int, wall_utc: datetime) -> None:
        prov = event.provenance
        if prov.symbol not in self._symbols:
            self._count("off_universe_symbol")
            return
        if prov.schema != self._schema or prov.dataset != self._dataset:
            self._count("off_schema_record")
            return
        key = (prov.symbol, prov.instrument_id)
        recv = _parse_utc(prov.ts_recv_utc)
        if recv < _parse_utc(prov.ts_event_utc):
            # impossible receive-before-event row — the same contract the
            # historical manifest validator enforces, mirrored per record.
            self._count("receive_before_event_dropped")
            return
        if recv > wall_utc + timedelta(milliseconds=MAX_RECV_AHEAD_MS):
            # corrupt/absurd vendor receipt: buffering it would wedge the
            # key's bar firing behind an unreachable watermark (fail-closed
            # per record, never per session).
            self._count("future_receipt_dropped")
            return
        last = self._last_recv.get(key)
        if last is not None and recv < last:
            self._count("receipt_order_regression")
            return
        fired_max = self._fired_max.get(key)
        if fired_max is not None and _parse_utc(prov.ts_event_utc) < fired_max:
            # a row for a bucket that already fired: folding it would rewrite a
            # completed bar (lookahead in reverse) — drop + count.
            self._count("late_row_dropped")
            return
        if self._writer is not None:
            self._writer.write_event(event)   # record BEFORE acting (provenance)
        self._last_recv[key] = recv
        row = {
            "dataset": prov.dataset,
            "schema": prov.schema,
            "symbol": prov.symbol,
            "instrument_id": prov.instrument_id,
            "vendor_seq": prov.vendor_seq if prov.vendor_seq is not None else 0,
            "ts_event_utc": prov.ts_event_utc,
            "ts_recv_utc": prov.ts_recv_utc,
            "bid_px": event.bid_px,
            "bid_sz": event.bid_sz,
            "ask_px": event.ask_px,
            "ask_sz": event.ask_sz,
            "reconnect_epoch": prov.reconnect_epoch,
        }
        self._rows.setdefault(key, []).append(row)
        self._quote_view._put(QuoteSnapshot(
            symbol=prov.symbol,
            instrument_id=prov.instrument_id,
            bid=event.bid_px,
            ask=event.ask_px,
            bid_sz=event.bid_sz,
            ask_sz=event.ask_sz,
            ts_event_utc=prov.ts_event_utc,
            ts_recv_utc=prov.ts_recv_utc,
            seen_at_ms=now_ms,
            reconnect_epoch=prov.reconnect_epoch,
            vendor_seq=prov.vendor_seq,
            dataset=prov.dataset,
            schema=prov.schema,
        ))

    # -- bar completion (no-lookahead) --

    def _complete_due_bars(self, wall_utc: datetime,
                           on_bar_complete: Callable[[MidBar], None]) -> None:
        changed = False
        for key, rows in self._rows.items():
            if not rows:
                continue
            symbol, instrument_id = key
            data_pin = f"{self._dataset}:{self._schema}:{BAR_INTERVAL}:live"
            bars, missing = resample_midbars(
                rows, symbol=symbol, instrument_id=instrument_id,
                interval=BAR_INTERVAL, dataset=self._dataset,
                schema=self._schema, data_pin=data_pin)
            fired_ends = self._fired_ends.setdefault(key, set())
            fired_bars = self._fired_bars.setdefault(key, [])
            fired_missing = self._fired_missing.setdefault(key, [])
            # STRICT IN-ORDER firing per key: walk (bars ∪ missing) ascending
            # by bucket_end and STOP at the first not-yet-settled record. A
            # later bucket must never fire before an earlier one (out-of-order
            # on_bar_complete would corrupt forward-time feature state, and an
            # out-of-order per-key list fails the reader rebuild) — vendor
            # ts_recv skew can otherwise make an older bucket's watermark
            # settle AFTER a newer bucket's end+grace.
            records = sorted(
                [( _parse_utc(bar.bucket_end_utc),
                   max(_parse_utc(bar.bucket_end_utc) + self._grace,
                       _parse_utc(bar.watermark_utc)),
                   bar, True) for bar in bars]
                + [( _parse_utc(miss.bucket_end_utc),
                     _parse_utc(miss.bucket_end_utc) + self._grace,
                     miss, False) for miss in missing],
                key=lambda item: item[0])
            fired_new = None
            for end, settle_at, record, is_bar in records:
                if end in fired_ends:
                    continue
                if wall_utc < settle_at:
                    break   # not settled: everything after must wait too
                fired_ends.add(end)
                fired_new = end
                changed = True
                if is_bar:
                    fired_bars.append(record)
                    on_bar_complete(record)
                else:
                    fired_missing.append(record)
            if fired_new is not None:
                # strict in-order firing ⇒ the last fired end IS the max.
                self._fired_max[key] = fired_new
                # PRUNE rows of fired buckets: a row with ts_event < the fired
                # horizon belongs to a fired bucket (minute-aligned boundary)
                # and can never contribute again — the delivery-side late-row
                # check refuses exactly the same rows. Keeps the per-item
                # resample O(pending buckets) and the memory bounded.
                self._rows[key] = [
                    row for row in rows
                    if _parse_utc(row["ts_event_utc"]) >= fired_new]
        if changed:
            all_bars: List[MidBar] = []
            all_missing: list = []
            for key in sorted(self._fired_bars):
                all_bars.extend(self._fired_bars[key])
            for key in sorted(self._fired_missing):
                all_missing.extend(self._fired_missing[key])
            self._bar_reader._rebuild(all_bars, all_missing)


class ReconnectingQuoteSource:
    """Bounded live-source reconnection (P0-3b) — an ITERABLE over a source
    FACTORY: ``factory(reconnect_epoch=n) -> iterable``.

    When the inner source exhausts or raises before ``stop_at_utc``, the
    wrapper emits ``None`` heartbeats through a bounded exponential backoff
    (so the feed keeps ticking and firing bar closes), then builds the next
    epoch's source — every quote after a reconnect carries the bumped
    ``reconnect_epoch`` natively because the factory receives it. After
    ``max_reconnects`` rebuilds, or once the stop instant has passed, the
    iterator ENDS: the feed's existing ``source_exhausted_early`` accounting
    turns that into the truncated-day exit-1 path. Fail-closed: inner source
    exceptions are a DISCONNECT, never a session crash. A healthy delivery
    resets the backoff ladder. ``sleep_fn`` is injectable for deterministic
    offline tests; ``stats`` is folded into the feed's data-quality counts."""

    def __init__(self, factory, *, utc_now_fn, stop_at_utc: str,
                 max_reconnects: int = 5,
                 backoff_initial_ms: int = 1_000,
                 backoff_multiplier: float = 2.0,
                 backoff_max_ms: int = 30_000,
                 heartbeat_slice_ms: int = 250,
                 sleep_fn: Optional[Callable[[float], None]] = None) -> None:
        self._factory = factory
        self._utc_now_fn = utc_now_fn
        self._stop_at = _parse_utc(stop_at_utc)
        self._max_reconnects = int(max_reconnects)
        self._backoff_initial_ms = int(backoff_initial_ms)
        self._backoff_multiplier = float(backoff_multiplier)
        self._backoff_max_ms = int(backoff_max_ms)
        self._heartbeat_slice_ms = max(1, int(heartbeat_slice_ms))
        self._sleep_fn = sleep_fn or _time.sleep
        self.stats: Dict[str, int] = {
            "reconnects": 0, "reconnect_gave_up": 0, "connect_failures": 0,
        }

    def __iter__(self):
        epoch = 0
        backoff_ms = self._backoff_initial_ms
        while True:
            try:
                source = self._factory(reconnect_epoch=epoch)
            except Exception:  # noqa: BLE001 — connect failure = disconnect
                self.stats["connect_failures"] += 1
                source = None
            if source is not None:
                try:
                    for item in source:
                        if item is not None:
                            # healthy stream: reset the backoff ladder
                            backoff_ms = self._backoff_initial_ms
                        yield item
                except Exception:  # noqa: BLE001 — disconnect, not a crash
                    pass
            if self._utc_now_fn() >= self._stop_at:
                return  # session over — a clean end, never reconnect past stop
            if self.stats["reconnects"] >= self._max_reconnects:
                self.stats["reconnect_gave_up"] = 1
                return
            self.stats["reconnects"] += 1
            epoch += 1
            remaining = min(backoff_ms, self._backoff_max_ms)
            while remaining > 0:
                if self._utc_now_fn() >= self._stop_at:
                    return
                slice_ms = min(self._heartbeat_slice_ms, remaining)
                self._sleep_fn(slice_ms / 1000.0)
                remaining -= slice_ms
                yield None  # heartbeat: bar closes keep firing while we wait
            backoff_ms = min(int(backoff_ms * self._backoff_multiplier),
                             self._backoff_max_ms)


def databento_live_source(*, dataset: str, schema: str,
                          symbols: Sequence[str],
                          credentials_path=None,
                          heartbeat_seconds: float = 1.0,
                          reconnect_epoch: int = 0,
                          allow_unverified_live: bool = False):
    """The credentialed realtime source (M1 tier-2b) — fail-closed until verified.

    The live realtime subscription is NOT provisioned (the entitled Databento key
    is historical-only), so this seam has never been verified against the real
    live gateway and REFUSES to run by default. When the subscription exists, the
    verification session runs it with ``allow_unverified_live=True``, asserts the
    record layout (``bbo-1s`` shares the flat ``*_00`` ``BBOMsg`` layout the
    credentialed historical pull verified on 2026-06-26; ``tbbo`` is UNVERIFIED),
    and the flag flips in a reviewed commit — the same one-session posture the
    historical ``bbo-1m`` pull followed.
    """
    if not allow_unverified_live:
        raise NotImplementedError(
            "tier-2b live realtime streaming is UNVERIFIED: the live Databento "
            "subscription is not provisioned (the entitled key is historical-"
            "only). Provision the subscription, then run the verification "
            "session with allow_unverified_live=True (see the paper runbook).")

    # pragma: no cover start - credentialed live path, never reached offline
    import json as _json
    import queue as _queue
    import threading
    from pathlib import Path as _Path

    if schema != "bbo-1s":
        raise NotImplementedError(
            f"live schema {schema!r} is unverified; bbo-1s is the pinned live "
            "NBBO schema (shares the BBOMsg layout the 2026-06-26 credentialed "
            "historical pull verified)")

    # The same decode chain the VERIFIED historical pull uses: DBN BBOMsg ->
    # _dbn_bbo1m_record_to_event_dict (fail-closed per record; ts_recv_utc is
    # popped and handed to recorder parse as a kwarg) -> recorder QuoteEvent.
    from agent.historical_backfill import _dbn_bbo1m_record_to_event_dict
    from recorder.event import MalformedRecord, NonFinitePrice, PrecisionLoss
    from recorder.event import parse as recorder_parse

    secrets = (_Path(credentials_path) if credentials_path
               else _Path(__file__).resolve().parents[3]
               / ".secrets" / "databento.json")
    api_key = _json.loads(secrets.read_text(encoding="utf-8"))["api_key"]
    import databento  # LAZY — the only live-streaming import site

    client = databento.Live(key=api_key)
    client.subscribe(dataset=dataset, schema=schema, stype_in="raw_symbol",
                     symbols=list(symbols))
    records: "_queue.Queue" = _queue.Queue(maxsize=10_000)
    _SENTINEL = object()
    id_to_symbol: Dict[int, str] = {}
    allowed_symbols = frozenset(symbols)

    def _pump() -> None:
        try:
            for record in client:
                records.put(record)
        finally:
            records.put(_SENTINEL)

    thread = threading.Thread(target=_pump, name="databento-live-pump",
                              daemon=True)
    thread.start()
    client.start()

    def _generator():
        try:
            while True:
                try:
                    record = records.get(timeout=heartbeat_seconds)
                except _queue.Empty:
                    yield None   # heartbeat: let ticks/bar closes fire
                    continue
                if record is _SENTINEL:
                    return
                kind = type(record).__name__
                if kind == "SymbolMappingMsg":
                    id_to_symbol[record.instrument_id] = getattr(
                        record, "stype_out_symbol", None)
                    continue
                if not kind.startswith("BBO") and "Bbo" not in kind:
                    yield None   # system/heartbeat/error records advance the clock
                    continue
                symbol = id_to_symbol.get(getattr(record, "instrument_id", None))
                if symbol is None or symbol not in allowed_symbols:
                    yield None
                    continue
                try:
                    record_dict = _dbn_bbo1m_record_to_event_dict(
                        record, symbol=symbol)
                except ValueError:
                    yield None   # one-sided/undefined record dropped (fail-closed)
                    continue
                ts_recv_utc = record_dict.pop("ts_recv_utc")
                try:
                    yield recorder_parse(
                        record_dict, dataset=dataset, schema=schema,
                        reconnect_epoch=reconnect_epoch,
                        ts_recv_utc=ts_recv_utc)
                except (MalformedRecord, NonFinitePrice, PrecisionLoss):
                    yield None   # vendor decode anomaly dropped (fail-closed)
        finally:
            client.stop()

    return _generator()
    # pragma: no cover end
