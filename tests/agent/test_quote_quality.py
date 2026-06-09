"""M3 §M.1 — quote-quality filters (§A). [S2]

PURE verdicts: warnings-as-data, never raises on bad market data; raises only on
programming errors (float inputs). All applicable reasons collected, sorted.
"""
import unittest
from decimal import Decimal

from agent.quote_quality import BPS_QUANTUM, MID_QUANTUM, QuoteSnapshot, QuoteVerdict, evaluate


def _snapshot(**overrides):
    base = dict(
        symbol="AAPL",
        instrument_id=1001,
        bid=Decimal("199.9900"),
        ask=Decimal("200.0100"),
        bid_sz=Decimal("300"),
        ask_sz=Decimal("200"),
        ts_event_utc="2026-06-15T14:00:00.000000Z",
        ts_recv_utc="2026-06-15T14:00:00.050000Z",
        seen_at_ms=1_000,
        reconnect_epoch=0,
        vendor_seq=42,
        dataset="EQUS.MINI",
        schema="tbbo",
    )
    base.update(overrides)
    return QuoteSnapshot(**base)


def _evaluate(q, *, now_ms=1_000, spread_bps_max=Decimal("50"), staleness_ms_max=2000):
    return evaluate(q, now_ms=now_ms, spread_bps_max=spread_bps_max,
                    staleness_ms_max=staleness_ms_max)


class TestAcceptPath(unittest.TestCase):
    def test_clean_quote_accepted_with_exact_mid_and_spread(self):
        verdict = _evaluate(_snapshot())
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.reasons, ())
        # mid = (199.99 + 200.01)/2 = 200.000000 exactly (MID_QUANTUM 1e-6)
        self.assertEqual(verdict.mid, Decimal("200.000000"))
        # spread = 0.02 / 200 * 10_000 = 1.00 bps exactly (BPS_QUANTUM 1e-2)
        self.assertEqual(verdict.spread_bps, Decimal("1.00"))
        self.assertEqual(verdict.age_ms, 0)

    def test_mid_quantum_exact_for_4dp_prices(self):
        # (bid+ask)/2 of 4dp prices is at most 5dp -> exact on the 1e-6 grid.
        verdict = _evaluate(_snapshot(bid=Decimal("100.0001"), ask=Decimal("100.0002")))
        self.assertEqual(verdict.mid, Decimal("100.000150"))
        self.assertEqual(verdict.mid.as_tuple().exponent, MID_QUANTUM.as_tuple().exponent)

    def test_spread_bps_quantized_to_2dp(self):
        verdict = _evaluate(_snapshot())
        self.assertEqual(verdict.spread_bps.as_tuple().exponent,
                         BPS_QUANTUM.as_tuple().exponent)


class TestRejectReasons(unittest.TestCase):
    def test_one_sided_when_price_side_none(self):
        verdict = _evaluate(_snapshot(bid=None, bid_sz=None))
        self.assertFalse(verdict.ok)
        self.assertIn("quote_one_sided", verdict.reasons)
        self.assertIsNone(verdict.mid)
        self.assertIsNone(verdict.spread_bps)

    def test_one_sided_when_only_size_none_keeps_mid_inspectable(self):
        # rev2 BUILD-F3: a size-derived one_sided does NOT suppress mid/spread.
        verdict = _evaluate(_snapshot(ask_sz=None))
        self.assertFalse(verdict.ok)
        self.assertIn("quote_one_sided", verdict.reasons)
        self.assertEqual(verdict.mid, Decimal("200.000000"))
        self.assertEqual(verdict.spread_bps, Decimal("1.00"))

    def test_nonfinite_decimal_is_reason_not_exception(self):
        verdict = _evaluate(_snapshot(bid=Decimal("NaN")))
        self.assertFalse(verdict.ok)
        self.assertIn("quote_nonfinite", verdict.reasons)
        self.assertIsNone(verdict.mid)

    def test_nonpositive_price(self):
        verdict = _evaluate(_snapshot(bid=Decimal("0")))
        self.assertFalse(verdict.ok)
        self.assertIn("quote_nonpositive", verdict.reasons)
        self.assertIsNone(verdict.mid)

    def test_nonpositive_size(self):
        verdict = _evaluate(_snapshot(bid_sz=Decimal("0")))
        self.assertFalse(verdict.ok)
        self.assertIn("quote_nonpositive", verdict.reasons)
        self.assertEqual(verdict.mid, Decimal("200.000000"))

    def test_crossed_vs_locked_distinct(self):
        crossed = _evaluate(_snapshot(bid=Decimal("200.0200"), ask=Decimal("200.0100")))
        self.assertIn("quote_crossed", crossed.reasons)
        self.assertNotIn("quote_locked", crossed.reasons)
        locked = _evaluate(_snapshot(bid=Decimal("200.0100"), ask=Decimal("200.0100")))
        self.assertIn("quote_locked", locked.reasons)
        self.assertNotIn("quote_crossed", locked.reasons)

    def test_staleness_strict_greater_boundary(self):
        fresh = _evaluate(_snapshot(), now_ms=3_000)   # age == 2000 == max -> fresh
        self.assertTrue(fresh.ok)
        self.assertEqual(fresh.age_ms, 2000)
        stale = _evaluate(_snapshot(), now_ms=3_001)   # age == 2001 > max -> stale
        self.assertFalse(stale.ok)
        self.assertEqual(stale.reasons, ("quote_stale",))
        self.assertEqual(stale.age_ms, 2001)

    def test_spread_too_wide_uses_quantized_field(self):
        # raw spread = 0.0500/9.9750 * 1e4 = 50.1253...; quantized 50.13 > 50 -> reject
        verdict = _evaluate(_snapshot(bid=Decimal("9.9500"), ask=Decimal("10.0000"),))
        self.assertIn("spread_too_wide", verdict.reasons)
        # rev2 MATH-Q8: a raw spread in (50, 50.005] quantizes to 50.00 and PASSES.
        # bid=99.7510, ask=100.2500 -> spread 0.4990/100.000500*1e4 = 49.8997 bps... pick exact:
        # Construct spread_bps raw = 50.004999 -> quantized 50.00 -> ok.
        # mid=10.000250, spread=0.050003/10.000250*1e4 = 50.0017... too coarse at 4dp ticks;
        # assert instead the boundary equality case: quantized == max passes (strict >).
        at_max = _evaluate(_snapshot(bid=Decimal("199.5000"), ask=Decimal("200.5000")))
        # mid=200.000000, spread = 1.0/200*1e4 = 50.00 == max -> NOT too wide
        self.assertNotIn("spread_too_wide", at_max.reasons)
        self.assertTrue(at_max.ok)

    def test_all_applicable_reasons_collected_sorted(self):
        verdict = _evaluate(
            _snapshot(bid=Decimal("200.0200"), ask=Decimal("200.0100"), ask_sz=Decimal("0")),
            now_ms=10_000,
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reasons, tuple(sorted(verdict.reasons)))
        self.assertIn("quote_crossed", verdict.reasons)
        self.assertIn("quote_nonpositive", verdict.reasons)
        self.assertIn("quote_stale", verdict.reasons)


class TestProgrammingErrors(unittest.TestCase):
    def test_float_price_raises(self):
        with self.assertRaises(ValueError):
            _evaluate(_snapshot(bid=200.0))

    def test_float_size_raises(self):
        with self.assertRaises(ValueError):
            _evaluate(_snapshot(ask_sz=100.0))

    def test_float_spread_max_raises(self):
        with self.assertRaises(ValueError):
            evaluate(_snapshot(), now_ms=0, spread_bps_max=50.0, staleness_ms_max=2000)

    def test_verdict_is_frozen(self):
        verdict = _evaluate(_snapshot())
        with self.assertRaises(Exception):
            verdict.ok = False


if __name__ == "__main__":
    unittest.main()
