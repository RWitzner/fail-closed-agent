"""M6 §B — ReconcileLedger: the validating facade over
`recorder.persistence.EventWriter` for the NEW stream
`journal/reconcile_alerts.jsonl` (the spec-named stream, design spec :284/:445;
the ExecLedger/RiskLedger/StatusLedger pattern). NO new writer, hash, or
serialization (FD-M6-3); one ledger per resolved path; injected `run_id` (via
the writer); `"v"` is the FIRST payload key (`RECONCILE_LEDGER_VERSION`) and
`rules_hash` the LAST on every row; kwarg-only exact field sets validated
BEFORE any write (a raise leaves the stream untouched — V16).

Row shapes (§B.1): `reconcile` (the spec drift row — superset of
{symbol, local, broker, diff, action}), `reconcile_note`, `reconcile_baseline`,
`reconcile_run` (the pass commit marker, written LAST — §B.3). Reconcile rows
NEVER ride the journal `decision_id`/`order_id` kwargs: broker order ids ride
the payload field `broker_order_id` (an arbitrary broker UUID as kwarg would
fail prefix validation and perturb the V4 open_orders fold — FD-M6-3).

The facade validates `local`/`broker`/`diff` STRUCTURALLY only (parseable
finite canonical Decimal string / closed state token / explicit None — M6C-17):
lineage is erased at stringification, so the `BrokerUSD`-vs-`ModeledUSD` wall
(FD-M6-10) is enforced at the engine's typed boundary `make_finding` (§A),
not here. Baseline money fields still enter TYPED (`as_broker_usd`) because
they never pass through the engine.

`rehydrate_reconcile_state` is the PURE latch fold (FD-M6-6): by ascending
`seq`, any `reconcile` (drift) row sets the latch; a `reconcile_run` row with
`completed=true AND clean=true AND phase ∈ {sod, eod, cli}` AND a drift-free
window since the previous summary clears it (immediate summaries NEVER clear —
M6C-1; an inconsistent drift-rows+clean-summary window keeps it set fail-closed
— M6C-22); trailing drift rows after the last summary keep it set (mid-pass
crash, fail-closed). `outstanding_cash_residue` is returned as the ROW CONTENT
DICT; the orchestrator seam projects it into a `DriftFinding` at step 6.5
(RC-14).

Documented builder resolutions (faithful to the contract; none contradict a
frozen pin):

1. `_ACCOUNT_SOURCES` is a VERBATIM local copy of
   `agent.risk.account_state.ACCOUNT_SOURCES` (V2): that module is outside this
   module's §4 import row (the exec_ledger `_PROVENANCE_KEYS` precedent —
   one vocabulary, one home; the copy is pinned by test against the source).
2. §B.1a pins `detail` as a "bounded str" without a number; the bound is the
   code constant `_DETAIL_MAX_LEN = 1000` (human-facing, never parsed back).
3. The fold raises `ReconcileError` on an out-of-vocab stream event_type —
   the stream is written exclusively by this facade (4 types), so an unknown
   type is corruption-grade, mirroring `PaperBook.rehydrate`'s posture (V5).
"""
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Optional

from agent.broker_reconcile import (
    FINDING_FIELDS,
    ORDER_STATE_TOKENS,
    ReconcileError,
    canonical_decimal_str,
    require_action,
    require_kind,
    require_note,
    require_phase,
)
from agent.journal import JournalCorruption  # noqa: F401 — re-exported (§B.1b)
from agent.serializer import as_broker_usd
from recorder.persistence import EventWriter, replay_stream

__all__ = [
    "STREAM_RECONCILE_ALERTS",
    "RECONCILE_LEDGER_VERSION",
    "EVT_RECONCILE",
    "EVT_RECONCILE_NOTE",
    "EVT_RECONCILE_BASELINE",
    "EVT_RECONCILE_RUN",
    "ReconcileLedger",
    "replay_reconcile",
    "rehydrate_reconcile_state",
    "EventWriter",
    "JournalCorruption",
]

STREAM_RECONCILE_ALERTS = "reconcile_alerts"
RECONCILE_LEDGER_VERSION = 1

# journal/reconcile_alerts.jsonl event types (§B.1)
EVT_RECONCILE = "reconcile"
EVT_RECONCILE_NOTE = "reconcile_note"
EVT_RECONCILE_BASELINE = "reconcile_baseline"
EVT_RECONCILE_RUN = "reconcile_run"

_EVENT_TYPES = frozenset({EVT_RECONCILE, EVT_RECONCILE_NOTE,
                          EVT_RECONCILE_BASELINE, EVT_RECONCILE_RUN})

# Latch-clearing phases (FD-M6-6/M6C-1: immediate summaries never clear).
_LATCH_CLEAR_PHASES = frozenset({"sod", "eod", "cli"})

# VERBATIM copy of agent.risk.account_state.ACCOUNT_SOURCES (V2) — that module
# is outside this module's §4 import row (resolution 1 in the module docstring).
_ACCOUNT_SOURCES = frozenset({"alpaca_paper", "alpaca_live", "fixture", "spy"})

# §C / §P.3 id prefixes — minted by their owners; VALIDATED (presence/format) here.
_RECONCILE_ID_PREFIXES = ("rc-",)
_DRIFT_ID_PREFIXES = ("rd-",)
_POSITION_ID_PREFIXES = ("pos-",)
_ORDER_ID_PREFIXES = ("o-", "synthetic-o-")

_DETAIL_MAX_LEN = 1000   # §B.1a "bounded str" (resolution 2 in the docstring)


# --- per-field validation helpers (§B.1a; raise-before-write) -------------------

def _require_str(value, *, field: str, allow_none: bool = False) -> Optional[str]:
    if value is None:
        if allow_none:
            return None
        raise ReconcileError(f"{field}: required non-null str (got None)")
    if not isinstance(value, str) or not value:
        raise ReconcileError(f"{field}: must be a non-empty str "
                             f"(got {value!r})")
    return value


def _require_id(value, *, field: str, prefixes,
                allow_none: bool = False) -> Optional[str]:
    """§C id format guard: presence + prefix only — minting is the owners' job."""
    if value is None:
        if allow_none:
            return None
        raise ReconcileError(f"{field}: required id (got None)")
    if not isinstance(value, str) or not any(
            value.startswith(p) and len(value) > len(p) for p in prefixes):
        raise ReconcileError(f"{field}: must be a str id with prefix in "
                             f"{tuple(prefixes)} (got {value!r})")
    return value


def _require_bool(value, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ReconcileError(f"{field}: must be a real bool "
                             f"(got {type(value).__name__}={value!r})")
    return value


def _require_count(value, *, field: str) -> int:
    """Non-negative int, bool rejected (§B.1a)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReconcileError(f"{field}: must be an int, bool excluded "
                             f"(got {type(value).__name__}={value!r})")
    if value < 0:
        raise ReconcileError(f"{field}: must be >= 0 (got {value})")
    return value


def _canonical_decimal(value: str, *, field: str) -> str:
    """A canonical finite Decimal string: parseable, finite, and byte-identical
    to the serializer's own rendering of the parsed value."""
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ReconcileError(f"{field}: unparseable Decimal string {value!r}")
    if not parsed.is_finite():
        raise ReconcileError(f"{field}: non-finite value string not allowed "
                             f"({value!r}) — S2")
    if canonical_decimal_str(parsed) != value:
        raise ReconcileError(f"{field}: non-canonical Decimal string {value!r}")
    return value


def _value_str(value, *, field: str, finding_field: str) -> Optional[str]:
    """local/broker/diff (§B.1a): canonical Decimal string, closed state token
    (order_state rows ONLY), or explicit None; bool/float/typed values anywhere
    => raise (the facade is structural-only — M6C-17)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReconcileError(f"{field}: must be a canonical Decimal string, a "
                             f"closed state token, or None "
                             f"(got {type(value).__name__}={value!r})")
    if finding_field == "order_state" and value in ORDER_STATE_TOKENS:
        return value
    return _canonical_decimal(value, field=field)


def _broker_usd(value, *, field: str):
    """BrokerUSD via as_broker_usd, exact/unquantized (§B.1a, LD-R5)."""
    try:
        return as_broker_usd(value)
    except TypeError:
        raise TypeError(f"{field}: broker-lineage field requires BrokerUSD "
                        f"(got {type(value).__name__})")


def _positions_list(value, *, field: str) -> list:
    """Sorted list of {"symbol": str, "qty": Decimal-string}, no duplicates."""
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping) \
            or not isinstance(value, (list, tuple)):
        raise ReconcileError(f"{field}: must be a list of "
                             f"{{symbol, qty}} entries (got {value!r})")
    out = []
    symbols = []
    for i, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item.keys()) != {"symbol", "qty"}:
            raise ReconcileError(f"{field}[{i}]: key set must be exactly "
                                 f"['qty', 'symbol'] (got {item!r})")
        symbol = _require_str(item["symbol"], field=f"{field}[{i}].symbol")
        qty = item["qty"]
        if not isinstance(qty, str):
            raise ReconcileError(f"{field}[{i}].qty: must be a Decimal string "
                                 f"(got {type(qty).__name__}={qty!r})")
        qty = _canonical_decimal(qty, field=f"{field}[{i}].qty")
        symbols.append(symbol)
        out.append({"symbol": symbol, "qty": qty})
    if symbols != sorted(symbols) or len(set(symbols)) != len(symbols):
        raise ReconcileError(f"{field}: symbols must be sorted with no "
                             f"duplicates (got {symbols})")
    return out


def _sorted_str_list(value, *, field: str) -> list:
    """Sorted, duplicate-free list of non-empty strings (§B.1a)."""
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping) \
            or not isinstance(value, (list, tuple)):
        raise ReconcileError(f"{field}: must be a list of strings "
                             f"(got {value!r})")
    out = []
    for i, item in enumerate(value):
        out.append(_require_str(item, field=f"{field}[{i}]"))
    if out != sorted(out) or len(set(out)) != len(out):
        raise ReconcileError(f"{field}: must be sorted with no duplicates "
                             f"(got {out})")
    return out


class ReconcileLedger:
    """Validating facade over EventWriter on journal/reconcile_alerts.jsonl.
    One kwarg-only record_* method per §B.1 event type; each validates the
    EXACT field set (missing/extra => TypeError), the per-field §B.1a rules,
    and writes `{"v": 1, **body, "rules_hash": ...}` — all BEFORE anything is
    written (a raise leaves the stream untouched)."""

    def __init__(self, writer: EventWriter, *, rules_hash: str) -> None:
        self._writer = writer
        self._rules_hash = _require_str(rules_hash, field="rules_hash")

    def _record(self, event_type: str, body: dict) -> dict:
        fields = {"v": RECONCILE_LEDGER_VERSION, **body,
                  "rules_hash": self._rules_hash}
        # NO decision_id/order_id journal kwargs — EVER (§B.1/FD-M6-3).
        return self._writer.record(event_type, fields)

    def record_reconcile(self, *, reconcile_id, drift_id, kind, symbol, field,
                         local, broker, diff, action, position_id,
                         local_order_id, broker_order_id) -> dict:
        """The spec drift row (V14 :347), superset of
        {symbol, local, broker, diff, action}."""
        kind = require_kind(kind)
        if symbol is None:
            if kind != "cash":
                raise ReconcileError(f"symbol: None is legal only on cash "
                                     f"rows (kind={kind!r})")
        else:
            symbol = _require_str(symbol, field="symbol")
        if field not in FINDING_FIELDS:
            raise ReconcileError(f"out-of-vocab finding field: {field!r}")
        if broker_order_id is not None and not isinstance(broker_order_id, str):
            # deliberately UN-prefixed: broker UUIDs / "flatten-<symbol>" ride
            # here, never the journal kwarg (§B.1a)
            raise ReconcileError(f"broker_order_id: must be a str or None "
                                 f"(got {type(broker_order_id).__name__})")
        body = {
            "reconcile_id": _require_id(reconcile_id, field="reconcile_id",
                                        prefixes=_RECONCILE_ID_PREFIXES),
            "drift_id": _require_id(drift_id, field="drift_id",
                                    prefixes=_DRIFT_ID_PREFIXES),
            "kind": kind,
            "symbol": symbol,
            "field": field,
            "local": _value_str(local, field="local", finding_field=field),
            "broker": _value_str(broker, field="broker", finding_field=field),
            "diff": _value_str(diff, field="diff", finding_field=field),
            "action": require_action(action),
            "position_id": _require_id(position_id, field="position_id",
                                       prefixes=_POSITION_ID_PREFIXES,
                                       allow_none=True),
            "local_order_id": _require_id(local_order_id,
                                          field="local_order_id",
                                          prefixes=_ORDER_ID_PREFIXES,
                                          allow_none=True),
            "broker_order_id": broker_order_id,
        }
        return self._record(EVT_RECONCILE, body)

    def record_reconcile_note(self, *, reconcile_id, note, symbol,
                              detail) -> dict:
        if not isinstance(detail, str):
            raise ReconcileError(f"detail: must be a str "
                                 f"(got {type(detail).__name__}={detail!r})")
        if len(detail) > _DETAIL_MAX_LEN:
            raise ReconcileError(f"detail: bounded at {_DETAIL_MAX_LEN} chars "
                                 f"(got {len(detail)})")
        body = {
            "reconcile_id": _require_id(reconcile_id, field="reconcile_id",
                                        prefixes=_RECONCILE_ID_PREFIXES),
            "note": require_note(note),
            "symbol": _require_str(symbol, field="symbol", allow_none=True),
            "detail": detail,
        }
        return self._record(EVT_RECONCILE_NOTE, body)

    def record_reconcile_baseline(self, *, reconcile_id, session_date_et,
                                  cash_usd, equity_usd, buying_power_usd,
                                  fills_seq_watermark, positions,
                                  durable_seeded) -> dict:
        body = {
            "reconcile_id": _require_id(reconcile_id, field="reconcile_id",
                                        prefixes=_RECONCILE_ID_PREFIXES),
            "session_date_et": _require_str(session_date_et,
                                            field="session_date_et"),
            # money EXACT/unquantized — rehydrate-bearing for the telescope
            # (LD-R5 discipline, §B.1)
            "cash_usd": _broker_usd(cash_usd, field="cash_usd"),
            "equity_usd": _broker_usd(equity_usd, field="equity_usd"),
            "buying_power_usd": _broker_usd(buying_power_usd,
                                            field="buying_power_usd"),
            # payload-legal name — bare `seq` is reserved (V12)
            "fills_seq_watermark": _require_count(fills_seq_watermark,
                                                  field="fills_seq_watermark"),
            "positions": _positions_list(positions, field="positions"),
            "durable_seeded": _sorted_str_list(durable_seeded,
                                               field="durable_seeded"),
        }
        return self._record(EVT_RECONCILE_BASELINE, body)

    def record_reconcile_run(self, *, reconcile_id, phase, session_date_et,
                             trigger_durable_key, broker_source,
                             checked_symbols, drift_count, adjusted_count,
                             note_count, completed, clean) -> dict:
        completed = _require_bool(completed, field="completed")
        clean = _require_bool(clean, field="clean")
        drift_count = _require_count(drift_count, field="drift_count")
        if clean and not completed:
            raise ReconcileError("clean=true requires completed=true (M6C-22)")
        if clean and drift_count != 0:
            # a buggy clean summary cannot clear a latch its own window's
            # rows set (M6C-22)
            raise ReconcileError(f"clean=true requires drift_count == 0 "
                                 f"(got {drift_count}) — M6C-22")
        if broker_source not in _ACCOUNT_SOURCES:
            raise ReconcileError(f"out-of-vocab broker_source: "
                                 f"{broker_source!r}")
        body = {
            "reconcile_id": _require_id(reconcile_id, field="reconcile_id",
                                        prefixes=_RECONCILE_ID_PREFIXES),
            "phase": require_phase(phase),
            "session_date_et": _require_str(session_date_et,
                                            field="session_date_et"),
            "trigger_durable_key": _require_str(trigger_durable_key,
                                                field="trigger_durable_key",
                                                allow_none=True),
            "broker_source": broker_source,
            "checked_symbols": _sorted_str_list(checked_symbols,
                                                field="checked_symbols"),
            "drift_count": drift_count,
            "adjusted_count": _require_count(adjusted_count,
                                             field="adjusted_count"),
            "note_count": _require_count(note_count, field="note_count"),
            "completed": completed,
            "clean": clean,
        }
        return self._record(EVT_RECONCILE_RUN, body)


# --- replay (S3 — the shared code path, semantics inherited verbatim) -----------

def replay_reconcile(path) -> list:
    """Re-read journal/reconcile_alerts.jsonl hash-verified (truncated tail
    dropped, complete corrupt line => JournalCorruption — V12 semantics via
    recorder.persistence.replay_stream)."""
    return replay_stream(path)


# --- rehydrate (the PURE latch fold, FD-M6-6) ------------------------------------

def rehydrate_reconcile_state(rows) -> dict:
    """PURE fold by ascending seq (FD-M6-6). Returns:
       {"latched": bool,           # drift row -> True; a reconcile_run row clears ONLY when
                                   # completed=true AND clean=true AND phase in {sod, eod, cli}
                                   # AND zero drift rows observed since the previous summary
                                   # (immediate summaries never clear, M6C-1; a drift-rows+clean-
                                   # summary window keeps True fail-closed, M6C-22); trailing
                                   # drift rows after the last summary keep True (fail-closed)
        "latest_baseline": Optional[dict],   # latest reconcile_baseline row content (cash telescope input)
        "outstanding_cash_residue": Optional[dict],  # latest kind="cash" drift-row content with
                                             # action "latched_operator" (RC-8); cleared by a
                                             # kind="cash" action="rebaselined" row or by a
                                             # completed=true summary whose window held ZERO
                                             # kind="cash" drift rows (sound: a cash-skipped pass
                                             # RE-JOURNALS an outstanding residue per FD-M6-17, so
                                             # a cash-row-free completed window means evaluated-
                                             # clean or no residue existed); seeds the in-process
                                             # re-journal state at step 6.5. The fold returns the
                                             # ROW CONTENT DICT; the orchestrator seam PROJECTS it
                                             # into a DriftFinding at step 6.5 (RC-14) — inside the
                                             # orchestrator the field is ALWAYS
                                             # Optional[DriftFinding], one shape on every path
        "pass_count": int,                   # ALL prior reconcile_run rows, completed or not
                                             # (occurrence input, §C/M6C-12)
        "drift_in_window": int}              # reconcile rows after the last summary; lets
                                             # in-process clear obey the same crash-window
                                             # rule as this fold"""
    latched = False
    latest_baseline = None
    outstanding_cash_residue = None
    pass_count = 0
    drift_in_window = 0      # reconcile rows since the previous summary
    cash_in_window = 0       # kind="cash" reconcile rows since the previous summary
    for row in sorted(rows, key=lambda r: r["seq"]):
        event_type = row.get("event_type")
        if event_type == EVT_RECONCILE:
            latched = True                      # any drift row sets, fail-closed
            drift_in_window += 1
            if row.get("kind") == "cash":
                cash_in_window += 1
                if row.get("action") == "rebaselined":
                    # the OPERATOR adjudicated the residue (§E/M6C-5)
                    outstanding_cash_residue = None
                elif row.get("action") == "latched_operator":
                    outstanding_cash_residue = row
        elif event_type == EVT_RECONCILE_RUN:
            pass_count += 1                     # ALL summaries count (M6C-12)
            completed = row.get("completed") is True
            if (completed and row.get("clean") is True
                    and row.get("phase") in _LATCH_CLEAR_PHASES
                    and drift_in_window == 0):
                latched = False
            if completed and cash_in_window == 0:
                # sound per RC-8: a cash-skipped pass re-journals an
                # outstanding residue, so a cash-row-free COMPLETED window
                # means evaluated-clean or no residue existed; an incomplete
                # summary clears NOTHING
                outstanding_cash_residue = None
            drift_in_window = 0                 # the summary bounds the window
            cash_in_window = 0
        elif event_type == EVT_RECONCILE_BASELINE:
            latest_baseline = row               # latest-wins by ascending seq
        elif event_type == EVT_RECONCILE_NOTE:
            pass                                # notes never fold
        else:
            # the stream is written exclusively by this facade (4 types) — an
            # unknown type is corruption-grade, the V5 posture (docstring
            # resolution 3)
            raise ReconcileError(f"out-of-vocab reconcile_alerts event_type: "
                                 f"{event_type!r}")
    return {
        "latched": latched,
        "latest_baseline": latest_baseline,
        "outstanding_cash_residue": outstanding_cash_residue,
        "pass_count": pass_count,
        "drift_in_window": drift_in_window,
    }
