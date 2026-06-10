"""M5 §R test 8 — execution_realism + fees. [S5, S2]

Pins (hand-computed Decimals throughout):
- tob full/partial/unfillable matrix incl. min(qty, ask_sz) and remainder-at-cap
  (FD-M5-19); the EX-2 case VERBATIM: qty=100, ask=10.01, ask_sz=40, cap=10.03,
  broker fills 70 => modeled-over-filled = 40×10.01 + 30×10.03 = 701.30,
  NEVER vwap×70 = 701.54.
- depth: walk on the COMMITTED mbp10_depth_sample.jsonl fixture (row 1, raw
  best-first levels as EquityBookState.snapshot() preserves them — duplicate-px
  vendor splits and zero-size padding included): exact VWAP, worst_price,
  levels_consumed, liquidity-short remainder. The asks side exposes exactly TWO
  displayed levels, so the buy walk integrates 2 levels + the cap-priced
  remainder; the THREE-level walk rides the bids side (sell mirror), where the
  raw ladder yields three consumed levels (300@201.15, 200@201.14, 300@201.14)
  + remainder at the cap.
- stale/epoch-mismatched depth => degrade to tob with reasons (never upgrade,
  never reject); strict '>' at DEPTH_FRESHNESS_TTL_MS.
- identity/schema mismatch => ExecError (schema at CONSTRUCTION — documented
  resolution 4 in execution_realism.py).
- divergence flags side-aware (EX-3: a SELL close with broker proceeds above
  modeled => broker_optimistic) + alert STRICT boundary at exactly
  broker × DIVERGENCE_ALERT_BPS/10000.
- slippage_vs_mid_bps formula + sign both sides (EX-8, adverse positive).
- fees §J: buy zero; sell SEC/TAF ceil; the $8.30 TAF cap boundary EXACTLY
  (50000 × 0.000166 = 8.30 => 8.30; 50001 => ceil 8.31 capped at 8.30).
- float injection raises everywhere; modeled money is ModeledUSD end-to-end.
"""
import dataclasses
import json
import unittest
from decimal import Decimal
from pathlib import Path

from agent.exec_reasons import ExecError
from agent.execution_config import DEPTH_FRESHNESS_TTL_MS, DIVERGENCE_ALERT_BPS
from agent.execution_realism import (
    DepthSnapshot,
    DepthView,
    DivergenceResult,
    ModeledFill,
    UNBOUND_MODELED_FILL_ID,
    assess_divergence,
    bind_modeled_fill_id,
    model_fill,
)
from agent.fees import (
    CENT,
    FEE_MODEL_VERSION,
    FeeAssumption,
    SEC_SECTION31_RATE,
    TAF_CAP_PER_TRADE_USD,
    TAF_PER_SHARE_SOLD,
    fees_for,
)
from agent.quote_quality import QuoteSnapshot, evaluate
from agent.serializer import BrokerUSD, ModeledUSD, row_hash

D = Decimal
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPTH_FIXTURE = (_REPO_ROOT / "tests" / "fixtures" / "databento"
                  / "mbp10_depth_sample.jsonl")


def _quote(**overrides) -> QuoteSnapshot:
    """QuoteSnapshot builder (test_order_pricing.py `_quote` pattern)."""
    base = dict(
        symbol="AAPL",
        instrument_id=1001,
        bid=D("10.00"),
        ask=D("10.01"),
        bid_sz=D("300"),
        ask_sz=D("200"),
        ts_event_utc="2026-06-09T13:30:00.500000Z",
        ts_recv_utc="2026-06-09T13:30:00.550000Z",
        seen_at_ms=10_000,
        reconnect_epoch=0,
        vendor_seq=42,
        dataset="EQUS.MINI",
        schema="tbbo",
    )
    base.update(overrides)
    return QuoteSnapshot(**base)


def _verdict(quote, *, now_ms=None):
    """The ONE quote decider, with a fresh clock unless overridden."""
    return evaluate(quote, now_ms=quote.seen_at_ms if now_ms is None else now_ms,
                    spread_bps_max=D("50"), staleness_ms_max=2000)


def _fill(*, side="buy", qty=D("100"), capped_limit=D("10.03"), quote_b=None,
          quote_b_verdict=None, depth=None, now_ms=None):
    if quote_b is None:
        quote_b = _quote()
    if quote_b_verdict is None:
        quote_b_verdict = _verdict(quote_b)
    if now_ms is None:
        now_ms = quote_b.seen_at_ms
    return model_fill(side=side, qty=qty, capped_limit=capped_limit,
                      quote_b=quote_b, quote_b_verdict=quote_b_verdict,
                      depth=depth, now_ms=now_ms)


def _fixture_row(index=0) -> dict:
    lines = _DEPTH_FIXTURE.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[index])


def _fixture_levels(side_rows):
    """Raw fixture levels as (px, sz) Decimal pairs — NO coalescing/dropping
    (EquityBookState.snapshot() preserves dup-px splits and zero-size padding;
    book_state.py:39-46 sorts best-first only)."""
    return tuple((D(px), D(sz)) for px, sz, _ct in side_rows)


def _fixture_depth(**overrides) -> DepthSnapshot:
    row = _fixture_row(0)
    bids = tuple(sorted(_fixture_levels(row["bids"]),
                        key=lambda lvl: lvl[0], reverse=True))
    asks = tuple(sorted(_fixture_levels(row["asks"]), key=lambda lvl: lvl[0]))
    base = dict(
        symbol=row["symbol"],
        instrument_id=row["instrument_id"],
        bids=bids,
        asks=asks,
        book_hash="bh-fixture-row1",
        seen_at_ms=10_000,
        reconnect_epoch=0,
        dataset=row["dataset"],
        schema=row["schema"],     # "mbp-10" from the committed fixture
    )
    base.update(overrides)
    return DepthSnapshot(**base)


def _depth_quote(**overrides) -> QuoteSnapshot:
    """L1 quote consistent with the fixture book (AAPL/1001, 201.15 × 201.16)."""
    base = dict(
        symbol="AAPL", instrument_id=1001,
        bid=D("201.15"), ask=D("201.16"),
        bid_sz=D("300"), ask_sz=D("200"),
    )
    base.update(overrides)
    return _quote(**base)


class FeeConstantsTest(unittest.TestCase):
    """§J constants verbatim (FD-M5-15: regulatory facts are CODE CONSTANTS)."""

    def test_constants_verbatim(self):
        self.assertEqual(SEC_SECTION31_RATE, D("0.0000278"))
        self.assertEqual(TAF_PER_SHARE_SOLD, D("0.000166"))
        self.assertEqual(TAF_CAP_PER_TRADE_USD, D("8.30"))
        self.assertEqual(FEE_MODEL_VERSION, "reg_fees_v1")
        self.assertEqual(CENT, D("0.01"))

    def test_realism_constants(self):
        # The boundary semantics below depend on these §B one-home values.
        self.assertEqual(DEPTH_FRESHNESS_TTL_MS, 2000)
        self.assertEqual(DIVERGENCE_ALERT_BPS, D("10"))


class FeesBuyTest(unittest.TestCase):

    def test_buy_is_zero_assumption(self):
        fa = fees_for(side="buy", qty=D("100"), notional=D("1000.00"))
        self.assertEqual(fa.model_version, "reg_fees_v1")
        self.assertEqual(fa.sec_usd, D("0"))
        self.assertEqual(fa.taf_usd, D("0"))
        self.assertEqual(fa.total_usd, D("0"))
        # Byte form pinned at CENT scale (documented resolution in fees.py).
        self.assertEqual(str(fa.sec_usd), "0.00")
        self.assertEqual(str(fa.total_usd), "0.00")

    def test_buy_still_validates_inputs(self):
        with self.assertRaises(ValueError):
            fees_for(side="buy", qty=100.0, notional=D("1000"))
        with self.assertRaises(ValueError):
            fees_for(side="buy", qty=D("100"), notional=0.5)


class FeesSellTest(unittest.TestCase):

    def test_sec_and_taf_ceil_round_against_us(self):
        # sec: 1000.00 × 0.0000278 = 0.0278 -> ceil cent 0.03
        # taf: 100 × 0.000166 = 0.0166 -> ceil cent 0.02
        fa = fees_for(side="sell", qty=D("100"), notional=D("1000.00"))
        self.assertEqual(fa.sec_usd, D("0.03"))
        self.assertEqual(fa.taf_usd, D("0.02"))
        self.assertEqual(fa.total_usd, D("0.05"))

    def test_minimal_ceil_to_one_cent(self):
        # sec: 100 × 0.0000278 = 0.00278 -> 0.01; taf: 1 × 0.000166 -> 0.01
        fa = fees_for(side="sell", qty=D("1"), notional=D("100"))
        self.assertEqual(fa.sec_usd, D("0.01"))
        self.assertEqual(fa.taf_usd, D("0.01"))
        self.assertEqual(fa.total_usd, D("0.02"))

    def test_sec_exact_cent_no_bump(self):
        # 5,000,000 × 0.0000278 = 139.00 EXACT -> ceil is a no-op.
        fa = fees_for(side="sell", qty=D("1"), notional=D("5000000"))
        self.assertEqual(fa.sec_usd, D("139.00"))

    def test_taf_cap_boundary_exactly_8_30(self):
        # 50000 × 0.000166 = 8.30 EXACTLY -> taf = 8.30 (equality, not capping).
        fa = fees_for(side="sell", qty=D("50000"), notional=D("1000000"))
        self.assertEqual(fa.taf_usd, D("8.30"))
        # 50001 × 0.000166 = 8.300166 -> ceil 8.31 -> CAPPED at 8.30.
        fa = fees_for(side="sell", qty=D("50001"), notional=D("1000000"))
        self.assertEqual(fa.taf_usd, D("8.30"))
        # Far past the cap stays 8.30.
        fa = fees_for(side="sell", qty=D("1000000"), notional=D("1000000"))
        self.assertEqual(fa.taf_usd, D("8.30"))

    def test_total_is_sum(self):
        fa = fees_for(side="sell", qty=D("50000"), notional=D("1000000"))
        # sec: 1,000,000 × 0.0000278 = 27.80 exact.
        self.assertEqual(fa.sec_usd, D("27.80"))
        self.assertEqual(fa.total_usd, D("36.10"))
        self.assertEqual(fa.total_usd, fa.sec_usd + fa.taf_usd)


class FeesValidationTest(unittest.TestCase):

    def test_bad_side_raises(self):
        with self.assertRaises(ValueError):
            fees_for(side="hold", qty=D("1"), notional=D("1"))

    def test_float_bool_injection_raises(self):
        for qty, notional in (
            (100.0, D("1000")),          # float qty
            (D("100"), 1000.0),          # float notional
            (True, D("1000")),           # bool qty
            (D("100"), False),           # bool notional
            (100, D("1000")),            # non-Decimal int qty
        ):
            with self.assertRaises(ValueError):
                fees_for(side="sell", qty=qty, notional=notional)

    def test_nonpositive_nonfinite_raise(self):
        for qty, notional in (
            (D("0"), D("1000")),
            (D("-1"), D("1000")),
            (D("100"), D("0")),
            (D("NaN"), D("1000")),
            (D("100"), D("Infinity")),
        ):
            with self.assertRaises(ValueError):
                fees_for(side="sell", qty=qty, notional=notional)

    def test_fee_assumption_frozen(self):
        fa = fees_for(side="buy", qty=D("1"), notional=D("1"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            fa.total_usd = D("9.99")


class DepthSnapshotConstructionTest(unittest.TestCase):

    def test_fixture_construction_ok_and_frozen(self):
        depth = _fixture_depth()
        self.assertEqual(depth.schema, "mbp-10")
        self.assertEqual(depth.symbol, "AAPL")
        self.assertEqual(depth.instrument_id, 1001)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            depth.schema = "tbbo"

    def test_wrong_schema_raises_at_construction(self):
        # Documented resolution 4: the schema gate fires at CONSTRUCTION, so a
        # non-mbp-10 DepthSnapshot is unrepresentable.
        with self.assertRaises(ExecError):
            _fixture_depth(schema="tbbo")
        with self.assertRaises(ExecError):
            _fixture_depth(schema="mbp-1")

    def test_float_and_bool_slots_raise(self):
        with self.assertRaises(ExecError):
            _fixture_depth(asks=((201.16, D("200")),))   # float px
        with self.assertRaises(ExecError):
            _fixture_depth(asks=((D("201.16"), 200.0),))  # float sz
        with self.assertRaises(ExecError):
            _fixture_depth(seen_at_ms=True)               # bool int-slot
        with self.assertRaises(ExecError):
            _fixture_depth(asks=((D("201.16"), True),))   # bool sz

    def test_bad_levels_raise(self):
        with self.assertRaises(ExecError):
            _fixture_depth(asks=((D("0"), D("200")),))        # px <= 0
        with self.assertRaises(ExecError):
            _fixture_depth(asks=((D("NaN"), D("200")),))      # non-finite px
        with self.assertRaises(ExecError):
            _fixture_depth(asks=((D("201.16"), D("-1")),))    # negative sz
        with self.assertRaises(ExecError):
            _fixture_depth(asks=tuple((D("201.16") + D(i) / 100, D("1"))
                                      for i in range(11)))    # > 10 levels
        with self.assertRaises(ExecError):                    # asks not best-first
            _fixture_depth(asks=((D("201.17"), D("1")), (D("201.16"), D("1"))))
        with self.assertRaises(ExecError):                    # bids not best-first
            _fixture_depth(bids=((D("201.14"), D("1")), (D("201.15"), D("1"))))


class ModelFillStructuralGateTest(unittest.TestCase):
    """Bare numbers/dicts are unrepresentable; a float in any slot raises (S2)."""

    def test_untyped_collaborators_raise(self):
        q = _quote()
        v = _verdict(q)
        with self.assertRaises(ExecError):
            model_fill(side="buy", qty=D("1"), capped_limit=D("10.03"),
                       quote_b={"ask": D("10.01")}, quote_b_verdict=v,
                       depth=None, now_ms=10_000)
        with self.assertRaises(ExecError):
            model_fill(side="buy", qty=D("1"), capped_limit=D("10.03"),
                       quote_b=q, quote_b_verdict={"ok": True},
                       depth=None, now_ms=10_000)
        with self.assertRaises(ExecError):
            model_fill(side="buy", qty=D("1"), capped_limit=D("10.03"),
                       quote_b=q, quote_b_verdict=v,
                       depth={"asks": ()}, now_ms=10_000)

    def test_float_bool_scalar_slots_raise(self):
        with self.assertRaises(ExecError):
            _fill(qty=100.0)
        with self.assertRaises(ExecError):
            _fill(capped_limit=10.03)
        with self.assertRaises(ExecError):
            _fill(qty=True)
        with self.assertRaises(ExecError):
            _fill(now_ms=True)
        with self.assertRaises(ExecError):
            _fill(now_ms="10000")

    def test_float_in_quote_slot_raises(self):
        clean = _quote()
        hostile = _quote(ask=10.01)  # float smuggled into the snapshot
        with self.assertRaises(ExecError):
            model_fill(side="buy", qty=D("1"), capped_limit=D("10.03"),
                       quote_b=hostile, quote_b_verdict=_verdict(clean),
                       depth=None, now_ms=10_000)

    def test_bad_side_and_nonpositive_raise(self):
        with self.assertRaises(ExecError):
            _fill(side="hold")
        with self.assertRaises(ExecError):
            _fill(qty=D("0"))
        with self.assertRaises(ExecError):
            _fill(capped_limit=D("-1"))


class TobBuyTest(unittest.TestCase):

    def test_full_fill_at_touch(self):
        fill = _fill(qty=D("100"), capped_limit=D("10.03"))
        self.assertEqual(fill.model, "tob_l1_v1")
        self.assertEqual(fill.realism_class, "modeled_full")
        self.assertEqual(fill.requested_qty, D("100"))
        self.assertEqual(fill.modeled_fillable_qty, D("100"))   # min(100, 200)
        self.assertEqual(fill.modeled_cost_usd, D("1001.00"))   # 100 × 10.01 EXACT
        self.assertEqual(str(fill.modeled_vwap), "10.010000")   # MID_QUANTUM
        self.assertEqual(str(fill.worst_price), "10.010000")    # touch (no remainder)
        self.assertEqual(str(fill.touch_price), "10.01")        # UNQUANTIZED
        self.assertIsNone(fill.levels_consumed)                 # tob: never levels
        self.assertEqual(fill.reasons, ())
        self.assertEqual(fill.modeled_fill_id, UNBOUND_MODELED_FILL_ID)

    def test_partial_min_qty_ask_sz_remainder_at_cap(self):
        # EX-2 inputs: qty=100, ask=10.01, ask_sz=40, cap=10.03.
        fill = _fill(quote_b=_quote(ask_sz=D("40")))
        self.assertEqual(fill.realism_class, "modeled_partial")
        self.assertEqual(fill.modeled_fillable_qty, D("40"))
        # 40 × 10.01 + 60 × 10.03 = 400.40 + 601.80 = 1002.20 EXACT.
        self.assertEqual(fill.modeled_cost_usd, D("1002.20"))
        self.assertEqual(str(fill.modeled_vwap), "10.022000")
        self.assertEqual(str(fill.worst_price), "10.030000")    # the cap
        self.assertEqual(str(fill.touch_price), "10.01")

    def test_unfillable_ask_above_cap(self):
        fill = _fill(quote_b=_quote(ask=D("10.04")), capped_limit=D("10.03"))
        self.assertEqual(fill.realism_class, "modeled_unfillable")
        self.assertEqual(fill.reasons, ("no_liquidity_at_cap",))
        self.assertEqual(fill.modeled_fillable_qty, D("0"))
        self.assertIsNone(fill.modeled_vwap)
        self.assertIsNone(fill.worst_price)
        self.assertIsNone(fill.touch_price)
        self.assertIsNone(fill.modeled_cost_usd)
        self.assertIsNone(fill.slippage_vs_mid_bps)
        self.assertIsNone(fill.levels_consumed)

    def test_boundary_equal_touch_is_fillable(self):
        fill = _fill(capped_limit=D("10.01"))   # ask == cap
        self.assertEqual(fill.realism_class, "modeled_full")

    def test_unusable_quote_b_is_unfillable_no_reason(self):
        # Documented resolution 6: a not-ok verdict => modeled_unfillable with
        # EMPTY reasons (quote-quality strings never re-keyed here).
        q = _quote()
        stale = _verdict(q, now_ms=q.seen_at_ms + 2001)   # quote_stale fires
        self.assertFalse(stale.ok)
        fill = _fill(quote_b=q, quote_b_verdict=stale)
        self.assertEqual(fill.realism_class, "modeled_unfillable")
        self.assertEqual(fill.reasons, ())
        self.assertIsNone(fill.modeled_cost_usd)

    def test_modeled_money_is_modeled_usd_end_to_end(self):
        fill = _fill(quote_b=_quote(ask_sz=D("40")))
        for field in ("modeled_vwap", "worst_price", "touch_price",
                      "modeled_cost_usd"):
            value = getattr(fill, field)
            self.assertIsInstance(value, ModeledUSD, field)
            self.assertNotIsInstance(value, BrokerUSD, field)


class TobSellTest(unittest.TestCase):

    def test_partial_mirror_on_bids(self):
        # bid=10.00, bid_sz=40, cap=9.98, qty=100:
        # 40 × 10.00 + 60 × 9.98 = 400.00 + 598.80 = 998.80 proceeds EXACT.
        fill = _fill(side="sell", quote_b=_quote(bid_sz=D("40")),
                     capped_limit=D("9.98"))
        self.assertEqual(fill.realism_class, "modeled_partial")
        self.assertEqual(fill.modeled_fillable_qty, D("40"))
        self.assertEqual(fill.modeled_cost_usd, D("998.80"))
        self.assertEqual(str(fill.modeled_vwap), "9.988000")
        self.assertEqual(str(fill.worst_price), "9.980000")     # the cap
        self.assertEqual(str(fill.touch_price), "10.00")        # bid, unquantized

    def test_unfillable_bid_below_cap(self):
        fill = _fill(side="sell", quote_b=_quote(bid=D("9.97")),
                     capped_limit=D("9.98"))
        self.assertEqual(fill.realism_class, "modeled_unfillable")
        self.assertEqual(fill.reasons, ("no_liquidity_at_cap",))


class SlippageVsMidTest(unittest.TestCase):
    """EX-8: buy (vwap−mid)/mid×10000, sell (mid−vwap)/mid×10000 — adverse
    positive both sides; BPS_QUANTUM; None iff vwap or mid None."""

    def test_buy_sign_and_value(self):
        # mid = 10.005000; vwap = 10.010000:
        # (10.010000 − 10.005000)/10.005000 × 10000 = 4.99750... -> 5.00.
        fill = _fill(qty=D("100"))
        self.assertEqual(fill.slippage_vs_mid_bps, D("5.00"))

    def test_sell_sign_and_value(self):
        # sell at bid: vwap = 10.000000; (10.005 − 10.000)/10.005 × 10000 -> 5.00.
        fill = _fill(side="sell", qty=D("100"), capped_limit=D("9.98"))
        self.assertEqual(fill.slippage_vs_mid_bps, D("5.00"))

    def test_sell_partial_remainder_increases_adverse_slippage(self):
        # vwap 9.988000: (10.005 − 9.988)/10.005 × 10000 = 16.9915... -> 16.99.
        fill = _fill(side="sell", quote_b=_quote(bid_sz=D("40")),
                     capped_limit=D("9.98"))
        self.assertEqual(fill.slippage_vs_mid_bps, D("16.99"))

    def test_none_when_unfillable(self):
        fill = _fill(quote_b=_quote(ask=D("10.04")), capped_limit=D("10.03"))
        self.assertIsNone(fill.slippage_vs_mid_bps)


class QuoteProvenanceTest(unittest.TestCase):

    def test_key_set_and_values(self):
        q = _quote()
        fill = _fill(quote_b=q)
        self.assertEqual(
            set(fill.quote.keys()),
            {"dataset", "schema", "ts_event_utc", "ts_recv_utc",
             "seen_at_ms", "reconnect_epoch", "vendor_seq", "book_hash"})
        self.assertEqual(fill.quote["dataset"], "EQUS.MINI")
        self.assertEqual(fill.quote["schema"], "tbbo")
        self.assertEqual(fill.quote["ts_event_utc"], q.ts_event_utc)
        self.assertEqual(fill.quote["ts_recv_utc"], q.ts_recv_utc)
        self.assertEqual(fill.quote["seen_at_ms"], 10_000)
        self.assertEqual(fill.quote["reconnect_epoch"], 0)
        self.assertEqual(fill.quote["vendor_seq"], 42)
        self.assertIsNone(fill.quote["book_hash"])   # no depth used

    def test_vendor_seq_none_passes_through(self):
        fill = _fill(quote_b=_quote(vendor_seq=None))
        self.assertIsNone(fill.quote["vendor_seq"])

    def test_mapping_is_read_only(self):
        fill = _fill()
        with self.assertRaises(TypeError):
            fill.quote["book_hash"] = "tampered"


class DepthWalkTest(unittest.TestCase):
    """depth_vwap_l2_v2 on the COMMITTED mbp10 fixture (row 1)."""

    def _depth_fill(self, *, side="buy", qty, capped_limit, depth=None):
        q = _depth_quote()
        if depth is None:
            depth = _fixture_depth()
        return model_fill(side=side, qty=qty, capped_limit=capped_limit,
                          quote_b=q, quote_b_verdict=_verdict(q), depth=depth,
                          now_ms=q.seen_at_ms)

    def test_buy_full_walk_two_levels_exact_vwap(self):
        # asks: 200 @ 201.16, 400 @ 201.17; qty 500, cap 201.17:
        # 200×201.16 + 300×201.17 = 40232 + 60351 = 100583.00 EXACT.
        fill = self._depth_fill(qty=D("500"), capped_limit=D("201.17"))
        self.assertEqual(fill.model, "depth_vwap_l2_v2")
        self.assertEqual(fill.realism_class, "modeled_full")
        self.assertEqual(fill.modeled_fillable_qty, D("500"))
        self.assertEqual(fill.modeled_cost_usd, D("100583.00"))
        self.assertEqual(str(fill.modeled_vwap), "201.166000")  # 100583/500
        self.assertEqual(str(fill.worst_price), "201.170000")   # deepest consumed
        self.assertEqual(fill.levels_consumed,
                         ((D("201.16"), D("200")), (D("201.17"), D("300"))))
        self.assertEqual(fill.quote["book_hash"], "bh-fixture-row1")
        self.assertEqual(fill.reasons, ())
        # EX-8 on a depth fill: mid 201.155000 ->
        # (201.166000 − 201.155000)/201.155000 × 10000 = 0.5468... -> 0.55.
        self.assertEqual(fill.slippage_vs_mid_bps, D("0.55"))

    def test_buy_liquidity_short_remainder_at_cap(self):
        # qty 700, cap 201.18: 200×201.16 + 400×201.17 + 100×201.18 (remainder)
        # = 40232 + 80468 + 20118 = 140818.00 EXACT (3 price components).
        fill = self._depth_fill(qty=D("700"), capped_limit=D("201.18"))
        self.assertEqual(fill.realism_class, "modeled_partial")
        self.assertEqual(fill.modeled_fillable_qty, D("600"))
        self.assertEqual(fill.modeled_cost_usd, D("140818.00"))
        # 140818/700 = 201.16857142857... -> 201.168571 (HALF_EVEN at 6dp).
        self.assertEqual(str(fill.modeled_vwap), "201.168571")
        self.assertEqual(str(fill.worst_price), "201.180000")   # the cap
        self.assertEqual(fill.levels_consumed,
                         ((D("201.16"), D("200")), (D("201.17"), D("400"))))

    def test_sell_three_level_walk_skips_zero_size_keeps_dup_px(self):
        # Raw fixture bids best-first: (201.15, 300.0), (201.15, 0),
        # (201.14, 200), (201.14, 300) — the zero-size padding level consumes
        # nothing; the dup-px vendor split stays split. qty 900, cap 201.13:
        # 300×201.15 + 200×201.14 + 300×201.14 + 100×201.13 (remainder)
        # = 60345 + 40228 + 60342 + 20113 = 181028.00 EXACT.
        fill = self._depth_fill(side="sell", qty=D("900"),
                                capped_limit=D("201.13"))
        self.assertEqual(fill.realism_class, "modeled_partial")
        self.assertEqual(fill.modeled_fillable_qty, D("800"))
        self.assertEqual(fill.modeled_cost_usd, D("181028.00"))
        # 181028/900 = 201.142222... -> 201.142222.
        self.assertEqual(str(fill.modeled_vwap), "201.142222")
        self.assertEqual(str(fill.worst_price), "201.130000")   # the cap
        self.assertEqual(fill.levels_consumed,
                         ((D("201.15"), D("300")),
                          (D("201.14"), D("200")),
                          (D("201.14"), D("300"))))

    def test_depth_unfillable_no_level_inside_cap(self):
        fill = self._depth_fill(qty=D("100"), capped_limit=D("201.15"))
        self.assertEqual(fill.model, "depth_vwap_l2_v2")
        self.assertEqual(fill.realism_class, "modeled_unfillable")
        self.assertEqual(fill.reasons, ("no_liquidity_at_cap",))
        self.assertIsNone(fill.levels_consumed)
        self.assertIsNone(fill.modeled_cost_usd)

    def test_depth_modeled_money_is_modeled_usd(self):
        fill = self._depth_fill(qty=D("500"), capped_limit=D("201.17"))
        for field in ("modeled_vwap", "worst_price", "touch_price",
                      "modeled_cost_usd"):
            self.assertIsInstance(getattr(fill, field), ModeledUSD, field)


class DepthDegradeTest(unittest.TestCase):
    """Stale/epoch-mismatched depth degrades to tob with the reason recorded —
    never a reject by itself, never an upgrade."""

    def _degrade_fill(self, depth, *, now_ms=None):
        q = _depth_quote()
        return model_fill(side="buy", qty=D("100"), capped_limit=D("201.18"),
                          quote_b=q, quote_b_verdict=_verdict(q), depth=depth,
                          now_ms=q.seen_at_ms if now_ms is None else now_ms)

    def test_stale_depth_degrades_strict_boundary(self):
        depth = _fixture_depth(seen_at_ms=10_000)
        # age == TTL exactly: depth USED (strict '>').
        fill = self._degrade_fill(depth, now_ms=10_000 + DEPTH_FRESHNESS_TTL_MS)
        self.assertEqual(fill.model, "depth_vwap_l2_v2")
        self.assertEqual(fill.reasons, ())
        # age == TTL + 1: stale -> tob, reason recorded, NOT a reject.
        fill = self._degrade_fill(depth,
                                  now_ms=10_000 + DEPTH_FRESHNESS_TTL_MS + 1)
        self.assertEqual(fill.model, "tob_l1_v1")
        self.assertEqual(fill.reasons, ("depth_stale",))
        self.assertEqual(fill.realism_class, "modeled_full")  # tob math: 100 <= 200
        self.assertIsNone(fill.levels_consumed)
        self.assertIsNone(fill.quote["book_hash"])            # depth NOT used

    def test_epoch_mismatch_degrades(self):
        fill = self._degrade_fill(_fixture_depth(reconnect_epoch=1))
        self.assertEqual(fill.model, "tob_l1_v1")
        self.assertEqual(fill.reasons, ("depth_epoch_mismatch",))

    def test_stale_and_epoch_mismatch_collects_both_sorted(self):
        depth = _fixture_depth(reconnect_epoch=1, seen_at_ms=1_000)
        fill = self._degrade_fill(depth, now_ms=10_000)
        self.assertEqual(fill.model, "tob_l1_v1")
        self.assertEqual(fill.reasons, ("depth_epoch_mismatch", "depth_stale"))

    def test_absent_depth_is_tob_with_no_reasons(self):
        fill = self._degrade_fill(None)
        self.assertEqual(fill.model, "tob_l1_v1")
        self.assertEqual(fill.reasons, ())


class DepthIdentityTest(unittest.TestCase):

    def test_symbol_mismatch_raises(self):
        q = _depth_quote(symbol="MSFT", instrument_id=2002)
        with self.assertRaises(ExecError):
            model_fill(side="buy", qty=D("1"), capped_limit=D("201.18"),
                       quote_b=q, quote_b_verdict=_verdict(q),
                       depth=_fixture_depth(), now_ms=q.seen_at_ms)

    def test_instrument_id_mismatch_raises(self):
        q = _depth_quote(instrument_id=9999)
        with self.assertRaises(ExecError):
            model_fill(side="buy", qty=D("1"), capped_limit=D("201.18"),
                       quote_b=q, quote_b_verdict=_verdict(q),
                       depth=_fixture_depth(), now_ms=q.seen_at_ms)


class ModeledFillIdBindingTest(unittest.TestCase):
    """§P.3 (documented resolution 1): model_fill emits the UNBOUND sentinel;
    the CALLER binds `"mf-" + row_hash({order_id, model, quote_b_seen_at_ms,
    vendor_seq|null})`."""

    def test_model_fill_emits_unbound_sentinel(self):
        self.assertEqual(_fill().modeled_fill_id, "")
        self.assertEqual(UNBOUND_MODELED_FILL_ID, "")

    def test_bind_computes_the_exact_p3_hash(self):
        fill = _fill()
        bound = bind_modeled_fill_id(fill, order_id="o-abc123")
        expected = "mf-" + row_hash({
            "order_id": "o-abc123",
            "model": "tob_l1_v1",
            "quote_b_seen_at_ms": 10_000,
            "vendor_seq": 42,
        })
        self.assertEqual(bound.modeled_fill_id, expected)
        # Deterministic; original unchanged (frozen, replace-based).
        self.assertEqual(
            bind_modeled_fill_id(fill, order_id="o-abc123").modeled_fill_id,
            expected)
        self.assertEqual(fill.modeled_fill_id, "")
        # Different order_id -> different id.
        self.assertNotEqual(
            bind_modeled_fill_id(fill, order_id="o-other").modeled_fill_id,
            expected)

    def test_vendor_seq_null_in_id_operands(self):
        fill = _fill(quote_b=_quote(vendor_seq=None))
        bound = bind_modeled_fill_id(fill, order_id="o-abc123")
        expected = "mf-" + row_hash({
            "order_id": "o-abc123",
            "model": "tob_l1_v1",
            "quote_b_seen_at_ms": 10_000,
            "vendor_seq": None,
        })
        self.assertEqual(bound.modeled_fill_id, expected)

    def test_rebind_and_bad_order_id_raise(self):
        bound = bind_modeled_fill_id(_fill(), order_id="o-abc123")
        with self.assertRaises(ExecError):
            bind_modeled_fill_id(bound, order_id="o-abc123")  # single-use
        with self.assertRaises(ExecError):
            bind_modeled_fill_id(_fill(), order_id="")
        with self.assertRaises(ExecError):
            bind_modeled_fill_id("not-a-fill", order_id="o-abc123")


class ModeledFillValidationTest(unittest.TestCase):
    """__post_init__ is fail-closed: vocab, sortedness, lineage, consistency."""

    def test_out_of_vocab_raises(self):
        fill = _fill()
        with self.assertRaises(ExecError):
            dataclasses.replace(fill, model="bogus_model")
        with self.assertRaises(ExecError):
            dataclasses.replace(fill, realism_class="bogus_class")
        with self.assertRaises(ExecError):
            dataclasses.replace(fill, reasons=("bogus_reason",))

    def test_unsorted_or_duplicated_reasons_raise(self):
        fill = _fill()
        with self.assertRaises(ExecError):
            dataclasses.replace(
                fill, reasons=("no_liquidity_at_cap", "depth_stale"))  # unsorted
        with self.assertRaises(ExecError):
            dataclasses.replace(fill, reasons=("depth_stale", "depth_stale"))

    def test_tob_with_levels_raises(self):
        with self.assertRaises(ExecError):
            dataclasses.replace(_fill(), levels_consumed=((D("1"), D("1")),))

    def test_money_lineage_wall(self):
        fill = _fill()
        with self.assertRaises(ExecError):
            dataclasses.replace(fill, modeled_vwap=D("10.01"))        # plain Decimal
        with self.assertRaises(ExecError):
            dataclasses.replace(fill, modeled_cost_usd=BrokerUSD("1001.00"))
        with self.assertRaises(ExecError):
            dataclasses.replace(fill, touch_price=D("10.01"))

    def test_unfillable_consistency_enforced(self):
        unfillable = _fill(quote_b=_quote(ask=D("10.04")),
                           capped_limit=D("10.03"))
        with self.assertRaises(ExecError):
            dataclasses.replace(unfillable,
                                modeled_cost_usd=ModeledUSD("1.00"))
        with self.assertRaises(ExecError):
            dataclasses.replace(unfillable, modeled_fillable_qty=D("1"))

    def test_quote_key_set_enforced(self):
        with self.assertRaises(ExecError):
            dataclasses.replace(_fill(), quote={"dataset": "EQUS.MINI"})


class AssessDivergenceTobTest(unittest.TestCase):

    def _ex2_fill(self):
        """EX-2 modeled fill: qty=100, ask=10.01, ask_sz=40, cap=10.03."""
        return _fill(quote_b=_quote(ask_sz=D("40")))

    def test_ex2_exact_reintegration_verbatim(self):
        # Broker fills 70: modeled-over-filled = 40×10.01 + 30×10.03 = 701.30.
        # NEVER vwap×70 = 10.022000 × 70 = 701.54.
        fill = self._ex2_fill()
        res = assess_divergence(side="buy", broker_cost_usd=BrokerUSD("701.30"),
                                filled_qty=D("70"), modeled=fill)
        self.assertEqual(res.divergence_usd, D("0"))
        self.assertEqual(res.flag, "aligned")
        self.assertFalse(res.alert)
        # The forbidden vwap×qty form would call 701.54 "aligned"; the frozen
        # re-integration says it diverges by exactly 0.24.
        res = assess_divergence(side="buy", broker_cost_usd=BrokerUSD("701.54"),
                                filled_qty=D("70"), modeled=fill)
        self.assertEqual(res.divergence_usd, D("0.24"))
        self.assertNotEqual(res.flag, "aligned")

    def test_buy_flag_mapping_side_aware(self):
        fill = self._ex2_fill()
        # Buy favorable = broker paid LESS than modeled => divergence < 0.
        res = assess_divergence(side="buy", broker_cost_usd=BrokerUSD("701.20"),
                                filled_qty=D("70"), modeled=fill)
        self.assertEqual(res.divergence_usd, D("-0.10"))
        self.assertEqual(res.flag, "broker_optimistic")
        res = assess_divergence(side="buy", broker_cost_usd=BrokerUSD("701.40"),
                                filled_qty=D("70"), modeled=fill)
        self.assertEqual(res.divergence_usd, D("0.10"))
        self.assertEqual(res.flag, "broker_conservative")
        # bps: 0.10 / 701.40 × 10000 = 1.4257... -> 1.43 (BPS_QUANTUM).
        self.assertEqual(res.divergence_bps, D("1.43"))

    def test_sell_flag_mapping_side_aware_ex3(self):
        # SELL close: broker_cost_usd is sale PROCEEDS. Modeled proceeds:
        # full fill 50 × bid 10.00 = 500.00.
        fill = _fill(side="sell", qty=D("50"), capped_limit=D("9.98"))
        # Broker proceeds ABOVE modeled => paper filled flatteringly =>
        # broker_optimistic (the parent PR-8 flag).
        res = assess_divergence(side="sell", broker_cost_usd=BrokerUSD("500.10"),
                                filled_qty=D("50"), modeled=fill)
        self.assertEqual(res.divergence_usd, D("0.10"))
        self.assertEqual(res.flag, "broker_optimistic")
        # Below modeled => broker_conservative.
        res = assess_divergence(side="sell", broker_cost_usd=BrokerUSD("499.90"),
                                filled_qty=D("50"), modeled=fill)
        self.assertEqual(res.divergence_usd, D("-0.10"))
        self.assertEqual(res.flag, "broker_conservative")
        res = assess_divergence(side="sell", broker_cost_usd=BrokerUSD("500.00"),
                                filled_qty=D("50"), modeled=fill)
        self.assertEqual(res.flag, "aligned")

    def test_alert_strict_boundary(self):
        # Full fill 100 @ ask 9.99 => re-integrated cost 999.00.
        # threshold = broker × DIVERGENCE_ALERT_BPS/10000 = 1000.00 × 0.001 = 1.00.
        fill = _fill(quote_b=_quote(bid=D("9.98"), ask=D("9.99")),
                     capped_limit=D("10.01"))
        res = assess_divergence(side="buy", broker_cost_usd=BrokerUSD("1000.00"),
                                filled_qty=D("100"), modeled=fill)
        self.assertEqual(res.divergence_usd, D("1.00"))
        self.assertFalse(res.alert)               # |1.00| > 1.00 is False: STRICT
        # Cost 998.99 (ask 9.9899) => divergence 1.01 > 1.00 => alert.
        fill = _fill(quote_b=_quote(bid=D("9.98"), ask=D("9.9899")),
                     capped_limit=D("10.01"))
        res = assess_divergence(side="buy", broker_cost_usd=BrokerUSD("1000.00"),
                                filled_qty=D("100"), modeled=fill)
        self.assertEqual(res.divergence_usd, D("1.01"))
        self.assertTrue(res.alert)

    def test_unassessed_when_modeled_side_null(self):
        unfillable = _fill(quote_b=_quote(ask=D("10.04")),
                           capped_limit=D("10.03"))
        res = assess_divergence(side="buy", broker_cost_usd=BrokerUSD("701.30"),
                                filled_qty=D("70"), modeled=unfillable)
        self.assertIsNone(res.divergence_usd)
        self.assertIsNone(res.divergence_bps)
        self.assertEqual(res.flag, "unassessed")
        self.assertFalse(res.alert)

    def test_outputs_are_plain_decimal_the_sanctioned_seam(self):
        res = assess_divergence(side="buy", broker_cost_usd=BrokerUSD("701.40"),
                                filled_qty=D("70"), modeled=self._ex2_fill())
        self.assertNotIsInstance(res.divergence_usd, BrokerUSD)
        self.assertNotIsInstance(res.divergence_usd, ModeledUSD)
        self.assertNotIsInstance(res.divergence_bps, BrokerUSD)
        self.assertNotIsInstance(res.divergence_bps, ModeledUSD)
        self.assertIsInstance(res.divergence_usd, Decimal)


class AssessDivergenceDepthTest(unittest.TestCase):

    def _depth_fill_700(self):
        """Fixture case: qty 700, cap 201.18 — levels ((201.16,200),(201.17,400)),
        worst = the cap 201.18, fillable 600."""
        q = _depth_quote()
        return model_fill(side="buy", qty=D("700"), capped_limit=D("201.18"),
                          quote_b=q, quote_b_verdict=_verdict(q),
                          depth=_fixture_depth(), now_ms=q.seen_at_ms)

    def test_walk_prefix_reintegration_with_remainder(self):
        # First 650 shares: 200×201.16 + 400×201.17 + 50×201.18 = 130759.00.
        fill = self._depth_fill_700()
        res = assess_divergence(side="buy",
                                broker_cost_usd=BrokerUSD("130759.00"),
                                filled_qty=D("650"), modeled=fill)
        self.assertEqual(res.divergence_usd, D("0"))
        self.assertEqual(res.flag, "aligned")
        # FORBIDDEN form: vwap×650 = 201.168571 × 650 = 130759.57115 — must NOT
        # be the basis (broker at ~that value is NOT aligned).
        res = assess_divergence(side="buy",
                                broker_cost_usd=BrokerUSD("130759.57"),
                                filled_qty=D("650"), modeled=fill)
        self.assertEqual(res.divergence_usd, D("0.57"))
        self.assertNotEqual(res.flag, "aligned")

    def test_walk_prefix_within_first_level(self):
        # First 150 shares: 150 × 201.16 = 30174.00.
        res = assess_divergence(side="buy",
                                broker_cost_usd=BrokerUSD("30174.00"),
                                filled_qty=D("150"),
                                modeled=self._depth_fill_700())
        self.assertEqual(res.divergence_usd, D("0"))
        self.assertEqual(res.flag, "aligned")


class AssessDivergenceValidationTest(unittest.TestCase):

    def test_broker_cost_lineage_wall(self):
        fill = _fill()
        with self.assertRaises(TypeError):
            assess_divergence(side="buy", broker_cost_usd=D("701.30"),
                              filled_qty=D("70"), modeled=fill)
        with self.assertRaises(TypeError):
            assess_divergence(side="buy", broker_cost_usd=ModeledUSD("701.30"),
                              filled_qty=D("70"), modeled=fill)
        with self.assertRaises(TypeError):
            assess_divergence(side="buy", broker_cost_usd=701.30,
                              filled_qty=D("70"), modeled=fill)

    def test_filled_qty_validation(self):
        fill = _fill()
        with self.assertRaises(ExecError):
            assess_divergence(side="buy", broker_cost_usd=BrokerUSD("701.30"),
                              filled_qty=70.0, modeled=fill)
        with self.assertRaises(ExecError):
            assess_divergence(side="buy", broker_cost_usd=BrokerUSD("701.30"),
                              filled_qty=D("0"), modeled=fill)
        # filled > requested: a broker cannot fill more than requested —
        # malformed collaborator input (documented resolution 2 guard).
        with self.assertRaises(ExecError):
            assess_divergence(side="buy", broker_cost_usd=BrokerUSD("701.30"),
                              filled_qty=D("101"), modeled=fill)

    def test_nonpositive_broker_cost_and_bad_modeled_raise(self):
        with self.assertRaises(ExecError):
            assess_divergence(side="buy", broker_cost_usd=BrokerUSD("0"),
                              filled_qty=D("1"), modeled=_fill())
        with self.assertRaises(ExecError):
            assess_divergence(side="buy", broker_cost_usd=BrokerUSD("1.00"),
                              filled_qty=D("1"), modeled={"model": "tob_l1_v1"})
        with self.assertRaises(ExecError):
            assess_divergence(side="hold", broker_cost_usd=BrokerUSD("1.00"),
                              filled_qty=D("1"), modeled=_fill())

    def test_divergence_result_flag_vocab_closed(self):
        with self.assertRaises(ExecError):
            DivergenceResult(divergence_usd=None, divergence_bps=None,
                             flag="bogus", alert=False)


class DepthViewProtocolTest(unittest.TestCase):

    def test_runtime_checkable_duck_type(self):
        class _Fake:
            def latest_book(self, symbol, instrument_id):
                return None

        self.assertIsInstance(_Fake(), DepthView)
        self.assertNotIsInstance(object(), DepthView)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
