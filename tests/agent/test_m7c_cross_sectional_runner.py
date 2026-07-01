"""M7c phase-1 multi-symbol, same-timestamp cross-sectional decision harness.

These tests pin the ONE genuinely new piece phase 1 needs (research packet step 2 /
phase-1 proxy contract FD-P1-7): a runner that assembles every valid symbol's
point-in-time ``SignalSnapshot`` at one decision instant, ranks them with the proxy,
selects the top 2, reuses ``_simulate_historical_long_trade`` per leg, aggregates both
legs under one artifact, applies the no-overlap rule, and scores the
``universe_equal_weight_long_v1`` benchmark.

The fixture is synthetic (no licensed quote rows): each universe symbol gets a
strictly-increasing price path with a DISTINCT per-symbol slope, so the cross-sectional
order is deterministic (AAPL strongest, then MSFT, ...). It only needs enough bars to
warm up the 51-bar feature window and exercise a couple of non-overlapping 30-bar
positions; it does NOT try to satisfy the 20-session / 30-trade M7 sample gate (that is
the credentialed clean-window run, which needs Databento creds).
"""
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agent.backtest_historical import (
    HistoricalCrossSectionalResult,
    run_historical_cross_sectional_backtest,
)
from agent.backtest_metrics import (
    UNIVERSE_EQUAL_WEIGHT_LONG_BENCHMARK,
    build_v2_artifact_payload,
)
from agent.strategies.relative_strength import (
    MIN_VALID_SYMBOLS,
    STRATEGY_ID as RS_STRATEGY_ID,
    TOP_N,
)

UNIVERSE = ("AAPL", "MSFT", "NVDA", "AMZN", "META",
            "GOOGL", "TSLA", "AVGO", "COST", "NFLX")
INSTRUMENT_IDS = {sym: 1001 + i for i, sym in enumerate(UNIVERSE)}
DATA_PINS = {sym: f"EQUS.MINI:tbbo:1m:fixture:{sym.lower()}" for sym in UNIVERSE}
RULES_HASH = "rh-m7c-xs"
SESSION = "2026-06-15"


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_WIGGLE = Decimal("0.0010")


def _rows_for_symbol(symbol, *, n_minutes, slope: Decimal, session=SESSION,
                     base: Decimal = Decimal("100.0000"), start_hhmm="14:30:00",
                     recv_delay_ms=300):
    """A gently-rising path: a DISTINCT per-symbol slope makes momentum/ema/sma ranks
    monotonic in slope, while keeping every minute-over-minute move well under the
    proxy's 5 bps marketable BUY limit (so decision->entry fills are not rejected as
    latency-lost-edge). A small +/-0.0010 wiggle keeps realized vol non-zero; prices
    stay at 4dp so each mid round-trips through the 1e-6 grid. slope >= 0.0010 keeps
    every step non-negative (no crossed/decreasing quotes)."""
    rows = []
    start = datetime.fromisoformat(f"{session}T{start_hhmm}+00:00")
    iid = INSTRUMENT_IDS[symbol]
    seq = 1
    for minute in range(n_minutes):
        ts_event = start + timedelta(minutes=minute)
        ts_recv = ts_event + timedelta(milliseconds=recv_delay_ms)
        mid = base + slope * Decimal(minute) + (_WIGGLE if minute % 2 else Decimal("0"))
        rows.append({
            "dataset": "EQUS.MINI",
            "schema": "tbbo",
            "symbol": symbol,
            "instrument_id": iid,
            "vendor_seq": seq,
            "ts_event_utc": _utc(ts_event),
            "ts_recv_utc": _utc(ts_recv),
            "bid_px": f"{mid - Decimal('0.01'):.6f}",
            "bid_sz": "100",
            "ask_px": f"{mid + Decimal('0.01'):.6f}",
            "ask_sz": "100",
            "reconnect_epoch": 0,
        })
        seq += 1
    return rows


def _full_universe_rows(n_minutes=130):
    """Every symbol present for the full window; slope strictly decreasing with universe
    index so AAPL is strongest and MSFT second (the deterministic top-2)."""
    return {
        sym: _rows_for_symbol(
            sym, n_minutes=n_minutes,
            slope=Decimal(len(UNIVERSE) - i) * Decimal("0.0010"))
        for i, sym in enumerate(UNIVERSE)
    }


def _run(symbol_quote_rows, *, horizon="30m"):
    return run_historical_cross_sectional_backtest(
        symbol_quote_rows=symbol_quote_rows,
        universe=UNIVERSE,
        instrument_ids=INSTRUMENT_IDS,
        rules_hash=RULES_HASH,
        symbol_data_pins=DATA_PINS,
        dataset="EQUS.MINI",
        schema="tbbo",
        horizon=horizon,
    )


def _minutes_between(start_utc: str, end_utc: str) -> int:
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    delta = datetime.strptime(end_utc, fmt) - datetime.strptime(start_utc, fmt)
    return int(delta.total_seconds() // 60)


class TestCrossSectionalRunner(unittest.TestCase):
    def setUp(self):
        self.result = _run(_full_universe_rows())

    def test_returns_cross_sectional_result(self):
        self.assertIsInstance(self.result, HistoricalCrossSectionalResult)

    def test_only_top_two_symbols_are_ever_traded(self):
        traded = {trade.symbol for trade in self.result.trades}
        self.assertEqual(traded, {"AAPL", "MSFT"})
        # Every opened position is a single-leg long with whole-share qty.
        for trade in self.result.trades:
            self.assertEqual(trade.qty, trade.qty.to_integral_value())
            self.assertGreaterEqual(trade.qty, Decimal("1"))

    def test_exit_is_thirty_bar_horizon(self):
        self.assertTrue(self.result.trades)
        for trade in self.result.trades:
            self.assertEqual(
                _minutes_between(trade.decision_bar_end_utc,
                                 trade.exit_bar_end_utc), 30)
            self.assertEqual(
                _minutes_between(trade.decision_bar_end_utc,
                                 trade.entry_bar_end_utc), 1)

    def test_no_overlapping_position_per_symbol(self):
        fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
        for symbol in ("AAPL", "MSFT"):
            legs = sorted(
                (t for t in self.result.trades if t.symbol == symbol),
                key=lambda t: t.entry_bar_end_utc)
            self.assertGreaterEqual(len(legs), 2)
            for prev, nxt in zip(legs, legs[1:]):
                prev_exit = datetime.strptime(prev.exit_bar_end_utc, fmt)
                next_entry = datetime.strptime(nxt.entry_bar_end_utc, fmt)
                self.assertGreater(next_entry, prev_exit)

    def test_overlap_suppression_is_counted(self):
        # Every emitted candidate leg becomes exactly one of: an opened trade, an
        # overlap suppression (symbol still held), or an execution skip (e.g. the
        # exit bar of a late decision is out of the series).
        leg_skips = [s for s in self.result.skips
                     if s.reason != "horizon_crosses_close"]
        self.assertEqual(
            self.result.candidate_count,
            len(self.result.trades) + self.result.overlap_suppressed_leg_count
            + len(leg_skips))
        self.assertGreater(self.result.overlap_suppressed_leg_count, 0)
        self.assertGreaterEqual(self.result.acting_decision_count, 1)

    def test_per_symbol_leg_counts(self):
        self.assertEqual(self.result.per_symbol_leg_counts.get("AAPL"),
                         sum(1 for t in self.result.trades if t.symbol == "AAPL"))
        self.assertEqual(set(self.result.per_symbol_leg_counts),
                         {"AAPL", "MSFT"})

    def test_equal_weight_long_active_pnl_relation(self):
        net = sum((t.net_execution_realistic_pnl_usd for t in self.result.trades),
                  Decimal("0"))
        active = (net - self.result.equal_weight_long_benchmark_net_usd).quantize(
            Decimal("0.000001"))
        self.assertEqual(self.result.equal_weight_long_active_pnl_usd, active)
        self.assertTrue(self.result.equal_weight_long_benchmark_net_usd.is_finite())

    def test_candidate_count_matches_top_n_per_valid_decision(self):
        # Every decision past warmup has 10 valid symbols, so each emits exactly TOP_N
        # candidate legs. (Warmup decisions, < 51 bars, fall below MIN_VALID_SYMBOLS and
        # are counted as insufficient-valid rather than emitting candidates.)
        self.assertEqual(self.result.candidate_count % TOP_N, 0)
        self.assertGreater(self.result.candidate_count, 0)
        self.assertGreater(self.result.insufficient_valid_decision_count, 0)


class TestCrossSectionalMinValidFloor(unittest.TestCase):
    def test_fewer_than_eight_valid_symbols_opens_nothing(self):
        rows = _full_universe_rows()
        # Drop three symbols entirely -> only 7 present -> below MIN_VALID_SYMBOLS.
        for sym in ("TSLA", "AVGO", "COST"):
            rows[sym] = []
        result = _run(rows)
        self.assertEqual(result.trades, ())
        self.assertEqual(result.acting_decision_count, 0)
        self.assertGreater(result.insufficient_valid_decision_count, 0)
        self.assertEqual(result.equal_weight_long_benchmark_net_usd, Decimal("0"))
        self.assertLess(MIN_VALID_SYMBOLS, len(UNIVERSE))


class TestCrossSectionalEdgeCases(unittest.TestCase):
    def test_top_name_priced_above_notional_floors_to_zero_and_is_skipped(self):
        # AAPL has the strongest momentum (rank 1) but is priced above
        # PAPER_NOTIONAL_USD, so whole-share floor -> 0 shares; the proxy skips it
        # WITHOUT promoting the next rank (FD select-then-floor), so only MSFT (rank 2)
        # is ever opened.
        rows = {
            "AAPL": _rows_for_symbol(
                "AAPL", n_minutes=130, slope=Decimal("0.30"),
                base=Decimal("1500.0000")),
        }
        for i, sym in enumerate(UNIVERSE[1:], start=1):
            rows[sym] = _rows_for_symbol(
                sym, n_minutes=130,
                slope=Decimal(len(UNIVERSE) - i) * Decimal("0.0010"))
        result = _run(rows)
        self.assertTrue(result.trades)
        self.assertEqual({t.symbol for t in result.trades}, {"MSFT"})
        self.assertNotIn("AAPL", result.per_symbol_leg_counts)
        # Each valid decision now emits exactly ONE candidate (AAPL dropped pre-floor).
        self.assertEqual(
            result.candidate_count,
            len(result.trades) + result.overlap_suppressed_leg_count
            + len([s for s in result.skips
                   if s.reason != "horizon_crosses_close"]))

    def test_symbol_absent_at_some_decisions_is_handled(self):
        rows = _full_universe_rows()
        # NVDA present only for the first 60 minutes, absent afterwards: at later
        # decisions it simply drops out of the decision set (valid_count 10 -> 9, still
        # >= MIN_VALID_SYMBOLS) without crashing.
        rows["NVDA"] = rows["NVDA"][:60]
        result = _run(rows)
        self.assertTrue(result.trades)
        self.assertEqual({t.symbol for t in result.trades}, {"AAPL", "MSFT"})
        self.assertNotIn("NVDA", result.per_symbol_leg_counts)

    def test_benchmark_fill_and_skip_legs_are_counted(self):
        result = _run(_full_universe_rows())
        self.assertGreaterEqual(result.acting_decision_count, 1)
        # Every acting decision deploys the equal-weight basket across all valid symbols.
        self.assertGreater(result.benchmark_leg_fill_count, 0)
        self.assertGreaterEqual(result.benchmark_leg_skip_count, 0)


class TestCrossSectionalDecisionEligibility(unittest.TestCase):
    def test_late_receipt_decision_bar_cannot_occupy_a_ranked_slot(self):
        # AAPL keeps the steepest slope (cross-sectional rank 1) but every quote is
        # received 90s after its event, so the decision bucket's watermark lands
        # AFTER the decision instant: per FD-2 that bar is not knowable at decision
        # time. The ranked set must equal the tradable set — AAPL may not take a
        # top-2 slot its own leg can never fill (that would displace the true #2/#3
        # and shrink fills without any price leak). With AAPL excluded at the
        # decision read, the top-2 are MSFT and NVDA.
        rows = _full_universe_rows()
        rows["AAPL"] = _rows_for_symbol(
            "AAPL", n_minutes=130,
            slope=Decimal(len(UNIVERSE)) * Decimal("0.0010"),
            recv_delay_ms=90_000)
        result = _run(rows)
        self.assertTrue(result.trades)
        self.assertEqual({t.symbol for t in result.trades}, {"MSFT", "NVDA"})
        self.assertNotIn("AAPL", result.per_symbol_leg_counts)
        self.assertGreater(
            result.exclusion_reason_counts.get("decision_bar_future_receipt", 0), 0)
        # Ranked set == tradable set: no candidate leg dies on a future-receipt
        # decision bar any more.
        self.assertFalse(
            [s for s in result.skips if s.reason == "future_receipt"])


class TestCrossSectionalArtifactAggregation(unittest.TestCase):
    def test_both_legs_aggregate_under_one_artifact(self):
        result = _run(_full_universe_rows())
        self.assertTrue(result.trades)
        payload = build_v2_artifact_payload(
            strategy_id=RS_STRATEGY_ID,
            rules_hash=RULES_HASH,
            data_pin="EQUS.MINI:tbbo:1m:historical:xs-manifest-hash",
            trades=result.trades,
            skips=result.skips,
            created_utc="2026-06-15T20:00:00.000000Z",
            input_manifest_hash="xs-manifest-hash",
            builder_git_commit="test-commit",
            tier="historical_reviewed",
            allocated_notional_usd=Decimal("100000"),
            p95_realism_gap_bps=result.p95_realism_gap_bps,
            max_single_fill_divergence_bps=result.max_single_fill_divergence_bps,
            ca_blackout_skips=result.ca_blackout_skip_count,
            data_quality_skip_count=result.data_quality_skip_count,
            provenance_extra={
                "universe_equal_weight_long_benchmark": UNIVERSE_EQUAL_WEIGHT_LONG_BENCHMARK,
                "universe_equal_weight_long_benchmark_pnl_usd": str(
                    result.equal_weight_long_benchmark_net_usd),
                "universe_equal_weight_long_active_pnl_usd": str(
                    result.equal_weight_long_active_pnl_usd),
            },
        )
        self.assertEqual(payload["strategy_id"], RS_STRATEGY_ID)
        self.assertEqual(payload["metrics"]["strategy_version"], RS_STRATEGY_ID)
        # Both held names appear in the single aggregated artifact sample.
        self.assertEqual(
            set(payload["metrics"]["sample"]["symbols"]), {"AAPL", "MSFT"})
        self.assertEqual(
            payload["metrics"]["provenance"][
                "universe_equal_weight_long_benchmark"],
            "universe_equal_weight_long_v1")
        # The pinned verifier benchmark is unchanged (exposure_matched_midbar_v1).
        self.assertEqual(
            payload["metrics"]["benchmark"]["method"],
            "exposure_matched_midbar_v1")


if __name__ == "__main__":
    unittest.main()
