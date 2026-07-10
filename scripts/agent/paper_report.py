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
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Mapping, Optional

from agent.journal import replay as journal_replay
from agent.serializer import dumps

_STREAMS = ("decisions", "orders", "fills", "positions", "risk",
            "reconcile_alerts", "status")

try:
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover — missing tz database on an exotic host
    _ET = None


def _et_date(ts_utc: str) -> Optional[str]:
    """ET civil date for a pinned-format UTC timestamp; None if unparseable."""
    try:
        instant = datetime.strptime(
            ts_utc, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if _ET is None:  # pragma: no cover
        return ts_utc[:10]
    return instant.astimezone(_ET).date().isoformat()


def _rows(journal_dir: Path, stream: str, session_date_et: Optional[str],
          run_id: Optional[str]) -> list:
    """Rows for THIS session: filtered by run_id when given (the runner's own
    run — journals persist across days for rehydrate continuity), else by the
    row's ET date (evidence written 20:00–24:00 ET lands on the NEXT UTC date
    but belongs to this session), else everything. Rows without a parseable
    ts_utc are kept — never silently lose evidence."""
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
        if not isinstance(ts, str):
            kept.append(row)
            continue
        et_date = _et_date(ts)
        if et_date is None:
            if ts.startswith(session_date_et):
                kept.append(row)
            continue
        if et_date == session_date_et:
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
                       data_quality_counts: Optional[Mapping[str, int]] = None,
                       ticks_run: Optional[int] = None,
                       sod_clean: Optional[bool] = None,
                       eod_clean: Optional[bool] = None,
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
            # Closes with NO modeled PnL vanish from the modeled sum above; a
            # visible counter keeps the modeled/broker split honest for the
            # weekly aggregator (missing evidence is never a silent zero).
            "modeled_null_closes": sum(
                1 for row in closes
                if row.get("realized_modeled_pnl") is None),
            "fees_usd": str(fees),
            "closes_by_reason": _count_by(closes, "reason"),
        },
        "session": {
            "ticks_run": ticks_run,
            "sod_clean": sod_clean,
            "eod_clean": eod_clean,
            "feed_truncated": bool(
                (data_quality_counts or {}).get("source_exhausted_early")),
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
    session = report.get("session", {})
    lines = [
        f"paper daily report — {report.get('session_date_et')} "
        f"(mode={report.get('mode')}, run_id={report.get('run_id')})",
        f"  session: ticks={session.get('ticks_run')} "
        f"sod_clean={session.get('sod_clean')} "
        f"eod_clean={session.get('eod_clean')} "
        f"feed_truncated={session.get('feed_truncated')}",
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


def next_report_path(report_dir, session_date_et: str) -> Path:
    """Restart-safe report path: `<date>.json`, then `<date>.1.json`, … — a
    same-day restart must never overwrite the first attempt's evidence."""
    directory = Path(report_dir)
    candidate = directory / f"{session_date_et}.json"
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = directory / f"{session_date_et}.{suffix}.json"
    return candidate


def write_report(report: Mapping, path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(dumps(dict(report)) + "\n", encoding="utf-8")
    return output
