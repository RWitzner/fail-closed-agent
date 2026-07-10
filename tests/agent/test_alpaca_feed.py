"""Alpaca IEX quote adapter — the $0 step-B route (offline contract).

Pins: deterministic synthetic instrument ids; normalization → recorder.parse
round-trip with the pinned ALPACA.IEX/mbp-1 provenance; one-sided/malformed
quotes drop to None (fail-closed); the source default is VERIFIED-True
(2026-07-10) while explicit allow_unverified_live=False re-arms the refusal
with NO SDK import on that path; LiveQuoteFeed accepts the mbp-1 schema;
the paper_session --live-source alpaca-iex composition pins dataset/schema."""
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agent.marketdata.alpaca_feed import (
    DATASET,
    SCHEMA,
    alpaca_instrument_id,
    alpaca_quote_source,
    normalize_alpaca_quote,
)
from agent.marketdata.live_feed import LiveQuoteFeed
from recorder.event import QuoteEvent, parse as recorder_parse

_TS = datetime(2026, 7, 10, 14, 0, 0, 123456, tzinfo=timezone.utc)


class TestInstrumentId(unittest.TestCase):
    def test_deterministic_31_bit_and_distinct(self):
        a1 = alpaca_instrument_id("AAPL")
        a2 = alpaca_instrument_id("AAPL")
        m = alpaca_instrument_id("MSFT")
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, m)
        for value in (a1, m):
            self.assertGreaterEqual(value, 0)
            self.assertLess(value, 2 ** 31)

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            alpaca_instrument_id("")


class TestNormalization(unittest.TestCase):
    def test_good_quote_parses_to_quote_event(self):
        record = normalize_alpaca_quote(
            symbol="AAPL", ts_event=_TS, bid_price=213.45, bid_size=3,
            ask_price=213.47, ask_size=5)
        event = recorder_parse(record, dataset=DATASET, schema=SCHEMA,
                               reconnect_epoch=0,
                               ts_recv_utc="2026-07-10T14:00:00.200000Z")
        self.assertIsInstance(event, QuoteEvent)
        self.assertEqual(event.provenance.dataset, "ALPACA.IEX")
        self.assertEqual(event.provenance.schema, "mbp-1")
        self.assertEqual(event.provenance.symbol, "AAPL")
        self.assertEqual(event.provenance.instrument_id,
                         alpaca_instrument_id("AAPL"))
        self.assertEqual(event.provenance.ts_event_utc,
                         "2026-07-10T14:00:00.123456Z")
        self.assertEqual(event.bid_px, Decimal("213.45"))
        self.assertEqual(event.ask_px, Decimal("213.47"))
        self.assertEqual(event.bid_sz, Decimal("3"))

    def test_one_sided_or_empty_book_drops(self):
        base = dict(symbol="AAPL", ts_event=_TS, bid_price=213.45,
                    bid_size=3, ask_price=213.47, ask_size=5)
        for missing in ("bid_price", "ask_price", "bid_size", "ask_size"):
            with self.subTest(missing=missing):
                broken = dict(base)
                broken[missing] = None
                self.assertIsNone(normalize_alpaca_quote(**broken))
                broken[missing] = 0
                self.assertIsNone(normalize_alpaca_quote(**broken))

    def test_bad_timestamp_drops(self):
        self.assertIsNone(normalize_alpaca_quote(
            symbol="AAPL", ts_event="not-a-time", bid_price=1.0,
            bid_size=1, ask_price=1.1, ask_size=1))
        self.assertIsNone(normalize_alpaca_quote(
            symbol="AAPL", ts_event=None, bid_price=1.0,
            bid_size=1, ask_price=1.1, ask_size=1))

    def test_iso_string_timestamp_accepted(self):
        record = normalize_alpaca_quote(
            symbol="MSFT", ts_event="2026-07-10T14:00:00.000001Z",
            bid_price=500.10, bid_size=2, ask_price=500.12, ask_size=2)
        self.assertEqual(record["ts_event"], "2026-07-10T14:00:00.000001Z")


class TestSourceGate(unittest.TestCase):
    def test_default_is_verified_true(self):
        # Flipped in the reviewed 2026-07-10 commit after the credentialed
        # verification session (4978/4978 quotes, round-trip identical).
        import inspect
        default = inspect.signature(alpaca_quote_source).parameters[
            "allow_unverified_live"].default
        self.assertIs(default, True)

    def test_explicit_false_rearms_refusal_with_no_sdk_import(self):
        # The emergency fail-closed lever survives the flip: passing False
        # refuses BEFORE any credential read or SDK import.
        before = "alpaca" in sys.modules
        with self.assertRaisesRegex(NotImplementedError, "UNVERIFIED"):
            alpaca_quote_source(symbols=["AAPL"], allow_unverified_live=False)
        if not before:
            self.assertNotIn("alpaca", sys.modules)


class TestFeedAcceptsMbp1(unittest.TestCase):
    def test_live_feed_constructs_with_alpaca_provenance(self):
        feed = LiveQuoteFeed(
            record_source=iter(()), symbols=["AAPL"],
            dataset=DATASET, schema=SCHEMA,
            stop_at_utc="2026-07-10T20:15:00.000000Z")
        self.assertEqual(feed.data_quality_counts(), {})


class TestPaperSessionComposition(unittest.TestCase):
    def test_live_source_alpaca_iex_pins_dataset_and_schema(self):
        from agent import orchestrator as orch_mod
        from agent import paper_session
        from agent.paper_session import SessionResult

        result = SessionResult(
            mode="observe", session_date_et="2026-07-06", trading_day=True,
            ticks_run=0, kill_state="monitoring", sod_clean=None,
            eod_clean=None, drift_latched=False, feed_truncated=False,
            report_path=None, report=None, exit_code=0)
        feed = mock.Mock()
        with TemporaryDirectory() as tmp, \
             mock.patch.object(orch_mod, "Orchestrator",
                               return_value=mock.Mock()), \
             mock.patch.object(paper_session, "run_paper_session",
                               return_value=result), \
             mock.patch("agent.marketdata.live_feed.LiveQuoteFeed",
                        return_value=feed) as feed_ctor, \
             mock.patch("agent.marketdata.alpaca_feed.alpaca_quote_source"
                        ) as source:
            rc = paper_session._main([
                "--journal-dir", str(Path(tmp) / "journal"),
                "--live", "--live-source", "alpaca-iex",
                "--symbols", "AAPL",
                "--session-date", "2026-07-06",
                "--report-dir", str(Path(tmp) / "reports")])
        self.assertEqual(rc, 0)
        kwargs = feed_ctor.call_args.kwargs
        self.assertEqual(kwargs["dataset"], "ALPACA.IEX")
        self.assertEqual(kwargs["schema"], "mbp-1")
        # eager epoch-0 construction keeps fail-fast semantics; the verified
        # adapter default governs (no allow_unverified_live override here)
        source.assert_called_once_with(
            symbols=["AAPL"], reconnect_epoch=0)


if __name__ == "__main__":
    unittest.main()
