"""M7 paper-phase criteria evaluator tests."""
import copy
import unittest

from agent.paper_phase_criteria import evaluate_paper_phase_criteria


def _passing_metrics():
    return {
        "basis": "execution_realistic_pnl",
        "pass": True,
        "runner_version": "m7-backtest-v1",
        "strategy_version": "directional.momentum_v1",
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
            "tier": "paper",
        },
    }


class TestPaperPhaseCriteria(unittest.TestCase):
    def test_full_matrix_passes(self):
        verdict = evaluate_paper_phase_criteria(_passing_metrics())

        self.assertEqual(verdict.passed, True)
        self.assertEqual(verdict.failures, ())

    def test_missing_required_metric_blocks(self):
        metrics = _passing_metrics()
        del metrics["sample"]["traded_session_count"]

        verdict = evaluate_paper_phase_criteria(metrics)

        self.assertEqual(verdict.passed, False)
        self.assertIn("missing:sample.traded_session_count", verdict.failures)

    def test_each_failed_threshold_blocks_m8(self):
        cases = (
            (("sample", "session_count"), 19, "min_sessions"),
            (("sample", "trade_count"), 29, "min_trades"),
            (("sample", "traded_session_count"), 4, "min_traded_sessions"),
            (("pnl", "net_execution_realistic_pnl_usd"), "0.00",
             "positive_net_pnl"),
            (("benchmark", "active_pnl_usd"), "0.00", "positive_active_pnl"),
            (("pnl", "profit_factor"), "1.09", "profit_factor_min"),
            (("pnl", "avg_trade_bps"), "0.00", "avg_trade_bps_positive"),
            (("risk", "max_drawdown_pct_allocated"), "0.0151",
             "max_drawdown_pct_allocated"),
            (("risk", "worst_day_pct_allocated"), "0.0076",
             "worst_day_pct_allocated"),
            (("risk", "p95_realism_gap_bps"), "15.01",
             "p95_realism_gap_bps_max"),
            (("risk", "max_single_fill_divergence_bps"), "50.01",
             "max_single_fill_divergence_bps"),
            (("quality", "unresolved_reconcile_drift_count"), 1,
             "unresolved_reconcile_drift_count"),
            (("quality", "s1_canary_breach_count"), 1,
             "s1_canary_breach_count"),
            (("quality", "live_broker_submit_count"), 1,
             "live_broker_submit_count"),
            (("quality", "artifact_mismatch_count"), 1,
             "artifact_mismatch_count"),
            (("quality", "unhandled_exception_count"), 1,
             "unhandled_exception_count"),
        )

        for path, value, expected_failure in cases:
            with self.subTest(path=path):
                metrics = copy.deepcopy(_passing_metrics())
                metrics[path[0]][path[1]] = value

                verdict = evaluate_paper_phase_criteria(metrics)

                self.assertEqual(verdict.passed, False)
                self.assertIn(expected_failure, verdict.failures)


if __name__ == "__main__":
    unittest.main()
