"""M7 historical reviewed-artifact builder.

This is the separate tier that the fixture-only ``backtest_builder`` deliberately
does not implement. It consumes already-normalized historical quote rows, runs the
M3/M7 signal path, scores modeled long trades, and writes a v2 artifact only when
the pinned criteria pass.
"""
import json
import hashlib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Iterable, Mapping as TypingMapping, Sequence, Tuple

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
from agent.strategies.directional_momentum import strategy_for_id
from agent.strategies import relative_strength as _rs
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


@dataclass(frozen=True)
class HistoricalDiagnosticExportResult:
    output_path: Path
    payload: dict
    backtest: HistoricalBacktestResult


@dataclass(frozen=True)
class HistoricalDiagnosticIndexResult:
    output_path: Path
    payload: dict


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


def _market_state_from_blackouts(bar: MidBar,
                                 ca_blackout_dates: "frozenset[str]") -> Verdict:
    ca_blackout = bar.session_date_et in ca_blackout_dates
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


def _market_state(bar: MidBar, manifest: HistoricalInputManifest | None) -> Verdict:
    dates = (manifest.ca_blackout_session_dates_et if manifest is not None
             else frozenset())
    return _market_state_from_blackouts(bar, dates)


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


def _schedule_from_windows(session_date_et: str, close_time_utc: str,
                           session_windows: TypingMapping[str, TypingMapping[str, str]]
                           | None) -> SessionSchedule:
    window = session_windows.get(session_date_et) if session_windows else None
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


def _schedule(session_date_et: str, close_time_utc: str,
              manifest: HistoricalInputManifest | None) -> SessionSchedule:
    windows = manifest.session_windows if manifest is not None else None
    return _schedule_from_windows(session_date_et, close_time_utc, windows)


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


def _decimal_string(value: Decimal, quantum: Decimal = _USD_QUANTUM) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"diagnostic value must be a finite Decimal: {value!r}")
    return str(value.quantize(quantum, rounding=ROUND_HALF_EVEN))


def _optional_decimal_string(value: Decimal | None,
                             quantum: Decimal = _USD_QUANTUM) -> str | None:
    if value is None:
        return None
    return _decimal_string(value, quantum)


def _positive_int(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative int")
    return value


def _spread_bps(bar: MidBar) -> Decimal:
    if bar.mid <= 0:
        return Decimal("0.000000")
    return _quantize_bps(((bar.ask - bar.bid) / bar.mid) * Decimal("10000"))


def _feature_decimal(features: Mapping[str, object], name: str) -> Decimal | None:
    raw = features.get(name)
    if not isinstance(raw, str):
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return value


def _trade_notional(trade: BacktestTrade) -> Decimal:
    return trade.entry_mid * trade.qty


def _trade_bps(trade: BacktestTrade) -> Decimal:
    notional = _trade_notional(trade)
    if notional <= 0:
        return Decimal("0.000000")
    return _quantize_bps(
        (trade.net_execution_realistic_pnl_usd / notional) * Decimal("10000")
    )


def _active_pnl(trade: BacktestTrade) -> Decimal:
    return _quantize_usd(
        trade.net_execution_realistic_pnl_usd - trade.benchmark_pnl_usd
    )


def _avg_decimal(total: Decimal, count: int) -> Decimal:
    if count <= 0:
        return Decimal("0.000000")
    return _quantize_usd(total / Decimal(count))


def _ratio_string(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.000000"
    value = (Decimal(numerator) / Decimal(denominator)).quantize(
        _BPS_QUANTUM, rounding=ROUND_HALF_EVEN)
    return str(value)


def _distribution(values, *, quantum: Decimal = _USD_QUANTUM) -> dict:
    finite = sorted(value for value in values
                    if isinstance(value, Decimal) and value.is_finite())

    def rank(percentile: int) -> str | None:
        if not finite:
            return None
        index = max(0, ((len(finite) * percentile + 99) // 100) - 1)
        return _decimal_string(finite[index], quantum)

    return {
        "count": len(finite),
        "min": rank(0),
        "p25": rank(25),
        "median": rank(50),
        "p75": rank(75),
        "p90": rank(90),
        "p95": rank(95),
        "p99": rank(99),
        "max": rank(100),
    }


def _trade_summary(trades) -> dict:
    trades_t = tuple(trades)
    count = len(trades_t)
    gross = sum((trade.gross_modeled_usd for trade in trades_t), Decimal("0"))
    fees = sum((trade.fees_usd for trade in trades_t), Decimal("0"))
    net = sum(
        (trade.net_execution_realistic_pnl_usd for trade in trades_t),
        Decimal("0"),
    )
    benchmark = sum(
        (trade.benchmark_pnl_usd for trade in trades_t),
        Decimal("0"),
    )
    active = _quantize_usd(net - benchmark)
    wins = sum(1 for trade in trades_t
               if trade.net_execution_realistic_pnl_usd > 0)
    return {
        "trade_count": count,
        "gross_modeled_usd": _decimal_string(gross),
        "fees_usd": _decimal_string(fees),
        "net_execution_realistic_pnl_usd": _decimal_string(net),
        "benchmark_pnl_usd": _decimal_string(benchmark),
        "active_pnl_usd": _decimal_string(active),
        "friction_proxy_usd": _decimal_string(_quantize_usd(benchmark - net)),
        "avg_active_pnl_per_trade": _decimal_string(_avg_decimal(active, count)),
        "avg_net_pnl_per_trade": _decimal_string(_avg_decimal(net, count)),
        "win_rate": _ratio_string(wins, count),
        "net_pnl_distribution": _distribution(
            (trade.net_execution_realistic_pnl_usd for trade in trades_t)),
    }


def _rank_quintile_labels(values) -> list[str | None]:
    pairs = [
        (value, index)
        for index, value in enumerate(values)
        if isinstance(value, Decimal) and value.is_finite()
    ]
    labels: list[str | None] = [None] * len(values)
    pairs.sort(key=lambda item: (item[0], item[1]))
    count = len(pairs)
    if count == 0:
        return labels
    for rank, (_, index) in enumerate(pairs):
        labels[index] = f"q{min(5, (rank * 5) // count + 1)}"
    return labels


def _bucket_summary(trades, labels) -> dict:
    buckets: dict[str, list[BacktestTrade]] = {}
    for trade, label in zip(trades, labels):
        if not label:
            continue
        buckets.setdefault(label, []).append(trade)
    rows = {}
    for label in sorted(buckets):
        bucket_trades = tuple(buckets[label])
        summary = _trade_summary(bucket_trades)
        summary["symbols"] = sorted({trade.symbol for trade in bucket_trades})
        summary["symbol_count"] = len(summary["symbols"])
        rows[label] = summary
    return rows


def _fixed_bps_regime(value: Decimal | None, *,
                      low_bps: Decimal, high_bps: Decimal) -> str | None:
    if not isinstance(value, Decimal) or not value.is_finite():
        return None
    if value <= low_bps:
        return f"lte_{_decimal_string(low_bps, _BPS_QUANTUM)}_bps"
    if value <= high_bps:
        return f"lte_{_decimal_string(high_bps, _BPS_QUANTUM)}_bps"
    return f"gt_{_decimal_string(high_bps, _BPS_QUANTUM)}_bps"


def _entry_utc_hour_bucket(trade: BacktestTrade) -> str:
    return f"entry_utc_hour_{trade.entry_bar_end_utc[11:13]}"


def _diagnostic_buckets(trades) -> dict:
    trades_t = tuple(trades)
    gaps = [_realism_gap_bps(trade) for trade in trades_t]
    entry_spreads = [trade.entry_spread_bps for trade in trades_t]
    vol_values = [trade.decision_realized_vol_21 for trade in trades_t]
    gap_labels = _rank_quintile_labels(gaps)
    spread_labels = _rank_quintile_labels(entry_spreads)
    vol_labels = _rank_quintile_labels(vol_values)
    combined = [
        f"spread_{spread}_gap_{gap}" if spread and gap else None
        for spread, gap in zip(spread_labels, gap_labels)
    ]
    return {
        "gap_quintile": _bucket_summary(trades_t, gap_labels),
        "entry_spread_quintile": _bucket_summary(trades_t, spread_labels),
        "combined_entry_spread_x_gap_quintile": _bucket_summary(
            trades_t, combined),
        "volatility_quintile": _bucket_summary(trades_t, vol_labels),
    }


def _regime_buckets(trades) -> dict:
    trades_t = tuple(trades)
    gap_labels = [
        _fixed_bps_regime(_realism_gap_bps(trade),
                          low_bps=Decimal("5.000000"),
                          high_bps=Decimal("15.000000"))
        for trade in trades_t
    ]
    spread_labels = [
        _fixed_bps_regime(trade.entry_spread_bps,
                          low_bps=Decimal("2.000000"),
                          high_bps=Decimal("5.000000"))
        for trade in trades_t
    ]
    time_labels = [_entry_utc_hour_bucket(trade) for trade in trades_t]
    return {
        "gap_regime": _bucket_summary(trades_t, gap_labels),
        "entry_spread_regime": _bucket_summary(trades_t, spread_labels),
        "entry_utc_hour": _bucket_summary(trades_t, time_labels),
    }


def _diagnostic_trade_row(index: int, trade: BacktestTrade) -> dict:
    return {
        "row_index": index,
        "symbol": trade.symbol,
        "instrument_id": trade.instrument_id,
        "decision_bar_end_utc": trade.decision_bar_end_utc,
        "entry_bar_end_utc": trade.entry_bar_end_utc,
        "exit_bar_end_utc": trade.exit_bar_end_utc,
        "qty": _decimal_string(trade.qty),
        "decision_mid": _optional_decimal_string(trade.decision_mid),
        "entry_mid": _decimal_string(trade.entry_mid),
        "exit_mid": _decimal_string(trade.exit_mid),
        "notional_usd": _decimal_string(_trade_notional(trade)),
        "gross_modeled_usd": _decimal_string(trade.gross_modeled_usd),
        "fees_usd": _decimal_string(trade.fees_usd),
        "net_execution_realistic_pnl_usd": _decimal_string(
            trade.net_execution_realistic_pnl_usd),
        "benchmark_pnl_usd": _decimal_string(trade.benchmark_pnl_usd),
        "active_pnl_usd": _decimal_string(_active_pnl(trade)),
        "trade_bps": _decimal_string(_trade_bps(trade), _BPS_QUANTUM),
        "realism_gap_bps": _decimal_string(
            _realism_gap_bps(trade), _BPS_QUANTUM),
        "decision_spread_bps": _optional_decimal_string(
            trade.decision_spread_bps, _BPS_QUANTUM),
        "entry_spread_bps": _optional_decimal_string(
            trade.entry_spread_bps, _BPS_QUANTUM),
        "exit_spread_bps": _optional_decimal_string(
            trade.exit_spread_bps, _BPS_QUANTUM),
        "adverse_entry_move_bps": _optional_decimal_string(
            trade.adverse_entry_move_bps, _BPS_QUANTUM),
        "decision_realized_vol_21": _optional_decimal_string(
            trade.decision_realized_vol_21, _BPS_QUANTUM),
        "decision_z_ret_21": _optional_decimal_string(
            trade.decision_z_ret_21, _BPS_QUANTUM),
    }


def _diagnostic_skip_row(index: int, skip: BacktestSkip) -> dict:
    detail = {
        key: value for key, value in skip.detail.items()
        if key in {"symbol", "strategy_id", "reasons",
                   "pricing_reasons", "pricing_error"}
    }
    return {
        "row_index": index,
        "symbol": detail.get("symbol"),
        "bucket_end_utc": skip.bucket_end_utc,
        "reason": skip.reason,
        "detail": detail,
    }


def _build_diagnostic_payload(*, backtest: HistoricalBacktestResult,
                              manifest: HistoricalInputManifest,
                              strategy_id: str, rules_hash: str,
                              data_pin: str, created_utc: str,
                              builder_git_commit: str,
                              max_trade_rows: int,
                              max_skip_rows: int) -> dict:
    trades = backtest.trades
    skips = backtest.skips
    skip_counts = Counter(skip.reason for skip in skips)
    notional = sum((_trade_notional(trade) for trade in trades), Decimal("0"))
    trade_summary = _trade_summary(trades)
    avg_trade_bps = (
        _quantize_bps((
            sum((trade.net_execution_realistic_pnl_usd for trade in trades),
                Decimal("0")) / notional
        ) * Decimal("10000"))
        if notional > 0 else Decimal("0.000000")
    )
    return {
        "v": 1,
        "kind": "m7_historical_diagnostic_export",
        "created_utc": created_utc,
        "builder_git_commit": builder_git_commit,
        "strategy_id": strategy_id,
        "rules_hash": rules_hash,
        "data_pin": data_pin,
        "manifest": {
            "manifest_hash": manifest.manifest_hash,
            "dataset": manifest.dataset,
            "schema": manifest.schema,
            "interval": manifest.interval,
            "symbol": manifest.symbol,
            "instrument_id": manifest.instrument_id,
            "calendar_pin": manifest.calendar_pin,
            "universe_hypothesis_id": manifest.universe_hypothesis_id,
            "universe_selection_rule": manifest.universe_selection_rule,
            "universe_symbols": list(manifest.universe_symbols),
            "latency_budget_ms": manifest.latency_budget_ms,
            "slippage_cap_bps": _decimal_string(
                manifest.slippage_cap_bps, _BPS_QUANTUM),
        },
        "summary": {
            "bar_count": backtest.bar_count,
            "candidate_count": backtest.candidate_count,
            "trade_count": len(trades),
            "skip_count": len(skips),
            "gross_modeled_usd": trade_summary["gross_modeled_usd"],
            "fees_usd": trade_summary["fees_usd"],
            "net_execution_realistic_pnl_usd": (
                trade_summary["net_execution_realistic_pnl_usd"]),
            "benchmark_pnl_usd": trade_summary["benchmark_pnl_usd"],
            "active_pnl_usd": trade_summary["active_pnl_usd"],
            "friction_proxy_usd": trade_summary["friction_proxy_usd"],
            "avg_active_pnl_per_trade": (
                trade_summary["avg_active_pnl_per_trade"]),
            "avg_net_pnl_per_trade": trade_summary["avg_net_pnl_per_trade"],
            "win_rate": trade_summary["win_rate"],
            "avg_trade_bps": _decimal_string(avg_trade_bps, _BPS_QUANTUM),
            "p95_realism_gap_bps": _decimal_string(
                backtest.p95_realism_gap_bps, _BPS_QUANTUM),
            "max_single_fill_divergence_bps": _decimal_string(
                backtest.max_single_fill_divergence_bps, _BPS_QUANTUM),
            "ca_blackout_skip_count": backtest.ca_blackout_skip_count,
            "data_quality_skip_count": backtest.data_quality_skip_count,
        },
        "skip_reason_counts": {
            reason: skip_counts[reason] for reason in sorted(skip_counts)
        },
        "skip_reason_percentages": {
            reason: _ratio_string(skip_counts[reason], len(skips))
            for reason in sorted(skip_counts)
        },
        "distributions": {
            "net_pnl_usd": trade_summary["net_pnl_distribution"],
            "active_pnl_usd": _distribution(
                (_active_pnl(trade) for trade in trades)),
            "realism_gap_bps": _distribution(
                (_realism_gap_bps(trade) for trade in trades),
                quantum=_BPS_QUANTUM),
            "decision_spread_bps": _distribution(
                (trade.decision_spread_bps for trade in trades),
                quantum=_BPS_QUANTUM),
            "entry_spread_bps": _distribution(
                (trade.entry_spread_bps for trade in trades),
                quantum=_BPS_QUANTUM),
            "exit_spread_bps": _distribution(
                (trade.exit_spread_bps for trade in trades),
                quantum=_BPS_QUANTUM),
            "adverse_entry_move_bps": _distribution(
                (trade.adverse_entry_move_bps for trade in trades),
                quantum=_BPS_QUANTUM),
            "decision_realized_vol_21": _distribution(
                (trade.decision_realized_vol_21 for trade in trades),
                quantum=_BPS_QUANTUM),
        },
        "friction_buckets": _diagnostic_buckets(trades),
        "time_buckets": {
            "entry_utc_hour": _regime_buckets(trades)["entry_utc_hour"],
        },
        "regime_buckets": {
            key: value
            for key, value in _regime_buckets(trades).items()
            if key != "entry_utc_hour"
        },
        "data_quality": {
            "ca_blackout_skip_count": backtest.ca_blackout_skip_count,
            "data_quality_skip_count": backtest.data_quality_skip_count,
            "candidate_to_trade_rate": _ratio_string(
                len(trades), backtest.candidate_count),
            "unresolved_flags_present": (
                backtest.data_quality_skip_count > 0
                or backtest.ca_blackout_skip_count > 0
            ),
        },
        "trade_rows": [
            _diagnostic_trade_row(index, trade)
            for index, trade in enumerate(trades[:max_trade_rows])
        ],
        "skip_rows": [
            _diagnostic_skip_row(index, skip)
            for index, skip in enumerate(skips[:max_skip_rows])
        ],
        "trade_rows_truncated": len(trades) > max_trade_rows,
        "skip_rows_truncated": len(skips) > max_skip_rows,
        "bounded_export": {
            "max_trade_rows": max_trade_rows,
            "max_skip_rows": max_skip_rows,
            "trade_rows_written": min(len(trades), max_trade_rows),
            "skip_rows_written": min(len(skips), max_skip_rows),
            "trade_rows_truncated": len(trades) > max_trade_rows,
            "skip_rows_truncated": len(skips) > max_skip_rows,
        },
    }


def _parse_diagnostic_decimal(value, *, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{name} must be a Decimal string or null")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{name} must parse as Decimal: {value!r}")
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _require_diagnostic_decimal(value, *, name: str) -> Decimal:
    parsed = _parse_diagnostic_decimal(value, name=name)
    if parsed is None:
        raise ValueError(f"{name} must not be null")
    return parsed


def _load_diagnostic_payload(path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("kind") != "m7_historical_diagnostic_export":
        raise ValueError(f"not an M7 historical diagnostic export: {path}")
    return payload


def _diagnostic_row_to_trade(row: Mapping[str, object]) -> BacktestTrade:
    return BacktestTrade(
        symbol=str(row["symbol"]),
        instrument_id=int(row["instrument_id"]),
        qty=_require_diagnostic_decimal(row["qty"], name="qty"),
        entry_bar_end_utc=str(row["entry_bar_end_utc"]),
        exit_bar_end_utc=str(row["exit_bar_end_utc"]),
        entry_mid=_require_diagnostic_decimal(row["entry_mid"], name="entry_mid"),
        exit_mid=_require_diagnostic_decimal(row["exit_mid"], name="exit_mid"),
        gross_modeled_usd=_require_diagnostic_decimal(
            row["gross_modeled_usd"], name="gross_modeled_usd"),
        fees_usd=_require_diagnostic_decimal(row["fees_usd"], name="fees_usd"),
        net_execution_realistic_pnl_usd=_require_diagnostic_decimal(
            row["net_execution_realistic_pnl_usd"],
            name="net_execution_realistic_pnl_usd"),
        benchmark_pnl_usd=_require_diagnostic_decimal(
            row["benchmark_pnl_usd"], name="benchmark_pnl_usd"),
        decision_bar_end_utc=(
            str(row["decision_bar_end_utc"])
            if row.get("decision_bar_end_utc") is not None else None),
        decision_mid=_parse_diagnostic_decimal(
            row.get("decision_mid"), name="decision_mid"),
        decision_spread_bps=_parse_diagnostic_decimal(
            row.get("decision_spread_bps"), name="decision_spread_bps"),
        entry_spread_bps=_parse_diagnostic_decimal(
            row.get("entry_spread_bps"), name="entry_spread_bps"),
        exit_spread_bps=_parse_diagnostic_decimal(
            row.get("exit_spread_bps"), name="exit_spread_bps"),
        adverse_entry_move_bps=_parse_diagnostic_decimal(
            row.get("adverse_entry_move_bps"), name="adverse_entry_move_bps"),
        decision_realized_vol_21=_parse_diagnostic_decimal(
            row.get("decision_realized_vol_21"), name="decision_realized_vol_21"),
        decision_z_ret_21=_parse_diagnostic_decimal(
            row.get("decision_z_ret_21"), name="decision_z_ret_21"),
    )


def _summary_decimal(summary: Mapping[str, object], key: str) -> Decimal:
    return _require_diagnostic_decimal(summary[key], name=key)


def _aggregate_diagnostic_summaries(payloads) -> dict:
    count = sum(int(payload["summary"]["trade_count"]) for payload in payloads)
    gross = sum((_summary_decimal(payload["summary"], "gross_modeled_usd")
                 for payload in payloads), Decimal("0"))
    fees = sum((_summary_decimal(payload["summary"], "fees_usd")
                for payload in payloads), Decimal("0"))
    net = sum((_summary_decimal(
        payload["summary"], "net_execution_realistic_pnl_usd")
               for payload in payloads), Decimal("0"))
    benchmark = sum((_summary_decimal(payload["summary"], "benchmark_pnl_usd")
                     for payload in payloads), Decimal("0"))
    active = _quantize_usd(net - benchmark)
    return {
        "symbol_count": len(payloads),
        "bar_count": sum(int(payload["summary"]["bar_count"])
                         for payload in payloads),
        "candidate_count": sum(int(payload["summary"]["candidate_count"])
                               for payload in payloads),
        "trade_count": count,
        "skip_count": sum(int(payload["summary"]["skip_count"])
                          for payload in payloads),
        "gross_modeled_usd": _decimal_string(gross),
        "fees_usd": _decimal_string(fees),
        "net_execution_realistic_pnl_usd": _decimal_string(net),
        "benchmark_pnl_usd": _decimal_string(benchmark),
        "active_pnl_usd": _decimal_string(active),
        "friction_proxy_usd": _decimal_string(_quantize_usd(benchmark - net)),
        "avg_active_pnl_per_trade": _decimal_string(_avg_decimal(active, count)),
        "avg_net_pnl_per_trade": _decimal_string(_avg_decimal(net, count)),
    }


def _share(value: Decimal, total: Decimal) -> str:
    if total == 0:
        return "0.000000"
    return str((value / total).quantize(_BPS_QUANTUM, rounding=ROUND_HALF_EVEN))


def _symbol_concentration(payloads, trades) -> dict:
    symbols = []
    total_trades = sum(int(payload["summary"]["trade_count"])
                       for payload in payloads)
    total_notional = sum((_trade_notional(trade) for trade in trades),
                         Decimal("0"))
    total_abs_active = sum((
        abs(_summary_decimal(payload["summary"], "active_pnl_usd"))
        for payload in payloads
    ), Decimal("0"))
    total_abs_net = sum((
        abs(_summary_decimal(payload["summary"],
                             "net_execution_realistic_pnl_usd"))
        for payload in payloads
    ), Decimal("0"))
    trade_shares = []
    notional_by_symbol = {}
    for trade in trades:
        notional_by_symbol[trade.symbol] = (
            notional_by_symbol.get(trade.symbol, Decimal("0"))
            + _trade_notional(trade)
        )
    for payload in payloads:
        symbol = payload["manifest"]["symbol"]
        summary = payload["summary"]
        trade_count = int(summary["trade_count"])
        active = _summary_decimal(summary, "active_pnl_usd")
        net = _summary_decimal(summary, "net_execution_realistic_pnl_usd")
        trade_share = (
            Decimal(trade_count) / Decimal(total_trades)
            if total_trades else Decimal("0")
        )
        trade_shares.append(trade_share)
        symbols.append({
            "symbol": symbol,
            "trade_count": trade_count,
            "trade_share": _share(Decimal(trade_count), Decimal(total_trades)),
            "gross_exposure_share": _share(
                notional_by_symbol.get(symbol, Decimal("0")), total_notional),
            "active_pnl_abs_share": _share(abs(active), total_abs_active),
            "net_pnl_abs_share": _share(abs(net), total_abs_net),
            "active_pnl_usd": _decimal_string(active),
            "net_execution_realistic_pnl_usd": _decimal_string(net),
        })
    sorted_trade_shares = sorted(trade_shares, reverse=True)
    herfindahl = sum((share * share for share in trade_shares), Decimal("0"))
    top_1 = sorted_trade_shares[0] if sorted_trade_shares else Decimal("0")
    top_2 = sum(sorted_trade_shares[:2], Decimal("0"))
    return {
        "symbols": sorted(symbols, key=lambda row: row["symbol"]),
        "top_1_symbol_trade_share": _decimal_string(top_1, _BPS_QUANTUM),
        "top_2_symbol_trade_share": _decimal_string(top_2, _BPS_QUANTUM),
        "trade_share_herfindahl": _decimal_string(herfindahl, _BPS_QUANTUM),
        "symbols_with_at_least_2_trades": sum(
            1 for payload in payloads
            if int(payload["summary"]["trade_count"]) >= 2),
        "symbols_with_positive_active_pnl": sum(
            1 for payload in payloads
            if _summary_decimal(payload["summary"], "active_pnl_usd") > 0),
        "symbols_with_positive_net_pnl": sum(
            1 for payload in payloads
            if _summary_decimal(
                payload["summary"], "net_execution_realistic_pnl_usd") > 0),
    }


def _diagnostic_reconciliation(payloads, source_summary_path) -> dict:
    if source_summary_path is None:
        return {"status": "not_checked", "reason": "no_source_summary_path"}
    source_path = Path(source_summary_path)
    source = json.loads(source_path.read_text(encoding="utf-8"))
    strategy_id = payloads[0]["strategy_id"] if payloads else None
    if source.get("strategy_id") != strategy_id:
        return {
            "status": "not_checked_strategy_mismatch",
            "source_summary_path": str(source_path),
            "source_strategy_id": source.get("strategy_id"),
            "diagnostic_strategy_id": strategy_id,
        }
    source_symbols = source.get("symbols")
    if not isinstance(source_symbols, dict):
        return {
            "status": "failed",
            "source_summary_path": str(source_path),
            "reason": "source summary has no symbol map",
        }
    mismatches = []
    for payload in payloads:
        symbol = payload["manifest"]["symbol"]
        source_symbol = source_symbols.get(symbol)
        if not isinstance(source_symbol, dict):
            mismatches.append({"symbol": symbol, "reason": "missing_source_symbol"})
            continue
        source_metrics = source_symbol.get("metrics", {})
        checks = {
            "trade_count": (
                int(source_symbol.get("trade_count", -1))
                == int(payload["summary"]["trade_count"])),
            "net_execution_realistic_pnl_usd": (
                source_metrics.get("net_execution_realistic_pnl_usd")
                == payload["summary"]["net_execution_realistic_pnl_usd"]),
            "active_pnl_usd": (
                source_metrics.get("active_pnl_usd")
                == payload["summary"]["active_pnl_usd"]),
        }
        failed = [key for key, ok in checks.items() if not ok]
        if failed:
            mismatches.append({"symbol": symbol, "failed": failed})
    return {
        "status": "ok" if not mismatches else "failed",
        "source_summary_path": str(source_path),
        "mismatches": mismatches,
    }


def _diagnostic_index_payload(*, payloads, diagnostic_paths, created_utc: str,
                              source_run_id: str | None,
                              source_summary_path: str | None,
                              sample_window_id: str | None,
                              holdout_window_id: str | None) -> dict:
    if not payloads:
        raise ValueError("at least one diagnostic payload is required")
    strategy_ids = sorted({payload["strategy_id"] for payload in payloads})
    rules_hashes = sorted({payload["rules_hash"] for payload in payloads})
    if len(strategy_ids) != 1:
        raise ValueError(f"diagnostic payloads disagree on strategy_id: {strategy_ids}")
    if len(rules_hashes) != 1:
        raise ValueError(f"diagnostic payloads disagree on rules_hash: {rules_hashes}")
    trades = tuple(
        _diagnostic_row_to_trade(row)
        for payload in payloads
        for row in payload.get("trade_rows", ())
    )
    skip_counts = Counter()
    for payload in payloads:
        skip_counts.update(payload.get("skip_reason_counts", {}))
    truncation = {
        "any_trade_rows_truncated": any(
            payload.get("trade_rows_truncated") for payload in payloads),
        "any_skip_rows_truncated": any(
            payload.get("skip_rows_truncated") for payload in payloads),
        "trade_rows_available": len(trades),
        "skip_rows_available": sum(
            len(payload.get("skip_rows", ())) for payload in payloads),
    }
    regimes = _regime_buckets(trades)
    return {
        "v": 1,
        "kind": "m7_historical_diagnostic_index",
        "created_utc": created_utc,
        "strategy_id": strategy_ids[0],
        "rules_hash": rules_hashes[0],
        "source_run_id": source_run_id,
        "source_summary_path": source_summary_path,
        "sample_window_id": sample_window_id,
        "holdout_window_id": holdout_window_id,
        "source_universe": list(payloads[0]["manifest"]["universe_symbols"]),
        "diagnostic_paths": [str(path) for path in diagnostic_paths],
        "data_pins": {
            payload["manifest"]["symbol"]: payload["data_pin"]
            for payload in payloads
        },
        "manifest_hashes": {
            payload["manifest"]["symbol"]: payload["manifest"]["manifest_hash"]
            for payload in payloads
        },
        "bounded_export": truncation,
        "reconciliation": _diagnostic_reconciliation(
            payloads, source_summary_path),
        "aggregate": _aggregate_diagnostic_summaries(payloads),
        "row_based_distribution_coverage": (
            "complete" if not truncation["any_trade_rows_truncated"]
            else "bounded_sample"),
        "distributions": {
            "net_pnl_usd": _distribution(
                (trade.net_execution_realistic_pnl_usd for trade in trades)),
            "active_pnl_usd": _distribution(
                (_active_pnl(trade) for trade in trades)),
            "realism_gap_bps": _distribution(
                (_realism_gap_bps(trade) for trade in trades),
                quantum=_BPS_QUANTUM),
            "entry_spread_bps": _distribution(
                (trade.entry_spread_bps for trade in trades),
                quantum=_BPS_QUANTUM),
            "decision_realized_vol_21": _distribution(
                (trade.decision_realized_vol_21 for trade in trades),
                quantum=_BPS_QUANTUM),
        },
        "friction_buckets": _diagnostic_buckets(trades),
        "time_buckets": {"entry_utc_hour": regimes["entry_utc_hour"]},
        "regime_buckets": {
            key: value for key, value in regimes.items()
            if key != "entry_utc_hour"
        },
        "skip_reason_counts": {
            reason: skip_counts[reason] for reason in sorted(skip_counts)
        },
        "concentration": _symbol_concentration(payloads, trades),
        "symbols": [
            {
                "symbol": payload["manifest"]["symbol"],
                "diagnostics_path": str(path),
                **payload["summary"],
                "trade_rows_truncated": payload.get("trade_rows_truncated"),
                "skip_rows_truncated": payload.get("skip_rows_truncated"),
            }
            for payload, path in zip(payloads, diagnostic_paths)
        ],
    }


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
        decision_bar_end_utc=decision.bucket_end_utc,
        entry_bar_end_utc=entry.bucket_end_utc,
        exit_bar_end_utc=exit_bar.bucket_end_utc,
        decision_mid=decision.mid,
        entry_mid=entry.mid,
        exit_mid=exit_bar.mid,
        gross_modeled_usd=gross,
        fees_usd=fees_q,
        net_execution_realistic_pnl_usd=net,
        benchmark_pnl_usd=benchmark,
        decision_spread_bps=_spread_bps(decision),
        entry_spread_bps=_spread_bps(entry),
        exit_spread_bps=_spread_bps(exit_bar),
        adverse_entry_move_bps=cap.adverse_move_bps,
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
                            strategy_id: str = STRATEGY_ID,
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
    strategy = strategy_for_id(strategy_id)

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
            trades.append(replace(
                result,
                decision_realized_vol_21=_feature_decimal(
                    feature.features, "realized_vol_21"),
                decision_z_ret_21=_feature_decimal(
                    feature.features, "z_ret_21"),
            ))

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


@dataclass(frozen=True)
class HistoricalCrossSectionalResult:
    """Aggregated result of the M7c phase-1 multi-symbol, same-timestamp harness.

    Every selected long leg across the universe aggregates into ``trades``/``skips``
    so the two held positions score under one ``(strategy_id, rules_hash, data_pin)``
    artifact (FD-P1-10). The ``universe_equal_weight_long_v1`` attribution is carried
    alongside as additional metric/provenance (FD-P1-9); the pinned verifier benchmark
    (``exposure_matched_midbar_v1``, via ``trade.benchmark_pnl_usd``) is unchanged.
    """
    trades: Tuple[BacktestTrade, ...]
    skips: Tuple[BacktestSkip, ...]
    bar_count: int
    candidate_count: int
    p95_realism_gap_bps: Decimal
    max_single_fill_divergence_bps: Decimal
    ca_blackout_skip_count: int
    data_quality_skip_count: int
    decision_count: int
    acting_decision_count: int
    insufficient_valid_decision_count: int
    overlap_suppressed_leg_count: int
    exclusion_reason_counts: TypingMapping[str, int]
    per_symbol_leg_counts: TypingMapping[str, int]
    benchmark_leg_fill_count: int
    benchmark_leg_skip_count: int
    equal_weight_long_benchmark_net_usd: Decimal
    equal_weight_long_active_pnl_usd: Decimal


def run_historical_cross_sectional_backtest(
        *, symbol_quote_rows: TypingMapping[str, Iterable[dict]],
        universe: Sequence[str], instrument_ids: TypingMapping[str, int],
        rules_hash: str, symbol_data_pins: TypingMapping[str, str],
        dataset: str = "EQUS.MINI", schema: str = "tbbo", agent_rules_path=None,
        strategy_id: str = _rs.STRATEGY_ID, latency_ms: int = 250,
        slippage_cap_bps: Decimal = Decimal("25"), horizon: str = "30m",
        rth_close_time_utc: str = _DEFAULT_RTH_CLOSE_UTC,
        session_windows: TypingMapping[str, TypingMapping[str, str]] | None = None,
        ca_blackout_session_dates_et: "frozenset[str]" = frozenset(),
        ) -> HistoricalCrossSectionalResult:
    """M7c phase-1 cross-sectional decision harness (research packet step 2).

    For each aligned decision instant it assembles every universe symbol's
    point-in-time ``SignalSnapshot``, ranks the valid decision set with the proxy,
    selects the top ``TOP_N`` long names, and reuses ``_simulate_historical_long_trade``
    per leg. The single-leg fill/exit machinery is unchanged; the only new logic is the
    same-timestamp assembly + ranking + no-overlap selection + the equal-weight-long
    benchmark. It writes nothing and imports no broker/preflight surface.
    """
    if strategy_id != _rs.STRATEGY_ID:
        raise ValueError(
            f"cross-sectional runner only supports {_rs.STRATEGY_ID!r}, "
            f"got {strategy_id!r}")
    if (isinstance(latency_ms, bool) or not isinstance(latency_ms, int)
            or latency_ms < 250):
        raise ValueError("latency_ms must be an int >= 250")
    if (not isinstance(slippage_cap_bps, Decimal) or not slippage_cap_bps.is_finite()
            or slippage_cap_bps <= 0):
        raise ValueError("slippage_cap_bps must be a positive finite Decimal")
    universe_t = tuple(universe)
    if len(set(universe_t)) != len(universe_t):
        raise ValueError("universe must not contain duplicate symbols")
    missing_meta = [
        sym for sym in universe_t
        if sym not in instrument_ids or sym not in symbol_data_pins
    ]
    if missing_meta:
        raise ValueError(
            f"instrument_ids/symbol_data_pins missing for {sorted(missing_meta)}")

    config = _signal_config(rules_hash=rules_hash, agent_rules_path=agent_rules_path)
    if horizon not in config.horizons:
        raise ValueError(
            f"horizon {horizon!r} not in configured horizons {config.horizons}")

    proxy = _rs.strategy_for_id(strategy_id)
    clock = _BacktestClock()
    ca_blackout_dates = frozenset(ca_blackout_session_dates_et)

    readers: dict = {}
    engines: dict = {}
    bars_by_end: dict = {}
    total_bar_count = 0
    for symbol in universe_t:
        iid = instrument_ids[symbol]
        rows = tuple(symbol_quote_rows.get(symbol, ()))
        bars, missing = resample_midbars(
            rows, symbol=symbol, instrument_id=iid, interval=config.interval,
            dataset=dataset, schema=schema, data_pin=symbol_data_pins[symbol])
        readers[symbol] = MidBarSeriesReader(bars, missing)
        engines[symbol] = FeatureEngine(reader=readers[symbol], config=config,
                                        clock=clock)
        bars_by_end[symbol] = {bar.bucket_end_utc: bar for bar in bars}
        total_bar_count += len(bars)

    timeline = sorted({
        end for symbol in universe_t for end in bars_by_end[symbol]
    })

    trades: list = []
    skips: list = []
    candidate_count = 0
    acting_decision_count = 0
    insufficient_valid_decision_count = 0
    overlap_suppressed_leg_count = 0
    ca_blackout_skip_count = 0
    exclusion_counts: dict = {}
    per_symbol_leg_counts: dict = {}
    open_until: dict = {}            # symbol -> exit instant of the last opened leg
    benchmark_net = Decimal("0")
    benchmark_leg_fill_count = 0
    benchmark_leg_skip_count = 0

    for index, decision_ts in enumerate(timeline):
        now_ms = index * 60_000
        clock.set(now_ms)
        assembled: list = []         # (symbol, snapshot, feature)
        for symbol in universe_t:
            bar = bars_by_end[symbol].get(decision_ts)
            if bar is None:
                continue
            iid = instrument_ids[symbol]
            market_state = _market_state_from_blackouts(bar, ca_blackout_dates)
            if market_state.ca_blackout:
                ca_blackout_skip_count += 1
                exclusion_counts["ca_blackout"] = (
                    exclusion_counts.get("ca_blackout", 0) + 1)
                continue
            feature = engines[symbol].compute(
                symbol=symbol, instrument_id=iid, as_of_utc=decision_ts)
            snapshot = assemble(
                symbol=symbol, instrument_id=iid, decision_ts_utc=decision_ts,
                decision_seen_at_ms=now_ms, event_start_bar_end_utc=decision_ts,
                feature=feature, quote=_quote_snapshot(bar, seen_at_ms=now_ms),
                market_state=market_state, calendar_pin="historical-reviewed-v1",
                config=config, now_ms=now_ms)
            if not isinstance(snapshot, SignalSnapshot):
                reasons = getattr(snapshot, "reasons", ())
                reason = reasons[0] if reasons else getattr(
                    snapshot, "stage", "excluded")
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                continue
            assembled.append((symbol, snapshot, feature))

        valid_snaps = [snapshot for _, snapshot, _ in assembled]
        feature_by_symbol = {symbol: feat for symbol, _, feat in assembled}
        ranking = _rs.rank_decision_set(valid_snaps, universe_order=universe_t)
        if ranking.valid_count < _rs.MIN_VALID_SYMBOLS:
            insufficient_valid_decision_count += 1
            continue

        # Exit horizon is identical for every leg at this decision (same event-start
        # bar end and the same session schedule). A horizon crossing the RTH close
        # skips the whole decision (mirrors the single-symbol runner's `continue`).
        decision_snapshot = assembled[0][1]
        schedule = _schedule_from_windows(
            decision_snapshot.session_date_et, rth_close_time_utc, session_windows)
        exit_bar_end = horizon_gate(decision_snapshot, horizon, schedule)
        if not isinstance(exit_bar_end, str):
            continue
        close = schedule.rth_close_utc
        entry_bar_end = _entry_bar_end(decision_ts)
        entry_instant = _parse_utc(entry_bar_end)

        candidates = proxy.decide(
            snapshots=valid_snaps, universe_order=universe_t, now_ms=now_ms)
        candidate_count += len(candidates)
        opened_this_decision = 0
        opened_notional_this_decision = Decimal("0")
        for candidate in candidates:
            leg = candidate.legs[0]
            symbol = leg.symbol
            prior_exit = open_until.get(symbol)
            if prior_exit is not None and entry_instant <= prior_exit:
                overlap_suppressed_leg_count += 1
                continue
            result = _simulate_historical_long_trade(
                reader=readers[symbol], symbol=symbol,
                instrument_id=leg.instrument_id,
                decision_bar_end_utc=decision_ts, entry_bar_end_utc=entry_bar_end,
                exit_bar_end_utc=exit_bar_end, decision_ts_utc=decision_ts,
                latency_ms=latency_ms, rth_close_utc=close, qty=leg.qty,
                strategy_limit=leg.limit_price, slippage_cap_bps=slippage_cap_bps)
            if isinstance(result, BacktestSkip):
                skips.append(result)
                continue
            feat = feature_by_symbol[symbol]
            trades.append(replace(
                result,
                decision_realized_vol_21=_feature_decimal(
                    feat.features, "realized_vol_21"),
                decision_z_ret_21=_feature_decimal(feat.features, "z_ret_21")))
            open_until[symbol] = _parse_utc(exit_bar_end)
            per_symbol_leg_counts[symbol] = (
                per_symbol_leg_counts.get(symbol, 0) + 1)
            opened_this_decision += 1
            opened_notional_this_decision += result.entry_mid * result.qty

        if opened_this_decision == 0:
            continue
        acting_decision_count += 1
        # universe_equal_weight_long_v1: deploy the strategy's ACTUAL opened notional
        # (already whole-share floored) equally across every valid symbol over the SAME
        # entry/exit bars and fill assumptions. Sizing off the realised notional (not a
        # nominal TOP_N x PAPER_NOTIONAL) keeps the active-PnL comparison exposure-matched
        # so whole-share flooring does not systematically over-fund the benchmark.
        per_name_notional = (
            opened_notional_this_decision / Decimal(ranking.valid_count))
        for ranked in ranking.ranked:
            if ranked.mid <= 0:
                continue
            qty_b = per_name_notional / ranked.mid
            if qty_b <= 0:
                continue
            bench = _simulate_historical_long_trade(
                reader=readers[ranked.symbol], symbol=ranked.symbol,
                instrument_id=ranked.instrument_id,
                decision_bar_end_utc=decision_ts, entry_bar_end_utc=entry_bar_end,
                exit_bar_end_utc=exit_bar_end, decision_ts_utc=decision_ts,
                latency_ms=latency_ms, rth_close_utc=close, qty=qty_b,
                strategy_limit=None, slippage_cap_bps=slippage_cap_bps)
            if isinstance(bench, BacktestTrade):
                benchmark_net += bench.net_execution_realistic_pnl_usd
                benchmark_leg_fill_count += 1
            else:
                benchmark_leg_skip_count += 1

    trades_t = tuple(trades)
    skips_t = tuple(skips)
    p95_gap, max_gap = _gap_summary(trades_t)
    strategy_net = sum(
        (trade.net_execution_realistic_pnl_usd for trade in trades_t),
        Decimal("0"))
    benchmark_net_q = _quantize_usd(benchmark_net)
    active = _quantize_usd(strategy_net - benchmark_net_q)
    return HistoricalCrossSectionalResult(
        trades=trades_t,
        skips=skips_t,
        bar_count=total_bar_count,
        candidate_count=candidate_count,
        p95_realism_gap_bps=p95_gap,
        max_single_fill_divergence_bps=max_gap,
        ca_blackout_skip_count=ca_blackout_skip_count,
        data_quality_skip_count=len(skips_t),
        decision_count=len(timeline),
        acting_decision_count=acting_decision_count,
        insufficient_valid_decision_count=insufficient_valid_decision_count,
        overlap_suppressed_leg_count=overlap_suppressed_leg_count,
        exclusion_reason_counts=dict(sorted(exclusion_counts.items())),
        per_symbol_leg_counts=dict(sorted(per_symbol_leg_counts.items())),
        benchmark_leg_fill_count=benchmark_leg_fill_count,
        benchmark_leg_skip_count=benchmark_leg_skip_count,
        equal_weight_long_benchmark_net_usd=benchmark_net_q,
        equal_weight_long_active_pnl_usd=active,
    )


def write_m7_historical_diagnostic_export(
        *, output_path, quote_rows: Iterable[dict], symbol: str,
        instrument_id: int, rules_hash: str, data_pin: str,
        dataset: str = "EQUS.MINI", schema: str = "tbbo",
        created_utc: str, input_manifest: Mapping[str, object],
        builder_git_commit: str, strategy_id: str = STRATEGY_ID,
        agent_rules_path=None, max_trade_rows: int = 2000,
        max_skip_rows: int = 2000,
        production_artifacts_dir=None) -> HistoricalDiagnosticExportResult:
    """Write a bounded M7 diagnostic export without raw quote rows or artifacts."""
    max_trade_rows = _positive_int(max_trade_rows, name="max_trade_rows")
    max_skip_rows = _positive_int(max_skip_rows, name="max_skip_rows")
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
    strategy_for_id(strategy_id)
    output = Path(output_path)
    production_dir = (
        Path(production_artifacts_dir).resolve()
        if production_artifacts_dir is not None
        else PRODUCTION_ARTIFACTS_DIR
    )
    try:
        output.resolve().relative_to(production_dir)
    except ValueError:
        pass
    else:
        raise HistoricalArtifactWriteRefused(
            "historical diagnostic exports must not write under artifacts/backtests")

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
        strategy_id=strategy_id,
    )
    payload = _build_diagnostic_payload(
        backtest=backtest,
        manifest=manifest,
        strategy_id=strategy_id,
        rules_hash=rules_hash,
        data_pin=data_pin,
        created_utc=created_utc,
        builder_git_commit=builder_git_commit,
        max_trade_rows=max_trade_rows,
        max_skip_rows=max_skip_rows,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(dumps(payload), encoding="utf-8")
    return HistoricalDiagnosticExportResult(
        output_path=output,
        payload=payload,
        backtest=backtest,
    )


def write_m7_historical_diagnostic_index(
        *, output_path, diagnostic_paths: Iterable[object], created_utc: str,
        source_run_id: str | None = None,
        source_summary_path: str | None = None,
        sample_window_id: str | None = None,
        holdout_window_id: str | None = None,
        production_artifacts_dir=None) -> HistoricalDiagnosticIndexResult:
    """Write a bounded cross-symbol diagnostic index outside artifact storage."""
    output = Path(output_path)
    production_dir = (
        Path(production_artifacts_dir).resolve()
        if production_artifacts_dir is not None
        else PRODUCTION_ARTIFACTS_DIR
    )
    try:
        output.resolve().relative_to(production_dir)
    except ValueError:
        pass
    else:
        raise HistoricalArtifactWriteRefused(
            "historical diagnostic indexes must not write under artifacts/backtests")
    paths = tuple(Path(path) for path in diagnostic_paths)
    if not paths:
        raise ValueError("diagnostic_paths must contain at least one path")
    payloads = tuple(_load_diagnostic_payload(path) for path in paths)
    payload = _diagnostic_index_payload(
        payloads=payloads,
        diagnostic_paths=paths,
        created_utc=created_utc,
        source_run_id=source_run_id,
        source_summary_path=source_summary_path,
        sample_window_id=sample_window_id,
        holdout_window_id=holdout_window_id,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(dumps(payload), encoding="utf-8")
    return HistoricalDiagnosticIndexResult(output_path=output, payload=payload)


def write_m7_historical_artifact(*, artifacts_dir, quote_rows: Iterable[dict],
                                 symbol: str, instrument_id: int,
                                 rules_hash: str, data_pin: str,
                                 dataset: str = "EQUS.MINI",
                                 schema: str = "tbbo",
                                 created_utc: str,
                                 input_manifest: Mapping[str, object],
                                 builder_git_commit: str,
                                 allow_reviewed_artifact: bool,
                                 strategy_id: str = STRATEGY_ID,
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
    strategy_for_id(strategy_id)
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
        strategy_id=strategy_id,
    )
    payload = build_v2_artifact_payload(
        strategy_id=strategy_id,
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
    artifact_path = output_dir / f"{strategy_id}.json"
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
