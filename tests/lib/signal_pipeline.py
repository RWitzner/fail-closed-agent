"""M3 §K/§M — the deterministic end-to-end signal pipeline harness.

quotes -> mid bars -> features -> probe ticks -> resolver -> report, all on injected
clocks and the committed config. Used by test_calibration_probe, test_calibration_report
and the golden-report fixture (run_id="run-m3-golden-v1", byte-identical regeneration).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agent import config as agent_config
from agent.bar_series import MidBarSeriesReader, resample_midbars
from agent.calibration import AsOfClimatology, ForecastResolver, ScoredLedger
from agent.calibration_report import build_report
from agent.feature_engine import FeatureEngine, FeatureView
from agent.journal import replay
from agent.market_calendar import FixtureScheduleProvider, MarketCalendar
from agent.market_state import (
    HaltState, LuldState, SessionState, SsrState, Tradability, Verdict,
)
from agent.market_state_cache import MarketStateCache
from agent.quote_quality import QuoteSnapshot
from agent.signal_config import SignalConfig
from agent.strategies.calibration_probe import CalibrationProbe, DecisionLedger
from recorder.persistence import EventWriter

from tests.lib.fakes import FakeClock
from tests.lib.signal_fixtures import (
    DATASET, DATA_PIN_V1, SCHEMA, calendar_fixture_pin, load_calendar_fixture,
)

UTC = timezone.utc

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_RUN_ID = "run-m3-golden-v1"
GOLDEN_GENERATED_TS = "2026-06-09T00:00:00.000000Z"
FIXED_WRITER_TS = "2026-06-15T21:00:00+00:00"


def committed_config() -> dict:
    return agent_config.load(REPO_ROOT / "config" / "agent_rules.json")


def _parse_canonical(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).astimezone(UTC)


def _canonical(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class HeldQuoteView:
    """Trivial QuoteView: tests/pipeline set the latest snapshot explicitly."""

    def __init__(self):
        self._latest = {}

    def put(self, snapshot: QuoteSnapshot) -> None:
        self._latest[(snapshot.symbol, snapshot.instrument_id)] = snapshot

    def latest(self, symbol, instrument_id):
        return self._latest.get((symbol, instrument_id))


def tradable_verdict(symbol, instrument_id, session_date_et) -> Verdict:
    return Verdict(
        symbol=symbol, instrument_id=instrument_id,
        session_state=SessionState.RTH, tradability=Tradability.TRADABLE,
        halt=HaltState.NONE, luld=LuldState.NORMAL, ssr=SsrState.INACTIVE,
        two_sided_nbbo=True, short_allowed=True, reasons=(),
        ca_blackout=False, session_date_et=session_date_et,
    )


class SignalPipeline:
    """One run directory, one run_id, one injected ms clock."""

    def __init__(self, *, quote_rows, journal_dir, run_id,
                 symbol="AAPL", instrument_id=1001, config_dict=None,
                 signal_config=None, calendar=None):
        self.symbol = symbol
        self.instrument_id = instrument_id
        config_dict = config_dict if config_dict is not None else committed_config()
        self.config = signal_config or SignalConfig.from_config(config_dict)
        self.clock = FakeClock(start_ms=1_000_000)

        self.bars, self.missing = resample_midbars(
            quote_rows, symbol=symbol, instrument_id=instrument_id,
            interval=self.config.interval, dataset=DATASET, schema=SCHEMA,
            data_pin=DATA_PIN_V1)
        self.reader = MidBarSeriesReader(self.bars, self.missing)

        self.engine = FeatureEngine(reader=self.reader, config=self.config,
                                    clock=self.clock)
        self.feature_view = FeatureView(engine=self.engine, clock=self.clock)
        self.quote_view = HeldQuoteView()
        self.calendar = calendar or MarketCalendar(FixtureScheduleProvider(
            load_calendar_fixture(), pin=calendar_fixture_pin()))
        self.market_state_cache = MarketStateCache(clock=self.clock)

        journal_dir = Path(journal_dir)
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.decisions_path = journal_dir / "decisions.jsonl"
        self.scored_path = journal_dir / "forecast_scored.jsonl"
        writer_clock = lambda: FIXED_WRITER_TS
        self.decision_ledger = DecisionLedger(
            EventWriter(self.decisions_path, run_id, clock=writer_clock),
            rules_hash=self.config.rules_hash)
        self.scored_ledger = ScoredLedger(
            EventWriter(self.scored_path, run_id, clock=writer_clock),
            rules_hash=self.config.rules_hash)
        self.climatology = AsOfClimatology(
            min_samples=self.config.min_reference_samples)
        self.resolver = ForecastResolver(
            reader=self.reader, ledger=self.scored_ledger,
            scored_stream_path=self.scored_path, climatology=self.climatology)
        self.probe = CalibrationProbe(
            config=self.config, calendar=self.calendar,
            market_state_cache=self.market_state_cache,
            feature_view=self.feature_view, quote_view=self.quote_view,
            ledger=self.decision_ledger, climatology=self.climatology,
            run_id=run_id, clock=self.clock)
        self.run_id = run_id

    def tick_on_bar(self, bar_index: int, *, refresh_features=True,
                    verdict=None, quote=None):
        """Drive one probe tick on the completed bar at `bar_index`."""
        bar = self.bars[bar_index]
        decision_dt = _parse_canonical(bar.bucket_end_utc) + timedelta(milliseconds=250)
        decision_ts = _canonical(decision_dt)
        self.clock.advance(1_000)
        now_ms = self.clock.now_ms()
        if refresh_features:
            self.feature_view.refresh(symbol=self.symbol,
                                      instrument_id=self.instrument_id,
                                      as_of_utc=decision_ts)
        if quote is None:
            quote = QuoteSnapshot(
                symbol=self.symbol, instrument_id=self.instrument_id,
                bid=bar.mid - Decimal("0.0100"), ask=bar.mid + Decimal("0.0100"),
                bid_sz=Decimal("300"), ask_sz=Decimal("200"),
                ts_event_utc=bar.bucket_end_utc, ts_recv_utc=bar.bucket_end_utc,
                seen_at_ms=now_ms, reconnect_epoch=0, vendor_seq=None,
                dataset=DATASET, schema=SCHEMA)
        self.quote_view.put(quote)
        session_date_et = self.calendar.session_date_for(decision_ts)
        if verdict is None:
            verdict = tradable_verdict(self.symbol, self.instrument_id, session_date_et)
        self.market_state_cache.put(verdict, now_ms=now_ms)
        return self.probe.on_bar_complete(
            symbol=self.symbol, instrument_id=self.instrument_id,
            event_start_bar_end_utc=bar.bucket_end_utc, decision_ts_utc=decision_ts)

    def resolve(self, *, now_utc):
        return self.resolver.resolve_due(replay(self.decisions_path), now_utc=now_utc)

    def report(self, *, generated_ts_utc=GOLDEN_GENERATED_TS, bins=10):
        return build_report(
            decision_rows=replay(self.decisions_path),
            scored_rows=replay(self.scored_path),
            run_id=self.run_id, rules_hash=self.config.rules_hash,
            generated_ts_utc=generated_ts_utc, bins=bins)


def run_golden_pipeline(journal_dir):
    """The frozen golden mini-run: quotes_session_v1, ticks on bars 50..74,
    resolve at 20:30Z, report with the pinned generated_ts."""
    from tests.lib.signal_fixtures import quotes_session_v1

    pipeline = SignalPipeline(
        quote_rows=quotes_session_v1(), journal_dir=journal_dir,
        run_id=GOLDEN_RUN_ID)
    for index in range(50, len(pipeline.bars)):
        pipeline.tick_on_bar(index)
    pipeline.resolve(now_utc="2026-06-15T20:30:00.000000Z")
    return pipeline.report()
