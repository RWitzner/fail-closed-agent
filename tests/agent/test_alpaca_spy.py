"""M0 AlpacaPaperBroker is a spy/no-op: requires a token, imports no alpaca-py.

It records accepted submissions and makes no network calls. Opening is impossible
in M0 (the open preflight rejects all), so the broker can only ever record a
reduce-only submission.
"""
import sys
import types
import unittest
from decimal import Decimal

from agent.broker.alpaca import AlpacaPaperBroker
from agent.broker.base import OrderIntent
from agent.execution_preflight import (
    PreflightForgery,
    PreflightRejected,
    mint_open_token,
    mint_reduce_only_token,
)

COMMITTED = {"agent_rules": {"enabled": False, "paper_trading": {"enabled": False}}}


def _reduce_intent():
    return OrderIntent(symbol="AAPL", side="sell", qty=Decimal("1"), is_reducing=True, intent_id="r1")


def _held():
    return types.SimpleNamespace(symbol="AAPL", qty=Decimal("10"))


class TestTokenRequired(unittest.TestCase):
    def test_submit_without_token_raises(self):
        b = AlpacaPaperBroker()
        with self.assertRaises(PreflightForgery):
            b.submit_order(_reduce_intent(), None)
        self.assertEqual(b.submitted, [])

    def test_submit_with_forged_token_raises(self):
        b = AlpacaPaperBroker()
        with self.assertRaises(PreflightForgery):
            b.submit_order(_reduce_intent(), types.SimpleNamespace(symbol="AAPL"))
        self.assertEqual(b.submitted, [])


class TestAcceptedSubmission(unittest.TestCase):
    def test_records_accepted_reduce_only_order(self):
        b = AlpacaPaperBroker()
        token = mint_reduce_only_token(_held(), _reduce_intent())
        ack = b.submit_order(_reduce_intent(), token)
        self.assertEqual(len(b.submitted), 1)
        self.assertIn("order_id", ack)


class TestNoOpeningInM0(unittest.TestCase):
    def test_cannot_open_on_committed_config(self):
        # FD-M5-14: the mint takes PreflightInputs; on committed gates the reject
        # is the byte-exact run_gates terminal (S1 coverage preserved).
        from agent.exec_reasons import PREFLIGHT_STAGES
        from tests.agent.test_execution_preflight_m5 import golden_inputs

        with self.assertRaises(PreflightRejected) as caught:
            mint_open_token(golden_inputs(gates_config=COMMITTED))
        reject = caught.exception.reject
        self.assertEqual(reject.reasons, ("run_gates_off",))
        self.assertEqual(reject.gate_stage, "run_gates")
        self.assertEqual(reject.stages_skipped, PREFLIGHT_STAGES[1:])
        self.assertIsNone(reject.capped_limit)


class TestNoSdkImport(unittest.TestCase):
    def test_module_does_not_import_alpaca_sdk(self):
        self.assertNotIn("alpaca", sys.modules)


if __name__ == "__main__":
    unittest.main()
