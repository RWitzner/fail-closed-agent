"""M4 §M test 7 — the nine-case S8 drill of §I, verbatim.

Invariants: S8 (trip => flatten-then-halt, reduce-only only, ALWAYS halts, residual
retried, never an open), S1 (zero open authorizations), S6 (deterministic rows).
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import agent.execution_preflight as execution_preflight
from agent.broker.base import OrderIntent, require_token
from agent.execution_preflight import mint_reduce_only_token
from agent.market_state import Tradability
from agent.risk.account_state import AccountStore, parse_account_payload
from agent.risk.loss_limits import LossRead
from agent.risk.reasons import RiskError
from agent.risk.risk_config import RiskConfig
from agent.risk.risk_kill import FlattenReport, KillEvaluation, RiskKillSwitch
from agent.risk.risk_ledger import (
    EVT_KILL_FLATTEN_INCOMPLETE,
    EVT_KILL_RETRIP,
    EVT_KILL_RETRY,
    EVT_KILL_TRANSITION,
    RiskLedger,
    replay_risk,
)
from agent.serializer import BrokerUSD
from recorder.persistence import EventWriter
from tests.lib.fakes import FakeClock, SpyBroker
from tests.lib.risk_fixtures import (
    account_payload,
    permissive_fixture_config,
    portfolio_fixture,
    verdict_fixture,
)

_CLOCK = lambda: "2026-06-08T20:00:00.000000+00:00"  # noqa: E731


class SelectiveBroker:
    """Raises for the configured symbols; records every attempt at entry."""

    def __init__(self, fail_symbols=()):
        self.calls = []
        self.submitted = []
        self.fail_symbols = set(fail_symbols)

    def submit_order(self, intent, token):
        self.calls.append(intent)
        if intent.symbol in self.fail_symbols:
            raise RuntimeError("broker refused")
        require_token(intent, token)
        self.submitted.append(intent)
        return {"order_id": intent.intent_id, "status": "ok"}

    def positions(self):
        return {}

    def account(self):
        return {}


def _cfg():
    return RiskConfig.from_config(permissive_fixture_config())


def _fresh_account(equity="98000.00"):
    store = AccountStore(clock=FakeClock(start_ms=0))
    store.put(parse_account_payload(account_payload(equity=equity), source="fixture",
                                    seen_at_ms=0, ts_read_utc="2026-06-08T14:00:00Z"))
    return store.get()


def _degraded_account(status):
    if status == "missing":
        return AccountStore(clock=FakeClock()).get()
    clock = FakeClock(start_ms=0)
    store = AccountStore(clock=clock)
    if status == "stale":
        store.put(parse_account_payload(account_payload(), source="fixture",
                                        seen_at_ms=0, ts_read_utc="t"))
        clock.advance(5001)
    elif status == "invalid":
        store.put(parse_account_payload(account_payload(equity="NaN"), source="fixture",
                                        seen_at_ms=0, ts_read_utc="t"))
    elif status == "skew":
        store.put(parse_account_payload(account_payload(), source="fixture",
                                        seen_at_ms=100, ts_read_utc="t"))
    return store.get()


def _loss_read():
    return LossRead(hwm_equity=BrokerUSD("100000.00"), daily_loss_usd=None,
                    drawdown_usd=None, breaches=())


def _switch(tmpdir, run_id="run-1"):
    path = Path(tmpdir) / "risk.jsonl"
    ledger = RiskLedger(EventWriter(path, run_id, clock=_CLOCK), rules_hash="rh")
    return RiskKillSwitch(cfg=_cfg(), ledger=ledger), path


class TestS8Drill(unittest.TestCase):
    def test_case1_happy_flatten(self):
        with TemporaryDirectory() as tmpdir:
            switch, path = _switch(tmpdir)
            portfolio = portfolio_fixture("zero_qty_row")  # zero-qty dropped at parse
            account = _fresh_account("98000.00")           # daily loss 2000 > cap 1000
            evaluation = switch.evaluate(account, _loss_read())
            self.assertEqual(evaluation.cause, "daily_loss_cap")
            self.assertIs(evaluation.skipped, False)
            self.assertEqual(evaluation.daily_loss_usd, Decimal("2000.00"))
            broker = SpyBroker()
            report = switch.trigger(evaluation.cause, broker, portfolio,
                                    evaluation=evaluation, account=account)
            self.assertEqual(switch.state, "halted")
            self.assertEqual(switch.generation, 1)
            self.assertEqual(report.flattened, ("AAPL", "MSFT"))
            self.assertEqual(report.failed, ())
            self.assertEqual(report.residual, ())
            by_symbol = {i.symbol: i for i in broker.calls}
            self.assertEqual(len(broker.calls), 2)
            self.assertIs(by_symbol["AAPL"].is_reducing, True)
            self.assertEqual(by_symbol["AAPL"].side, "sell")          # long -> sell
            self.assertEqual(by_symbol["AAPL"].qty, Decimal("10"))    # |held|
            self.assertIs(by_symbol["MSFT"].is_reducing, True)
            self.assertEqual(by_symbol["MSFT"].side, "buy")           # short -> cover
            self.assertEqual(by_symbol["MSFT"].qty, Decimal("5"))
            rows = [r for r in replay_risk(path)
                    if r["event_type"] == EVT_KILL_TRANSITION]
            self.assertEqual([(r["from_state"], r["to_state"]) for r in rows],
                             [("monitoring", "flattening"), ("flattening", "halted")])
            self.assertEqual(rows[0]["cause"], "daily_loss_cap")
            self.assertEqual(rows[0]["daily_loss_usd"], "2000.00")
            self.assertEqual(rows[0]["cap_usd"], "1000")
            self.assertEqual(rows[0]["residual"], ["AAPL", "MSFT"])   # at-trigger set
            self.assertEqual(rows[0]["flattened"], [])
            self.assertEqual(rows[1]["flattened"], ["AAPL", "MSFT"])
            self.assertEqual(rows[1]["residual"], [])
            self.assertEqual(rows[0]["account_snapshot_id"],
                             account.read.account_snapshot_id)
            self.assertIs(rows[0]["stale_inputs"], False)

    def test_case2_failure_isolation_and_retry(self):
        with TemporaryDirectory() as tmpdir:
            switch, path = _switch(tmpdir)
            portfolio = portfolio_fixture("long_short")
            broker = SelectiveBroker(fail_symbols={"MSFT"})
            report = switch.trigger("operator_manual", broker, portfolio)
            self.assertEqual(switch.state, "halted")
            self.assertEqual(report.flattened, ("AAPL",))
            self.assertEqual(report.failed[0][0], "MSFT")
            self.assertEqual(report.residual, ("MSFT",))
            incomplete = [r for r in replay_risk(path)
                          if r["event_type"] == EVT_KILL_FLATTEN_INCOMPLETE]
            self.assertEqual(len(incomplete), 1)
            self.assertEqual(incomplete[0]["residual"], ["MSFT"])
            retry_broker = SpyBroker()
            retry = switch.retry_residual(retry_broker, portfolio)
            self.assertEqual(retry.residual, ())
            self.assertEqual(switch.residual_symbols(), ())
            self.assertEqual(len(retry_broker.calls), 1)              # ONLY MSFT
            self.assertEqual(retry_broker.calls[0].symbol, "MSFT")
            self.assertIs(retry_broker.calls[0].is_reducing, True)
            self.assertEqual(switch.state, "halted")                  # stays halted
            retry_rows = [r for r in replay_risk(path)
                          if r["event_type"] == EVT_KILL_RETRY]
            self.assertEqual(retry_rows[0]["residual_before"], ["MSFT"])
            self.assertEqual(retry_rows[0]["residual_after"], [])

    def test_case3_total_broker_failure_still_halts(self):
        with TemporaryDirectory() as tmpdir:
            switch, _ = _switch(tmpdir)
            portfolio = portfolio_fixture("long_short")
            broker = SelectiveBroker(fail_symbols={"AAPL", "MSFT"})
            report = switch.trigger("drill", broker, portfolio)
            self.assertEqual(switch.state, "halted")                  # M0 invariant
            self.assertEqual(sorted(s for s, _ in report.failed), ["AAPL", "MSFT"])
            self.assertEqual(report.residual, ("AAPL", "MSFT"))

    def test_case4_retrip_idempotency(self):
        with TemporaryDirectory() as tmpdir:
            switch, path = _switch(tmpdir)
            portfolio = portfolio_fixture("long_only")
            broker = SpyBroker()
            first = switch.trigger("drill", broker, portfolio)
            calls_after_first = len(broker.calls)
            second = switch.trigger("drill", broker, portfolio)
            self.assertEqual(len(broker.calls), calls_after_first)    # zero new submits
            self.assertEqual(switch.generation, 1)                    # +1 only once
            self.assertEqual(second, first)                           # prior report
            retrips = [r for r in replay_risk(path)
                       if r["event_type"] == EVT_KILL_RETRIP]
            self.assertEqual(len(retrips), 1)
            self.assertEqual(retrips[0]["current_state"], "halted")

    def test_case5_no_open_proof(self):
        with TemporaryDirectory() as tmpdir:
            switch, _ = _switch(tmpdir)
            portfolio = portfolio_fixture("long_short")
            broker = SpyBroker()
            with mock.patch.object(execution_preflight, "mint_open_token",
                                   side_effect=AssertionError("opened!")) as patched:
                switch.trigger("drill", broker, portfolio)
                patched.assert_not_called()                           # never called
            for auth in execution_preflight._authorizations.values():  # white-box
                self.assertNotEqual(auth.kind, "open")
            for intent in broker.calls:
                self.assertIs(intent.is_reducing, True)

    def test_case6_skip_not_trip(self):
        with TemporaryDirectory() as tmpdir:
            switch, path = _switch(tmpdir)
            for status in ("stale", "missing", "invalid", "skew"):
                evaluation = switch.evaluate(_degraded_account(status), _loss_read())
                self.assertIs(evaluation.skipped, True, status)
                self.assertIsNone(evaluation.cause, status)
                self.assertEqual(switch.state, "monitoring", status)  # no transition
                # paired: the reduce mint still succeeds under this degraded state
                held = portfolio_fixture("long_only").positions[0]
                intent = OrderIntent(symbol="AAPL", side="sell", qty=Decimal("10"),
                                     is_reducing=True, intent_id=f"r-{status}")
                token = mint_reduce_only_token(held, intent)
                self.assertIsNotNone(token, status)
            self.assertEqual(replay_risk(path), [])                   # no submit, no row

    def test_case7_rehydrate_latch(self):
        with TemporaryDirectory() as tmpdir:
            switch, path = _switch(tmpdir)
            portfolio = portfolio_fixture("long_short")
            switch.trigger("daily_loss_cap", SelectiveBroker({"MSFT"}), portfolio)
            rows = replay_risk(path)
            fresh = RiskKillSwitch(cfg=_cfg(), ledger=None)
            fresh.rehydrate(rows)
            self.assertEqual(fresh.state, "halted")                   # latched (FD-M4-19)
            self.assertEqual(fresh.generation, switch.generation)
            self.assertEqual(fresh.residual_symbols(), ("MSFT",))

    def test_case8_tradability_annotation_changes_nothing(self):
        with TemporaryDirectory() as tmpdir:
            switch, path = _switch(tmpdir)
            portfolio = portfolio_fixture("long_short")
            broker = SpyBroker()
            verdict = verdict_fixture("AAPL", Tradability.NOT_TRADABLE)
            report = switch.trigger("drill", broker, portfolio,
                                    tradability={"AAPL": verdict})
            # FD-M4-20: the NOT_TRADABLE verdict changes NOTHING about the submit set
            self.assertEqual(sorted(i.symbol for i in broker.calls), ["AAPL", "MSFT"])
            self.assertEqual(report.flattened, ("AAPL", "MSFT"))
            rows = [r for r in replay_risk(path)
                    if r["event_type"] == EVT_KILL_TRANSITION]
            self.assertEqual(rows[1]["tradability_annotations"],
                             [["AAPL", "not_tradable"]])
        with TemporaryDirectory() as tmpdir:
            switch, path = _switch(tmpdir)
            switch.trigger("drill", SpyBroker(), portfolio_fixture("long_only"))
            rows = [r for r in replay_risk(path)
                    if r["event_type"] == EVT_KILL_TRANSITION]
            # kwarg omitted: annotations [] and measured-value fields null (M4C-1)
            self.assertEqual(rows[0]["tradability_annotations"], [])
            self.assertIsNone(rows[0]["daily_loss_usd"])
            self.assertIsNone(rows[0]["drawdown_usd"])
            self.assertIsNone(rows[0]["cap_usd"])
            self.assertIsNone(rows[0]["account_snapshot_id"])
            self.assertIs(rows[0]["stale_inputs"], True)

    def test_case9_crash_mid_flatten_replay(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "risk.jsonl"
            ledger = RiskLedger(EventWriter(path, "run-1", clock=_CLOCK),
                                rules_hash="rh")
            # journal truncated immediately after the monitoring->flattening row
            ledger.record_kill_transition(
                from_state="monitoring", to_state="flattening", cause="daily_loss_cap",
                generation=3, stale_inputs=False, flattened=(), failed=(),
                residual=("AAPL", "MSFT"), tradability_annotations=())
            replay_path = Path(tmpdir) / "risk2.jsonl"
            ledger2 = RiskLedger(EventWriter(replay_path, "run-2", clock=_CLOCK),
                                 rules_hash="rh")
            switch = RiskKillSwitch(cfg=_cfg(), ledger=ledger2)
            switch.rehydrate(replay_risk(path))
            self.assertEqual(switch.state, "halted")                  # safety-F5
            self.assertEqual(switch.generation, 3)
            self.assertEqual(switch.residual_symbols(), ("AAPL", "MSFT"))
            incomplete = [r for r in replay_risk(replay_path)
                          if r["event_type"] == EVT_KILL_FLATTEN_INCOMPLETE]
            self.assertEqual(len(incomplete), 1)
            self.assertEqual(incomplete[0]["residual"], ["AAPL", "MSFT"])
            self.assertEqual(incomplete[0]["failed"], [])
            # retry_residual is legal and submits ONLY reduce-only orders
            broker = SpyBroker()
            retry = switch.retry_residual(broker, portfolio_fixture("long_short"))
            self.assertEqual(retry.residual, ())
            for intent in broker.calls:
                self.assertIs(intent.is_reducing, True)
            # a retrip after the rehydrate returns a synthesized report, resubmits nothing
            switch2 = RiskKillSwitch(cfg=_cfg(), ledger=None)
            switch2.rehydrate(replay_risk(path))
            broker2 = SpyBroker()
            report = switch2.trigger("drill", broker2, portfolio_fixture("long_short"))
            self.assertIsInstance(report, FlattenReport)
            self.assertEqual(report.residual, ("AAPL", "MSFT"))
            self.assertEqual(broker2.calls, [])                       # resubmits nothing


class TestRehydrateMarkerGuard(unittest.TestCase):
    """Harden round 1, M4-R1-F2: the kill_flatten_incomplete marker fires only
    while exposure genuinely remains (rebuilt residual non-empty)."""

    def test_no_marker_after_fully_successful_retry(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "risk.jsonl"
            ledger = RiskLedger(EventWriter(path, "run-1", clock=_CLOCK),
                                rules_hash="rh")
            ledger.record_kill_transition(
                from_state="monitoring", to_state="flattening", cause="daily_loss_cap",
                generation=3, stale_inputs=False, flattened=(), failed=(),
                residual=("AAPL", "MSFT"), tradability_annotations=())
            # the retry fully succeeded (journaled in the same stream)
            ledger.record_kill_retry_residual(
                generation=3, residual_before=("AAPL", "MSFT"),
                residual_after=(), flattened=("AAPL", "MSFT"), failed=())
            out_path = Path(tmpdir) / "risk2.jsonl"
            ledger2 = RiskLedger(EventWriter(out_path, "run-2", clock=_CLOCK),
                                 rules_hash="rh")
            switch = RiskKillSwitch(cfg=_cfg(), ledger=ledger2)
            switch.rehydrate(replay_risk(path))
            self.assertEqual(switch.state, "halted")          # latch unaffected
            self.assertEqual(switch.residual_symbols(), ())
            markers = [r for r in replay_risk(out_path)
                       if r["event_type"] == EVT_KILL_FLATTEN_INCOMPLETE]
            self.assertEqual(markers, [])                     # flatten IS complete

    def test_partial_retry_marks_remaining_residual_only(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "risk.jsonl"
            ledger = RiskLedger(EventWriter(path, "run-1", clock=_CLOCK),
                                rules_hash="rh")
            ledger.record_kill_transition(
                from_state="monitoring", to_state="flattening", cause="daily_loss_cap",
                generation=3, stale_inputs=False, flattened=(), failed=(),
                residual=("AAPL", "MSFT"), tradability_annotations=())
            ledger.record_kill_retry_residual(
                generation=3, residual_before=("AAPL", "MSFT"),
                residual_after=("MSFT",), flattened=("AAPL",), failed=())
            out_path = Path(tmpdir) / "risk2.jsonl"
            ledger2 = RiskLedger(EventWriter(out_path, "run-2", clock=_CLOCK),
                                 rules_hash="rh")
            switch = RiskKillSwitch(cfg=_cfg(), ledger=ledger2)
            switch.rehydrate(replay_risk(path))
            markers = [r for r in replay_risk(out_path)
                       if r["event_type"] == EVT_KILL_FLATTEN_INCOMPLETE]
            self.assertEqual(len(markers), 1)
            self.assertEqual(markers[0]["residual"], ["MSFT"])


class TestVocabAndDeterminism(unittest.TestCase):
    def test_out_of_vocab_and_reserved_cause_raise(self):
        with TemporaryDirectory() as tmpdir:
            switch, _ = _switch(tmpdir)
            with self.assertRaises(RiskError):
                switch.trigger("panic", SpyBroker(), portfolio_fixture("flat"))
            with self.assertRaises(RiskError):
                switch.trigger("live_gate_flip", SpyBroker(),
                               portfolio_fixture("flat"))  # reserved (M8)
            self.assertEqual(switch.state, "monitoring")

    def test_retry_illegal_outside_halted_or_without_residual(self):
        with TemporaryDirectory() as tmpdir:
            switch, _ = _switch(tmpdir)
            with self.assertRaises(RiskError):
                switch.retry_residual(SpyBroker(), portfolio_fixture("long_only"))

    def test_transition_rows_byte_deterministic(self):
        def run():
            with TemporaryDirectory() as tmpdir:
                switch, path = _switch(tmpdir, run_id="run-det")
                account = _fresh_account()
                evaluation = switch.evaluate(account, _loss_read())
                switch.trigger("daily_loss_cap", SpyBroker(),
                               portfolio_fixture("long_short"),
                               evaluation=evaluation, account=account)
                return [json.dumps(r, sort_keys=True) for r in replay_risk(path)]
        self.assertEqual(run(), run())

    def test_evaluate_drawdown_and_baseline(self):
        with TemporaryDirectory() as tmpdir:
            switch, _ = _switch(tmpdir)
            account = _fresh_account("99500.00")  # daily loss 500 <= 1000
            loss = LossRead(hwm_equity=BrokerUSD("102000.00"), daily_loss_usd=None,
                            drawdown_usd=None, breaches=())
            evaluation = switch.evaluate(account, loss)   # drawdown 2500 > 2000
            self.assertEqual(evaluation.cause, "drawdown_cap")
            self.assertEqual(evaluation.drawdown_usd, Decimal("2500.00"))
            no_baseline = LossRead(hwm_equity=None, daily_loss_usd=None,
                                   drawdown_usd=None, breaches=())
            evaluation = switch.evaluate(account, no_baseline)
            self.assertIsNone(evaluation.cause)           # no HWM -> no drawdown trip
            self.assertIsNone(evaluation.drawdown_usd)

    def test_daily_loss_checked_before_drawdown(self):
        with TemporaryDirectory() as tmpdir:
            switch, _ = _switch(tmpdir)
            account = _fresh_account("90000.00")
            loss = LossRead(hwm_equity=BrokerUSD("102000.00"), daily_loss_usd=None,
                            drawdown_usd=None, breaches=())
            evaluation = switch.evaluate(account, loss)
            self.assertEqual(evaluation.cause, "daily_loss_cap")  # first breach wins

    def test_kill_evaluation_is_frozen(self):
        evaluation = KillEvaluation(cause=None, skipped=True, daily_loss_usd=None,
                                    drawdown_usd=None)
        with self.assertRaises(Exception):
            evaluation.cause = "drill"


if __name__ == "__main__":
    unittest.main()
