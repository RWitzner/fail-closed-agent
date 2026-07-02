"""agent.m7_run_driver — the committed pull→pin→build→validate→stage driver (M7d §10).

Offline: the credentialed pull is exercised only through an injected
``quote_event_source`` (QuoteEvents, the same seam the backfill tests pin); the
calendar comes from the committed cross-checked H2-2026 fixture. Asserts:
  - the staged tree (quotes/*.jsonl, manifest.json, summary.json) is written and
    the on-disk manifest re-validates against the on-disk rows,
  - production ``artifacts/backtests/`` is NEVER touched (stays .gitkeep-only),
  - the summary carries the per-gate table, realism decomposition (p95/p99/max),
    sample audit, breadth, and provenance the M7d packet requires on any NULL,
  - rows outside the pinned per-date session windows are dropped and counted,
  - determinism: identical inputs → identical manifest_hash/data_pin,
  - fail-closed: existing run dir, staging under production, unknown session date.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.backtest_historical import validate_historical_cross_sectional_manifest
from agent.m7_run_driver import StagedRunResult, stage_cross_sectional_run
from agent.market_calendar import FixtureScheduleProvider
from recorder.event import Provenance, QuoteEvent

_REPO_ROOT = Path(__file__).resolve().parents[2]
_H2_FIXTURE = (_REPO_ROOT / "tests" / "fixtures" / "calendar"
               / "xnys_sessions_2026H2_v1.json")

UNIVERSE = ("AAPL", "MSFT", "NVDA", "AMZN", "META",
            "GOOGL", "TSLA", "AVGO", "COST", "NFLX")
INSTRUMENT_IDS = {sym: 1001 + i for i, sym in enumerate(UNIVERSE)}
SESSIONS = ("2026-06-15", "2026-06-16", "2026-06-17")
_HYPOTHESIS = "m7d_horizon_substrate_v0_20260626"
_SELECTION = "Hold the M7c 10-name universe constant; vary holding horizon only."
_WIGGLE = Decimal("0.0010")


def _provider() -> FixtureScheduleProvider:
    fixture = json.loads(_H2_FIXTURE.read_text(encoding="utf-8"))
    return FixtureScheduleProvider(fixture, pin=fixture["pin"])


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _event(symbol, *, minute_dt, mid: Decimal):
    ts_event = minute_dt
    ts_recv = ts_event + timedelta(milliseconds=300)
    prov = Provenance(
        dataset="EQUS.MINI", schema="bbo-1m",
        instrument_id=INSTRUMENT_IDS[symbol], symbol=symbol, vendor_seq=None,
        ts_event_utc=_utc(ts_event), ts_recv_utc=_utc(ts_recv),
        reconnect_epoch=0)
    return QuoteEvent(
        provenance=prov,
        bid_px=(mid - Decimal("0.01")).quantize(Decimal("0.0001")),
        bid_sz=Decimal("100"),
        ask_px=(mid + Decimal("0.01")).quantize(Decimal("0.0001")),
        ask_sz=Decimal("100"))


def _source(*, sessions=SESSIONS, n_minutes=130, extra_events=None):
    """Injected quote_event_source: per-symbol rising mids, higher slope for
    earlier universe names (mirrors the backfill round-trip fixture)."""
    extra = extra_events or {}

    def source(symbol):
        slope = Decimal(len(UNIVERSE) - UNIVERSE.index(symbol)) * Decimal("0.0010")
        events = []
        global_minute = 0
        for session in sessions:
            start = datetime.fromisoformat(f"{session}T13:30:00+00:00")
            for minute in range(n_minutes):
                mid = (Decimal("100.0000") + slope * Decimal(global_minute)
                       + (_WIGGLE if global_minute % 2 else Decimal("0")))
                events.append(_event(symbol,
                                     minute_dt=start + timedelta(minutes=minute),
                                     mid=mid))
                global_minute += 1
        events.extend(extra.get(symbol, ()))
        return events

    return source


def _stage(tmp, *, run_id="t1", source=None, session_dates=SESSIONS,
           horizon="30m", **kwargs):
    return stage_cross_sectional_run(
        run_id=run_id,
        universe=UNIVERSE,
        dataset="EQUS.MINI",
        schema="bbo-1m",
        session_dates=session_dates,
        schedule_provider=_provider(),
        hypothesis_id=_HYPOTHESIS,
        selection_rule=_SELECTION,
        horizon=horizon,
        staging_root=Path(tmp),
        created_utc="2026-07-02T20:00:00.000000Z",
        builder_git_commit="test-commit",
        quote_event_source=source if source is not None else _source(),
        window_rationale="unit-test window",
        **kwargs)


class TestStagedRun(unittest.TestCase):
    def _production_listing(self):
        prod = _REPO_ROOT / "artifacts" / "backtests"
        return sorted(p.name for p in prod.iterdir())

    def test_happy_path_stages_tree_and_never_touches_production(self):
        before = self._production_listing()
        with TemporaryDirectory() as tmp:
            result = _stage(tmp)
            self.assertIsInstance(result, StagedRunResult)
            staged = Path(tmp) / "t1"
            self.assertTrue((staged / "manifest.json").exists())
            self.assertTrue((staged / "summary.json").exists())
            for symbol in UNIVERSE:
                self.assertTrue((staged / "quotes" / f"{symbol}.jsonl").exists())
            # 3 sessions < 20 => criteria fail => NO artifact json staged
            self.assertFalse(result.criteria_passed)
            self.assertFalse(result.production_artifact_written)
            summary = json.loads((staged / "summary.json").read_text())
            self.assertEqual(summary["mode"], "staged_only")
            self.assertFalse(summary["production_artifact_written"])
            self.assertIn("min_sessions", summary["criteria_failures"])
            self.assertEqual(summary["horizon"], "30m")
            self.assertEqual(summary["window"]["session_dates"],
                             list(SESSIONS))
        self.assertEqual(self._production_listing(), before)
        self.assertEqual(before, [".gitkeep"])

    def test_on_disk_manifest_revalidates_against_on_disk_rows(self):
        with TemporaryDirectory() as tmp:
            result = _stage(tmp)
            staged = Path(tmp) / "t1"
            manifest = json.loads((staged / "manifest.json").read_text())
            rows_by_symbol = {}
            for symbol in UNIVERSE:
                lines = (staged / "quotes" / f"{symbol}.jsonl").read_text()
                rows_by_symbol[symbol] = tuple(
                    json.loads(line) for line in lines.splitlines() if line)
            parsed = validate_historical_cross_sectional_manifest(
                manifest, symbol_quote_rows=rows_by_symbol,
                dataset="EQUS.MINI", schema="bbo-1m",
                data_pin=result.data_pin)
            self.assertEqual(parsed.manifest_hash, result.manifest_hash)
            self.assertEqual(parsed.horizon, "30m")
            # pinned sessions came from the calendar provider (not the naive
            # 13:30-20:00 default): 2026-06-15 is EDT so they coincide, but the
            # window map must cover exactly the pinned dates.
            self.assertEqual(sorted(parsed.session_windows), list(SESSIONS))

    def test_summary_carries_packet_diagnostics(self):
        with TemporaryDirectory() as tmp:
            _stage(tmp)
            summary = json.loads(
                (Path(tmp) / "t1" / "summary.json").read_text())
            gates = summary["gate_table"]
            for gate in ("min_sessions", "min_trades", "min_traded_sessions",
                         "profit_factor_min", "max_drawdown_pct_allocated",
                         "worst_day_pct_allocated", "p95_realism_gap_bps_max",
                         "max_single_fill_divergence_bps", "positive_net_pnl",
                         "positive_active_pnl",
                         "positive_equal_weight_long_active_pnl",
                         "avg_trade_bps_positive"):
                self.assertIn(gate, gates)
                self.assertIn("passed", gates[gate])
            decomposition = summary["realism_decomposition"]
            for key in ("gap_bps", "entry_spread_bps", "exit_spread_bps"):
                self.assertIn("p95", decomposition[key])
                self.assertIn("p99", decomposition[key])
                self.assertIn("max", decomposition[key])
            self.assertIn("per_symbol_gap_p95", decomposition)
            audit = summary["sample_audit"]
            self.assertIn("exclusion_reason_counts", audit)
            self.assertIn("entry_hour_histogram_utc", audit)
            self.assertIn("dropped_rows_by_symbol", audit)
            self.assertIn("per_symbol_leg_counts", summary["breadth"])
            self.assertEqual(summary["provenance"]["hypothesis_id"],
                             _HYPOTHESIS)
            self.assertEqual(summary["provenance"]["builder_git_commit"],
                             "test-commit")
            self.assertTrue(summary["rules_hash"])
            self.assertTrue(summary["manifest_hash"])
            self.assertTrue(summary["data_pin"].endswith(
                summary["manifest_hash"]))

    def test_rows_outside_pinned_sessions_are_dropped_and_counted(self):
        # one post-close row (20:30Z) and one row on an UNPINNED date (06-18)
        extra = {"AAPL": (
            _event("AAPL",
                   minute_dt=datetime.fromisoformat(
                       "2026-06-15T20:30:00+00:00"),
                   mid=Decimal("101.0000")),
            _event("AAPL",
                   minute_dt=datetime.fromisoformat(
                       "2026-06-18T14:00:00+00:00"),
                   mid=Decimal("101.0000")),
        )}
        with TemporaryDirectory() as tmp:
            result = _stage(tmp, source=_source(extra_events=extra))
            summary = result.summary
            self.assertEqual(
                summary["sample_audit"]["dropped_rows_by_symbol"]["AAPL"], 2)
            staged_rows = (Path(tmp) / "t1" / "quotes" / "AAPL.jsonl"
                           ).read_text().splitlines()
            dates = {json.loads(line)["ts_event_utc"][:10]
                     for line in staged_rows if line}
            self.assertEqual(dates, set(SESSIONS))

    def test_determinism_same_inputs_same_hashes(self):
        with TemporaryDirectory() as tmp:
            first = _stage(tmp, run_id="a")
            second = _stage(tmp, run_id="b")
            self.assertEqual(first.manifest_hash, second.manifest_hash)
            self.assertEqual(first.data_pin, second.data_pin)

    def test_existing_run_dir_refused(self):
        with TemporaryDirectory() as tmp:
            _stage(tmp, run_id="dup")
            with self.assertRaises(ValueError):
                _stage(tmp, run_id="dup")

    def test_staging_under_production_refused(self):
        prod = _REPO_ROOT / "artifacts" / "backtests"
        with self.assertRaises(ValueError):
            _stage(prod, run_id="evil")
        self.assertFalse((prod / "evil").exists())

    def test_unknown_session_date_fails_closed(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(Exception):
                _stage(tmp, session_dates=("2026-06-15", "2027-01-05"))
            self.assertFalse((Path(tmp) / "t1" / "manifest.json").exists())

    def test_no_databento_import_through_injected_source(self):
        import sys

        with TemporaryDirectory() as tmp:
            _stage(tmp, run_id="pure")
        self.assertNotIn("databento", sys.modules)


if __name__ == "__main__":
    unittest.main()
