"""M4 §K — RiskLedger: the validating facade over `recorder.persistence.EventWriter`
for `journal/risk.jsonl` (the status_ledger.py:211-219 pattern). NO new writer, hash,
or serialization; one ledger per resolved path; injected `run_id`; `rules_hash` on
every row; `"v"` is the FIRST payload key (`RISK_LEDGER_VERSION`).

Money-field discipline (FD-M4-25/LD-R5): every REHYDRATE-BEARING model-state money
field is journaled EXACT (unquantized canonical Decimal-string via the serializer);
`USD_QUANTUM` quantize-only applies ONLY to the `account_snapshot` provenance row's
money fields (nothing rehydrates from that row — broker payloads may carry sub-cent
precision and a round-trip requirement would reject ground truth).
"""
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Optional

from agent.journal import JournalCorruption  # re-exported; either import path is valid
from agent.risk.account_state import USD_QUANTUM, AccountInvalid, BrokerAccountRead
from agent.risk.reasons import (
    ACCOUNT_STATUSES,
    GATE_STAGES,
    PDT_STATES,
    RiskError,
    require_kill_cause,
    require_kill_state,
    require_pdt_state,
    require_reason,
)
from recorder.persistence import EventWriter, replay_stream

__all__ = [
    "STREAM_RISK",
    "RISK_LEDGER_VERSION",
    "EVT_RISK_VERDICT",
    "EVT_ACCOUNT_SNAPSHOT",
    "EVT_ACCOUNT_ALERT",
    "EVT_KILL_TRANSITION",
    "EVT_KILL_RETRIP",
    "EVT_KILL_RETRY",
    "EVT_KILL_FLATTEN_INCOMPLETE",
    "EVT_KILL_EVAL_SKIPPED",
    "EVT_IML_OBSERVATION",
    "EVT_MARGIN_DEFICIT",
    "EVT_DEFICIT_SATISFIED",
    "EVT_DEFICIT_EXPIRED",
    "EVT_DEFICIT_UNBASELINED",
    "EVT_MARGIN_WINDOW_UNRESOLVED",
    "EVT_FREEZE_START",
    "EVT_FREEZE_END",
    "EVT_PDT_TRANSITION",
    "EVT_HWM_UPDATE",
    "RiskLedger",
    "replay_risk",
    "rehydrate_risk_state",
    "EventWriter",
    "JournalCorruption",
]

STREAM_RISK = "risk"
RISK_LEDGER_VERSION = 1

# Persisted-value arithmetic runs under THIS pinned context, never the ambient one
# (M3 harden-round-1 precedent, quote_quality.py:23; M4-DET-1).
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)
EVT_RISK_VERDICT = "risk_verdict"
EVT_ACCOUNT_SNAPSHOT = "account_snapshot"
EVT_ACCOUNT_ALERT = "account_alert"
EVT_KILL_TRANSITION = "kill_switch_transition"
EVT_KILL_RETRIP = "kill_retrip"
EVT_KILL_RETRY = "kill_retry_residual"
EVT_KILL_FLATTEN_INCOMPLETE = "kill_flatten_incomplete"
EVT_KILL_EVAL_SKIPPED = "kill_eval_skipped"
EVT_IML_OBSERVATION = "iml_observation"
EVT_MARGIN_DEFICIT = "margin_deficit_detected"
EVT_DEFICIT_SATISFIED = "margin_deficit_satisfied"
EVT_DEFICIT_EXPIRED = "margin_deficit_expired"
EVT_DEFICIT_UNBASELINED = "margin_deficit_unbaselined"      # M4C-2/RM-6 (rev 2)
EVT_MARGIN_WINDOW_UNRESOLVED = "margin_window_unresolved"   # safety-F11 (rev 2)
EVT_FREEZE_START = "margin_freeze_start"
EVT_FREEZE_END = "margin_freeze_end"
EVT_PDT_TRANSITION = "pdt_regime_transition"
EVT_HWM_UPDATE = "loss_hwm_update"

# "window_resolved" (harden round 1, M4-R1): a re-emission of the detected-row
# fields carrying NEWLY-RESOLVED bd5/bd15 windows for a record whose detection-time
# computation failed (safety-F11) — the §K.4 field-merge overlays the nulls.
_DEFICIT_CAUSES = frozenset({"opened", "increased", "window_resolved"})
_WINDOW_ERRORS = frozenset({"unknown_session_date", "scan_limit_exceeded"})
_ALERT_TRANSITIONS = frozenset({"degraded", "recovered"})
_PDT_EVIDENCE = frozenset({"account_flag", "broker_rejection"})
_SATISFACTION_BASES = frozenset({"eod_iml_delta"})


def _exact_decimal(value, *, field: str, allow_none: bool = False) -> Optional[Decimal]:
    """A REHYDRATE-BEARING money field: EXACT, unquantized (LD-R5). Rejects
    float/bool/non-finite up front (S2)."""
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{field}: required non-null Decimal (got None)")
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, Decimal):
        raise ValueError(f"{field}: must be a Decimal (got {type(value).__name__}={value!r})")
    if not value.is_finite():
        raise ValueError(f"{field}: non-finite Decimal not allowed ({value!r})")
    return value


# Pinned context for the journaling-path quantize (harden round 1, M4-DET-1): a
# caller-mutated ambient context must never make a VALID snapshot fail to journal.
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


def _quantized_usd(value, *, field: str) -> Optional[Decimal]:
    """account_snapshot-row-ONLY: USD_QUANTUM quantize-only ROUND_HALF_EVEN (no
    round-trip check — broker payloads may legitimately carry sub-cent precision)."""
    if value is None:
        return None
    d = _exact_decimal(value, field=field)
    with localcontext(_DECIMAL_CTX):
        return d.quantize(USD_QUANTUM, rounding=ROUND_HALF_EVEN)


def _require_bool(value, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise RiskError(f"{field}: must be a bool (got {type(value).__name__})")
    return value


def _sorted_str_list(values, *, field: str) -> list:
    out = list(values)
    for item in out:
        if not isinstance(item, str):
            raise RiskError(f"{field}: members must be strings (got {item!r})")
    return sorted(out)


def _pair_list(values, *, field: str) -> list:
    out = []
    for item in values:
        pair = list(item)
        if len(pair) != 2 or not all(isinstance(part, str) for part in pair):
            raise RiskError(f"{field}: members must be (str, str) pairs (got {item!r})")
        out.append(pair)
    return sorted(out)


class RiskLedger:
    """One kwarg-only record_* method per event type (StatusLedger shape); each
    validates vocabularies (reasons ⊆ RISK_REASONS, states/causes in their frozensets,
    reserved codes refused in M4), applies the §K.2 money discipline, and never names
    a journal `_RESERVED` key in a payload."""

    def __init__(self, writer: EventWriter, *, rules_hash: str) -> None:
        self._writer = writer
        self._rules_hash = rules_hash

    def _record(self, event_type: str, body: dict, *, decision_id=None) -> dict:
        fields = {"v": RISK_LEDGER_VERSION, **body, "rules_hash": self._rules_hash}
        return self._writer.record(event_type, fields, decision_id=decision_id)

    # --- verdicts -------------------------------------------------------------

    def record_risk_verdict(self, verdict, *, decision_id: Optional[str] = None) -> dict:
        reasons = [require_reason(code) for code in verdict.reasons]
        if (verdict.allowed is True) != (len(reasons) == 0):
            raise RiskError("RiskVerdict invariant broken: allowed <=> reasons == ()")
        gate_stage = verdict.gate_stage
        if gate_stage is not None and gate_stage not in GATE_STAGES:
            raise RiskError(f"out-of-vocabulary gate_stage: {gate_stage!r}")
        for stage in verdict.stages_skipped:
            if stage not in GATE_STAGES:
                raise RiskError(f"out-of-vocabulary skipped stage: {stage!r}")
        require_kill_state(verdict.kill_state)
        legs = []
        for leg in verdict.legs:
            legs.append({
                "symbol": leg.symbol,
                "instrument_id": leg.instrument_id,
                "side": leg.side,
                "classification": leg.classification,
                "qty": _exact_decimal(leg.qty, field="leg.qty"),
                "limit_price": _exact_decimal(leg.limit_price, field="leg.limit_price",
                                              allow_none=True),
                "cap_notional": _exact_decimal(leg.cap_notional, field="leg.cap_notional",
                                               allow_none=True),
                "mark_used": _require_bool(leg.mark_used, field="leg.mark_used"),
            })
        caps_used = [list(row) for row in verdict.caps_used]
        body = {
            "verdict_id": verdict.verdict_id,
            "allowed": _require_bool(verdict.allowed, field="allowed"),
            "reasons": sorted(reasons),
            "gate_stage": gate_stage,
            "stages_skipped": list(verdict.stages_skipped),
            "strategy_id": verdict.strategy_id,
            "legs": legs,
            "gross_notional": _exact_decimal(verdict.gross_notional,
                                             field="gross_notional"),
            "caps_used": caps_used,
            "account_snapshot_id": verdict.account_snapshot_id,
            "kill_state": verdict.kill_state,
            "kill_generation": int(verdict.kill_generation),
            "session_date_et": verdict.session_date_et,
        }
        return self._record(EVT_RISK_VERDICT, body, decision_id=decision_id)

    # --- account --------------------------------------------------------------

    def record_account_snapshot(self, *, result) -> dict:
        """Frozen CALLER obligation: written on every `AccountStore.put`, valid and
        invalid alike (F4). The put-time `status` pair is ONLY {fresh, invalid} —
        staleness/skew are get-time concepts and never appear on this row."""
        if isinstance(result, BrokerAccountRead):
            body = {
                "account_snapshot_id": result.account_snapshot_id,
                "status": "fresh",
                "equity": _quantized_usd(result.equity, field="equity"),
                "last_equity": _quantized_usd(result.last_equity, field="last_equity"),
                "cash": _quantized_usd(result.cash, field="cash"),
                "buying_power": _quantized_usd(result.buying_power, field="buying_power"),
                "maintenance_margin": _quantized_usd(result.maintenance_margin,
                                                     field="maintenance_margin"),
                "daytrading_buying_power": _quantized_usd(
                    result.daytrading_buying_power, field="daytrading_buying_power"),
                "pattern_day_trader": result.pattern_day_trader,
                "daytrade_count": result.daytrade_count,
                "multiplier": _exact_decimal(result.multiplier, field="multiplier",
                                             allow_none=True),
                "source": result.source,
                "ts_read_utc": result.ts_read_utc,
                "invalid_reason": None,
            }
        elif isinstance(result, AccountInvalid):
            body = {
                "account_snapshot_id": None,   # no id is derivable from an invalid put (F4)
                "status": "invalid",
                "equity": None, "last_equity": None, "cash": None,
                "buying_power": None, "maintenance_margin": None,
                "daytrading_buying_power": None, "pattern_day_trader": None,
                "daytrade_count": None, "multiplier": None,
                "source": result.source,
                "ts_read_utc": None,
                "invalid_reason": result.reason,
            }
        else:
            raise RiskError(
                f"record_account_snapshot takes BrokerAccountRead | AccountInvalid, "
                f"got {type(result).__name__}")
        return self._record(EVT_ACCOUNT_SNAPSHOT, body)

    def record_account_alert(self, *, transition: str, status: str,
                             age_ms: Optional[int] = None,
                             invalid_reason: Optional[str] = None) -> dict:
        if transition not in _ALERT_TRANSITIONS:
            raise RiskError(f"out-of-vocabulary account_alert transition: {transition!r}")
        if status not in ACCOUNT_STATUSES:
            raise RiskError(f"out-of-vocabulary account status: {status!r}")
        body = {"transition": transition, "status": status,
                "age_ms": age_ms, "invalid_reason": invalid_reason}
        return self._record(EVT_ACCOUNT_ALERT, body)

    # --- kill switch ------------------------------------------------------------

    def record_kill_transition(self, *, from_state: str, to_state: str, cause: str,
                               generation: int,
                               daily_loss_usd: Optional[Decimal] = None,
                               drawdown_usd: Optional[Decimal] = None,
                               cap_usd: Optional[Decimal] = None,
                               account_snapshot_id: Optional[str] = None,
                               stale_inputs: bool, flattened, failed, residual,
                               tradability_annotations) -> dict:
        require_kill_state(from_state)
        require_kill_state(to_state)
        require_kill_cause(cause)
        body = {
            "from_state": from_state,
            "to_state": to_state,
            "cause": cause,
            "generation": int(generation),
            "daily_loss_usd": _exact_decimal(daily_loss_usd, field="daily_loss_usd",
                                             allow_none=True),
            "drawdown_usd": _exact_decimal(drawdown_usd, field="drawdown_usd",
                                           allow_none=True),
            "cap_usd": _exact_decimal(cap_usd, field="cap_usd", allow_none=True),
            "account_snapshot_id": account_snapshot_id,
            "stale_inputs": _require_bool(stale_inputs, field="stale_inputs"),
            "flattened": _sorted_str_list(flattened, field="flattened"),
            "failed": _pair_list(failed, field="failed"),
            "residual": _sorted_str_list(residual, field="residual"),
            "tradability_annotations": _pair_list(tradability_annotations,
                                                  field="tradability_annotations"),
        }
        return self._record(EVT_KILL_TRANSITION, body)

    def record_kill_retrip(self, *, cause: str, generation: int,
                           current_state: str) -> dict:
        require_kill_cause(cause)
        require_kill_state(current_state)
        return self._record(EVT_KILL_RETRIP, {
            "cause": cause, "generation": int(generation),
            "current_state": current_state})

    def record_kill_retry_residual(self, *, generation: int, residual_before,
                                   residual_after, flattened, failed) -> dict:
        return self._record(EVT_KILL_RETRY, {
            "generation": int(generation),
            "residual_before": _sorted_str_list(residual_before, field="residual_before"),
            "residual_after": _sorted_str_list(residual_after, field="residual_after"),
            "flattened": _sorted_str_list(flattened, field="flattened"),
            "failed": _pair_list(failed, field="failed")})

    def record_kill_flatten_incomplete(self, *, generation: int, failed,
                                       residual) -> dict:
        return self._record(EVT_KILL_FLATTEN_INCOMPLETE, {
            "generation": int(generation),
            "failed": _pair_list(failed, field="failed"),
            "residual": _sorted_str_list(residual, field="residual")})

    def record_kill_eval_skipped(self, *, account_status: str, generation: int) -> dict:
        if account_status not in ACCOUNT_STATUSES:
            raise RiskError(f"out-of-vocabulary account status: {account_status!r}")
        return self._record(EVT_KILL_EVAL_SKIPPED, {
            "account_status": account_status, "generation": int(generation)})

    # --- intraday margin --------------------------------------------------------

    def record_iml_observation(self, *, session_date_et: str, ts_market_utc: str,
                               equity: Decimal, maintenance_margin: Decimal,
                               iml: Decimal, deficiency: Decimal,
                               after_iml_reducing: bool, eod: bool,
                               account_snapshot_id: str) -> dict:
        return self._record(EVT_IML_OBSERVATION, {
            "session_date_et": session_date_et,
            "ts_market_utc": ts_market_utc,
            "equity": _exact_decimal(equity, field="equity"),
            "maintenance_margin": _exact_decimal(maintenance_margin,
                                                 field="maintenance_margin"),
            "iml": _exact_decimal(iml, field="iml"),
            "deficiency": _exact_decimal(deficiency, field="deficiency"),
            "after_iml_reducing": _require_bool(after_iml_reducing,
                                                field="after_iml_reducing"),
            "eod": _require_bool(eod, field="eod"),
            "account_snapshot_id": account_snapshot_id})

    def record_margin_deficit_detected(self, *, deficit_id: str, cause: str,
                                       session_date_et: str, amount: Decimal,
                                       minor: bool, equity_at_detection: Decimal,
                                       satisfaction_deadline_et: Optional[str],
                                       expires_after_et: Optional[str]) -> dict:
        if cause not in _DEFICIT_CAUSES:
            raise RiskError(f"out-of-vocabulary deficit cause: {cause!r}")
        return self._record(EVT_MARGIN_DEFICIT, {
            "deficit_id": deficit_id,
            "cause": cause,
            "session_date_et": session_date_et,
            "amount": _exact_decimal(amount, field="amount"),
            "minor": _require_bool(minor, field="minor"),
            "equity_at_detection": _exact_decimal(equity_at_detection,
                                                  field="equity_at_detection"),
            "satisfaction_deadline_et": satisfaction_deadline_et,
            "expires_after_et": expires_after_et})

    def record_margin_deficit_satisfied(self, *, deficit_id: str, session_date_et: str,
                                        satisfied_on_et: str, iml_eod_d: Decimal,
                                        iml_eod_e: Decimal,
                                        basis: str = "eod_iml_delta") -> dict:
        if basis not in _SATISFACTION_BASES:
            raise RiskError(f"out-of-vocabulary satisfaction basis: {basis!r}")
        return self._record(EVT_DEFICIT_SATISFIED, {
            "deficit_id": deficit_id,
            "session_date_et": session_date_et,
            "satisfied_on_et": satisfied_on_et,
            "iml_eod_d": _exact_decimal(iml_eod_d, field="iml_eod_d"),
            "iml_eod_e": _exact_decimal(iml_eod_e, field="iml_eod_e"),
            "basis": basis})

    def record_margin_deficit_expired(self, *, deficit_id: str, session_date_et: str,
                                      expires_after_et: str) -> dict:
        return self._record(EVT_DEFICIT_EXPIRED, {
            "deficit_id": deficit_id,
            "session_date_et": session_date_et,
            "expires_after_et": expires_after_et})

    def record_margin_deficit_unbaselined(self, *, deficit_id: str,
                                          session_date_et: str,
                                          noted_at_close_et: str) -> dict:
        return self._record(EVT_DEFICIT_UNBASELINED, {
            "deficit_id": deficit_id,
            "session_date_et": session_date_et,
            "noted_at_close_et": noted_at_close_et})

    def record_margin_window_unresolved(self, *, deficit_id: str, session_date_et: str,
                                        error: str) -> dict:
        if error not in _WINDOW_ERRORS:
            raise RiskError(f"out-of-vocabulary window error: {error!r}")
        return self._record(EVT_MARGIN_WINDOW_UNRESOLVED, {
            "deficit_id": deficit_id,
            "session_date_et": session_date_et,
            "error": error})

    def record_margin_freeze_start(self, *, trigger_deficit_id: str,
                                   effective_from_et: str, expires_on_et: str) -> dict:
        return self._record(EVT_FREEZE_START, {
            "trigger_deficit_id": trigger_deficit_id,
            "effective_from_et": effective_from_et,
            "expires_on_et": expires_on_et})

    def record_margin_freeze_end(self, *, trigger_deficit_id: Optional[str],
                                 expires_on_et: str) -> dict:
        return self._record(EVT_FREEZE_END, {
            "trigger_deficit_id": trigger_deficit_id,
            "expires_on_et": expires_on_et})

    # --- pdt / loss ---------------------------------------------------------------

    def record_pdt_transition(self, *, from_state: str, to_state: str, evidence: str,
                              rejection_code: Optional[int] = None,
                              pattern_day_trader: Optional[bool] = None,
                              daytrade_count: Optional[int] = None) -> dict:
        require_pdt_state(from_state)
        require_pdt_state(to_state)
        if evidence not in _PDT_EVIDENCE:
            raise RiskError(f"out-of-vocabulary pdt evidence: {evidence!r}")
        return self._record(EVT_PDT_TRANSITION, {
            "from_state": from_state,
            "to_state": to_state,
            "evidence": evidence,
            "rejection_code": rejection_code,
            "pattern_day_trader": pattern_day_trader,
            "daytrade_count": daytrade_count})

    def record_loss_hwm_update(self, *, session_date_et: str, hwm_equity: Decimal,
                               equity: Decimal, account_snapshot_id: str) -> dict:
        return self._record(EVT_HWM_UPDATE, {
            "session_date_et": session_date_et,
            "hwm_equity": _exact_decimal(hwm_equity, field="hwm_equity"),
            "equity": _exact_decimal(equity, field="equity"),
            "account_snapshot_id": account_snapshot_id})


def replay_risk(path) -> list:
    """Re-read journal/risk.jsonl hash-verified — SAME truncated-tail +
    JournalCorruption semantics as every other stream (S3)."""
    return replay_stream(path)


def rehydrate_risk_state(rows) -> dict:
    """PURE fold in ascending journal `seq` (status_ledger.py:356-371 shape),
    latest-row-wins per key, with the per-deficit_id FIELD-merge (F13): detected rows
    supply amount/minor/equity/deadlines; satisfied/expired/unbaselined/window rows
    OVERLAY only their own fields. A kill slice whose LATEST transition row is
    monitoring→flattening folds to HALTED + that row's residual[] (safety-F5)."""
    kill = {"state": "monitoring", "generation": 0, "residual": []}
    deficits: dict = {}
    iml_eod: dict = {}
    freeze = None
    pdt = {"state": "unknown", "rejection_latched": False}
    loss = {"hwm_equity": None}

    for row in sorted(rows, key=lambda r: r["seq"]):
        event_type = row.get("event_type")
        if event_type == EVT_KILL_TRANSITION:
            kill = {"state": row["to_state"], "generation": row["generation"],
                    "residual": list(row["residual"])}
        elif event_type == EVT_KILL_RETRY:
            kill["generation"] = row["generation"]
            kill["residual"] = list(row["residual_after"])
        elif event_type == EVT_MARGIN_DEFICIT:
            record = deficits.setdefault(row["deficit_id"], {})
            record.update(
                deficit_id=row["deficit_id"],
                session_date_et=row["session_date_et"],
                amount=row["amount"],
                minor=row["minor"],
                equity_at_detection=row["equity_at_detection"],
                satisfaction_deadline_et=row["satisfaction_deadline_et"],
                expires_after_et=row["expires_after_et"])
        elif event_type == EVT_DEFICIT_SATISFIED:
            record = deficits.setdefault(row["deficit_id"], {})
            record["satisfied_on_et"] = row["satisfied_on_et"]
        elif event_type == EVT_DEFICIT_EXPIRED:
            record = deficits.setdefault(row["deficit_id"], {})
            record["expired_noted"] = True
        elif event_type == EVT_DEFICIT_UNBASELINED:
            record = deficits.setdefault(row["deficit_id"], {})
            record["unbaselined_noted"] = True
        elif event_type == EVT_MARGIN_WINDOW_UNRESOLVED:
            record = deficits.setdefault(row["deficit_id"], {})
            record["window_unresolved"] = row["error"]
        elif event_type == EVT_IML_OBSERVATION and row["eod"] is True:
            iml_eod[row["session_date_et"]] = row["iml"]
        elif event_type == EVT_FREEZE_START:
            freeze = {"trigger_deficit_id": row["trigger_deficit_id"],
                      "effective_from_et": row["effective_from_et"],
                      "expires_on_et": row["expires_on_et"],
                      "end_emitted": False}
        elif event_type == EVT_FREEZE_END:
            if freeze is not None:
                freeze["end_emitted"] = True
        elif event_type == EVT_PDT_TRANSITION:
            pdt["state"] = row["to_state"]
            if row["evidence"] == "broker_rejection":
                pdt["rejection_latched"] = True  # durable, never unlatched (LD-R3)
        elif event_type == EVT_HWM_UPDATE:
            loss["hwm_equity"] = row["hwm_equity"]

    if kill["state"] == "flattening":
        # safety-F5: the in-process `finally` cannot survive a crash — a trailing
        # monitoring→flattening row rehydrates to HALTED with the at-trigger residual.
        kill["state"] = "halted"

    return {"kill": kill,
            "margin": {"deficits": deficits, "iml_eod": iml_eod, "freeze": freeze},
            "pdt": pdt,
            "loss": loss}
