"""M0 makes no network calls and needs no secrets (supports S1's spirit).

The agent modules import no broker/data SDK, the M0 flows open no socket, and the
suite is green with `.secrets/` absent.
"""
import socket
import sys
import types
import unittest
from decimal import Decimal
from unittest import mock


class TestNoSdkImported(unittest.TestCase):
    def test_no_broker_or_data_sdk_in_sys_modules(self):
        import agent.broker.alpaca  # noqa: F401
        import agent.marketdata.base  # noqa: F401

        self.assertNotIn("alpaca", sys.modules)
        self.assertNotIn("databento", sys.modules)


class TestNoSocketOpened(unittest.TestCase):
    def test_m0_flows_open_no_socket(self):
        from agent.broker.alpaca import AlpacaPaperBroker
        from agent.broker.base import OrderIntent
        from agent.execution_preflight import mint_reduce_only_token
        from agent.serializer import dumps

        with mock.patch("socket.socket", side_effect=AssertionError("M0 must not open sockets")):
            dumps({"x": Decimal("1.0")})
            broker = AlpacaPaperBroker()
            held = types.SimpleNamespace(symbol="AAPL", qty=Decimal("3"))
            intent = OrderIntent(
                symbol="AAPL", side="sell", qty=Decimal("1"), is_reducing=True, intent_id="r"
            )
            token = mint_reduce_only_token(held, intent)
            broker.submit_order(intent, token)
        # Reaching here without AssertionError proves no socket was created.
        self.assertEqual(len(broker.submitted), 1)


if __name__ == "__main__":
    unittest.main()
