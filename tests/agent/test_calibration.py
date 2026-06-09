"""M3 §M.7 — resolver + scoring (§G). [S3, S4]

The S3 resolver wall (future_receipt => DEFER, never score, never ingest), FD-8
terminal-unresolved idempotency, climatology as-of + resume-seeding, and the frozen
scoring math (Murphy on the constant-p-per-bin fixture).
"""
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.bar_series import MidBar, MissingBar, MidBarSeriesReader
from agent.calibration import (
    BRIER_QUANTUM,
    REPORT_QUANTUM,
    UNRESOLVED_REASONS,
    AsOfClimatology,
    ForecastResolver,
    ResolveStats,
    ScoredLedger,
    brier,
    brier_skill_score,
    murphy_decomposition,
    reliability_bins,
)
from agent.journal import replay
from recorder.persistence import EventWriter

PIN = "EQUS.MINI:tbbo:1m:fixture:test-v1"


def _bar(end_hhmm, watermark, mid="100.000000", *, start_hhmm=None):
    hh, mm = end_hhmm.split(":")
    if start_hhmm is None:
        start_mm = int(mm) - 1
        start_hh = int(hh)
        if start_mm < 0:
            start_mm, start_hh = 59, start_hh - 1
        start = f"2026-06-15T{start_hh:02d}:{start_mm:02d}:00.000000Z"
    else:
        shh, smm = start_hhmm.split(":")
        start = f"2026-06-15T{shh}:{smm}:00.000000Z"
    end = f"2026-06-15T{hh}:{mm}:00.000000Z"
    mid_d = Decimal(mid)
    return MidBar(
        symbol="AAPL", instrument_id=1001, interval="1m",
        bucket_start_utc=start, bucket_end_utc=end, session_date_et="2026-06-15",
        bid=mid_d, ask=mid_d, mid=mid_d,
        watermark_utc=watermark, source_dataset="EQUS.MINI", source_schema="tbbo",
        data_pin=PIN,
        quote_provenance={"ts_event_utc": start, "ts_recv_utc": watermark,
                          "reconnect_epoch": 0, "vendor_seq": 1},
    )


def _decision_row(forecast_id, *, t0="14:00", th="14:05", p="0.600000",
                  horizon="5m", k="0", action="forecast_only", decision_id=None):
    return {
        "action": action,
        "symbol": "AAPL",
        "instrument_id": 1001,
        "decision_id": decision_id or f"d-{forecast_id}",
        "forecast_id": forecast_id,
        "horizon": horizon,
        "event_start_bar_key": f"AAPL|1m|2026-06-15T{t0}:00.000000Z",
        "resolve_bar_key": f"AAPL|1m|2026-06-15T{th}:00.000000Z",
        "forecast": {"event_type": "up_move", "h": horizon, "k": k, "p": p},
        "data_pin": PIN,
    }


class _LedgerEnv:
    def __init__(self, tmpdir):
        self.path = Path(tmpdir) / "forecast_scored.jsonl"
        self.writer = EventWriter(self.path, "run-test",
                                  clock=lambda: "2026-06-15T18:00:00+00:00")
        self.ledger = ScoredLedger(self.writer, rules_hash="rh-test")


class TestScoring(unittest.TestCase):
    def _resolve(self, tmpdir, bars, rows, now="2026-06-15T15:00:00.000000Z",
                 climatology=None, missing=()):
        env = _LedgerEnv(tmpdir)
        resolver = ForecastResolver(
            reader=MidBarSeriesReader(bars, missing), ledger=env.ledger,
            scored_stream_path=env.path, climatology=climatology,
        )
        stats = resolver.resolve_due(rows, now_utc=now)
        return env, resolver, stats

    def test_outcome_boundary_equality_scores_one(self):
        bars = [
            _bar("14:00", "2026-06-15T13:59:59.000000Z", "100.000000"),
            _bar("14:05", "2026-06-15T14:04:59.000000Z", "100.000000"),  # == t0*(1+0)
        ]
        with TemporaryDirectory() as tmpdir:
            env, _, stats = self._resolve(tmpdir, bars, [_decision_row("f-1")])
            self.assertEqual(stats.scored, 1)
            rows = replay(env.path)
            self.assertEqual(rows[0]["outcome"], 1)  # >= boundary (D2)
            self.assertEqual(rows[0]["event_type"], "forecast_scored")
            self.assertEqual(rows[0]["resolved_as_of_utc"], "2026-06-15T15:00:00.000000Z")
            # brier_i = (0.600000 - 1)^2 = 0.160000000000 exact at 1e-12
            self.assertEqual(Decimal(rows[0]["brier_i"]), Decimal("0.160000000000"))
            prov = rows[0]["label_provenance"]
            self.assertEqual(set(prov), {"t0", "th", "source_dataset", "source_schema"})
            self.assertEqual(set(prov["t0"]), {"watermark_utc", "ts_event_utc",
                                               "ts_recv_utc", "reconnect_epoch",
                                               "vendor_seq"})

    def test_down_move_scores_zero(self):
        bars = [
            _bar("14:00", "2026-06-15T13:59:59.000000Z", "100.000000"),
            _bar("14:05", "2026-06-15T14:04:59.000000Z", "99.999999"),
        ]
        with TemporaryDirectory() as tmpdir:
            env, _, _ = self._resolve(tmpdir, bars, [_decision_row("f-1")])
            self.assertEqual(replay(env.path)[0]["outcome"], 0)

    def test_not_due_rows_left_alone(self):
        bars = [_bar("14:00", "2026-06-15T13:59:59.000000Z")]
        with TemporaryDirectory() as tmpdir:
            _, _, stats = self._resolve(
                tmpdir, bars, [_decision_row("f-1", th="16:00")],
                now="2026-06-15T15:00:00.000000Z")
            self.assertEqual((stats.considered, stats.due, stats.scored), (1, 0, 0))

    def test_s3_future_receipt_defers_then_scores_once(self):
        bars = [
            _bar("14:00", "2026-06-15T13:59:59.000000Z", "100.000000"),
            # resolve bar received LATE: watermark 14:21 > first resolve attempt
            _bar("14:05", "2026-06-15T14:21:00.000000Z", "101.000000"),
        ]
        climatology = AsOfClimatology(min_samples=1)
        with TemporaryDirectory() as tmpdir:
            env, resolver, stats = self._resolve(
                tmpdir, bars, [_decision_row("f-1")],
                now="2026-06-15T14:20:00.000000Z", climatology=climatology)
            # deferred: NO row, NO climatology ingest (rev2 SAFETY-F1)
            self.assertEqual(stats.deferred_not_eligible, 1)
            self.assertEqual(stats.scored, 0)
            self.assertEqual(replay(env.path), [])
            self.assertEqual(climatology.rate(symbol="AAPL", horizon="5m")[1],
                             "constant_0.5")
            # retry after the watermark: scores exactly once
            stats2 = resolver.resolve_due([_decision_row("f-1")],
                                          now_utc="2026-06-15T14:22:00.000000Z")
            self.assertEqual(stats2.scored, 1)
            self.assertEqual(len(replay(env.path)), 1)
            self.assertEqual(climatology.rate(symbol="AAPL", horizon="5m")[2], 1)

    def test_s4_gap_is_terminal_unresolved_with_reasons(self):
        bars = [
            _bar("14:00", "2026-06-15T13:59:59.000000Z"),
            _bar("14:10", "2026-06-15T14:09:59.000000Z", start_hhmm="14:09"),
        ]
        with TemporaryDirectory() as tmpdir:
            env, _, stats = self._resolve(tmpdir, bars, [_decision_row("f-1")])
            self.assertEqual(stats.unresolved, 1)
            row = replay(env.path)[0]
            self.assertEqual(row["event_type"], "forecast_unresolved")
            self.assertEqual(row["reason"], "no_mid_bar_resolve")
            self.assertEqual(row["missing_bar_reason"], "no_quotes_in_bucket")

    def test_label_source_mismatch(self):
        other_pin_bar = MidBar(**{
            **_bar("14:05", "2026-06-15T14:04:59.000000Z").__dict__,
            "data_pin": "EQUS.MINI:tbbo:1m:fixture:OTHER",
        })
        bars = [_bar("14:00", "2026-06-15T13:59:59.000000Z"), other_pin_bar]
        with TemporaryDirectory() as tmpdir:
            env, _, stats = self._resolve(tmpdir, bars, [_decision_row("f-1")])
            self.assertEqual(stats.unresolved, 1)
            row = replay(env.path)[0]
            self.assertEqual(row["reason"], "label_source_mismatch")
            self.assertIsNone(row["missing_bar_reason"])

    def test_fd8_rerun_appends_nothing(self):
        bars = [
            _bar("14:00", "2026-06-15T13:59:59.000000Z", "100.000000"),
            _bar("14:05", "2026-06-15T14:04:59.000000Z", "101.000000"),
        ]
        with TemporaryDirectory() as tmpdir:
            env, resolver, _ = self._resolve(tmpdir, bars, [_decision_row("f-1")])
            stats = resolver.resolve_due([_decision_row("f-1")],
                                         now_utc="2026-06-15T15:00:00.000000Z")
            self.assertEqual(stats.skipped_already_resolved, 1)
            self.assertEqual(stats.scored, 0)
            self.assertEqual(len(replay(env.path)), 1)

    def test_duplicate_decision_rows_score_once(self):
        bars = [
            _bar("14:00", "2026-06-15T13:59:59.000000Z", "100.000000"),
            _bar("14:05", "2026-06-15T14:04:59.000000Z", "101.000000"),
        ]
        with TemporaryDirectory() as tmpdir:
            env, _, stats = self._resolve(
                tmpdir, bars, [_decision_row("f-1"), _decision_row("f-1")])
            self.assertEqual(stats.scored, 1)
            self.assertEqual(len(replay(env.path)), 1)

    def test_resume_seeding_matches_uninterrupted_run(self):
        bars = [
            _bar("14:00", "2026-06-15T13:59:59.000000Z", "100.000000"),
            _bar("14:05", "2026-06-15T14:04:59.000000Z", "101.000000"),
            _bar("14:10", "2026-06-15T14:09:59.000000Z", "102.000000",
                 start_hhmm="14:09"),
        ]
        rows = [_decision_row("f-1"),
                _decision_row("f-2", t0="14:05", th="14:10")]
        with TemporaryDirectory() as tmpdir:
            # uninterrupted run
            clim_a = AsOfClimatology(min_samples=1)
            env_a, _, _ = self._resolve(tmpdir, bars, rows, climatology=clim_a)
        with TemporaryDirectory() as tmpdir:
            env = _LedgerEnv(tmpdir)
            clim_b = AsOfClimatology(min_samples=1)
            first = ForecastResolver(reader=MidBarSeriesReader(bars),
                                     ledger=env.ledger, scored_stream_path=env.path,
                                     climatology=clim_b)
            first.resolve_due([rows[0]], now_utc="2026-06-15T15:00:00.000000Z")
            # resume: NEW resolver + NEW climatology over the existing stream
            clim_c = AsOfClimatology(min_samples=1)
            second = ForecastResolver(reader=MidBarSeriesReader(bars),
                                      ledger=env.ledger, scored_stream_path=env.path,
                                      climatology=clim_c)
            second.resolve_due(rows, now_utc="2026-06-15T15:00:00.000000Z")
            self.assertEqual(clim_c.rate(symbol="AAPL", horizon="5m"),
                             clim_a.rate(symbol="AAPL", horizon="5m"))

    def test_stats_shape(self):
        self.assertEqual(
            set(ResolveStats.__dataclass_fields__),
            {"considered", "due", "scored", "unresolved",
             "skipped_already_resolved", "deferred_not_eligible"},
        )

    def test_shared_climatology_across_calls_matches_uninterrupted(self):
        # harden round 1, M3-01 BLOCKER: one resolver + one climatology, TWO
        # resolve_due calls where call 1 already scores — the re-seed in call 2
        # must NOT double-count call 1's outcomes.
        bars = [
            _bar("14:00", "2026-06-15T13:59:59.000000Z", "100.000000"),
            _bar("14:05", "2026-06-15T14:04:59.000000Z", "101.000000"),
            _bar("14:10", "2026-06-15T14:09:59.000000Z", "100.000000",
                 start_hhmm="14:09"),
        ]
        rows = [_decision_row("f-1"),
                _decision_row("f-2", t0="14:05", th="14:10")]
        with TemporaryDirectory() as tmpdir:
            clim_a = AsOfClimatology(min_samples=1)
            self._resolve(tmpdir, bars, rows, climatology=clim_a)
            uninterrupted = clim_a.rate(symbol="AAPL", horizon="5m")
        with TemporaryDirectory() as tmpdir:
            env = _LedgerEnv(tmpdir)
            clim_b = AsOfClimatology(min_samples=1)
            resolver = ForecastResolver(reader=MidBarSeriesReader(bars),
                                        ledger=env.ledger,
                                        scored_stream_path=env.path,
                                        climatology=clim_b)
            # call 1 scores f-1 (f-2 not yet due); call 2 scores f-2.
            resolver.resolve_due(rows, now_utc="2026-06-15T14:06:00.000000Z")
            resolver.resolve_due(rows, now_utc="2026-06-15T15:00:00.000000Z")
            self.assertEqual(clim_b.rate(symbol="AAPL", horizon="5m"), uninterrupted)


class TestLedgerValidation(unittest.TestCase):
    def test_rejects_bad_reason_outcome_and_future_receipt(self):
        with TemporaryDirectory() as tmpdir:
            env = _LedgerEnv(tmpdir)
            with self.assertRaises(ValueError):
                env.ledger.record_unresolved(
                    decision_id="d", forecast_id="f", reason="made_up",
                    missing_bar_reason=None, event_start_bar_key="k1",
                    resolve_bar_key="k2", resolved_as_of_utc="t", data_pin=PIN)
            with self.assertRaises(ValueError):
                env.ledger.record_unresolved(
                    decision_id="d", forecast_id="f", reason="no_mid_bar_t0",
                    missing_bar_reason="future_receipt",  # deferral must NEVER persist
                    event_start_bar_key="k1", resolve_bar_key="k2",
                    resolved_as_of_utc="t", data_pin=PIN)
            with self.assertRaises(ValueError):
                env.ledger.record_scored(
                    decision_id="d", forecast_id="f", outcome=2,
                    brier_i=Decimal("0.1"), mid_t0=Decimal("1"), mid_th=Decimal("1"),
                    event_start_bar_key="k1", resolve_bar_key="k2",
                    resolved_as_of_utc="t", label_provenance={}, data_pin=PIN)


class TestClimatology(unittest.TestCase):
    def test_below_min_samples_degrades_to_constant_half(self):
        clim = AsOfClimatology(min_samples=3)
        clim.ingest_resolved(symbol="AAPL", horizon="5m", outcome=1, forecast_id="f-1")
        clim.ingest_resolved(symbol="AAPL", horizon="5m", outcome=0, forecast_id="f-2")
        p, fid, n = clim.rate(symbol="AAPL", horizon="5m")
        self.assertEqual((p, fid, n), (Decimal("0.500000"), "constant_0.5", 2))

    def test_as_of_rate_excludes_future_ingests(self):
        clim = AsOfClimatology(min_samples=2)
        clim.ingest_resolved(symbol="AAPL", horizon="5m", outcome=1, forecast_id="f-1")
        clim.ingest_resolved(symbol="AAPL", horizon="5m", outcome=1, forecast_id="f-2")
        before = clim.rate(symbol="AAPL", horizon="5m")
        self.assertEqual(before, (Decimal("1.000000"), "climatology_asof_v1", 2))
        clim.ingest_resolved(symbol="AAPL", horizon="5m", outcome=0, forecast_id="f-3")
        after = clim.rate(symbol="AAPL", horizon="5m")
        self.assertEqual(after[0], Decimal("0.666667"))
        self.assertEqual(after[2], 3)

    def test_cells_are_per_symbol_horizon(self):
        clim = AsOfClimatology(min_samples=1)
        clim.ingest_resolved(symbol="AAPL", horizon="5m", outcome=1, forecast_id="f-1")
        self.assertEqual(clim.rate(symbol="AAPL", horizon="30m")[1], "constant_0.5")
        self.assertEqual(clim.rate(symbol="MSFT", horizon="5m")[1], "constant_0.5")

    def test_ingest_is_idempotent_per_forecast_id(self):
        # harden round 1, M3-01: a repeated forecast_id is a silent no-op.
        clim = AsOfClimatology(min_samples=1)
        clim.ingest_resolved(symbol="AAPL", horizon="5m", outcome=1, forecast_id="f-1")
        clim.ingest_resolved(symbol="AAPL", horizon="5m", outcome=1, forecast_id="f-1")
        clim.ingest_resolved(symbol="AAPL", horizon="5m", outcome=0, forecast_id="f-1")
        self.assertEqual(clim.rate(symbol="AAPL", horizon="5m"),
                         (Decimal("1.000000"), "climatology_asof_v1", 1))

    def test_missing_forecast_id_raises(self):
        clim = AsOfClimatology(min_samples=1)
        with self.assertRaises(ValueError):
            clim.ingest_resolved(symbol="AAPL", horizon="5m", outcome=1, forecast_id="")


def murphy_samples_v1():
    """40 samples, at most ONE distinct p per occupied bin (rev2 MATH-Q1)."""
    samples = []
    samples += [(Decimal("0.050000"), 0)] * 18 + [(Decimal("0.050000"), 1)] * 2   # bin 0
    samples += [(Decimal("0.550000"), 1)] * 6 + [(Decimal("0.550000"), 0)] * 4    # bin 5
    samples += [(Decimal("0.950000"), 1)] * 9 + [(Decimal("0.950000"), 0)] * 1    # bin 9
    return samples


class TestScoringMath(unittest.TestCase):
    def test_brier_mean(self):
        samples = [(Decimal("0.600000"), 1), (Decimal("0.600000"), 0)]
        # (0.16 + 0.36)/2 = 0.26
        self.assertEqual(brier(samples), Decimal("0.26000000"))

    def test_murphy_identity_on_constant_p_bins(self):
        result = murphy_decomposition(murphy_samples_v1(), bins=10)
        self.assertEqual(set(result), {"brier", "reliability", "resolution",
                                       "uncertainty", "base_rate", "n"})
        lhs = result["brier"]
        rhs = result["reliability"] - result["resolution"] + result["uncertainty"]
        self.assertLessEqual(abs(lhs - rhs), Decimal("0.000001"))
        self.assertEqual(result["n"], 40)

    def test_murphy_hand_computed(self):
        # bin 0: n=20, p̄=0.05, ō=0.1; bin 5: n=10, p̄=0.55, ō=0.6;
        # bin 9: n=10, p̄=0.95, ō=0.9; ō_total = (2+6+9)/40 = 0.425
        result = murphy_decomposition(murphy_samples_v1(), bins=10)
        # REL = (20*0.0025 + 10*0.0025 + 10*0.0025)/40 = 0.1/40 = 0.0025
        self.assertEqual(result["reliability"], Decimal("0.00250000"))
        # RES = (20*(0.1-0.425)^2 + 10*(0.6-0.425)^2 + 10*(0.9-0.425)^2)/40
        #     = (2.1125 + 0.30625 + 2.25625)/40 = 0.116875
        self.assertEqual(result["resolution"], Decimal("0.11687500"))
        # identity (exact here): BS = 0.0025 - 0.116875 + 0.244375 = 0.13
        self.assertEqual(result["brier"], Decimal("0.13000000"))
        # UNC = 0.425*0.575 = 0.244375
        self.assertEqual(result["uncertainty"], Decimal("0.24437500"))
        self.assertEqual(result["base_rate"], Decimal("0.42500000"))

    def test_reliability_bins_edges_and_thin(self):
        samples = murphy_samples_v1()
        bins = reliability_bins(samples, bins=10)
        self.assertEqual(len(bins), 10)
        self.assertEqual(bins[0]["count"], 20)
        self.assertEqual(bins[5]["count"], 10)
        self.assertEqual(bins[9]["count"], 10)
        empty = bins[3]
        self.assertEqual(empty["count"], 0)
        self.assertIsNone(empty["mean_forecast_p"])
        self.assertIsNone(empty["observed_freq"])
        self.assertFalse(empty["thin"])
        self.assertTrue(bins[5]["thin"])  # 0 < 10 < 30

    def test_bin_index_boundaries(self):
        # p=0.95 and p=1-quantum land in bin 9; p=0.0 in bin 0 (direct construction).
        bins = reliability_bins([(Decimal("0.999999"), 1), (Decimal("0.950000"), 1),
                                 (Decimal("0.000000"), 0)], bins=10)
        self.assertEqual(bins[9]["count"], 2)
        self.assertEqual(bins[0]["count"], 1)

    def test_bss_formula_and_zero_reference(self):
        self.assertEqual(brier_skill_score(Decimal("0.10"), Decimal("0.25")),
                         Decimal("0.60000000"))
        self.assertEqual(brier_skill_score(Decimal("0.10"), Decimal("0")),
                         "unavailable:zero_reference_brier")

    def test_persisted_arithmetic_immune_to_ambient_decimal_context(self):
        # harden round 1, M3-R4: a caller-shrunk global context must not change
        # persisted bytes (or make 8/12-dp quantize raise).
        import decimal

        samples = murphy_samples_v1()
        baseline_brier = brier(samples)
        baseline_murphy = murphy_decomposition(samples, bins=10)
        baseline_bss = brier_skill_score(Decimal("0.10"), Decimal("0.30"))
        clim = AsOfClimatology(min_samples=1)
        for i in range(3):
            clim.ingest_resolved(symbol="AAPL", horizon="5m",
                                 outcome=1 if i < 2 else 0, forecast_id=f"f-{i}")
        baseline_rate = clim.rate(symbol="AAPL", horizon="5m")

        original = decimal.getcontext()
        hostile = decimal.Context(prec=4, rounding=decimal.ROUND_DOWN)
        try:
            decimal.setcontext(hostile)
            self.assertEqual(brier(samples), baseline_brier)
            self.assertEqual(murphy_decomposition(samples, bins=10), baseline_murphy)
            self.assertEqual(brier_skill_score(Decimal("0.10"), Decimal("0.30")),
                             baseline_bss)
            self.assertEqual(clim.rate(symbol="AAPL", horizon="5m"), baseline_rate)
        finally:
            decimal.setcontext(original)


if __name__ == "__main__":
    unittest.main()
