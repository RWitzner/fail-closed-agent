"""S1: on the COMMITTED config, nothing opens — at the Broker boundary.

This is the load-bearing safety canary. It loads the real committed config files
(not a fixture) and proves: the run gates are identity-`False`, no opening order
can be minted, a simulated decision loop never reaches the broker, and a
tighten-only overlay cannot loosen the committed gates.
"""
import unittest
from decimal import Decimal
from pathlib import Path

from agent.config import load, tighten_only_merge
from agent.execution_preflight import PreflightRejected, mint_open_token
from agent.gates import live_allowed, opening_allowed
from agent.broker.base import OrderIntent
from tests.lib.fakes import SpyBroker

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / "config"
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


def _committed_config():
    return {
        "agent_rules": load(_CONFIG / "agent_rules.json"),
        "risk_rules": load(_CONFIG / "risk_rules.json"),
    }


class TestCommittedConfigCanary(unittest.TestCase):
    def test_committed_gates_are_identity_false(self):
        cfg = _committed_config()
        self.assertIs(cfg["agent_rules"]["enabled"], False)
        self.assertIs(cfg["agent_rules"]["paper_trading"]["enabled"], False)
        self.assertIs(cfg["risk_rules"]["live_trading"]["enabled"], False)

    def test_committed_config_forbids_opening_and_live(self):
        cfg = _committed_config()
        self.assertFalse(opening_allowed(cfg))
        self.assertFalse(live_allowed(cfg))

    def test_no_opening_submit_and_zero_total_on_committed_config(self):
        # FD-M5-14: the mint takes PreflightInputs; on the committed config every
        # preflight terminates at run_gates with the byte-exact terminal shape (S1).
        from agent.exec_reasons import PREFLIGHT_STAGES
        from tests.agent.test_execution_preflight_m5 import (
            golden_candidate,
            golden_inputs,
        )

        cfg = _committed_config()
        broker = SpyBroker()
        for sym in ["AAPL", "MSFT", "NVDA"]:
            intent = OrderIntent(symbol=sym, side="buy", qty=Decimal("1"), intent_id=f"o-{sym}")
            try:
                token, _ = mint_open_token(golden_inputs(
                    gates_config=cfg,
                    candidate=golden_candidate(symbol=sym, qty="1")))
            except PreflightRejected as caught:
                reject = caught.reject
                self.assertEqual(reject.reasons, ("run_gates_off",))
                self.assertEqual(reject.gate_stage, "run_gates")
                self.assertEqual(reject.stages_skipped, PREFLIGHT_STAGES[1:])
                self.assertIsNone(reject.capped_limit)
                continue  # cannot open -> the broker is never reached
            broker.submit_order(intent, token)  # unreachable on committed config
        self.assertEqual(broker.calls, [])
        self.assertEqual(broker.submitted, [])

    def test_malformed_live_block_reads_as_off(self):
        # live_trading.enabled = "true" (a string) must read False (identity-strict).
        malformed = load(_FIXTURES / "malformed_live_block.json")
        self.assertFalse(live_allowed(malformed))

    def test_armed_overlay_cannot_loosen_committed_via_tighten_only(self):
        overlay = load(_FIXTURES / "local_armed_overlay.json")
        merged = tighten_only_merge(_committed_config(), overlay)
        self.assertFalse(opening_allowed(merged))
        self.assertFalse(live_allowed(merged))


class TestRiskRulesCanary(unittest.TestCase):
    """M4 §M test 10 extension (S1, R5): the committed risk_rules additions stay
    fully shut, and a hostile overlay merges back to the committed values."""

    _CAP_KEYS = (
        "max_position_usd", "max_gross_exposure_usd", "max_net_exposure_usd",
        "max_daily_loss_usd", "max_drawdown_usd", "max_sector_exposure_usd",
        "max_abs_beta_notional_usd",
    )

    def test_committed_caps_all_integer_zero(self):
        caps = _committed_config()["risk_rules"]["caps"]
        self.assertEqual(set(caps), set(self._CAP_KEYS))
        for name in self._CAP_KEYS:
            self.assertIsInstance(caps[name], int, name)
            self.assertNotIsInstance(caps[name], bool, name)
            self.assertEqual(caps[name], 0, name)

    def test_committed_short_selling_identity_false_and_universe_empty(self):
        risk = _committed_config()["risk_rules"]["risk"]
        self.assertIs(risk["short_selling"]["enabled"], False)
        self.assertEqual(risk["universe"], {})

    def test_committed_gates_still_identity_false_with_risk_block_present(self):
        # extends, never replaces, the M0 assertions
        cfg = _committed_config()
        self.assertIs(cfg["agent_rules"]["enabled"], False)
        self.assertIs(cfg["agent_rules"]["paper_trading"]["enabled"], False)
        self.assertIs(cfg["risk_rules"]["live_trading"]["enabled"], False)
        self.assertFalse(opening_allowed(cfg))
        self.assertFalse(live_allowed(cfg))

    def test_risk_armed_overlay_merges_back_to_committed_values(self):
        overlay = load(_FIXTURES / "risk_armed_overlay.json")
        committed = _committed_config()
        merged = tighten_only_merge(committed, overlay)
        self.assertFalse(opening_allowed(merged))
        self.assertFalse(live_allowed(merged))
        caps = merged["risk_rules"]["caps"]
        self.assertEqual(set(caps), set(self._CAP_KEYS))  # injected key dropped
        for name in self._CAP_KEYS:
            self.assertEqual(caps[name], 0, name)         # raised caps refused
        risk = merged["risk_rules"]["risk"]
        self.assertIs(risk["short_selling"]["enabled"], False)
        self.assertEqual(risk["universe"], {})            # altered universe refused
        self.assertNotIn("locate_provider", risk)         # injected key dropped
        self.assertEqual(merged, committed)               # byte-equal merge-back

    def test_committed_config_can_open_always_terminates_at_run_gates(self):
        # S1 composition at the can_open chokepoint, on the REAL committed JSON.
        from agent.risk.account_state import AccountStore, parse_account_payload
        from agent.risk.can_open import RiskEngine
        from agent.risk.intraday_margin import FreezeState, MarginRead
        from agent.risk.loss_limits import LossRead
        from agent.risk.pdt_compat import PdtRead
        from agent.risk.risk_config import RiskConfig
        from agent.candidate import Candidate, Leg
        from agent.serializer import BrokerUSD
        from tests.lib.fakes import FakeClock
        from tests.lib.risk_fixtures import account_payload, portfolio_fixture

        cfg = _committed_config()
        engine = RiskEngine(cfg=RiskConfig.from_config(cfg), gates_config=cfg,
                            run_id="run-canary")
        store = AccountStore(clock=FakeClock(start_ms=0))
        store.put(parse_account_payload(account_payload(), source="fixture",
                                        seen_at_ms=0, ts_read_utc="t"))
        candidate = Candidate(strategy_id="s1", legs=(
            Leg(symbol="AAPL", instrument_id=1001, side="buy",
                qty=Decimal("1"), limit_price=Decimal("190.00")),),
            paper_eligible=True, score=None)
        verdict = engine.can_open(
            candidate, portfolio_fixture("flat"), store.get(),
            market_state={}, kill_state="monitoring", kill_generation=0,
            margin_read=MarginRead(outstanding_nonminor=(),
                                   freeze=FreezeState(False, None, None, None),
                                   asof_session_date_et="2026-06-08"),
            pdt_read=PdtRead(state="unknown", evidence=None, rejection_latched=False),
            loss_read=LossRead(hwm_equity=BrokerUSD("100000.00"), daily_loss_usd=None,
                               drawdown_usd=None, breaches=()),
            now_ms=0)
        self.assertIs(verdict.allowed, False)
        self.assertEqual(verdict.gate_stage, "run_gates")
        self.assertEqual(verdict.reasons, ("run_gates_off",))
        self.assertEqual(verdict.legs, ())
        self.assertEqual(verdict.gross_notional, Decimal("0"))
        self.assertEqual(verdict.caps_used, ())
        self.assertIsNone(verdict.session_date_et)


class TestExecutionConfigCanary(unittest.TestCase):
    """M5 §R test 12 (S1, M5C-T2/S12): the committed execution block reads as
    committed; a hostile overlay is DEFANGED per knob (the min()-merge DOES
    take effect — the defense is the floor or the parse error, never the
    merge); the committed artifacts/backtests/ dir holds ONLY .gitkeep."""

    _COMMITTED_EXECUTION = {
        "slippage_cap_bps": 25,
        "order_poll_interval_ms": 500,
        "account_refresh_interval_ms": 2500,
        "max_open_orders": 1,
    }

    def test_committed_execution_block_reads_as_committed(self):
        from agent.execution_config import ExecutionConfig

        cfg = _committed_config()
        self.assertEqual(cfg["agent_rules"]["execution"],
                         self._COMMITTED_EXECUTION)
        self.assertEqual(cfg["agent_rules"]["latency_budget_ms"], 250)
        parsed = ExecutionConfig.from_config(cfg)
        self.assertEqual(parsed.slippage_cap_bps, Decimal("25"))
        self.assertEqual(parsed.order_poll_interval_ms, 500)
        self.assertEqual(parsed.account_refresh_interval_ms, 2500)
        self.assertEqual(parsed.max_open_orders, 1)
        self.assertEqual(parsed.effective_latency_budget_ms, 250)

    def test_hostile_execution_overlay_defanged_per_knob(self):
        # M5C-T2 exact semantics: tighten_only_merge min()s non-bool numerics,
        # so the lowering latency overlay DOES land in the merged dict; the
        # floor (not the merge) defends. Raised knobs merge back via min();
        # gates AND-merge back to False; injected keys are dropped.
        from agent.execution_config import ExecutionConfig

        committed = _committed_config()
        overlay = {"agent_rules": {
            "enabled": True,                            # gates true
            "paper_trading": {"enabled": True},
            "latency_budget_ms": 1,                     # lowering DOES merge
            "execution": {
                "slippage_cap_bps": 10000,              # raised: min() keeps 25
                "max_open_orders": 99,                  # raised: min() keeps 1
                "injected_knob": 1,                     # dropped by the merge
            },
        }}
        merged = tighten_only_merge(committed, overlay)
        # gates: AND-merge keeps identity-False.
        self.assertIs(merged["agent_rules"]["enabled"], False)
        self.assertIs(merged["agent_rules"]["paper_trading"]["enabled"], False)
        self.assertFalse(opening_allowed(merged))
        # raised knobs: min() keeps the committed values; injected key dropped.
        self.assertEqual(merged["agent_rules"]["execution"],
                         self._COMMITTED_EXECUTION)
        # the polarity trap: latency 1 MERGES to 1 (min() took effect) ...
        self.assertEqual(merged["agent_rules"]["latency_budget_ms"], 1)
        # ... and the parser FLOORS it to 250 (the defense is the floor).
        parsed = ExecutionConfig.from_config(merged)
        self.assertEqual(parsed.effective_latency_budget_ms, 250)
        self.assertEqual(parsed.slippage_cap_bps, Decimal("25"))
        self.assertEqual(parsed.max_open_orders, 1)

    def test_hostile_latency_zero_overlay_fails_loud_at_parse(self):
        # M5C-T2: latency 0 merges to 0 and the parser raises ValueError at
        # startup — the floor only applies to values that PARSE.
        from agent.execution_config import ExecutionConfig

        merged = tighten_only_merge(
            _committed_config(), {"agent_rules": {"latency_budget_ms": 0}})
        self.assertEqual(merged["agent_rules"]["latency_budget_ms"], 0)
        with self.assertRaises(ValueError):
            ExecutionConfig.from_config(merged)

    def test_committed_artifacts_backtests_dir_is_gitkeep_only(self):
        # M5C-S12/FD-M5-27: the S9 premise — NO committed artifact exists, so
        # every real-strategy open rejects backtest_artifact_missing.
        entries = sorted(p.name for p in (_ROOT / "artifacts" / "backtests").iterdir())
        self.assertEqual(entries, [".gitkeep"])


class TestFullOrchestratorS1Canary(unittest.TestCase):
    """M5 §R test 12 — the full-orchestrator S1 canary, TWO compositions
    (M5C-T3/S9) + the observe no-broker assertion (M5C-T10). All compositions
    run the REAL orchestrator over the REAL committed config with the
    run-gates file ABSENT."""

    def setUp(self):
        import shutil
        import tempfile

        from agent.execution_preflight import unbind_runtime
        from tests.agent.test_execution_preflight_m5 import (
            purge_open_authorizations,
        )

        self.tmp = Path(tempfile.mkdtemp(prefix="m5-canary-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(unbind_runtime)          # teardown hygiene
        purge_open_authorizations()              # start open-kind clean
        self.addCleanup(purge_open_authorizations)

    @staticmethod
    def _registry():
        from agent import execution_preflight
        return execution_preflight._authorizations

    @staticmethod
    def _broker_instances(root, *, max_depth=8):
        """Bounded object-graph walk: every Broker-Protocol instance reachable
        from the orchestrator's attributes (M5C-T10)."""
        import types

        from agent.broker.base import Broker

        seen, found = set(), []

        def walk(obj, depth):
            if depth > max_depth or id(obj) in seen:
                return
            seen.add(id(obj))
            if isinstance(obj, (types.ModuleType, type)):
                return
            try:
                if isinstance(obj, Broker):
                    found.append(obj)
            except TypeError:
                pass
            if isinstance(obj, dict):
                for value in list(obj.values()):
                    walk(value, depth + 1)
            elif isinstance(obj, (list, tuple, set, frozenset)):
                for value in obj:
                    walk(value, depth + 1)
            attrs = getattr(obj, "__dict__", None)
            if isinstance(attrs, dict):
                for value in list(attrs.values()):
                    walk(value, depth + 1)

        walk(root, 0)
        return found

    def test_s1_canary_a_committed_config_spybroker_never_reached(self):
        # (a) committed config + ABSENT run-gates file + would_open-rich
        # replay + injected SpyBroker => broker.calls == [] (zero submits of
        # ANY kind at the Protocol boundary), preflight registry empty
        # afterwards, M3 decision rows > 0 (the probe ran).
        from tests.lib.exec_fixtures import (
            ExecPipeline,
            committed_assembled_config,
        )

        baseline = set(self._registry())
        broker = SpyBroker()
        pipeline = ExecPipeline(
            journal_dir=self.tmp / "canary-a", run_id="run-s1-canary-a",
            broker=broker, config=committed_assembled_config())
        try:
            for index in range(50, 59):
                pipeline.tick_on_bar(index)
        finally:
            pipeline.close()
        self.assertEqual(pipeline.orch.mode, "paper")
        # the run-gates file was ABSENT: the provenance row says so.
        gates_rows = pipeline.rows_of("status", "run_gates_file")
        self.assertEqual(len(gates_rows), 1)
        self.assertIs(gates_rows[0]["present"], False)
        self.assertIs(gates_rows[0]["enabled"], False)
        self.assertIs(gates_rows[0]["paper_enabled"], False)
        # zero submits of ANY kind at the Broker Protocol boundary.
        self.assertEqual(broker.calls, [])
        self.assertEqual(broker.submitted, [])
        self.assertEqual(broker.cancel_calls, [])
        # the preflight registry is empty afterwards: nothing minted.
        self.assertEqual(set(self._registry()) - baseline, set())
        self.assertEqual(
            [auth for auth in self._registry().values()
             if auth.kind == "open"], [])
        # the probe DID run: M3 decision rows > 0 on the committed config.
        decisions = pipeline.rows_of("decisions", "decision")
        self.assertGreater(len(decisions), 0)
        # ... and the exec order path journaled nothing.
        self.assertEqual(pipeline.rows_of("orders", "order_submit_attempt"), [])
        self.assertEqual(pipeline.rows_of("orders", "order_submitted"), [])

    def test_s1_canary_b_gates_consulting_variant_stops_at_run_gates(self):
        # (b) the GATES-CONSULTING variant (RC-4): committed config + ABSENT
        # run-gates file + RealStrategyStub + FakeBroker => the scan RUNS (no
        # gates pre-check, M5C-S9) and EVERY decision journals a can_open
        # refusal terminating at run_gates (reasons=("run_gates_off",)); zero
        # preflight mints, zero broker calls, zero open-kind registry entries.
        # A regression dropping any single gate layer moves this canary.
        from unittest import mock

        from agent import orchestrator as orch_mod
        from agent.broker.fake import FakeBroker
        from tests.lib.exec_fixtures import (
            ExecPipeline,
            RealStrategyStub,
            committed_assembled_config,
        )
        from tests.lib.fakes import FakeClock

        class _NoQuoteView:
            def latest(self, symbol, instrument_id):
                return None

        broker = FakeBroker(quote_view=_NoQuoteView(), clock=FakeClock(0),
                            instrument_ids={"AAPL": 1001})
        submit_spy = mock.MagicMock(wraps=broker.submit_order)
        cancel_spy = mock.MagicMock(wraps=broker.cancel_order)
        broker.submit_order = submit_spy
        broker.cancel_order = cancel_spy
        stub = RealStrategyStub([{"on_bar": 1, "qty": "10"},
                                 {"on_bar": 2, "qty": "10"},
                                 {"on_bar": 3, "qty": "10"}])
        mints = []
        real_mint = orch_mod.mint_open_token

        def mint_spy(inputs):
            mints.append(inputs)
            return real_mint(inputs)

        with mock.patch.object(orch_mod, "mint_open_token", mint_spy):
            pipeline = ExecPipeline(
                journal_dir=self.tmp / "canary-b", run_id="run-s1-canary-b",
                broker=broker, strategy=stub,
                config=committed_assembled_config())
            try:
                for index in range(50, 56):
                    pipeline.tick_on_bar(index)
            finally:
                pipeline.close()
        # the scan RAN (M5C-S9: there is no gates pre-check before scan).
        self.assertGreater(stub.scan_calls, 0)
        decisions = pipeline.rows_of("orders", "strategy_decision")
        self.assertEqual(len(decisions), 3)
        self.assertEqual({row["action"] for row in decisions}, {"would_open"})
        # EVERY decision journals a can_open refusal terminating at run_gates.
        verdicts = pipeline.rows_of("risk", "risk_verdict")
        self.assertEqual(len(verdicts), len(decisions))
        for verdict in verdicts:
            self.assertIs(verdict["allowed"], False)
            self.assertEqual(verdict["gate_stage"], "run_gates")
            self.assertEqual(verdict["reasons"], ["run_gates_off"])
        self.assertEqual({row["decision_id"] for row in verdicts},
                         {row["decision_id"] for row in decisions})
        # the mint is NEVER reached (RC-4: can_open rung 1 terminates first).
        self.assertEqual(mints, [])
        # zero broker calls; zero write-ahead rows; zero open-kind entries.
        self.assertEqual(submit_spy.call_count, 0)
        self.assertEqual(cancel_spy.call_count, 0)
        self.assertEqual(pipeline.rows_of("orders", "order_submit_attempt"), [])
        self.assertEqual(
            [auth for auth in self._registry().values()
             if auth.kind == "open"], [])

    def test_observe_mode_constructs_no_broker_object(self):
        # M5C-T10: an in-process observe composition holds NO Broker-Protocol
        # instance anywhere in its object graph, and never imports
        # agent.broker.alpaca (popped first so the assertion stays honest even
        # after earlier tests imported it; restored afterwards).
        import sys

        from tests.lib.exec_fixtures import (
            ExecPipeline,
            committed_assembled_config,
        )

        popped = sys.modules.pop("agent.broker.alpaca", None)
        if popped is not None:
            self.addCleanup(sys.modules.__setitem__,
                            "agent.broker.alpaca", popped)
        pipeline = ExecPipeline(
            journal_dir=self.tmp / "observe", run_id="run-s1-canary-observe",
            config=committed_assembled_config())
        try:
            pipeline.tick_on_bar(50)
        finally:
            pipeline.close()
        self.assertEqual(pipeline.orch.mode, "observe")
        self.assertIsNone(pipeline.orch.broker)
        self.assertEqual(self._broker_instances(pipeline.orch), [])
        self.assertNotIn("agent.broker.alpaca", sys.modules)

        # positive control: the SAME walk finds an injected broker in a paper
        # composition (proves the walk can see one at all).
        control = ExecPipeline(
            journal_dir=self.tmp / "observe-control",
            run_id="run-s1-canary-observe-control",
            broker=SpyBroker(), config=committed_assembled_config())
        try:
            found = self._broker_instances(control.orch)
        finally:
            control.close()
        self.assertEqual([type(b).__name__ for b in found], ["SpyBroker"])
        self.assertNotIn("agent.broker.alpaca", sys.modules)


class TestM6ReconcileCanary(unittest.TestCase):
    """M6 W5: reconcile observes broker drift under committed gates, but the
    pass never mutates the broker or mints open-capable preflight authority."""

    def setUp(self):
        import shutil
        import tempfile

        from agent.execution_preflight import unbind_runtime
        from tests.agent.test_execution_preflight_m5 import (
            purge_open_authorizations,
        )

        self.tmp = Path(tempfile.mkdtemp(prefix="m6-canary-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(unbind_runtime)
        purge_open_authorizations()
        self.addCleanup(purge_open_authorizations)

    @staticmethod
    def _open_kind_authorizations():
        from agent import execution_preflight
        return [auth for auth in execution_preflight._authorizations.values()
                if auth.kind == "open"]

    def test_reconcile_detects_drift_with_committed_gates_off_and_no_broker_mutation(self):
        from agent.execution_config import ExecutionConfig
        from agent.reconcile_ledger import replay_reconcile
        from tests.lib.exec_fixtures import (
            ExecPipeline,
            committed_assembled_config,
        )
        from tests.lib.risk_fixtures import FakeAccountProvider, account_payload

        cfg = committed_assembled_config()
        self.assertIs(cfg["agent_rules"]["enabled"], False)
        self.assertIs(cfg["agent_rules"]["paper_trading"]["enabled"], False)
        self.assertIs(cfg["risk_rules"]["live_trading"]["enabled"], False)
        agent_rules_before = (_CONFIG / "agent_rules.json").read_bytes()
        risk_rules_before = (_CONFIG / "risk_rules.json").read_bytes()
        rules_hash_before = ExecutionConfig.from_config(cfg).rules_hash

        broker = SpyBroker()
        provider = FakeAccountProvider(
            account_payloads=[account_payload(equity="41900.00",
                                              last_equity="41900.00",
                                              cash="40000.00")],
            positions_payloads=[[{
                "symbol": "AAPL", "qty": "10", "market_value": "1900.00",
                "avg_entry_price": "190.00",
            }]])
        pipeline = ExecPipeline(
            journal_dir=self.tmp / "drift", run_id="run-m6-canary",
            broker=broker, account_provider=provider,
            config=cfg)
        try:
            result = pipeline.orch.run_reconcile(phase="cli")
        finally:
            pipeline.close()

        self.assertFalse(result.clean)
        self.assertTrue(pipeline.orch.drift_latched)
        rows = replay_reconcile(self.tmp / "drift" / "reconcile_alerts.jsonl")
        self.assertEqual(rows[0]["kind"], "position_unknown_broker")
        self.assertEqual(rows[0]["action"], "latched_operator")
        self.assertEqual(rows[-1]["event_type"], "reconcile_run")
        self.assertFalse(rows[-1]["clean"])
        self.assertEqual(broker.calls, [])
        self.assertEqual(broker.submitted, [])
        self.assertEqual(broker.cancel_calls, [])
        self.assertEqual(self._open_kind_authorizations(), [])
        self.assertEqual((_CONFIG / "agent_rules.json").read_bytes(),
                         agent_rules_before)
        self.assertEqual((_CONFIG / "risk_rules.json").read_bytes(),
                         risk_rules_before)
        self.assertEqual(ExecutionConfig.from_config(
            committed_assembled_config()).rules_hash, rules_hash_before)

    def test_committed_config_still_blocks_can_open_before_reconcile_state(self):
        # The existing RiskEngine canary is still the source of truth: committed
        # config terminates at run_gates. This guards against a reconcile latch
        # accidentally becoming a prerequisite before S1's first rung.
        case = TestRiskRulesCanary(
            "test_committed_config_can_open_always_terminates_at_run_gates"
        )
        case.test_committed_config_can_open_always_terminates_at_run_gates()


if __name__ == "__main__":
    unittest.main()
