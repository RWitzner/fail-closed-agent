"""M7 pure backtest primitives.

This module is deliberately broker-free and journal-free. It reads the M3 mid-bar
series through its existing eligibility seam, applies the M7 latency guard, and
returns immutable in-memory results for artifact builders.
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Dict, Union

from agent.bar_series import MidBar, MidBarSeriesReader, MissingBar
from agent.bar_series import _canonical_utc, _parse_utc

PNL_QUANTUM = Decimal("0.000001")

BACKTEST_SKIP_REASONS = frozenset({
    "future_receipt",
    "out_of_series",
    "no_quotes_in_bucket",
    "invalid_quotes_only",
    "horizon_crosses_close",
    "quote_b_before_latency",
    "candidate_invalid",
    "pricing_rejected",
    "latency_lost_edge",
    "ca_blackout",
    "market_state_not_tradable",
})


@dataclass(frozen=True)
class BacktestSkip:
    reason: str
    bucket_end_utc: str
    detail: Dict[str, object]

    def __post_init__(self) -> None:
        if self.reason not in BACKTEST_SKIP_REASONS:
            raise ValueError(f"out-of-vocabulary backtest skip reason: {self.reason!r}")


@dataclass(frozen=True)
class BacktestTrade:
    symbol: str
    instrument_id: int
    qty: Decimal
    entry_bar_end_utc: str
    exit_bar_end_utc: str
    entry_mid: Decimal
    exit_mid: Decimal
    gross_modeled_usd: Decimal
    fees_usd: Decimal
    net_execution_realistic_pnl_usd: Decimal
    benchmark_pnl_usd: Decimal


def _skip(reason: str, bucket_end_utc: str, symbol: str) -> BacktestSkip:
    return BacktestSkip(reason=reason, bucket_end_utc=bucket_end_utc,
                        detail={"symbol": symbol})


def _from_missing(missing: MissingBar, *, symbol: str) -> BacktestSkip:
    return _skip(missing.reason, missing.bucket_end_utc, symbol)


def _as_decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{name} must be a finite Decimal-compatible value")
    parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _as_nonnegative_latency_ms(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("latency_ms must be a nonnegative integer")
    return value


def read_eligible_midbar(reader: MidBarSeriesReader, symbol: str, instrument_id: int,
                         bucket_end_utc: str, *, as_of_utc: str
                         ) -> Union[MidBar, BacktestSkip]:
    """Return the bar only when M3 FD-2 says it is eligible at ``as_of_utc``."""
    result = reader.get(symbol, instrument_id, bucket_end_utc, as_of_utc=as_of_utc)
    if isinstance(result, MissingBar):
        return _from_missing(result, symbol=symbol)
    return result


def simulate_long_midbar_trade(*, reader: MidBarSeriesReader, symbol: str,
                               instrument_id: int, entry_bar_end_utc: str,
                               exit_bar_end_utc: str, decision_ts_utc: str,
                               latency_ms: int, rth_close_utc: str, qty: Decimal,
                               fees_usd: Decimal
                               ) -> Union[BacktestTrade, BacktestSkip]:
    """Simulate one deterministic long trade over eligible mid bars.

    Quote B must be the next selected bar after the configured latency instant.
    Any horizon crossing the RTH close is skipped rather than filled from a future
    bar.
    """
    exit_end = _parse_utc(exit_bar_end_utc)
    if exit_end > _parse_utc(rth_close_utc):
        return _skip("horizon_crosses_close", exit_bar_end_utc, symbol)

    qty_d = _as_decimal(qty, name="qty")
    fees_d = _as_decimal(fees_usd, name="fees_usd")
    if qty_d <= 0:
        raise ValueError("qty must be positive")
    if fees_d < 0:
        raise ValueError("fees_usd must be nonnegative")

    latency_instant = (
        _parse_utc(decision_ts_utc)
        + timedelta(milliseconds=_as_nonnegative_latency_ms(latency_ms))
    )
    entry_end = _parse_utc(entry_bar_end_utc)
    if entry_end <= latency_instant:
        return _skip("quote_b_before_latency", entry_bar_end_utc, symbol)

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

    gross = ((exit_bar.mid - entry.mid) * qty_d).quantize(
        PNL_QUANTUM, rounding=ROUND_HALF_EVEN)
    fees_q = fees_d.quantize(PNL_QUANTUM, rounding=ROUND_HALF_EVEN)
    net = (gross - fees_q).quantize(PNL_QUANTUM, rounding=ROUND_HALF_EVEN)
    return BacktestTrade(
        symbol=symbol,
        instrument_id=instrument_id,
        qty=qty_d,
        entry_bar_end_utc=entry.bucket_end_utc,
        exit_bar_end_utc=exit_bar.bucket_end_utc,
        entry_mid=entry.mid,
        exit_mid=exit_bar.mid,
        gross_modeled_usd=gross,
        fees_usd=fees_q,
        net_execution_realistic_pnl_usd=net,
        benchmark_pnl_usd=gross,
    )
