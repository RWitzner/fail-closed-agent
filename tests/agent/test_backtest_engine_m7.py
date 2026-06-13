"""M7 Wave 2 - pure anti-lookahead backtest engine tests."""
import unittest
from decimal import Decimal
from tempfile import TemporaryDirectory

from agent.backtest_gate import verify_artifact
from agent.backtest_engine import (
    BacktestSkip,
    BacktestTrade,
    read_eligible_midbar,
    simulate_long_midbar_trade,
)
from agent.backtest_metrics import build_v2_artifact_payload
from agent.bar_series import MidBar, MidBarSeriesReader

DATA_PIN = "EQUS.MINI:tbbo:1m:fixture:m7-engine-v1"


def _bar(end_utc, watermark_utc, mid, *, symbol="AAPL", instrument_id=1001):
    end_minute = end_utc[:16]
    start_utc = end_minute + ":00.000000Z"
    mid_d = Decimal(mid)
    return MidBar(
        symbol=symbol,
        instrument_id=instrument_id,
        interval="1m",
        bucket_start_utc=start_utc,
        bucket_end_utc=end_utc,
        session_date_et="2026-06-15",
        bid=mid_d,
        ask=mid_d,
        mid=mid_d,
        watermark_utc=watermark_utc,
        source_dataset="EQUS.MINI",
        source_schema="tbbo",
        data_pin=DATA_PIN,
        quote_provenance={
            "ts_event_utc": end_utc,
            "ts_recv_utc": watermark_utc,
            "reconnect_epoch": 0,
            "vendor_seq": None,
        },
    )


def _reader(*bars):
    return MidBarSeriesReader(bars)


class TestEligibleReads(unittest.TestCase):
    def test_future_receipt_bar_is_rejected(self):
        reader = _reader(_bar(
            "2026-06-15T14:31:00.000000Z",
            "2026-06-15T14:32:00.000000Z",
            "100.000000",
        ))

        result = read_eligible_midbar(
            reader, "AAPL", 1001, "2026-06-15T14:31:00.000000Z",
            as_of_utc="2026-06-15T14:31:00.000000Z")

        self.assertEqual(result, BacktestSkip(
            reason="future_receipt",
            bucket_end_utc="2026-06-15T14:31:00.000000Z",
            detail={"symbol": "AAPL"},
        ))

    def test_watermark_equal_to_asof_is_eligible_with_mixed_surface_forms(self):
        reader = _reader(_bar(
            "2026-06-15T14:31:00.000000Z",
            "2026-06-15T14:31:00Z",
            "100.000000",
        ))

        result = read_eligible_midbar(
            reader, "AAPL", 1001, "2026-06-15T14:31:00Z",
            as_of_utc="2026-06-15T14:31:00.000000Z")

        self.assertIsInstance(result, MidBar)
        self.assertEqual(result.mid, Decimal("100.000000"))

    def test_lexicographic_timestamp_trap_stays_ineligible(self):
        reader = _reader(_bar(
            "2026-06-15T14:31:00.000000Z",
            "2026-06-15T14:31:00.500000Z",
            "100.000000",
        ))

        result = read_eligible_midbar(
            reader, "AAPL", 1001, "2026-06-15T14:31:00.000000Z",
            as_of_utc="2026-06-15T14:31:00Z")

        self.assertIsInstance(result, BacktestSkip)
        self.assertEqual(result.reason, "future_receipt")


class TestTradeSimulation(unittest.TestCase):
    def test_horizon_crossing_rth_close_is_skipped(self):
        reader = _reader(
            _bar("2026-06-15T19:59:00.000000Z",
                 "2026-06-15T19:59:00.250000Z", "100.000000"),
            _bar("2026-06-15T20:01:00.000000Z",
                 "2026-06-15T20:01:00.000000Z", "101.000000"),
        )

        result = simulate_long_midbar_trade(
            reader=reader,
            symbol="AAPL",
            instrument_id=1001,
            entry_bar_end_utc="2026-06-15T19:59:00.000000Z",
            exit_bar_end_utc="2026-06-15T20:01:00.000000Z",
            decision_ts_utc="2026-06-15T19:59:00.000000Z",
            latency_ms=250,
            rth_close_utc="2026-06-15T20:00:00.000000Z",
            qty=Decimal("2"),
            fees_usd=Decimal("0"),
        )

        self.assertEqual(result.reason, "horizon_crosses_close")

    def test_quote_b_must_be_at_or_after_latency_budget(self):
        too_early = _reader(_bar(
            "2026-06-15T14:31:00.000000Z",
            "2026-06-15T14:31:00.249000Z",
            "100.000000",
        ))
        rejected = simulate_long_midbar_trade(
            reader=too_early,
            symbol="AAPL",
            instrument_id=1001,
            entry_bar_end_utc="2026-06-15T14:31:00.000000Z",
            exit_bar_end_utc="2026-06-15T14:32:00.000000Z",
            decision_ts_utc="2026-06-15T14:31:00.000000Z",
            latency_ms=250,
            rth_close_utc="2026-06-15T20:00:00.000000Z",
            qty=Decimal("2"),
            fees_usd=Decimal("0"),
        )
        self.assertEqual(rejected.reason, "quote_b_before_latency")

        accepted = _reader(
            _bar("2026-06-15T14:31:00.000000Z",
                 "2026-06-15T14:31:00.250000Z", "100.000000"),
            _bar("2026-06-15T14:32:00.000000Z",
                 "2026-06-15T14:32:00.000000Z", "101.000000"),
        )
        trade = simulate_long_midbar_trade(
            reader=accepted,
            symbol="AAPL",
            instrument_id=1001,
            entry_bar_end_utc="2026-06-15T14:31:00.000000Z",
            exit_bar_end_utc="2026-06-15T14:32:00.000000Z",
            decision_ts_utc="2026-06-15T14:31:00.000000Z",
            latency_ms=250,
            rth_close_utc="2026-06-15T20:00:00.000000Z",
            qty=Decimal("2"),
            fees_usd=Decimal("0.10"),
        )
        self.assertIsInstance(trade, BacktestTrade)
        self.assertEqual(trade.net_execution_realistic_pnl_usd,
                         Decimal("1.900000"))


class TestArtifactPayload(unittest.TestCase):
    def test_artifact_payload_is_deterministic_and_verifier_compatible(self):
        trade = BacktestTrade(
            symbol="AAPL",
            instrument_id=1001,
            qty=Decimal("2"),
            entry_bar_end_utc="2026-06-15T14:31:00.000000Z",
            exit_bar_end_utc="2026-06-15T14:32:00.000000Z",
            entry_mid=Decimal("100.000000"),
            exit_mid=Decimal("101.000000"),
            gross_modeled_usd=Decimal("2.000000"),
            fees_usd=Decimal("0.100000"),
            net_execution_realistic_pnl_usd=Decimal("1.900000"),
        )
        kwargs = dict(
            strategy_id="directional.momentum_v1",
            rules_hash="rh-m7",
            data_pin=DATA_PIN,
            trades=(trade,),
            skips=(BacktestSkip("future_receipt", "2026-06-15T14:30:00.000000Z",
                                {"symbol": "AAPL"}),),
            created_utc="2026-06-13T00:00:00.000000Z",
            input_manifest_hash="mh-test",
            builder_git_commit="test",
            tier="fixture",
        )

        payload_a = build_v2_artifact_payload(**kwargs)
        payload_b = build_v2_artifact_payload(**kwargs)

        self.assertEqual(payload_a, payload_b)
        self.assertEqual(payload_a["metrics"]["basis"], "execution_realistic_pnl")
        self.assertEqual(payload_a["metrics"]["pass"], True)
        with TemporaryDirectory() as tmp:
            path = f"{tmp}/directional.momentum_v1.json"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(__import__("json").dumps(payload_a))
            self.assertEqual(verify_artifact(
                "directional.momentum_v1",
                rules_hash="rh-m7",
                data_pin=DATA_PIN,
                artifacts_dir=tmp,
            ).status, "ok")
