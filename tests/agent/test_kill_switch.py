"""S8: kill-switch flattens-then-halts, reduce-only only (never an opening order).

Flatten submits closing / position-decreasing orders for held positions using
`ReduceOnlyPreflightToken`s, then halts. It can never mint an opening order, so
"nothing opens" (S1) is preserved even while risk is being reduced.
"""
import types
import unittest
from decimal import Decimal

from agent.broker.alpaca import AlpacaPaperBroker
from agent.kill_switch import KillSwitch


def _pos(symbol, qty):
    return types.SimpleNamespace(symbol=symbol, qty=Decimal(qty))


class TestFlattenThenHalt(unittest.TestCase):
    def test_trigger_flattens_all_held_positions(self):
        broker = AlpacaPaperBroker()
        ks = KillSwitch()
        ks.trigger(broker, [_pos("AAPL", "10"), _pos("MSFT", "5")])
        symbols = sorted(o.symbol for o in broker.submitted)
        self.assertEqual(symbols, ["AAPL", "MSFT"])

    def test_all_flatten_orders_are_reducing(self):
        broker = AlpacaPaperBroker()
        ks = KillSwitch()
        ks.trigger(broker, [_pos("AAPL", "10")])
        self.assertTrue(all(o.is_reducing for o in broker.submitted))

    def test_halt_reached_only_after_flatten(self):
        broker = AlpacaPaperBroker()
        ks = KillSwitch()
        self.assertEqual(ks.state, "active")
        ks.trigger(broker, [_pos("AAPL", "10")])
        self.assertEqual(ks.state, "halted")
        self.assertTrue(ks.is_halted())

    def test_halted_blocks_opening(self):
        broker = AlpacaPaperBroker()
        ks = KillSwitch()
        ks.trigger(broker, [])
        self.assertFalse(ks.allows_opening())

    def test_trigger_with_no_positions_just_halts(self):
        broker = AlpacaPaperBroker()
        ks = KillSwitch()
        ks.trigger(broker, [])
        self.assertEqual(broker.submitted, [])
        self.assertTrue(ks.is_halted())


if __name__ == "__main__":
    unittest.main()
