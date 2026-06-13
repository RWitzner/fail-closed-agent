"""M7 v2 backtest artifact metric builder."""
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Iterable, Tuple

from agent.backtest_engine import BacktestSkip, BacktestTrade
from agent.paper_phase_criteria import (
    PINNED_MAX_DRAWDOWN_PCT_ALLOCATED,
    PINNED_MAX_SINGLE_FILL_DIVERGENCE_BPS,
    PINNED_MIN_SESSIONS,
    PINNED_MIN_TRADES,
    PINNED_MIN_TRADED_SESSIONS,
    PINNED_P95_REALISM_GAP_BPS_MAX,
    PINNED_PROFIT_FACTOR_MIN,
    PINNED_WORST_DAY_PCT_ALLOCATED,
)
from agent.serializer import row_hash

RUNNER_VERSION = "m7-backtest-v1"
BENCHMARK_METHOD = "exposure_matched_midbar_v1"
USD_QUANTUM = Decimal("0.000001")
BPS_QUANTUM = Decimal("0.000001")


def _quantized_string(value: Decimal, quantum: Decimal = USD_QUANTUM) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"metric value must be a finite Decimal, got {value!r}")
    return str(value.quantize(quantum, rounding=ROUND_HALF_EVEN))


def _positive_decimal(value: Decimal, *, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a positive finite Decimal")
    return value


def _nonnegative_decimal(value: Decimal, *, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a nonnegative finite Decimal")
    return value


def _symbols(trades: Tuple[BacktestTrade, ...],
             skips: Tuple[BacktestSkip, ...]) -> list:
    symbols = {trade.symbol for trade in trades}
    for skip in skips:
        symbol = skip.detail.get("symbol")
        if isinstance(symbol, str) and symbol:
            symbols.add(symbol)
    if not symbols:
        raise ValueError("artifact sample must include at least one symbol")
    return sorted(symbols)


def _date_part(ts: str) -> str:
    return ts[:10]


def _sample_bounds(trades: Tuple[BacktestTrade, ...],
                   skips: Tuple[BacktestSkip, ...],
                   created_utc: str) -> tuple:
    timestamps = []
    for trade in trades:
        timestamps.extend((trade.entry_bar_end_utc, trade.exit_bar_end_utc))
    for skip in skips:
        timestamps.append(skip.bucket_end_utc)
    if not timestamps:
        timestamps.append(created_utc)
    return min(timestamps), max(timestamps)


def _profit_factor(trades: Tuple[BacktestTrade, ...]) -> Decimal:
    gains = sum((trade.net_execution_realistic_pnl_usd for trade in trades
                 if trade.net_execution_realistic_pnl_usd > 0), Decimal("0"))
    losses = sum((-trade.net_execution_realistic_pnl_usd for trade in trades
                  if trade.net_execution_realistic_pnl_usd < 0), Decimal("0"))
    if losses == 0:
        return Decimal("999999.000000") if gains > 0 else Decimal("0")
    return (gains / losses).quantize(BPS_QUANTUM, rounding=ROUND_HALF_EVEN)


def _max_drawdown(values: Iterable[Decimal]) -> Decimal:
    peak = Decimal("0")
    equity = Decimal("0")
    worst = Decimal("0")
    for value in values:
        equity += value
        if equity > peak:
            peak = equity
        drawdown = equity - peak
        if drawdown < worst:
            worst = drawdown
    return worst


def _worst_day(trades: Tuple[BacktestTrade, ...]) -> Decimal:
    by_day = {}
    for trade in trades:
        day = _date_part(trade.exit_bar_end_utc)
        by_day[day] = by_day.get(day, Decimal("0")) + (
            trade.net_execution_realistic_pnl_usd)
    return min(by_day.values()) if by_day else Decimal("0")


def _loss_pct(loss_usd: Decimal, allocated_notional_usd: Decimal) -> Decimal:
    if loss_usd >= 0:
        return Decimal("0")
    return ((-loss_usd) / allocated_notional_usd).quantize(
        BPS_QUANTUM, rounding=ROUND_HALF_EVEN)


def build_v2_artifact_payload(*, strategy_id: str, rules_hash: str, data_pin: str,
                              trades: Iterable[BacktestTrade],
                              skips: Iterable[BacktestSkip], created_utc: str,
                              input_manifest_hash: str,
                              builder_git_commit: str, tier: str,
                              allocated_notional_usd: Decimal,
                              p95_realism_gap_bps: Decimal,
                              max_single_fill_divergence_bps: Decimal) -> dict:
    """Build the hash-bound M7 v2 artifact payload.

    Thresholds are artifact data, but they are pinned to the M7 paper/M8 floors
    here so fixture artifacts cannot relax the reviewed criteria.
    """
    trades_t = tuple(trades)
    skips_t = tuple(skips)
    allocated = _positive_decimal(
        allocated_notional_usd, name="allocated_notional_usd")
    p95_gap = _nonnegative_decimal(
        p95_realism_gap_bps, name="p95_realism_gap_bps")
    max_fill_gap = _nonnegative_decimal(
        max_single_fill_divergence_bps,
        name="max_single_fill_divergence_bps")
    start_utc, end_utc = _sample_bounds(trades_t, skips_t, created_utc)
    sessions = {
        _date_part(trade.entry_bar_end_utc) for trade in trades_t
    } | {
        _date_part(skip.bucket_end_utc) for skip in skips_t
    }
    traded_sessions = {_date_part(trade.exit_bar_end_utc) for trade in trades_t}

    gross = sum((trade.gross_modeled_usd for trade in trades_t), Decimal("0"))
    fees = sum((trade.fees_usd for trade in trades_t), Decimal("0"))
    net = sum((trade.net_execution_realistic_pnl_usd for trade in trades_t),
              Decimal("0"))
    notional = sum((trade.entry_mid * trade.qty for trade in trades_t),
                   Decimal("0"))
    avg_trade_bps = (
        (net / notional) * Decimal("10000")
        if notional > 0 else Decimal("0")
    )
    benchmark_pnl = sum((trade.benchmark_pnl_usd for trade in trades_t),
                        Decimal("0"))
    active_pnl = net - benchmark_pnl
    max_drawdown = _max_drawdown(
        trade.net_execution_realistic_pnl_usd for trade in trades_t)
    worst_day = _worst_day(trades_t)
    profit_factor = _profit_factor(trades_t)
    max_drawdown_pct_allocated = _loss_pct(max_drawdown, allocated)
    worst_day_pct_allocated = _loss_pct(worst_day, allocated)

    thresholds = {
        "min_sessions": PINNED_MIN_SESSIONS,
        "min_trades": PINNED_MIN_TRADES,
        "min_traded_sessions": PINNED_MIN_TRADED_SESSIONS,
        "require_positive_net_pnl": True,
        "require_positive_active_pnl": True,
        "profit_factor_min": _quantized_string(PINNED_PROFIT_FACTOR_MIN),
        "max_drawdown_pct_allocated": _quantized_string(
            PINNED_MAX_DRAWDOWN_PCT_ALLOCATED),
        "worst_day_pct_allocated": _quantized_string(
            PINNED_WORST_DAY_PCT_ALLOCATED),
        "p95_realism_gap_bps_max": _quantized_string(
            PINNED_P95_REALISM_GAP_BPS_MAX),
        "max_single_fill_divergence_bps": _quantized_string(
            PINNED_MAX_SINGLE_FILL_DIVERGENCE_BPS),
    }
    passed = (
        net > 0
        and active_pnl > 0
        and len(sessions) >= PINNED_MIN_SESSIONS
        and len(trades_t) >= PINNED_MIN_TRADES
        and len(traded_sessions) >= PINNED_MIN_TRADED_SESSIONS
        and profit_factor >= PINNED_PROFIT_FACTOR_MIN
        and avg_trade_bps > 0
        and max_drawdown_pct_allocated <= PINNED_MAX_DRAWDOWN_PCT_ALLOCATED
        and worst_day_pct_allocated <= PINNED_WORST_DAY_PCT_ALLOCATED
        and p95_gap <= PINNED_P95_REALISM_GAP_BPS_MAX
        and (
            max_fill_gap <= PINNED_MAX_SINGLE_FILL_DIVERGENCE_BPS
        )
    )

    metrics = {
        "basis": "execution_realistic_pnl",
        "pass": passed,
        "runner_version": RUNNER_VERSION,
        "strategy_version": strategy_id,
        "sample": {
            "start_utc": start_utc,
            "end_utc": end_utc,
            "session_count": len(sessions),
            "decision_count": len(trades_t) + len(skips_t),
            "trade_count": len(trades_t),
            "traded_session_count": len(traded_sessions),
            "symbols": _symbols(trades_t, skips_t),
        },
        "pnl": {
            "gross_modeled_usd": _quantized_string(gross),
            "fees_usd": _quantized_string(fees),
            "net_execution_realistic_pnl_usd": _quantized_string(net),
            "avg_trade_bps": _quantized_string(avg_trade_bps, BPS_QUANTUM),
            "profit_factor": _quantized_string(profit_factor),
        },
        "benchmark": {
            "method": BENCHMARK_METHOD,
            "benchmark_pnl_usd": _quantized_string(benchmark_pnl),
            "active_pnl_usd": _quantized_string(active_pnl),
        },
        "risk": {
            "max_drawdown_usd": _quantized_string(max_drawdown),
            "max_drawdown_pct_allocated": _quantized_string(
                max_drawdown_pct_allocated),
            "worst_day_usd": _quantized_string(worst_day),
            "worst_day_pct_allocated": _quantized_string(
                worst_day_pct_allocated),
            "p95_realism_gap_bps": _quantized_string(p95_gap),
            "max_single_fill_divergence_bps": _quantized_string(max_fill_gap),
        },
        "quality": {
            "future_receipt_count": sum(
                1 for skip in skips_t if skip.reason == "future_receipt"),
            "missing_bar_count": sum(
                1 for skip in skips_t if skip.reason in {
                    "out_of_series",
                    "no_quotes_in_bucket",
                    "invalid_quotes_only",
                }),
            "ca_blackout_skips": 0,
            "data_quality_skip_count": len(skips_t),
            "unresolved_reconcile_drift_count": 0,
            "s1_canary_breach_count": 0,
            "live_broker_submit_count": 0,
            "artifact_mismatch_count": 0,
            "unhandled_exception_count": 0,
        },
        "thresholds": thresholds,
        "provenance": {
            "input_manifest_hash": input_manifest_hash,
            "builder_git_commit": builder_git_commit,
            "tier": tier,
        },
    }

    payload = {
        "v": 2,
        "strategy_id": strategy_id,
        "rules_hash": rules_hash,
        "data_pin": data_pin,
        "metrics": metrics,
        "created_utc": created_utc,
    }
    payload["artifact_hash"] = row_hash(payload)
    return payload
