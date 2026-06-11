"""M6 broker reconcile: PURE diff core. NOT recorder/reconcile.py (M1 depth
dual-hash) — different concept, deliberately different module (FD-M6-16).
No broker, no I/O, no clock lives here; the orchestrator feeds parsed reads in
and takes planned actions out (FD-M6-1).

Wave 1 of the M6 build: the frozen §3 vocabularies (closed sets — emitting
out-of-vocab raises `ReconcileError(ExecError)` with NO row written), the §A
dataclasses/constants, the `make_finding` typed boundary (M6C-17, FD-M6-10),
the §A.2 None-coerced sort keys (M6C-33), and the probe-result sentinels.
The PURE diff core (`diff_positions` + LIFO planner, `diff_cash`,
`resolve_order_probe`, `identity_note`) lands with wave 2 (§J).
"""
import json
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN
from typing import Optional, Tuple

from agent.exec_reasons import ExecError, ORDER_STATES
from agent.serializer import ModeledUSD, as_broker_usd, dumps

__all__ = [
    "RECONCILE_PHASES",
    "DRIFT_KINDS",
    "RECONCILE_ACTIONS",
    "RECONCILE_NOTES",
    "FINDING_FIELDS",
    "ORDER_STATE_TOKENS",
    "COST_TOLERANCE_PER_SHARE",
    "IDENTITY_TOLERANCE_USD",
    "NOT_FOUND",
    "PROBE_FAILED",
    "ReconcileError",
    "DriftFinding",
    "PlannedAdjust",
    "TerminalResolution",
    "ProbeResolution",
    "ReconcilePassResult",
    "make_finding",
    "canonical_decimal_str",
    "require_phase",
    "require_kind",
    "require_action",
    "require_note",
]


class ReconcileError(ExecError):
    """M6 reconcile invariant violation (out-of-vocab member, malformed finding,
    structural row defect) -> FATAL, fail-closed; raised BEFORE anything is
    written (NO row — §3). A drifted-but-well-formed broker read is DATA and
    never raises ReconcileError."""


# --- §3 frozen vocabularies (closed sets) --------------------------------------

RECONCILE_PHASES = frozenset({"sod", "eod", "immediate", "cli"})

DRIFT_KINDS = frozenset({
    "position_qty",             # sum(local open qty) != broker qty (both sides held or zero)
    "position_avg_cost",        # cost beyond qty x COST_TOLERANCE_PER_SHARE, qty agreeing
    "position_unknown_broker",  # broker holds a symbol the book does not know
    "position_missing_broker",  # book holds, broker flat (absence == qty 0, V2)
    "short_unrepresentable",    # broker qty < 0 (PaperPosition is long-only)
    "cash",                     # telescope residue vs the latest baseline (FD-M6-17)
    "order_state",              # local-open vs broker-terminal/absent divergence (FD-M5-7 join)
    "fills_missed",             # broker filled_qty ahead of the journaled cum watermark (alert-only lens)
    "ca_silent_adjust",         # FreezeSignal-triggered (immediate phase only); SYNTHESIZED by the
                                # orchestrator from the signal's prev/curr qty, never the re-read (M6C-1)
})

RECONCILE_ACTIONS = frozenset({
    "adjusted",            # position_adjust emitted and folded (broker won) — sod/eod/cli only
    "adjust_deferred",     # drift journaled; adjustment deferred (deferral-set symbol, M6C-2,
                           # or immediate phase, M6C-1); next eligible sod/eod/cli pass adjusts
    "resolved_terminal",   # exec-ledger order_terminal row emitted (journal-side resolution)
    "alert_only",          # fills_missed: the economic effect is adopted by the position/cash rows
    "rebaselined",         # cash baseline re-anchored by the OPERATOR path ONLY
                           # (agent reconcile --rebaseline-cash, §E/M6C-5) — never automatic
    "latched_operator",    # no automatic remedy; the latch holds for the operator
    "frozen_immediate",    # CA path: freeze + latch, never adjust (FD-M6-8)
})

RECONCILE_NOTES = frozenset({
    "cost_unverifiable",             # avg_entry_price is None — lens skipped, not drift
    "durable_id_missing",            # symbol diffed but exempt from the M2 detector
    "broker_read_failed",            # AccountInvalid / ValueError / BrokerHttpError-as-data (FD-M6-7)
    "order_probe_failed",            # probe raised / returned error-as-data (pass incomplete)
    "order_probe_unknown",           # broker status maps to "unknown"/"pending_cancel" — non-terminal, kept live
    "broker_internal_inconsistency", # |equity - (cash + sum(MV))| > 0.01 (provenance check, never drift)
    "adjust_deferred_inflight",      # deferral-set symbol (M6C-2); adjust deferred
    "cash_skipped_inflight",         # deferral set non-empty => cash read torn by construction (§1 row 19)
    "baseline_seeded",               # first-ever pass: no baseline, cash lens skipped once
    "reconcile_skipped_no_broker",   # observe/degrade path — nothing to reconcile against
    "flatten_probe_result",          # flatten-<symbol> probe outcome (terminal/not-found); detail
                                     # "<symbol>:<state|not_found>" — D17 attribution observability;
                                     # NEVER a terminal resolution (M6C-3)
})

# §B.1a closed finding-field set (the DriftFinding.field slot).
FINDING_FIELDS = frozenset({"qty", "avg_cost", "cash", "order_state", "fills"})

# Money fields: local/broker enter TYPED as BrokerUSD (FD-M6-10/M6C-17);
# qty/fills values enter as plain finite Decimals; order_state sides may also
# be closed state tokens (the §3 resolution table).
_MONEY_FIELDS = frozenset({"avg_cost", "cash"})

# Closed local/broker state tokens for `order_state` rows, derived from the §3
# resolution table: local ∈ {open, unconfirmed}; broker ∈ ORDER_STATES (V33)
# ∪ {not_found} (404/KeyError mapped at the impure edge).
ORDER_STATE_TOKENS = frozenset(ORDER_STATES) | frozenset(
    {"not_found", "open", "unconfirmed"})

# --- §F code constants (FD-M6-5 — no config block, polarity discipline V20) ----

COST_TOLERANCE_PER_SHARE = Decimal("0.005")     # FD-M6-4(b) — code constant, no config (FD-M6-5)
IDENTITY_TOLERANCE_USD = Decimal("0.01")        # FD-M6-4(d) — note-only check, never a drift verdict

# Pinned context for ALL derived diff arithmetic — ambient-context immunity
# (the M4-DET-1 / quote_quality.py:23 precedent).
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)

# Probe-result sentinels (§A): the impure edge maps KeyError / 404-as-data /
# raises to these; the PURE resolver consumes only these shapes (the third
# shape is a parsed BrokerOrder from parse_order_payload — the ONE parser).
NOT_FOUND = object()      # broker says the client_order_id does not exist
PROBE_FAILED = object()   # probe raised / non-404 error-as-data => pass completed=False


# --- vocabulary guards (raise ReconcileError, never coerce) ---------------------

def _require_vocab(vocab, value, *, what: str):
    if value not in vocab:
        raise ReconcileError(f"out-of-vocab {what}: {value!r}")
    return value


def require_phase(value):
    """ReconcileError unless `value` ∈ RECONCILE_PHASES."""
    return _require_vocab(RECONCILE_PHASES, value, what="reconcile phase")


def require_kind(value):
    """ReconcileError unless `value` ∈ DRIFT_KINDS."""
    return _require_vocab(DRIFT_KINDS, value, what="drift kind")


def require_action(value):
    """ReconcileError unless `value` ∈ RECONCILE_ACTIONS."""
    return _require_vocab(RECONCILE_ACTIONS, value, what="reconcile action")


def require_note(value):
    """ReconcileError unless `value` ∈ RECONCILE_NOTES."""
    return _require_vocab(RECONCILE_NOTES, value, what="reconcile note")


def canonical_decimal_str(value: Decimal) -> str:
    """THE one Decimal rendering path (M6C-17/§A.2): the serializer's rendering
    (`dumps` -> JSON string), never str(Decimal) ad hoc. Rejects floats and
    non-finite Decimals via the serializer itself."""
    return json.loads(dumps(value))


# --- §A dataclasses --------------------------------------------------------------

@dataclass(frozen=True)
class DriftFinding:
    kind: str                       # DRIFT_KINDS member
    symbol: Optional[str]           # None only for kind == "cash"
    field: str                      # "qty" | "avg_cost" | "cash" | "order_state" | "fills"
    local: Optional[str]            # canonical Decimal string or closed state token; None = absent
    broker: Optional[str]
    diff: Optional[str]             # broker - local (Decimal string); None when non-numeric
    action: str                     # RECONCILE_ACTIONS member
    position_id: Optional[str] = None
    local_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None


def _typed_value(value, *, field: str, slot: str):
    """One side of the §A typed boundary: returns (rendered_str_or_token_or_None,
    numeric_Decimal_or_None). Money fields require BrokerUSD via `as_broker_usd`;
    qty/fills values are plain finite Decimals; ModeledUSD / float / bool / NaN
    raise TypeError with nothing constructed (FD-M6-10, test 1.23)."""
    if value is None:
        return None, None
    if isinstance(value, ModeledUSD):
        raise TypeError(f"{slot}: ModeledUSD must never reach a reconcile "
                        f"field (FD-M6-10)")
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{slot}: bool/float not allowed in a reconcile "
                        f"finding (S2); use Decimal")
    if isinstance(value, str):
        if field != "order_state":
            raise ReconcileError(f"{slot}: state token only legal on "
                                 f"order_state findings (got {value!r})")
        if value not in ORDER_STATE_TOKENS:
            raise ReconcileError(f"{slot}: out-of-vocab order-state token: "
                                 f"{value!r}")
        return value, None
    if not isinstance(value, Decimal):
        raise TypeError(f"{slot}: must be a Decimal/BrokerUSD value, a closed "
                        f"state token, or None (got {type(value).__name__})")
    if not value.is_finite():
        raise TypeError(f"{slot}: non-finite Decimal not allowed ({value!r})")
    if field in _MONEY_FIELDS:
        value = as_broker_usd(value)    # TypeError unless BrokerUSD (M6C-17)
    return canonical_decimal_str(value), value


def make_finding(*, kind, symbol, field, local, broker, action,
                 position_id=None, local_order_id=None,
                 broker_order_id=None) -> DriftFinding:
    """THE one typed boundary (M6C-17, FD-M6-10): money values enter TYPED as
    BrokerUSD and are validated via as_broker_usd (a ModeledUSD / float / bool /
    NaN => TypeError, nothing constructed — test 1.23's home); qty values enter
    as plain finite Decimals. The canonical value string is rendered HERE, once
    (the serializer's Decimal rendering, never str(Decimal) ad hoc), and
    diff = broker - local is computed under _DECIMAL_CTX before stringification.
    Lineage is erased at stringification, so the lineage wall CANNOT live at the
    facade — the facade (§B.1a) validates strings structurally only."""
    kind = require_kind(kind)
    action = require_action(action)
    if field not in FINDING_FIELDS:
        raise ReconcileError(f"out-of-vocab finding field: {field!r}")
    if symbol is None:
        if kind != "cash":
            raise ReconcileError(f"symbol: None is legal only on cash "
                                 f"findings (kind={kind!r})")
    elif not isinstance(symbol, str) or not symbol:
        raise ReconcileError(f"symbol: must be a non-empty str or None "
                             f"(got {symbol!r})")
    for name, value in (("position_id", position_id),
                        ("local_order_id", local_order_id),
                        ("broker_order_id", broker_order_id)):
        if value is not None and not isinstance(value, str):
            raise ReconcileError(f"{name}: must be a str or None "
                                 f"(got {type(value).__name__})")
    local_str, local_num = _typed_value(local, field=field, slot="local")
    broker_str, broker_num = _typed_value(broker, field=field, slot="broker")
    diff_str = None
    if local_num is not None and broker_num is not None:
        diff_str = canonical_decimal_str(
            _DECIMAL_CTX.subtract(broker_num, local_num))
    return DriftFinding(kind=kind, symbol=symbol, field=field, local=local_str,
                        broker=broker_str, diff=diff_str, action=action,
                        position_id=position_id, local_order_id=local_order_id,
                        broker_order_id=broker_order_id)


@dataclass(frozen=True)
class PlannedAdjust:
    position_id: str
    symbol: str
    prev_qty: Decimal
    adjusted_qty: Decimal                  # >= 0; 0 closes (status -> "closed")
    prev_broker_cost_usd: "BrokerUSD"
    adjusted_broker_cost_usd: "BrokerUSD"  # symbol-level sum == broker-derived cost


@dataclass(frozen=True)
class TerminalResolution:               # M6C-30 — the record_order_terminal inputs, pinned
    decision_id: str                    # the order's original journaled ids (FD-M6-21)
    order_id: str
    terminal_state: str                 # TERMINAL_STATES member
    filled_qty: Decimal                 # journaled cum watermark for expired-resolutions (M6C-29)
    cum_notional_usd: "BrokerUSD"       # journaled cum notional (Decimal("0") when no fills)
    ts_broker_utc: Optional[str]        # None for not-found resolutions (M6C-29)


@dataclass(frozen=True)
class ProbeResolution:                  # M6C-30 — resolve_order_probe's frozen return shape
    findings: Tuple[DriftFinding, ...]
    notes: Tuple[tuple, ...]
    terminal_resolutions: Tuple[TerminalResolution, ...]
    defer_symbols: Tuple[str, ...]      # symbols this probe adds to the deferral set (M6C-2/3)


@dataclass(frozen=True)
class ReconcilePassResult:
    reconcile_id: str
    phase: str                            # RECONCILE_PHASES member
    session_date_et: str
    completed: bool                       # False => drift UNKNOWN; latch unchanged (FD-M6-7)
    findings: Tuple[DriftFinding, ...]    # sorted by the None-coerced total key
                                          # (kind, symbol or "", position_id or "", local_order_id or "")
                                          # — M6C-33: Optional[str] slots never reach None-vs-str ordering
    adjustments: Tuple[PlannedAdjust, ...]
    notes: Tuple[tuple, ...]              # (note, symbol_or_None, detail_str), sorted with the
                                          # symbol slot None-coerced to "" (M6C-33)
    clean: bool                           # completed AND findings == () — NECESSARY, not sufficient,
                                          # for a latch clear: the clear additionally requires
                                          # phase ∈ {sod, eod, cli}, a drift-free window, and NO
                                          # frozen symbol (FD-M6-6, §B.1, §D step 7 — RC-11)


# --- §A.2 None-coerced sort keys (M6C-33) ---------------------------------------

def _finding_key(finding: DriftFinding):
    """The §A.2 None-coerced total sort key for findings — Optional[str] slots
    never reach a None-vs-str comparison."""
    return (finding.kind, finding.symbol or "", finding.position_id or "",
            finding.local_order_id or "")


def _note_key(note):
    """(note, symbol_or_None, detail) -> None-coerced total key (M6C-33)."""
    return (note[0], note[1] or "", note[2])
