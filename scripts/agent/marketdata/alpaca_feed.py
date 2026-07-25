"""Alpaca IEX live quote source — the $0 step-B route behind the feed seam.

``LiveQuoteFeed`` consumes any iterable of QuoteEvents/None heartbeats; this
module supplies one from Alpaca's real-time stock websocket (the free plan's
IEX feed), so live observe/paper sessions need NO paid data subscription.
Predeclared data-provenance change (2026-07-10, operator's call — cheapest first):

- dataset = ``ALPACA.IEX`` (NOT EQUS.MINI: IEX-only top-of-book, ~2% of
  consolidated volume — an NBBO approximation, honest in the provenance);
- schema = ``mbp-1`` (the registry's event-driven L1 tag: Alpaca's ``quotes``
  channel emits per top-of-book CHANGE, not time-conflated bars);
- instrument_id is SYNTHETIC (Alpaca has no numeric instrument id): a
  deterministic 31-bit digest of ``"ALPACA.IEX:" + symbol`` — stable across
  sessions/restarts, namespaced so it can never collide semantically with
  Databento ids inside one journal (datasets are never mixed in one session);
- realism-gap numbers produced on this feed are NOT comparable with the
  Databento-based M7 backtest caps (different quote source) — the paper-phase
  OPS evidence is unaffected, the formal edge-validation comparability note
  lives in the runbook.

VERIFIED 2026-07-10 (tier-2b one-session posture, mirroring
``databento_live_source``): the credentialed RTH run of
``agent.verify_alpaca_feed`` (15:20:03Z, 75 s, AAPL+MSFT) parsed 4978/4978
quotes with zero one-sided drops and an IDENTICAL record→replay round-trip
(report: ``reports/alpaca_feed/verified_feed.json``; the first run at
13:32Z failed only because replay's ``QUOTE_SCHEMAS`` lacked ``mbp-1`` —
fixed + TDD'ed same day). ``allow_unverified_live`` therefore defaults to
True and remains the emergency fail-closed lever: flip it back to False in
a reviewed commit if the vendor payload ever drifts. The SDK import lives
ONLY inside ``_build_real_client`` (FD-M5-5 wall — third sanctioned site,
consciously allow-listed in the AST guard).

SIZE-UNIT PIN (the verification session's human review): Alpaca stock quote
sizes are ROUND LOTS (×100 shares) — the docs say "bid size in round lots" /
"ask size in round lots", and the live samples (sizes 40/80/120/160: all
sub-100 and none a multiple of 100) can only be lot counts for displayed
protected quotes. ``bid_sz``/``ask_sz`` are recorded VERBATIM in vendor
units (recorder provenance discipline — no silent ×100); size-consuming
features must convert at read time, and the Databento realism-comparability
break above already covers the unit difference.
"""
import hashlib
from datetime import datetime, timezone
from typing import Optional, Sequence

from agent.bar_series import _parse_utc

DATASET = "ALPACA.IEX"
SCHEMA = "mbp-1"

_REPO_ROOT_PARENTS = 3  # scripts/agent/marketdata/alpaca_feed.py -> repo root


def alpaca_instrument_id(symbol: str) -> int:
    """Deterministic synthetic 31-bit instrument id for an Alpaca symbol."""
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("symbol must be a non-empty string")
    digest = hashlib.sha256(f"{DATASET}:{symbol}".encode("ascii")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _canonical_utc(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalize_alpaca_quote(*, symbol: str, ts_event, bid_price, bid_size,
                           ask_price, ask_size) -> Optional[dict]:
    """Alpaca quote fields → the recorder's parse-ready record dict.

    Returns None for one-sided/empty books (mirrors the databento adapter's
    per-record drop; the caller yields a heartbeat instead). Prices/sizes are
    stringified so ``recorder.event.parse`` owns ALL Decimal quantization and
    its NonFinitePrice/PrecisionLoss guards stay load-bearing (the SDK hands
    floats; str(float) round-trips shortest-repr, parse re-checks)."""
    try:
        bid = float(bid_price) if bid_price is not None else 0.0
        ask = float(ask_price) if ask_price is not None else 0.0
        bid_sz = float(bid_size) if bid_size is not None else 0.0
        ask_sz = float(ask_size) if ask_size is not None else 0.0
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or bid_sz <= 0 or ask_sz <= 0:
        return None                       # one-sided/empty book: drop
    if isinstance(ts_event, datetime):
        ts_event_utc = _canonical_utc(ts_event)
    elif isinstance(ts_event, str):
        try:
            ts_event_utc = _canonical_utc(_parse_utc(ts_event))
        except ValueError:
            return None
    else:
        return None
    return {
        "symbol": symbol,
        "instrument_id": alpaca_instrument_id(symbol),
        "ts_event": ts_event_utc,
        "bid_px": repr(bid),
        "bid_sz": repr(bid_sz),
        "ask_px": repr(ask),
        "ask_sz": repr(ask_sz),
    }


def _build_real_client(credentials_path=None):
    """The ONLY Alpaca-SDK import site in this module (FD-M5-5 wall).

    Returns a connected-but-not-running ``StockDataStream`` on the IEX feed,
    authenticated from ``.secrets/alpaca_paper.json`` (same pinned credential
    file as the broker — market data and paper trading share the key pair)."""
    from pathlib import Path

    from agent.secrets_runtime import load_alpaca_paper_credentials

    default = (Path(__file__).resolve().parents[_REPO_ROOT_PARENTS]
               / ".secrets" / "alpaca_paper.json")
    creds = load_alpaca_paper_credentials(credentials_path or default)

    from alpaca.data.enums import DataFeed
    from alpaca.data.live.stock import StockDataStream

    return StockDataStream(creds["key_id"], creds["secret_key"],
                           feed=DataFeed.IEX)


def alpaca_quote_source(*, symbols: Sequence[str],
                        credentials_path=None,
                        heartbeat_seconds: float = 1.0,
                        reconnect_epoch: int = 0,
                        allow_unverified_live: bool = True):
    """Live IEX quote source — VERIFIED 2026-07-10 (module docstring).

    The one-session verification asserted the real payload layout, the
    size-unit semantics (round lots) and the record→replay round-trip, so
    ``allow_unverified_live`` now defaults to True. Passing False re-arms
    the refusal — the emergency lever if the vendor payload ever drifts.
    """
    if not allow_unverified_live:
        raise NotImplementedError(
            "the Alpaca IEX live quote stream is UNVERIFIED: run "
            "agent.verify_alpaca_feed during market hours to pin the real "
            "payload layout + record->replay round-trip, then flip this seam "
            "in a reviewed commit (same one-session posture as the Databento "
            "tier-2b path).")

    # pragma: no cover start - credentialed live path, never reached offline
    import queue as _queue
    import threading

    from recorder.event import MalformedRecord, NonFinitePrice, PrecisionLoss
    from recorder.event import parse as recorder_parse

    stream = _build_real_client(credentials_path)
    records: "_queue.Queue" = _queue.Queue(maxsize=10_000)
    _SENTINEL = object()
    allowed = frozenset(symbols)

    async def _on_quote(quote) -> None:
        records.put(quote)

    stream.subscribe_quotes(_on_quote, *sorted(allowed))

    def _pump() -> None:
        try:
            stream.run()
        finally:
            records.put(_SENTINEL)

    thread = threading.Thread(target=_pump, name="alpaca-iex-pump",
                              daemon=True)
    thread.start()

    def _generator():
        try:
            while True:
                try:
                    quote = records.get(timeout=heartbeat_seconds)
                except _queue.Empty:
                    yield None   # heartbeat: ticks/bar closes keep firing
                    continue
                if quote is _SENTINEL:
                    return
                symbol = getattr(quote, "symbol", None)
                if symbol not in allowed:
                    yield None
                    continue
                record = normalize_alpaca_quote(
                    symbol=symbol,
                    ts_event=getattr(quote, "timestamp", None),
                    bid_price=getattr(quote, "bid_price", None),
                    bid_size=getattr(quote, "bid_size", None),
                    ask_price=getattr(quote, "ask_price", None),
                    ask_size=getattr(quote, "ask_size", None))
                if record is None:
                    yield None   # one-sided/malformed: dropped (fail-closed)
                    continue
                ts_recv_utc = _canonical_utc(datetime.now(timezone.utc))
                try:
                    yield recorder_parse(
                        record, dataset=DATASET, schema=SCHEMA,
                        reconnect_epoch=reconnect_epoch,
                        ts_recv_utc=ts_recv_utc)
                except (MalformedRecord, NonFinitePrice, PrecisionLoss):
                    yield None   # vendor anomaly dropped (fail-closed)
        finally:
            try:
                stream.stop()
            except Exception:  # noqa: BLE001 — best-effort shutdown
                pass

    return _generator()
    # pragma: no cover end
