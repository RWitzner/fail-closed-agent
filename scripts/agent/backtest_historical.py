"""M7 historical reviewed-artifact builder.

This is the separate tier that the fixture-only ``backtest_builder`` deliberately
does not implement. It consumes already-normalized historical quote rows, runs the
M3/M7 signal path, scores modeled long trades, and writes a v2 artifact only when
the pinned criteria pass.
"""
import json
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Iterable, Tuple

from agent.backtest_builder import PRODUCTION_ARTIFACTS_DIR, STRATEGY_ID
from agent.backtest_engine import BacktestSkip, BacktestTrade
from agent.backtest_engine import read_eligible_midbar
from agent.backtest_metrics import build_v2_artifact_payload
from agent.bar_series import MidBar, MidBarSeriesReader, _canonical_utc, _parse_utc
from agent.bar_series import resample_midbars
from agent.exec_reasons import ExecError
from agent.feature_engine import FeatureEngine
from agent.fees import FEE_MODEL_VERSION, fees_for
from agent.market_calendar import SessionSchedule
from agent.market_state import (
    HaltState,
    LuldState,
    SessionState,
    SsrState,
    Tradability,
    Verdict,
)
from agent.order_pricing import marketable_limit_cap
from agent.paper_phase_criteria import CriteriaVerdict, evaluate_paper_phase_criteria
from agent.quote_quality import QuoteSnapshot
from agent.serializer import dumps, row_hash
from agent.signal_config import SignalConfig
from agent.signal_snapshot import SignalSnapshot, assemble, horizon_gate
from agent.strategies.directional_momentum import MomentumV1Strategy
from agent.strategy import ScanContext

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_AGENT_RULES = _REPO_ROOT / "config" / "agent_rules.json"
_USD_QUANTUM = Decimal("0.000001")
_BPS_QUANTUM = Decimal("0.000001")
_DEFAULT_ALLOCATED_NOTIONAL = Decimal("100000")
_DEFAULT_RTH_CLOSE_UTC = "20:00:00.000000Z"
_NORMALIZER_ID = "m7-historical-normalized-quotes-v1"
_PRICING_MODEL_VERSION = "m7-historical-quote-a-b-spread-v1"
_REALISM_GAP_MODEL_VERSION = "historical_quote_model_vs_raw_mid_v1"
_MANIFEST_VERSION = 1


class HistoricalArtifactWriteRefused(ValueError):
    """Raised when the historical tier would write without reviewed intent."""


@dataclass(frozen=True)
class HistoricalBacktestResult:
    trades: Tuple[BacktestTrade, ...]
    skips: Tuple[BacktestSkip, ...]
    bar_count: int
    candidate_count: int
    p95_realism_gap_bps: Decimal
    max_single_fill_divergence_bps: Decimal
    ca_blackout_skip_count: int = 0
    data_quality_skip_count: int = 0


@dataclass(frozen=True)
class HistoricalInputManifest:
    manifest_hash: str
    data_pin: str
    dataset: str
    schema: str
    interval: str
    symbol: str
    instrument_id: int
    calendar_pin: str
    session_windows: Mapping[str, Mapping[str, str]]
    ca_blackout_session_dates_et: frozenset[str]
    latency_budget_ms: int
    slippage_cap_bps: Decimal
    fee_model_version: str
    pricing_model_version: str
    realism_gap_model_version: str
    universe_hypothesis_id: str
    universe_selection_rule: str
    universe_symbols: Tuple[str, ...]


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


def load_input_manifest_json(path) -> dict:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("historical input manifest must be a JSON object")
    return manifest


def _quote_rows_sha256(rows: Tuple[dict, ...]) -> str:
    hasher = hashlib.sha256()
    for row in rows:
        hasher.update(dumps(row).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _manifest_hash(manifest: Mapping[str, object]) -> str:
    body = dict(manifest)
    body.pop("manifest_hash", None)
    return row_hash(body)


def _require_str(manifest: Mapping[str, object], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"input_manifest.{key} must be a non-empty string")
    return value


def _require_int(manifest: Mapping[str, object], key: str) -> int:
    value = manifest.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"input_manifest.{key} must be an int")
    return value


def _parse_manifest_decimal(value, *, path: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{path} must be a Decimal string")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{path} must parse as Decimal")
    if not parsed.is_finite():
        raise ValueError(f"{path} must be finite")
    return parsed


def _expected_data_pin(*, dataset: str, schema: str, interval: str,
                       source_id_prefix: str, manifest_hash: str) -> str:
    return f"{dataset}:{schema}:{interval}:{source_id_prefix}:{manifest_hash}"


def _validate_quote_row_quality(rows: Tuple[dict, ...], *, dataset: str,
                                schema: str, symbol: str,
                                instrument_id: int) -> None:
    matched = 0
    for line_no, row in enumerate(rows, start=1):
        if (row.get("schema") != schema or row.get("symbol") != symbol
                or row.get("instrument_id") != instrument_id):
            continue
        matched += 1
        if row.get("dataset") != dataset:
            raise ValueError(f"quote row {line_no} dataset does not match manifest")
        ts_event = row.get("ts_event_utc")
        ts_recv = row.get("ts_recv_utc")
        if not isinstance(ts_event, str) or not isinstance(ts_recv, str):
            raise ValueError(
                f"quote row {line_no} missing ts_event_utc/ts_recv_utc")
        if _parse_utc(ts_recv) < _parse_utc(ts_event):
            raise ValueError(
                f"quote row {line_no} ts_recv_utc must be >= ts_event_utc")
        for field in ("bid_sz", "ask_sz"):
            value = _parse_manifest_decimal(
                row.get(field), path=f"quote row {line_no}.{field}")
            if value <= 0:
                raise ValueError(
                    f"quote row {line_no}.{field} must be positive")
    if matched == 0:
        raise ValueError("historical quote input has no rows matching manifest")


def _validate_universe_manifest(
        manifest: Mapping[str, object], *, symbol: str
        ) -> tuple[str, str, Tuple[str, ...]]:
    universe = manifest.get("universe")
    if not isinstance(universe, Mapping):
        raise ValueError("input_manifest.universe must be an object")
    hypothesis_id = universe.get("hypothesis_id")
    selection_rule = universe.get("selection_rule")
    raw_symbols = universe.get("symbols")
    if not isinstance(hypothesis_id, str) or not hypothesis_id:
        raise ValueError(
            "input_manifest.universe.hypothesis_id must be a non-empty string")
    if not isinstance(selection_rule, str) or not selection_rule:
        raise ValueError(
            "input_manifest.universe.selection_rule must be a non-empty string")
    if (not isinstance(raw_symbols, list) or not raw_symbols
            or any(not isinstance(item, str) or not item
                   for item in raw_symbols)):
        raise ValueError(
            "input_manifest.universe.symbols must be a non-empty list of strings")
    symbols = tuple(raw_symbols)
    if len(set(symbols)) != len(symbols):
        raise ValueError("input_manifest.universe.symbols must be unique")
    if symbol not in symbols:
        raise ValueError("input_manifest.universe.symbols must include CLI symbol")
    return hypothesis_id, selection_rule, symbols


def validate_historical_input_manifest(
        manifest: Mapping[str, object], *, quote_rows: Tuple[dict, ...],
        symbol: str, instrument_id: int, dataset: str, schema: str,
        data_pin: str) -> HistoricalInputManifest:
    if not isinstance(manifest, Mapping):
        raise ValueError("input_manifest must be a mapping")
    if manifest.get("v") != _MANIFEST_VERSION:
        raise ValueError(f"input_manifest.v must be {_MANIFEST_VERSION}")
    manifest_hash = _require_str(manifest, "manifest_hash")
    recomputed = _manifest_hash(manifest)
    if manifest_hash != recomputed:
        raise ValueError("input_manifest.manifest_hash does not match manifest body")

    manifest_dataset = _require_str(manifest, "dataset")
    manifest_schema = _require_str(manifest, "schema")
    interval = _require_str(manifest, "interval")
    manifest_symbol = _require_str(manifest, "symbol")
    manifest_instrument_id = _require_int(manifest, "instrument_id")
    row_count = _require_int(manifest, "row_count")
    source_id_prefix = _require_str(manifest, "source_id_prefix")

    if manifest_dataset != dataset:
        raise ValueError("input_manifest.dataset does not match CLI dataset")
    if manifest_schema != schema:
        raise ValueError("input_manifest.schema does not match CLI schema")
    if interval != "1m":
        raise ValueError("input_manifest.interval must be '1m'")
    if manifest_symbol != symbol:
        raise ValueError("input_manifest.symbol does not match CLI symbol")
    if manifest_instrument_id != instrument_id:
        raise ValueError("input_manifest.instrument_id does not match CLI instrument")
    if row_count != len(quote_rows):
        raise ValueError("input_manifest.row_count does not match quote rows")
    if source_id_prefix != "historical":
        raise ValueError("input_manifest.source_id_prefix must be 'historical'")

    expected_quote_hash = _require_str(manifest, "quote_rows_sha256")
    actual_quote_hash = _quote_rows_sha256(quote_rows)
    if expected_quote_hash != actual_quote_hash:
        raise ValueError("input_manifest.quote_rows_sha256 does not match rows")
    normalizer_id = _require_str(manifest, "normalizer_id")
    if normalizer_id != _NORMALIZER_ID:
        raise ValueError(f"input_manifest.normalizer_id must be {_NORMALIZER_ID!r}")
    expected_pin = _expected_data_pin(
        dataset=dataset, schema=schema, interval=interval,
        source_id_prefix=source_id_prefix, manifest_hash=manifest_hash)
    if data_pin != expected_pin:
        raise ValueError("data_pin does not match input manifest hash")
    universe_hypothesis_id, universe_selection_rule, universe_symbols = (
        _validate_universe_manifest(manifest, symbol=symbol))

    calendar = manifest.get("calendar")
    if not isinstance(calendar, dict):
        raise ValueError("input_manifest.calendar must be an object")
    calendar_pin = calendar.get("calendar_pin")
    sessions = calendar.get("sessions")
    if not isinstance(calendar_pin, str) or not calendar_pin:
        raise ValueError("input_manifest.calendar.calendar_pin must be a string")
    if not isinstance(sessions, dict) or not sessions:
        raise ValueError("input_manifest.calendar.sessions must be a non-empty object")
    for session_date, window in sessions.items():
        if not isinstance(session_date, str) or not isinstance(window, dict):
            raise ValueError("input_manifest.calendar.sessions entries are invalid")
        for field in ("rth_open_utc", "rth_close_utc"):
            if not isinstance(window.get(field), str):
                raise ValueError(
                    f"input_manifest.calendar.sessions.{session_date}.{field} "
                    "must be a string")
            _parse_utc(window[field])

    corporate_actions = manifest.get("corporate_actions")
    if not isinstance(corporate_actions, dict):
        raise ValueError("input_manifest.corporate_actions must be an object")
    blackouts = corporate_actions.get("blackout_session_dates_et", [])
    if (not isinstance(blackouts, list)
            or any(not isinstance(item, str) for item in blackouts)):
        raise ValueError(
            "input_manifest.corporate_actions.blackout_session_dates_et "
            "must be a list of strings")

    execution = manifest.get("execution")
    if not isinstance(execution, dict):
        raise ValueError("input_manifest.execution must be an object")
    latency = execution.get("latency_budget_ms")
    if isinstance(latency, bool) or not isinstance(latency, int) or latency < 250:
        raise ValueError(
            "input_manifest.execution.latency_budget_ms must be an int >= 250")
    slippage_cap_bps = _parse_manifest_decimal(
        execution.get("slippage_cap_bps"),
        path="input_manifest.execution.slippage_cap_bps")
    if slippage_cap_bps <= 0:
        raise ValueError("input_manifest.execution.slippage_cap_bps must be > 0")
    fee_model_version = execution.get("fee_model_version")
    pricing_model_version = execution.get("pricing_model_version")
    realism_gap_model_version = execution.get("realism_gap_model_version")
    if fee_model_version != FEE_MODEL_VERSION:
        raise ValueError(
            f"input_manifest.execution.fee_model_version must be {FEE_MODEL_VERSION!r}")
    if pricing_model_version != _PRICING_MODEL_VERSION:
        raise ValueError(
            "input_manifest.execution.pricing_model_version must be "
            f"{_PRICING_MODEL_VERSION!r}")
    if realism_gap_model_version != _REALISM_GAP_MODEL_VERSION:
        raise ValueError(
            "input_manifest.execution.realism_gap_model_version must be "
            f"{_REALISM_GAP_MODEL_VERSION!r}")

    _validate_quote_row_quality(
        quote_rows, dataset=dataset, schema=schema, symbol=symbol,
        instrument_id=instrument_id)
    return HistoricalInputManifest(
        manifest_hash=manifest_hash,
        data_pin=data_pin,
        dataset=dataset,
        schema=schema,
        interval=interval,
        symbol=symbol,
        instrument_id=instrument_id,
        calendar_pin=calendar_pin,
        session_windows=sessions,
        ca_blackout_session_dates_et=frozenset(blackouts),
        latency_budget_ms=latency,
        slippage_cap_bps=slippage_cap_bps,
        fee_model_version=fee_model_version,
        pricing_model_version=pricing_model_version,
        realism_gap_model_version=realism_gap_model_version,
        universe_hypothesis_id=universe_hypothesis_id,
        universe_selection_rule=universe_selection_rule,
        universe_symbols=universe_symbols,
    )


def _signal_config(*, rules_hash: str, agent_rules_path=None) -> SignalConfig:
    path = Path(agent_rules_path) if agent_rules_path else _DEFAULT_AGENT_RULES
    config = json.loads(path.read_text(encoding="utf-8"))
    return replace(SignalConfig.from_config(config), rules_hash=rules_hash)


def _market_state(bar: MidBar, manifest: HistoricalInputManifest | None) -> Verdict:
    ca_blackout = (
        manifest is not None
        and bar.session_date_et in manifest.ca_blackout_session_dates_et
    )
    return Verdict(
        symbol=bar.symbol,
        instrument_id=bar.instrument_id,
        session_state=SessionState.RTH,
        tradability=(
            Tradability.NOT_TRADABLE if ca_blackout else Tradability.TRADABLE
        ),
        halt=HaltState.NONE,
        luld=LuldState.NORMAL,
        ssr=SsrState.INACTIVE,
        two_sided_nbbo=True,
        short_allowed=True,
        reasons=("ca_blackout",) if ca_blackout else (),
        ca_blackout=ca_blackout,
        session_date_et=bar.session_date_et,
    )


def _quote_size(bar: MidBar, field: str) -> Decimal:
    if field not in bar.quote_provenance:
        raise ValueError(f"midbar quote provenance missing {field}")
    value = _parse_manifest_decimal(
        bar.quote_provenance[field], path=f"midbar.quote_provenance.{field}")
    if value <= 0:
        raise ValueError(f"midbar.quote_provenance.{field} must be positive")
    return value


def _quote_snapshot(bar: MidBar, *, seen_at_ms: int) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=bar.symbol,
        instrument_id=bar.instrument_id,
        bid=bar.bid,
        ask=bar.ask,
        bid_sz=_quote_size(bar, "bid_sz"),
        ask_sz=_quote_size(bar, "ask_sz"),
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


def _schedule(session_date_et: str, close_time_utc: str,
              manifest: HistoricalInputManifest | None) -> SessionSchedule:
    window = (
        manifest.session_windows.get(session_date_et)
        if manifest is not None else None
    )
    rth_open = (
        window["rth_open_utc"] if window is not None
        else f"{session_date_et}T13:30:00.000000Z"
    )
    rth_close = (
        window["rth_close_utc"] if window is not None
        else _rth_close_utc(session_date_et, close_time_utc)
    )
    return SessionSchedule(
        session_date_et=session_date_et,
        is_trading_day=True,
        is_early_close=False,
        pre_open_utc=f"{session_date_et}T08:00:00.000000Z",
        rth_open_utc=rth_open,
        rth_close_utc=rth_close,
        post_close_utc=f"{session_date_et}T23:59:59.000000Z",
    )


def _add_minutes(ts_utc: str, minutes: int) -> str:
    return _canonical_utc(_parse_utc(ts_utc) + timedelta(minutes=minutes))


def _entry_bar_end(decision_bar_end_utc: str) -> str:
    return _add_minutes(decision_bar_end_utc, 1)


def _skip(reason: str, bucket_end_utc: str, symbol: str,
          detail: Mapping[str, object] | None = None) -> BacktestSkip:
    payload = {"symbol": symbol}
    if detail:
        payload.update(detail)
    return BacktestSkip(reason=reason, bucket_end_utc=bucket_end_utc,
                        detail=payload)


def _is_whole_share(qty: Decimal) -> bool:
    return qty == qty.to_integral_value()


def _quantize_usd(value: Decimal) -> Decimal:
    return value.quantize(_USD_QUANTUM, rounding=ROUND_HALF_EVEN)


def _quantize_bps(value: Decimal) -> Decimal:
    return value.quantize(_BPS_QUANTUM, rounding=ROUND_HALF_EVEN)


def _simulate_historical_long_trade(*, reader: MidBarSeriesReader, symbol: str,
                                    instrument_id: int,
                                    decision_bar_end_utc: str,
                                    entry_bar_end_utc: str,
                                    exit_bar_end_utc: str,
                                    decision_ts_utc: str, latency_ms: int,
                                    rth_close_utc: str, qty: Decimal,
                                    strategy_limit: Decimal | None,
                                    slippage_cap_bps: Decimal
                                    ) -> BacktestTrade | BacktestSkip:
    exit_end = _parse_utc(exit_bar_end_utc)
    if exit_end > _parse_utc(rth_close_utc):
        return _skip("horizon_crosses_close", exit_bar_end_utc, symbol)

    if not isinstance(qty, Decimal) or not qty.is_finite() or qty <= 0:
        raise ValueError("qty must be a positive finite Decimal")
    latency_instant = _parse_utc(decision_ts_utc) + timedelta(milliseconds=latency_ms)
    entry_end = _parse_utc(entry_bar_end_utc)
    if entry_end <= latency_instant:
        return _skip("quote_b_before_latency", entry_bar_end_utc, symbol)

    decision = read_eligible_midbar(
        reader, symbol, instrument_id, decision_bar_end_utc,
        as_of_utc=decision_ts_utc)
    if isinstance(decision, BacktestSkip):
        return decision
    entry = read_eligible_midbar(
        reader, symbol, instrument_id, entry_bar_end_utc,
        as_of_utc=_canonical_utc(entry_end))
    if isinstance(entry, BacktestSkip):
        return entry
    if _parse_utc(entry.watermark_utc) < latency_instant:
        return _skip("quote_b_before_latency", entry_bar_end_utc, symbol)
    exit_bar = read_eligible_midbar(
        reader, symbol, instrument_id, exit_bar_end_utc,
        as_of_utc=exit_bar_end_utc)
    if isinstance(exit_bar, BacktestSkip):
        return exit_bar

    try:
        cap = marketable_limit_cap(
            side="buy",
            quote_a=_quote_snapshot(decision, seen_at_ms=0),
            quote_b=_quote_snapshot(entry, seen_at_ms=0),
            slippage_cap_bps=slippage_cap_bps,
            strategy_limit=strategy_limit,
        )
    except ExecError as exc:
        return _skip("pricing_rejected", entry_bar_end_utc, symbol,
                     {"pricing_error": str(exc)})
    if "latency_lost_edge" in cap.reasons:
        return _skip("latency_lost_edge", entry_bar_end_utc, symbol,
                     {"pricing_reasons": cap.reasons})
    if not cap.marketable or cap.capped_limit is None:
        return _skip("pricing_rejected", entry_bar_end_utc, symbol,
                     {"pricing_reasons": cap.reasons})

    entry_fill = entry.ask
    exit_fill = exit_bar.bid
    gross = _quantize_usd((exit_fill - entry_fill) * qty)
    sell_notional = _quantize_usd(exit_fill * qty)
    fees = fees_for(side="sell", qty=qty, notional=sell_notional).total_usd
    fees_q = _quantize_usd(fees)
    net = _quantize_usd(gross - fees_q)
    benchmark = _quantize_usd((exit_bar.mid - decision.mid) * qty)
    return BacktestTrade(
        symbol=symbol,
        instrument_id=instrument_id,
        qty=qty,
        entry_bar_end_utc=entry.bucket_end_utc,
        exit_bar_end_utc=exit_bar.bucket_end_utc,
        entry_mid=entry.mid,
        exit_mid=exit_bar.mid,
        gross_modeled_usd=gross,
        fees_usd=fees_q,
        net_execution_realistic_pnl_usd=net,
        benchmark_pnl_usd=benchmark,
    )


def _realism_gap_bps(trade: BacktestTrade) -> Decimal:
    notional = trade.entry_mid * trade.qty
    if notional <= 0:
        return Decimal("0.000000")
    raw_mid_gross = _quantize_usd((trade.exit_mid - trade.entry_mid) * trade.qty)
    gap_usd = abs(raw_mid_gross - trade.gross_modeled_usd)
    return _quantize_bps((gap_usd / notional) * Decimal("10000"))


def _gap_summary(trades: Tuple[BacktestTrade, ...]) -> tuple[Decimal, Decimal]:
    if not trades:
        return Decimal("0.000000"), Decimal("0.000000")
    gaps = sorted(_realism_gap_bps(trade) for trade in trades)
    p95_index = max(0, ((len(gaps) * 95 + 99) // 100) - 1)
    return gaps[p95_index], gaps[-1]


def run_historical_backtest(*, quote_rows: Iterable[dict], symbol: str,
                            instrument_id: int, rules_hash: str,
                            data_pin: str, dataset: str = "EQUS.MINI",
                            schema: str = "tbbo", agent_rules_path=None,
                            input_manifest: Mapping[str, object] | None = None,
                            latency_ms: int = 250,
                            slippage_cap_bps: Decimal = Decimal("25"),
                            rth_close_time_utc: str = _DEFAULT_RTH_CLOSE_UTC
                            ) -> HistoricalBacktestResult:
    quote_rows_t = tuple(quote_rows)
    manifest = None
    if input_manifest is not None:
        manifest = validate_historical_input_manifest(
            input_manifest,
            quote_rows=quote_rows_t,
            symbol=symbol,
            instrument_id=instrument_id,
            dataset=dataset,
            schema=schema,
            data_pin=data_pin,
        )
        latency_ms = manifest.latency_budget_ms
        slippage_cap_bps = manifest.slippage_cap_bps
    if isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 250:
        raise ValueError("latency_ms must be an int >= 250")
    if not isinstance(slippage_cap_bps, Decimal) or not slippage_cap_bps.is_finite() or slippage_cap_bps <= 0:
        raise ValueError("slippage_cap_bps must be a positive finite Decimal")

    config = _signal_config(rules_hash=rules_hash,
                            agent_rules_path=agent_rules_path)
    bars, missing = resample_midbars(
        quote_rows_t,
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
    ca_blackout_skip_count = 0
    candidate_count = 0
    for index, bar in enumerate(bars):
        now_ms = index * 60_000
        clock.set(now_ms)
        feature = features.compute(
            symbol=symbol,
            instrument_id=instrument_id,
            as_of_utc=bar.bucket_end_utc,
        )
        market_state = _market_state(bar, manifest)
        if market_state.ca_blackout:
            ca_blackout_skip_count += 1
            skips.append(_skip("ca_blackout", bar.bucket_end_utc, symbol))
            continue
        snapshot = assemble(
            symbol=symbol,
            instrument_id=instrument_id,
            decision_ts_utc=bar.bucket_end_utc,
            decision_seen_at_ms=now_ms,
            event_start_bar_end_utc=bar.bucket_end_utc,
            feature=feature,
            quote=_quote_snapshot(bar, seen_at_ms=now_ms),
            market_state=market_state,
            calendar_pin=(
                manifest.calendar_pin if manifest is not None
                else "historical-reviewed-v1"
            ),
            config=config,
            now_ms=now_ms,
        )
        if not isinstance(snapshot, SignalSnapshot):
            if getattr(snapshot, "stage", None) == "market_state":
                skips.append(_skip(
                    "market_state_not_tradable", bar.bucket_end_utc, symbol,
                    {"reasons": snapshot.reasons}))
            continue
        ctx = ScanContext(snapshot=snapshot, rules_hash=rules_hash, now_ms=now_ms)
        candidates = strategy.scan(ctx)
        if not candidates:
            continue
        candidate_count += len(candidates)
        horizon = config.horizons[0]
        schedule = _schedule(bar.session_date_et, rth_close_time_utc, manifest)
        close = schedule.rth_close_utc
        exit_bar_end = horizon_gate(
            snapshot, horizon, schedule)
        if not isinstance(exit_bar_end, str):
            continue
        leg = candidates[0].legs[0]
        if (
                len(candidates[0].legs) != 1
                or leg.side != "buy"
                or not candidates[0].paper_eligible
                or not _is_whole_share(leg.qty)
        ):
            skips.append(_skip(
                "candidate_invalid", bar.bucket_end_utc, symbol,
                {"strategy_id": candidates[0].strategy_id}))
            continue
        result = _simulate_historical_long_trade(
            reader=reader,
            symbol=symbol,
            instrument_id=instrument_id,
            decision_bar_end_utc=bar.bucket_end_utc,
            entry_bar_end_utc=_entry_bar_end(bar.bucket_end_utc),
            exit_bar_end_utc=exit_bar_end,
            decision_ts_utc=bar.bucket_end_utc,
            latency_ms=latency_ms,
            rth_close_utc=close,
            qty=leg.qty,
            strategy_limit=leg.limit_price,
            slippage_cap_bps=slippage_cap_bps,
        )
        if isinstance(result, BacktestSkip):
            skips.append(result)
        else:
            trades.append(result)

    trades_t = tuple(trades)
    skips_t = tuple(skips)
    p95_gap, max_gap = _gap_summary(trades_t)
    return HistoricalBacktestResult(
        trades=trades_t,
        skips=skips_t,
        bar_count=len(bars),
        candidate_count=candidate_count,
        p95_realism_gap_bps=p95_gap,
        max_single_fill_divergence_bps=max_gap,
        ca_blackout_skip_count=ca_blackout_skip_count,
        data_quality_skip_count=len(skips_t),
    )


def write_m7_historical_artifact(*, artifacts_dir, quote_rows: Iterable[dict],
                                 symbol: str, instrument_id: int,
                                 rules_hash: str, data_pin: str,
                                 dataset: str = "EQUS.MINI",
                                 schema: str = "tbbo",
                                 created_utc: str,
                                 input_manifest: Mapping[str, object],
                                 builder_git_commit: str,
                                 allow_reviewed_artifact: bool,
                                 agent_rules_path=None,
                                 production_artifacts_dir=None
                                 ) -> HistoricalArtifactBuildResult:
    quote_rows_t = tuple(quote_rows)
    manifest = validate_historical_input_manifest(
        input_manifest,
        quote_rows=quote_rows_t,
        symbol=symbol,
        instrument_id=instrument_id,
        dataset=dataset,
        schema=schema,
        data_pin=data_pin,
    )
    output_dir = Path(artifacts_dir)
    production_dir = (
        Path(production_artifacts_dir).resolve()
        if production_artifacts_dir is not None
        else PRODUCTION_ARTIFACTS_DIR
    )
    resolved_output_dir = output_dir.resolve()
    try:
        relative_to_production = resolved_output_dir.relative_to(production_dir)
    except ValueError:
        relative_to_production = None
    if relative_to_production is not None:
        if relative_to_production != Path("."):
            raise HistoricalArtifactWriteRefused(
                "historical artifact writes must target the exact "
                "artifacts/backtests directory, not a nested path")
        if not allow_reviewed_artifact:
            raise HistoricalArtifactWriteRefused(
                "historical artifact write to artifacts/backtests requires "
                "--allow-reviewed-artifact")

    backtest = run_historical_backtest(
        quote_rows=quote_rows_t,
        symbol=symbol,
        instrument_id=instrument_id,
        rules_hash=rules_hash,
        data_pin=data_pin,
        dataset=dataset,
        schema=schema,
        agent_rules_path=agent_rules_path,
        input_manifest=input_manifest,
    )
    payload = build_v2_artifact_payload(
        strategy_id=STRATEGY_ID,
        rules_hash=rules_hash,
        data_pin=data_pin,
        trades=backtest.trades,
        skips=backtest.skips,
        created_utc=created_utc,
        input_manifest_hash=manifest.manifest_hash,
        builder_git_commit=builder_git_commit,
        tier="historical_reviewed",
        allocated_notional_usd=_DEFAULT_ALLOCATED_NOTIONAL,
        p95_realism_gap_bps=backtest.p95_realism_gap_bps,
        max_single_fill_divergence_bps=backtest.max_single_fill_divergence_bps,
        ca_blackout_skips=backtest.ca_blackout_skip_count,
        data_quality_skip_count=backtest.data_quality_skip_count,
        provenance_extra={
            "normalizer_id": _NORMALIZER_ID,
            "calendar_pin": manifest.calendar_pin,
            "fee_model_version": manifest.fee_model_version,
            "pricing_model_version": manifest.pricing_model_version,
            "realism_gap_model_version": manifest.realism_gap_model_version,
            "universe_hypothesis_id": manifest.universe_hypothesis_id,
            "universe_selection_rule": manifest.universe_selection_rule,
            "universe_symbols": list(manifest.universe_symbols),
            "latency_budget_ms": str(manifest.latency_budget_ms),
            "slippage_cap_bps": str(manifest.slippage_cap_bps),
        },
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
