"""M2 §B — TradabilityDecider + SessionState/halt/LULD/SSR tests (contract §J
test_market_state.py).

Offline, stdlib-only, no clock/IO/float in the decider. The decider is PURE:
identical inputs -> identical Verdict (mirrors book_state purity). Drives the §H.3
``tradability_transitions.jsonl`` fixture across EVERY session/halt/LULD/SSR/
resumption transition, plus the named per-rule cases:
  - RTH two-sided -> TRADABLE; one-sided / crossed book -> NOT_TRADABLE,
  - vendor halt / unknown halt -> NOT_TRADABLE; RESUMING -> NOT_TRADABLE + AUCTION state,
  - LULD paused/unknown -> NOT_TRADABLE; LIMIT (band present) -> REDUCE_ONLY,
  - LULD band-edge cross-check (NBO at upper edge, luld:normal) -> REDUCE_ONLY,
  - RTH + luld NORMAL/LIMIT + band None -> NOT_TRADABLE 'luld_band_unknown' (5b);
    PRE/POST with no band is NOT forced NOT_TRADABLE by that rule,
  - float band raises at serialize (S2),
  - SSR active/unknown -> short_allowed=False (Reg SHO 201); decider never derives SSR,
  - CLOSED/UNKNOWN phase blocks; PRE/POST -> REDUCE_ONLY,
  - ca_blackout / frozen dominate (S7),
  - merge_severity is tighten-only & order-independent,
  - an out-of-vocab enum -> MarketStateError.
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path

from agent.market_calendar import SessionPhase
from agent.market_state import (
    HaltReason,
    HaltState,
    LuldBand,
    LuldState,
    LuldTier,
    MarketStateError,
    Nbbo,
    SessionState,
    SsrState,
    StatusFlags,
    Tradability,
    TradabilityDecider,
    TradabilityInputs,
    Verdict,
    merge_severity,
)
from agent.serializer import dumps

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "market_state"
    / "tradability_transitions.jsonl"
)

_TS = "2026-06-15T14:00:00.000000Z"
_DATE = "2026-06-15"


def _nbbo(*, bid=None, bid_sz=None, ask=None, ask_sz=None, symbol="AAPL"):
    return Nbbo(
        symbol=symbol,
        best_bid=Decimal(bid) if bid is not None else None,
        best_ask=Decimal(ask) if ask is not None else None,
        bid_sz=Decimal(bid_sz) if bid_sz is not None else None,
        ask_sz=Decimal(ask_sz) if ask_sz is not None else None,
        ts_utc=_TS,
    )


def _band(lower="100.00", upper="300.00", *, ref="200.00", tier=LuldTier.TIER1, doubled=False):
    return LuldBand(
        reference_px=Decimal(ref),
        lower_px=Decimal(lower),
        upper_px=Decimal(upper),
        tier=tier,
        doubled=doubled,
    )


def _inputs(
    *,
    session_phase=SessionPhase.RTH,
    halt=HaltState.NONE,
    halt_reason=HaltReason.NONE,
    luld=LuldState.NORMAL,
    luld_band=None,
    ssr=SsrState.INACTIVE,
    prior_close=None,
    nbbo=None,
    ca_blackout=False,
    frozen=False,
    symbol="AAPL",
    instrument_id=1001,
):
    status = StatusFlags(
        symbol=symbol,
        halt=halt,
        halt_reason=halt_reason,
        luld=luld,
        luld_band=luld_band,
        ssr=ssr,
        prior_close=Decimal(prior_close) if prior_close is not None else None,
        source="alpaca",
    )
    return TradabilityInputs(
        symbol=symbol,
        instrument_id=instrument_id,
        ts_utc=_TS,
        session_date_et=_DATE,
        session_phase=session_phase,
        status=status,
        nbbo=nbbo,
        ca_blackout=ca_blackout,
        frozen=frozen,
    )


class TestNbboTwoSided(unittest.TestCase):
    def test_two_sided_true_for_clean_book(self):
        self.assertTrue(_nbbo(bid="100.00", bid_sz="1", ask="100.10", ask_sz="1").two_sided)

    def test_one_sided_is_false(self):
        self.assertFalse(_nbbo(bid="100.00", bid_sz="1").two_sided)
        self.assertFalse(_nbbo(ask="100.10", ask_sz="1").two_sided)

    def test_crossed_or_locked_is_false(self):
        # crossed: bid > ask
        self.assertFalse(_nbbo(bid="100.20", bid_sz="1", ask="100.10", ask_sz="1").two_sided)
        # locked: bid == ask
        self.assertFalse(_nbbo(bid="100.10", bid_sz="1", ask="100.10", ask_sz="1").two_sided)

    def test_nonpositive_size_is_false(self):
        self.assertFalse(_nbbo(bid="100.00", bid_sz="0", ask="100.10", ask_sz="1").two_sided)
        self.assertFalse(_nbbo(bid="100.00", bid_sz="1", ask="100.10", ask_sz="0").two_sided)

    def test_non_finite_price_or_size_is_false(self):
        # harden DECIDER-2: a non-finite (NaN/Inf) price/size -> two_sided False (fail-closed DATA),
        # NEVER a raised decimal.InvalidOperation from the ordered comparisons.
        self.assertFalse(_nbbo(bid="100.00", bid_sz="NaN", ask="100.10", ask_sz="1").two_sided)
        self.assertFalse(_nbbo(bid="NaN", bid_sz="1", ask="100.10", ask_sz="1").two_sided)
        self.assertFalse(_nbbo(bid="100.00", bid_sz="1", ask="Infinity", ask_sz="1").two_sided)
        self.assertFalse(_nbbo(bid="100.00", bid_sz="1", ask="100.10", ask_sz="-Infinity").two_sided)


class TestDeciderPurity(unittest.TestCase):
    def test_decider_is_pure_deterministic(self):
        decider = TradabilityDecider()
        inp = _inputs(
            luld_band=_band(),
            nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
        )
        v1 = decider.decide(inp)
        v2 = decider.decide(inp)
        self.assertEqual(v1, v2)


class TestTradableAndBook(unittest.TestCase):
    def test_rth_two_sided_is_tradable(self):
        v = TradabilityDecider().decide(
            _inputs(
                luld_band=_band(),
                nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
            )
        )
        self.assertEqual(v.tradability, Tradability.TRADABLE)
        self.assertEqual(v.session_state, SessionState.RTH)
        self.assertTrue(v.two_sided_nbbo)

    def test_no_two_sided_nbbo_blocks(self):
        v = TradabilityDecider().decide(
            _inputs(luld_band=_band(), nbbo=_nbbo(bid="200.00", bid_sz="1"))
        )
        self.assertEqual(v.tradability, Tradability.NOT_TRADABLE)
        self.assertIn("no_two_sided_nbbo", v.reasons)

    def test_crossed_book_blocks(self):
        v = TradabilityDecider().decide(
            _inputs(
                luld_band=_band(),
                nbbo=_nbbo(bid="200.20", bid_sz="1", ask="200.10", ask_sz="1"),
            )
        )
        self.assertEqual(v.tradability, Tradability.NOT_TRADABLE)
        self.assertFalse(v.two_sided_nbbo)

    def test_nbbo_none_blocks(self):
        v = TradabilityDecider().decide(_inputs(luld_band=_band(), nbbo=None))
        self.assertEqual(v.tradability, Tradability.NOT_TRADABLE)


class TestHalt(unittest.TestCase):
    def test_vendor_halt_and_unknown_halt_block(self):
        for halt in (HaltState.HALTED, HaltState.UNKNOWN):
            v = TradabilityDecider().decide(
                _inputs(
                    halt=halt,
                    luld_band=_band(),
                    nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
                )
            )
            self.assertEqual(v.tradability, Tradability.NOT_TRADABLE)
            self.assertEqual(v.session_state, SessionState.HALTED)

    def test_resuming_halt_is_not_tradable(self):
        v = TradabilityDecider().decide(
            _inputs(
                halt=HaltState.RESUMING,
                luld_band=_band(),
                nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
            )
        )
        self.assertEqual(v.tradability, Tradability.NOT_TRADABLE)

    def test_resuming_halt_sets_auction_state(self):
        v = TradabilityDecider().decide(
            _inputs(
                halt=HaltState.RESUMING,
                luld_band=_band(),
                nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
            )
        )
        self.assertEqual(v.session_state, SessionState.AUCTION)
        self.assertEqual(v.tradability, Tradability.NOT_TRADABLE)


class TestLuld(unittest.TestCase):
    def test_luld_paused_or_unknown_blocks(self):
        for luld in (LuldState.PAUSED, LuldState.UNKNOWN):
            v = TradabilityDecider().decide(
                _inputs(
                    luld=luld,
                    luld_band=_band(),
                    nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
                )
            )
            self.assertEqual(v.tradability, Tradability.NOT_TRADABLE)
            self.assertEqual(v.session_state, SessionState.HALTED)

    def test_luld_limit_is_reduce_only(self):
        v = TradabilityDecider().decide(
            _inputs(
                luld=LuldState.LIMIT,
                luld_band=_band(lower="180.00", upper="182.00"),
                nbbo=_nbbo(bid="180.50", bid_sz="1", ask="181.50", ask_sz="1"),
            )
        )
        self.assertEqual(v.tradability, Tradability.REDUCE_ONLY)

    def test_luld_band_edge_forces_reduce_only(self):
        # NBO at the upper band edge with luld:normal -> band-check False -> REDUCE_ONLY
        v = TradabilityDecider().decide(
            _inputs(
                luld=LuldState.NORMAL,
                luld_band=_band(lower="200.50", upper="202.50"),
                nbbo=_nbbo(bid="200.40", bid_sz="1", ask="202.50", ask_sz="1"),
            )
        )
        self.assertEqual(v.tradability, Tradability.REDUCE_ONLY)
        self.assertIn("luld_band_edge", v.reasons)

    def test_normal_luld_with_missing_band_blocks(self):
        for luld in (LuldState.NORMAL, LuldState.LIMIT):
            v = TradabilityDecider().decide(
                _inputs(
                    luld=luld,
                    luld_band=None,
                    nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
                )
            )
            self.assertEqual(v.tradability, Tradability.NOT_TRADABLE)
            self.assertEqual(v.session_state, SessionState.HALTED)
            self.assertIn("luld_band_unknown", v.reasons)

    def test_pre_post_missing_band_not_forced_by_5b(self):
        # PRE/POST with no band is NOT forced NOT_TRADABLE by the 5b rule -> REDUCE_ONLY
        for phase in (SessionPhase.PRE, SessionPhase.POST):
            v = TradabilityDecider().decide(
                _inputs(
                    session_phase=phase,
                    luld=LuldState.NORMAL,
                    luld_band=None,
                    nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
                )
            )
            self.assertEqual(v.tradability, Tradability.REDUCE_ONLY)
            self.assertNotIn("luld_band_unknown", v.reasons)

    def test_luld_band_is_decimal_not_float(self):
        with self.assertRaises(ValueError):
            dumps({"lower_px": 200.5})

    def test_luld_band_check_strictly_inside(self):
        decider = TradabilityDecider()
        band = _band(lower="100.00", upper="200.00")
        self.assertTrue(decider.luld_band_check(price=Decimal("150.00"), band=band))
        self.assertFalse(decider.luld_band_check(price=Decimal("100.00"), band=band))
        self.assertFalse(decider.luld_band_check(price=Decimal("200.00"), band=band))
        self.assertFalse(decider.luld_band_check(price=Decimal("99.99"), band=band))
        self.assertFalse(decider.luld_band_check(price=Decimal("150.00"), band=None))


class TestSsr(unittest.TestCase):
    def test_ssr_active_or_unknown_blocks_short(self):
        for ssr in (SsrState.ACTIVE, SsrState.UNKNOWN):
            v = TradabilityDecider().decide(
                _inputs(
                    ssr=ssr,
                    luld_band=_band(),
                    nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
                )
            )
            self.assertFalse(v.short_allowed)

    def test_ssr_inactive_allows_short_when_tradable(self):
        v = TradabilityDecider().decide(
            _inputs(
                ssr=SsrState.INACTIVE,
                luld_band=_band(),
                nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
            )
        )
        self.assertTrue(v.short_allowed)

    def test_decider_does_not_derive_ssr_from_price(self):
        # A steep decline vs prior_close but ssr INACTIVE -> short still allowed (OP-4):
        # the decider ingests the injected flag and never computes the 10% trigger.
        v = TradabilityDecider().decide(
            _inputs(
                ssr=SsrState.INACTIVE,
                prior_close="200.00",
                luld_band=_band(lower="100.00", upper="300.00"),
                nbbo=_nbbo(bid="150.00", bid_sz="1", ask="150.10", ask_sz="1"),
            )
        )
        self.assertTrue(v.short_allowed)

    def test_short_not_allowed_when_not_tradable(self):
        # tradability != TRADABLE -> short_allowed False even with ssr inactive
        v = TradabilityDecider().decide(
            _inputs(
                session_phase=SessionPhase.PRE,
                ssr=SsrState.INACTIVE,
                luld_band=_band(),
                nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
            )
        )
        self.assertEqual(v.tradability, Tradability.REDUCE_ONLY)
        self.assertFalse(v.short_allowed)


class TestSessionPhase(unittest.TestCase):
    def test_closed_and_unknown_phase_block(self):
        for phase, state in (
            (SessionPhase.CLOSED, SessionState.CLOSED),
            (SessionPhase.UNKNOWN, SessionState.UNKNOWN),
        ):
            v = TradabilityDecider().decide(
                _inputs(
                    session_phase=phase,
                    luld_band=_band(),
                    nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
                )
            )
            self.assertEqual(v.tradability, Tradability.NOT_TRADABLE)
            self.assertEqual(v.session_state, state)

    def test_unknown_phase_reason_is_calendar_unknown(self):
        v = TradabilityDecider().decide(
            _inputs(session_phase=SessionPhase.UNKNOWN, luld_band=_band())
        )
        self.assertIn("calendar_unknown", v.reasons)

    def test_pre_post_is_reduce_only(self):
        for phase in (SessionPhase.PRE, SessionPhase.POST):
            v = TradabilityDecider().decide(
                _inputs(
                    session_phase=phase,
                    luld_band=_band(),
                    nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
                )
            )
            self.assertEqual(v.tradability, Tradability.REDUCE_ONLY)


class TestCorporateActionDominance(unittest.TestCase):
    def test_ca_blackout_dominates(self):
        v = TradabilityDecider().decide(
            _inputs(
                ca_blackout=True,
                luld_band=_band(),
                nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
            )
        )
        self.assertEqual(v.tradability, Tradability.NOT_TRADABLE)
        self.assertIn("ca_blackout", v.reasons)
        self.assertTrue(v.ca_blackout)

    def test_frozen_dominates_everything(self):
        v = TradabilityDecider().decide(
            _inputs(
                frozen=True,
                session_phase=SessionPhase.RTH,
                luld_band=_band(),
                nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
            )
        )
        self.assertEqual(v.tradability, Tradability.NOT_TRADABLE)
        self.assertEqual(v.session_state, SessionState.HALTED)
        self.assertIn("ca_frozen", v.reasons)


class TestMergeSeverity(unittest.TestCase):
    def test_severity_merge_is_tighten_only(self):
        order = [Tradability.TRADABLE, Tradability.REDUCE_ONLY, Tradability.NOT_TRADABLE]
        for a in order:
            for b in order:
                merged = merge_severity(a, b)
                # never looser than either input
                self.assertGreaterEqual(_rank(merged), _rank(a))
                self.assertGreaterEqual(_rank(merged), _rank(b))
                # order-independent
                self.assertEqual(merge_severity(a, b), merge_severity(b, a))

    def test_merge_takes_more_restrictive(self):
        self.assertEqual(
            merge_severity(Tradability.TRADABLE, Tradability.REDUCE_ONLY),
            Tradability.REDUCE_ONLY,
        )
        self.assertEqual(
            merge_severity(Tradability.REDUCE_ONLY, Tradability.NOT_TRADABLE),
            Tradability.NOT_TRADABLE,
        )


def _rank(t):
    return {Tradability.TRADABLE: 0, Tradability.REDUCE_ONLY: 1, Tradability.NOT_TRADABLE: 2}[t]


class TestFailClosedEnum(unittest.TestCase):
    def test_unknown_state_raises_market_state_error(self):
        # An out-of-vocabulary enum reaching decide() -> MarketStateError.
        bad = _inputs(
            luld_band=_band(),
            nbbo=_nbbo(bid="200.00", bid_sz="1", ask="200.10", ask_sz="1"),
        )
        object.__setattr__(bad, "session_phase", "totally_bogus_phase")
        with self.assertRaises(MarketStateError):
            TradabilityDecider().decide(bad)

    def test_auction_phase_input_raises_market_state_error(self):
        # harden DECIDER-1: AUCTION is an internal-only SessionState (produced on HaltState.RESUMING);
        # the calendar's phase_at NEVER emits it as a phase. An AUCTION session_phase INPUT is an
        # upstream invariant break -> MarketStateError (fail-closed), NOT a raw KeyError — and even the
        # frozen/ca_blackout terminal short-circuits must not mask it.
        with self.assertRaises(MarketStateError):
            TradabilityDecider().decide(_inputs(session_phase=SessionPhase.AUCTION))
        with self.assertRaises(MarketStateError):
            TradabilityDecider().decide(
                _inputs(session_phase=SessionPhase.AUCTION, frozen=True, ca_blackout=True)
            )

    def test_non_finite_nbbo_is_not_tradable_not_raise(self):
        # harden DECIDER-2: a non-finite NBBO size -> NOT_TRADABLE (anomaly as DATA), never a raised
        # decimal.InvalidOperation escaping the decider.
        nbbo = _nbbo(bid="200.00", bid_sz="NaN", ask="200.10", ask_sz="1")
        verdict = TradabilityDecider().decide(
            _inputs(luld=LuldState.NORMAL, luld_band=_band(), nbbo=nbbo)
        )
        self.assertEqual(verdict.tradability, Tradability.NOT_TRADABLE)
        self.assertIn("no_two_sided_nbbo", verdict.reasons)

    def test_luld_band_check_non_finite_is_false(self):
        # harden DECIDER-2: a non-finite price or band edge -> luld_band_check False (fail-closed), no raise.
        dec = TradabilityDecider()
        self.assertFalse(dec.luld_band_check(price=Decimal("NaN"), band=_band()))
        self.assertFalse(dec.luld_band_check(price=Decimal("100.00"), band=_band(lower="NaN")))


# --- §H.3 fixture-driven coverage of every transition -------------------------


def _band_from_row(row):
    """Build the LuldBand the decider needs for a fixture row.

    Uses explicit ``luld_band_lower/upper/luld_tier`` when present (row 5). Otherwise,
    when ``luld`` is NORMAL/LIMIT, synthesizes a wide in-range band so the row exercises
    the post-band-presence path — EXCEPT a row whose ``expect_reason`` is
    'luld_band_unknown' (row 6), where the band is deliberately OMITTED to exercise the
    §B step-5b band-presence guard. Non-NORMAL/LIMIT luld states need no band.
    """
    if "luld_band_lower" in row:
        return LuldBand(
            reference_px=Decimal(row.get("prior_close") or "0.0001"),
            lower_px=Decimal(row["luld_band_lower"]),
            upper_px=Decimal(row["luld_band_upper"]),
            tier=LuldTier(row["luld_tier"]),
            doubled=False,
        )
    if row.get("luld") in ("normal", "limit") and row.get("expect_reason") != "luld_band_unknown":
        return _band(lower="0.0100", upper="100000.0000", ref="100.0000")
    return None


def _nbbo_from_row(row):
    if row.get("bid_px") is None and row.get("ask_px") is None:
        return None
    return Nbbo(
        symbol=row["symbol"],
        best_bid=Decimal(row["bid_px"]) if row.get("bid_px") is not None else None,
        best_ask=Decimal(row["ask_px"]) if row.get("ask_px") is not None else None,
        bid_sz=Decimal(row["bid_sz"]) if row.get("bid_sz") is not None else None,
        ask_sz=Decimal(row["ask_sz"]) if row.get("ask_sz") is not None else None,
        ts_utc=_TS,
    )


def _inputs_from_row(row):
    status = StatusFlags(
        symbol=row["symbol"],
        halt=HaltState(row["halt"]),
        halt_reason=HaltReason.NONE,
        luld=LuldState(row["luld"]),
        luld_band=_band_from_row(row),
        ssr=SsrState(row["ssr"]),
        prior_close=Decimal(row["prior_close"]) if row.get("prior_close") is not None else None,
        source="alpaca",
    )
    return TradabilityInputs(
        symbol=row["symbol"],
        instrument_id=row["instrument_id"],
        ts_utc=_TS,
        session_date_et=_DATE,
        session_phase=SessionPhase(row["session_phase"]),
        status=status,
        nbbo=_nbbo_from_row(row),
        ca_blackout=bool(row["ca_blackout"]),
        frozen=False,
    )


class TestEveryTransitionFixture(unittest.TestCase):
    def test_every_transition_fixture(self):
        decider = TradabilityDecider()
        rows = [
            json.loads(line)
            for line in _FIXTURE.read_text().splitlines()
            if line.strip()
        ]
        self.assertEqual(len(rows), 11)  # all §H.3 rows incl. the missing-band row
        for i, row in enumerate(rows, start=1):
            inputs = _inputs_from_row(row)
            verdict = decider.decide(inputs)
            self.assertIsInstance(verdict, Verdict)
            self.assertEqual(
                verdict.tradability.value,
                row["expect_tradability"],
                msg=f"row {i}: {row!r} -> {verdict!r}",
            )
            if "expect_short_allowed" in row:
                self.assertEqual(
                    verdict.short_allowed,
                    row["expect_short_allowed"],
                    msg=f"row {i} short_allowed",
                )
            if "expect_reason" in row:
                self.assertIn(
                    row["expect_reason"],
                    verdict.reasons,
                    msg=f"row {i} reason",
                )


if __name__ == "__main__":
    unittest.main()
