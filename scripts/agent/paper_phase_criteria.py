"""M7 paper-phase success criteria evaluator.

The evaluator consumes already-built artifact metrics and verifies the pinned
paper-phase matrix. It does not rerun a backtest or consult broker/journal state.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple


@dataclass(frozen=True)
class CriteriaVerdict:
    passed: bool
    failures: Tuple[str, ...]


def _section(metrics: dict, key: str) -> dict:
    value = metrics.get(key) if isinstance(metrics, dict) else None
    return value if isinstance(value, dict) else {}


def _int_at(metrics: dict, section: str, key: str,
            failures: list) -> Optional[int]:
    value = _section(metrics, section).get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        failures.append(f"missing:{section}.{key}")
        return None
    return value


def _decimal_at(metrics: dict, section: str, key: str,
                failures: list) -> Optional[Decimal]:
    value = _section(metrics, section).get(key)
    if not isinstance(value, str):
        failures.append(f"missing:{section}.{key}")
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        failures.append(f"missing:{section}.{key}")
        return None
    if not parsed.is_finite():
        failures.append(f"missing:{section}.{key}")
        return None
    return parsed


def _threshold_int(metrics: dict, key: str, failures: list) -> Optional[int]:
    value = _section(metrics, "thresholds").get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        failures.append(f"missing:thresholds.{key}")
        return None
    return value


def _threshold_decimal(metrics: dict, key: str,
                       failures: list) -> Optional[Decimal]:
    value = _section(metrics, "thresholds").get(key)
    if not isinstance(value, str):
        failures.append(f"missing:thresholds.{key}")
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        failures.append(f"missing:thresholds.{key}")
        return None
    if not parsed.is_finite():
        failures.append(f"missing:thresholds.{key}")
        return None
    return parsed


def _threshold_bool(metrics: dict, key: str, failures: list) -> Optional[bool]:
    value = _section(metrics, "thresholds").get(key)
    if not isinstance(value, bool):
        failures.append(f"missing:thresholds.{key}")
        return None
    return value


def _check_min(actual: Optional[int], threshold: Optional[int], name: str,
               failures: list) -> None:
    if actual is not None and threshold is not None and actual < threshold:
        failures.append(name)


def _check_gt_zero(actual: Optional[Decimal], enabled: Optional[bool], name: str,
                   failures: list) -> None:
    if enabled is not True:
        failures.append(f"{name}_threshold")
        return
    if actual is not None and actual <= 0:
        failures.append(name)


def _check_decimal_min(actual: Optional[Decimal], threshold: Optional[Decimal],
                       name: str, failures: list) -> None:
    if actual is not None and threshold is not None and actual < threshold:
        failures.append(name)


def _check_decimal_max(actual: Optional[Decimal], threshold: Optional[Decimal],
                       name: str, failures: list) -> None:
    if actual is not None and threshold is not None and actual > threshold:
        failures.append(name)


def _check_zero(actual: Optional[int], name: str, failures: list) -> None:
    if actual is not None and actual != 0:
        failures.append(name)


def evaluate_paper_phase_criteria(metrics: dict) -> CriteriaVerdict:
    failures = []
    if not isinstance(metrics, dict):
        return CriteriaVerdict(False, ("missing:metrics",))
    if metrics.get("pass") is not True:
        failures.append("metrics.pass")

    session_count = _int_at(metrics, "sample", "session_count", failures)
    trade_count = _int_at(metrics, "sample", "trade_count", failures)
    traded_session_count = _int_at(
        metrics, "sample", "traded_session_count", failures)
    _check_min(session_count, _threshold_int(metrics, "min_sessions", failures),
               "min_sessions", failures)
    _check_min(trade_count, _threshold_int(metrics, "min_trades", failures),
               "min_trades", failures)
    _check_min(traded_session_count,
               _threshold_int(metrics, "min_traded_sessions", failures),
               "min_traded_sessions", failures)

    _check_gt_zero(
        _decimal_at(metrics, "pnl", "net_execution_realistic_pnl_usd",
                    failures),
        _threshold_bool(metrics, "require_positive_net_pnl", failures),
        "positive_net_pnl",
        failures,
    )
    _check_gt_zero(
        _decimal_at(metrics, "benchmark", "active_pnl_usd", failures),
        _threshold_bool(metrics, "require_positive_active_pnl", failures),
        "positive_active_pnl",
        failures,
    )
    _check_decimal_min(
        _decimal_at(metrics, "pnl", "profit_factor", failures),
        _threshold_decimal(metrics, "profit_factor_min", failures),
        "profit_factor_min",
        failures,
    )
    avg_trade_bps = _decimal_at(metrics, "pnl", "avg_trade_bps", failures)
    if avg_trade_bps is not None and avg_trade_bps <= 0:
        failures.append("avg_trade_bps_positive")

    _check_decimal_max(
        _decimal_at(metrics, "risk", "max_drawdown_pct_allocated", failures),
        _threshold_decimal(metrics, "max_drawdown_pct_allocated", failures),
        "max_drawdown_pct_allocated",
        failures,
    )
    _check_decimal_max(
        _decimal_at(metrics, "risk", "worst_day_pct_allocated", failures),
        _threshold_decimal(metrics, "worst_day_pct_allocated", failures),
        "worst_day_pct_allocated",
        failures,
    )
    _check_decimal_max(
        _decimal_at(metrics, "risk", "p95_realism_gap_bps", failures),
        _threshold_decimal(metrics, "p95_realism_gap_bps_max", failures),
        "p95_realism_gap_bps_max",
        failures,
    )
    _check_decimal_max(
        _decimal_at(metrics, "risk", "max_single_fill_divergence_bps",
                    failures),
        _threshold_decimal(metrics, "max_single_fill_divergence_bps", failures),
        "max_single_fill_divergence_bps",
        failures,
    )

    for key in (
            "unresolved_reconcile_drift_count",
            "s1_canary_breach_count",
            "live_broker_submit_count",
            "artifact_mismatch_count",
            "unhandled_exception_count",
    ):
        _check_zero(_int_at(metrics, "quality", key, failures), key, failures)

    return CriteriaVerdict(passed=not failures, failures=tuple(failures))
