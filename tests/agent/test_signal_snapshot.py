"""M3 §M.4 — SignalSnapshot assembly + frozen gate order (§D). [S2, fail-closed]

Gate order is FROZEN (funnel attribution depends on it): identity -> features ->
quote -> market_state -> (per-horizon) horizon. Within the failing stage ALL
applicable reasons are collected, sorted (rev2 SAFETY-F7). Gates 1-4 stop the tick;
gate 5 is per-horizon.
"""
import unittest
from decimal import Decimal
from pathlib import Path

from agent import config as agent_config
from agent.feature_engine import FeatureSnapshot
from agent.market_calendar import SessionSchedule
from agent.market_state import (
    HaltState, LuldState, SessionState, SsrState, Tradability, Verdict,
)
from agent.market_state_cache import MarketStateCache
from agent.quote_quality import QuoteSnapshot
from agent.signal_config import SignalConfig
from agent.signal_snapshot import GateFail, SignalSnapshot, assemble, horizon_gate

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = agent_config.load(REPO_ROOT / "config" / "agent_rules.json")
SIGNAL = SignalConfig.from_config(CONFIG)

TICK_BAR_END = "2026-06-15T14:21:00.000000Z"
DECISION_TS = "2026-06-15T14:21:00.250000Z"


def _feature(**overrides):
    base = dict(
        symbol="AAPL", instrument_id=1001, interval="1m",
        feature_cutoff_bar_end_utc=TICK_BAR_END,
        watermark_utc="2026-06-15T14:20:59.000000Z",
        features={name: "0.00000000" for name in (
            "z_ret_21", "momentum_9", "momentum_21", "rsi14_centered",
            "ema_gap_9_21", "sma_gap_21_50", "realized_vol_21")},
        available=True, n_bars=51,
        data_pin="EQUS.MINI:tbbo:1m:fixture:test-v1",
        rules_hash=SIGNAL.rules_hash,
        feature_snapshot_id="fs-test", refreshed_at_ms=1_000,
    )
    base.update(overrides)
    return FeatureSnapshot(**base)


def _quote(**overrides):
    base = dict(
        symbol="AAPL", instrument_id=1001,
        bid=Decimal("199.9900"), ask=Decimal("200.0100"),
        bid_sz=Decimal("300"), ask_sz=Decimal("200"),
        ts_event_utc="2026-06-15T14:20:59.500000Z",
        ts_recv_utc="2026-06-15T14:20:59.600000Z",
        seen_at_ms=900, reconnect_epoch=0, vendor_seq=7,
        dataset="EQUS.MINI", schema="tbbo",
    )
    base.update(overrides)
    return QuoteSnapshot(**base)


def _verdict(**overrides):
    base = dict(
        symbol="AAPL", instrument_id=1001,
        session_state=SessionState.RTH, tradability=Tradability.TRADABLE,
        halt=HaltState.NONE, luld=LuldState.NORMAL, ssr=SsrState.INACTIVE,
        two_sided_nbbo=True, short_allowed=True, reasons=(),
        ca_blackout=False, session_date_et="2026-06-15",
    )
    base.update(overrides)
    return Verdict(**base)


def _assemble(*, feature=..., quote=..., market_state=None, now_ms=1_000,
              event_start_bar_end_utc=TICK_BAR_END):
    return assemble(
        symbol="AAPL", instrument_id=1001,
        decision_ts_utc=DECISION_TS, decision_seen_at_ms=now_ms,
        event_start_bar_end_utc=event_start_bar_end_utc,
        feature=_feature() if feature is ... else feature,
        quote=_quote() if quote is ... else quote,
        market_state=market_state or _verdict(),
        calendar_pin="fixture:XNYS-2026-v1",
        config=SIGNAL, now_ms=now_ms,
    )


def _schedule(close_et_utc="2026-06-15T20:00:00.000000Z"):
    return SessionSchedule(
        session_date_et="2026-06-15", is_trading_day=True, is_early_close=False,
        pre_open_utc="2026-06-15T08:00:00.000000Z",
        rth_open_utc="2026-06-15T13:30:00.000000Z",
        rth_close_utc=close_et_utc,
        post_close_utc="2026-06-16T00:00:00.000000Z",
    )


class TestAssemblePasses(unittest.TestCase):
    def test_clean_inputs_yield_snapshot(self):
        snapshot = _assemble()
        self.assertIsInstance(snapshot, SignalSnapshot)
        self.assertEqual(snapshot.event_start_bar_end_utc, TICK_BAR_END)
        self.assertEqual(snapshot.session_date_et, "2026-06-15")
        self.assertEqual(snapshot.horizons, ("5m", "30m"))
        self.assertEqual(snapshot.threshold_k, Decimal("0"))
        self.assertTrue(snapshot.quote_verdict.ok)


class TestGateOrder(unittest.TestCase):
    def test_multi_fault_trips_earliest_stage_only(self):
        # identity fault (wrong symbol on quote) + stale feature + bad market state:
        result = _assemble(
            quote=_quote(symbol="MSFT", instrument_id=2002),
            feature=_feature(refreshed_at_ms=-100_000),
            market_state=MarketStateCache.safe_default_verdict("AAPL", 1001, "2026-06-15"),
        )
        self.assertIsInstance(result, GateFail)
        self.assertEqual(result.stage, "identity")
        self.assertEqual(result.reasons, ("identity_mismatch",))

    def test_identity_mismatch_on_feature(self):
        result = _assemble(feature=_feature(symbol="MSFT", instrument_id=2002))
        self.assertEqual(result.stage, "identity")

    def test_market_state_identity_mismatch(self):
        result = _assemble(market_state=_verdict(symbol="MSFT", instrument_id=2002))
        self.assertEqual(result.stage, "identity")


class TestFeaturesStage(unittest.TestCase):
    def test_feature_none(self):
        result = _assemble(feature=None)
        self.assertEqual((result.stage, result.reasons),
                         ("features", ("features_unavailable",)))
        self.assertIsNone(result.horizon)

    def test_unavailable(self):
        result = _assemble(feature=_feature(available=False, features={}, n_bars=50))
        self.assertEqual(result.stage, "features")
        self.assertIn("features_unavailable", result.reasons)

    def test_stale_strict_boundary(self):
        # refreshed at 1_000, max 5_000: now 6_000 -> age exactly 5_000 -> fresh.
        # (the quote rides the clock so only the FEATURE boundary is under test)
        ok = _assemble(now_ms=6_000, quote=_quote(seen_at_ms=5_900))
        self.assertIsInstance(ok, SignalSnapshot)
        stale = _assemble(now_ms=6_001, quote=_quote(seen_at_ms=5_900))
        self.assertIsInstance(stale, GateFail)
        self.assertEqual((stale.stage, stale.reasons), ("features", ("feature_stale",)))

    def test_bar_lag_boundary_at_exactly_two_intervals(self):
        # cutoff 14:21; decision at 14:23:00.000 == exactly 2 intervals -> pass.
        at_limit = assemble(
            symbol="AAPL", instrument_id=1001,
            decision_ts_utc="2026-06-15T14:23:00.000000Z", decision_seen_at_ms=1_000,
            event_start_bar_end_utc=TICK_BAR_END,
            feature=_feature(), quote=_quote(), market_state=_verdict(),
            calendar_pin="p", config=SIGNAL, now_ms=1_000,
        )
        self.assertIsInstance(at_limit, SignalSnapshot)
        over = assemble(
            symbol="AAPL", instrument_id=1001,
            decision_ts_utc="2026-06-15T14:23:00.000001Z", decision_seen_at_ms=1_000,
            event_start_bar_end_utc=TICK_BAR_END,
            feature=_feature(), quote=_quote(), market_state=_verdict(),
            calendar_pin="p", config=SIGNAL, now_ms=1_000,
        )
        self.assertIsInstance(over, GateFail)
        self.assertEqual((over.stage, over.reasons), ("features", ("bar_lag_exceeded",)))

    def test_cutoff_mismatch_rev2(self):
        result = _assemble(
            feature=_feature(feature_cutoff_bar_end_utc="2026-06-15T14:20:00.000000Z"),
        )
        self.assertIsInstance(result, GateFail)
        self.assertEqual((result.stage, result.reasons),
                         ("features", ("feature_cutoff_mismatch",)))


class TestQuoteStage(unittest.TestCase):
    def test_quote_none(self):
        result = _assemble(quote=None)
        self.assertEqual((result.stage, result.reasons), ("quote", ("quote_missing",)))

    def test_quote_reasons_pass_through(self):
        result = _assemble(quote=_quote(bid=Decimal("200.0200")))  # crossed
        self.assertEqual(result.stage, "quote")
        self.assertIn("quote_crossed", result.reasons)


class TestMarketStateStage(unittest.TestCase):
    def test_safe_default_exact_three_tuple(self):
        result = _assemble(
            market_state=MarketStateCache.safe_default_verdict("AAPL", 1001, "2026-06-15"))
        self.assertIsInstance(result, GateFail)
        self.assertEqual(result.stage, "market_state")
        self.assertEqual(result.reasons, (
            "market_state_not_rth", "market_state_not_tradable",
            "market_state_stale_default",
        ))

    def test_reduce_only_in_rth_exact_single(self):
        result = _assemble(market_state=_verdict(tradability=Tradability.REDUCE_ONLY))
        self.assertEqual(result.reasons, ("market_state_not_tradable",))

    def test_pre_session_includes_not_rth(self):
        result = _assemble(market_state=_verdict(session_state=SessionState.PRE,
                                                 tradability=Tradability.REDUCE_ONLY))
        self.assertEqual(result.stage, "market_state")
        self.assertIn("market_state_not_rth", result.reasons)
        self.assertIn("market_state_not_tradable", result.reasons)


class TestHorizonGate(unittest.TestCase):
    def setUp(self):
        self.snapshot = _assemble()
        self.assertIsInstance(self.snapshot, SignalSnapshot)

    def test_pass_returns_canonical_resolve_key(self):
        result = horizon_gate(self.snapshot, "5m", _schedule())
        self.assertEqual(result, "2026-06-15T14:26:00.000000Z")

    def test_none_schedule_is_calendar_unknown(self):
        result = horizon_gate(self.snapshot, "5m", None)
        self.assertIsInstance(result, GateFail)
        self.assertEqual((result.stage, result.reasons, result.horizon),
                         ("horizon", ("calendar_unknown",), "5m"))

    def test_crosses_close_1531_plus_30m(self):
        late = assemble(
            symbol="AAPL", instrument_id=1001,
            decision_ts_utc="2026-06-15T19:31:00.250000Z", decision_seen_at_ms=1_000,
            event_start_bar_end_utc="2026-06-15T19:31:00.000000Z",   # 15:31 ET
            feature=_feature(feature_cutoff_bar_end_utc="2026-06-15T19:31:00.000000Z"),
            quote=_quote(), market_state=_verdict(),
            calendar_pin="p", config=SIGNAL, now_ms=1_000,
        )
        self.assertIsInstance(late, SignalSnapshot)
        result = horizon_gate(late, "30m", _schedule())  # 16:01 ET > 16:00 close
        self.assertEqual(result.reasons, ("session_horizon_crosses_close",))
        self.assertEqual(result.horizon, "30m")

    def test_half_day_1245_plus_30m_crosses_1225_plus_5m_passes(self):
        half_close = "2026-11-27T18:00:00.000000Z"  # 13:00 ET (EST)
        schedule = SessionSchedule(
            session_date_et="2026-11-27", is_trading_day=True, is_early_close=True,
            pre_open_utc="2026-11-27T09:00:00.000000Z",
            rth_open_utc="2026-11-27T14:30:00.000000Z",
            rth_close_utc=half_close,
            post_close_utc="2026-11-27T22:00:00.000000Z",
        )
        snap_1245 = assemble(
            symbol="AAPL", instrument_id=1001,
            decision_ts_utc="2026-11-27T17:45:00.250000Z", decision_seen_at_ms=1_000,
            event_start_bar_end_utc="2026-11-27T17:45:00.000000Z",  # 12:45 ET
            feature=_feature(feature_cutoff_bar_end_utc="2026-11-27T17:45:00.000000Z"),
            quote=_quote(), market_state=_verdict(session_date_et="2026-11-27"),
            calendar_pin="p", config=SIGNAL, now_ms=1_000,
        )
        crosses = horizon_gate(snap_1245, "30m", schedule)  # 13:15 ET > 13:00
        self.assertEqual(crosses.reasons, ("session_horizon_crosses_close",))

        snap_1225 = assemble(
            symbol="AAPL", instrument_id=1001,
            decision_ts_utc="2026-11-27T17:25:00.250000Z", decision_seen_at_ms=1_000,
            event_start_bar_end_utc="2026-11-27T17:25:00.000000Z",  # 12:25 ET
            feature=_feature(feature_cutoff_bar_end_utc="2026-11-27T17:25:00.000000Z"),
            quote=_quote(), market_state=_verdict(session_date_et="2026-11-27"),
            calendar_pin="p", config=SIGNAL, now_ms=1_000,
        )
        passes = horizon_gate(snap_1225, "5m", schedule)  # 12:30 ET <= 13:00
        self.assertEqual(passes, "2026-11-27T17:30:00.000000Z")

    def test_exactly_at_close_passes(self):
        # resolve == rth_close is allowed (<=): 15:30 ET + 30m == 16:00 close.
        snap = assemble(
            symbol="AAPL", instrument_id=1001,
            decision_ts_utc="2026-06-15T19:30:00.250000Z", decision_seen_at_ms=1_000,
            event_start_bar_end_utc="2026-06-15T19:30:00.000000Z",
            feature=_feature(feature_cutoff_bar_end_utc="2026-06-15T19:30:00.000000Z"),
            quote=_quote(), market_state=_verdict(),
            calendar_pin="p", config=SIGNAL, now_ms=1_000,
        )
        result = horizon_gate(snap, "30m", _schedule())
        self.assertEqual(result, "2026-06-15T20:00:00.000000Z")

    def test_unknown_horizon_raises(self):
        with self.assertRaises(ValueError):
            horizon_gate(self.snapshot, "7m", _schedule())


if __name__ == "__main__":
    unittest.main()
