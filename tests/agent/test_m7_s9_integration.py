"""M7 Wave 5 - S9 artifact gate integration."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import orchestrator as orch_mod
from agent.backtest_builder import write_m7_fixture_artifact
from agent.broker.alpaca import AlpacaPaperBroker
from agent.broker.fake import FakeBroker

from tests.lib.alpaca_fixtures import ScriptedOrderApi, order_payload
from tests.lib.exec_fixtures import (
    DATA_PIN_EXEC_V1,
    ExecPipeline,
    RealStrategyStub,
    committed_assembled_config,
)
from tests.lib.fakes import FakeClock
from tests.lib.risk_fixtures import FakeAccountProvider


class DirectionalStrategyStub(RealStrategyStub):
    strategy_id = "directional.momentum_v1"


def _ack(payload):
    return order_payload(
        client_order_id=payload["client_order_id"],
        symbol=payload["symbol"],
        qty=payload["qty"],
        status="new",
        filled_qty="0",
        filled_avg_price=None,
        limit_price=payload["limit_price"],
    )


def _write_artifact(pipeline, *, data_pin=DATA_PIN_EXEC_V1):
    return write_m7_fixture_artifact(
        artifacts_dir=pipeline.artifacts_dir,
        rules_hash=pipeline.exec_config.rules_hash,
        data_pin=data_pin,
        created_utc="2026-06-13T00:00:00.000000Z",
        input_manifest_hash="mh-s9-test",
        builder_git_commit="test",
        tier="fixture",
        fixture_net_pnl_usd="1.900000",
        allow_reviewed_artifact=False,
    )


class TestM7S9Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="m7-s9-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(orch_mod.unbind_runtime)

    def _pipeline(self, subdir, *, artifacts_data_pin=None):
        api = ScriptedOrderApi({"submit": [_ack]})
        pipeline = ExecPipeline(
            journal_dir=self.tmp / subdir,
            broker=AlpacaPaperBroker(order_api=api),
            strategy=DirectionalStrategyStub([{"on_bar": 1, "qty": "10"}]),
            run_gates="valid",
            artifacts=None,
            account_provider=FakeAccountProvider(),
        )
        self.addCleanup(pipeline.close)
        if artifacts_data_pin is not None:
            _write_artifact(pipeline, data_pin=artifacts_data_pin)
        return pipeline, api

    def test_real_strategy_missing_artifact_rejects_before_broker_submit(self):
        pipeline, api = self._pipeline("missing")

        pipeline.tick_on_bar(50)
        pipeline.tick_quote_only(50)

        rejects = pipeline.rows_of("orders", "reject")
        self.assertEqual(len(rejects), 1)
        self.assertIn("backtest_artifact_missing", rejects[0]["reasons"])
        self.assertEqual(api.submit_calls, [])
        self.assertEqual(pipeline.rows_of("orders", "order_submit_attempt"), [])

    def test_real_strategy_mismatched_artifact_rejects_before_broker_submit(self):
        pipeline, api = self._pipeline(
            "mismatch", artifacts_data_pin=DATA_PIN_EXEC_V1 + ":other")

        pipeline.tick_on_bar(50)
        pipeline.tick_quote_only(50)

        rejects = pipeline.rows_of("orders", "reject")
        self.assertEqual(len(rejects), 1)
        self.assertIn("artifact_key_mismatch", rejects[0]["reasons"])
        self.assertEqual(api.submit_calls, [])
        self.assertEqual(pipeline.rows_of("orders", "order_submit_attempt"), [])

    def test_valid_v2_artifact_allows_later_preflight_gates_to_submit(self):
        pipeline, api = self._pipeline("valid", artifacts_data_pin=DATA_PIN_EXEC_V1)

        pipeline.tick_on_bar(50)
        pipeline.tick_quote_only(50)

        self.assertEqual(len(api.submit_calls), 1)
        self.assertEqual(len(pipeline.rows_of("orders", "order_submit_attempt")),
                         1)
        self.assertEqual(pipeline.rows_of("orders", "reject"), [])

    def test_committed_config_valid_artifact_still_zero_submits(self):
        class _NoQuoteView:
            def latest(self, symbol, instrument_id):
                return None

        broker = FakeBroker(quote_view=_NoQuoteView(), clock=FakeClock(0),
                            instrument_ids={"AAPL": 1001})
        submit_spy = mock.MagicMock(wraps=broker.submit_order)
        broker.submit_order = submit_spy
        pipeline = ExecPipeline(
            journal_dir=self.tmp / "committed",
            broker=broker,
            strategy=DirectionalStrategyStub([{"on_bar": 1, "qty": "10"}]),
            config=committed_assembled_config(),
        )
        self.addCleanup(pipeline.close)
        _write_artifact(pipeline, data_pin=DATA_PIN_EXEC_V1)

        for index in range(50, 53):
            pipeline.tick_on_bar(index)

        self.assertGreater(pipeline.orch._strategy.scan_calls, 0)
        verdicts = pipeline.rows_of("risk", "risk_verdict")
        self.assertEqual({row["gate_stage"] for row in verdicts}, {"run_gates"})
        self.assertEqual({tuple(row["reasons"]) for row in verdicts},
                         {("run_gates_off",)})
        self.assertEqual(submit_spy.call_count, 0)
        self.assertEqual(pipeline.rows_of("orders", "order_submit_attempt"), [])


if __name__ == "__main__":
    unittest.main()
