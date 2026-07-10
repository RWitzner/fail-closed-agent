"""agent.paper_phase_report — the weekly criteria aggregator.

Pins: per-trade modeled PnL → PF/avg-bps/drawdown/worst-day; divergence
p95/max; sessions bookkeeping (highest restart suffix wins; incomplete days
never count as clean sessions); missing evidence stays MISSING (explicit
missing: failures — the benchmark leg is not zero-filled); modeled-null
closes are excluded from PF and counted loudly."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.journal import JournalWriter
from agent.paper_phase_criteria import evaluate_paper_phase_criteria
from agent.paper_phase_report import build_phase_metrics, render_phase_report


def _report(report_dir: Path, date: str, *, run_id, truncated=False,
            incomplete=False, drift_rows=0, suffix=None):
    report_dir.mkdir(parents=True, exist_ok=True)
    name = f"{date}.json" if suffix is None else f"{date}.{suffix}.json"
    (report_dir / name).write_text(json.dumps({
        "session_date_et": date, "run_id": run_id,
        "session": {"feed_truncated": truncated},
        "session_incomplete": incomplete,
        "reconcile": {"drift_rows": drift_rows},
    }), encoding="utf-8")


def _close(writer, *, ts, modeled, fee="0.00"):
    body = {"realized_broker_pnl": modeled, "fees_assessed":
            {"total_usd": fee}}
    if modeled is not None:
        body["realized_modeled_pnl"] = modeled
    writer.append("position_close", body)


class TestBuildPhaseMetrics(unittest.TestCase):
    def _journal(self, tmp, run_id, closes, divergences=()):
        clock_values = iter(
            [f"2026-07-0{1 + i % 6}T15:00:00.000000Z"
             for i in range(len(closes) + len(divergences))])
        writer = JournalWriter(Path(tmp) / "positions.jsonl", run_id=run_id,
                               clock=lambda: next(clock_values))
        for value in closes:
            _close(writer, ts=None, modeled=value)
        if divergences:
            fills = JournalWriter(
                Path(tmp) / "fills.jsonl", run_id=run_id,
                clock=lambda: "2026-07-01T15:00:00.000000Z")
            for bps in divergences:
                fills.append("fill_divergence", {"divergence_bps": bps})

    def test_aggregates_trades_and_divergence(self):
        with TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "reports"
            journal_dir = Path(tmp) / "journal"
            journal_dir.mkdir()
            _report(report_dir, "2026-07-01", run_id="run-1")
            _report(report_dir, "2026-07-02", run_id="run-1")
            self._journal(journal_dir, "run-1",
                          ["10.00", "-4.00", "6.00", None],
                          divergences=["1.00", "-12.00", "3.00"])

            report = build_phase_metrics(
                report_dir=report_dir, journal_dir=journal_dir,
                allocated_notional_usd="10000")
            metrics = report["metrics"]

            self.assertEqual(metrics["sample"]["trade_count"], 3)
            self.assertEqual(report["modeled_null_closes"], 1)
            self.assertEqual(
                metrics["pnl"]["net_execution_realistic_pnl_usd"],
                "12.000000")
            self.assertEqual(metrics["pnl"]["profit_factor"], "4.000000")
            self.assertEqual(metrics["risk"]["p95_realism_gap_bps"],
                             "12.000000")
            self.assertEqual(
                metrics["risk"]["max_single_fill_divergence_bps"],
                "12.000000")

    def test_missing_benchmark_stays_missing_not_zero(self):
        with TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "reports"
            journal_dir = Path(tmp) / "journal"
            journal_dir.mkdir()
            _report(report_dir, "2026-07-01", run_id="run-1")
            self._journal(journal_dir, "run-1", ["1.00"])

            report = build_phase_metrics(
                report_dir=report_dir, journal_dir=journal_dir,
                allocated_notional_usd="10000")
            verdict = evaluate_paper_phase_criteria(report["metrics"])

            self.assertFalse(verdict.passed)
            self.assertIn("missing:benchmark.active_pnl_usd",
                          verdict.failures)
            self.assertNotIn("benchmark", report["metrics"])

    def test_sessions_bookkeeping_and_restart_suffix(self):
        with TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "reports"
            journal_dir = Path(tmp) / "journal"
            journal_dir.mkdir()
            # first attempt truncated, restart clean → the date counts ONCE,
            # as complete (highest suffix wins)
            _report(report_dir, "2026-07-01", run_id="run-a", truncated=True)
            _report(report_dir, "2026-07-01", run_id="run-b", suffix=1)
            _report(report_dir, "2026-07-02", run_id="run-c",
                    incomplete=True)

            report = build_phase_metrics(
                report_dir=report_dir, journal_dir=journal_dir,
                allocated_notional_usd="10000")

            self.assertEqual(report["sessions"]["complete"], ["2026-07-01"])
            self.assertEqual(report["sessions"]["incomplete"],
                             ["2026-07-02"])
            self.assertEqual(
                report["metrics"]["sample"]["session_count"], 1)
            self.assertEqual(
                report["metrics"]["quality"]["unhandled_exception_count"], 1)

    def test_rejects_nonpositive_allocation(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                build_phase_metrics(
                    report_dir=Path(tmp), journal_dir=Path(tmp),
                    allocated_notional_usd="0")

    def test_render_distinguishes_failed_from_missing(self):
        with TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "reports"
            journal_dir = Path(tmp) / "journal"
            journal_dir.mkdir()
            _report(report_dir, "2026-07-01", run_id="run-1")
            self._journal(journal_dir, "run-1", ["-5.00", "1.00"])

            report = build_phase_metrics(
                report_dir=report_dir, journal_dir=journal_dir,
                allocated_notional_usd="10000")
            text = render_phase_report(report)

            self.assertIn("NOT PASSING", text)
            self.assertIn("missing evidence:", text)
            self.assertIn("failed criteria:", text)


if __name__ == "__main__":
    unittest.main()
