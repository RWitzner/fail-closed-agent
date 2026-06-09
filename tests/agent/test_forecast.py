"""M3 §M.6 — stable logistic forecaster (§F). [S2]"""
import math
import unittest
from decimal import Decimal, ROUND_HALF_EVEN

from agent.forecast import (
    Forecast, ForecastEvent, NonFiniteFeature, P_MAX, P_MIN, PROB_QUANTUM, predict,
)
from agent.signal_config import FEATURE_NAMES

EVENT = ForecastEvent(
    horizon="5m", threshold_k=Decimal("0"),
    event_start_bar_end_utc="2026-06-15T14:21:00.000000Z",
    resolve_bar_end_utc="2026-06-15T14:26:00.000000Z",
)

COEFFS = {
    "intercept": Decimal("0"), "z_ret_21": Decimal("0.05"),
    "momentum_9": Decimal("1.0"), "momentum_21": Decimal("0.5"),
    "rsi14_centered": Decimal("0.20"), "ema_gap_9_21": Decimal("5.0"),
    "sma_gap_21_50": Decimal("2.5"), "realized_vol_21": Decimal("0"),
}


def _features(**overrides):
    base = {name: "0.00000000" for name in FEATURE_NAMES}
    base.update(overrides)
    return base


def _call(features, coeffs=None):
    return predict(features, coefficients=coeffs or COEFFS,
                   model_version="logit-mom-v1", model_artifact_hash="deadbeef",
                   event=EVENT)


class TestGoldenValues(unittest.TestCase):
    def test_zero_vector_gives_half(self):
        forecast = _call(_features())
        self.assertEqual(forecast.p, Decimal("0.500000"))

    def test_hand_computed_z(self):
        features = _features(z_ret_21="0.50000000", momentum_9="0.01000000",
                             rsi14_centered="0.20000000")
        z = 0.05 * 0.5 + 1.0 * 0.01 + 0.20 * 0.20
        expected = Decimal(repr(1.0 / (1.0 + math.exp(-z)))).quantize(
            PROB_QUANTUM, ROUND_HALF_EVEN)
        forecast = _call(features)
        self.assertEqual(forecast.p, expected)
        self.assertEqual(forecast.event, EVENT)
        self.assertEqual(forecast.model_version, "logit-mom-v1")

    def test_determinism(self):
        features = _features(momentum_21="0.00123400")
        self.assertEqual(str(_call(features).p), str(_call(features).p))


class TestClampAndStability(unittest.TestCase):
    def test_z_clamp_pins_p_to_pmax_exactly(self):
        # momentum_9 coefficient 1.0: a feature value of 1e6 -> z clamped to +30,
        # sigmoid(30) quantizes to 1.000000 -> clamped to P_MAX exactly (never 1).
        forecast = _call(_features(momentum_9="1000000.00000000"))
        self.assertEqual(forecast.p, P_MAX)

    def test_z_clamp_pins_p_to_pmin_exactly(self):
        forecast = _call(_features(momentum_9="-1000000.00000000"))
        self.assertEqual(forecast.p, P_MIN)

    def test_extreme_coefficients_finite(self):
        coeffs = dict(COEFFS)
        coeffs["ema_gap_9_21"] = Decimal("1E+20")
        forecast = _call(_features(ema_gap_9_21="0.99999999"), coeffs)
        self.assertTrue(forecast.p.is_finite())
        self.assertTrue(P_MIN <= forecast.p <= P_MAX)


class TestValidation(unittest.TestCase):
    def test_missing_coefficient_key_raises(self):
        coeffs = dict(COEFFS)
        del coeffs["momentum_9"]
        with self.assertRaises(ValueError):
            _call(_features(), coeffs)

    def test_extra_coefficient_key_raises(self):
        coeffs = dict(COEFFS)
        coeffs["smuggled"] = Decimal("1")
        with self.assertRaises(ValueError):
            _call(_features(), coeffs)

    def test_missing_feature_raises_nonfinite(self):
        features = _features()
        del features["z_ret_21"]
        with self.assertRaises(NonFiniteFeature):
            _call(features)

    def test_nonfinite_feature_raises(self):
        with self.assertRaises(NonFiniteFeature):
            _call(_features(z_ret_21="NaN"))
        with self.assertRaises(NonFiniteFeature):
            _call(_features(z_ret_21="Infinity"))

    def test_unparseable_feature_raises(self):
        with self.assertRaises(NonFiniteFeature):
            _call(_features(z_ret_21="not-a-number"))


if __name__ == "__main__":
    unittest.main()
