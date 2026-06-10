"""M4 §M test 3 — pure exposure math: notional bounds, poisoned aggregates, cap
projection boundaries.

Invariants: S2 (Decimal exactness), R3 (broker market_value is the held basis).
"""
import unittest
from decimal import Decimal

from agent.candidate import Candidate, Leg
from agent.risk.account_state import MARK_FRESHNESS_TTL_MS
from agent.risk.exposure import (
    UnknownMeta,
    beta_notional,
    gross_exposure,
    leg_cap_notional,
    net_exposure,
    project_caps,
    sector_exposure,
    symbol_exposure,
)
from agent.risk.reasons import RiskError
from agent.risk.risk_config import RiskConfig
from tests.lib.risk_fixtures import (
    gates_on_fixture_config,
    marks_fixture,
    permissive_fixture_config,
    portfolio_fixture,
)


def _leg(symbol="AAPL", side="buy", qty="10", limit="190.00", instrument_id=1001):
    return Leg(symbol=symbol, instrument_id=instrument_id, side=side,
               qty=Decimal(qty), limit_price=Decimal(limit) if limit is not None else None)


def _candidate(*legs):
    return Candidate(strategy_id="s1", legs=tuple(legs), paper_eligible=True, score=None)


class TestLegCapNotional(unittest.TestCase):
    def setUp(self):
        self.fx = marks_fixture()
        self.now = self.fx["now_ms"]

    def test_fresh_mark_uses_max_of_limit_and_mid(self):
        # limit 190.00 < mid 191.000000 -> mark tightens the bound (FD-M4-16).
        notional, mark_used = leg_cap_notional(self.fx["leg"], self.fx["fresh"],
                                               now_ms=self.now)
        self.assertEqual(notional, Decimal("10") * Decimal("191.000000"))
        self.assertIs(mark_used, True)

    def test_fresh_mark_below_limit_still_mark_used(self):
        leg = _leg(limit="195.00")
        notional, mark_used = leg_cap_notional(leg, self.fx["fresh"], now_ms=self.now)
        self.assertEqual(notional, Decimal("10") * Decimal("195.00"))  # max() keeps limit
        self.assertIs(mark_used, True)

    def test_stale_or_absent_mark_falls_back_to_limit(self):
        notional, mark_used = leg_cap_notional(self.fx["leg"], self.fx["stale"],
                                               now_ms=self.now)
        self.assertEqual(str(notional), "1900.00")  # exact canonical Decimal string
        self.assertIs(mark_used, False)
        notional, mark_used = leg_cap_notional(self.fx["leg"], None, now_ms=self.now)
        self.assertEqual(notional, Decimal("10") * Decimal("190.00"))
        self.assertIs(mark_used, False)

    def test_mark_fresh_at_exactly_ttl_stale_at_plus_one(self):
        # marks_fixture: "fresh" is seen exactly MARK_FRESHNESS_TTL_MS ago (boundary).
        notional, mark_used = leg_cap_notional(self.fx["leg"], self.fx["fresh"],
                                               now_ms=self.now)
        self.assertIs(mark_used, True)
        notional, mark_used = leg_cap_notional(self.fx["leg"], self.fx["fresh"],
                                               now_ms=self.now + 1)
        self.assertIs(mark_used, False)

    def test_unpriceable_none_zero_negative_limit(self):
        for leg in (self.fx["leg_no_limit"], self.fx["leg_zero_limit"],
                    self.fx["leg_negative_limit"]):
            notional, mark_used = leg_cap_notional(leg, self.fx["fresh"], now_ms=self.now)
            self.assertIsNone(notional)   # RM-3: unpriceable, never a negative notional
            self.assertIs(mark_used, False)

    def test_ttl_clamp_raises_on_longer_ttl(self):
        with self.assertRaises(ValueError):
            leg_cap_notional(self.fx["leg"], self.fx["fresh"], now_ms=self.now,
                             ttl_ms=MARK_FRESHNESS_TTL_MS + 1)
        leg_cap_notional(self.fx["leg"], self.fx["fresh"], now_ms=self.now,
                         ttl_ms=MARK_FRESHNESS_TTL_MS - 1)  # shorten OK

    def test_identity_mismatched_mark_raises(self):
        with self.assertRaises(RiskError):
            leg_cap_notional(self.fx["leg"], self.fx["mismatched_symbol"], now_ms=self.now)
        with self.assertRaises(RiskError):
            leg_cap_notional(self.fx["leg"], self.fx["mismatched_instrument"],
                             now_ms=self.now)


class TestPortfolioSums(unittest.TestCase):
    def setUp(self):
        self.pf = portfolio_fixture("long_short")  # AAPL +1900.00, MSFT -2100.00
        self.universe = RiskConfig.from_config(permissive_fixture_config()).universe

    def test_gross_net_symbol_exact(self):
        self.assertEqual(gross_exposure(self.pf), Decimal("4000.00"))
        self.assertEqual(net_exposure(self.pf), Decimal("-200.00"))
        self.assertEqual(symbol_exposure(self.pf, "AAPL"), Decimal("1900.00"))
        self.assertEqual(symbol_exposure(self.pf, "MSFT"), Decimal("2100.00"))
        self.assertEqual(symbol_exposure(self.pf, "NVDA"), Decimal("0"))

    def test_sector_exposure_held_short_adds_absolute(self):
        # RM-4: the held MSFT short ADDS |market_value| to "tech".
        sectors = sector_exposure(self.pf, self.universe)
        self.assertEqual(sectors, {"tech": Decimal("4000.00")})

    def test_beta_notional_signed(self):
        # 1900*1.2 + (-2100)*1.1 = 2280 - 2310 = -30
        self.assertEqual(beta_notional(self.pf, self.universe), Decimal("-30.000"))

    def test_poisoned_aggregate_never_partial_sum(self):
        universe = {"AAPL": self.universe["AAPL"]}  # MSFT held but unmapped
        sectors = sector_exposure(self.pf, universe)
        self.assertIsInstance(sectors, UnknownMeta)
        self.assertEqual(sectors.kind, "sector")
        self.assertEqual(sectors.symbols, ("MSFT",))
        beta = beta_notional(self.pf, universe)
        self.assertIsInstance(beta, UnknownMeta)
        self.assertEqual(beta.kind, "beta")
        self.assertEqual(beta.symbols, ("MSFT",))


class TestProjectCaps(unittest.TestCase):
    def setUp(self):
        self.cfg = RiskConfig.from_config(permissive_fixture_config())

    def test_boundary_equal_passes_cap_plus_cent_rejects(self):
        # flat portfolio; single AAPL buy projected exactly AT max_position_usd=10000.
        pf = portfolio_fixture("flat")
        leg = _leg(qty="1", limit="10000")
        cand = _candidate(leg)
        reasons, rows = project_caps(cand, {0: Decimal("10000")}, pf, self.cfg)
        self.assertNotIn("position_cap_exceeded", reasons)
        reasons, rows = project_caps(cand, {0: Decimal("10000.01")}, pf, self.cfg)
        self.assertIn("position_cap_exceeded", reasons)

    def test_zero_cap_rejects_any_positive_exposure(self):
        cfg = RiskConfig.from_config(gates_on_fixture_config())
        pf = portfolio_fixture("flat")
        cand = _candidate(_leg(qty="1", limit="0.01"))
        reasons, rows = project_caps(cand, {0: Decimal("0.01")}, pf, cfg)
        self.assertIn("position_cap_exceeded", reasons)
        self.assertIn("gross_exposure_cap_exceeded", reasons)
        self.assertIn("net_exposure_cap_exceeded", reasons)
        # empty universe poisons sector+beta for the candidate symbol (RM-5):
        self.assertIn("sector_unknown", reasons)
        self.assertIn("beta_unknown", reasons)
        self.assertNotIn("sector_cap_exceeded", reasons)
        self.assertNotIn("beta_cap_exceeded", reasons)

    def test_additive_conservative_signs(self):
        # A SELL (short-establish) leg: |notional| adds to gross/position/sector,
        # signed (−) to net/beta (FD-M4-17).
        pf = portfolio_fixture("long_short")  # gross 4000, net -200, beta -30
        leg = _leg(symbol="XOM", side="sell", qty="10", limit="100", instrument_id=77)
        cand = _candidate(leg)
        reasons, rows = project_caps(cand, {0: Decimal("1000")}, pf, self.cfg)
        by_name = {row[0]: row for row in rows}
        self.assertEqual(by_name["max_gross_exposure_usd"][1], "5000.00")   # 4000 + |−1000|
        self.assertEqual(by_name["max_net_exposure_usd"][1], "1200.00")     # |-200 - 1000|
        self.assertEqual(by_name["max_position_usd:XOM"][1], "1000")
        self.assertEqual(by_name["max_sector_exposure_usd:energy"][1], "1000")
        # beta: -30 + (-1000 * 0.9) = -930 -> |.|
        self.assertEqual(by_name["max_abs_beta_notional_usd"][1], "930.000")
        self.assertEqual(reasons, ())

    def test_candidate_symbol_outside_universe_poisons_sector_and_beta(self):
        pf = portfolio_fixture("flat")
        leg = _leg(symbol="NVDA", instrument_id=55)
        cand = _candidate(leg)
        reasons, rows = project_caps(cand, {0: Decimal("1900.00")}, pf, self.cfg)
        self.assertIn("sector_unknown", reasons)
        self.assertIn("beta_unknown", reasons)
        names = [row[0] for row in rows]
        self.assertNotIn("max_abs_beta_notional_usd", names)
        self.assertFalse(any(n.startswith("max_sector_exposure_usd") for n in names))

    def test_caps_used_rows_sorted_by_name_with_exact_strings(self):
        pf = portfolio_fixture("long_short")
        cand = _candidate(_leg(qty="2", limit="190.00"))
        reasons, rows = project_caps(cand, {0: Decimal("380.00")}, pf, self.cfg)
        names = [row[0] for row in rows]
        self.assertEqual(names, sorted(names))
        by_name = {row[0]: row for row in rows}
        self.assertEqual(by_name["max_position_usd:AAPL"],
                         ("max_position_usd:AAPL", "2280.00", "10000"))
        self.assertEqual(by_name["max_gross_exposure_usd"],
                         ("max_gross_exposure_usd", "4380.00", "50000"))
        # net: -200 + 380 = 180
        self.assertEqual(by_name["max_net_exposure_usd"],
                         ("max_net_exposure_usd", "180.00", "50000"))
        # sector tech: 4000 + 380 = 4380
        self.assertEqual(by_name["max_sector_exposure_usd:tech"],
                         ("max_sector_exposure_usd:tech", "4380.00", "20000"))
        # beta: -30 + 380*1.2 = 426
        self.assertEqual(by_name["max_abs_beta_notional_usd"],
                         ("max_abs_beta_notional_usd", "426.000", "30000"))

    def test_held_symbol_outside_universe_poisons_even_with_mapped_candidate(self):
        pf = portfolio_fixture("long_short")
        cfg_universe_no_msft = permissive_fixture_config()
        del cfg_universe_no_msft["risk_rules"]["risk"]["universe"]["MSFT"]
        cfg = RiskConfig.from_config(cfg_universe_no_msft)
        cand = _candidate(_leg())
        reasons, rows = project_caps(cand, {0: Decimal("190.00")}, pf, cfg)
        self.assertIn("sector_unknown", reasons)
        self.assertIn("beta_unknown", reasons)


if __name__ == "__main__":
    unittest.main()
