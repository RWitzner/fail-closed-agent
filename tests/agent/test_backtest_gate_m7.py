"""M7 Wave 1 - v2 backtest-artifact gate tests.

These tests extend the M5 verifier matrix in test_synthetic_isolation.py
(v2-only since the S9 hardening; v1 artifacts are rejected outright). Builders
use a mandatory temp artifacts_dir; the committed production dir is not
written.
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
            "universe_hypothesis_id": "unit-test-aapl-only-v1",
            "universe_selection_rule": "unit test fixture predeclares AAPL",
            "universe_symbols": ["AAPL"],
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


def _reviewed_runtime_metrics(config, strategy_id=_STRATEGY_ID):
    """Synthesized passing numbers, used only in temporary test artifacts."""
    from agent.execution_config import ExecutionConfig
    from agent.fees import FEE_MODEL_VERSION
    execution = ExecutionConfig.from_config(config)
    metrics = _v2_metrics(strategy_version=strategy_id)
    metrics["provenance"].update({
        "tier": "historical_reviewed",
        "latency_budget_ms": str(execution.effective_latency_budget_ms),
        "slippage_cap_bps": str(execution.slippage_cap_bps),
        "fee_model_version": FEE_MODEL_VERSION,
    })
    return metrics


class TestRuntimeBinding(unittest.TestCase):
    def setUp(self):
        from tests.lib.exec_fixtures import permissive_paper_fixture_config
        from agent.execution_config import ExecutionConfig
        from agent.signal_config import SignalConfig
        self.config = permissive_paper_fixture_config()
        self.runtime_hash = ExecutionConfig.from_config(self.config).rules_hash
        self.research_hash = SignalConfig.from_config(self.config["agent_rules"]).rules_hash
        self.live_pin = "EQUS.MINI:tbbo:1m:live"
        metrics = _reviewed_runtime_metrics(self.config)
        self.research = _artifact_payload(rules_hash=self.research_hash,
            data_pin="EQUS.MINI:tbbo:1m:historical:mh-abc123", metrics=metrics)

    def build(self, **overrides):
        from agent.runtime_artifact import build_runtime_artifact
        args = dict(research_artifact=self.research, config=self.config,
            live_data_pin=self.live_pin, created_utc="2026-09-05T00:00:00.000000Z")
        args.update(overrides)
        return build_runtime_artifact(**args)

    def verify(self, payload, **overrides):
        from agent.backtest_gate import verify_artifact_payload
        args = dict(strategy_id=_STRATEGY_ID, rules_hash=self.runtime_hash,
            data_pin=self.live_pin, runtime_config=self.config)
        args.update(overrides)
        return verify_artifact_payload(payload, **args).status

    @staticmethod
    def rehash(payload):
        payload["artifact_hash"] = row_hash({k: v for k, v in payload.items()
                                              if k != "artifact_hash"})

    def test_binding_preserves_historical_evidence_and_checks_both_config_hashes(self):
        original = json.dumps(self.research, sort_keys=True)
        payload = self.build()
        self.assertNotEqual(self.runtime_hash, self.research_hash)
        self.assertEqual(self.verify(payload), "ok")
        self.assertEqual(payload["research_artifact"], self.research)
        self.assertEqual(json.dumps(self.research, sort_keys=True), original)
        self.assertEqual(self.verify(payload, rules_hash="changed-risk-config"), "key_mismatch")
        self.assertEqual(self.verify(payload, runtime_config=None), "key_mismatch")
        payload["research_artifact"]["rules_hash"] = "changed-signal-config"
        self.rehash(payload["research_artifact"])
        self.rehash(payload)
        self.assertEqual(self.verify(payload), "key_mismatch")

    def test_binding_refuses_manifest_execution_drift_and_unresearched_symbols(self):
        for key, value in (("latency_budget_ms", "999"), ("slippage_cap_bps", "999"),
                           ("fee_model_version", "other-model"), ("latency_budget_ms", None)):
            with self.subTest(key=key, value=value):
                payload = self.build()
                provenance = payload["research_artifact"]["metrics"]["provenance"]
                if value is None:
                    provenance.pop(key)
                else:
                    provenance[key] = value
                self.rehash(payload["research_artifact"])
                self.rehash(payload)
                self.assertEqual(self.verify(payload), "key_mismatch")
                with self.assertRaises(ValueError):
                    self.build(research_artifact=payload["research_artifact"])
        payload = self.build()
        payload["research_artifact"]["metrics"]["sample"]["symbols"] = ["MSFT"]
        self.rehash(payload["research_artifact"])
        self.rehash(payload)
        self.assertEqual(self.verify(payload), "key_mismatch")

    def test_binding_refuses_vendor_schema_interval_and_manifest_drift(self):
        for pin in ("ALPACA.IEX:tbbo:1m:live", "EQUS.MINI:bbo-1s:1m:live",
                    "EQUS.MINI:tbbo:5m:live", "EQUS.MINI:tbbo:1m:replay"):
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                self.build(live_data_pin=pin)
        self.research["data_pin"] = "EQUS.MINI:tbbo:1m:historical:wrong-manifest"
        self.rehash(self.research)
        with self.assertRaises(ValueError):
            self.build()

    def test_rehashed_failed_research_still_cannot_pass(self):
        payload = self.build()
        payload["research_artifact"]["metrics"]["pnl"]["net_execution_realistic_pnl_usd"] = "-100"
        self.rehash(payload["research_artifact"])
        self.rehash(payload)
        self.assertEqual(self.verify(payload), "hash_invalid")
        with self.assertRaises(ValueError):
            self.build(research_artifact=payload["research_artifact"])

    def test_fixture_evidence_and_malformed_nested_payload_fail_closed(self):
        self.research["metrics"]["provenance"]["tier"] = "fixture"
        self.rehash(self.research)
        with self.assertRaises(ValueError):
            self.build()
        self.setUp()
        for value in ([], ["bad"], None, "bad"):
            payload = self.build()
            payload["research_artifact"]["metrics"] = value
            self.rehash(payload["research_artifact"])
            self.rehash(payload)
            self.assertEqual(self.verify(payload), "hash_invalid")
        for key in ("strategy_id", "rules_hash", "data_pin", "created_utc"):
            payload = self.build()
            payload["research_artifact"][key] = ["malformed"]
            self.rehash(payload["research_artifact"])
            self.rehash(payload)
            self.assertNotEqual(self.verify(payload), "ok")

    def test_writer_requires_explicit_review_and_never_overwrites(self):
        from pathlib import Path
        from agent.runtime_artifact import write_runtime_artifact
        with TemporaryDirectory() as tmp:
            args = dict(artifacts_dir=tmp, research_artifact=self.research,
                config=self.config, live_data_pin=self.live_pin, created_utc="2026-09-05T00:00:00Z")
            with self.assertRaises(ValueError):
                write_runtime_artifact(**args)
            self.assertEqual(list(Path(tmp).iterdir()), [])
            path = write_runtime_artifact(**args, allow_reviewed_runtime=True)
            self.assertEqual(verify_artifact(_STRATEGY_ID, rules_hash=self.runtime_hash,
                data_pin=self.live_pin, artifacts_dir=tmp,
                runtime_config=self.config).status, "ok")
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                write_runtime_artifact(**args, allow_reviewed_runtime=True)
            self.assertEqual(path.read_bytes(), before)


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

    def test_v2_hash_valid_claimed_pass_rejects_failed_actual_metrics(self):
        with TemporaryDirectory() as tmp:
            metrics = _v2_metrics()
            metrics["pnl"] = dict(
                metrics["pnl"],
                net_execution_realistic_pnl_usd="-999.00",
                avg_trade_bps="-10.00",
                profit_factor="0.10",
            )
            metrics["benchmark"] = dict(
                metrics["benchmark"], active_pnl_usd="-888.00")
            _write_artifact(tmp, _artifact_payload(metrics=metrics))

            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_v2_hash_valid_claimed_pass_rejects_quality_breach(self):
        with TemporaryDirectory() as tmp:
            metrics = _v2_metrics()
            metrics["quality"] = dict(
                metrics["quality"], s1_canary_breach_count=1)
            _write_artifact(tmp, _artifact_payload(metrics=metrics))

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

    def test_v2_thresholds_cannot_be_weakened_below_pinned_paper_gate(self):
        cases = (
            {"min_sessions": 19},
            {"min_trades": 29},
            {"min_traded_sessions": 4},
            {"require_positive_net_pnl": False},
            {"require_positive_active_pnl": False},
            {"profit_factor_min": "1.09"},
            {"max_drawdown_pct_allocated": "0.0151"},
            {"worst_day_pct_allocated": "0.0076"},
            {"p95_realism_gap_bps_max": "15.01"},
            {"max_single_fill_divergence_bps": "50.01"},
        )
        for weakened in cases:
            with self.subTest(weakened=weakened), TemporaryDirectory() as tmp:
                metrics = _v2_metrics()
                metrics["thresholds"] = dict(metrics["thresholds"], **weakened)
                _write_artifact(tmp, _artifact_payload(metrics=metrics))

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

    def test_v2_key_mismatch_precedes_semantic_failure(self):
        with TemporaryDirectory() as tmp:
            metrics = _v2_metrics()
            metrics["quality"] = dict(
                metrics["quality"], s1_canary_breach_count=1)
            _write_artifact(tmp, _artifact_payload(metrics=metrics))

            self.assertEqual(
                self._verify(tmp, data_pin="EQUS.MINI:tbbo:1m:other").status,
                "key_mismatch")

    def test_v2_universe_provenance_symbols_must_be_string_list(self):
        with TemporaryDirectory() as tmp:
            metrics = _v2_metrics()
            metrics["provenance"] = dict(
                metrics["provenance"],
                universe_symbols="AAPL",
            )
            _write_artifact(tmp, _artifact_payload(metrics=metrics))

            self.assertEqual(self._verify(tmp).status, "hash_invalid")


_XS_STRATEGY_ID = "relative_strength.long_only_proxy_v1"
_EQUAL_WEIGHT_PROVENANCE = {
    "universe_equal_weight_long_benchmark": "universe_equal_weight_long_v1",
    "universe_equal_weight_long_benchmark_pnl_usd": "12.00",
    "universe_equal_weight_long_active_pnl_usd": "105.50",
}


def _xs_metrics(**provenance_overrides):
    metrics = _v2_metrics(strategy_version=_XS_STRATEGY_ID)
    provenance = dict(metrics["provenance"])
    provenance.update(_EQUAL_WEIGHT_PROVENANCE)
    provenance.update(provenance_overrides)
    metrics["provenance"] = provenance
    return metrics


class TestBacktestGateVersionAndCrossSectional(unittest.TestCase):
    """S9 hardening: v1 artifacts are dead weight (no writer emits them) and
    were the last semantic-recompute bypass; the equal-weight second benchmark
    the cross-sectional WRITER gates must also hold at VERIFY time."""

    def _verify(self, artifacts_dir, *, strategy_id=_STRATEGY_ID):
        return verify_artifact(strategy_id, rules_hash=_RULES_HASH,
                               data_pin=_DATA_PIN, artifacts_dir=artifacts_dir)

    def test_v1_artifact_is_rejected(self):
        body = {
            "v": 1,
            "strategy_id": _STRATEGY_ID,
            "rules_hash": _RULES_HASH,
            "data_pin": _DATA_PIN,
            "metrics": {"basis": "execution_realistic_pnl",
                        "net": "-999999.00"},
            "created_utc": "2026-06-10T00:00:00.000000Z",
        }
        payload = dict(body)
        payload["artifact_hash"] = row_hash(body)
        with TemporaryDirectory() as tmp:
            _write_artifact(tmp, payload)

            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_cross_sectional_missing_equal_weight_provenance_is_rejected(self):
        with TemporaryDirectory() as tmp:
            metrics = _v2_metrics(strategy_version=_XS_STRATEGY_ID)
            _write_artifact(tmp, _artifact_payload(
                strategy_id=_XS_STRATEGY_ID, metrics=metrics))

            self.assertEqual(
                self._verify(tmp, strategy_id=_XS_STRATEGY_ID).status,
                "hash_invalid")

    def test_cross_sectional_negative_equal_weight_active_pnl_is_rejected(self):
        with TemporaryDirectory() as tmp:
            metrics = _xs_metrics(
                universe_equal_weight_long_active_pnl_usd="-710.00")
            _write_artifact(tmp, _artifact_payload(
                strategy_id=_XS_STRATEGY_ID, metrics=metrics))

            self.assertEqual(
                self._verify(tmp, strategy_id=_XS_STRATEGY_ID).status,
                "hash_invalid")

    def test_cross_sectional_wrong_equal_weight_benchmark_id_is_rejected(self):
        with TemporaryDirectory() as tmp:
            metrics = _xs_metrics(
                universe_equal_weight_long_benchmark="some_other_benchmark")
            _write_artifact(tmp, _artifact_payload(
                strategy_id=_XS_STRATEGY_ID, metrics=metrics))

            self.assertEqual(
                self._verify(tmp, strategy_id=_XS_STRATEGY_ID).status,
                "hash_invalid")

    def test_equal_weight_positivity_applies_when_key_present_off_family(self):
        with TemporaryDirectory() as tmp:
            metrics = _v2_metrics()
            metrics["provenance"] = dict(
                metrics["provenance"],
                universe_equal_weight_long_active_pnl_usd="-1.00")
            _write_artifact(tmp, _artifact_payload(metrics=metrics))

            self.assertEqual(self._verify(tmp).status, "hash_invalid")

    def test_cross_sectional_positive_equal_weight_verifies_ok(self):
        with TemporaryDirectory() as tmp:
            payload = _artifact_payload(
                strategy_id=_XS_STRATEGY_ID, metrics=_xs_metrics())
            path = _write_artifact(tmp, payload)

            self.assertEqual(
                self._verify(tmp, strategy_id=_XS_STRATEGY_ID),
                ArtifactCheck(status="ok", artifact_path=path,
                              artifact_hash=payload["artifact_hash"]))
