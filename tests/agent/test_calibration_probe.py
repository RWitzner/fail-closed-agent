"""M3 §M.9 — CalibrationProbe E2E + the S1 guards (§H). [S1, S6]

S1 static: AST walk over ALL M3 module sources — no broker/preflight/kill-switch/
arming import at ANY scope, no reference to the FD-12 forbidden tokens, and no
reference to importlib/__import__ at all (string-import bypass). S1 subprocess: a
fixed-argv child imports every M3 module fresh and asserts none of the forbidden
modules land in sys.modules. S1 behavioral: the full pipeline journals only
decision/forecast_scored/forecast_unresolved event types.
"""
import ast
import json
import subprocess
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.journal import replay
from agent.market_state_cache import MarketStateCache
from agent.signal_config import SignalConfig
from agent.strategies.calibration_probe import ACTIONS, DecisionLedger
from recorder.persistence import EventWriter

from tests.lib.signal_fixtures import quotes_session, quotes_session_v1
from tests.lib.signal_pipeline import SignalPipeline, committed_config, run_golden_pipeline

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"

M3_MODULE_FILES = [
    SCRIPTS / "agent" / "signal_config.py",
    SCRIPTS / "agent" / "quote_quality.py",
    SCRIPTS / "agent" / "bar_series.py",
    SCRIPTS / "agent" / "feature_engine.py",
    SCRIPTS / "agent" / "signal_snapshot.py",
    SCRIPTS / "agent" / "strategy.py",
    SCRIPTS / "agent" / "candidate.py",
    SCRIPTS / "agent" / "forecast.py",
    SCRIPTS / "agent" / "calibration.py",
    SCRIPTS / "agent" / "calibration_report.py",
    SCRIPTS / "agent" / "strategies" / "calibration_probe.py",
]

FORBIDDEN_MODULES = ("agent.broker", "agent.execution_preflight",
                     "agent.kill_switch", "agent.arming")
FORBIDDEN_TOKENS = {
    "submit_order", "mint_open_token", "mint_reduce_only_token", "OrderIntent",
    "OpenPreflightToken", "ReduceOnlyPreflightToken", "PreflightToken",
    "require_token", "consume", "importlib", "__import__",
}


class TestS1StaticGuard(unittest.TestCase):
    def test_all_m3_module_files_exist(self):
        for path in M3_MODULE_FILES:
            self.assertTrue(path.exists(), path)

    def test_no_forbidden_imports_or_tokens_at_any_scope(self):
        for path in M3_MODULE_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for banned in FORBIDDEN_MODULES:
                            self.assertFalse(
                                alias.name == banned or alias.name.startswith(banned + "."),
                                f"{path}: imports {alias.name}")
                        self.assertNotEqual(alias.name, "importlib",
                                            f"{path}: imports importlib")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for banned in FORBIDDEN_MODULES:
                        self.assertFalse(
                            module == banned or module.startswith(banned + "."),
                            f"{path}: imports from {module}")
                    self.assertNotEqual(module, "importlib",
                                        f"{path}: imports from importlib")
                elif isinstance(node, ast.Name):
                    self.assertNotIn(node.id, FORBIDDEN_TOKENS,
                                     f"{path}: references {node.id}")
                elif isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, FORBIDDEN_TOKENS,
                                     f"{path}: references .{node.attr}")

    def test_s1_subprocess_import_isolation(self):
        script = (
            "import sys\n"
            "sys.path.insert(0, r'" + str(SCRIPTS) + "')\n"
            "import agent.signal_config, agent.quote_quality, agent.bar_series\n"
            "import agent.feature_engine, agent.signal_snapshot, agent.strategy\n"
            "import agent.candidate, agent.forecast, agent.calibration\n"
            "import agent.calibration_report, agent.strategies.calibration_probe\n"
            "bad = [m for m in sys.modules if m == 'agent.broker'\n"
            "       or m.startswith('agent.broker.')\n"
            "       or m in ('agent.execution_preflight', 'agent.kill_switch',\n"
            "                'agent.arming')]\n"
            "assert not bad, bad\n"
            "print('CLEAN')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],  # fixed argv array, never shell=True
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLEAN", result.stdout)


class TestProbeE2E(unittest.TestCase):
    def test_valid_tick_emits_two_forecast_rows(self):
        with TemporaryDirectory() as tmpdir:
            pipeline = SignalPipeline(quote_rows=quotes_session_v1(),
                                      journal_dir=tmpdir, run_id="run-e2e-1")
            decisions = pipeline.tick_on_bar(50)
            self.assertEqual(len(decisions), 2)
            self.assertEqual([d.horizon for d in decisions], ["5m", "30m"])
            for decision in decisions:
                self.assertEqual(decision.action, "forecast_only")
                row = decision.row
                self.assertIs(row["paper_eligible"], False)
                self.assertEqual(row["rules_hash"], pipeline.config.rules_hash)
                self.assertEqual(row["data_pin"],
                                 "EQUS.MINI:tbbo:1m:fixture:signal-aapl-v1")
                self.assertTrue(decision.decision_id.startswith("d-"))
                self.assertTrue(decision.forecast_id.startswith("f-"))
                # edge_label = p - reference (constant_0.5 before any resolution)
                self.assertEqual(Decimal(str(row["edge_label"])),
                                 decision.forecast.p - Decimal("0.500000"))
                self.assertEqual(row["reference_forecaster_id"], "constant_0.5")
                self.assertEqual(row["forecast"]["event_type"], "up_move")
            self.assertNotEqual(decisions[0].decision_id, decisions[1].decision_id)
            self.assertNotEqual(decisions[0].forecast_id, decisions[1].forecast_id)

    def test_pre_horizon_failure_emits_exactly_one_row(self):
        with TemporaryDirectory() as tmpdir:
            pipeline = SignalPipeline(quote_rows=quotes_session_v1(),
                                      journal_dir=tmpdir, run_id="run-e2e-2")
            stale = MarketStateCache.safe_default_verdict("AAPL", 1001, "2026-06-15")
            decisions = pipeline.tick_on_bar(50, verdict=stale)
            self.assertEqual(len(decisions), 1)
            decision = decisions[0]
            self.assertEqual(decision.action, "do_nothing")
            self.assertIsNone(decision.horizon)
            self.assertEqual(decision.row["gate_stage"], "market_state")
            self.assertEqual(tuple(decision.row["reasons"]), (
                "market_state_not_rth", "market_state_not_tradable",
                "market_state_stale_default"))
            self.assertIsNone(decision.row["resolve_bar_key"])
            rows = replay(pipeline.decisions_path)
            self.assertEqual(len(rows), 1)

    def test_uncovered_calendar_date_fails_per_horizon(self):
        with TemporaryDirectory() as tmpdir:
            pipeline = SignalPipeline(
                quote_rows=quotes_session(session_date="2026-06-16", minutes=55),
                journal_dir=tmpdir, run_id="run-e2e-3")
            decisions = pipeline.tick_on_bar(50)
            self.assertEqual(len(decisions), 2)
            for decision, horizon in zip(decisions, ("5m", "30m")):
                self.assertEqual(decision.action, "do_nothing")
                self.assertEqual(decision.horizon, horizon)
                self.assertEqual(decision.reasons, ("calendar_unknown",))
                self.assertEqual(decision.row["gate_stage"], "horizon")

    def test_horizon_independence_first_horizon_fails_second_forecasts(self):
        # rev2 SAFETY-F4: horizons ("30m","5m") on the half-day 12:45 tick — the
        # FIRST configured horizon crosses the 13:00 close, the second still fires.
        config_dict = json.loads(json.dumps(committed_config()))
        config_dict["signal"]["horizons"] = ["30m", "5m"]
        with TemporaryDirectory() as tmpdir:
            pipeline = SignalPipeline(
                quote_rows=quotes_session(session_date="2026-11-27",
                                          start_et="11:54", minutes=52),
                journal_dir=tmpdir, run_id="run-e2e-4",
                signal_config=SignalConfig.from_config(config_dict))
            bar = pipeline.bars[50]
            self.assertEqual(bar.bucket_end_utc, "2026-11-27T17:45:00.000000Z")  # 12:45 ET
            decisions = pipeline.tick_on_bar(50)
            self.assertEqual(len(decisions), 2)
            first, second = decisions
            self.assertEqual((first.action, first.horizon), ("do_nothing", "30m"))
            self.assertEqual(first.reasons, ("session_horizon_crosses_close",))
            self.assertEqual((second.action, second.horizon), ("forecast_only", "5m"))

    def test_lagging_feature_view_yields_distinct_ids_not_duplicates(self):
        # rev2 SAFETY-F5: tick N+1 without a refresh must NOT re-key to bar N.
        with TemporaryDirectory() as tmpdir:
            pipeline = SignalPipeline(quote_rows=quotes_session_v1(),
                                      journal_dir=tmpdir, run_id="run-e2e-5")
            first = pipeline.tick_on_bar(50)
            second = pipeline.tick_on_bar(51, refresh_features=False)
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0].action, "do_nothing")
            self.assertEqual(second[0].row["gate_stage"], "features")
            self.assertEqual(second[0].reasons, ("feature_cutoff_mismatch",))
            all_ids = [d.decision_id for d in first] + [d.decision_id for d in second]
            self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_whole_second_form_tick_mints_canonical_keys_and_same_ids(self):
        # harden round 1, M3-02: a recorder-derived whole-second boundary must
        # journal the SAME canonical bar key and the SAME ids as the canonical form.
        with TemporaryDirectory() as dir_a, TemporaryDirectory() as dir_b:
            canonical = SignalPipeline(quote_rows=quotes_session_v1(),
                                       journal_dir=dir_a, run_id="run-form")
            bar = canonical.bars[50]
            decisions_canonical = canonical.tick_on_bar(50)

            other = SignalPipeline(quote_rows=quotes_session_v1(),
                                   journal_dir=dir_b, run_id="run-form")
            whole_second = bar.bucket_end_utc.split(".")[0] + "Z"
            self.assertNotEqual(whole_second, bar.bucket_end_utc)
            other.clock.advance(1_000)
            other.feature_view.refresh(symbol="AAPL", instrument_id=1001,
                                       as_of_utc=bar.bucket_end_utc)
            from decimal import Decimal as D
            from agent.quote_quality import QuoteSnapshot
            from tests.lib.signal_pipeline import tradable_verdict
            now_ms = other.clock.now_ms()
            other.quote_view.put(QuoteSnapshot(
                symbol="AAPL", instrument_id=1001,
                bid=bar.mid - D("0.0100"), ask=bar.mid + D("0.0100"),
                bid_sz=D("300"), ask_sz=D("200"),
                ts_event_utc=bar.bucket_end_utc, ts_recv_utc=bar.bucket_end_utc,
                seen_at_ms=now_ms, reconnect_epoch=0, vendor_seq=None,
                dataset="EQUS.MINI", schema="tbbo"))
            other.market_state_cache.put(
                tradable_verdict("AAPL", 1001, "2026-06-15"), now_ms=now_ms)
            decisions_other = other.probe.on_bar_complete(
                symbol="AAPL", instrument_id=1001,
                event_start_bar_end_utc=whole_second,
                decision_ts_utc=bar.bucket_end_utc.replace(".000000Z", ".250000Z"))

            self.assertEqual([d.decision_id for d in decisions_canonical],
                             [d.decision_id for d in decisions_other])
            for d in decisions_other:
                self.assertEqual(d.row["event_start_bar_key"],
                                 f"AAPL|1m|{bar.bucket_end_utc}")

    def test_rerun_same_run_id_reproduces_ids_and_row_hashes(self):
        with TemporaryDirectory() as dir_a, TemporaryDirectory() as dir_b:
            runs = []
            for tmpdir in (dir_a, dir_b):
                pipeline = SignalPipeline(quote_rows=quotes_session_v1(),
                                          journal_dir=tmpdir, run_id="run-repro")
                pipeline.tick_on_bar(50)
                pipeline.tick_on_bar(51)
                runs.append(replay(pipeline.decisions_path))
            ids_a = [(r["decision_id"], r.get("forecast_id"), r["hash"]) for r in runs[0]]
            ids_b = [(r["decision_id"], r.get("forecast_id"), r["hash"]) for r in runs[1]]
            self.assertEqual(ids_a, ids_b)

    def test_s1_behavioral_event_types_only(self):
        with TemporaryDirectory() as tmpdir:
            run_golden_pipeline(tmpdir)
            decisions = replay(Path(tmpdir) / "decisions.jsonl")
            scored = replay(Path(tmpdir) / "forecast_scored.jsonl")
            self.assertTrue(decisions)
            self.assertTrue(scored)
            self.assertEqual({r["event_type"] for r in decisions}, {"decision"})
            self.assertLessEqual({r["event_type"] for r in scored},
                                 {"forecast_scored", "forecast_unresolved"})
            for row in decisions:
                self.assertIs(row["paper_eligible"], False)
                self.assertIn(row["action"], ACTIONS)


class TestDecisionLedgerValidation(unittest.TestCase):
    def _fields(self, **overrides):
        base = {
            "symbol": "AAPL", "instrument_id": 1001, "strategy": "calibration_probe_v1",
            "action": "do_nothing", "gate_stage": "features", "reasons": ["feature_stale"],
            "horizon": None, "forecast_id": None, "forecast": None,
            "reference_base_rate_asof_t0": None, "reference_forecaster_id": None,
            "reference_n": None, "edge_label": None, "signal_provenance": None,
            "quote_provenance": None,
            "market_state_provenance": {"tradability": "tradable"},
            "event_start_bar_key": "AAPL|1m|2026-06-15T14:21:00.000000Z",
            "resolve_bar_key": None,
            "decision_ts_utc": "2026-06-15T14:21:00.250000Z",
            "decision_seen_at_ms": 1, "data_pin": "p", "rules_hash": "rh",
            "paper_eligible": False,
        }
        base.update(overrides)
        return base

    def _ledger(self, tmpdir):
        writer = EventWriter(Path(tmpdir) / "decisions.jsonl", "run-x",
                             clock=lambda: "2026-06-15T21:00:00+00:00")
        return DecisionLedger(writer, rules_hash="rh")

    def test_rejects_would_open(self):
        with TemporaryDirectory() as tmpdir:
            with self.assertRaises(ValueError):
                self._ledger(tmpdir).record_decision(
                    decision_id="d-1", fields=self._fields(action="would_open"))

    def test_rejects_paper_eligible_true(self):
        with TemporaryDirectory() as tmpdir:
            with self.assertRaises(ValueError):
                self._ledger(tmpdir).record_decision(
                    decision_id="d-1", fields=self._fields(paper_eligible=True))

    def test_rejects_out_of_vocab_reason_and_stage(self):
        with TemporaryDirectory() as tmpdir:
            ledger = self._ledger(tmpdir)
            with self.assertRaises(ValueError):
                ledger.record_decision(
                    decision_id="d-1", fields=self._fields(reasons=["made_up"]))
            with self.assertRaises(ValueError):
                ledger.record_decision(
                    decision_id="d-1", fields=self._fields(gate_stage="vibes"))

    def test_rejects_field_set_mismatch(self):
        with TemporaryDirectory() as tmpdir:
            fields = self._fields()
            fields["smuggled"] = 1
            with self.assertRaises(ValueError):
                self._ledger(tmpdir).record_decision(decision_id="d-1", fields=fields)


if __name__ == "__main__":
    unittest.main()
