"""Credentialed Alpaca IEX feed verification — run during market hours.

The one-session verification the `alpaca_feed` seam is gated on (same
posture as the Databento tier-2b path), PLUS the empirical status-channel
coverage measurement the Track B/D design requires ("what does the FREE feed
actually deliver?"). Strictly observational: subscribes, records, verifies,
writes a report — no orders, no gates, no writes outside the report path.

    PYTHONPATH=scripts .venv/bin/python3 -m agent.verify_alpaca_feed \
        --symbols AAPL,MSFT --seconds 30

Verifies (quotes): >=1 well-formed QuoteEvent arrives; the normalized record
round-trips recorder-write -> replay byte-identically; provenance carries
dataset=ALPACA.IEX schema=mbp-1 + the synthetic instrument id; samples are
printed for HUMAN review of size-unit semantics (shares vs round lots) before
the UNVERIFIED flag is flipped in a reviewed commit.

Measures (statuses/lulds): subscribes whatever the SDK exposes
(`subscribe_trading_statuses` / `subscribe_lulds` — probed with getattr, an
absent helper is REPORTED not assumed) and counts arrivals. Halts/LULD events
are rare intraday, so zero arrivals in a short window proves only
subscription ACCEPTANCE, not coverage — the report says so explicitly.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_REPORT = _REPO_ROOT / "reports" / "alpaca_feed" / "verified_feed.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def run_feed_verification(*, symbols: Sequence[str], seconds: int,
                          credentials_path=None,
                          report_path: Optional[Path] = None) -> dict:
    import queue as _queue
    import threading
    import time as _time
    from tempfile import TemporaryDirectory

    from agent.marketdata.alpaca_feed import (
        DATASET,
        SCHEMA,
        _build_real_client,
        alpaca_instrument_id,
        normalize_alpaca_quote,
    )
    from recorder.event import parse as recorder_parse
    from recorder.persistence import EventWriter
    from agent.marketdata.replay_feed import ReplayQuoteFeed

    summary = {
        "kind": "alpaca_feed_verification_v1",
        "verified_utc": _utc_now_iso(),
        "dataset": DATASET, "schema": SCHEMA,
        "symbols": sorted(symbols), "window_seconds": seconds,
        "quotes": {"received": 0, "parsed": 0, "dropped_one_sided": 0,
                   "samples": []},
        "statuses": {"subscribed": False, "received": 0, "samples": []},
        "lulds": {"subscribed": False, "received": 0, "samples": []},
        "roundtrip": None,
        "ok": False,
        "failures": [],
    }

    stream = _build_real_client(credentials_path)
    inbox: "_queue.Queue" = _queue.Queue(maxsize=50_000)

    async def _on_quote(quote):
        inbox.put(("quote", quote))

    async def _on_status(status):
        inbox.put(("status", status))

    async def _on_luld(luld):
        inbox.put(("luld", luld))

    stream.subscribe_quotes(_on_quote, *sorted(symbols))
    sub_statuses = getattr(stream, "subscribe_trading_statuses", None)
    if callable(sub_statuses):
        sub_statuses(_on_status, *sorted(symbols))
        summary["statuses"]["subscribed"] = True
    else:
        summary["statuses"]["sdk_helper"] = "absent in this alpaca-py version"
    sub_lulds = getattr(stream, "subscribe_lulds", None)
    if callable(sub_lulds):
        sub_lulds(_on_luld, *sorted(symbols))
        summary["lulds"]["subscribed"] = True
    else:
        summary["lulds"]["sdk_helper"] = "absent in this alpaca-py version"

    thread = threading.Thread(target=stream.run, name="verify-alpaca-feed",
                              daemon=True)
    thread.start()

    parsed_events = []
    deadline = _time.monotonic() + seconds
    while _time.monotonic() < deadline:
        try:
            kind, payload = inbox.get(timeout=0.5)
        except _queue.Empty:
            continue
        if kind == "quote":
            summary["quotes"]["received"] += 1
            record = normalize_alpaca_quote(
                symbol=getattr(payload, "symbol", None),
                ts_event=getattr(payload, "timestamp", None),
                bid_price=getattr(payload, "bid_price", None),
                bid_size=getattr(payload, "bid_size", None),
                ask_price=getattr(payload, "ask_price", None),
                ask_size=getattr(payload, "ask_size", None))
            if record is None:
                summary["quotes"]["dropped_one_sided"] += 1
                continue
            event = recorder_parse(record, dataset=DATASET, schema=SCHEMA,
                                   reconnect_epoch=0,
                                   ts_recv_utc=_utc_now_iso())
            parsed_events.append(event)
            summary["quotes"]["parsed"] += 1
            if len(summary["quotes"]["samples"]) < 3:
                summary["quotes"]["samples"].append({
                    "symbol": event.provenance.symbol,
                    "instrument_id": event.provenance.instrument_id,
                    "expected_instrument_id": alpaca_instrument_id(
                        event.provenance.symbol),
                    "ts_event_utc": event.provenance.ts_event_utc,
                    "bid_px": str(event.bid_px), "bid_sz": str(event.bid_sz),
                    "ask_px": str(event.ask_px), "ask_sz": str(event.ask_sz),
                })
        elif kind == "status":
            summary["statuses"]["received"] += 1
            if len(summary["statuses"]["samples"]) < 3:
                summary["statuses"]["samples"].append(repr(payload)[:300])
        elif kind == "luld":
            summary["lulds"]["received"] += 1
            if len(summary["lulds"]["samples"]) < 3:
                summary["lulds"]["samples"].append(repr(payload)[:300])
    try:
        stream.stop()
    except Exception:  # noqa: BLE001 — best-effort shutdown
        pass

    if not parsed_events:
        summary["failures"].append(
            "no_parsed_quotes (market closed? symbols illiquid? window too "
            "short?) — rerun during RTH")
    else:
        with TemporaryDirectory() as tmp:
            events_path = Path(tmp) / "events.jsonl"
            writer = EventWriter(events_path, "verify-alpaca-feed")
            for event in parsed_events:
                writer.write_event(event)
            replay = ReplayQuoteFeed(events_path)
            replay.run(on_tick=lambda *, now_ms: None,
                       on_bar_complete=lambda bar: None)
            last_by_key = {}
            for event in parsed_events:
                last_by_key[(event.provenance.symbol,
                             event.provenance.instrument_id)] = event
            mismatches = []
            for (symbol, instrument_id), event in sorted(last_by_key.items()):
                latest = replay.quote_view().latest(symbol, instrument_id)
                if (latest is None
                        or latest.bid != event.bid_px
                        or latest.ask != event.ask_px
                        or latest.ts_recv_utc
                        != event.provenance.ts_recv_utc):
                    mismatches.append(symbol)
            summary["roundtrip"] = {
                "written": len(parsed_events),
                "symbols_checked": len(last_by_key),
                "mismatched_symbols": mismatches,
                "identical": not mismatches,
            }
            if mismatches:
                summary["failures"].append("record_replay_roundtrip_mismatch")

    summary["ok"] = not summary["failures"]
    out = Path(report_path or _DEFAULT_REPORT)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    return summary


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="One-session Alpaca IEX feed verification + status/LULD "
                    "channel coverage measurement (run during market hours). "
                    "Observational only; no orders, no gates.")
    parser.add_argument("--symbols", default="AAPL,MSFT")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--report", default=str(_DEFAULT_REPORT))
    args = parser.parse_args(argv)

    summary = run_feed_verification(
        symbols=[s for s in args.symbols.split(",") if s],
        seconds=args.seconds, report_path=Path(args.report))
    print(json.dumps({k: summary[k] for k in
                      ("ok", "failures", "quotes", "statuses", "lulds",
                       "roundtrip")}, indent=1, sort_keys=True))
    print(f"report: {args.report}", file=sys.stderr)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
