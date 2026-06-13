"""M7 Wave 3 - first paper-eligible directional strategy."""
import ast
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
from agent.strategy import ScanContext, Strategy
from agent.strategies import directional_momentum
from agent.strategies.directional_momentum import MomentumV1Strategy


def _feature(**overrides):
    features = {
        "z_ret_21": "0.10000000",
        "momentum_9": "0.03000000",
        "momentum_21": "0.02000000",
        "rsi14_centered": "0.10000000",
        "ema_gap_9_21": "0.01000000",
        "sma_gap_21_50": "0.01000000",
        "realized_vol_21": "0.01000000",
    }
    features.update(overrides.pop("features", {}))
    return FeatureSnapshot(
        symbol="AAPL",
        instrument_id=1001,
        interval="1m",
        feature_cutoff_bar_end_utc="2026-06-15T14:31:00.000000Z",
        watermark_utc="2026-06-15T14:31:00.000000Z",
        features=features,
        available=overrides.pop("available", True),
        n_bars=51,
        data_pin="EQUS.MINI:tbbo:1m:fixture:m7-strategy-v1",
        rules_hash="rh-m7",
        feature_snapshot_id="fs-test",
        refreshed_at_ms=1000,
        **overrides,
    )


def _quote(mid=Decimal("100.000000"), spread_bps=Decimal("2.00")):
    return QuoteSnapshot(
        symbol="AAPL",
        instrument_id=1001,
        bid=Decimal("99.990000"),
        ask=Decimal("100.010000"),
        bid_sz=Decimal("100"),
        ask_sz=Decimal("100"),
        ts_event_utc="2026-06-15T14:31:00.000000Z",
        ts_recv_utc="2026-06-15T14:31:00.000000Z",
        seen_at_ms=1000,
        reconnect_epoch=0,
        vendor_seq=1,
        dataset="EQUS.MINI",
        schema="tbbo",
    ), QuoteVerdict(ok=True, reasons=(), mid=mid,
                    spread_bps=spread_bps, age_ms=0)


def _market_state():
    return Verdict(
        symbol="AAPL",
        instrument_id=1001,
        session_state=SessionState.RTH,
        tradability=Tradability.TRADABLE,
        halt=HaltState.NONE,
        luld=LuldState.NORMAL,
        ssr=SsrState.INACTIVE,
        two_sided_nbbo=True,
        short_allowed=True,
        reasons=(),
        ca_blackout=False,
        session_date_et="2026-06-15",
    )


def _snapshot(*, feature=None, mid=Decimal("100.000000"),
              quote_verdict=None, spread_bps=Decimal("2.00")):
    quote, verdict = _quote(mid, spread_bps=spread_bps)
    if quote_verdict is not None:
        verdict = quote_verdict
    return SignalSnapshot(
        symbol="AAPL",
        instrument_id=1001,
        decision_ts_utc="2026-06-15T14:31:00.000000Z",
        decision_seen_at_ms=1000,
        rules_hash="rh-m7",
        feature=feature if feature is not None else _feature(),
        quote=quote,
        quote_verdict=verdict,
        market_state=_market_state(),
        calendar_pin="cal-test",
        session_date_et="2026-06-15",
        event_start_bar_end_utc="2026-06-15T14:31:00.000000Z",
        horizons=("1m",),
        threshold_k=Decimal("1.0"),
    )


class TestMomentumV1Strategy(unittest.TestCase):
    def test_positive_momentum_snapshot_emits_single_paper_candidate(self):
        strategy = MomentumV1Strategy()
        ctx = ScanContext(snapshot=_snapshot(), rules_hash="rh-m7", now_ms=1000)

        candidates = strategy.scan(ctx)

        self.assertIsInstance(strategy, Strategy)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.strategy_id, "directional.momentum_v1")
        self.assertEqual(candidate.paper_eligible, True)
        self.assertEqual(candidate.score, Decimal("198.000000"))
        self.assertEqual(len(candidate.legs), 1)
        leg = candidate.legs[0]
        self.assertEqual(leg.symbol, "AAPL")
        self.assertEqual(leg.instrument_id, 1001)
        self.assertEqual(leg.side, "buy")
        self.assertEqual(leg.qty, Decimal("10"))
        self.assertEqual(leg.limit_price, Decimal("101.98"))

    def test_missing_or_weak_signal_emits_no_candidate(self):
        cases = (
            _feature(features={"momentum_9": "0.00000000"}),
            _feature(features={"momentum_21": "-0.00010000"}),
            _feature(features={"z_ret_21": "-0.00000001"}),
            _feature(features={"realized_vol_21": "0.00000000"}),
            _feature(available=False),
        )
        strategy = MomentumV1Strategy()

        for feature in cases:
            with self.subTest(feature=feature):
                ctx = ScanContext(snapshot=_snapshot(feature=feature),
                                  rules_hash="rh-m7", now_ms=1000)
                self.assertEqual(strategy.scan(ctx), ())

    def test_quote_mid_is_required(self):
        strategy = MomentumV1Strategy()
        verdict = QuoteVerdict(ok=True, reasons=(), mid=None,
                               spread_bps=Decimal("2.00"), age_ms=0)
        ctx = ScanContext(snapshot=_snapshot(quote_verdict=verdict),
                          rules_hash="rh-m7", now_ms=1000)

        self.assertEqual(strategy.scan(ctx), ())

    def test_module_import_surface_stays_pure(self):
        source = Path(__import__(
            "agent.strategies.directional_momentum", fromlist=["x"]).__file__
        ).read_text(encoding="utf-8")
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
                module = node.module or ""
                self.assertNotIn(module, forbidden_roots)
                self.assertNotIn(module.split(".")[0], forbidden_roots)
            elif isinstance(node, ast.Call):
                self.assertFalse(
                    isinstance(node.func, ast.Name)
                    and node.func.id in {"open", "__import__"},
                    "strategy must not perform file I/O or dynamic imports")


class TestMomentumV2Strategy(unittest.TestCase):
    def _strategy(self):
        cls = getattr(directional_momentum, "MomentumV2Strategy", None)
        self.assertIsNotNone(cls, "MomentumV2Strategy must be registered")
        return cls()

    def test_stricter_trend_snapshot_emits_v2_candidate(self):
        strategy = self._strategy()
        feature = _feature(features={
            "z_ret_21": "0.30000000",
            "momentum_9": "0.03000000",
            "momentum_21": "0.02000000",
            "ema_gap_9_21": "0.01000000",
            "sma_gap_21_50": "0.01200000",
            "realized_vol_21": "0.01000000",
        })
        ctx = ScanContext(snapshot=_snapshot(feature=feature),
                          rules_hash="rh-m7", now_ms=1000)

        candidates = strategy.scan(ctx)

        self.assertIsInstance(strategy, Strategy)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.strategy_id, "directional.momentum_v2")
        self.assertEqual(candidate.paper_eligible, True)
        self.assertEqual(candidate.score, Decimal("95.000000"))
        leg = candidate.legs[0]
        self.assertEqual(leg.side, "buy")
        self.assertEqual(leg.qty, Decimal("10"))
        self.assertEqual(leg.limit_price, Decimal("100.95"))

    def test_v2_rejects_weak_trend_overextension_and_wide_spread(self):
        strategy = self._strategy()
        cases = (
            {"ema_gap_9_21": "-0.00010000", "z_ret_21": "0.30000000"},
            {"sma_gap_21_50": "0.00000000", "z_ret_21": "0.30000000"},
            {"z_ret_21": "0.14999999"},
            {"z_ret_21": "2.50000001"},
            {"realized_vol_21": "0.00000000", "z_ret_21": "0.30000000"},
        )
        for features in cases:
            with self.subTest(features=features):
                ctx = ScanContext(
                    snapshot=_snapshot(feature=_feature(features=features)),
                    rules_hash="rh-m7",
                    now_ms=1000,
                )
                self.assertEqual(strategy.scan(ctx), ())

        wide_spread_ctx = ScanContext(
            snapshot=_snapshot(
                feature=_feature(features={"z_ret_21": "0.30000000"}),
                spread_bps=Decimal("5.000001"),
            ),
            rules_hash="rh-m7",
            now_ms=1000,
        )
        self.assertEqual(strategy.scan(wide_spread_ctx), ())

    def test_strategy_registry_rejects_unknown_ids(self):
        registry = getattr(directional_momentum, "strategy_for_id", None)
        self.assertIsNotNone(registry, "strategy_for_id must be defined")
        self.assertEqual(
            registry("directional.momentum_v2").strategy_id,
            "directional.momentum_v2",
        )
        with self.assertRaises(ValueError):
            registry("directional.unknown")


if __name__ == "__main__":
    unittest.main()
