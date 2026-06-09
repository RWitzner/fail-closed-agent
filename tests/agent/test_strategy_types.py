"""M3 §M.5 — Candidate/Leg/Strategy pure types (§E). [S1 typing edge]

These types carry NO order authority — orders remain preflight-token-gated (M5).
No M3 module instantiates Candidate (the probe's S1 test asserts emitted types).
"""
import unittest
from decimal import Decimal

from agent.candidate import Candidate, Leg
from agent.strategy import ScanContext, Strategy


def _leg(**overrides):
    base = dict(symbol="AAPL", instrument_id=1001, side="buy",
                qty=Decimal("10"), limit_price=Decimal("200.0100"))
    base.update(overrides)
    return Leg(**base)


class TestLegValidation(unittest.TestCase):
    def test_valid_leg(self):
        leg = _leg()
        self.assertEqual(leg.qty, Decimal("10"))

    def test_zero_or_negative_qty_raises(self):
        with self.assertRaises(ValueError):
            _leg(qty=Decimal("0"))
        with self.assertRaises(ValueError):
            _leg(qty=Decimal("-1"))

    def test_nonfinite_qty_raises(self):
        with self.assertRaises(ValueError):
            _leg(qty=Decimal("NaN"))

    def test_float_qty_raises(self):
        with self.assertRaises(ValueError):
            _leg(qty=10.0)

    def test_bad_side_raises(self):
        with self.assertRaises(ValueError):
            _leg(side="short")

    def test_nonfinite_limit_raises(self):
        with self.assertRaises(ValueError):
            _leg(limit_price=Decimal("Infinity"))

    def test_none_limit_allowed(self):
        self.assertIsNone(_leg(limit_price=None).limit_price)

    def test_frozen(self):
        leg = _leg()
        with self.assertRaises(Exception):
            leg.qty = Decimal("99")


class TestCandidateValidation(unittest.TestCase):
    def test_valid_candidate(self):
        cand = Candidate(strategy_id="test", legs=(_leg(),),
                         paper_eligible=False, score=Decimal("0.1"))
        self.assertEqual(len(cand.legs), 1)

    def test_empty_legs_raises(self):
        with self.assertRaises(ValueError):
            Candidate(strategy_id="test", legs=(), paper_eligible=False, score=None)

    def test_nonfinite_score_raises(self):
        with self.assertRaises(ValueError):
            Candidate(strategy_id="test", legs=(_leg(),),
                      paper_eligible=False, score=Decimal("NaN"))

    def test_frozen(self):
        cand = Candidate(strategy_id="test", legs=(_leg(),),
                         paper_eligible=False, score=None)
        with self.assertRaises(Exception):
            cand.paper_eligible = True


class TestProtocolBoundaries(unittest.TestCase):
    def test_probe_does_not_satisfy_strategy_protocol(self):
        from agent.strategies.calibration_probe import CalibrationProbe
        self.assertFalse(isinstance(
            CalibrationProbe.__new__(CalibrationProbe), Strategy))

    def test_forecast_decision_is_not_a_candidate(self):
        from agent.strategies.calibration_probe import ForecastDecision
        self.assertFalse(issubclass(ForecastDecision, Candidate))

    def test_conforming_object_satisfies_protocol(self):
        class Conforming:
            strategy_id = "x"
            def scan(self, ctx):
                return []
        self.assertTrue(isinstance(Conforming(), Strategy))


if __name__ == "__main__":
    unittest.main()
