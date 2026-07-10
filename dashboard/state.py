"""Dashboard state builder — a READ-ONLY, hash-verified journal roll-up.

Feeds the local live view: incremental readers (``read_new`` deltas) per
journal stream accumulate into one JSON-friendly snapshot the browser polls.
Strictly observational: no broker, no gates, no writes — the dashboard can
never affect a session. BrokerUSD and ModeledUSD stay SEPARATE (S5).

A live session appends to the journals while we read; the incremental reader
is integrity-verified and treats a partial trailing line as pending, so a
concurrent snapshot is always a consistent prefix. JournalCorruption is
REPORTED in the snapshot (and the stream retried from scratch next poll)
rather than crashing the view.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Optional

from agent.journal import IncrementalJournalReader, JournalCorruption

_STREAMS = ("decisions", "orders", "fills", "positions", "risk",
            "reconcile_alerts", "status", "status_plane")


def _dec(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _tail(rows, limit):
    return rows[-limit:] if limit and len(rows) > limit else list(rows)


class JournalStateSource:
    """Accumulates journal rows incrementally and serves snapshots."""

    def __init__(self, journal_dir, report_dir=None) -> None:
        self._journal_dir = Path(journal_dir)
        self._report_dir = Path(report_dir) if report_dir else None
        self._readers: Dict[str, IncrementalJournalReader] = {}
        self._rows: Dict[str, list] = {name: [] for name in _STREAMS}
        self._corruption: Dict[str, str] = {}

    def _refresh(self) -> None:
        for name in _STREAMS:
            path = self._journal_dir / f"{name}.jsonl"
            if not path.exists():
                continue
            reader = self._readers.get(name)
            if reader is None:
                reader = IncrementalJournalReader(path)
                self._readers[name] = reader
            try:
                replaced, fresh = reader.read_new()
            except JournalCorruption as exc:
                self._corruption[name] = str(exc)
                # retry from scratch on the next poll (the writer may have
                # completed a partial line, or the operator may have fixed it)
                self._readers.pop(name, None)
                continue
            self._corruption.pop(name, None)
            if replaced:
                self._rows[name] = fresh
            else:
                self._rows[name].extend(fresh)

    def snapshot(self, *, limit: int = 50) -> dict:
        self._refresh()
        rows = self._rows

        positions_rows = rows["positions"]
        opens = [r for r in positions_rows
                 if r.get("event_type") == "position_open"]
        closes = [r for r in positions_rows
                  if r.get("event_type") == "position_close"]
        open_ids = {r.get("position_id") for r in opens}
        closed_ids = {r.get("position_id") for r in closes}
        live_ids = {pid for pid in open_ids - closed_ids if pid is not None}
        live_positions = [
            {"symbol": r.get("symbol"), "qty": str(r.get("qty")),
             "opened_ts_utc": r.get("opened_ts_utc"),
             "broker_cost_usd": r.get("broker_cost_usd"),
             "modeled_cost_usd": r.get("modeled_cost_usd"),
             "position_id": r.get("position_id")}
            for r in opens if r.get("position_id") in live_ids
        ]

        broker_pnl = Decimal("0")
        modeled_pnl = Decimal("0")
        modeled_null_closes = 0
        fees = Decimal("0")
        for row in closes:
            value = _dec(row.get("realized_broker_pnl"))
            if value is not None:
                broker_pnl += value
            modeled = _dec(row.get("realized_modeled_pnl"))
            if modeled is None:
                modeled_null_closes += 1
            else:
                modeled_pnl += modeled
            assessed = row.get("fees_assessed")
            if isinstance(assessed, dict):
                fee = _dec(assessed.get("total_usd"))
                if fee is not None:
                    fees += fee

        orders_rows = rows["orders"]
        order_events = [
            {"event_type": r.get("event_type"), "symbol": r.get("symbol"),
             "side": r.get("side"), "qty": str(r.get("qty")),
             "limit_price": r.get("limit_price"),
             "status": r.get("status"),
             "reasons": list(r.get("reasons") or ()),
             "ts_utc": r.get("ts_utc"),
             "decision_id": r.get("decision_id")}
            for r in orders_rows
        ]
        reject_reasons: Dict[str, int] = {}
        for row in orders_rows:
            if row.get("event_type") != "reject":
                continue
            for reason in row.get("reasons") or ():
                key = str(reason)
                reject_reasons[key] = reject_reasons.get(key, 0) + 1

        fills_rows = rows["fills"]
        fill_events = [
            {"event_type": r.get("event_type"), "symbol": r.get("symbol"),
             "qty": str(r.get("qty")), "price": r.get("price"),
             "divergence_bps": (r.get("fill_divergence") or {}).get(
                 "divergence_bps") if isinstance(
                     r.get("fill_divergence"), dict) else r.get(
                         "divergence_bps"),
             "ts_utc": r.get("ts_utc")}
            for r in fills_rows
        ]
        fill_counts = {"broker_fill": 0, "modeled_execution_fill": 0,
                       "fill_divergence": 0}
        for row in fills_rows:
            key = row.get("event_type")
            if key in fill_counts:
                fill_counts[key] += 1

        decisions_rows = rows["decisions"]
        decisions = [
            {"event_type": r.get("event_type"),
             "action": r.get("action"),
             "symbol": r.get("symbol") or _symbol_from_bar_key(r),
             "decision_ts_utc": r.get("decision_ts_utc") or r.get("ts_utc"),
             "edge_label": r.get("edge_label"),
             "decision_id": r.get("decision_id")}
            for r in decisions_rows
        ]

        risk_rows = rows["risk"]
        kill_state = "monitoring"
        kill_transitions = []
        for row in risk_rows:
            if row.get("event_type") == "kill_switch_transition":
                kill_state = row.get("to_state") or kill_state
                kill_transitions.append({
                    "from": row.get("from_state"), "to": row.get("to_state"),
                    "cause": row.get("cause"), "ts_utc": row.get("ts_utc")})

        status_rows = rows["status_plane"]
        status_transitions = [
            {"symbol": r.get("symbol"), "field": r.get("field"),
             "from": r.get("from"), "to": r.get("to"),
             "source": r.get("source"), "ts_utc": r.get("ts_utc")}
            for r in status_rows
        ]

        run_ids = []
        for name in _STREAMS:
            for row in rows[name]:
                run_id = row.get("run_id")
                if run_id and run_id not in run_ids:
                    run_ids.append(run_id)

        reconcile_rows = rows["reconcile_alerts"]
        reconcile_runs = [r for r in reconcile_rows
                          if r.get("event_type") == "reconcile_run"]
        drift_rows = [r for r in reconcile_rows
                      if r.get("event_type") == "reconcile"]

        return {
            "generated_utc": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%f") + "Z",
            "journal_dir": str(self._journal_dir),
            "run_ids": run_ids,
            "kill": {"state": kill_state,
                     "transitions": _tail(kill_transitions, limit)},
            "pnl": {
                # NEVER conflated: broker ledger truth vs modeled label (S5)
                "realized_broker_pnl_usd": str(broker_pnl),
                "realized_modeled_pnl_usd": str(modeled_pnl),
                "modeled_null_closes": modeled_null_closes,
                "fees_usd": str(fees),
            },
            "positions": {
                "open_count": len(live_positions),
                "live": _tail(live_positions, limit),
                "opens": len(opens),
                "closes": len(closes),
            },
            "orders": {"recent": _tail(order_events, limit),
                       "total": len(order_events),
                       "reject_reasons": reject_reasons},
            "fills": {"recent": _tail(fill_events, limit),
                      "counts": fill_counts},
            "decisions": {"recent": _tail(decisions, limit),
                          "total": len(decisions)},
            "status_plane": {"recent": _tail(status_transitions, limit),
                             "total": len(status_transitions)},
            "reconcile": {"runs": len(reconcile_runs),
                          "drift_rows": len(drift_rows)},
            "corruption": dict(self._corruption),
            "reports": self._report_summaries(),
        }

    def _report_summaries(self) -> list:
        if self._report_dir is None or not self._report_dir.exists():
            return []
        import json

        summaries = []
        for path in sorted(self._report_dir.glob("*.json")):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                summaries.append({"file": path.name, "error": "unreadable"})
                continue
            summaries.append({
                "file": path.name,
                "session_date_et": report.get("session_date_et"),
                "mode": report.get("mode"),
                "session": report.get("session"),
                "session_incomplete": report.get("session_incomplete", False),
                "trading": report.get("trading"),
                "kill_state": (report.get("kill") or {}).get("state"),
                "data_quality": report.get("data_quality"),
            })
        return summaries


def _symbol_from_bar_key(row) -> Optional[str]:
    for key in ("event_basis", "event_start_bar_key"):
        value = row.get(key)
        if isinstance(value, str) and "|" in value:
            return value.split("|", 1)[0]
    return None
