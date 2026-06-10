"""M5 §R test 10 — the orchestrator (`scripts/agent/orchestrator.py`).

Covers: startup ordering (M4 §O + rev-2 step order M5C-S2), step-6 purity +
step-10 placement, the ctor injection seam (M5C-T3), MarginObservation per buy
fill, portfolio_is_stale usage, the F4 snapshot obligation, the HALTED latch,
the latency seam (FD-M5-10) + the wall-clock AST scan, the §M.4 FSM over
ScriptedOrderApi, one-in-flight discipline, session-edge cancel + close_of_day,
FD-M5-17 recovery (adopt / not_found + open-deny), FD-M5-24 restart adopt+
cancel, RC-3 degrade-to-observe offline orphans, THE RC-1 global in-flight
close guard, and the two §R 13 orchestrator-integration cases deferred from
wave 4 (pre-substitution rules_hash pin; gates-absent reduce-and-recover).

Compositions are REAL parts only: FakeClock, ScriptedOrderApi, real ledgers
into tmp dirs, real risk components over the permissive fixture config.
"""
import ast
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent import orchestrator as orch_mod
from agent.broker.alpaca import AlpacaPaperBroker, SyntheticConfinementError
from agent.exec_ledger import ExecLedger
from agent.execution_preflight import unbind_runtime
from agent.journal import replay
from agent.paper_book import PaperBook
from agent.risk.risk_ledger import RiskLedger
from agent.serializer import BrokerUSD, row_hash
from recorder.persistence import EventWriter

from tests.lib.alpaca_fixtures import BrokerTimeout, ScriptedOrderApi, order_payload
from tests.lib.exec_fixtures import (
    ExecPipeline,
    FIXED_WRITER_TS,
    RealStrategyStub,
    ReEmittingExitProvider,
    committed_assembled_config,
    permissive_paper_fixture_config,
    run_synthetic_golden,
)
from tests.lib.fakes import SpyBroker
from tests.lib.risk_fixtures import FakeAccountProvider, account_payload
from tests.lib.signal_fixtures import quotes_session  # noqa: F401 (composition doc)

_ROW_CLOCK = lambda: FIXED_WRITER_TS  # noqa: E731


def _ack(payload):
    """Scripted submit step: echo the wire payload back as an Alpaca ack."""
    return order_payload(
        client_order_id=payload["client_order_id"], symbol=payload["symbol"],
        qty=payload["qty"], status="new", filled_qty="0",
        filled_avg_price=None, limit_price=payload["limit_price"])


def _ack_filled(avg):
    def step(payload):
        return order_payload(
            client_order_id=payload["client_order_id"],
            symbol=payload["symbol"], qty=payload["qty"], status="filled",
            filled_qty=payload["qty"], filled_avg_price=avg,
            limit_price=payload["limit_price"])
    return step


def _poll(status, filled, avg, *, qty="10", symbol="AAPL"):
    def step(client_order_id):
        return order_payload(
            client_order_id=client_order_id, symbol=symbol, qty=qty,
            status=status, filled_qty=filled, filled_avg_price=avg)
    return step


class OrchestratorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="m5-orch-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(unbind_runtime)  # teardown hygiene (registry runtime)

    def make_pipeline(self, subdir="run", **kwargs):
        pipeline = ExecPipeline(journal_dir=self.tmp / subdir, **kwargs)
        self.addCleanup(pipeline.close)
        return pipeline

    # -- shared FSM composition (RealStrategyStub over ScriptedOrderApi) -------

    def fsm_pipeline(self, *, script, subdir="fsm", run_gates="valid",
                     exit_provider=None, positions_rows=None, qty="10"):
        api = ScriptedOrderApi(script)
        stub = RealStrategyStub([{"on_bar": 1, "qty": qty}])
        broker = AlpacaPaperBroker(order_api=api)
        provider = FakeAccountProvider(
            positions_payloads=[positions_rows if positions_rows is not None
                                else []])
        pipeline = self.make_pipeline(
            subdir=subdir, broker=broker, strategy=stub,
            exit_provider=exit_provider, run_gates=run_gates,
            artifacts="valid", account_provider=provider)
        return pipeline, api, stub

    # ---------------------------------------------------------------- startup

    def test_startup_seeds_all_four_before_first_put_or_can_open(self):
        events = []

        def spy(target, name, label):
            original = getattr(target, name)

            def wrapper(*args, **kwargs):
                events.append(label)
                return original(*args, **kwargs)
            return mock.patch.object(target, name, wrapper)

        with spy(orch_mod.IntradayMarginModel, "rehydrate", "seed:margin"), \
                spy(orch_mod.RiskKillSwitch, "rehydrate", "seed:kill"), \
                spy(orch_mod.LegacyPdtCompatMode, "__init__", "seed:pdt"), \
                spy(orch_mod.LossLimitsMonitor, "rehydrate", "seed:loss"), \
                spy(orch_mod.AccountStore, "put", "account:put"), \
                spy(orch_mod.RiskEngine, "can_open", "risk:can_open"):
            pipeline, api, stub = self.fsm_pipeline(
                script={"submit": [_ack]}, subdir="ordering")
            pipeline.tick_on_bar(50)   # account put + a scan decision

        seeds = [event for event in events if event.startswith("seed:")]
        self.assertEqual(sorted(set(seeds)),
                         ["seed:kill", "seed:loss", "seed:margin", "seed:pdt"])
        first_consumer = min(events.index("account:put"),
                             events.index("risk:can_open"))
        for label in ("seed:margin", "seed:kill", "seed:pdt", "seed:loss"):
            self.assertLess(events.index(label), first_consumer,
                            f"{label} must precede the first put/can_open")

    def _seed_dangling_order(self, journal_dir, *, run_id="run-prior",
                             symbol="AAPL", qty="10"):
        journal_dir = Path(journal_dir)
        journal_dir.mkdir(parents=True, exist_ok=True)
        ledger = ExecLedger(
            orders=EventWriter(journal_dir / "orders.jsonl", run_id,
                               clock=_ROW_CLOCK),
            fills=EventWriter(journal_dir / "fills.jsonl", run_id,
                              clock=_ROW_CLOCK),
            positions=EventWriter(journal_dir / "positions.jsonl", run_id,
                                  clock=_ROW_CLOCK),
            rules_hash="0" * 64)
        decision_id = "d-" + row_hash({"prior": symbol})
        order_id = "o-" + row_hash({"prior": symbol})
        ledger.record_order_submit_attempt(
            client_order_id=order_id, preflight_id="pf-" + row_hash({"p": 1}),
            risk_verdict_id=None, strategy_id="stub.real_v1", symbol=symbol,
            instrument_id=1001, side="buy", qty=Decimal(qty),
            order_intent={"order_type": "marketable_limit", "tif": "day",
                          "limit_price": Decimal("200.10")},
            token_kind="open", kill_generation=0, quote_b=None,
            decision_id=decision_id, order_id=order_id)
        ledger.record_order_submitted(
            client_order_id=order_id, broker_order_id="broker-prior-1",
            state="accepted", raw_status="new", ts_broker_utc=None,
            source="alpaca_paper", decision_id=decision_id, order_id=order_id)
        return order_id

    def test_step6_pure_and_step10_recovery_after_mode_select(self):
        journal_dir = self.tmp / "recover"
        order_id = self._seed_dangling_order(journal_dir)
        events = []

        def found(client_order_id):
            events.append("broker_query")
            return order_payload(client_order_id=client_order_id,
                                 symbol="AAPL", qty="10", status="new",
                                 filled_qty="0", filled_avg_price=None)

        api = ScriptedOrderApi({
            "get_by_client_order_id": [
                found,                                   # FD-M5-24 adopt query
                _poll("new", "0", None),                 # cancel_order lookup
                _poll("canceled", "0", None),            # next-tick poll
            ],
            "cancel": [_poll("pending_cancel", "0", None)("ignored")],
        })
        real_rehydrate = orch_mod.rehydrate_exec_state

        def rehydrate_spy(*args, **kwargs):
            events.append("exec_rehydrate")
            return real_rehydrate(*args, **kwargs)

        broker = AlpacaPaperBroker(order_api=api)
        with mock.patch.object(orch_mod, "rehydrate_exec_state",
                               rehydrate_spy):
            # run-gates ABSENT => gates view False; recovery MUST still run
            # (reduce-and-recover, M5C-S3).
            pipeline = self.make_pipeline(
                subdir="recover", broker=broker, strategy=None,
                account_provider=FakeAccountProvider())

        # step 6 is PURE: the rehydrate fold completed BEFORE any broker call.
        self.assertIn("exec_rehydrate", events)
        self.assertIn("broker_query", events)
        self.assertLess(events.index("exec_rehydrate"),
                        events.index("broker_query"))
        # FD-M5-24: adopt + ONE best-effort cancel (restart_unknown_state).
        cancels = pipeline.rows_of("orders", "post_submit_cancel_attempt")
        self.assertEqual([row["cause"] for row in cancels],
                         ["restart_unknown_state"])
        self.assertTrue(pipeline.orch.in_flight)   # adopted => resume polling
        # The adopted order resolves on the next poll.
        pipeline.tick_on_bar(50)
        terminals = pipeline.rows_of("orders", "order_terminal")
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0]["terminal_state"], "canceled")
        self.assertEqual(terminals[0]["order_id"], order_id)
        self.assertFalse(pipeline.orch.in_flight)

    def test_degrade_to_observe_resolves_offline_orphans(self):
        journal_dir = self.tmp / "degrade"
        self._seed_dangling_order(journal_dir)
        pipeline = self.make_pipeline(
            subdir="degrade",
            credentials_path=self.tmp / "missing_credentials.json")
        self.assertEqual(pipeline.orch.mode, "observe")
        self.assertIsNone(pipeline.orch.broker)
        status_rows = pipeline.rows("status")
        self.assertIn("mode_degraded",
                      [row["event_type"] for row in status_rows])
        unconfirmed = pipeline.rows_of("orders", "order_submit_unconfirmed")
        self.assertEqual([row["resolution"] for row in unconfirmed],
                         ["offline_orphan"])                      # RC-3
        self.assertIn("AAPL", pipeline.orch.open_deny)

    def test_ctor_injection_seam_and_wall_one(self):
        broker = SpyBroker()
        pipeline = self.make_pipeline(subdir="seam", broker=broker)
        self.assertIs(pipeline.orch.broker, broker)
        self.assertEqual(pipeline.orch.mode, "paper")
        pipeline.close()
        # Wall 1 runs on INJECTED pairs too (FD-M5-8 type identity).
        from agent.strategies.synthetic import ScriptedSyntheticStrategy
        strategy = ScriptedSyntheticStrategy(
            [{"on_bar": 1, "action": "open", "symbol": "AAPL", "qty": "10",
              "limit": None}])
        with self.assertRaises(SyntheticConfinementError):
            ExecPipeline(journal_dir=self.tmp / "wall1", broker=SpyBroker(),
                         strategy=strategy)
        # The failed ctor released its lock + runtime (fail-loud cleanup).
        again = ExecPipeline(journal_dir=self.tmp / "wall1",
                             broker=SpyBroker())
        self.addCleanup(again.close)

    def test_halted_journal_starts_halted_and_retry_residual_legal(self):
        journal_dir = self.tmp / "halted"
        journal_dir.mkdir(parents=True)
        ledger = RiskLedger(EventWriter(journal_dir / "risk.jsonl",
                                        "run-prior", clock=_ROW_CLOCK),
                            rules_hash="0" * 64)
        ledger.record_kill_transition(
            from_state="monitoring", to_state="flattening", cause="drill",
            generation=1, daily_loss_usd=None, drawdown_usd=None,
            cap_usd=None, account_snapshot_id=None, stale_inputs=True,
            flattened=(), failed=(), residual=("AAPL",),
            tradability_annotations=())
        ledger.record_kill_transition(
            from_state="flattening", to_state="halted", cause="drill",
            generation=1, daily_loss_usd=None, drawdown_usd=None,
            cap_usd=None, account_snapshot_id=None, stale_inputs=True,
            flattened=(), failed=(("AAPL", "no quote"),), residual=("AAPL",),
            tradability_annotations=())
        pipeline, api, stub = self.fsm_pipeline(script={}, subdir="halted")
        self.assertEqual(pipeline.orch.risk_kill.state, "halted")
        self.assertIn("halted_latch",
                      [row["event_type"] for row in pipeline.rows("status")])
        pipeline.tick_on_bar(50)   # scan runs; can_open refuses at rung 2
        verdicts = pipeline.rows_of("risk", "risk_verdict")
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["gate_stage"], "kill")
        self.assertEqual(verdicts[0]["reasons"], ["kill_switch_halted"])
        # opens structurally impossible: no write-ahead, no submit.
        self.assertEqual(pipeline.rows_of("orders", "order_submit_attempt"), [])
        self.assertEqual(api.submit_calls, [])
        # retry_residual stays LEGAL in HALTED (operator-attended).
        report = pipeline.orch.retry_residual()
        self.assertEqual(report.residual, ("AAPL",))
        self.assertEqual(pipeline.orch.risk_kill.state, "halted")

    # ---------------------------------------------------------------- latency

    def test_latency_seam_no_submit_until_budget(self):
        pipeline, api, stub = self.fsm_pipeline(
            script={"submit": [_ack]}, subdir="latency")
        pipeline.tick_on_bar(50)               # DECIDED at t0
        self.assertTrue(pipeline.orch.in_flight)
        self.assertEqual(api.submit_calls, [])
        # clock NOT advanced => not due.
        pipeline.orch.on_tick(now_ms=pipeline.clock.now_ms())
        self.assertEqual(api.submit_calls, [])
        # advanced to budget-1 => still not due.
        pipeline.tick_quote_only(50, advance_ms=249, shift_ms=400)
        self.assertEqual(api.submit_calls, [])
        # at exactly the budget => due (strict >= in the scheduler item).
        pipeline.tick_quote_only(50, advance_ms=1, shift_ms=600)
        self.assertEqual(len(api.submit_calls), 1)

    def test_no_wall_clock_or_sleep_in_orchestrator_source(self):
        source = Path(orch_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name.split(".")[0], "time")
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual((node.module or "").split(".")[0], "time")
            if isinstance(node, ast.Attribute):
                self.assertNotIn(
                    node.attr, ("sleep", "now", "utcnow", "monotonic"),
                    "wall-clock/sleep reference in orchestrator.py (FD-M5-10)")

    def test_broker_imports_are_lazy_at_module_scope(self):
        """§3/M5C-T10: orchestrator module scope may import agent.broker.base
        + agent.broker.order_state ONLY; alpaca/fake/flatten_proxy stay inside
        the step-9 mode-select / §M.6 kill-wiring function bodies."""
        source = Path(orch_mod.__file__).read_text(encoding="utf-8")
        allowed = {"agent.broker.base", "agent.broker.order_state"}
        offending = []

        def walk(node, in_func):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, True)
                    continue
                if not in_func and isinstance(child, (ast.Import,
                                                      ast.ImportFrom)):
                    names = ([alias.name for alias in child.names]
                             if isinstance(child, ast.Import)
                             else [child.module or ""])
                    for name in names:
                        if (name.startswith("agent.broker")
                                and name not in allowed):
                            offending.append(name)
                walk(child, in_func)

        walk(ast.parse(source), False)
        self.assertEqual(offending, [])

    # -------------------------------------------------------------------- FSM

    def test_fsm_decide_requote_preflight_submit_watch_book(self):
        pipeline, api, stub = self.fsm_pipeline(script={
            "submit": [_ack],
            "get_by_client_order_id": [
                _poll("partially_filled", "3", "200.10"),
                _poll("filled", "10", "200.16"),
            ],
        })
        pipeline.tick_on_bar(50)                 # DECIDED (t0)
        pipeline.tick_quote_only(50)             # REQUOTE->PREFLIGHT->SUBMIT
        self.assertEqual(len(api.submit_calls), 1)
        pipeline.tick_quote_only(50, advance_ms=500, shift_ms=900)   # poll 1
        pipeline.tick_quote_only(50, advance_ms=500, shift_ms=1400)  # poll 2

        decisions = pipeline.rows_of("orders", "strategy_decision")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["action"], "would_open")
        self.assertEqual(decisions[0]["strategy_kind"], "real")
        decision_id = decisions[0]["decision_id"]

        verdicts = pipeline.rows_of("risk", "risk_verdict")
        self.assertEqual(len(verdicts), 1)
        self.assertTrue(verdicts[0]["allowed"])
        self.assertEqual(verdicts[0]["decision_id"], decision_id)

        attempts = pipeline.rows_of("orders", "order_submit_attempt")
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        self.assertEqual(attempt["decision_id"], decision_id)
        self.assertEqual(attempt["client_order_id"], attempt["order_id"])
        self.assertTrue(attempt["preflight_id"].startswith("pf-"))
        self.assertTrue(attempt["risk_verdict_id"].startswith("rv-"))
        self.assertEqual(attempt["token_kind"], "open")
        order_id = attempt["order_id"]

        submitted = pipeline.rows_of("orders", "order_submitted")
        self.assertEqual(len(submitted), 1)
        # write-ahead: the attempt row precedes the submitted row (same stream).
        self.assertLess(attempt["seq"], submitted[0]["seq"])

        modeled = pipeline.rows_of("fills", "modeled_execution_fill")
        self.assertEqual(len(modeled), 1)
        self.assertEqual(modeled[0]["model"], "tob_l1_v1")
        self.assertEqual(modeled[0]["order_id"], order_id)

        fills = pipeline.rows_of("fills", "broker_fill")
        self.assertEqual(len(fills), 2)
        # FD-M5-18 exactness under avg drift: 3x200.10 then 10x200.16-3x200.10.
        self.assertEqual(fills[0]["delta_qty"], "3")
        self.assertEqual(Decimal(fills[0]["delta_cost_usd"]),
                         Decimal("600.30"))
        self.assertEqual(fills[1]["delta_qty"], "7")
        self.assertEqual(Decimal(fills[1]["delta_cost_usd"]),
                         Decimal("1401.30"))

        updates = pipeline.rows_of("orders", "broker_order_update")
        self.assertGreaterEqual(len(updates), 2)
        terminals = pipeline.rows_of("orders", "order_terminal")
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0]["terminal_state"], "filled")
        self.assertEqual(Decimal(terminals[0]["cum_notional_usd"]),
                         Decimal("2001.60"))

        divergence = pipeline.rows_of("fills", "fill_divergence")
        self.assertEqual(len(divergence), 1)
        self.assertIn(divergence[0]["flag"],
                      ("aligned", "broker_optimistic", "broker_conservative"))

        opens = pipeline.rows_of("positions", "position_open")
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0]["order_id"], order_id)
        position = pipeline.orch.book.position(opens[0]["position_id"])
        self.assertEqual(position.qty, Decimal("10"))
        self.assertEqual(position.broker_cost_usd, Decimal("2001.60"))
        self.assertFalse(pipeline.orch.in_flight)

    def test_one_in_flight_blocks_further_scans(self):
        api_script = {
            "submit": [_ack],
            "get_by_client_order_id": [_poll("new", "0", None)
                                       for _ in range(10)],
        }
        api = ScriptedOrderApi(api_script)
        stub = RealStrategyStub([{"on_bar": 1, "qty": "10"},
                                 {"on_bar": 2, "qty": "10"}])
        broker = AlpacaPaperBroker(order_api=api)
        pipeline = self.make_pipeline(
            subdir="inflight", broker=broker, strategy=stub,
            run_gates="valid", artifacts="valid",
            account_provider=FakeAccountProvider())
        pipeline.tick_on_bar(50)
        pipeline.tick_quote_only(50)             # submits order 1 (rests new)
        self.assertTrue(pipeline.orch.in_flight)
        scans_before = stub.scan_calls
        pipeline.tick_on_bar(51)                 # new bar while in flight
        self.assertEqual(stub.scan_calls, scans_before)   # scan NOT called
        self.assertEqual(
            len(pipeline.rows_of("orders", "strategy_decision")), 1)
        self.assertEqual(len(api.submit_calls), 1)

    def test_margin_observation_after_every_buy_fill(self):
        pipeline, api, stub = self.fsm_pipeline(script={
            "submit": [_ack],
            "get_by_client_order_id": [
                _poll("partially_filled", "3", "200.10"),
                _poll("filled", "10", "200.16"),
            ],
        }, subdir="margin")
        pipeline.tick_on_bar(50)
        pipeline.tick_quote_only(50)
        pipeline.tick_quote_only(50, advance_ms=500, shift_ms=900)
        pipeline.tick_quote_only(50, advance_ms=500, shift_ms=1400)
        observations = pipeline.rows_of("risk", "iml_observation")
        non_eod = [row for row in observations if row["eod"] is False]
        self.assertEqual(len(non_eod), 2)        # one per buy fill (RM-8)
        self.assertTrue(all(row["after_iml_reducing"] for row in non_eod))

    def test_portfolio_is_stale_is_used_for_portfolio_read(self):
        calls = []
        real = orch_mod.portfolio_is_stale

        def spy(seen_at_ms, now_ms, **kwargs):
            calls.append((seen_at_ms, now_ms))
            return real(seen_at_ms, now_ms, **kwargs)

        with mock.patch.object(orch_mod, "portfolio_is_stale", spy):
            pipeline, api, stub = self.fsm_pipeline(
                script={"submit": [_ack]}, subdir="stale")
            pipeline.tick_on_bar(50)
        self.assertGreaterEqual(len(calls), 1)   # FD-M4-22 helper is THE seam

    def test_record_account_snapshot_on_every_put(self):
        provider = FakeAccountProvider(account_payloads=[
            account_payload(),
            account_payload(equity=1.5),         # float => AccountInvalid
        ])
        put_calls = []
        real_put = orch_mod.AccountStore.put

        def put_spy(self_store, result):
            put_calls.append(type(result).__name__)
            return real_put(self_store, result)

        with mock.patch.object(orch_mod.AccountStore, "put", put_spy):
            pipeline = self.make_pipeline(
                subdir="f4", broker=AlpacaPaperBroker(order_api=ScriptedOrderApi({})),
                account_provider=provider)
            for index in (50, 51, 52, 53):       # refresh at +1000 and +4000
                pipeline.tick_on_bar(index)
        snapshots = pipeline.rows_of("risk", "account_snapshot")
        self.assertEqual(len(put_calls), 2)
        self.assertEqual(len(snapshots), len(put_calls))   # F4: every put
        self.assertIn("AccountInvalid", put_calls)

    # ------------------------------------------------------- session edge

    def test_session_edge_cancels_open_order_and_closes_day(self):
        api = ScriptedOrderApi({
            "submit": [_ack],
            "get_by_client_order_id": [_poll("new", "0", None)
                                       for _ in range(40)],
            "cancel": [_poll("pending_cancel", "0", None)("x")],
        })
        stub = RealStrategyStub([{"on_bar": 1, "qty": "10"}])
        broker = AlpacaPaperBroker(order_api=api)
        pipeline = self.make_pipeline(
            subdir="edge", broker=broker, strategy=stub, run_gates="valid",
            artifacts="valid", account_provider=FakeAccountProvider(),
            start_et="15:00", minutes=70)
        pipeline.tick_on_bar(50)                 # decision ~15:51 ET
        pipeline.tick_quote_only(50)             # submit; rests "new"
        self.assertTrue(pipeline.orch.in_flight)
        for index in range(51, 62):              # cross 16:00 ET
            pipeline.tick_on_bar(index)
        cancels = pipeline.rows_of("orders", "post_submit_cancel_attempt")
        self.assertIn("session_end", [row["cause"] for row in cancels])
        eod = [row for row in pipeline.rows_of("risk", "iml_observation")
               if row["eod"] is True]
        self.assertEqual(len(eod), 1)            # close_of_day at the edge

    # ---------------------------------------------------------------- recovery

    def test_submit_timeout_adopts_by_client_order_id(self):
        pipeline, api, stub = self.fsm_pipeline(script={
            "submit": [BrokerTimeout("submit response lost (injected)")],
            "get_by_client_order_id": [_poll("new", "0", None)],
        }, subdir="adopt")
        pipeline.tick_on_bar(50)
        pipeline.tick_quote_only(50)
        self.assertEqual(len(api.submit_calls), 1)     # NEVER blind-resubmit
        submitted = pipeline.rows_of("orders", "order_submitted")
        self.assertEqual(len(submitted), 1)            # adopted
        self.assertTrue(pipeline.orch.in_flight)       # resumes polling
        self.assertEqual(pipeline.rows_of("orders",
                                          "order_submit_unconfirmed"), [])

    def test_submit_timeout_not_found_denies_symbol(self):
        from tests.lib.alpaca_fixtures import not_found_error
        pipeline, api, stub = self.fsm_pipeline(script={
            "submit": [BrokerTimeout("submit response lost (injected)")],
            "get_by_client_order_id": [not_found_error() for _ in range(3)],
        }, subdir="notfound")
        pipeline.tick_on_bar(50)
        pipeline.tick_quote_only(50)
        unconfirmed = pipeline.rows_of("orders", "order_submit_unconfirmed")
        self.assertEqual(len(unconfirmed), 1)
        self.assertEqual(unconfirmed[0]["resolution"], "not_found")
        self.assertEqual(unconfirmed[0]["attempts"], 3)
        self.assertIn("AAPL", pipeline.orch.open_deny)
        self.assertFalse(pipeline.orch.in_flight)
        scans_before = stub.scan_calls
        pipeline.tick_on_bar(51)                 # denied symbol: no scan call
        self.assertEqual(stub.scan_calls, scans_before)
        self.assertEqual(
            len(pipeline.rows_of("orders", "strategy_decision")), 1)

    # -------------------------------------------------------------------- RC-1

    def test_rc1_global_in_flight_guard_single_close(self):
        mints = []
        real_mint = orch_mod.mint_reduce_only_token

        def mint_spy(position, intent):
            mints.append(intent.intent_id)
            return real_mint(position, intent)

        exit_provider = ReEmittingExitProvider(symbol="AAPL", qty="10",
                                               start_call=1)
        with mock.patch.object(orch_mod, "mint_reduce_only_token", mint_spy):
            pipeline, api, stub = self.fsm_pipeline(script={
                "submit": [
                    _ack_filled("200.10"),       # open: fills on the ack
                    _ack,                        # close: rests "new"
                ],
                "get_by_client_order_id": [
                    _poll("new", "0", None),                  # close poll 1
                    _poll("new", "0", None),                  # close poll 2
                    _poll("filled", "10", "200.30"),          # close fills
                ],
            }, subdir="rc1", exit_provider=exit_provider)
            pipeline.tick_on_bar(50)             # open DECIDED; exit dropped
            pipeline.tick_quote_only(50)         # open submit -> filled (booked)
            self.assertFalse(pipeline.orch.in_flight)
            pipeline.tick_on_bar(51)             # exit -> close #1 submits
            self.assertTrue(pipeline.orch.in_flight)
            pipeline.tick_on_bar(52)             # re-emit while WATCH: dropped
            pipeline.tick_on_bar(53)             # re-emit dropped
            pipeline.tick_on_bar(54)             # close fills -> booked
            pipeline.tick_on_bar(55)             # re-emit, position closed

        closes = pipeline.rows_of("orders", "strategy_decision")
        would_close = [row for row in closes if row["action"] == "would_close"]
        self.assertEqual(len(would_close), 1)    # exactly ONE close decision
        self.assertEqual(len(mints), 1)          # ONE reduce mint
        reduce_attempts = [
            row for row in pipeline.rows_of("orders", "order_submit_attempt")
            if row["token_kind"] == "reduce_only"]
        self.assertEqual(len(reduce_attempts), 1)   # ONE close order
        position_closes = pipeline.rows_of("positions", "position_close")
        self.assertEqual(len(position_closes), 1)
        self.assertEqual(position_closes[0]["reason"], "strategy_exit")
        self.assertEqual(Decimal(position_closes[0]["realized_broker_pnl"]),
                         Decimal("2.00"))        # 10x(200.30-200.10)

    def test_over_qty_exit_journals_refusal_and_loop_survives(self):
        # LC-1/SF-1: an ExitInstruction whose qty exceeds the held size makes
        # mint_reduce_only_token raise PreflightRejected ("reduce-only qty must
        # be >0 and <= held size"). _start_close MUST catch it, journal a
        # contract-legal refusal, and return — the tick loop must survive (the
        # re-emitting provider would otherwise re-crash every subsequent tick).
        # No reduce submit happens; self._task stays None (set after the mint).
        from agent.strategies.synthetic import ScriptedSyntheticStrategy
        strategy = ScriptedSyntheticStrategy([
            {"on_bar": 1, "action": "open", "symbol": "AAPL", "qty": "10",
             "limit": "210.00"}])
        provider = FakeAccountProvider(positions_payloads=[[
            {"symbol": "AAPL", "qty": "10", "market_value": "2000.00",
             "instrument_id": 1001}]])
        over_qty = ReEmittingExitProvider(symbol="AAPL", qty="999",
                                          start_call=2)
        pipeline = self.make_pipeline(
            subdir="overqty", strategy=strategy, exit_provider=over_qty,
            account_provider=provider, fill_policy="immediate_full")

        # bar 50: open fills at the ack (immediate_full); exit not yet due.
        pipeline.tick_on_bar(50)
        pipeline.tick_quote_only(50)
        opens = pipeline.rows_of("positions", "position_open")
        self.assertEqual(len(opens), 1)
        self.assertFalse(pipeline.orch.in_flight)

        # bar 51+: exits() emits the over-qty instruction. The loop MUST NOT
        # crash; subsequent ticks keep running (the re-emit re-attempts).
        pipeline.tick_on_bar(51)
        self.assertFalse(pipeline.orch.in_flight)   # mint refused -> no task
        pipeline.tick_on_bar(52)                    # re-emit still survives
        pipeline.tick_on_bar(53)

        # A refusal is journaled (the over-qty exit is NOT silently dropped):
        # an order_state_alert naming the over-qty reduce — the contract-faithful
        # diagnostic row (no fabricated reduce-side reject reason; the reduce
        # reject vocab is {no_price_for_cap, broker_rejected}, neither apt).
        alerts = pipeline.rows_of("orders", "order_state_alert")
        over = [r for r in alerts if r["note"] == "reduce_qty_exceeds_held"]
        self.assertTrue(over, "the over-qty exit must journal a refusal alert")

        # NO reduce order ever submitted; NO position close booked.
        reduce_attempts = [
            r for r in pipeline.rows_of("orders", "order_submit_attempt")
            if r["token_kind"] == "reduce_only"]
        self.assertEqual(reduce_attempts, [])
        self.assertEqual(pipeline.rows_of("positions", "position_close"), [])
        # the position is still open and intact.
        position = pipeline.orch.book.position(opens[0]["position_id"])
        self.assertEqual(position.status, "open")
        self.assertEqual(position.qty, Decimal("10"))

    def test_close_path_feeds_sell_side_modeled_fill(self):
        # EC-1: the strategy close path must compute a SELL-side ModeledFill from
        # the SAME quote_b it priced reduce_cap on (§J/§K/EX-9), journal a
        # modeled_execution_fill row for the close order, carry it on the close
        # task, and pass it to close_position at terminal — so the close
        # fill_divergence gets a REAL side-aware flag (EX-3) and the
        # position_close carries a non-null realized_modeled_pnl with sell-side
        # fees over the MODELED exit proceeds (EX-9). Pre-fix these were dead in
        # every integrated run (close modeled=None, flag hardcoded "unassessed").
        from agent.strategies.synthetic import ScriptedSyntheticStrategy
        strategy = ScriptedSyntheticStrategy([
            {"on_bar": 2, "action": "open", "symbol": "AAPL", "qty": "10",
             "limit": "210.00"},
            {"on_bar": 4, "action": "close", "symbol": "AAPL", "qty": "10",
             "limit": None}])
        pipeline = self.make_pipeline(
            subdir="ec1", strategy=strategy, exit_provider=strategy,
            fill_policy="immediate_full")
        for index in range(50, 59):
            pipeline.tick_on_bar(index)
            pipeline.tick_quote_only(index)

        opens = pipeline.rows_of("positions", "position_open")
        closes = pipeline.rows_of("positions", "position_close")
        self.assertEqual(len(opens), 1)
        self.assertEqual(len(closes), 1)
        close_order_id = closes[0]["order_id"]
        open_order_id = opens[0]["order_id"]

        # TWO modeled_execution_fill rows now: the open buy AND the close sell,
        # each joined to its own order.
        modeled = pipeline.rows_of("fills", "modeled_execution_fill")
        by_order = {r["order_id"]: r for r in modeled}
        self.assertIn(open_order_id, by_order)
        self.assertIn(close_order_id, by_order)
        self.assertEqual(by_order[close_order_id]["model"], "tob_l1_v1")

        # the close fill_divergence carries a REAL side-aware flag (not the dead
        # hardcoded "unassessed").
        sell_div = [r for r in pipeline.rows_of("fills", "fill_divergence")
                    if r["side"] == "sell"]
        self.assertEqual(len(sell_div), 1)
        self.assertIn(sell_div[0]["flag"],
                      ("aligned", "broker_optimistic", "broker_conservative"))
        self.assertIsNotNone(sell_div[0]["divergence_usd"])

        # the position_close now carries realized_modeled_pnl + sell-side fees
        # (EX-9: fees over the modeled exit proceeds; a full close of 10 shares
        # at a >$0 price ⇒ nonzero SEC+TAF).
        self.assertIsNotNone(closes[0]["realized_modeled_pnl"])
        self.assertGreater(Decimal(closes[0]["fees_assessed"]["total_usd"]),
                           Decimal("0"))

    # ------------------------------------------------------- §R 13 (deferred)

    def test_rules_hash_identical_with_and_without_run_gates_file(self):
        hashes = {}
        for label, run_gates in (("absent", None), ("present", "valid")):
            pipeline = self.make_pipeline(
                subdir=f"gates-{label}", broker=SpyBroker(),
                config=committed_assembled_config(), run_gates=run_gates)
            pipeline.tick_on_bar(50)
            pipeline.tick_on_bar(51)
            per_stream = {}
            for stream in ("decisions", "risk", "status"):
                values = {row["rules_hash"] for row in pipeline.rows(stream)
                          if "rules_hash" in row}
                per_stream[stream] = values
                self.assertLessEqual(len(values), 1, stream)
            self.assertTrue(per_stream["decisions"])   # the probe DID run
            hashes[label] = per_stream
            pipeline.close()
        # M5C-S4: the substitution NEVER feeds rules_hash — identical bytes.
        self.assertEqual(hashes["absent"], hashes["present"])

    def test_gates_absent_paper_can_cancel_and_flatten_but_never_open(self):
        journal_dir = self.tmp / "reduce-recover"
        journal_dir.mkdir(parents=True)
        # prior run: an open position + a dangling order.
        config = permissive_paper_fixture_config()
        from agent.execution_config import ExecutionConfig
        rules_hash_value = ExecutionConfig.from_config(config).rules_hash
        prior_ledger = ExecLedger(
            orders=EventWriter(journal_dir / "orders.jsonl", "run-prior",
                               clock=_ROW_CLOCK),
            fills=EventWriter(journal_dir / "fills.jsonl", "run-prior",
                              clock=_ROW_CLOCK),
            positions=EventWriter(journal_dir / "positions.jsonl",
                                  "run-prior", clock=_ROW_CLOCK),
            rules_hash=rules_hash_value)
        prior_book = PaperBook(ledger=prior_ledger, run_id="run-prior",
                               quote_staleness_ms_max=2000,
                               spread_bps_max=Decimal("50"))
        opening_order_id = "o-" + row_hash({"prior-open": "AAPL"})
        fill = SimpleNamespace(delta_qty=Decimal("10"),
                               delta_cost_usd=BrokerUSD(Decimal("2001.00")))
        prior_book.open_position(
            decision_id="d-" + row_hash({"prior-open": 1}),
            order_id=opening_order_id, symbol="AAPL", instrument_id=1001,
            strategy_id="stub.real_v1", fills=[fill], modeled=None,
            opened_ts_utc="2026-06-15T13:45:00.000000Z")
        dangling_id = self._seed_dangling_order(journal_dir)

        def flatten_status(client_order_id):
            return order_payload(
                client_order_id=client_order_id, symbol="AAPL", qty="10",
                side="sell", status="filled", filled_qty="10",
                filled_avg_price="200.00")

        api = ScriptedOrderApi({
            "get_by_client_order_id": [
                _poll("canceled", "0", None),     # adopt query -> terminal
                _poll("canceled", "0", None),     # cancel_order lookup
                flatten_status,                   # kill-flatten booking poll
            ],
            "cancel": [_poll("canceled", "0", None)("x")],
            "submit": [
                lambda payload: order_payload(
                    client_order_id=payload["client_order_id"],
                    symbol=payload["symbol"], qty=payload["qty"],
                    side=payload["side"], status="filled",
                    filled_qty=payload["qty"],
                    filled_avg_price="200.00",
                    limit_price=payload["limit_price"]),
            ],
        })
        broker = AlpacaPaperBroker(order_api=api)
        stub = RealStrategyStub([{"on_bar": 1, "qty": "10"}])
        provider = FakeAccountProvider(positions_payloads=[[{
            "symbol": "AAPL", "qty": "10", "market_value": "2001.00",
            "instrument_id": 1001}]])
        pipeline = self.make_pipeline(
            subdir="reduce-recover", broker=broker, strategy=stub,
            artifacts="valid", account_provider=provider, run_gates=None)

        # recovery: the dangling order was adopted + cancel-attempted, and is
        # terminal — gates being OFF never blocked it (M5C-S3).
        cancels = pipeline.rows_of("orders", "post_submit_cancel_attempt")
        self.assertEqual([row["cause"] for row in cancels],
                         ["restart_unknown_state"])
        terminals = pipeline.rows_of("orders", "order_terminal")
        self.assertEqual([row["order_id"] for row in terminals],
                         [dangling_id])

        pipeline.tick_on_bar(50)   # scan runs; the open REFUSES at run_gates
        verdicts = pipeline.rows_of("risk", "risk_verdict")
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["gate_stage"], "run_gates")
        self.assertEqual(verdicts[0]["reasons"], ["run_gates_off"])
        self.assertEqual(api.submit_calls, [])    # zero open submits

        # the kill flatten still works (reduce path is never gate-blocked).
        pipeline.orch.trigger_kill("drill")
        self.assertEqual(pipeline.orch.risk_kill.state, "halted")
        self.assertEqual(len(api.submit_calls), 1)
        self.assertEqual(api.submit_calls[0]["side"], "sell")
        self.assertEqual(api.submit_calls[0]["client_order_id"],
                         "flatten-AAPL")
        position_closes = pipeline.rows_of("positions", "position_close")
        self.assertEqual(len(position_closes), 1)
        self.assertEqual(position_closes[0]["reason"], "kill_flatten")

    # ------------------------------------------------------------- golden smoke

    def test_run_synthetic_golden_produces_all_four_streams(self):
        pipeline = run_synthetic_golden(self.tmp / "golden")
        for stream in ("orders", "fills", "positions", "risk"):
            self.assertTrue(pipeline.rows(stream),
                            f"{stream}.jsonl should not be empty")
        opens = pipeline.rows_of("positions", "position_open")
        closes = pipeline.rows_of("positions", "position_close")
        self.assertEqual(len(opens), 1)
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["reason"], "synthetic_script")
        for row in pipeline.rows_of("orders", "order_submit_attempt"):
            self.assertTrue(row["order_id"].startswith("synthetic-o-"))


if __name__ == "__main__":
    unittest.main()
