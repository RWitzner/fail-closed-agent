"""Dashboard live-view state — read-only, hash-verified, S5-split PnL.

The state builder rolls the journal streams into the JSON the browser polls.
Pins: golden-journal aggregation (broker vs modeled NEVER conflated), live
positions derived open-minus-close, incremental refresh picks up appended
rows, corruption is reported not fatal, and the HTTP layer serves the page +
state on loopback."""
import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from dashboard.app import make_server
from dashboard.state import JournalStateSource

from tests.lib.exec_fixtures import run_synthetic_golden

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOLDEN = _REPO_ROOT / "tests" / "fixtures" / "execution" / "golden"


class TestJournalStateSource(unittest.TestCase):
    def test_golden_journal_snapshot(self):
        with TemporaryDirectory() as tmp:
            for name in ("orders", "fills", "positions"):
                shutil.copy(_GOLDEN / f"{name}.jsonl", Path(tmp))
            source = JournalStateSource(tmp)
            state = source.snapshot()

            self.assertEqual(state["pnl"]["realized_broker_pnl_usd"],
                             "1.000000")
            self.assertEqual(state["pnl"]["realized_modeled_pnl_usd"],
                             "0.930000")
            self.assertEqual(state["pnl"]["fees_usd"], "0.07")
            self.assertEqual(state["positions"]["opens"], 1)
            self.assertEqual(state["positions"]["closes"], 1)
            self.assertEqual(state["positions"]["open_count"], 0)
            self.assertEqual(state["fills"]["counts"]["broker_fill"], 4)
            self.assertEqual(
                state["fills"]["counts"]["modeled_execution_fill"], 2)
            self.assertTrue(state["run_ids"])
            # the whole snapshot must be JSON-serializable for the API
            json.dumps(state)

    def test_incremental_refresh_sees_appended_rows(self):
        from agent.journal import JournalWriter

        with TemporaryDirectory() as tmp:
            source = JournalStateSource(tmp)
            self.assertEqual(source.snapshot()["decisions"]["total"], 0)

            writer = JournalWriter(
                Path(tmp) / "decisions.jsonl", run_id="run-live",
                clock=lambda: "2026-07-06T14:00:00.000000Z")
            writer.append("forecast_only", {
                "action": "do_nothing",
                "decision_ts_utc": "2026-07-06T14:00:00.000000Z",
                "event_start_bar_key": "AAPL|1m|2026-07-06T13:59:00.000000Z"})
            state = source.snapshot()
            self.assertEqual(state["decisions"]["total"], 1)
            self.assertEqual(state["decisions"]["recent"][0]["symbol"],
                             "AAPL")

            writer.append("forecast_only", {
                "action": "do_nothing",
                "decision_ts_utc": "2026-07-06T14:01:00.000000Z",
                "event_start_bar_key": "MSFT|1m|2026-07-06T14:00:00.000000Z"})
            self.assertEqual(source.snapshot()["decisions"]["total"], 2)

    def test_corruption_reported_not_fatal(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "decisions.jsonl").write_text(
                "not-a-journal-row\n", encoding="utf-8")
            source = JournalStateSource(tmp)
            state = source.snapshot()
            self.assertIn("decisions", state["corruption"])
            self.assertEqual(state["decisions"]["total"], 0)

    def test_report_summaries_surface_incomplete_days(self):
        with TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "reports"
            report_dir.mkdir()
            (report_dir / "2026-07-06.json").write_text(json.dumps({
                "session_date_et": "2026-07-06", "mode": "paper",
                "session_incomplete": True,
                "trading": {"realized_broker_pnl_usd": "-1.00"},
                "kill": {"state": "monitoring"}}), encoding="utf-8")
            source = JournalStateSource(tmp, report_dir=report_dir)
            reports = source.snapshot()["reports"]
            self.assertEqual(len(reports), 1)
            self.assertTrue(reports[0]["session_incomplete"])

    def test_golden_pipeline_end_to_end(self):
        with TemporaryDirectory() as tmp:
            pipeline = run_synthetic_golden(Path(tmp))
            pipeline.close()
            state = JournalStateSource(tmp).snapshot()
            self.assertEqual(state["pnl"]["realized_broker_pnl_usd"],
                             "1.000000")
            self.assertEqual(state["kill"]["state"], "monitoring")


class TestHttpLiveView(unittest.TestCase):
    def test_serves_page_and_state_on_loopback(self):
        with TemporaryDirectory() as tmp:
            for name in ("orders", "fills", "positions"):
                shutil.copy(_GOLDEN / f"{name}.jsonl", Path(tmp))
            server = make_server("127.0.0.1", 0, journal_dir=tmp)
            import threading

            thread = threading.Thread(target=server.serve_forever,
                                      daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                page = urlopen(
                    f"http://127.0.0.1:{port}/", timeout=5).read()
                self.assertIn(b"live paper view", page)
                state = json.loads(urlopen(
                    f"http://127.0.0.1:{port}/api/state", timeout=5).read())
                self.assertEqual(state["pnl"]["realized_broker_pnl_usd"],
                                 "1.000000")
            finally:
                server.shutdown()
                server.server_close()

    def test_no_source_serves_explicit_error(self):
        server = make_server("127.0.0.1", 0)
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            state = json.loads(urlopen(
                f"http://127.0.0.1:{port}/api/state", timeout=5).read())
            self.assertIn("error", state)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
