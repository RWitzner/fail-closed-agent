"""Weekly paper-phase criteria aggregator — evidence in, verdict out.

Feeds the PINNED paper-phase matrix (``paper_phase_criteria``) from real
session evidence: the daily reports (session/date bookkeeping, incomplete
days, drift) plus a journal re-scan (per-trade MODELED PnL → profit factor /
avg bps / drawdown / worst day; ``fill_divergence.divergence_bps`` → p95/max).
``--allocated-notional-usd`` is EXPLICIT — the drawdown/worst-day percentages
are meaningless without the operator-declared allocation, so there is no
default.

Missing evidence is NEVER converted to zero: sections the evidence cannot
support are OMITTED and surface as explicit ``missing:...`` failures from the
evaluator (e.g. the paper-path exposure-matched benchmark leg and several
quality counters exist only once armed sessions journal them). The verdict is
therefore fail-closed-incomplete until the full paper pipeline produces the
full evidence set — by design, not by accident.

Sessions: the authoritative report per date is the HIGHEST restart suffix;
``session_incomplete`` days are counted separately and never as clean
sessions. Multi-day journals are filtered per session date via the daily
reports' run_ids when present.
"""
import json
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from agent.journal import replay as journal_replay
from agent.paper_phase_criteria import (
    PINNED_MAX_DRAWDOWN_PCT_ALLOCATED,
    PINNED_MAX_SINGLE_FILL_DIVERGENCE_BPS,
    PINNED_MIN_SESSIONS,
    PINNED_MIN_TRADED_SESSIONS,
    PINNED_MIN_TRADES,
    PINNED_P95_REALISM_GAP_BPS_MAX,
    PINNED_PROFIT_FACTOR_MIN,
    PINNED_WORST_DAY_PCT_ALLOCATED,
    evaluate_paper_phase_criteria,
)
from agent.serializer import dumps

_BPS_Q = Decimal("0.000001")
_PCT_Q = Decimal("0.000001")
_USD_Q = Decimal("0.000001")


def _dec(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _q(value: Decimal, quantum: Decimal) -> str:
    return str(value.quantize(quantum, rounding=ROUND_HALF_EVEN))


def _p95(sorted_values: List[Decimal]) -> Decimal:
    """Nearest-rank p95 (matches the conservative backtest convention)."""
    if not sorted_values:
        return Decimal("0")
    rank = max(1, -(-len(sorted_values) * 95 // 100))   # ceil(n*0.95)
    return sorted_values[rank - 1]


def latest_reports_by_date(report_dir: Path) -> Dict[str, dict]:
    """Highest-restart-suffix report per session date."""
    by_date: Dict[str, tuple] = {}
    for path in sorted(report_dir.glob("*.json")):
        stem = path.name[:-len(".json")]
        parts = stem.split(".")
        date = parts[0]
        try:
            suffix = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        current = by_date.get(date)
        if current is None or suffix > current[0]:
            by_date[date] = (suffix, report)
    return {date: report for date, (suffix, report) in sorted(by_date.items())}


def build_phase_metrics(*, report_dir, journal_dir,
                        allocated_notional_usd: str,
                        start_date: Optional[str] = None,
                        end_date: Optional[str] = None) -> dict:
    """Aggregate the evidence into the evaluator's metrics shape + extras."""
    allocated = _dec(allocated_notional_usd)
    if allocated is None or allocated <= 0:
        raise ValueError("--allocated-notional-usd must be a positive decimal")
    report_dir = Path(report_dir)
    journal_dir = Path(journal_dir)

    reports = latest_reports_by_date(report_dir)
    if start_date:
        reports = {d: r for d, r in reports.items() if d >= start_date}
    if end_date:
        reports = {d: r for d, r in reports.items() if d <= end_date}

    run_ids = set()
    complete_dates: List[str] = []
    incomplete_dates: List[str] = []
    truncated_dates: List[str] = []
    drift_rows = 0
    for date, report in reports.items():
        if report.get("run_id"):
            run_ids.add(report["run_id"])
        session = report.get("session") or {}
        if report.get("session_incomplete"):
            incomplete_dates.append(date)
        elif session.get("feed_truncated"):
            truncated_dates.append(date)
        else:
            complete_dates.append(date)
        reconcile = report.get("reconcile") or {}
        drift_rows += int(reconcile.get("drift_rows") or 0)

    def _rows(stream: str) -> list:
        path = journal_dir / f"{stream}.jsonl"
        if not path.exists():
            return []
        rows = journal_replay(path)
        if run_ids:
            rows = [r for r in rows if r.get("run_id") in run_ids]
        return rows

    closes = [r for r in _rows("positions")
              if r.get("event_type") == "position_close"]
    per_trade: List[Decimal] = []
    modeled_null_closes = 0
    fees = Decimal("0")
    daily_pnl: Dict[str, Decimal] = {}
    for row in closes:
        pnl = _dec(row.get("realized_modeled_pnl"))
        if pnl is None:
            modeled_null_closes += 1
            continue
        per_trade.append(pnl)
        date = str(row.get("ts_utc", ""))[:10]
        daily_pnl[date] = daily_pnl.get(date, Decimal("0")) + pnl
        assessed = row.get("fees_assessed")
        if isinstance(assessed, dict):
            fee = _dec(assessed.get("total_usd"))
            if fee is not None:
                fees += fee

    net = sum(per_trade, Decimal("0"))
    gains = sum((p for p in per_trade if p > 0), Decimal("0"))
    losses = sum((-p for p in per_trade if p < 0), Decimal("0"))

    divergences = sorted(
        d for d in (_dec((r.get("divergence_bps"))) for r in _rows("fills")
                    if r.get("event_type") == "fill_divergence")
        if d is not None)
    abs_divergences = sorted(abs(d) for d in divergences)

    # equity path over sessions → max drawdown + worst day (vs allocated)
    equity = Decimal("0")
    high_water = Decimal("0")
    max_drawdown = Decimal("0")
    worst_day = Decimal("0")
    for date in sorted(daily_pnl):
        day = daily_pnl[date]
        worst_day = min(worst_day, day)
        equity += day
        high_water = max(high_water, equity)
        max_drawdown = max(max_drawdown, high_water - equity)

    metrics = {
        "basis": "execution_realistic_pnl",
        "pass": True,   # the CLAIM under evaluation — the verdict adjudicates
        "sample": {
            "start_utc": (min(reports) if reports else None),
            "end_utc": (max(reports) if reports else None),
            "session_count": len(complete_dates),
            "trade_count": len(per_trade),
            "traded_session_count": len(daily_pnl),
        },
        "pnl": {
            "gross_modeled_usd": _q(net + fees, _USD_Q),
            "fees_usd": _q(fees, _USD_Q),
            "net_execution_realistic_pnl_usd": _q(net, _USD_Q),
            "avg_trade_bps": (
                _q((net / allocated) * Decimal("10000")
                   / Decimal(len(per_trade)), _BPS_Q)
                if per_trade else None),
            "profit_factor": (_q(gains / losses, _BPS_Q) if losses > 0
                              else None),
        },
        # benchmark: the paper-path exposure_matched_midbar_v1 leg does not
        # exist in journal evidence yet — OMITTED, never zero-filled; the
        # evaluator reports missing:benchmark.active_pnl_usd until armed
        # sessions journal it.
        "risk": {
            "max_drawdown_usd": _q(max_drawdown, _USD_Q),
            "max_drawdown_pct_allocated": _q(max_drawdown / allocated,
                                             _PCT_Q),
            "worst_day_usd": _q(worst_day, _USD_Q),
            "worst_day_pct_allocated": _q(abs(worst_day) / allocated,
                                          _PCT_Q),
            "p95_realism_gap_bps": (_q(_p95(abs_divergences), _BPS_Q)
                                    if abs_divergences else None),
            "max_single_fill_divergence_bps": (
                _q(abs_divergences[-1], _BPS_Q) if abs_divergences else None),
        },
        "quality": {
            # only evidence-backed counters; the rest surface as missing:
            "unresolved_reconcile_drift_count": drift_rows,
            "unhandled_exception_count": len(incomplete_dates),
        },
        "thresholds": {
            "min_sessions": PINNED_MIN_SESSIONS,
            "min_trades": PINNED_MIN_TRADES,
            "min_traded_sessions": PINNED_MIN_TRADED_SESSIONS,
            "require_positive_net_pnl": True,
            "require_positive_active_pnl": True,
            "profit_factor_min": str(PINNED_PROFIT_FACTOR_MIN),
            "max_drawdown_pct_allocated": str(
                PINNED_MAX_DRAWDOWN_PCT_ALLOCATED),
            "worst_day_pct_allocated": str(PINNED_WORST_DAY_PCT_ALLOCATED),
            "p95_realism_gap_bps_max": str(PINNED_P95_REALISM_GAP_BPS_MAX),
            "max_single_fill_divergence_bps": str(
                PINNED_MAX_SINGLE_FILL_DIVERGENCE_BPS),
        },
    }
    # None values would read as "present but null" — strip them so the
    # evaluator emits explicit missing: markers instead.
    for section in ("sample", "pnl", "risk"):
        metrics[section] = {k: v for k, v in metrics[section].items()
                            if v is not None}

    return {
        "kind": "paper_phase_report_v1",
        "allocated_notional_usd": str(allocated),
        "window": {"start": start_date, "end": end_date},
        "sessions": {
            "complete": complete_dates,
            "incomplete": incomplete_dates,
            "truncated": truncated_dates,
        },
        "modeled_null_closes": modeled_null_closes,
        "metrics": metrics,
    }


def render_phase_report(report: dict) -> str:
    verdict = evaluate_paper_phase_criteria(report["metrics"])
    sessions = report["sessions"]
    missing = sorted(f for f in verdict.failures if f.startswith("missing:"))
    failed = sorted(f for f in verdict.failures if not f.startswith("missing:"))
    lines = [
        "paper phase report "
        f"(window {report['window']['start']}..{report['window']['end']}, "
        f"allocated {report['allocated_notional_usd']})",
        f"  sessions: complete={len(sessions['complete'])} "
        f"truncated={len(sessions['truncated'])} "
        f"incomplete={len(sessions['incomplete'])}",
        f"  modeled_null_closes={report['modeled_null_closes']}",
        f"  VERDICT: {'PASS' if verdict.passed else 'NOT PASSING'}",
    ]
    if failed:
        lines.append(f"  failed criteria: {', '.join(failed)}")
    if missing:
        lines.append(f"  missing evidence: {', '.join(missing)}")
    return "\n".join(lines) + "\n"


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Aggregate daily paper evidence into the PINNED "
                    "paper-phase criteria verdict. Missing evidence stays "
                    "missing (fail-closed), never a silent zero.")
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--journal-dir", required=True)
    parser.add_argument("--allocated-notional-usd", required=True)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--out", default=None,
                        help="write the full JSON report here")
    args = parser.parse_args(argv)

    report = build_phase_metrics(
        report_dir=args.report_dir, journal_dir=args.journal_dir,
        allocated_notional_usd=args.allocated_notional_usd,
        start_date=args.start_date, end_date=args.end_date)
    verdict = evaluate_paper_phase_criteria(report["metrics"])
    report["verdict"] = {"passed": verdict.passed,
                         "failures": list(verdict.failures)}
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(dumps(report) + "\n", encoding="utf-8")
    print(render_phase_report(report), end="")
    sys.stdout.flush()
    return 0 if verdict.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
