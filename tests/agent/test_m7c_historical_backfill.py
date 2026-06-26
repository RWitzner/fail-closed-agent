"""M7c credentialed historical backfill + cross-sectional input-manifest builder.

The pure builder/normalizer/derivation is offline-complete and the key property is the
round-trip: a manifest built by ``build_cross_sectional_input_manifest`` must be accepted
by ``validate_historical_cross_sectional_manifest`` AND drive the real cross-sectional
harness through the artifact writer. The live Databento pull is exercised only through an
injected fake source (the real SDK adapter is tier-2b, verified against the live API).
"""
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from agent.backtest_historical import (
    HistoricalCrossSectionalArtifactBuildResult,
    validate_historical_cross_sectional_manifest,
    write_m7_historical_cross_sectional_artifact,
)
from agent.historical_backfill import (
    _dbn_bbo1m_record_to_event_dict,
    _dbn_price,
    _ns_to_iso_utc,
    build_cross_sectional_input_manifest,
    cross_sectional_data_pin,
    derive_session_windows,
    instrument_ids_from_rows,
    normalize_quote_event,
    pull_normalized_window,
    write_quote_rows_jsonl,
)
from agent.strategies.relative_strength import STRATEGY_ID as RS_STRATEGY_ID
from recorder.event import Provenance, QuoteEvent

UNIVERSE = ("AAPL", "MSFT", "NVDA", "AMZN", "META",
            "GOOGL", "TSLA", "AVGO", "COST", "NFLX")
INSTRUMENT_IDS = {sym: 1001 + i for i, sym in enumerate(UNIVERSE)}
_HYPOTHESIS = "m7c_relative_strength_market_neutral_v0_20260613"
_SELECTION = "Reuse the full ordered M7 broad universe before relative-strength metrics."
_CAL_PIN = "xnys-rth-regular-unit-test-v1"
_WIGGLE = Decimal("0.0010")


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row(symbol, *, minute_dt, mid: Decimal, iid):
    ts_event = minute_dt
    ts_recv = ts_event + timedelta(milliseconds=300)
    return {
        "dataset": "EQUS.MINI",
        "schema": "bbo-1m",
        "symbol": symbol,
        "instrument_id": iid,
        "vendor_seq": 0,
        "ts_event_utc": _utc(ts_event),
        "ts_recv_utc": _utc(ts_recv),
        "bid_px": str((mid - Decimal("0.01")).quantize(Decimal("0.0001"))),
        "bid_sz": "100",
        "ask_px": str((mid + Decimal("0.01")).quantize(Decimal("0.0001"))),
        "ask_sz": "100",
        "reconnect_epoch": 0,
    }


def _symbol_rows(symbol, *, sessions, n_minutes, slope: Decimal,
                 base: Decimal = Decimal("100.0000")):
    # Price rises with a CONTINUOUS global minute across sessions (timestamps reset per
    # session) so a higher-slope name stays cross-sectionally strongest end to end —
    # otherwise resetting the path each session scrambles the top-2 selection.
    iid = INSTRUMENT_IDS[symbol]
    rows = []
    global_minute = 0
    for session in sessions:
        start = datetime.fromisoformat(f"{session}T13:30:00+00:00")
        for minute in range(n_minutes):
            mid = (base + slope * Decimal(global_minute)
                   + (_WIGGLE if global_minute % 2 else Decimal("0")))
            rows.append(_row(symbol, minute_dt=start + timedelta(minutes=minute),
                             mid=mid, iid=iid))
            global_minute += 1
    return rows


def _universe_rows(*, sessions, n_minutes):
    return {
        sym: _symbol_rows(sym, sessions=sessions, n_minutes=n_minutes,
                          slope=Decimal(len(UNIVERSE) - i) * Decimal("0.0010"))
        for i, sym in enumerate(UNIVERSE)
    }


class TestNormalizeAndDerive(unittest.TestCase):
    def _quote_event(self):
        prov = Provenance(
            dataset="EQUS.MINI", schema="bbo-1m", instrument_id=38, symbol="AAPL",
            vendor_seq=None, ts_event_utc="2026-05-11T13:30:59.678428Z",
            ts_recv_utc="2026-05-11T13:31:00.000000Z", reconnect_epoch=0)
        return QuoteEvent(provenance=prov, bid_px=Decimal("200.0000"),
                          bid_sz=Decimal("120"), ask_px=Decimal("200.0100"),
                          ask_sz=Decimal("100"))

    def test_normalize_quote_event_emits_canonical_row(self):
        row = normalize_quote_event(self._quote_event(), dataset="EQUS.MINI",
                                    schema="bbo-1m")
        self.assertEqual(row, {
            "dataset": "EQUS.MINI", "schema": "bbo-1m", "symbol": "AAPL",
            "instrument_id": 38, "vendor_seq": 0,
            "ts_event_utc": "2026-05-11T13:30:59.678428Z",
            "ts_recv_utc": "2026-05-11T13:31:00.000000Z",
            "bid_px": "200.0000", "bid_sz": "120", "ask_px": "200.0100",
            "ask_sz": "100", "reconnect_epoch": 0,
        })

    def test_instrument_ids_from_rows(self):
        rows = _universe_rows(sessions=("2026-06-15",), n_minutes=3)
        ids = instrument_ids_from_rows(rows)
        self.assertEqual(ids, INSTRUMENT_IDS)

    def test_instrument_ids_rejects_mixed(self):
        rows = _universe_rows(sessions=("2026-06-15",), n_minutes=3)
        rows["AAPL"][0] = {**rows["AAPL"][0], "instrument_id": 999999}
        with self.assertRaises(ValueError):
            instrument_ids_from_rows(rows)

    def test_derive_session_windows(self):
        rows = _universe_rows(sessions=("2026-06-15", "2026-06-16"), n_minutes=2)
        windows = derive_session_windows(rows)
        self.assertEqual(set(windows), {"2026-06-15", "2026-06-16"})
        self.assertEqual(windows["2026-06-15"]["rth_open_utc"],
                         "2026-06-15T13:30:00.000000Z")
        self.assertEqual(windows["2026-06-15"]["rth_close_utc"],
                         "2026-06-15T20:00:00.000000Z")


class TestManifestBuilderRoundTrip(unittest.TestCase):
    def setUp(self):
        self.rows = _universe_rows(sessions=("2026-06-15",), n_minutes=3)
        self.manifest = build_cross_sectional_input_manifest(
            symbol_rows=self.rows, universe=UNIVERSE, dataset="EQUS.MINI",
            schema="bbo-1m", hypothesis_id=_HYPOTHESIS, selection_rule=_SELECTION,
            calendar_pin=_CAL_PIN)

    def test_built_manifest_validates(self):
        parsed = validate_historical_cross_sectional_manifest(
            self.manifest, symbol_quote_rows=self.rows, dataset="EQUS.MINI",
            schema="bbo-1m", data_pin=cross_sectional_data_pin(self.manifest))
        self.assertEqual(parsed.universe_symbols, UNIVERSE)
        self.assertEqual(parsed.instrument_ids, INSTRUMENT_IDS)
        self.assertEqual(parsed.horizon, "30m")
        self.assertEqual(parsed.slippage_cap_bps, Decimal("25"))
        self.assertEqual(parsed.symbol_data_pins["AAPL"],
                         f"{cross_sectional_data_pin(self.manifest)}:AAPL")

    def test_data_pin_binds_manifest_hash(self):
        self.assertEqual(
            cross_sectional_data_pin(self.manifest),
            f"EQUS.MINI:bbo-1m:1m:historical:{self.manifest['manifest_hash']}")

    def test_builder_rejects_universe_row_mismatch(self):
        rows = dict(self.rows)
        del rows["NFLX"]
        with self.assertRaises(ValueError):
            build_cross_sectional_input_manifest(
                symbol_rows=rows, universe=UNIVERSE, dataset="EQUS.MINI",
                schema="bbo-1m", hypothesis_id=_HYPOTHESIS,
                selection_rule=_SELECTION, calendar_pin=_CAL_PIN)

    def test_builder_rejects_duplicate_universe(self):
        with self.assertRaises(ValueError):
            build_cross_sectional_input_manifest(
                symbol_rows=self.rows, universe=UNIVERSE + ("AAPL",),
                dataset="EQUS.MINI", schema="bbo-1m", hypothesis_id=_HYPOTHESIS,
                selection_rule=_SELECTION, calendar_pin=_CAL_PIN)

    def test_custom_sessions_must_cover_every_row_date(self):
        # Two session dates in the rows, but the operator supplies a window for only one.
        rows = _universe_rows(sessions=("2026-06-15", "2026-06-16"), n_minutes=3)
        partial_sessions = {
            "2026-06-15": {"rth_open_utc": "2026-06-15T13:30:00.000000Z",
                           "rth_close_utc": "2026-06-15T20:00:00.000000Z"},
        }
        with self.assertRaises(ValueError) as ctx:
            build_cross_sectional_input_manifest(
                symbol_rows=rows, universe=UNIVERSE, dataset="EQUS.MINI",
                schema="bbo-1m", hypothesis_id=_HYPOTHESIS,
                selection_rule=_SELECTION, calendar_pin=_CAL_PIN,
                sessions=partial_sessions)
        self.assertIn("2026-06-16", str(ctx.exception))

    def test_zero_row_symbol_is_rejected_even_with_explicit_ids(self):
        rows = dict(self.rows)
        rows["NFLX"] = []
        with self.assertRaises(ValueError) as ctx:
            build_cross_sectional_input_manifest(
                symbol_rows=rows, universe=UNIVERSE, dataset="EQUS.MINI",
                schema="bbo-1m", hypothesis_id=_HYPOTHESIS,
                selection_rule=_SELECTION, calendar_pin=_CAL_PIN,
                instrument_ids=INSTRUMENT_IDS)
        self.assertIn("NFLX", str(ctx.exception))

    def test_blackout_and_custom_horizon_flow_into_manifest(self):
        manifest = build_cross_sectional_input_manifest(
            symbol_rows=self.rows, universe=UNIVERSE, dataset="EQUS.MINI",
            schema="bbo-1m", hypothesis_id=_HYPOTHESIS, selection_rule=_SELECTION,
            calendar_pin=_CAL_PIN, blackout_session_dates_et=("2026-06-15",),
            horizon="5m")
        parsed = validate_historical_cross_sectional_manifest(
            manifest, symbol_quote_rows=self.rows, dataset="EQUS.MINI",
            schema="bbo-1m", data_pin=cross_sectional_data_pin(manifest))
        self.assertEqual(parsed.horizon, "5m")
        self.assertIn("2026-06-15", parsed.ca_blackout_session_dates_et)


class TestBuilderDrivesRealHarness(unittest.TestCase):
    def test_build_validate_write_run_end_to_end(self):
        # Multi-session synthetic data -> build manifest -> the real cross-sectional
        # harness runs through the writer (no patch). It fails the 20-session pinned gate
        # (only 3 sessions) but proves the builder's manifest drives the whole pipeline.
        sessions = ("2026-06-15", "2026-06-16", "2026-06-17")
        rows = _universe_rows(sessions=sessions, n_minutes=130)
        manifest = build_cross_sectional_input_manifest(
            symbol_rows=rows, universe=UNIVERSE, dataset="EQUS.MINI",
            schema="bbo-1m", hypothesis_id=_HYPOTHESIS, selection_rule=_SELECTION,
            calendar_pin=_CAL_PIN)
        with TemporaryDirectory() as tmp:
            result = write_m7_historical_cross_sectional_artifact(
                artifacts_dir=tmp,
                symbol_quote_rows=rows,
                rules_hash="rh-backfill-it",
                data_pin=cross_sectional_data_pin(manifest),
                dataset="EQUS.MINI",
                schema="bbo-1m",
                created_utc="2026-06-18T20:00:00.000000Z",
                input_manifest=manifest,
                builder_git_commit="test-commit",
                allow_reviewed_artifact=True,
            )
            self.assertIsInstance(
                result, HistoricalCrossSectionalArtifactBuildResult)
            self.assertEqual({t.symbol for t in result.backtest.trades},
                             {"AAPL", "MSFT"})
            self.assertFalse(result.criteria.passed)  # only 3 sessions
            self.assertFalse((Path(tmp) / f"{RS_STRATEGY_ID}.json").exists())


class TestLivePullSeam(unittest.TestCase):
    def test_pull_with_injected_source_normalizes_per_symbol(self):
        def source(symbol):
            prov = Provenance(
                dataset="EQUS.MINI", schema="bbo-1m",
                instrument_id=INSTRUMENT_IDS[symbol], symbol=symbol,
                vendor_seq=None, ts_event_utc="2026-06-15T13:30:59.000000Z",
                ts_recv_utc="2026-06-15T13:31:00.000000Z", reconnect_epoch=0)
            return [QuoteEvent(provenance=prov, bid_px=Decimal("100.0000"),
                               bid_sz=Decimal("100"), ask_px=Decimal("100.0200"),
                               ask_sz=Decimal("100"))]

        out = pull_normalized_window(
            dataset="EQUS.MINI", schema="bbo-1m", universe=("AAPL", "MSFT"),
            start_utc="2026-06-15T13:30:00.000000Z",
            end_utc="2026-06-15T20:00:00.000000Z", quote_event_source=source)
        self.assertEqual(set(out), {"AAPL", "MSFT"})
        self.assertEqual(out["AAPL"][0]["symbol"], "AAPL")
        self.assertEqual(out["AAPL"][0]["instrument_id"], INSTRUMENT_IDS["AAPL"])
        self.assertEqual(out["MSFT"][0]["bid_px"], "100.0000")

    def test_pull_with_injected_source_imports_no_databento(self):
        import sys

        def source(symbol):
            prov = Provenance(
                dataset="EQUS.MINI", schema="bbo-1m",
                instrument_id=INSTRUMENT_IDS[symbol], symbol=symbol,
                vendor_seq=None, ts_event_utc="2026-06-15T13:30:59.000000Z",
                ts_recv_utc="2026-06-15T13:31:00.000000Z", reconnect_epoch=0)
            return [QuoteEvent(provenance=prov, bid_px=Decimal("100.0000"),
                               bid_sz=Decimal("100"), ask_px=Decimal("100.0200"),
                               ask_sz=Decimal("100"))]

        pull_normalized_window(
            dataset="EQUS.MINI", schema="bbo-1m", universe=("AAPL",),
            start_utc="2026-06-15T13:30:00.000000Z",
            end_utc="2026-06-15T20:00:00.000000Z", quote_event_source=source)
        self.assertNotIn("databento", sys.modules)

    def test_pull_filters_extended_hours_rows(self):
        def source(symbol):
            prov_kw = dict(dataset="EQUS.MINI", schema="bbo-1m",
                           instrument_id=INSTRUMENT_IDS[symbol], symbol=symbol,
                           vendor_seq=None, reconnect_epoch=0)
            quote = dict(bid_px=Decimal("100.0000"), bid_sz=Decimal("100"),
                         ask_px=Decimal("100.0200"), ask_sz=Decimal("100"))
            # one pre-open (08:00), one RTH (14:00), one post-close (21:00) boundary
            return [
                QuoteEvent(provenance=Provenance(
                    ts_event_utc="2026-06-15T07:59:59.000000Z",
                    ts_recv_utc="2026-06-15T08:00:00.000000Z", **prov_kw), **quote),
                QuoteEvent(provenance=Provenance(
                    ts_event_utc="2026-06-15T13:59:59.000000Z",
                    ts_recv_utc="2026-06-15T14:00:00.000000Z", **prov_kw), **quote),
                QuoteEvent(provenance=Provenance(
                    ts_event_utc="2026-06-15T20:59:59.000000Z",
                    ts_recv_utc="2026-06-15T21:00:00.000000Z", **prov_kw), **quote),
            ]

        out = pull_normalized_window(
            dataset="EQUS.MINI", schema="bbo-1m", universe=("AAPL",),
            start_utc="2026-06-15T08:00:00.000000Z",
            end_utc="2026-06-15T21:00:00.000000Z", quote_event_source=source)
        kept = [row["ts_recv_utc"] for row in out["AAPL"]]
        self.assertEqual(kept, ["2026-06-15T14:00:00.000000Z"])

    def test_dbn_price_scales_int_1e9_fixed_point(self):
        self.assertEqual(_dbn_price(200000000000), Decimal("200.00"))
        self.assertEqual(_dbn_price(100020000000), Decimal("100.02"))

    def test_ns_to_iso_utc_is_exact(self):
        self.assertEqual(_ns_to_iso_utc(1_000_000_000), "1970-01-01T00:00:01.000000Z")

    def test_dbn_record_adapter_decodes_top_of_book(self):
        record = SimpleNamespace(
            bid_px_00=200000000000, ask_px_00=200010000000,
            bid_sz_00=120, ask_sz_00=100, instrument_id=38,
            ts_event=1_000_000_000, ts_recv=1_000_500_000, sequence=7)
        out = _dbn_bbo1m_record_to_event_dict(record, symbol="AAPL")
        self.assertEqual(out["bid_px"], "200.0000")
        self.assertEqual(out["ask_px"], "200.0100")
        self.assertEqual(out["bid_sz"], "120")
        self.assertEqual(out["symbol"], "AAPL")
        # vendor_seq (recorder.parse contract name) carries the DBN sequence; ts_recv is
        # ISO, not raw ns (parse takes ts_recv_utc as a kwarg).
        self.assertEqual(out["vendor_seq"], 7)
        self.assertEqual(out["ts_event"], "1970-01-01T00:00:01.000000Z")
        self.assertEqual(out["ts_recv_utc"], "1970-01-01T00:00:01.000500Z")
        self.assertNotIn("ts_recv_utc_ns", out)

    def test_dbn_record_adapter_drops_undefined_side(self):
        record = SimpleNamespace(
            bid_px_00=9223372036854775807, ask_px_00=200010000000,
            bid_sz_00=0, ask_sz_00=100, instrument_id=38,
            ts_event=1_000_000_000, ts_recv=1_000_500_000, sequence=0)
        with self.assertRaises(ValueError):
            _dbn_bbo1m_record_to_event_dict(record, symbol="AAPL")

    def test_dbn_record_adapter_fails_closed_on_bad_fields(self):
        base = dict(bid_px_00=200000000000, ask_px_00=200010000000,
                    bid_sz_00=120, ask_sz_00=100, instrument_id=38,
                    ts_event=1_000_000_000, ts_recv=1_000_500_000, sequence=0)
        # Missing bid price (getattr default None) -> ValueError, not TypeError.
        with self.assertRaises(ValueError):
            _dbn_bbo1m_record_to_event_dict(
                SimpleNamespace(**{**base, "bid_px_00": None}), symbol="AAPL")
        # Float price would silently truncate via int() -> reject.
        with self.assertRaises(ValueError):
            _dbn_bbo1m_record_to_event_dict(
                SimpleNamespace(**{**base, "ask_px_00": 200.01}), symbol="AAPL")
        # Non-positive size -> reject at source.
        with self.assertRaises(ValueError):
            _dbn_bbo1m_record_to_event_dict(
                SimpleNamespace(**{**base, "ask_sz_00": 0}), symbol="AAPL")
        # Missing ts_recv must NOT silently fall back to ts_event (fail-closed latency).
        bad_recv = {k: v for k, v in base.items() if k != "ts_recv"}
        with self.assertRaises(ValueError):
            _dbn_bbo1m_record_to_event_dict(SimpleNamespace(**bad_recv), symbol="AAPL")
        # UNDEF timestamp sentinel (UINT64_MAX) = a no-event carried-forward minute -> drop.
        with self.assertRaises(ValueError):
            _dbn_bbo1m_record_to_event_dict(
                SimpleNamespace(**{**base, "ts_event": 18446744073709551615}),
                symbol="AAPL")

    def test_ns_to_iso_utc_truncates_sub_microsecond_and_rolls_seconds(self):
        self.assertEqual(_ns_to_iso_utc(500), "1970-01-01T00:00:00.000000Z")
        self.assertEqual(_ns_to_iso_utc(999_999_999), "1970-01-01T00:00:00.999999Z")
        self.assertEqual(_ns_to_iso_utc(1_000_000_000), "1970-01-01T00:00:01.000000Z")

    def test_write_quote_rows_jsonl_roundtrips(self):
        rows = _universe_rows(sessions=("2026-06-15",), n_minutes=2)["AAPL"]
        with TemporaryDirectory() as tmp:
            path = write_quote_rows_jsonl(Path(tmp) / "AAPL.jsonl", rows)
            from agent.backtest_historical import load_quote_rows_jsonl
            loaded = load_quote_rows_jsonl(path)
            self.assertEqual(list(loaded), rows)


if __name__ == "__main__":
    unittest.main()
