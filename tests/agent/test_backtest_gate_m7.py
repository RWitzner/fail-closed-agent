"""M7 Wave 1 - v2 backtest-artifact gate tests.

These tests extend the M5 verifier without weakening the existing v1 matrix in
test_synthetic_isolation.py. Builders use a mandatory temp artifacts_dir; the
committed production dir is not written.
"""
import json
import os
import unittest
from tempfile import TemporaryDirectory

from agent.backtest_gate import ArtifactCheck, verify_artifact
from agent.serializer import row_hash

_STRATEGY_ID = "directional.momentum_v1"
_RULES_HASH = "rh-m7"
_DATA_PIN = "EQUS.MINI:tbbo:1m:fixture:m7-v1"


def _v2_metrics(**overrides):
    metrics = {
        "basis": "execution_realistic_pnl",
        "pass": True,
        "runner_version": "m7-backtest-v1",
        "strategy_version": _STRATEGY_ID,
        "sample": {
            "start_utc": "2026-06-01T13:30:00.000000Z",
            "end_utc": "2026-06-30T20:00:00.000000Z",
            "session_count": 20,
            "decision_count": 250,
            "trade_count": 42,
            "traded_session_count": 6,
            "symbols": ["AAPL"],
        },
        "pnl": {
            "gross_modeled_usd": "125.00",
            "fees_usd": "7.50",
            "net_execution_realistic_pnl_usd": "117.50",
            "avg_trade_bps": "3.10",
            "profit_factor": "1.25",
        },
        "benchmark": {
            "method": "exposure_matched_midbar_v1",
            "benchmark_pnl_usd": "12.00",
            "active_pnl_usd": "105.50",
        },
        "risk": {
            "max_drawdown_usd": "25.00",
            "max_drawdown_pct_allocated": "0.0100",
            "worst_day_usd": "-12.00",
            "worst_day_pct_allocated": "0.0075",
            "p95_realism_gap_bps": "10.00",
            "max_single_fill_divergence_bps": "40.00",
        },
        "quality": {
            "future_receipt_count": 0,
            "missing_bar_count": 1,
            "ca_blackout_skips": 0,
            "data_quality_skip_count": 2,
            "unresolved_reconcile_drift_count": 0,
            "s1_canary_breach_count": 0,
            "live_broker_submit_count": 0,
            "artifact_mismatch_count": 0,
            "unhandled_exception_count": 0,
        },
        "thresholds": {
            "min_sessions": 20,
            "min_trades": 30,
            "min_traded_sessions": 5,
            "require_positive_net_pnl": True,
            "require_positive_active_pnl": True,
            "profit_factor_min": "1.10",
            "max_drawdown_pct_allocated": "0.0150",
            "worst_day_pct_allocated": "0.0075",
            "p95_realism_gap_bps_max": "15",
            "max_single_fill_divergence_bps": "50",
        },
        "provenance": {
            "input_manifest_hash": "mh-abc123",
            "builder_git_commit": "test",
            "tier": "fixture",
        },
    }
    metrics.update(overrides)
    return metrics


def _artifact_payload(**overrides):
    body = {
        "v": 2,
        "strategy_id": _STRATEGY_ID,
        "rules_hash": _RULES_HASH,
        "data_pin": _DATA_PIN,
        "metrics": _v2_metrics(),
        "created_utc": "2026-06-13T00:00:00.000000Z",
    }
    body.update(overrides)
    payload = dict(body)
    payload["artifact_hash"] = row_hash(body)
    return payload


def _write_artifact(artifacts_dir, payload):
    path = os.path.join(artifacts_dir, payload["strategy_id"] + ".json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


class TestBacktestGateV2(unittest.TestCase):
    def _verify(self, artifacts_dir, *, strategy_id=_STRATEGY_ID,
                rules_hash=_RULES_HASH, data_pin=_DATA_PIN):
        return verify_artifact(strategy_id, rules_hash=rules_hash,
                               data_pin=data_pin, artifacts_dir=artifacts_dir)

    def test_valid_v2_artifact_is_ok(self):
        with TemporaryDirectory() as tmp:
            payload = _artifact_payload()
            path = _write_artifact(tmp, payload)

            self.assertEqual(self._verify(tmp), ArtifactCheck(
                status="ok", artifact_path=path,
                artifact_hash=payload["artifact_hash"]))

    def test_v2_pass_false_is_hash_invalid_even_with_valid_hash(self):
        with TemporaryDirectory() as tmp:
            payload = _artifact_payload(metrics=dict(_v2_metrics(), **{"pass": False}))
            _write_artifact(tmp, payload)

            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_v2_wrong_basis_is_hash_invalid(self):
        with TemporaryDirectory() as tmp:
            payload = _artifact_payload(
                metrics=_v2_metrics(basis="raw_broker_pnl"))
            _write_artifact(tmp, payload)

            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_v2_missing_required_metric_key_is_hash_invalid(self):
        with TemporaryDirectory() as tmp:
            metrics = _v2_metrics()
            del metrics["quality"]
            _write_artifact(tmp, _artifact_payload(metrics=metrics))

            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_v2_decimal_metric_must_be_string(self):
        with TemporaryDirectory() as tmp:
            metrics = _v2_metrics()
            metrics["pnl"] = dict(metrics["pnl"],
                                  net_execution_realistic_pnl_usd=117.50)
            raw = _artifact_payload()
            raw["metrics"] = metrics
            # Cannot row_hash float content; stale hash is enough to prove the
            # verifier degrades instead of raising.
            _write_artifact(tmp, raw)

            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_v2_threshold_decimal_must_be_string(self):
        with TemporaryDirectory() as tmp:
            metrics = _v2_metrics()
            metrics["thresholds"] = dict(
                metrics["thresholds"],
                max_single_fill_divergence_bps=50.0,
            )
            raw = _artifact_payload()
            raw["metrics"] = metrics

            _write_artifact(tmp, raw)

            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_v2_unknown_version_is_hash_invalid(self):
        with TemporaryDirectory() as tmp:
            _write_artifact(tmp, _artifact_payload(v=99))

            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_v2_key_mismatch_still_distinct_after_metric_validation(self):
        with TemporaryDirectory() as tmp:
            _write_artifact(tmp, _artifact_payload())

            self.assertEqual(
                self._verify(tmp, data_pin="EQUS.MINI:tbbo:1m:other").status,
                "key_mismatch")
