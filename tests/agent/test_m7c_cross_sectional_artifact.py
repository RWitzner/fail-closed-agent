"""M7c phase-1 multi-symbol cross-sectional reviewed-artifact flow (packet step 5).

This is the production-artifact-writer + multi-symbol manifest + CLI plumbing on top
of the already-built cross-sectional harness (``run_historical_cross_sectional_backtest``).
It mirrors the single-symbol ``write_m7_historical_artifact`` contract but binds a
multi-symbol manifest: ONE hash-bound manifest declares the predeclared universe block
plus a per-symbol data binding (instrument_id, row_count, quote_rows_sha256). Each
symbol's ``data_pin`` is DERIVED from the manifest hash (never stored in the body — that
would be circular), exactly like the single-symbol pin. The two long legs aggregate under
one ``(strategy_id, rules_hash, data_pin)`` artifact (FD-P1-10) and the artifact carries
the ``universe_equal_weight_long_v1`` attribution as provenance (FD-P1-9); the pinned
verifier benchmark stays ``exposure_matched_midbar_v1``.

Like the single-symbol tests, the criteria/write/provenance path is exercised with the
harness PATCHED to a deterministic passing result (a real 20-session/30-trade pass needs
the credentialed clean-window run). Manifest-validation failures raise before the harness
runs, so those tests need no patch; one un-patched single-session run exercises the real
wiring end to end (it fails the 20-session gate, which is the point).
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent.__main__ import main
from agent.backtest_engine import BacktestSkip, BacktestTrade
from agent.backtest_gate import verify_artifact
from agent.backtest_historical import (
    HistoricalArtifactWriteRefused,
    HistoricalCrossSectionalArtifactBuildResult,
    HistoricalCrossSectionalResult,
    validate_historical_cross_sectional_manifest,
    write_m7_historical_cross_sectional_artifact,
)
from agent.serializer import dumps, row_hash
from agent.strategies.relative_strength import STRATEGY_ID as RS_STRATEGY_ID

_RULES_HASH = "rh-m7c-xs-artifact"
_MOMENTUM_ID = "directional.momentum_v1"
_HYPOTHESIS_ID = "m7c_relative_strength_market_neutral_v0_20260613"
_SELECTION_RULE = (
    "Reuse the full ordered M7 broad universe before relative-strength metrics; "
    "no symbol additions or removals from failed v1/v2 diagnostics.")
UNIVERSE = ("AAPL", "MSFT", "NVDA", "AMZN", "META",
            "GOOGL", "TSLA", "AVGO", "COST", "NFLX")
INSTRUMENT_IDS = {sym: 1001 + i for i, sym in enumerate(UNIVERSE)}
SESSION = "2026-06-15"
_WIGGLE = Decimal("0.0010")


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _rows_for_symbol(symbol, *, n_minutes, slope: Decimal, session=SESSION,
                     base: Decimal = Decimal("100.0000"), start_hhmm="14:30:00"):
    rows = []
    start = datetime.fromisoformat(f"{session}T{start_hhmm}+00:00")
    iid = INSTRUMENT_IDS[symbol]
    seq = 1
    for minute in range(n_minutes):
        ts_event = start + timedelta(minutes=minute)
        ts_recv = ts_event + timedelta(milliseconds=300)
        mid = base + slope * Decimal(minute) + (_WIGGLE if minute % 2 else Decimal("0"))
        rows.append({
            "dataset": "EQUS.MINI",
            "schema": "tbbo",
            "symbol": symbol,
            "instrument_id": iid,
            "vendor_seq": seq,
            "ts_event_utc": _utc(ts_event),
            "ts_recv_utc": _utc(ts_recv),
            "bid_px": f"{mid - Decimal('0.01'):.6f}",
            "bid_sz": "100",
            "ask_px": f"{mid + Decimal('0.01'):.6f}",
            "ask_sz": "100",
            "reconnect_epoch": 0,
        })
        seq += 1
    return rows


def _universe_rows(n_minutes=4):
    return {
        sym: _rows_for_symbol(
            sym, n_minutes=n_minutes,
            slope=Decimal(len(UNIVERSE) - i) * Decimal("0.0010"))
        for i, sym in enumerate(UNIVERSE)
    }


def _quote_rows_hash(rows):
    import hashlib

    hasher = hashlib.sha256()
    for row in rows:
        hasher.update(dumps(row).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _manifest(symbol_rows, *, universe=UNIVERSE, hypothesis_id=_HYPOTHESIS_ID,
              selection_rule=_SELECTION_RULE, blackouts=(), horizon="30m"):
    sessions = sorted({
        row["ts_event_utc"][:10]
        for rows in symbol_rows.values() for row in rows
    })
    body = {
        "v": 1,
        "dataset": "EQUS.MINI",
        "schema": "tbbo",
        "interval": "1m",
        "source_id_prefix": "historical",
        "normalizer_id": "m7-historical-normalized-quotes-v1",
        "raw_source": "unit-test",
        "universe": {
            "hypothesis_id": hypothesis_id,
            "selection_rule": selection_rule,
            "symbols": list(universe),
        },
        "symbols": {
            sym: {
                "instrument_id": INSTRUMENT_IDS[sym],
                "row_count": len(rows),
                "quote_rows_sha256": _quote_rows_hash(rows),
            }
            for sym, rows in symbol_rows.items()
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
            "horizon": horizon,
            "fee_model_version": "reg_fees_v1",
            "pricing_model_version": "m7-historical-quote-a-b-spread-v1",
            "realism_gap_model_version": "historical_quote_model_vs_raw_mid_v1",
        },
    }
    body["manifest_hash"] = row_hash({
        key: value for key, value in body.items() if key != "manifest_hash"
    })
    return body


def _rehash(manifest):
    manifest["manifest_hash"] = row_hash({
        key: value for key, value in manifest.items() if key != "manifest_hash"
    })
    return manifest


def _data_pin(manifest):
    return (
        f"{manifest['dataset']}:{manifest['schema']}:{manifest['interval']}:"
        f"{manifest['source_id_prefix']}:{manifest['manifest_hash']}"
    )


def _passing_xs_result():
    """A deterministic passing cross-sectional result: 30 winning legs across two held
    names (AAPL, MSFT) and 20 sessions, with an equal-weight-long benchmark net below
    the strategy net so both the exposure-matched and equal-weight active PnL are > 0."""
    dates = (
        "2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04",
        "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10",
        "2026-06-11", "2026-06-12", "2026-06-15", "2026-06-16",
        "2026-06-17", "2026-06-18", "2026-06-19", "2026-06-22",
        "2026-06-23", "2026-06-24", "2026-06-25", "2026-06-26",
    )
    symbols = ("AAPL", "MSFT")
    trades = tuple(
        BacktestTrade(
            symbol=symbols[index % 2],
            instrument_id=1001 + (index % 2),
            qty=Decimal("10"),
            entry_bar_end_utc=f"{dates[index % len(dates)]}T14:31:00.000000Z",
            exit_bar_end_utc=f"{dates[index % len(dates)]}T15:01:00.000000Z",
            entry_mid=Decimal("100.000000"),
            exit_mid=Decimal("100.500000"),
            gross_modeled_usd=Decimal("5.000000"),
            fees_usd=Decimal("0.020000"),
            net_execution_realistic_pnl_usd=Decimal("4.980000"),
            benchmark_pnl_usd=Decimal("1.000000"),
        )
        for index in range(30)
    )
    net = sum((t.net_execution_realistic_pnl_usd for t in trades), Decimal("0"))
    benchmark_net = Decimal("60.000000")
    return HistoricalCrossSectionalResult(
        trades=trades,
        skips=(),
        bar_count=2600,
        candidate_count=40,
        p95_realism_gap_bps=Decimal("4.000000"),
        max_single_fill_divergence_bps=Decimal("8.000000"),
        ca_blackout_skip_count=0,
        data_quality_skip_count=0,
        decision_count=200,
        acting_decision_count=20,
        insufficient_valid_decision_count=5,
        overlap_suppressed_leg_count=10,
        exclusion_reason_counts={"features_unavailable": 3},
        per_symbol_leg_counts={"AAPL": 15, "MSFT": 15},
        benchmark_leg_fill_count=180,
        benchmark_leg_skip_count=2,
        equal_weight_long_benchmark_net_usd=benchmark_net,
        equal_weight_long_active_pnl_usd=(net - benchmark_net).quantize(
            Decimal("0.000001")),
    )


class TestCrossSectionalManifestValidation(unittest.TestCase):
    def setUp(self):
        self.rows = _universe_rows()
        self.manifest = _manifest(self.rows)

    def _validate(self, manifest=None, rows=None, data_pin=None):
        manifest = self.manifest if manifest is None else manifest
        rows = self.rows if rows is None else rows
        data_pin = _data_pin(manifest) if data_pin is None else data_pin
        return validate_historical_cross_sectional_manifest(
            manifest, symbol_quote_rows=rows, dataset="EQUS.MINI",
            schema="tbbo", data_pin=data_pin)

    def test_happy_path_returns_parsed_manifest(self):
        parsed = self._validate()
        self.assertEqual(parsed.manifest_hash, self.manifest["manifest_hash"])
        self.assertEqual(parsed.universe_symbols, UNIVERSE)
        self.assertEqual(parsed.universe_hypothesis_id, _HYPOTHESIS_ID)
        self.assertEqual(set(parsed.instrument_ids), set(UNIVERSE))
        self.assertEqual(parsed.instrument_ids["AAPL"], 1001)
        # Per-symbol pins are DERIVED from the manifest hash + symbol (never stored).
        self.assertEqual(
            parsed.symbol_data_pins["AAPL"],
            f"{_data_pin(self.manifest)}:AAPL")
        self.assertEqual(parsed.latency_budget_ms, 250)
        self.assertEqual(parsed.slippage_cap_bps, Decimal("25"))
        self.assertEqual(parsed.horizon, "30m")

    def test_manifest_hash_mismatch_is_rejected(self):
        bad = dict(self.manifest, manifest_hash="0" * 64)
        with self.assertRaises(ValueError) as ctx:
            self._validate(manifest=bad)
        self.assertIn("manifest_hash", str(ctx.exception))

    def test_top_level_data_pin_must_bind_manifest_hash(self):
        with self.assertRaises(ValueError) as ctx:
            self._validate(data_pin="EQUS.MINI:tbbo:1m:historical:not-the-manifest")
        self.assertIn("data_pin", str(ctx.exception))

    def test_per_symbol_quote_hash_must_match_rows(self):
        bad = _rehash({
            **self.manifest,
            "symbols": {
                **self.manifest["symbols"],
                "MSFT": {**self.manifest["symbols"]["MSFT"],
                         "quote_rows_sha256": "0" * 64},
            },
        })
        with self.assertRaises(ValueError) as ctx:
            self._validate(manifest=bad)
        self.assertIn("quote_rows_sha256", str(ctx.exception))

    def test_per_symbol_row_count_must_match_rows(self):
        bad = _rehash({
            **self.manifest,
            "symbols": {
                **self.manifest["symbols"],
                "NVDA": {**self.manifest["symbols"]["NVDA"], "row_count": 999},
            },
        })
        with self.assertRaises(ValueError) as ctx:
            self._validate(manifest=bad)
        self.assertIn("row_count", str(ctx.exception))

    def test_symbols_block_must_cover_exactly_the_universe(self):
        dropped = dict(self.manifest["symbols"])
        del dropped["NFLX"]
        bad = _rehash({**self.manifest, "symbols": dropped})
        with self.assertRaises(ValueError) as ctx:
            self._validate(manifest=bad)
        self.assertIn("symbols", str(ctx.exception))

    def test_quote_rows_keys_must_match_universe(self):
        rows = dict(self.rows)
        del rows["COST"]
        with self.assertRaises(ValueError) as ctx:
            self._validate(rows=rows)
        self.assertIn("COST", str(ctx.exception))

    def test_normalizer_pin_is_enforced(self):
        bad = _rehash({**self.manifest, "normalizer_id": "some-other-normalizer"})
        with self.assertRaises(ValueError) as ctx:
            self._validate(manifest=bad)
        self.assertIn("normalizer_id", str(ctx.exception))

    def test_execution_model_versions_are_pinned(self):
        bad = _rehash({
            **self.manifest,
            "execution": {**self.manifest["execution"],
                          "pricing_model_version": "tampered"},
        })
        with self.assertRaises(ValueError) as ctx:
            self._validate(manifest=bad)
        self.assertIn("pricing_model_version", str(ctx.exception))

    def test_latency_floor_is_enforced(self):
        bad = _rehash({
            **self.manifest,
            "execution": {**self.manifest["execution"], "latency_budget_ms": 100},
        })
        with self.assertRaises(ValueError) as ctx:
            self._validate(manifest=bad)
        self.assertIn("latency_budget_ms", str(ctx.exception))

    def test_universe_block_is_required(self):
        bad = dict(self.manifest)
        del bad["universe"]
        bad = _rehash(bad)
        with self.assertRaises(ValueError) as ctx:
            self._validate(manifest=bad)
        self.assertIn("input_manifest.universe", str(ctx.exception))

    def test_instrument_id_bool_is_rejected(self):
        # bool is an int subclass; the validator must not accept True/False as an id.
        bad = _rehash({
            **self.manifest,
            "symbols": {
                **self.manifest["symbols"],
                "AAPL": {**self.manifest["symbols"]["AAPL"], "instrument_id": True},
            },
        })
        with self.assertRaises(ValueError) as ctx:
            self._validate(manifest=bad)
        self.assertIn("instrument_id must be an int", str(ctx.exception))

    def test_row_count_bool_is_rejected(self):
        bad = _rehash({
            **self.manifest,
            "symbols": {
                **self.manifest["symbols"],
                "AAPL": {**self.manifest["symbols"]["AAPL"], "row_count": True},
            },
        })
        with self.assertRaises(ValueError) as ctx:
            self._validate(manifest=bad)
        self.assertIn("row_count must be an int", str(ctx.exception))

    def test_duplicate_symbols_in_universe_are_rejected(self):
        universe = self.manifest["universe"]
        bad = _rehash({
            **self.manifest,
            "universe": {**universe, "symbols": list(universe["symbols"]) + ["AAPL"]},
        })
        with self.assertRaises(ValueError) as ctx:
            self._validate(manifest=bad)
        self.assertIn("must be unique", str(ctx.exception))

    def test_empty_symbols_block_is_rejected(self):
        bad = _rehash({**self.manifest, "symbols": {}})
        with self.assertRaises(ValueError) as ctx:
            self._validate(manifest=bad)
        self.assertIn("input_manifest.symbols", str(ctx.exception))

    def test_horizon_must_be_present_and_non_empty(self):
        execution = dict(self.manifest["execution"])
        del execution["horizon"]
        bad = _rehash({**self.manifest, "execution": execution})
        with self.assertRaises(ValueError) as ctx:
            self._validate(manifest=bad)
        self.assertIn("horizon", str(ctx.exception))


class TestCrossSectionalArtifactWriter(unittest.TestCase):
    def setUp(self):
        self.rows = _universe_rows()
        self.manifest = _manifest(self.rows)

    def _write(self, *, artifacts_dir, allow=True, strategy_id=RS_STRATEGY_ID,
               production_artifacts_dir=None, result=None):
        result = _passing_xs_result() if result is None else result
        with patch(
                "agent.backtest_historical.run_historical_cross_sectional_backtest",
                return_value=result):
            return write_m7_historical_cross_sectional_artifact(
                artifacts_dir=artifacts_dir,
                symbol_quote_rows=self.rows,
                rules_hash=_RULES_HASH,
                data_pin=_data_pin(self.manifest),
                created_utc="2026-06-26T20:00:00.000000Z",
                input_manifest=self.manifest,
                builder_git_commit="test-commit",
                allow_reviewed_artifact=allow,
                strategy_id=strategy_id,
                production_artifacts_dir=production_artifacts_dir,
            )

    def test_passing_write_produces_verifier_ok_artifact(self):
        with TemporaryDirectory() as tmp:
            result = self._write(artifacts_dir=tmp)
            self.assertIsInstance(
                result, HistoricalCrossSectionalArtifactBuildResult)
            self.assertTrue(result.criteria.passed)
            self.assertEqual(result.payload["strategy_id"], RS_STRATEGY_ID)
            self.assertEqual(result.artifact_path,
                             Path(tmp) / f"{RS_STRATEGY_ID}.json")
            provenance = result.payload["metrics"]["provenance"]
            self.assertEqual(provenance["tier"], "historical_reviewed")
            self.assertEqual(provenance["universe_hypothesis_id"], _HYPOTHESIS_ID)
            self.assertEqual(provenance["universe_symbols"], list(UNIVERSE))
            self.assertEqual(provenance["horizon"], "30m")
            # FD-P1-9: the equal-weight-long benchmark is carried as provenance, not as
            # the pinned verifier benchmark.
            self.assertEqual(
                provenance["universe_equal_weight_long_benchmark"],
                "universe_equal_weight_long_v1")
            self.assertEqual(
                provenance["universe_equal_weight_long_benchmark_pnl_usd"],
                "60.000000")
            # Breadth/leg diagnostics ride as one canonical-JSON string so the artifact
            # stays self-sufficient for the Phase Gate within the verifier's string-only
            # provenance schema.
            diagnostics = json.loads(provenance["cross_sectional_diagnostics"])
            self.assertEqual(diagnostics["per_symbol_leg_counts"],
                             {"AAPL": 15, "MSFT": 15})
            self.assertEqual(diagnostics["acting_decision_count"], 20)
            self.assertEqual(
                result.payload["metrics"]["benchmark"]["method"],
                "exposure_matched_midbar_v1")
            self.assertEqual(
                set(result.payload["metrics"]["sample"]["symbols"]),
                {"AAPL", "MSFT"})
            self.assertEqual(
                verify_artifact(RS_STRATEGY_ID, rules_hash=_RULES_HASH,
                                data_pin=_data_pin(self.manifest),
                                artifacts_dir=tmp).status,
                "ok")

    def test_only_relative_strength_proxy_strategy_id_is_accepted(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self._write(artifacts_dir=tmp, strategy_id=_MOMENTUM_ID)

    def test_passing_write_requires_reviewed_flag_even_outside_production(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(HistoricalArtifactWriteRefused):
                self._write(artifacts_dir=tmp, allow=False)
            self.assertFalse((Path(tmp) / f"{RS_STRATEGY_ID}.json").exists())

    def test_production_dir_write_requires_reviewed_flag(self):
        with TemporaryDirectory() as tmp:
            production = Path(tmp) / "artifacts" / "backtests"
            production.mkdir(parents=True)
            with self.assertRaises(HistoricalArtifactWriteRefused):
                self._write(artifacts_dir=production, allow=False,
                            production_artifacts_dir=production)

    def test_nested_production_dir_is_refused_even_with_flag(self):
        with TemporaryDirectory() as tmp:
            production = Path(tmp) / "artifacts" / "backtests"
            nested = production / "reviewed"
            with self.assertRaises(HistoricalArtifactWriteRefused):
                self._write(artifacts_dir=nested, allow=True,
                            production_artifacts_dir=production)
            self.assertFalse((nested / f"{RS_STRATEGY_ID}.json").exists())

    def test_real_single_session_run_fails_sample_gate_without_writing(self):
        # No patch: drive the real cross-sectional harness. A single session yields
        # AAPL/MSFT legs but fails the 20-session pinned gate -> criteria fail, no write.
        rows = _universe_rows(n_minutes=130)
        manifest = _manifest(rows)
        with TemporaryDirectory() as tmp:
            result = write_m7_historical_cross_sectional_artifact(
                artifacts_dir=tmp,
                symbol_quote_rows=rows,
                rules_hash=_RULES_HASH,
                data_pin=_data_pin(manifest),
                created_utc="2026-06-26T20:00:00.000000Z",
                input_manifest=manifest,
                builder_git_commit="test-commit",
                allow_reviewed_artifact=True,
            )
            self.assertFalse(result.criteria.passed)
            self.assertFalse((Path(tmp) / f"{RS_STRATEGY_ID}.json").exists())
            self.assertEqual({t.symbol for t in result.backtest.trades},
                             {"AAPL", "MSFT"})

    def test_manifest_horizon_flows_through_to_runner(self):
        # A horizon absent from the configured horizons must be rejected by the real
        # runner — proving the manifest horizon is passed through, not silently
        # defaulted to 30m.
        rows = _universe_rows(n_minutes=130)
        manifest = _manifest(rows, horizon="60m")
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                write_m7_historical_cross_sectional_artifact(
                    artifacts_dir=tmp,
                    symbol_quote_rows=rows,
                    rules_hash=_RULES_HASH,
                    data_pin=_data_pin(manifest),
                    created_utc="2026-06-26T20:00:00.000000Z",
                    input_manifest=manifest,
                    builder_git_commit="test-commit",
                    allow_reviewed_artifact=True,
                )
            self.assertIn("horizon", str(ctx.exception))
            self.assertFalse((Path(tmp) / f"{RS_STRATEGY_ID}.json").exists())


class TestCrossSectionalArtifactCli(unittest.TestCase):
    def test_cli_writes_reviewed_cross_sectional_artifact(self):
        rows = _universe_rows()
        manifest = _manifest(rows)
        with TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text(dumps(manifest), encoding="utf-8")
            argv = ["m7-historical-cross-sectional-artifact",
                    "--input-manifest-json", str(manifest_path),
                    "--artifacts-dir", tmp,
                    "--rules-hash", _RULES_HASH,
                    "--data-pin", _data_pin(manifest),
                    "--created-utc", "2026-06-26T20:00:00.000000Z",
                    "--builder-git-commit", "test-commit",
                    "--allow-reviewed-artifact"]
            for sym, sym_rows in rows.items():
                quotes = Path(tmp) / f"{sym}.jsonl"
                with open(quotes, "w", encoding="utf-8") as handle:
                    for row in sym_rows:
                        handle.write(dumps(row) + "\n")
                argv += ["--symbol-quotes", f"{sym}={quotes}"]
            with patch(
                    "agent.backtest_historical."
                    "run_historical_cross_sectional_backtest",
                    return_value=_passing_xs_result()):
                rc = main(argv)
            self.assertEqual(rc, 0)
            self.assertEqual(
                verify_artifact(RS_STRATEGY_ID, rules_hash=_RULES_HASH,
                                data_pin=_data_pin(manifest),
                                artifacts_dir=tmp).status,
                "ok")

    def test_cli_criteria_fail_returns_one_without_writing(self):
        rows = _universe_rows(n_minutes=130)
        manifest = _manifest(rows)
        with TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text(dumps(manifest), encoding="utf-8")
            argv = ["m7-historical-cross-sectional-artifact",
                    "--input-manifest-json", str(manifest_path),
                    "--artifacts-dir", tmp,
                    "--rules-hash", _RULES_HASH,
                    "--data-pin", _data_pin(manifest),
                    "--created-utc", "2026-06-26T20:00:00.000000Z",
                    "--allow-reviewed-artifact"]
            for sym, sym_rows in rows.items():
                quotes = Path(tmp) / f"{sym}.jsonl"
                with open(quotes, "w", encoding="utf-8") as handle:
                    for row in sym_rows:
                        handle.write(dumps(row) + "\n")
                argv += ["--symbol-quotes", f"{sym}={quotes}"]
            rc = main(argv)
            self.assertEqual(rc, 1)
            self.assertFalse((Path(tmp) / f"{RS_STRATEGY_ID}.json").exists())


if __name__ == "__main__":
    unittest.main()
