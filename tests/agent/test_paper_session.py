"""agent.paper_session + agent.paper_report — the day-runner and the daily roll-up.

Offline + deterministic. Asserts:
  - observe rehearsal (committed config, no broker): SOD/EOD skipped, loop runs
    over a live-shaped feed, report written, exit 0, zero submits (S1),
  - paper mode over a feed crossing the RTH close: SOD runs first, the IN-LOOP
    session edge fires the EOD reconcile, ensure_eod_reconcile stays a no-op
    (exactly one EOD), report aggregates the run, exit 0 on clean,
  - a feed dying before the close: ensure_eod_reconcile runs the EOD fallback,
  - a halted prior journal: runner exits 4,
  - the strategy registry: momentum ids resolve; the cross-sectional RS id and
    unknown ids fail closed,
  - the report roll-up over the golden synthetic journal: exact PnL split,
    fees, fill counts; empty journal rolls up to zeros.
"""
import json
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from agent import config as agent_config
from agent import orchestrator as orch_mod
from agent.bar_series import _parse_utc
from agent.broker.alpaca import AlpacaPaperBroker
from agent.market_calendar import FixtureScheduleProvider
from agent.marketdata.live_feed import LiveClock, LiveQuoteFeed
from agent.paper_report import build_daily_report, render_text
from agent.paper_session import (
    SessionResult,
    build_strategy,
    run_paper_session,
)
from recorder.event import Provenance, QuoteEvent

from tests.lib.alpaca_fixtures import ScriptedOrderApi
from tests.lib.exec_fixtures import (
    permissive_paper_fixture_config,
    run_synthetic_golden,
)
from tests.lib.risk_fixtures import FakeAccountProvider, account_payload

_REPO_ROOT = Path(__file__).resolve().parents[2]
_H2_FIXTURE = (_REPO_ROOT / "tests" / "fixtures" / "calendar"
               / "xnys_sessions_2026H2_v1.json")


def _provider() -> FixtureScheduleProvider:
    fixture = json.loads(_H2_FIXTURE.read_text(encoding="utf-8"))
    return FixtureScheduleProvider(fixture, pin=fixture["pin"])


class _TimeMachine:
    def __init__(self, start_utc: str) -> None:
        self._mono = 0.0
        self._base = _parse_utc(start_utc)

    def monotonic(self) -> float:
        return self._mono

    def utc_now(self):
        return self._base + timedelta(seconds=self._mono)

    def utc_iso(self) -> str:
        return self.utc_now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def advance(self, seconds: float) -> None:
        self._mono += seconds


def _quote(tm, symbol="AAPL", *, instrument_id=1001,
           bid="100.00", ask="100.02") -> QuoteEvent:
    now_iso = tm.utc_iso()
    prov = Provenance(
        dataset="EQUS.MINI", schema="bbo-1s", instrument_id=instrument_id,
        symbol=symbol, vendor_seq=None, ts_event_utc=now_iso,
        ts_recv_utc=now_iso, reconnect_epoch=0)
    return QuoteEvent(provenance=prov, bid_px=Decimal(bid),
                      bid_sz=Decimal("100"), ask_px=Decimal(ask),
                      ask_sz=Decimal("100"))


def _scripted(tm, steps):
    for advance_s, item in steps:
        tm.advance(advance_s)
        yield item() if callable(item) else item


def _live_feed(tm, steps, *, stop_at):
    return LiveQuoteFeed(
        record_source=_scripted(tm, steps),
        symbols=("AAPL",), dataset="EQUS.MINI", schema="bbo-1s",
        stop_at_utc=stop_at,
        clock=LiveClock(monotonic_fn=tm.monotonic),
        utc_now_fn=tm.utc_now)


def _committed_config() -> dict:
    return {
        "agent_rules": agent_config.load(
            _REPO_ROOT / "config" / "agent_rules.json"),
        "risk_rules": agent_config.load(
            _REPO_ROOT / "config" / "risk_rules.json"),
    }


def _reconcile_phases(journal_dir: Path) -> list:
    path = journal_dir / "reconcile_alerts.jsonl"
    if not path.exists():
        return []
    phases = []
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if row.get("event_type") == "reconcile_run":
            phases.append(row.get("phase"))
    return phases


class TestObserveRehearsal(unittest.TestCase):
    def test_observe_session_runs_and_reports_zero_submits(self):
        tm = _TimeMachine("2026-07-06T13:30:00.000000Z")
        steps = [(5.0, (lambda: _quote(tm)))] * 40
        feed = _live_feed(tm, steps, stop_at="2026-07-06T20:15:00.000000Z")
        with TemporaryDirectory() as tmp:
            orch = orch_mod.Orchestrator(
                journal_dir=Path(tmp), run_id="run-session-observe",
                clock=feed.clock(), quote_view=feed.quote_view(),
                bar_reader=feed.bar_reader(), calendar_provider=_provider(),
                config=_committed_config())
            try:
                result = run_paper_session(
                    orchestrator=orch, feed=feed, journal_dir=Path(tmp),
                    session_date_et="2026-07-06",
                    report_dir=Path(tmp) / "reports",
                    utc_now_iso_fn=tm.utc_iso)
            finally:
                orch.close()
            self.assertIsInstance(result, SessionResult)
            self.assertEqual(result.mode, "observe")
            self.assertEqual(result.exit_code, 0)
            self.assertIsNone(result.sod_clean)
            self.assertIsNone(result.eod_clean)
            self.assertGreater(result.ticks_run, 0)
            self.assertTrue(result.report_path.exists())
            # no orders stream: nothing could submit in observe mode (S1)
            self.assertEqual(
                result.report["counts_by_stream"].get("orders", {}), {})
            self.assertEqual(_reconcile_phases(Path(tmp)), [])


class TestPaperSession(unittest.TestCase):
    def _paper_orch(self, tmp, feed, *, run_gates=True):
        gates_path = Path(tmp) / "run_gates.json"
        if run_gates:
            gates_path.write_text(json.dumps(
                {"enabled": True, "paper_trading": {"enabled": True}}))
        provider = FakeAccountProvider(
            account_payloads=[account_payload()], positions_payloads=[[]])
        return orch_mod.Orchestrator(
            journal_dir=Path(tmp) / "journal",
            run_id="run-session-paper",
            clock=feed.clock(), quote_view=feed.quote_view(),
            bar_reader=feed.bar_reader(), calendar_provider=_provider(),
            config=permissive_paper_fixture_config(),
            broker=AlpacaPaperBroker(order_api=ScriptedOrderApi({})),
            account_provider=provider,
            run_gates_path=gates_path)

    def test_paper_session_sod_loop_inloop_eod_report(self):
        # 19:58 ET-close approach: quotes cross the 20:00Z RTH close so the
        # in-loop session edge fires the EOD reconcile itself.
        tm = _TimeMachine("2026-07-06T19:58:00.000000Z")
        steps = ([(10.0, (lambda: _quote(tm)))] * 11    # -> 19:59:50
                 + [(40.0, (lambda: _quote(tm)))]       # 20:00:30 post-close
                 + [(30.0, (lambda: _quote(tm)))]       # 20:01:00 completes bars
                 + [(2.0, None), (2.0, None)])
        feed = _live_feed(tm, steps, stop_at="2026-07-06T20:15:00.000000Z")
        with TemporaryDirectory() as tmp:
            orch = self._paper_orch(tmp, feed)
            try:
                self.assertEqual(orch.mode, "paper")
                result = run_paper_session(
                    orchestrator=orch, feed=feed,
                    journal_dir=Path(tmp) / "journal",
                    session_date_et="2026-07-06",
                    report_dir=Path(tmp) / "reports",
                    utc_now_iso_fn=tm.utc_iso)
            finally:
                orch.close()
            self.assertEqual(result.mode, "paper")
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(result.sod_clean)
            # the in-loop edge ran EOD; ensure_eod_reconcile was a no-op
            self.assertIsNone(result.eod_clean)
            phases = _reconcile_phases(Path(tmp) / "journal")
            self.assertEqual(phases, ["sod", "eod"])
            self.assertEqual(result.report["reconcile"]["runs"], 2)
            self.assertTrue(result.report["reconcile"]["all_clean"])
            self.assertEqual(result.kill_state, "monitoring")

    def test_feed_dying_early_falls_back_to_ensure_eod(self):
        tm = _TimeMachine("2026-07-06T19:58:00.000000Z")
        steps = [(10.0, (lambda: _quote(tm)))] * 3   # dies at 19:58:30
        feed = _live_feed(tm, steps, stop_at="2026-07-06T20:15:00.000000Z")
        with TemporaryDirectory() as tmp:
            orch = self._paper_orch(tmp, feed)
            try:
                result = run_paper_session(
                    orchestrator=orch, feed=feed,
                    journal_dir=Path(tmp) / "journal",
                    session_date_et="2026-07-06",
                    report_dir=None,
                    utc_now_iso_fn=tm.utc_iso)
            finally:
                orch.close()
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(result.sod_clean)
            self.assertTrue(result.eod_clean)   # the runner's fallback ran it
            self.assertEqual(_reconcile_phases(Path(tmp) / "journal"),
                             ["sod", "eod"])
            self.assertIsNone(result.report_path)

    def test_halted_prior_journal_exits_4(self):
        from agent.risk.risk_ledger import RiskLedger
        from recorder.persistence import EventWriter

        tm = _TimeMachine("2026-07-06T19:58:00.000000Z")
        steps = [(10.0, (lambda: _quote(tm)))] * 2
        feed = _live_feed(tm, steps, stop_at="2026-07-06T20:15:00.000000Z")
        with TemporaryDirectory() as tmp:
            journal_dir = Path(tmp) / "journal"
            journal_dir.mkdir(parents=True)
            ledger = RiskLedger(
                EventWriter(journal_dir / "risk.jsonl", "run-prior",
                            clock=lambda: "2026-07-06T00:00:00.000000Z"),
                rules_hash="0" * 64)
            for from_state, to_state in (("monitoring", "flattening"),
                                         ("flattening", "halted")):
                ledger.record_kill_transition(
                    from_state=from_state, to_state=to_state, cause="drill",
                    generation=1, daily_loss_usd=None, drawdown_usd=None,
                    cap_usd=None, account_snapshot_id=None, stale_inputs=True,
                    flattened=(), failed=(), residual=(),
                    tradability_annotations=())
            orch = self._paper_orch(tmp, feed)
            try:
                result = run_paper_session(
                    orchestrator=orch, feed=feed, journal_dir=journal_dir,
                    session_date_et="2026-07-06", report_dir=None,
                    utc_now_iso_fn=tm.utc_iso)
            finally:
                orch.close()
            self.assertEqual(result.kill_state, "halted")
            self.assertEqual(result.exit_code, 4)


class TestStrategyRegistry(unittest.TestCase):
    def test_momentum_ids_resolve(self):
        v1 = build_strategy("directional.momentum_v1")
        v2 = build_strategy("directional.momentum_v2")
        self.assertEqual(v1.strategy_id, "directional.momentum_v1")
        self.assertEqual(v2.strategy_id, "directional.momentum_v2")
        self.assertFalse(v1.synthetic)
        self.assertIsNone(build_strategy(None))

    def test_cross_sectional_and_unknown_fail_closed(self):
        with self.assertRaises(ValueError) as ctx:
            build_strategy("relative_strength.long_only_proxy_v1")
        self.assertIn("CROSS-SECTIONAL", str(ctx.exception))
        with self.assertRaises(ValueError):
            build_strategy("no.such_strategy")


class TestDailyReport(unittest.TestCase):
    def test_golden_synthetic_journal_rolls_up(self):
        with TemporaryDirectory() as tmp:
            pipeline = run_synthetic_golden(Path(tmp))
            golden_run_id = pipeline.orch.run_id
            pipeline.close()
            report = build_daily_report(
                Path(tmp), run_id=golden_run_id, mode="synthetic",
                kill_state="monitoring",
                data_quality_counts={"off_universe_symbol": 1})
            trading = report["trading"]
            self.assertEqual(trading["position_opens"], 1)
            self.assertEqual(trading["position_closes"], 1)
            self.assertEqual(trading["realized_broker_pnl_usd"], "1.000000")
            self.assertEqual(trading["realized_modeled_pnl_usd"], "0.930000")
            self.assertEqual(trading["fees_usd"], "0.07")
            self.assertEqual(report["fills"]["broker_fills"], 4)
            self.assertEqual(report["fills"]["modeled_fills"], 2)
            self.assertEqual(report["fills"]["divergence_rows"], 2)
            self.assertEqual(report["data_quality"],
                             {"off_universe_symbol": 1})
            text = render_text(report)
            self.assertIn("broker_pnl=1.000000", text)
            self.assertIn("modeled_pnl=0.930000", text)

    def test_empty_journal_rolls_up_to_zeros(self):
        with TemporaryDirectory() as tmp:
            report = build_daily_report(Path(tmp))
            self.assertEqual(report["trading"]["position_opens"], 0)
            self.assertEqual(report["trading"]["realized_broker_pnl_usd"], "0")
            self.assertIsNone(report["reconcile"]["all_clean"])
            self.assertEqual(report["counts_by_stream"], {})


if __name__ == "__main__":
    unittest.main()
