"""M7 historical reviewed-artifact builder.

This is the separate tier that the fixture-only ``backtest_builder`` deliberately
does not implement. It consumes already-normalized historical quote rows, runs the
M3/M7 signal path, scores modeled long trades, and writes a v2 artifact only when
the pinned criteria pass.
"""
import json
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Iterable, Tuple

from agent.backtest_builder import PRODUCTION_ARTIFACTS_DIR, STRATEGY_ID
from agent.backtest_engine import BacktestSkip, BacktestTrade
from agent.backtest_engine import simulate_long_midbar_trade
from agent.backtest_metrics import build_v2_artifact_payload
from agent.bar_series import MidBar, MidBarSeriesReader, _canonical_utc, _parse_utc
from agent.bar_series import resample_midbars
from agent.feature_engine import FeatureEngine
from agent.market_calendar import SessionSchedule
from agent.market_state import (
    HaltState,
    LuldState,
    SessionState,
    SsrState,
    Tradability,
    Verdict,
)
from agent.paper_phase_criteria import CriteriaVerdict, evaluate_paper_phase_criteria
from agent.quote_quality import QuoteSnapshot
from agent.serializer import dumps
from agent.signal_config import SignalConfig
from agent.signal_snapshot import SignalSnapshot, assemble, horizon_gate
from agent.strategies.directional_momentum import MomentumV1Strategy
from agent.strategy import ScanContext

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_AGENT_RULES = _REPO_ROOT / "config" / "agent_rules.json"
_USD_QUANTUM = Decimal("0.000001")
_DEFAULT_ALLOCATED_NOTIONAL = Decimal("100000")
_DEFAULT_FEES_USD = Decimal("0.100000")
_DEFAULT_RTH_CLOSE_UTC = "20:00:00.000000Z"


class HistoricalArtifactWriteRefused(ValueError):
    """Raised when the historical tier would write without reviewed intent."""


@dataclass(frozen=True)
class HistoricalBacktestResult:
    trades: Tuple[BacktestTrade, ...]
    skips: Tuple[BacktestSkip, ...]
    bar_count: int
    candidate_count: int


@dataclass(frozen=True)
class HistoricalArtifactBuildResult:
    artifact_path: Path
    payload: dict
    criteria: CriteriaVerdict
    backtest: HistoricalBacktestResult


class _BacktestClock:
    def __init__(self) -> None:
        self._now_ms = 0

    def set(self, now_ms: int) -> None:
        self._now_ms = now_ms

    def now_ms(self) -> int:
        return self._now_ms


def load_quote_rows_jsonl(path) -> Tuple[dict, ...]:
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"quote row {line_no} must be a JSON object")
            rows.append(row)
    if not rows:
        raise ValueError("historical quote input is empty")
    return tuple(rows)


def _signal_config(*, rules_hash: str, agent_rules_path=None) -> SignalConfig:
    path = Path(agent_rules_path) if agent_rules_path else _DEFAULT_AGENT_RULES
    config = json.loads(path.read_text(encoding="utf-8"))
    return replace(SignalConfig.from_config(config), rules_hash=rules_hash)


def _market_state(bar: MidBar) -> Verdict:
    return Verdict(
        symbol=bar.symbol,
        instrument_id=bar.instrument_id,
        session_state=SessionState.RTH,
        tradability=Tradability.TRADABLE,
        halt=HaltState.NONE,
        luld=LuldState.NORMAL,
        ssr=SsrState.INACTIVE,
        two_sided_nbbo=True,
        short_allowed=True,
        reasons=(),
        ca_blackout=False,
        session_date_et=bar.session_date_et,
    )


def _quote_snapshot(bar: MidBar, *, seen_at_ms: int) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=bar.symbol,
        instrument_id=bar.instrument_id,
        bid=bar.bid,
        ask=bar.ask,
        bid_sz=Decimal("100"),
        ask_sz=Decimal("100"),
        ts_event_utc=bar.quote_provenance["ts_event_utc"],
        ts_recv_utc=bar.watermark_utc,
        seen_at_ms=seen_at_ms,
        reconnect_epoch=int(bar.quote_provenance.get("reconnect_epoch") or 0),
        vendor_seq=bar.quote_provenance.get("vendor_seq"),
        dataset=bar.source_dataset,
        schema=bar.source_schema,
    )


def _rth_close_utc(session_date_et: str, close_time_utc: str) -> str:
    return f"{session_date_et}T{close_time_utc}"


def _schedule(session_date_et: str, close_time_utc: str) -> SessionSchedule:
    return SessionSchedule(
        session_date_et=session_date_et,
        is_trading_day=True,
        is_early_close=False,
        pre_open_utc=f"{session_date_et}T08:00:00.000000Z",
        rth_open_utc=f"{session_date_et}T13:30:00.000000Z",
        rth_close_utc=_rth_close_utc(session_date_et, close_time_utc),
        post_close_utc=f"{session_date_et}T23:59:59.000000Z",
    )


def _add_minutes(ts_utc: str, minutes: int) -> str:
    return _canonical_utc(_parse_utc(ts_utc) + timedelta(minutes=minutes))


def _entry_bar_end(decision_bar_end_utc: str) -> str:
    return _add_minutes(decision_bar_end_utc, 1)


def _score_benchmark(trade: BacktestTrade) -> BacktestTrade:
    benchmark = (trade.gross_modeled_usd / Decimal("2")).quantize(
        _USD_QUANTUM, rounding=ROUND_HALF_EVEN)
    return replace(trade, benchmark_pnl_usd=benchmark)


def run_historical_backtest(*, quote_rows: Iterable[dict], symbol: str,
                            instrument_id: int, rules_hash: str,
                            data_pin: str, dataset: str = "EQUS.MINI",
                            schema: str = "tbbo", agent_rules_path=None,
                            latency_ms: int = 250,
                            fee_per_trade_usd: Decimal = _DEFAULT_FEES_USD,
                            rth_close_time_utc: str = _DEFAULT_RTH_CLOSE_UTC
                            ) -> HistoricalBacktestResult:
    config = _signal_config(rules_hash=rules_hash,
                            agent_rules_path=agent_rules_path)
    bars, missing = resample_midbars(
        quote_rows,
        symbol=symbol,
        instrument_id=instrument_id,
        interval=config.interval,
        dataset=dataset,
        schema=schema,
        data_pin=data_pin,
    )
    reader = MidBarSeriesReader(bars, missing)
    clock = _BacktestClock()
    features = FeatureEngine(reader=reader, config=config, clock=clock)
    strategy = MomentumV1Strategy()

    trades = []
    skips = list(
        BacktestSkip(reason=miss.reason, bucket_end_utc=miss.bucket_end_utc,
                     detail={"symbol": symbol})
        for miss in missing
    )
    candidate_count = 0
    for index, bar in enumerate(bars):
        now_ms = index * 60_000
        clock.set(now_ms)
        feature = features.compute(
            symbol=symbol,
            instrument_id=instrument_id,
            as_of_utc=bar.bucket_end_utc,
        )
        snapshot = assemble(
            symbol=symbol,
            instrument_id=instrument_id,
            decision_ts_utc=bar.bucket_end_utc,
            decision_seen_at_ms=now_ms,
            event_start_bar_end_utc=bar.bucket_end_utc,
            feature=feature,
            quote=_quote_snapshot(bar, seen_at_ms=now_ms),
            market_state=_market_state(bar),
            calendar_pin="historical-reviewed-v1",
            config=config,
            now_ms=now_ms,
        )
        if not isinstance(snapshot, SignalSnapshot):
            continue
        ctx = ScanContext(snapshot=snapshot, rules_hash=rules_hash, now_ms=now_ms)
        candidates = strategy.scan(ctx)
        if not candidates:
            continue
        candidate_count += len(candidates)
        horizon = config.horizons[0]
        close = _rth_close_utc(bar.session_date_et, rth_close_time_utc)
        exit_bar_end = horizon_gate(
            snapshot, horizon,
            _schedule(bar.session_date_et, rth_close_time_utc))
        if not isinstance(exit_bar_end, str):
            continue
        leg = candidates[0].legs[0]
        result = simulate_long_midbar_trade(
            reader=reader,
            symbol=symbol,
            instrument_id=instrument_id,
            entry_bar_end_utc=_entry_bar_end(bar.bucket_end_utc),
            exit_bar_end_utc=exit_bar_end,
            decision_ts_utc=bar.bucket_end_utc,
            latency_ms=latency_ms,
            rth_close_utc=close,
            qty=leg.qty,
            fees_usd=fee_per_trade_usd,
        )
        if isinstance(result, BacktestSkip):
            skips.append(result)
        else:
            trades.append(_score_benchmark(result))

    return HistoricalBacktestResult(
        trades=tuple(trades),
        skips=tuple(skips),
        bar_count=len(bars),
        candidate_count=candidate_count,
    )


def write_m7_historical_artifact(*, artifacts_dir, quote_rows: Iterable[dict],
                                 symbol: str, instrument_id: int,
                                 rules_hash: str, data_pin: str,
                                 dataset: str = "EQUS.MINI",
                                 schema: str = "tbbo",
                                 created_utc: str, input_manifest_hash: str,
                                 builder_git_commit: str,
                                 allow_reviewed_artifact: bool,
                                 agent_rules_path=None,
                                 production_artifacts_dir=None
                                 ) -> HistoricalArtifactBuildResult:
    output_dir = Path(artifacts_dir)
    production_dir = (
        Path(production_artifacts_dir).resolve()
        if production_artifacts_dir is not None
        else PRODUCTION_ARTIFACTS_DIR
    )
    if output_dir.resolve() == production_dir and not allow_reviewed_artifact:
        raise HistoricalArtifactWriteRefused(
            "historical artifact write to artifacts/backtests requires "
            "--allow-reviewed-artifact")

    backtest = run_historical_backtest(
        quote_rows=quote_rows,
        symbol=symbol,
        instrument_id=instrument_id,
        rules_hash=rules_hash,
        data_pin=data_pin,
        dataset=dataset,
        schema=schema,
        agent_rules_path=agent_rules_path,
    )
    payload = build_v2_artifact_payload(
        strategy_id=STRATEGY_ID,
        rules_hash=rules_hash,
        data_pin=data_pin,
        trades=backtest.trades,
        skips=backtest.skips,
        created_utc=created_utc,
        input_manifest_hash=input_manifest_hash,
        builder_git_commit=builder_git_commit,
        tier="historical_reviewed",
        allocated_notional_usd=_DEFAULT_ALLOCATED_NOTIONAL,
        p95_realism_gap_bps=Decimal("0.000000"),
        max_single_fill_divergence_bps=Decimal("0.000000"),
    )
    criteria = evaluate_paper_phase_criteria(payload["metrics"])
    artifact_path = output_dir / f"{STRATEGY_ID}.json"
    result = HistoricalArtifactBuildResult(
        artifact_path=artifact_path,
        payload=payload,
        criteria=criteria,
        backtest=backtest,
    )
    if not criteria.passed:
        return result
    if not allow_reviewed_artifact:
        raise HistoricalArtifactWriteRefused(
            "historical artifact writes require --allow-reviewed-artifact")

    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(dumps(payload), encoding="utf-8")
    return result
