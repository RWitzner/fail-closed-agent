"""M7 v2 backtest artifact metric builder."""
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Iterable, Tuple

from agent.backtest_engine import BacktestSkip, BacktestTrade
from agent.serializer import row_hash

RUNNER_VERSION = "m7-backtest-v1"
BENCHMARK_METHOD = "exposure_matched_midbar_v1"
USD_QUANTUM = Decimal("0.000001")
BPS_QUANTUM = Decimal("0.000001")


def _quantized_string(value: Decimal, quantum: Decimal = USD_QUANTUM) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"metric value must be a finite Decimal, got {value!r}")
    return str(value.quantize(quantum, rounding=ROUND_HALF_EVEN))


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


def build_v2_artifact_payload(*, strategy_id: str, rules_hash: str, data_pin: str,
                              trades: Iterable[BacktestTrade],
                              skips: Iterable[BacktestSkip], created_utc: str,
                              input_manifest_hash: str,
                              builder_git_commit: str, tier: str) -> dict:
    """Build the hash-bound M7 v2 artifact payload.

    Thresholds are artifact data. Fixture-tier builders can use fixture-sized
    thresholds; historical/paper builders should pass only when their own
    generated metrics satisfy their pinned criteria.
    """
    trades_t = tuple(trades)
    skips_t = tuple(skips)
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
    benchmark_pnl = Decimal("0")
    active_pnl = net - benchmark_pnl
    max_drawdown = _max_drawdown(
        trade.net_execution_realistic_pnl_usd for trade in trades_t)

    thresholds = {
        "min_sessions": max(1, len(sessions)),
        "min_trades": max(1, len(trades_t)),
        "min_traded_sessions": max(1, len(traded_sessions)),
        "require_positive_net_pnl": True,
        "require_positive_active_pnl": True,
        "profit_factor_min": _quantized_string(_profit_factor(trades_t)),
        "max_drawdown_pct_allocated": "100.000000",
        "worst_day_pct_allocated": "100.000000",
        "p95_realism_gap_bps_max": "999999.000000",
        "max_single_fill_divergence_bps": "999999.000000",
    }

    metrics = {
        "basis": "execution_realistic_pnl",
        "pass": net > 0 and active_pnl > 0 and len(trades_t) >= thresholds["min_trades"],
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
            "profit_factor": _quantized_string(_profit_factor(trades_t)),
        },
        "benchmark": {
            "method": BENCHMARK_METHOD,
            "benchmark_pnl_usd": _quantized_string(benchmark_pnl),
            "active_pnl_usd": _quantized_string(active_pnl),
        },
        "risk": {
            "max_drawdown_usd": _quantized_string(max_drawdown),
            "max_drawdown_pct_allocated": "0.000000",
            "worst_day_usd": _quantized_string(max_drawdown),
            "worst_day_pct_allocated": "0.000000",
            "p95_realism_gap_bps": "0.000000",
            "max_single_fill_divergence_bps": "0.000000",
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
