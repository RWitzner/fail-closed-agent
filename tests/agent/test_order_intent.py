"""OrderIntent rejects non-finite / non-Decimal qty and limit_price (spec S2 spirit).

Floats must never reach the broker submission path — qty/limit_price are Decimal,
finite, and (for qty) positive.
"""
import unittest
from decimal import Decimal

from agent.broker.base import OrderIntent


class TestOrderIntentValidation(unittest.TestCase):
    def test_decimal_qty_ok(self):
        OrderIntent(symbol="AAPL", side="buy", qty=Decimal("1"))

    def test_float_qty_rejected(self):
        with self.assertRaises(ValueError):
            OrderIntent(symbol="AAPL", side="buy", qty=1.0)

    def test_int_qty_rejected(self):
        with self.assertRaises(ValueError):
            OrderIntent(symbol="AAPL", side="buy", qty=1)

    def test_nan_qty_rejected(self):
        with self.assertRaises(ValueError):
            OrderIntent(symbol="AAPL", side="buy", qty=Decimal("NaN"))

    def test_nonpositive_qty_rejected(self):
        with self.assertRaises(ValueError):
            OrderIntent(symbol="AAPL", side="buy", qty=Decimal("0"))

    def test_float_limit_price_rejected(self):
        with self.assertRaises(ValueError):
            OrderIntent(symbol="AAPL", side="buy", qty=Decimal("1"), limit_price=1.5)

    def test_decimal_limit_price_ok(self):
        OrderIntent(symbol="AAPL", side="buy", qty=Decimal("1"), limit_price=Decimal("1.50"))

    def test_none_limit_price_ok(self):
        OrderIntent(symbol="AAPL", side="buy", qty=Decimal("1"), limit_price=None)


if __name__ == "__main__":
    unittest.main()
