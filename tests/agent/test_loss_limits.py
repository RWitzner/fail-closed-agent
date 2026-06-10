"""M4 §M test 6 — daily-loss + HWM drawdown monitor.

Invariants: R14 (strict '>'; cap 0 = zero budget; no trip on degraded reads),
FD-M4-18 (last_equity basis; run-lifetime HWM, journaled + rehydrated).
"""
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.risk.account_state import AccountRead, AccountStore, parse_account_payload
from agent.risk.loss_limits import LossLimitsMonitor, LossRead, TripSignal
from agent.risk.risk_config import RiskConfig
from agent.risk.risk_ledger import EVT_HWM_UPDATE, RiskLedger, replay_risk
from recorder.persistence import EventWriter
from tests.lib.fakes import FakeClock
from tests.lib.risk_fixtures import account_payload, permissive_fixture_config

_CLOCK = lambda: "2026-06-08T20:00:00.000000+00:00"  # noqa: E731


def _cfg(daily=1000, drawdown=2000):
    config = permissive_fixture_config()
    config["risk_rules"]["caps"]["max_daily_loss_usd"] = daily
    config["risk_rules"]["caps"]["max_drawdown_usd"] = drawdown
    return RiskConfig.from_config(config)


def _fresh(equity, last_equity="100000.00", seen_at_ms=0):
    store = AccountStore(clock=FakeClock(start_ms=seen_at_ms))
    store.put(parse_account_payload(
        account_payload(equity=equity, last_equity=last_equity),
        source="fixture", seen_at_ms=seen_at_ms,
        ts_read_utc="2026-06-08T14:00:00.000000Z"))
    return store.get()


def _degraded(status):
    if status == "missing":
        return AccountStore(clock=FakeClock()).get()
    if status == "stale":
        clock = FakeClock(start_ms=0)
        store = AccountStore(clock=clock)
        store.put(parse_account_payload(account_payload(), source="fixture",
                                        seen_at_ms=0, ts_read_utc="t"))
        clock.advance(5001)
        return store.get()
    if status == "invalid":
        store = AccountStore(clock=FakeClock(start_ms=0))
        store.put(parse_account_payload(account_payload(equity="NaN"), source="fixture",
                                        seen_at_ms=0, ts_read_utc="t"))
        return store.get()
    if status == "skew":
        store = AccountStore(clock=FakeClock(start_ms=0))
        store.put(parse_account_payload(account_payload(), source="fixture",
                                        seen_at_ms=100, ts_read_utc="t"))
        return store.get()
    raise AssertionError(status)


class TestLossLimits(unittest.TestCase):
    def test_hwm_none_before_first_fresh_observation(self):
        monitor = LossLimitsMonitor(cfg=_cfg(), ledger=None)
        read = monitor.read()
        self.assertIsInstance(read, LossRead)
        self.assertIsNone(read.hwm_equity)
        self.assertIsNone(read.daily_loss_usd)
        self.assertIsNone(read.drawdown_usd)
        self.assertEqual(read.breaches, ())

    def test_daily_loss_strict_boundary(self):
        monitor = LossLimitsMonitor(cfg=_cfg(daily=1000), ledger=None)
        # loss == cap passes
        signal = monitor.observe(_fresh("99000.00"), session_date_et="2026-06-08")
        self.assertIsNone(signal)
        self.assertEqual(monitor.read().daily_loss_usd, Decimal("1000.00"))
        self.assertEqual(monitor.read().breaches, ())
        # +0.01 trips
        signal = monitor.observe(_fresh("98999.99"), session_date_et="2026-06-08")
        self.assertIsInstance(signal, TripSignal)
        self.assertEqual(signal.cause, "daily_loss_cap")
        self.assertEqual(signal.measured_usd, Decimal("1000.01"))
        self.assertEqual(signal.cap_usd, Decimal("1000"))
        self.assertEqual(signal.basis, "last_equity")
        self.assertEqual(signal.session_date_et, "2026-06-08")
        self.assertIn("daily_loss_breached", monitor.read().breaches)

    def test_drawdown_strict_boundary_and_hwm_monotonic(self):
        monitor = LossLimitsMonitor(cfg=_cfg(daily=100000, drawdown=2000), ledger=None)
        monitor.observe(_fresh("100000.00"), session_date_et="2026-06-08")
        monitor.observe(_fresh("103000.00"), session_date_et="2026-06-08")  # new high
        self.assertEqual(monitor.read().hwm_equity, Decimal("103000.00"))
        # drawdown == cap passes (103000 - 101000 = 2000)
        signal = monitor.observe(_fresh("101000.00"), session_date_et="2026-06-08")
        self.assertIsNone(signal)
        # +0.01 trips
        signal = monitor.observe(_fresh("100999.99"), session_date_et="2026-06-08")
        self.assertIsInstance(signal, TripSignal)
        self.assertEqual(signal.cause, "drawdown_cap")
        self.assertEqual(signal.measured_usd, Decimal("2000.01"))
        self.assertEqual(signal.basis, "high_water_mark")
        # HWM never decreased
        self.assertEqual(monitor.read().hwm_equity, Decimal("103000.00"))

    def test_daily_loss_checked_first_one_signal_per_call(self):
        monitor = LossLimitsMonitor(cfg=_cfg(daily=10, drawdown=10), ledger=None)
        monitor.observe(_fresh("100000.00"), session_date_et="2026-06-08")  # HWM base
        signal = monitor.observe(_fresh("90000.00"), session_date_et="2026-06-08")
        self.assertEqual(signal.cause, "daily_loss_cap")
        self.assertEqual(sorted(monitor.read().breaches),
                         ["daily_loss_breached", "drawdown_breached"])

    def test_cap_zero_is_zero_budget_never_disabled(self):
        monitor = LossLimitsMonitor(cfg=_cfg(daily=0, drawdown=0), ledger=None)
        signal = monitor.observe(_fresh("99999.99"), session_date_et="2026-06-08")
        self.assertIsInstance(signal, TripSignal)  # any positive loss trips
        self.assertEqual(signal.cause, "daily_loss_cap")
        monitor2 = LossLimitsMonitor(cfg=_cfg(daily=0, drawdown=0), ledger=None)
        self.assertIsNone(monitor2.observe(_fresh("100000.00"),
                                           session_date_et="2026-06-08"))  # 0 loss passes

    def test_degraded_reads_update_nothing_and_never_trip(self):
        for status in ("missing", "stale", "invalid", "skew"):
            monitor = LossLimitsMonitor(cfg=_cfg(daily=0, drawdown=0), ledger=None)
            signal = monitor.observe(_degraded(status), session_date_et="2026-06-08")
            self.assertIsNone(signal, status)
            self.assertIsNone(monitor.read().hwm_equity, status)

    def test_hwm_rows_sparse_and_rehydrate(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "risk.jsonl"
            ledger = RiskLedger(EventWriter(path, "run-1", clock=_CLOCK),
                                rules_hash="rh")
            monitor = LossLimitsMonitor(cfg=_cfg(daily=100000, drawdown=100000),
                                        ledger=ledger)
            monitor.observe(_fresh("100000.00"), session_date_et="2026-06-08")
            monitor.observe(_fresh("99000.00"), session_date_et="2026-06-08")   # no row
            monitor.observe(_fresh("103000.00"), session_date_et="2026-06-08")  # new high
            rows = [r for r in replay_risk(path) if r["event_type"] == EVT_HWM_UPDATE]
            self.assertEqual(len(rows), 2)  # edge-triggered, sparse
            self.assertEqual(rows[0]["hwm_equity"], "100000.00")
            self.assertEqual(rows[1]["hwm_equity"], "103000.00")
            fresh_monitor = LossLimitsMonitor(cfg=_cfg(), ledger=None)
            fresh_monitor.rehydrate(replay_risk(path))
            self.assertEqual(fresh_monitor.read().hwm_equity, Decimal("103000.00"))

    def test_trip_signal_field_exactness(self):
        monitor = LossLimitsMonitor(cfg=_cfg(daily=1000), ledger=None)
        account = _fresh("98000.00")
        signal = monitor.observe(account, session_date_et="2026-06-08")
        self.assertEqual(signal.equity, Decimal("98000.00"))
        self.assertEqual(signal.account_snapshot_id, account.read.account_snapshot_id)
        self.assertEqual(signal.measured_usd, Decimal("2000.00"))


if __name__ == "__main__":
    unittest.main()
