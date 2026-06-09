"""Databento schema -> typed parsed event (contract §B; tests §N).

S6 contributor (correlation metadata born on every event) and the S2
"floats are born here" seam: every vendor price/size becomes a Decimal via
Decimal(str(x)), is finite-checked, and is quantized at parse so str(Decimal)
is canonical. A non-finite / None / float-NaN price, a sub-$1 sub-penny price
that would not round-trip, a fractional share, an unknown schema, or a missing
required field all FAIL LOUD (fail-closed, spec §14).

BLOCKER 1: the vendor sequence is `vendor_seq` everywhere (a top-level `seq`
collides with the journal's reserved monotonic seq). BLOCKER 2: to_row/from_row
are the single flat persistence<->replay shape; from_row(to_row(ev)) == ev.
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path

from recorder.event import (
    BarEvent,
    DefinitionEvent,
    DepthEvent,
    DepthLevel,
    MalformedRecord,
    NonFinitePrice,
    PRICE_QUANTUM,
    PrecisionLoss,
    Provenance,
    QuoteEvent,
    SCHEMA_REGISTRY,
    SIZE_QUANTUM,
    TradeEvent,
    UnknownSchema,
    _quantize_checked,
    parse,
)
from recorder.event_row import from_row, to_row

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "databento"


def _load_jsonl(name):
    path = FIXTURES / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _parse_record(record):
    """Parse one fixture record using its own dataset/schema, with a fixed recv stamp."""
    return parse(
        record,
        dataset=record["dataset"],
        schema=record["schema"],
        reconnect_epoch=0,
        ts_recv_utc="2026-06-09T13:30:00.000999Z",
    )


class TestQuoteParse(unittest.TestCase):
    def test_parses_tbbo_quote_decimal_prices(self):
        record = _load_jsonl("equs_mini_tbbo_sample.jsonl")[0]
        ev = _parse_record(record)
        self.assertIsInstance(ev, QuoteEvent)
        for value in (ev.bid_px, ev.bid_sz, ev.ask_px, ev.ask_sz):
            self.assertIsInstance(value, Decimal)
            self.assertNotIsInstance(value, float)
        self.assertEqual(ev.bid_px, Decimal("201.1500"))
        self.assertEqual(ev.ask_px, Decimal("201.1600"))
        self.assertEqual(ev.bid_sz, Decimal("300"))
        self.assertEqual(ev.ask_sz, Decimal("200"))

    def test_provenance_carries_correlation_metadata(self):
        record = _load_jsonl("equs_mini_tbbo_sample.jsonl")[0]
        ev = _parse_record(record)
        prov = ev.provenance
        self.assertEqual(prov.dataset, "EQUS.MINI")
        self.assertEqual(prov.schema, "tbbo")
        self.assertEqual(prov.instrument_id, 1001)
        self.assertEqual(prov.symbol, "AAPL")
        self.assertEqual(prov.vendor_seq, 1001)
        self.assertEqual(prov.ts_event_utc, "2026-06-09T13:30:00.100000Z")
        self.assertEqual(prov.ts_recv_utc, "2026-06-09T13:30:00.000999Z")
        self.assertEqual(prov.reconnect_epoch, 0)

    def test_provenance_has_no_seq_attribute(self):
        # BLOCKER 1: the vendor sequence is vendor_seq, never `seq`.
        record = _load_jsonl("equs_mini_tbbo_sample.jsonl")[0]
        prov = _parse_record(record).provenance
        self.assertFalse(hasattr(prov, "seq"))
        self.assertTrue(hasattr(prov, "vendor_seq"))


class TestDepthParse(unittest.TestCase):
    def test_parses_mbp10_depth_ladder(self):
        record = _load_jsonl("mbp10_depth_sample.jsonl")[0]
        ev = _parse_record(record)
        self.assertIsInstance(ev, DepthEvent)
        self.assertLessEqual(len(ev.bids), 10)
        self.assertLessEqual(len(ev.asks), 10)
        for level in (*ev.bids, *ev.asks):
            self.assertIsInstance(level, DepthLevel)
            self.assertIsInstance(level.px, Decimal)
            self.assertIsInstance(level.sz, Decimal)
            self.assertIsInstance(level.ct, int)

    def test_size_300pt0_canonicalizes(self):
        # MAJOR 3: "300.0" size -> Decimal('300') -> str "300".
        record = _load_jsonl("mbp10_depth_sample.jsonl")[0]
        ev = _parse_record(record)
        first_bid = ev.bids[0]
        self.assertEqual(first_bid.sz, Decimal("300"))
        self.assertEqual(str(first_bid.sz), "300")

    def test_fractional_share_size_raises_precision_loss(self):
        # MAJOR 3: a fractional "1.5" size does not round-trip SIZE_QUANTUM -> PrecisionLoss.
        record = {
            "dataset": "<DEPTH_DATASET>", "schema": "mbp-10", "instrument_id": 1001,
            "symbol": "AAPL", "vendor_seq": 2099, "ts_event": "2026-06-09T13:30:00.900000Z",
            "bids": [["201.1500", "1.5", 1]], "asks": [["201.1600", "100", 1]],
        }
        with self.assertRaises(PrecisionLoss):
            _parse_record(record)

    def test_negative_depth_level_size_raises_malformed(self):
        # G3 (R5): the negative depth-level size guard (event._depth_level: sz < 0).
        # '-1' quantizes cleanly to a whole share, so it reaches the sz<0 guard (not the
        # round-trip guard) -> MalformedRecord. A standalone sz<0 level must fail loud.
        record = {
            "dataset": "<DEPTH_DATASET>", "schema": "mbp-10", "instrument_id": 1001,
            "symbol": "AAPL", "vendor_seq": 2097, "ts_event": "2026-06-09T13:30:00.700000Z",
            "bids": [["200.0000", "-1", 1]], "asks": [["201.0000", "100", 1]],
        }
        with self.assertRaises(MalformedRecord):
            _parse_record(record)

    def test_more_than_10_depth_levels_is_malformed(self):
        levels = [[f"201.{i:04d}", "100", 1] for i in range(11)]
        record = {
            "dataset": "<DEPTH_DATASET>", "schema": "mbp-10", "instrument_id": 1001,
            "symbol": "AAPL", "vendor_seq": 2098, "ts_event": "2026-06-09T13:30:00.800000Z",
            "bids": levels, "asks": [["202.0000", "100", 1]],
        }
        with self.assertRaises(MalformedRecord):
            _parse_record(record)


class TestRegistryDispatch(unittest.TestCase):
    def test_parses_trade_and_bar_and_definition(self):
        trade = _parse_record({
            "dataset": "EQUS.MINI", "schema": "trades", "instrument_id": 1001, "symbol": "AAPL",
            "vendor_seq": 4001, "ts_event": "2026-06-09T13:30:00.300000Z",
            "price": "201.5000", "size": "100", "side": "B",
        })
        self.assertIsInstance(trade, TradeEvent)
        self.assertEqual(trade.price, Decimal("201.5000"))
        self.assertEqual(trade.size, Decimal("100"))
        self.assertEqual(trade.side, "B")

        bar = _parse_record({
            "dataset": "EQUS.MINI", "schema": "ohlcv-1m", "instrument_id": 1001, "symbol": "AAPL",
            "vendor_seq": 5001, "ts_event": "2026-06-09T13:31:00.000000Z",
            "open": "201.0000", "high": "202.0000", "low": "200.5000",
            "close": "201.7500", "volume": "12000",
        })
        self.assertIsInstance(bar, BarEvent)
        self.assertEqual(bar.open, Decimal("201.0000"))
        self.assertEqual(bar.volume, Decimal("12000"))

        defn = _parse_record({
            "dataset": "EQUS.MINI", "schema": "definitions", "instrument_id": 1001, "symbol": "AAPL",
            "vendor_seq": 6001, "ts_event": "2026-06-09T13:00:00.000000Z",
            "mic": "XNAS", "raw_symbol": "AAPL",
        })
        self.assertIsInstance(defn, DefinitionEvent)
        self.assertEqual(defn.mic, "XNAS")
        self.assertEqual(defn.raw_symbol, "AAPL")

    def test_schema_registry_dispatch_isinstance(self):
        for schema, event_type in SCHEMA_REGISTRY.items():
            ev = _parse_record(_minimal_record_for(schema))
            self.assertIsInstance(ev, event_type)
            self.assertEqual(ev.provenance.schema, schema)

    def test_unknown_schema_raises(self):
        record = {
            "dataset": "EQUS.MINI", "schema": "mbo", "instrument_id": 1001, "symbol": "AAPL",
            "vendor_seq": 7001, "ts_event": "2026-06-09T13:30:00.000000Z",
        }
        with self.assertRaises(UnknownSchema):
            _parse_record(record)


class TestTradeSideVocabulary(unittest.TestCase):
    """D4 (R2#4): trades `side` is a CLOSED vocabulary {A,B,N}; anything else
    RAISES MalformedRecord at parse (fail-closed, not fail-open)."""

    def _trade_record(self, side):
        return {
            "dataset": "EQUS.MINI", "schema": "trades", "instrument_id": 1001, "symbol": "AAPL",
            "vendor_seq": 4002, "ts_event": "2026-06-09T13:30:00.400000Z",
            "price": "201.5000", "size": "100", "side": side,
        }

    def test_valid_sides_parse(self):
        for side in ("A", "B", "N"):
            ev = _parse_record(self._trade_record(side))
            self.assertIsInstance(ev, TradeEvent)
            self.assertEqual(ev.side, side)

    def test_unknown_side_raises_malformed(self):
        for bad in ("X", "a", "b", "BUY", "", "S", "1"):
            with self.assertRaises(MalformedRecord):
                _parse_record(self._trade_record(bad))


class TestFailClosed(unittest.TestCase):
    def test_float_or_nonfinite_price_raises(self):
        for bad in (float("nan"), float("inf"), 201.15, None):
            record = {
                "dataset": "EQUS.MINI", "schema": "tbbo", "instrument_id": 1001, "symbol": "AAPL",
                "vendor_seq": 8001, "ts_event": "2026-06-09T13:30:00.000000Z",
                "bid_px": bad, "bid_sz": "300", "ask_px": "201.1600", "ask_sz": "200",
            }
            with self.assertRaises(NonFinitePrice):
                _parse_record(record)

    def test_subpenny_price_raises_precision_loss(self):
        # MAJOR 4: sub-$1 sub-penny price (0.00005) would zero on quantize -> PrecisionLoss.
        with self.assertRaises(PrecisionLoss):
            for record in _load_jsonl("sub_dollar_subpenny_sample.jsonl"):
                _parse_record(record)

    def test_quantize_checked_subpenny_raises(self):
        self.assertEqual(
            Decimal("0.00005").quantize(PRICE_QUANTUM), Decimal("0.0000")
        )  # sanity: it really would zero
        with self.assertRaises(PrecisionLoss):
            _quantize_checked("0.00005", PRICE_QUANTUM, field="bid_px")

    def test_quantize_checked_canonicalizes(self):
        self.assertEqual(_quantize_checked("300.0", SIZE_QUANTUM, field="sz"), Decimal("300"))
        self.assertEqual(str(_quantize_checked("300.0", SIZE_QUANTUM, field="sz")), "300")
        self.assertEqual(str(_quantize_checked("1.50", PRICE_QUANTUM, field="px")), "1.5000")

    def test_quantize_checked_rejects_nonfinite(self):
        for bad in (float("nan"), float("inf"), None):
            with self.assertRaises(NonFinitePrice):
                _quantize_checked(bad, PRICE_QUANTUM, field="px")

    def test_malformed_record_is_fatal(self):
        record = {
            "dataset": "EQUS.MINI", "schema": "tbbo", "instrument_id": 1001, "symbol": "AAPL",
            "vendor_seq": 8002, "ts_event": "2026-06-09T13:30:00.000000Z",
            "bid_px": "201.1500", "bid_sz": "300", "ask_px": "201.1600",  # ask_sz missing
        }
        with self.assertRaises(MalformedRecord):
            _parse_record(record)


class TestRowRoundtrip(unittest.TestCase):
    def _all_events(self):
        return [
            _parse_record(_load_jsonl("equs_mini_tbbo_sample.jsonl")[0]),
            _parse_record(_load_jsonl("mbp10_depth_sample.jsonl")[0]),
            _parse_record({
                "dataset": "EQUS.MINI", "schema": "trades", "instrument_id": 1001, "symbol": "AAPL",
                "vendor_seq": 4001, "ts_event": "2026-06-09T13:30:00.300000Z",
                "price": "201.5000", "size": "100", "side": "B",
            }),
            _parse_record({
                "dataset": "EQUS.MINI", "schema": "ohlcv-1m", "instrument_id": 1001, "symbol": "AAPL",
                "vendor_seq": 5001, "ts_event": "2026-06-09T13:31:00.000000Z",
                "open": "201.0000", "high": "202.0000", "low": "200.5000",
                "close": "201.7500", "volume": "12000",
            }),
            _parse_record({
                "dataset": "EQUS.MINI", "schema": "definitions", "instrument_id": 1001, "symbol": "AAPL",
                "vendor_seq": 6001, "ts_event": "2026-06-09T13:00:00.000000Z",
                "mic": "XNAS", "raw_symbol": "AAPL",
            }),
        ]

    def test_row_roundtrip_is_identity(self):
        for ev in self._all_events():
            row = to_row(ev)
            self.assertIsInstance(row, dict)
            self.assertIn("vendor_seq", row)
            self.assertNotIn("seq", row)
            self.assertEqual(from_row(row), ev)

    def test_row_has_no_journal_reserved_keys(self):
        reserved = {"event_type", "run_id", "seq", "hash", "decision_id", "order_id", "ts_utc"}
        for ev in self._all_events():
            self.assertEqual(reserved & set(to_row(ev)), set())

    def test_from_row_unknown_schema_raises(self):
        row = to_row(self._all_events()[0])
        row["schema"] = "mbo"
        with self.assertRaises(UnknownSchema):
            from_row(row)

    def test_from_row_missing_required_field_raises_malformed(self):
        # G4 (R5): from_row's missing-required-field raise (§B2 BLOCKER-2 seam,
        # event_row._require). Dropping a required flat field -> MalformedRecord.
        # instrument_id is required for the provenance build on every event_type.
        row = to_row(self._all_events()[0])  # a QuoteEvent row
        self.assertIn("instrument_id", row)  # guard: it WAS present before we drop it
        del row["instrument_id"]
        with self.assertRaises(MalformedRecord):
            from_row(row)

    def test_from_row_missing_depth_side_raises_malformed(self):
        # G4 (R5): the depth 'bids' side is also a required flat field on a DepthEvent row.
        depth_row = to_row(_parse_record(_load_jsonl("mbp10_depth_sample.jsonl")[0]))
        self.assertIn("bids", depth_row)
        del depth_row["bids"]
        with self.assertRaises(MalformedRecord):
            from_row(depth_row)


def _minimal_record_for(schema):
    base = {
        "dataset": "EQUS.MINI", "schema": schema, "instrument_id": 1001, "symbol": "AAPL",
        "vendor_seq": 1, "ts_event": "2026-06-09T13:30:00.000000Z",
    }
    if SCHEMA_REGISTRY[schema] is QuoteEvent:
        base.update(bid_px="201.1500", bid_sz="300", ask_px="201.1600", ask_sz="200")
    elif SCHEMA_REGISTRY[schema] is TradeEvent:
        base.update(price="201.5000", size="100", side="B")
    elif SCHEMA_REGISTRY[schema] is BarEvent:
        base.update(open="201.0000", high="202.0000", low="200.5000", close="201.7500", volume="12000")
    elif SCHEMA_REGISTRY[schema] is DepthEvent:
        base.update(bids=[["201.1500", "300", 3]], asks=[["201.1600", "200", 2]])
    elif SCHEMA_REGISTRY[schema] is DefinitionEvent:
        base.update(mic="XNAS", raw_symbol="AAPL")
    return base


if __name__ == "__main__":
    unittest.main()
