"""M7c Phase-1 RED tests — long-only cross-sectional relative-strength proxy.

These are written BEFORE implementation and are expected to FAIL until
`agent.strategies.relative_strength` exists and
`agent.backtest_metrics.UNIVERSE_EQUAL_WEIGHT_LONG_BENCHMARK` is added. They pin
the phase-1 contract (docs/superpowers/specs/2026-06-13-M7c-phase1-proxy-contract.md):
cross-sectional ranking over the valid decision set at one timestamp, top-2
long-only selection, validity/exclusion semantics, the >=8-valid floor, and the
new equal-weight-long benchmark identifier.
"""
import ast
import importlib
import unittest
from decimal import Decimal
from pathlib import Path

from agent.feature_engine import FeatureSnapshot
from agent.market_state import (
    HaltState,
    LuldState,
    SessionState,
    SsrState,
    Tradability,
    Verdict,
)
from agent.quote_quality import QuoteSnapshot, QuoteVerdict
from agent.signal_snapshot import SignalSnapshot

# Predeclared M7c universe order (packet "Predeclared Universe").
UNIVERSE = ("AAPL", "MSFT", "NVDA", "AMZN", "META",
            "GOOGL", "TSLA", "AVGO", "COST", "NFLX")
INSTRUMENT_IDS = {sym: 1001 + i for i, sym in enumerate(UNIVERSE)}
DECISION_TS = "2026-06-15T14:31:00.000000Z"


def _rs():
    """Import the not-yet-existing proxy module (RED until implemented)."""
    return importlib.import_module("agent.strategies.relative_strength")


def _fmt(value: Decimal) -> str:
    return f"{value:.8f}"


def _feature(symbol, *, strength, available=True):
    """Feature snapshot whose scored features are co-monotonic in `strength`.

    Higher strength => higher momentum/ema/sma/rsi and LOWER realized vol, so a
    higher strength is unambiguously a higher rs_score regardless of the exact
    rank normalization the implementation picks.
    """
    unit = Decimal("0.001")
    feats = {
        "momentum_9": _fmt(strength * unit),
        "momentum_21": _fmt(strength * unit),
        "ema_gap_9_21": _fmt(strength * unit),
        "sma_gap_21_50": _fmt(strength * unit),
        "rsi14_centered": _fmt(strength * unit),
        "realized_vol_21": _fmt((Decimal("11") - strength) * unit),
        "z_ret_21": _fmt(strength * unit),
    }
    return FeatureSnapshot(
        symbol=symbol,
        instrument_id=INSTRUMENT_IDS[symbol],
        interval="1m",
        feature_cutoff_bar_end_utc=DECISION_TS,
        watermark_utc=DECISION_TS,
        features=feats,
        available=available,
        n_bars=51,
        data_pin="EQUS.MINI:tbbo:1m:fixture:m7c-proxy",
        rules_hash="rh-m7c",
        feature_snapshot_id=f"fs-{symbol}",
        refreshed_at_ms=1000,
    )


def _quote(symbol, *, mid=Decimal("100.000000"), spread_bps=Decimal("2.00"),
           ok=True):
    quote = QuoteSnapshot(
        symbol=symbol,
        instrument_id=INSTRUMENT_IDS[symbol],
        bid=Decimal("99.990000"),
        ask=Decimal("100.010000"),
        bid_sz=Decimal("100"),
        ask_sz=Decimal("100"),
        ts_event_utc=DECISION_TS,
        ts_recv_utc=DECISION_TS,
        seen_at_ms=1000,
        reconnect_epoch=0,
        vendor_seq=1,
        dataset="EQUS.MINI",
        schema="tbbo",
    )
    verdict = QuoteVerdict(
        ok=ok,
        reasons=() if ok else ("one_sided",),
        mid=mid if ok else None,
        spread_bps=spread_bps,
        age_ms=0,
    )
    return quote, verdict


def _market_state(symbol, *, two_sided=True):
    return Verdict(
        symbol=symbol,
        instrument_id=INSTRUMENT_IDS[symbol],
        session_state=SessionState.RTH,
        tradability=Tradability.TRADABLE,
        halt=HaltState.NONE,
        luld=LuldState.NORMAL,
        ssr=SsrState.INACTIVE,
        two_sided_nbbo=two_sided,
        short_allowed=True,
        reasons=(),
        ca_blackout=False,
        session_date_et="2026-06-15",
    )


def _snapshot(symbol, *, strength, available=True, quote_ok=True,
              two_sided=True, spread_bps=Decimal("2.00"),
              mid=Decimal("100.000000")):
    quote, verdict = _quote(symbol, mid=mid, spread_bps=spread_bps, ok=quote_ok)
    return SignalSnapshot(
        symbol=symbol,
        instrument_id=INSTRUMENT_IDS[symbol],
        decision_ts_utc=DECISION_TS,
        decision_seen_at_ms=1000,
        rules_hash="rh-m7c",
        feature=_feature(symbol, strength=strength, available=available),
        quote=quote,
        quote_verdict=verdict,
        market_state=_market_state(symbol, two_sided=two_sided),
        calendar_pin="cal-test",
        session_date_et="2026-06-15",
        event_start_bar_end_utc=DECISION_TS,
        horizons=("1m",),
        threshold_k=Decimal("1.0"),
    )


def _co_monotonic_set():
    """All 10 symbols valid; strength decreases with universe index, so AAPL is
    the strongest and MSFT second (top-2)."""
    return [
        _snapshot(sym, strength=Decimal(len(UNIVERSE) - i))
        for i, sym in enumerate(UNIVERSE)
    ]


class TestRelativeStrengthProxyContract(unittest.TestCase):
    def test_strategy_id_is_pinned(self):
        rs = _rs()
        self.assertEqual(rs.STRATEGY_ID, "relative_strength.long_only_proxy_v1")

    def test_rank_selects_top_two_by_score(self):
        rs = _rs()
        ranking = rs.rank_decision_set(
            _co_monotonic_set(), universe_order=UNIVERSE)
        self.assertEqual(ranking.valid_count, 10)
        self.assertEqual(
            [r.symbol for r in ranking.ranked[:2]], ["AAPL", "MSFT"])
        # rs_score is strictly decreasing across the co-monotonic set.
        scores = [r.rs_score for r in ranking.ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_ties_break_by_universe_order(self):
        rs = _rs()
        snaps = []
        for i, sym in enumerate(UNIVERSE):
            # AAPL and MSFT identical and strongest; the rest strictly weaker.
            strength = Decimal("10") if sym in ("AAPL", "MSFT") else Decimal(8 - i)
            snaps.append(_snapshot(sym, strength=strength))
        ranking = rs.rank_decision_set(snaps, universe_order=UNIVERSE)
        self.assertEqual(
            [r.symbol for r in ranking.ranked[:2]], ["AAPL", "MSFT"])

    def test_invalid_symbols_are_excluded(self):
        rs = _rs()
        snaps = list(_co_monotonic_set())
        # Replace four symbols with invalid variants.
        snaps[2] = _snapshot("NVDA", strength=Decimal("9"), available=False)
        snaps[3] = _snapshot("AMZN", strength=Decimal("9"), quote_ok=False)
        snaps[4] = _snapshot("META", strength=Decimal("9"), two_sided=False)
        snaps[5] = _snapshot("GOOGL", strength=Decimal("9"),
                             spread_bps=Decimal("-1"))
        ranking = rs.rank_decision_set(snaps, universe_order=UNIVERSE)
        ranked_syms = {r.symbol for r in ranking.ranked}
        excluded_syms = {e.symbol for e in ranking.excluded}
        self.assertEqual(ranking.valid_count, 6)
        self.assertEqual(excluded_syms, {"NVDA", "AMZN", "META", "GOOGL"})
        self.assertTrue(ranked_syms.isdisjoint(excluded_syms))
        for exclusion in ranking.excluded:
            self.assertIsInstance(exclusion.reason, str)
            self.assertTrue(exclusion.reason)

    def test_fewer_than_eight_valid_does_nothing(self):
        rs = _rs()
        snaps = list(_co_monotonic_set())
        snaps[7] = _snapshot("AVGO", strength=Decimal("3"), available=False)
        snaps[8] = _snapshot("COST", strength=Decimal("2"), quote_ok=False)
        snaps[9] = _snapshot("NFLX", strength=Decimal("1"),
                             spread_bps=Decimal("-1"))
        ranking = rs.rank_decision_set(snaps, universe_order=UNIVERSE)
        self.assertEqual(ranking.valid_count, 7)
        proxy = rs.RelativeStrengthLongOnlyProxyV1()
        candidates = proxy.decide(
            snapshots=snaps, universe_order=UNIVERSE, now_ms=1000)
        self.assertEqual(tuple(candidates), ())

    def test_decide_emits_two_long_buy_candidates(self):
        rs = _rs()
        proxy = rs.RelativeStrengthLongOnlyProxyV1()
        self.assertEqual(
            proxy.strategy_id, "relative_strength.long_only_proxy_v1")
        candidates = proxy.decide(
            snapshots=_co_monotonic_set(), universe_order=UNIVERSE, now_ms=1000)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            {c.legs[0].symbol for c in candidates}, {"AAPL", "MSFT"})
        for candidate in candidates:
            self.assertEqual(
                candidate.strategy_id, "relative_strength.long_only_proxy_v1")
            self.assertTrue(candidate.paper_eligible)
            self.assertEqual(len(candidate.legs), 1)
            leg = candidate.legs[0]
            self.assertEqual(leg.side, "buy")
            self.assertGreaterEqual(leg.qty, Decimal("1"))
            self.assertEqual(leg.qty, leg.qty.to_integral_value())

    def test_strategy_registry(self):
        rs = _rs()
        self.assertEqual(
            rs.strategy_for_id("relative_strength.long_only_proxy_v1").strategy_id,
            "relative_strength.long_only_proxy_v1",
        )
        with self.assertRaises(ValueError):
            rs.strategy_for_id("relative_strength.unknown")

    def test_module_import_surface_stays_pure(self):
        module = _rs()
        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_roots = {
            "agent.broker",
            "agent.execution_preflight",
            "agent.orchestrator",
            "agent.paper_book",
            "datetime",
            "importlib",
            "os",
            "pathlib",
            "subprocess",
            "time",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name, forbidden_roots)
                    self.assertNotIn(alias.name.split(".")[0], forbidden_roots)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                self.assertNotIn(mod, forbidden_roots)
                self.assertNotIn(mod.split(".")[0], forbidden_roots)
            elif isinstance(node, ast.Call):
                self.assertFalse(
                    isinstance(node.func, ast.Name)
                    and node.func.id in {"open", "__import__"},
                    "strategy must not perform file I/O or dynamic imports")


class TestEqualWeightLongBenchmark(unittest.TestCase):
    def test_benchmark_identifier_present(self):
        metrics = importlib.import_module("agent.backtest_metrics")
        self.assertEqual(
            getattr(metrics, "UNIVERSE_EQUAL_WEIGHT_LONG_BENCHMARK", None),
            "universe_equal_weight_long_v1",
        )


if __name__ == "__main__":
    unittest.main()
