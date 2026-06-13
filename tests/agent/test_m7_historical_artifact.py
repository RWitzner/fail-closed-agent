"""M7 historical reviewed-artifact flow.

This is distinct from the fixture-only ``m7-backtest`` builder: production
``artifacts/backtests`` writes require an explicit reviewed-artifact flag and
must still produce a normal verifier-compatible v2 artifact.
"""
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent.__main__ import main
from agent.backtest_gate import verify_artifact
from agent.backtest_engine import BacktestTrade
from agent.backtest_engine import BacktestSkip
from agent.backtest_historical import (
    HistoricalBacktestResult,
    HistoricalArtifactWriteRefused,
    run_historical_backtest,
    write_m7_historical_diagnostic_export,
    write_m7_historical_diagnostic_index,
    write_m7_historical_artifact,
)
from agent.serializer import dumps, row_hash


_STRATEGY_ID = "directional.momentum_v1"
_STRATEGY_ID_V2 = "directional.momentum_v2"
_RULES_HASH = "rh-historical-test"


def _historical_rows():
    rows = []
    start_dates = (
        "2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04",
        "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10",
        "2026-06-11", "2026-06-12", "2026-06-15", "2026-06-16",
        "2026-06-17", "2026-06-18", "2026-06-19", "2026-06-22",
        "2026-06-23", "2026-06-24", "2026-06-25", "2026-06-26",
    )
    price_cents = 10_000
    vendor_seq = 1
    for date in start_dates:
        start = datetime.fromisoformat(date + "T14:30:00+00:00")
        for minute in range(90):
            ts_event = start + timedelta(minutes=minute)
            ts_recv = ts_event + timedelta(milliseconds=300)
            # Non-constant positive path: positive momentum and non-zero vol.
            price_cents += 1 + (minute % 3)
            mid = price_cents / 100
            rows.append({
                "dataset": "EQUS.MINI",
                "schema": "tbbo",
                "symbol": "AAPL",
                "instrument_id": 1001,
                "vendor_seq": vendor_seq,
                "ts_event_utc": (
                    ts_event.astimezone(timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                ),
                "ts_recv_utc": (
                    ts_recv.astimezone(timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                ),
                "bid_px": f"{mid - 0.01:.6f}",
                "bid_sz": "100",
                "ask_px": f"{mid + 0.01:.6f}",
                "ask_sz": "100",
                "reconnect_epoch": 0,
            })
            vendor_seq += 1
    return rows


def _write_jsonl(path, rows):
    import json

    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _quote_rows_hash(rows):
    import hashlib

    hasher = hashlib.sha256()
    for row in rows:
        hasher.update(dumps(row).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _manifest(rows, *, schema="tbbo", blackouts=()):
    sessions = sorted({
        row["ts_event_utc"][:10] for row in rows
        if row.get("schema") == schema and row.get("symbol") == "AAPL"
    })
    body = {
        "v": 1,
        "dataset": "EQUS.MINI",
        "schema": schema,
        "interval": "1m",
        "source_id_prefix": "historical",
        "symbol": "AAPL",
        "instrument_id": 1001,
        "row_count": len(rows),
        "start_utc": rows[0]["ts_event_utc"],
        "end_utc": rows[-1]["ts_event_utc"],
        "quote_rows_sha256": _quote_rows_hash(rows),
        "normalizer_id": "m7-historical-normalized-quotes-v1",
        "raw_source": "unit-test",
        "dropped_rows": {
            "one_sided": 0,
            "undef": 0,
        },
        "universe": {
            "hypothesis_id": "unit-test-aapl-only-v1",
            "selection_rule": "unit test fixture predeclares AAPL",
            "symbols": ["AAPL"],
        },
        "calendar": {
            "calendar_pin": "unit-test-calendar-v1",
            "sessions": {
                session: {
                    "rth_open_utc": f"{session}T13:30:00.000000Z",
                    "rth_close_utc": f"{session}T20:00:00.000000Z",
                }
                for session in sessions
            },
        },
        "corporate_actions": {
            "provenance": "unit-test-ca-v1",
            "blackout_session_dates_et": list(blackouts),
        },
        "execution": {
            "latency_budget_ms": 250,
            "slippage_cap_bps": "25",
            "fee_model_version": "reg_fees_v1",
            "pricing_model_version": "m7-historical-quote-a-b-spread-v1",
            "realism_gap_model_version": "historical_quote_model_vs_raw_mid_v1",
        },
    }
    body["manifest_hash"] = row_hash(body)
    return body


def _data_pin(manifest):
    return (
        f"{manifest['dataset']}:{manifest['schema']}:{manifest['interval']}:"
        f"{manifest['source_id_prefix']}:{manifest['manifest_hash']}"
    )


def _passing_backtest_result():
    dates = (
        "2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04",
        "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10",
        "2026-06-11", "2026-06-12", "2026-06-15", "2026-06-16",
        "2026-06-17", "2026-06-18", "2026-06-19", "2026-06-22",
        "2026-06-23", "2026-06-24", "2026-06-25", "2026-06-26",
    )
    trades = tuple(
        BacktestTrade(
            symbol="AAPL",
            instrument_id=1001,
            qty=Decimal("10"),
            entry_bar_end_utc=f"{dates[index % len(dates)]}T14:31:00.000000Z",
            exit_bar_end_utc=f"{dates[index % len(dates)]}T14:36:00.000000Z",
            entry_mid=Decimal("100.000000"),
            exit_mid=Decimal("100.500000"),
            gross_modeled_usd=Decimal("5.000000"),
            fees_usd=Decimal("0.020000"),
            net_execution_realistic_pnl_usd=Decimal("4.980000"),
            benchmark_pnl_usd=Decimal("1.000000"),
        )
        for index in range(30)
    )
    return HistoricalBacktestResult(
        trades=trades,
        skips=(),
        bar_count=1800,
        candidate_count=30,
        p95_realism_gap_bps=Decimal("4.000000"),
        max_single_fill_divergence_bps=Decimal("8.000000"),
    )


def _diagnostic_backtest_result():
    trade = BacktestTrade(
        symbol="AAPL",
        instrument_id=1001,
        qty=Decimal("10"),
        entry_bar_end_utc="2026-06-01T14:31:00.000000Z",
        exit_bar_end_utc="2026-06-01T14:36:00.000000Z",
        entry_mid=Decimal("100.000000"),
        exit_mid=Decimal("100.250000"),
        gross_modeled_usd=Decimal("2.300000"),
        fees_usd=Decimal("0.020000"),
        net_execution_realistic_pnl_usd=Decimal("2.280000"),
        benchmark_pnl_usd=Decimal("1.000000"),
        decision_bar_end_utc="2026-06-01T14:30:00.000000Z",
        decision_mid=Decimal("99.900000"),
        decision_spread_bps=Decimal("1.000000"),
        entry_spread_bps=Decimal("2.000000"),
        exit_spread_bps=Decimal("3.000000"),
        adverse_entry_move_bps=Decimal("0.500000"),
        decision_realized_vol_21=Decimal("0.010000"),
        decision_z_ret_21=Decimal("0.250000"),
    )
    skip = BacktestSkip(
        reason="latency_lost_edge",
        bucket_end_utc="2026-06-01T14:40:00.000000Z",
        detail={"symbol": "AAPL", "pricing_reasons": ("latency_lost_edge",)},
    )
    return HistoricalBacktestResult(
        trades=(trade,),
        skips=(skip,),
        bar_count=120,
        candidate_count=2,
        p95_realism_gap_bps=Decimal("3.000000"),
        max_single_fill_divergence_bps=Decimal("3.000000"),
    )


class TestHistoricalArtifactFlow(unittest.TestCase):
    def test_reviewed_historical_flow_can_write_verifier_ok_artifact(self):
        with TemporaryDirectory() as tmp:
            rows = _historical_rows()
            manifest = _manifest(rows)
            with patch("agent.backtest_historical.run_historical_backtest",
                       return_value=_passing_backtest_result()):
                result = write_m7_historical_artifact(
                    artifacts_dir=tmp,
                    quote_rows=rows,
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    allow_reviewed_artifact=True,
                )

            self.assertTrue(result.criteria.passed)
            self.assertEqual(result.payload["metrics"]["provenance"]["tier"],
                             "historical_reviewed")
            self.assertEqual(
                result.payload["metrics"]["provenance"]["input_manifest_hash"],
                manifest["manifest_hash"],
            )
            self.assertEqual(
                result.payload["metrics"]["provenance"]["pricing_model_version"],
                "m7-historical-quote-a-b-spread-v1",
            )
            self.assertEqual(
                result.payload["metrics"]["provenance"]["universe_hypothesis_id"],
                "unit-test-aapl-only-v1",
            )
            self.assertEqual(
                result.payload["metrics"]["provenance"]["universe_symbols"],
                ["AAPL"],
            )
            self.assertEqual(result.artifact_path,
                             Path(tmp) / f"{_STRATEGY_ID}.json")
            self.assertEqual(
                verify_artifact(
                    _STRATEGY_ID,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    artifacts_dir=tmp,
                ).status,
                "ok",
            )

    def test_reviewed_historical_flow_can_select_v2_strategy_id(self):
        with TemporaryDirectory() as tmp:
            rows = _historical_rows()
            manifest = _manifest(rows)
            with patch("agent.backtest_historical.run_historical_backtest",
                       return_value=_passing_backtest_result()):
                result = write_m7_historical_artifact(
                    artifacts_dir=tmp,
                    quote_rows=rows,
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    allow_reviewed_artifact=True,
                    strategy_id=_STRATEGY_ID_V2,
                )

            self.assertTrue(result.criteria.passed)
            self.assertEqual(result.payload["strategy_id"], _STRATEGY_ID_V2)
            self.assertEqual(
                result.payload["metrics"]["strategy_version"], _STRATEGY_ID_V2)
            self.assertEqual(result.artifact_path,
                             Path(tmp) / f"{_STRATEGY_ID_V2}.json")
            self.assertEqual(
                verify_artifact(
                    _STRATEGY_ID_V2,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    artifacts_dir=tmp,
                ).status,
                "ok",
            )

    def test_unknown_historical_strategy_id_is_rejected_before_backtest(self):
        rows = _historical_rows()
        manifest = _manifest(rows)
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                write_m7_historical_artifact(
                    artifacts_dir=tmp,
                    quote_rows=rows,
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    allow_reviewed_artifact=True,
                    strategy_id="directional.unknown",
                )

        self.assertIn("unknown strategy_id", str(ctx.exception))

    def test_manifest_hash_is_bound_to_data_pin(self):
        rows = _historical_rows()
        manifest = _manifest(rows)
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                write_m7_historical_artifact(
                    artifacts_dir=tmp,
                    quote_rows=rows,
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin="EQUS.MINI:tbbo:1m:historical:not-the-manifest",
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    allow_reviewed_artifact=True,
                )

        self.assertIn("data_pin", str(ctx.exception))

    def test_manifest_quote_hash_must_match_normalized_rows(self):
        rows = _historical_rows()
        manifest = _manifest(rows)
        manifest["quote_rows_sha256"] = "0" * 64
        manifest["manifest_hash"] = row_hash({
            key: value for key, value in manifest.items()
            if key != "manifest_hash"
        })
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                write_m7_historical_artifact(
                    artifacts_dir=tmp,
                    quote_rows=rows,
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(_manifest(rows)),
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    allow_reviewed_artifact=True,
                )

        self.assertIn("quote_rows_sha256", str(ctx.exception))

    def test_manifest_must_pin_predeclared_universe_hypothesis(self):
        rows = _historical_rows()
        manifest = _manifest(rows)
        del manifest["universe"]
        manifest["manifest_hash"] = row_hash({
            key: value for key, value in manifest.items()
            if key != "manifest_hash"
        })
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                write_m7_historical_artifact(
                    artifacts_dir=tmp,
                    quote_rows=rows,
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    allow_reviewed_artifact=True,
                )

        self.assertIn("input_manifest.universe", str(ctx.exception))

    def test_manifest_universe_must_include_symbol(self):
        rows = _historical_rows()
        manifest = _manifest(rows)
        manifest["universe"] = {
            **manifest["universe"],
            "symbols": ["MSFT"],
        }
        manifest["manifest_hash"] = row_hash({
            key: value for key, value in manifest.items()
            if key != "manifest_hash"
        })
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                write_m7_historical_artifact(
                    artifacts_dir=tmp,
                    quote_rows=rows,
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    allow_reviewed_artifact=True,
                )

        self.assertIn("input_manifest.universe.symbols", str(ctx.exception))

    def test_zero_size_quote_rows_are_rejected_before_snapshot(self):
        rows = list(_historical_rows())
        rows[0] = {**rows[0], "bid_sz": "0"}
        manifest = _manifest(rows)
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                write_m7_historical_artifact(
                    artifacts_dir=tmp,
                    quote_rows=rows,
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    allow_reviewed_artifact=True,
                )

        self.assertIn("bid_sz", str(ctx.exception))

    def test_quote_rows_with_receive_before_event_are_rejected(self):
        rows = list(_historical_rows())
        rows[0] = {
            **rows[0],
            "ts_event_utc": "2026-06-01T14:30:00.000000Z",
            "ts_recv_utc": "2026-06-01T14:29:59.999999Z",
        }
        manifest = _manifest(rows)
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                write_m7_historical_artifact(
                    artifacts_dir=tmp,
                    quote_rows=rows,
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    allow_reviewed_artifact=True,
                )

        self.assertIn("ts_recv_utc must be >= ts_event_utc", str(ctx.exception))

    def test_runner_does_not_use_half_gross_benchmark_proxy(self):
        rows = _historical_rows()
        manifest = _manifest(rows)

        result = run_historical_backtest(
            quote_rows=rows,
            symbol="AAPL",
            instrument_id=1001,
            rules_hash=_RULES_HASH,
            data_pin=_data_pin(manifest),
            dataset="EQUS.MINI",
            schema="tbbo",
            input_manifest=manifest,
        )

        self.assertGreater(len(result.trades), 0)
        trade = result.trades[0]
        self.assertNotEqual(
            trade.benchmark_pnl_usd,
            (trade.gross_modeled_usd / Decimal("2")).quantize(Decimal("0.000001")),
        )

    def test_historical_production_write_requires_reviewed_flag(self):
        with TemporaryDirectory() as tmp:
            production = Path(tmp) / "artifacts" / "backtests"
            production.mkdir(parents=True)
            rows = _historical_rows()
            manifest = _manifest(rows)

            with self.assertRaises(HistoricalArtifactWriteRefused):
                write_m7_historical_artifact(
                    artifacts_dir=production,
                    quote_rows=rows,
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    allow_reviewed_artifact=False,
                    production_artifacts_dir=production,
                )

    def test_nested_production_write_is_refused_even_with_reviewed_flag(self):
        with TemporaryDirectory() as tmp:
            production = Path(tmp) / "artifacts" / "backtests"
            nested = production / "reviewed"
            rows = _historical_rows()
            manifest = _manifest(rows)

            with self.assertRaises(HistoricalArtifactWriteRefused):
                write_m7_historical_artifact(
                    artifacts_dir=nested,
                    quote_rows=rows,
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    allow_reviewed_artifact=True,
                    production_artifacts_dir=production,
                )

            self.assertFalse((nested / f"{_STRATEGY_ID}.json").exists())

    def test_historical_diagnostic_export_is_bounded_and_omits_raw_quotes(self):
        with TemporaryDirectory() as tmp:
            rows = _historical_rows()
            manifest = _manifest(rows)
            diagnostics_path = Path(tmp) / "diagnostics" / "aapl.json"
            with patch("agent.backtest_historical.run_historical_backtest",
                       return_value=_diagnostic_backtest_result()):
                result = write_m7_historical_diagnostic_export(
                    output_path=diagnostics_path,
                    quote_rows=rows,
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    strategy_id=_STRATEGY_ID_V2,
                    max_trade_rows=1,
                    max_skip_rows=1,
                )

            self.assertEqual(result.output_path, diagnostics_path)
            self.assertTrue(diagnostics_path.exists())
            self.assertFalse((Path(tmp) / f"{_STRATEGY_ID_V2}.json").exists())

            payload = result.payload
            self.assertEqual(payload["kind"], "m7_historical_diagnostic_export")
            self.assertEqual(payload["strategy_id"], _STRATEGY_ID_V2)
            self.assertEqual(payload["manifest"]["manifest_hash"],
                             manifest["manifest_hash"])
            self.assertEqual(payload["summary"]["trade_count"], 1)
            self.assertEqual(payload["summary"]["skip_count"], 1)
            self.assertEqual(payload["skip_reason_counts"],
                             {"latency_lost_edge": 1})
            self.assertEqual(payload["trade_rows"][0]["trade_bps"], "22.800000")
            self.assertEqual(payload["skip_rows"][0]["reason"],
                             "latency_lost_edge")
            self.assertEqual(payload["trade_rows"][0]["entry_spread_bps"],
                             "2.000000")
            self.assertEqual(payload["trade_rows"][0]["notional_usd"],
                             "1000.000000")
            self.assertEqual(payload["distributions"]["entry_spread_bps"]["p95"],
                             "2.000000")
            self.assertIn("entry_spread_quintile",
                          payload["friction_buckets"])
            self.assertIn("entry_utc_hour", payload["time_buckets"])
            self.assertFalse(payload["trade_rows_truncated"])
            self.assertFalse(payload["skip_rows_truncated"])

            raw_output = diagnostics_path.read_text(encoding="utf-8")
            self.assertNotIn("bid_px", raw_output)
            self.assertNotIn("ask_px", raw_output)
            self.assertNotIn("quote_rows", raw_output)

    def test_historical_diagnostic_export_refuses_production_artifact_dir(self):
        with TemporaryDirectory() as tmp:
            production = Path(tmp) / "artifacts" / "backtests"
            rows = _historical_rows()
            manifest = _manifest(rows)

            with self.assertRaises(HistoricalArtifactWriteRefused):
                write_m7_historical_diagnostic_export(
                    output_path=production / "AAPL.json",
                    quote_rows=rows,
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    production_artifacts_dir=production,
                )

            self.assertFalse((production / "AAPL.json").exists())

    def test_historical_diagnostic_index_summarizes_cross_symbol_exports(self):
        with TemporaryDirectory() as tmp:
            rows = _historical_rows()
            manifest = _manifest(rows)
            diagnostics_path = Path(tmp) / "diagnostics" / "aapl.json"
            index_path = Path(tmp) / "diagnostics" / "index.json"
            with patch("agent.backtest_historical.run_historical_backtest",
                       return_value=_diagnostic_backtest_result()):
                write_m7_historical_diagnostic_export(
                    output_path=diagnostics_path,
                    quote_rows=rows,
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    strategy_id=_STRATEGY_ID_V2,
                    max_trade_rows=10,
                    max_skip_rows=10,
                )

            result = write_m7_historical_diagnostic_index(
                output_path=index_path,
                diagnostic_paths=(diagnostics_path,),
                created_utc="2026-06-13T12:01:00.000000Z",
                source_run_id="unit-diagnostic-run",
                holdout_window_id="unit-holdout",
            )

            self.assertTrue(index_path.exists())
            payload = result.payload
            self.assertEqual(payload["kind"], "m7_historical_diagnostic_index")
            self.assertEqual(payload["strategy_id"], _STRATEGY_ID_V2)
            self.assertEqual(payload["aggregate"]["trade_count"], 1)
            self.assertEqual(payload["aggregate"]["net_execution_realistic_pnl_usd"],
                             "2.280000")
            self.assertEqual(
                payload["concentration"]["symbols_with_positive_net_pnl"], 1)
            self.assertEqual(
                payload["concentration"]["top_1_symbol_trade_share"],
                "1.000000")
            self.assertEqual(payload["bounded_export"]["any_trade_rows_truncated"],
                             False)
            self.assertIn("entry_spread_quintile",
                          payload["friction_buckets"])


class TestHistoricalArtifactCli(unittest.TestCase):
    def test_cli_writes_reviewed_historical_artifact(self):
        with TemporaryDirectory() as tmp:
            quotes = Path(tmp) / "quotes.jsonl"
            rows = _historical_rows()
            manifest = _manifest(rows)
            manifest_path = Path(tmp) / "manifest.json"
            _write_jsonl(quotes, rows)
            manifest_path.write_text(dumps(manifest), encoding="utf-8")
            with patch("agent.backtest_historical.run_historical_backtest",
                       return_value=_passing_backtest_result()):
                rc = main([
                    "m7-historical-artifact",
                    "--quotes-jsonl", str(quotes),
                    "--input-manifest-json", str(manifest_path),
                    "--artifacts-dir", tmp,
                    "--symbol", "AAPL",
                    "--instrument-id", "1001",
                    "--rules-hash", _RULES_HASH,
                    "--data-pin", _data_pin(manifest),
                    "--created-utc", "2026-06-13T12:00:00.000000Z",
                    "--builder-git-commit", "test-commit",
                    "--strategy-id", _STRATEGY_ID_V2,
                    "--allow-reviewed-artifact",
                ])

            self.assertEqual(rc, 0)
            self.assertEqual(
                verify_artifact(
                    _STRATEGY_ID_V2,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    artifacts_dir=tmp,
                ).status,
                "ok",
            )

    def test_cli_writes_bounded_historical_diagnostics(self):
        with TemporaryDirectory() as tmp:
            quotes = Path(tmp) / "quotes.jsonl"
            rows = _historical_rows()
            manifest = _manifest(rows)
            manifest_path = Path(tmp) / "manifest.json"
            diagnostics_path = Path(tmp) / "diagnostics" / "aapl.json"
            _write_jsonl(quotes, rows)
            manifest_path.write_text(dumps(manifest), encoding="utf-8")
            with patch("agent.backtest_historical.run_historical_backtest",
                       return_value=_diagnostic_backtest_result()):
                rc = main([
                    "m7-historical-diagnostics",
                    "--quotes-jsonl", str(quotes),
                    "--input-manifest-json", str(manifest_path),
                    "--output-path", str(diagnostics_path),
                    "--symbol", "AAPL",
                    "--instrument-id", "1001",
                    "--rules-hash", _RULES_HASH,
                    "--data-pin", _data_pin(manifest),
                    "--created-utc", "2026-06-13T12:00:00.000000Z",
                    "--builder-git-commit", "test-commit",
                    "--strategy-id", _STRATEGY_ID_V2,
                    "--max-trade-rows", "1",
                    "--max-skip-rows", "1",
                ])

            self.assertEqual(rc, 0)
            self.assertTrue(diagnostics_path.exists())
            raw_output = diagnostics_path.read_text(encoding="utf-8")
            self.assertIn("m7_historical_diagnostic_export", raw_output)
            self.assertNotIn("bid_px", raw_output)
            self.assertNotIn("ask_px", raw_output)

    def test_cli_writes_bounded_historical_diagnostics_index(self):
        with TemporaryDirectory() as tmp:
            rows = _historical_rows()
            manifest = _manifest(rows)
            diagnostics_path = Path(tmp) / "diagnostics" / "aapl.json"
            index_path = Path(tmp) / "diagnostics" / "index.json"
            with patch("agent.backtest_historical.run_historical_backtest",
                       return_value=_diagnostic_backtest_result()):
                write_m7_historical_diagnostic_export(
                    output_path=diagnostics_path,
                    quote_rows=rows,
                    symbol="AAPL",
                    instrument_id=1001,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    created_utc="2026-06-13T12:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    strategy_id=_STRATEGY_ID_V2,
                )

            rc = main([
                "m7-historical-diagnostics-index",
                "--output-path", str(index_path),
                "--diagnostics-path", str(diagnostics_path),
                "--created-utc", "2026-06-13T12:01:00.000000Z",
                "--source-run-id", "unit-diagnostic-run",
                "--holdout-window-id", "unit-holdout",
            ])

            self.assertEqual(rc, 0)
            self.assertTrue(index_path.exists())
            raw_output = index_path.read_text(encoding="utf-8")
            self.assertIn("m7_historical_diagnostic_index", raw_output)
            self.assertIn("concentration", raw_output)


if __name__ == "__main__":
    unittest.main()
