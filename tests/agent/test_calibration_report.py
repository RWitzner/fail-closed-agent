"""M3 §M.8 — calibration report (§G). [S6]

The TWO frozen funnel identities, FD-11 bin edges, deterministic ordering, report
dedupe, decision-time BSS references, and the byte-identical committed golden.
"""
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.calibration_report import build_report, render_markdown, write_report
from agent.serializer import dumps

from tests.lib.signal_pipeline import run_golden_pipeline

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = REPO_ROOT / "tests" / "fixtures" / "signal" / "golden_report_v1.json"


def _decision_row(*, seq, action="forecast_only", symbol="AAPL", instrument_id=1001,
                  bar="14:21", gate_stage=None, horizon="5m", forecast_id=None,
                  p="0.600000", ref="0.500000"):
    key = f"{symbol}|1m|2026-06-15T{bar}:00.000000Z"
    row = {
        "event_type": "decision", "seq": seq,
        "symbol": symbol, "instrument_id": instrument_id,
        "action": action, "gate_stage": gate_stage, "horizon": horizon,
        "event_start_bar_key": key,
        "forecast_id": forecast_id,
        "forecast": None, "reference_base_rate_asof_t0": None,
        "signal_provenance": None,
    }
    if action == "forecast_only":
        row["forecast"] = {"event_type": "up_move", "h": horizon, "k": "0", "p": p}
        row["reference_base_rate_asof_t0"] = ref
        row["signal_provenance"] = {"model_version": "logit-mom-v1"}
    return row


def _scored_row(*, seq, forecast_id, outcome=1, event_type="forecast_scored",
                reason=None):
    row = {"event_type": event_type, "seq": seq, "forecast_id": forecast_id,
           "outcome": outcome}
    if reason is not None:
        row["reason"] = reason
    return row


class TestFunnelIdentities(unittest.TestCase):
    def test_mixed_set_satisfies_both_identities(self):
        # 5 ticks: 1 identity-fail, 1 market-state-fail (pre-horizon, one row each);
        # 3 reach the horizon stage -> 2 horizons each: 1 horizon-fail + 5 forecasts.
        decisions = [
            _decision_row(seq=1, action="do_nothing", gate_stage="identity",
                          horizon=None, bar="14:01"),
            _decision_row(seq=2, action="do_nothing", gate_stage="market_state",
                          horizon=None, bar="14:02"),
            _decision_row(seq=3, bar="14:03", horizon="5m", forecast_id="f-1"),
            _decision_row(seq=4, bar="14:03", horizon="30m", forecast_id="f-2"),
            _decision_row(seq=5, bar="14:04", horizon="5m", forecast_id="f-3"),
            _decision_row(seq=6, action="do_nothing", gate_stage="horizon",
                          bar="14:04", horizon="30m"),
            _decision_row(seq=7, bar="14:05", horizon="5m", forecast_id="f-4"),
            _decision_row(seq=8, bar="14:05", horizon="30m", forecast_id="f-5"),
        ]
        scored = [
            _scored_row(seq=1, forecast_id="f-1", outcome=1),
            _scored_row(seq=2, forecast_id="f-2", outcome=0),
            _scored_row(seq=3, forecast_id="f-3", outcome=1),
            _scored_row(seq=4, forecast_id="f-1", outcome=0),  # duplicate -> dropped
            _scored_row(seq=5, forecast_id="f-4",
                        event_type="forecast_unresolved", reason="no_mid_bar_resolve",
                        outcome=None),
        ]
        report = build_report(decision_rows=decisions, scored_rows=scored,
                              run_id="run-r", rules_hash="rh",
                              generated_ts_utc="2026-06-09T00:00:00.000000Z")
        funnel = report["funnel"]
        self.assertEqual(funnel["ticks"], 5)
        self.assertEqual(funnel["ticks_reaching_horizon"], 3)
        # identity 1
        self.assertEqual(
            funnel["do_nothing_identity"] + funnel["do_nothing_features"]
            + funnel["do_nothing_quote"] + funnel["do_nothing_market_state"]
            + funnel["ticks_reaching_horizon"],
            funnel["ticks"])
        # identity 2 (len(horizons)=2)
        self.assertEqual(funnel["do_nothing_horizon"] + funnel["forecasts"],
                         funnel["ticks_reaching_horizon"] * 2)
        self.assertEqual(funnel["scored"], 3)
        self.assertEqual(funnel["unresolved"], 1)
        self.assertEqual(report["dedupe"]["duplicate_scored_dropped"], 1)
        self.assertEqual(report["unresolved"]["by_reason"],
                         {"no_mid_bar_resolve": 1})

    def test_bss_uses_decision_time_references_including_constant_half(self):
        decisions = [
            _decision_row(seq=1, bar="14:03", forecast_id="f-1",
                          p="0.800000", ref="0.500000"),
            _decision_row(seq=2, bar="14:04", forecast_id="f-2",
                          p="0.800000", ref="1.000000"),  # climatology ref, perfect
        ]
        scored = [
            _scored_row(seq=1, forecast_id="f-1", outcome=1),
            _scored_row(seq=2, forecast_id="f-2", outcome=1),
        ]
        report = build_report(decision_rows=decisions, scored_rows=scored,
                              run_id="r", rules_hash="rh",
                              generated_ts_utc="2026-06-09T00:00:00.000000Z")
        # BS_ref(climatology) = ((0.5-1)^2 + (1-1)^2)/2 = 0.125
        self.assertEqual(report["aggregate"]["brier_ref_climatology_asof"],
                         Decimal("0.12500000"))
        # BS_ref(constant half) = 0.25 always
        self.assertEqual(report["aggregate"]["brier_ref_constant_half"],
                         Decimal("0.25000000"))
        # BS_model = (0.04 + 0.04)/2 = 0.04
        self.assertEqual(report["aggregate"]["brier"], Decimal("0.04000000"))
        self.assertEqual(report["aggregate"]["bss_vs_constant_half"],
                         Decimal("0.84000000"))


class TestBinsAndOrdering(unittest.TestCase):
    def test_bin_edges_and_thin_and_empty(self):
        decisions = [
            _decision_row(seq=1, bar="14:03", forecast_id="f-1", p="0.000000"),
            _decision_row(seq=2, bar="14:04", forecast_id="f-2", p="0.950000"),
            _decision_row(seq=3, bar="14:05", forecast_id="f-3", p="0.999999"),
        ]
        scored = [
            _scored_row(seq=1, forecast_id="f-1", outcome=0),
            _scored_row(seq=2, forecast_id="f-2", outcome=1),
            _scored_row(seq=3, forecast_id="f-3", outcome=1),
        ]
        report = build_report(decision_rows=decisions, scored_rows=scored,
                              run_id="r", rules_hash="rh",
                              generated_ts_utc="2026-06-09T00:00:00.000000Z")
        bins = report["bins"]
        self.assertEqual(bins[0]["count"], 1)   # p=0.0 -> bin 0
        self.assertEqual(bins[9]["count"], 2)   # 0.95 and 1-quantum -> bin 9
        self.assertTrue(bins[9]["thin"])
        empty = bins[4]
        self.assertEqual(empty["count"], 0)
        self.assertIsNone(empty["mean_forecast_p"])
        self.assertIsNone(empty["observed_freq"])

    def test_per_cell_sorted_by_symbol_horizon(self):
        decisions = [
            _decision_row(seq=1, symbol="MSFT", bar="14:03", horizon="5m",
                          forecast_id="f-1"),
            _decision_row(seq=2, symbol="AAPL", bar="14:03", horizon="30m",
                          forecast_id="f-2"),
            _decision_row(seq=3, symbol="AAPL", bar="14:04", horizon="5m",
                          forecast_id="f-3"),
        ]
        scored = [
            _scored_row(seq=1, forecast_id="f-1"),
            _scored_row(seq=2, forecast_id="f-2"),
            _scored_row(seq=3, forecast_id="f-3"),
        ]
        report = build_report(decision_rows=decisions, scored_rows=scored,
                              run_id="r", rules_hash="rh",
                              generated_ts_utc="2026-06-09T00:00:00.000000Z")
        self.assertEqual([(c["symbol"], c["horizon"]) for c in report["per_cell"]],
                         [("AAPL", "30m"), ("AAPL", "5m"), ("MSFT", "5m")])


class TestFrozenShape(unittest.TestCase):
    def test_top_level_key_set_is_frozen(self):
        # harden round 1, M3-03: no extra keys (the ad-hoc 'horizons' is gone).
        report = build_report(decision_rows=[], scored_rows=[], run_id="r",
                              rules_hash="rh",
                              generated_ts_utc="2026-06-09T00:00:00.000000Z")
        self.assertEqual(set(report), {
            "run_id", "rules_hash", "model_version", "generated_ts_utc",
            "funnel", "dedupe", "aggregate", "bins", "per_cell", "unresolved",
        })

    def test_zero_sample_report_has_ten_empty_bins(self):
        # harden round 1, M3-EDGE-4: FD-11 always renders the full bin array.
        report = build_report(decision_rows=[], scored_rows=[], run_id="r",
                              rules_hash="rh",
                              generated_ts_utc="2026-06-09T00:00:00.000000Z")
        self.assertEqual(len(report["bins"]), 10)
        for bin_row in report["bins"]:
            self.assertEqual(bin_row["count"], 0)
            self.assertIsNone(bin_row["mean_forecast_p"])
            self.assertIsNone(bin_row["observed_freq"])
            self.assertFalse(bin_row["thin"])
        self.assertIsNone(report["aggregate"]["brier"])
        self.assertEqual(report["funnel"]["scored"], 0)


class TestGoldenReport(unittest.TestCase):
    def test_golden_report_byte_identical(self):
        with TemporaryDirectory() as tmpdir:
            report = run_golden_pipeline(tmpdir)
        regenerated = dumps(report) + "\n"
        committed = GOLDEN_PATH.read_text(encoding="utf-8")
        self.assertEqual(regenerated, committed)

    def test_write_report_and_render(self):
        with TemporaryDirectory() as tmpdir:
            report = run_golden_pipeline(tmpdir)
            with TemporaryDirectory() as out:
                json_path = write_report(report, out_dir=out)
                self.assertTrue(json_path.exists())
                self.assertTrue(json_path.with_suffix(".md").exists())
                self.assertEqual(json_path.read_text(encoding="utf-8"),
                                 dumps(report) + "\n")
            markdown = render_markdown(report)
            self.assertIn("run-m3-golden-v1", markdown)
            self.assertIn("## Funnel", markdown)


if __name__ == "__main__":
    unittest.main()
