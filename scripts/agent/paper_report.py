"""Daily paper report — a deterministic roll-up of the journal streams.

The dashboard is still the M0 stub, so the paper phase's daily evidence comes
straight from the journals: counts per stream/event, realized broker-vs-modeled
PnL split (never conflated — the S5 posture), reject/exclusion reasons, kill
state, reconcile status, and the live feed's data-quality drop counts. Pure
reads via ``agent.journal.replay`` (hash-verified, truncated tail dropped);
missing streams roll up as zeros — a report can always be produced.

This is an OPERATOR evidence artifact, not the paper-phase criteria evaluator
(``paper_phase_criteria.evaluate_paper_phase_criteria`` owns the formal gate).
"""
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Mapping, Optional

from agent.journal import replay as journal_replay
from agent.serializer import dumps

_STREAMS = ("decisions", "orders", "fills", "positions", "risk",
            "reconcile_alerts", "status")


def _rows(journal_dir: Path, stream: str, session_date_et: Optional[str],
          run_id: Optional[str]) -> list:
    """Rows for THIS session: filtered by run_id when given (the runner's own
    run — journals persist across days for rehydrate continuity), else by the
    row ts_utc date, else everything."""
    path = journal_dir / f"{stream}.jsonl"
    if not path.exists():
        return []
    rows = journal_replay(path)
    if run_id is not None:
        return [row for row in rows if row.get("run_id") == run_id]
    if session_date_et is None:
        return rows
    kept = []
    for row in rows:
        ts = row.get("ts_utc")
        if not isinstance(ts, str) or ts.startswith(session_date_et):
            kept.append(row)
    return kept


def _sum_decimal(rows, key: str) -> Decimal:
    total = Decimal("0")
    for row in rows:
        raw = row.get(key)
        if raw is None:
            continue
        try:
            total += Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError):
            continue
    return total


def _count_by(rows, key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        counts[str(value)] = counts.get(str(value), 0) + 1
    return {k: counts[k] for k in sorted(counts)}


def build_daily_report(journal_dir, *, session_date_et: Optional[str] = None,
                       run_id: Optional[str] = None,
                       mode: Optional[str] = None,
                       kill_state: Optional[str] = None,
                       data_quality_counts: Optional[Mapping[str, int]] = None
                       ) -> dict:
    journal_dir = Path(journal_dir)
    streams = {name: _rows(journal_dir, name, session_date_et, run_id)
               for name in _STREAMS}
    by_type = {
        name: _count_by(rows, "event_type")
        for name, rows in streams.items() if rows
    }

    closes = [row for row in streams["positions"]
              if row.get("event_type") == "position_close"]
    opens = [row for row in streams["positions"]
             if row.get("event_type") == "position_open"]
    fees = Decimal("0")
    for row in closes:
        assessed = row.get("fees_assessed")
        if isinstance(assessed, Mapping) and assessed.get("total_usd"):
            try:
                fees += Decimal(str(assessed["total_usd"]))
            except (InvalidOperation, ValueError):
                pass

    reject_reasons: Dict[str, int] = {}
    for row in streams["orders"]:
        if row.get("event_type") != "reject":
            continue
        for reason in row.get("reasons") or ():
            reject_reasons[str(reason)] = reject_reasons.get(str(reason), 0) + 1
    verdict_reasons: Dict[str, int] = {}
    for row in streams["risk"]:
        if row.get("event_type") != "risk_verdict":
            continue
        for reason in row.get("reasons") or ():
            verdict_reasons[str(reason)] = (
                verdict_reasons.get(str(reason), 0) + 1)

    kill_rows = [row for row in streams["risk"]
                 if row.get("event_type") == "kill_switch_transition"]
    reconcile_runs = [row for row in streams["reconcile_alerts"]
                      if row.get("event_type") == "reconcile_run"]
    drift_rows = [row for row in streams["reconcile_alerts"]
                  if row.get("event_type") == "reconcile"]

    report = {
        "kind": "paper_daily_report_v1",
        "session_date_et": session_date_et,
        "run_id": run_id,
        "mode": mode,
        "journal_dir": str(journal_dir),
        "counts_by_stream": by_type,
        "trading": {
            "position_opens": len(opens),
            "position_closes": len(closes),
            "realized_broker_pnl_usd": str(_sum_decimal(
                closes, "realized_broker_pnl")),
            "realized_modeled_pnl_usd": str(_sum_decimal(
                closes, "realized_modeled_pnl")),
            "fees_usd": str(fees),
            "closes_by_reason": _count_by(closes, "reason"),
        },
        "fills": {
            "broker_fills": by_type.get("fills", {}).get("broker_fill", 0),
            "modeled_fills": by_type.get("fills", {}).get(
                "modeled_execution_fill", 0),
            "divergence_rows": by_type.get("fills", {}).get(
                "fill_divergence", 0),
        },
        "rejects": {
            "order_reject_reasons": {k: reject_reasons[k]
                                     for k in sorted(reject_reasons)},
            "risk_verdict_reasons": {k: verdict_reasons[k]
                                     for k in sorted(verdict_reasons)},
        },
        "kill": {
            "state": kill_state,
            "transitions": [
                {"from": row.get("from_state"), "to": row.get("to_state"),
                 "cause": row.get("cause")} for row in kill_rows],
            "eval_skipped_rows": by_type.get("risk", {}).get(
                "kill_eval_skipped", 0),
        },
        "reconcile": {
            "runs": len(reconcile_runs),
            "all_clean": (all(bool(row.get("clean")) for row in reconcile_runs)
                          if reconcile_runs else None),
            "drift_rows": len(drift_rows),
        },
        "data_quality": dict(data_quality_counts or {}),
    }
    return report


def render_text(report: Mapping) -> str:
    trading = report.get("trading", {})
    reconcile = report.get("reconcile", {})
    kill = report.get("kill", {})
    lines = [
        f"paper daily report — {report.get('session_date_et')} "
        f"(mode={report.get('mode')}, run_id={report.get('run_id')})",
        f"  opens={trading.get('position_opens')} "
        f"closes={trading.get('position_closes')} "
        f"broker_pnl={trading.get('realized_broker_pnl_usd')} "
        f"modeled_pnl={trading.get('realized_modeled_pnl_usd')} "
        f"fees={trading.get('fees_usd')}",
        f"  fills: broker={report.get('fills', {}).get('broker_fills')} "
        f"modeled={report.get('fills', {}).get('modeled_fills')} "
        f"divergence_rows={report.get('fills', {}).get('divergence_rows')}",
        f"  kill: state={kill.get('state')} "
        f"transitions={len(kill.get('transitions', []))} "
        f"eval_skipped={kill.get('eval_skipped_rows')}",
        f"  reconcile: runs={reconcile.get('runs')} "
        f"all_clean={reconcile.get('all_clean')} "
        f"drift_rows={reconcile.get('drift_rows')}",
        f"  data_quality: {report.get('data_quality')}",
    ]
    return "\n".join(lines) + "\n"


def write_report(report: Mapping, path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(dumps(dict(report)) + "\n", encoding="utf-8")
    return output
