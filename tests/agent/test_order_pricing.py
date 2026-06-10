"""M5 §R test 2 — order_pricing: tick grid + frozen marketable-limit cap. [S2, S4-economics]

PURE Decimal math, hand-computed expectations throughout. Pins:
- tick_for boundaries ($1.00 => 0.01; $0.9999 => 0.0001); on_tick_grid exactness.
- BUY cap directed ROUND_DOWN toward the budget; SELL cap ROUND_UP; min/max with
  strategy_limit; boundary-equal IS marketable.
- Sub-$1 4dp grid (the Alpaca 42210000 mirror) + the $1.00 boundary crossing on
  tick_for(raw) of the RAW value (§C frozen).
- latency_lost_edge STRICT (>) on the QUANTIZED adverse_move_bps — THE authoritative
  form (EX-1): the boundary pair ask_A=0.7999 -> ask_B=0.8019 @ 25 bps raw-fires
  (25.0031...) but quantizes to 25.00 and must NOT fire.
- reduce_cap direction pinned (EX-7): rounds AWAY from the touch, the deliberate
  inverse of the open path's budget-respect rounding (sell, bid=1.02, bps=25 => 1.01).
- Float injection raises (S2 posture, ExecError identity).
"""
import dataclasses
import unittest
from decimal import Decimal

from agent.exec_reasons import ExecError
from agent.order_pricing import (
    CapResult,
    marketable_limit_cap,
    on_tick_grid,
    reduce_cap,
    tick_for,
)
from agent.quote_quality import QuoteSnapshot


def _quote(**overrides):
    """QuoteSnapshot builder (the test_quote_quality.py `_snapshot` pattern)."""
    base = dict(
        symbol="AAPL",
        instrument_id=1001,
        bid=Decimal("99.9800"),
        ask=Decimal("99.9900"),
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


def _cap(*, side="buy", quote_a=None, quote_b=None,
         slippage_cap_bps=Decimal("25"), strategy_limit=None):
    if quote_a is None:
        quote_a = _quote()
    if quote_b is None:
        quote_b = quote_a
    return marketable_limit_cap(
        side=side, quote_a=quote_a, quote_b=quote_b,
        slippage_cap_bps=slippage_cap_bps, strategy_limit=strategy_limit,
    )


class TestTickFor(unittest.TestCase):
    def test_one_dollar_boundary_is_penny_grid(self):
        self.assertEqual(tick_for(Decimal("1.00")), Decimal("0.01"))

    def test_just_under_one_dollar_is_4dp_grid(self):
        self.assertEqual(tick_for(Decimal("0.9999")), Decimal("0.0001"))

    def test_above_a_dollar(self):
        self.assertEqual(tick_for(Decimal("200.51")), Decimal("0.01"))

    def test_deep_sub_dollar(self):
        self.assertEqual(tick_for(Decimal("0.0001")), Decimal("0.0001"))

    def test_zero_raises_exec_error(self):
        with self.assertRaises(ExecError):
            tick_for(Decimal("0"))

    def test_negative_raises_exec_error(self):
        with self.assertRaises(ExecError):
            tick_for(Decimal("-1.00"))

    def test_nan_raises_exec_error(self):
        with self.assertRaises(ExecError):
            tick_for(Decimal("NaN"))

    def test_infinity_raises_exec_error(self):
        with self.assertRaises(ExecError):
            tick_for(Decimal("Infinity"))

    def test_float_raises(self):
        with self.assertRaises(ExecError):
            tick_for(1.0)


class TestOnTickGrid(unittest.TestCase):
    def test_penny_grid_exact(self):
        self.assertTrue(on_tick_grid(Decimal("1.01")))
        self.assertTrue(on_tick_grid(Decimal("200.01")))

    def test_penny_grid_off(self):
        self.assertFalse(on_tick_grid(Decimal("1.005")))
        self.assertFalse(on_tick_grid(Decimal("100.105")))

    def test_sub_dollar_4dp_grid(self):
        self.assertTrue(on_tick_grid(Decimal("0.1234")))
        self.assertFalse(on_tick_grid(Decimal("0.12345")))

    def test_trailing_zeros_are_on_grid(self):
        # exactness is a value property, not a representation property.
        self.assertTrue(on_tick_grid(Decimal("1.0100")))

    def test_nonpositive_propagates_exec_error(self):
        with self.assertRaises(ExecError):
            on_tick_grid(Decimal("0"))


class TestBuyCap(unittest.TestCase):
    def test_round_down_toward_budget(self):
        # raw = 99.99 * (1 + 25/10000) = 99.99 * 1.0025 = 100.2399750 (exact, prec 28)
        # ROUND_DOWN @ 0.01 -> 100.23  (ROUND_HALF_EVEN would give 100.24 — direction matters)
        q = _quote(ask=Decimal("99.99"))
        res = _cap(side="buy", quote_a=q, quote_b=q)
        self.assertEqual(res.capped_limit, Decimal("100.23"))
        self.assertTrue(on_tick_grid(res.capped_limit))
        self.assertTrue(res.marketable)          # 100.23 >= 99.99
        self.assertEqual(res.reasons, ())
        self.assertEqual(res.adverse_move_bps, Decimal("0.00"))  # A == B touch

    def test_boundary_equal_is_marketable_at_zero_bps(self):
        # bps=0: raw == ask_B == cap; capped_limit == ask_B -> marketable (§C: boundary-equal IS marketable)
        q = _quote(ask=Decimal("200.01"))
        res = _cap(side="buy", quote_a=q, quote_b=q, slippage_cap_bps=Decimal("0"))
        self.assertEqual(res.capped_limit, Decimal("200.01"))
        self.assertTrue(res.marketable)
        self.assertEqual(res.reasons, ())


class TestSellCap(unittest.TestCase):
    def test_round_up_toward_budget(self):
        # raw = 99.99 * (1 - 25/10000) = 99.99 * 0.9975 = 99.7400250 (exact)
        # ROUND_UP @ 0.01 -> 99.75  (ROUND_HALF_EVEN/ROUND_DOWN would give 99.74)
        q = _quote(bid=Decimal("99.99"))
        res = _cap(side="sell", quote_a=q, quote_b=q)
        self.assertEqual(res.capped_limit, Decimal("99.75"))
        self.assertTrue(on_tick_grid(res.capped_limit))
        self.assertTrue(res.marketable)          # 99.75 <= 99.99
        self.assertEqual(res.reasons, ())


class TestStrategyLimit(unittest.TestCase):
    def test_buy_min_with_strategy_limit(self):
        # cap = 100.23 (above); strategy says worst 100.10 -> min = 100.10, still marketable
        q = _quote(ask=Decimal("99.99"))
        res = _cap(side="buy", quote_a=q, quote_b=q, strategy_limit=Decimal("100.10"))
        self.assertEqual(res.capped_limit, Decimal("100.10"))
        self.assertTrue(res.marketable)
        self.assertEqual(res.reasons, ())

    def test_buy_strategy_limit_below_ask_not_marketable(self):
        q = _quote(ask=Decimal("99.99"))
        res = _cap(side="buy", quote_a=q, quote_b=q, strategy_limit=Decimal("99.50"))
        self.assertEqual(res.capped_limit, Decimal("99.50"))
        self.assertFalse(res.marketable)
        self.assertEqual(res.reasons, ("not_marketable",))

    def test_buy_strategy_limit_equal_to_ask_is_marketable(self):
        # boundary-equal IS marketable (§C frozen).
        q = _quote(ask=Decimal("99.99"))
        res = _cap(side="buy", quote_a=q, quote_b=q, strategy_limit=Decimal("99.99"))
        self.assertEqual(res.capped_limit, Decimal("99.99"))
        self.assertTrue(res.marketable)
        self.assertEqual(res.reasons, ())

    def test_sell_max_with_strategy_limit(self):
        # cap = 99.75; strategy floor 99.90 -> max = 99.90, marketable (99.90 <= 99.99)
        q = _quote(bid=Decimal("99.99"))
        res = _cap(side="sell", quote_a=q, quote_b=q, strategy_limit=Decimal("99.90"))
        self.assertEqual(res.capped_limit, Decimal("99.90"))
        self.assertTrue(res.marketable)
        self.assertEqual(res.reasons, ())

    def test_sell_strategy_limit_above_bid_not_marketable(self):
        q = _quote(bid=Decimal("99.99"))
        res = _cap(side="sell", quote_a=q, quote_b=q, strategy_limit=Decimal("100.05"))
        self.assertEqual(res.capped_limit, Decimal("100.05"))
        self.assertFalse(res.marketable)
        self.assertEqual(res.reasons, ("not_marketable",))


class TestSubDollarGrid(unittest.TestCase):
    """The Alpaca sub-penny 42210000 mirror: < $1.00 prices live on a 4dp grid."""

    def test_buy_sub_dollar_cap_on_4dp_grid(self):
        # raw = 0.5000 * 1.0025 = 0.501250; ROUND_DOWN @ 0.0001 -> 0.5012
        q = _quote(bid=Decimal("0.4990"), ask=Decimal("0.5000"))
        res = _cap(side="buy", quote_a=q, quote_b=q)
        self.assertEqual(res.capped_limit, Decimal("0.5012"))
        self.assertEqual(res.capped_limit.as_tuple().exponent, -4)
        self.assertTrue(on_tick_grid(res.capped_limit))
        self.assertTrue(res.marketable)

    def test_sell_sub_dollar_cap_round_up(self):
        # raw = 0.5000 * 0.9975 = 0.498750; ROUND_UP @ 0.0001 -> 0.4988
        q = _quote(bid=Decimal("0.5000"), ask=Decimal("0.5010"))
        res = _cap(side="sell", quote_a=q, quote_b=q)
        self.assertEqual(res.capped_limit, Decimal("0.4988"))
        self.assertTrue(res.marketable)

    def test_dollar_boundary_uses_tick_of_raw(self):
        # ask_B = 0.9999 (4dp grid) but raw = 0.9999 * 1.0025 = 1.00239975 >= 1.00,
        # so tick_for(raw) = 0.01 decides (§C frozen): ROUND_DOWN -> 1.00; marketable.
        q = _quote(bid=Decimal("0.9990"), ask=Decimal("0.9999"))
        res = _cap(side="buy", quote_a=q, quote_b=q)
        self.assertEqual(res.capped_limit, Decimal("1.00"))
        self.assertTrue(res.marketable)          # 1.00 >= 0.9999
        self.assertEqual(res.reasons, ())

    def test_sub_dollar_strategy_limit_off_4dp_grid_is_invalid_tick(self):
        q = _quote(bid=Decimal("0.4990"), ask=Decimal("0.5000"))
        res = _cap(side="buy", quote_a=q, quote_b=q, strategy_limit=Decimal("0.50005"))
        self.assertIn("invalid_tick", res.reasons)
        self.assertIsNone(res.capped_limit)
        self.assertFalse(res.marketable)


class TestLatencyLostEdge(unittest.TestCase):
    def test_strict_boundary_exactly_at_cap_does_not_fire(self):
        # adverse = (100.25 - 100.00)/100.00 * 10000 = 25.00 exactly; 25.00 > 25 is False.
        qa = _quote(ask=Decimal("100.00"))
        qb = _quote(ask=Decimal("100.25"))
        res = _cap(side="buy", quote_a=qa, quote_b=qb)
        self.assertEqual(res.adverse_move_bps, Decimal("25.00"))
        self.assertNotIn("latency_lost_edge", res.reasons)
        self.assertEqual(res.reasons, ())

    def test_just_past_cap_fires(self):
        # adverse = (100.26 - 100.00)/100.00 * 10000 = 26.00 > 25 -> fires.
        # cap itself: 100.26 * 1.0025 = 100.510650 -> 100.51, still marketable.
        qa = _quote(ask=Decimal("100.00"))
        qb = _quote(ask=Decimal("100.26"))
        res = _cap(side="buy", quote_a=qa, quote_b=qb)
        self.assertEqual(res.adverse_move_bps, Decimal("26.00"))
        self.assertEqual(res.reasons, ("latency_lost_edge",))
        self.assertEqual(res.capped_limit, Decimal("100.51"))
        self.assertTrue(res.marketable)

    def test_ex1_boundary_pair_quantized_form_does_not_fire(self):
        # EX-1 (§C frozen): ask_A=0.7999 -> ask_B=0.8019 @ 25 bps.
        # raw adverse = 0.0020/0.7999*10000 = 25.0031... -> the RAW form WOULD fire,
        raw_bps = Decimal("0.0020") / Decimal("0.7999") * Decimal("10000")
        self.assertGreater(raw_bps, Decimal("25"))
        # but the quantized form (BPS_QUANTUM, ROUND_HALF_EVEN) is 25.00 and is THE
        # authoritative comparison: 25.00 > 25 is False -> must NOT fire.
        qa = _quote(bid=Decimal("0.7989"), ask=Decimal("0.7999"))
        qb = _quote(bid=Decimal("0.8009"), ask=Decimal("0.8019"))
        res = _cap(side="buy", quote_a=qa, quote_b=qb)
        self.assertEqual(res.adverse_move_bps, Decimal("25.00"))
        self.assertNotIn("latency_lost_edge", res.reasons)
        self.assertEqual(res.reasons, ())
        # cap: 0.8019 * 1.0025 = 0.80390475 -> tick 0.0001, ROUND_DOWN -> 0.8039
        self.assertEqual(res.capped_limit, Decimal("0.8039"))
        self.assertTrue(res.marketable)

    def test_buy_favorable_move_is_signed_negative(self):
        # ask fell: (99.90 - 100.00)/100.00 * 10000 = -10.00; never fires.
        qa = _quote(ask=Decimal("100.00"))
        qb = _quote(ask=Decimal("99.90"))
        res = _cap(side="buy", quote_a=qa, quote_b=qb)
        self.assertEqual(res.adverse_move_bps, Decimal("-10.00"))
        self.assertEqual(res.reasons, ())

    def test_sell_mirror_on_bids_sign_flipped_adverse_positive(self):
        # bid fell 100.00 -> 99.70: adverse = (100.00 - 99.70)/100.00 * 10000 = 30.00 -> fires.
        qa = _quote(bid=Decimal("100.00"))
        qb = _quote(bid=Decimal("99.70"))
        res = _cap(side="sell", quote_a=qa, quote_b=qb)
        self.assertEqual(res.adverse_move_bps, Decimal("30.00"))
        self.assertIn("latency_lost_edge", res.reasons)

    def test_sell_favorable_move_negative_does_not_fire(self):
        # bid rose 100.00 -> 100.10: adverse = -10.00.
        qa = _quote(bid=Decimal("100.00"))
        qb = _quote(bid=Decimal("100.10"))
        res = _cap(side="sell", quote_a=qa, quote_b=qb)
        self.assertEqual(res.adverse_move_bps, Decimal("-10.00"))
        self.assertNotIn("latency_lost_edge", res.reasons)


class TestUnpriceableCandidate(unittest.TestCase):
    def test_zero_strategy_limit(self):
        res = _cap(side="buy", strategy_limit=Decimal("0"))
        self.assertEqual(res.reasons, ("unpriceable_candidate",))
        self.assertIsNone(res.capped_limit)
        self.assertFalse(res.marketable)

    def test_negative_strategy_limit(self):
        res = _cap(side="buy", strategy_limit=Decimal("-1"))
        self.assertEqual(res.reasons, ("unpriceable_candidate",))
        self.assertIsNone(res.capped_limit)

    def test_buy_quote_b_ask_missing(self):
        qb = _quote(ask=None, ask_sz=None)
        res = _cap(side="buy", quote_a=_quote(), quote_b=qb)
        self.assertEqual(res.reasons, ("unpriceable_candidate",))
        self.assertIsNone(res.capped_limit)
        self.assertFalse(res.marketable)
        # adverse not measurable -> quantized zero, still a Decimal on BPS_QUANTUM.
        self.assertEqual(res.adverse_move_bps, Decimal("0.00"))

    def test_buy_quote_b_ask_nonfinite(self):
        qb = _quote(ask=Decimal("NaN"))
        res = _cap(side="buy", quote_a=_quote(), quote_b=qb)
        self.assertEqual(res.reasons, ("unpriceable_candidate",))
        self.assertIsNone(res.capped_limit)

    def test_buy_quote_b_ask_nonpositive(self):
        qb = _quote(ask=Decimal("0"))
        res = _cap(side="buy", quote_a=_quote(), quote_b=qb)
        self.assertEqual(res.reasons, ("unpriceable_candidate",))
        self.assertIsNone(res.capped_limit)

    def test_sell_quote_b_bid_missing(self):
        qb = _quote(bid=None, bid_sz=None)
        res = _cap(side="sell", quote_a=_quote(), quote_b=qb)
        self.assertEqual(res.reasons, ("unpriceable_candidate",))
        self.assertIsNone(res.capped_limit)

    def test_quote_a_side_missing_does_not_make_unpriceable(self):
        # §C freezes "unpriceable_candidate iff strategy_limit <= 0 or the needed
        # quote-B side is missing/non-finite/<=0" — an unusable quote-A touch (stage 8's
        # job) only makes the adverse move unmeasurable (0.00, no latency fire).
        qa = _quote(ask=None, ask_sz=None)
        qb = _quote(ask=Decimal("99.99"))
        res = _cap(side="buy", quote_a=qa, quote_b=qb)
        self.assertEqual(res.reasons, ())
        self.assertEqual(res.capped_limit, Decimal("100.23"))
        self.assertEqual(res.adverse_move_bps, Decimal("0.00"))
        self.assertTrue(res.marketable)


class TestInvalidTickAndCollectAll(unittest.TestCase):
    def test_strategy_limit_off_penny_grid(self):
        res = _cap(side="buy", strategy_limit=Decimal("100.105"))
        self.assertEqual(res.reasons, ("invalid_tick",))
        self.assertIsNone(res.capped_limit)
        self.assertFalse(res.marketable)

    def test_reasons_collected_and_sorted(self):
        # invalid_tick (off-grid limit) + unpriceable_candidate (quote-B ask missing).
        qb = _quote(ask=None, ask_sz=None)
        res = _cap(side="buy", quote_a=_quote(), quote_b=qb,
                   strategy_limit=Decimal("100.105"))
        self.assertEqual(res.reasons, ("invalid_tick", "unpriceable_candidate"))
        self.assertEqual(res.reasons, tuple(sorted(res.reasons)))

    def test_invalid_tick_coexists_with_latency_lost_edge(self):
        qa = _quote(ask=Decimal("100.00"))
        qb = _quote(ask=Decimal("100.26"))
        res = _cap(side="buy", quote_a=qa, quote_b=qb,
                   strategy_limit=Decimal("100.105"))
        self.assertEqual(res.reasons, ("invalid_tick", "latency_lost_edge"))
        self.assertIsNone(res.capped_limit)


class TestReduceCap(unittest.TestCase):
    def test_sell_direction_pinned_ex7(self):
        # EX-7 frozen direction pin: sell, bid=1.02, bps=25.
        # raw = 1.02 * 0.9975 = 1.017450; ROUND_DOWN @ 0.01 -> 1.01 — one tick PAST the
        # 25 bps budget, AWAY from the touch (budget-respect ROUND_UP would give 1.02).
        q = _quote(bid=Decimal("1.02"), ask=Decimal("1.03"))
        self.assertEqual(reduce_cap(side="sell", quote=q, cap_bps=Decimal("25")),
                         Decimal("1.01"))

    def test_buy_to_cover_rounds_up_away_from_touch(self):
        # raw = 1.02 * 1.0025 = 1.022550; ROUND_UP @ 0.01 -> 1.03.
        q = _quote(bid=Decimal("1.01"), ask=Decimal("1.02"))
        self.assertEqual(reduce_cap(side="buy", quote=q, cap_bps=Decimal("25")),
                         Decimal("1.03"))

    def test_sub_dollar_sell_on_4dp_grid(self):
        # raw = 0.5000 * 0.9975 = 0.498750; ROUND_DOWN @ 0.0001 -> 0.4987
        # (the open-path SELL cap on the same numbers is 0.4988 — EX-7 inverse).
        q = _quote(bid=Decimal("0.5000"), ask=Decimal("0.5010"))
        self.assertEqual(reduce_cap(side="sell", quote=q, cap_bps=Decimal("25")),
                         Decimal("0.4987"))

    def test_none_on_missing_side(self):
        self.assertIsNone(reduce_cap(side="sell",
                                     quote=_quote(bid=None, bid_sz=None),
                                     cap_bps=Decimal("25")))
        self.assertIsNone(reduce_cap(side="buy",
                                     quote=_quote(ask=None, ask_sz=None),
                                     cap_bps=Decimal("25")))

    def test_none_on_nonpositive_side(self):
        self.assertIsNone(reduce_cap(side="sell", quote=_quote(bid=Decimal("0")),
                                     cap_bps=Decimal("25")))
        self.assertIsNone(reduce_cap(side="sell", quote=_quote(bid=Decimal("-0.50")),
                                     cap_bps=Decimal("25")))

    def test_none_on_nonfinite_side(self):
        self.assertIsNone(reduce_cap(side="sell", quote=_quote(bid=Decimal("NaN")),
                                     cap_bps=Decimal("25")))

    def test_bad_side_raises_exec_error(self):
        with self.assertRaises(ExecError):
            reduce_cap(side="flatten", quote=_quote(), cap_bps=Decimal("25"))


class TestFloatInjection(unittest.TestCase):
    """S2 posture: a float in a price/budget slot is a PROGRAMMING error -> ExecError."""

    def test_float_quote_price_raises(self):
        with self.assertRaises(ExecError):
            _cap(side="buy", quote_a=_quote(), quote_b=_quote(ask=99.99))

    def test_float_slippage_cap_raises(self):
        with self.assertRaises(ExecError):
            _cap(side="buy", slippage_cap_bps=25.0)

    def test_float_strategy_limit_raises(self):
        with self.assertRaises(ExecError):
            _cap(side="buy", strategy_limit=100.10)

    def test_float_reduce_quote_raises(self):
        with self.assertRaises(ExecError):
            reduce_cap(side="sell", quote=_quote(bid=1.02), cap_bps=Decimal("25"))

    def test_float_reduce_cap_bps_raises(self):
        with self.assertRaises(ExecError):
            reduce_cap(side="sell", quote=_quote(), cap_bps=25.0)


class TestProgrammingErrors(unittest.TestCase):
    def test_unknown_side_raises_exec_error(self):
        with self.assertRaises(ExecError):
            _cap(side="hold")

    def test_nonfinite_strategy_limit_raises_exec_error(self):
        # Candidate/OrderIntent guarantee finite-or-None; NaN here is malformed
        # collaborator input (ExecError docstring), not a rejectable condition.
        with self.assertRaises(ExecError):
            _cap(side="buy", strategy_limit=Decimal("NaN"))

    def test_cap_result_is_frozen(self):
        res = _cap(side="buy")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            res.marketable = False


if __name__ == "__main__":
    unittest.main()
