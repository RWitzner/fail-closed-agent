"""M5 §P — ExecLedger: the validating facade over `recorder.persistence.EventWriter`
for the three NEW execution streams `journal/orders.jsonl`, `journal/fills.jsonl`,
`journal/positions.jsonl` (the StatusLedger/RiskLedger pattern). NO new writer,
hash, or serialization; one ledger per resolved path triple; injected `run_id`
(via the writers); `"v"` is the FIRST payload key (`EXEC_LEDGER_VERSION`);
`rules_hash` on every row; `decision_id`/`order_id` ride the journal kwargs and
never appear in a payload (`journal._RESERVED`). M3's `decisions.jsonl` /
`forecast_scored.jsonl` and M4's `risk.jsonl` are untouched (FD-M5-9).

MONEY-LINEAGE ENFORCEMENT (M5C-S10): every broker-lineage money field
(`delta_cost_usd`, `broker_cost_usd`, `broker_exit_notional_usd`,
`cum_notional_usd`, `realized_broker_pnl`, `broker_account_pnl`) passes
`serializer.as_broker_usd`; every modeled-lineage slot (`modeled_cost_usd`,
`realized_modeled_pnl`, `execution_realistic_pnl`) passes the LOCAL
`as_modeled_usd` guard defined HERE (serializer.py is NOT edited — §3; the guard
mirrors `as_broker_usd`: TypeError on a non-ModeledUSD, BrokerUSD included).
Serialization erases the newtype, so this seam is the last place a lineage swap
is catchable; a violation raises BEFORE anything is written. Fields the contract
pins as plain Decimal (`divergence_usd`, the EX-4 slice/residual costs, the
quantize-only provenance values) accept ANY finite Decimal — the newtypes are
Decimal subclasses and are not refused there (no over-enforcement).

`fill_id` is COMPUTED HERE in `record_broker_fill` (the ONE home — M5C-T7):
`"bf-" + row_hash({order_id, cum_filled_qty_after, cum_notional_after})` with
`cum_notional_after = cur.filled_qty x cur.filled_avg_price` (exact Decimal
product under a pinned context — the FD-M5-18 telescoped total, NEVER
sigma-delta_cost) journaled on the row so replay can recover the operand.

Documented resolutions (faithful to the contract; none contradict a frozen pin):

1. `record_broker_fill` consumes the emitted `FillDelta` and the `cur`
   `BrokerOrder` snapshot DUCK-TYPED (attribute access, no isinstance): §3 pins
   this module's imports to [exec_reasons, recorder.persistence, agent.journal,
   agent.serializer] and the pure-family wall forbids importing `agent.broker*`,
   so the §F types cannot be imported here. A missing attribute or a
   delta-vs-cur inconsistency (cum/avg mismatch — the delta was not computed
   from `cur`) raises `ExecError` (malformed collaborator input).
2. `kill_state` is journaled as a plain string: §P.2 carries no vocabulary
   annotation on it and `KILL_STATES` lives in `agent.risk.reasons` (one
   vocabulary, one home), which is outside this module's §3 import row.
3. The §P.2 frozen provenance key set is pinned locally (`_PROVENANCE_KEYS`):
   `execution_realism.QUOTE_PROVENANCE_KEYS` cannot be imported here (§3 row).
   Both are verbatim copies of the same §P.2 line.
4. `modeled_execution_fill.reasons` is validated as a sorted list of strings
   only: the closed `MODELED_FILL_REASONS` vocabulary lives in
   `execution_realism` (its producer, `ModeledFill.__post_init__`, already
   enforces membership structurally at construction).
5. `client_order_id == order_id` is enforced wherever both appear (FD-M5-7,
   frozen: "client_order_id = order_id").
6. Pinned-literal row fields are emitted BY THE LEDGER, never taken as kwargs
   (they can therefore never be wrong): `divergence_alert.threshold_bps = "10"`,
   `pnl_snapshot.basis = {broker: "broker_fills", modeled:
   "modeled_fill_plus_fees"}`, `pnl_snapshot.used_for_strategy_evaluation =
   "execution_realistic_pnl"`. `position_open.side` IS a kwarg but must equal
   the §P.2-pinned `"long"` (M5 is long-only; the field varies in M6+).
7. `record_fill_divergence` enforces the FD-M5-20 invariant `flag ==
   "unassessed" <=> modeled side null <=> divergence_usd null <=>
   divergence_bps null` (the RiskLedger allowed<=>reasons posture).
8. `rehydrate_exec_state` adds two keyword-only parameters beyond the §P.1
   three-arg sketch: `run_id` (REQUIRED — §P.1 pins `open_deny` as "rebuilt
   from order_submit_unconfirmed rows of THIS run only", which is underivable
   from the row lists alone; FD-M5-17's deny set is in-memory per-run and must
   NOT resurrect across restarts) and `book_rehydrate` (the §K seam:
   `PaperBook.rehydrate` lands with the paper_book wave; the default is a
   documented pass-through that groups position/fill rows per `position_id`
   in ascending `seq`). NOTE FOR THE PAPER_BOOK WAVE: the orchestrator should
   pass `book_rehydrate=PaperBook.rehydrate` once §K lands.
9. `open_orders` drops an `order_id` once the stream shows it can no longer be
   live: an `order_terminal` row (§P.2), a `broker_reject` row (FD-M5-17:
   terminal for that client_order_id), or a `reject` row carrying the order_id
   (the §M.4 consume-time path: the write-ahead attempt was journaled but
   `submit_order` raised before any network call — the order never existed at
   the broker). `order_submit_unconfirmed` does NOT drop it: unconfirmed
   orders are presumed-live until reconciled (fail-closed, FD-M5-17).
10. All §P.2 payload fields are REQUIRED kwargs (nullable fields take an
    explicit None) so a forgotten field is a TypeError at the call site —
    the §R 6 missing/extra exactness posture.
"""
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Mapping, Optional

from agent.exec_reasons import (
    CANCEL_CAUSES,
    CANCEL_OUTCOMES,
    CLOSE_REASONS,
    DIVERGENCE_FLAGS,
    EXTRA_REJECT_REASONS,
    ExecError,
    FILL_SOURCES,
    MODELED_FILL_MODELS,
    ORDER_STATES,
    REALISM_CLASSES,
    STRATEGY_DECISION_ACTIONS,
    SUBMIT_RESOLUTIONS,
    TERMINAL_STATES,
    require_member,
    require_reason,
    require_stage,
)
from agent.journal import JournalCorruption, _RESERVED  # noqa: F401 — re-exported
from agent.serializer import ModeledUSD, as_broker_usd, row_hash
from recorder.persistence import EventWriter, replay_stream

__all__ = [
    "STREAM_ORDERS",
    "STREAM_FILLS",
    "STREAM_POSITIONS",
    "EXEC_LEDGER_VERSION",
    "EVT_STRATEGY_DECISION",
    "EVT_REJECT",
    "EVT_ORDER_SUBMIT_ATTEMPT",
    "EVT_ORDER_SUBMITTED",
    "EVT_BROKER_ORDER_UPDATE",
    "EVT_BROKER_REJECT",
    "EVT_ORDER_SUBMIT_UNCONFIRMED",
    "EVT_POST_SUBMIT_CANCEL_ATTEMPT",
    "EVT_ORDER_STATE_ALERT",
    "EVT_ORDER_TERMINAL",
    "EVT_BROKER_FILL",
    "EVT_MODELED_EXECUTION_FILL",
    "EVT_FILL_DIVERGENCE",
    "EVT_DIVERGENCE_ALERT",
    "EVT_POSITION_OPEN",
    "EVT_MARK",
    "EVT_PNL_SNAPSHOT",
    "EVT_POSITION_CLOSE",
    "ExecLedger",
    "as_modeled_usd",
    "replay_orders",
    "replay_fills",
    "replay_positions",
    "rehydrate_exec_state",
    "EventWriter",
    "JournalCorruption",
]

STREAM_ORDERS = "orders"
STREAM_FILLS = "fills"
STREAM_POSITIONS = "positions"
EXEC_LEDGER_VERSION = 1

# journal/orders.jsonl event types (§P.2)
EVT_STRATEGY_DECISION = "strategy_decision"
EVT_REJECT = "reject"
EVT_ORDER_SUBMIT_ATTEMPT = "order_submit_attempt"
EVT_ORDER_SUBMITTED = "order_submitted"
EVT_BROKER_ORDER_UPDATE = "broker_order_update"
EVT_BROKER_REJECT = "broker_reject"
EVT_ORDER_SUBMIT_UNCONFIRMED = "order_submit_unconfirmed"
EVT_POST_SUBMIT_CANCEL_ATTEMPT = "post_submit_cancel_attempt"
EVT_ORDER_STATE_ALERT = "order_state_alert"
EVT_ORDER_TERMINAL = "order_terminal"
# journal/fills.jsonl event types
EVT_BROKER_FILL = "broker_fill"
EVT_MODELED_EXECUTION_FILL = "modeled_execution_fill"
EVT_FILL_DIVERGENCE = "fill_divergence"
EVT_DIVERGENCE_ALERT = "divergence_alert"
# journal/positions.jsonl event types
EVT_POSITION_OPEN = "position_open"
EVT_MARK = "mark"
EVT_PNL_SNAPSHOT = "pnl_snapshot"
EVT_POSITION_CLOSE = "position_close"

# Frozen §P.2 provenance sub-dict key set (resolution 3); + book_hash where noted.
_PROVENANCE_KEYS = ("dataset", "schema", "ts_event_utc", "ts_recv_utc",
                    "seen_at_ms", "reconnect_epoch", "vendor_seq")
_FEE_KEYS = ("model_version", "sec_usd", "taf_usd", "total_usd")
_DETAIL_KEYS = ("risk_reasons", "quote_reasons", "broker_code", "broker_message")
_INTENT_KEYS = ("order_type", "tif", "limit_price")

_TOKEN_KINDS = frozenset({"open", "reduce_only"})
_STRATEGY_KINDS = frozenset({"real", "synthetic"})
_MARK_SOURCES = frozenset({"best_bid", "best_ask"})
_SIDES = frozenset({"buy", "sell"})
_DENY_RESOLUTIONS = frozenset({"not_found", "offline_orphan"})

# §P.3 id prefixes — minted by their owners; VALIDATED (presence/format) here.
_ORDER_ID_PREFIXES = ("o-", "synthetic-o-")
_DECISION_ID_PREFIXES = ("d-",)
_PREFLIGHT_ID_PREFIXES = ("pf-",)
_RISK_VERDICT_ID_PREFIXES = ("rv-",)
_MODELED_FILL_ID_PREFIXES = ("mf-",)
_POSITION_ID_PREFIXES = ("pos-",)

# Pinned §P.2 literals (resolution 6).
_THRESHOLD_BPS = "10"
_PNL_BASIS = {"broker": "broker_fills", "modeled": "modeled_fill_plus_fees"}
_USED_FOR_STRATEGY_EVALUATION = "execution_realistic_pnl"
_POSITION_SIDE = "long"

# order_ids that can no longer be live (resolution 9).
_ORDER_CLOSING_EVENTS = frozenset({EVT_ORDER_TERMINAL, EVT_BROKER_REJECT,
                                   EVT_REJECT})

# Pinned context for the ONE derived value journaled here (cum_notional_after) —
# ambient-context immunity (the M4-DET-1 / quote_quality.py:23 precedent).
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


def as_modeled_usd(value) -> ModeledUSD:
    """LOCAL modeled-lineage guard (M5C-S10) mirroring `serializer.as_broker_usd`:
    only a finite `ModeledUSD` is accepted — `BrokerUSD` (a sibling Decimal
    subclass) and plain Decimal both raise TypeError. serializer.py is NOT
    edited (§3)."""
    if not isinstance(value, ModeledUSD):
        raise TypeError("modeled-lineage field requires ModeledUSD")
    if not value.is_finite():
        raise ValueError("non-finite ModeledUSD")
    return value


def _opt_modeled_usd(value, *, field: str) -> Optional[ModeledUSD]:
    if value is None:
        return None
    try:
        return as_modeled_usd(value)
    except TypeError:
        raise TypeError(f"{field}: modeled-lineage field requires ModeledUSD "
                        f"(got {type(value).__name__})")


def _broker_usd(value, *, field: str):
    try:
        return as_broker_usd(value)
    except TypeError:
        raise TypeError(f"{field}: broker-lineage field requires BrokerUSD "
                        f"(got {type(value).__name__})")


def _exact_decimal(value, *, field: str, allow_none: bool = False) -> Optional[Decimal]:
    """An EXACT/unquantized journaled Decimal (the M4 LD-R5 rule). Rejects
    bool/float/non-Decimal/non-finite up front (S2)."""
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{field}: required non-null Decimal (got None)")
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, Decimal):
        raise ValueError(f"{field}: must be a Decimal (got {type(value).__name__}={value!r})")
    if not value.is_finite():
        raise ValueError(f"{field}: non-finite Decimal not allowed ({value!r})")
    return value


def _require_str(value, *, field: str, allow_none: bool = False) -> Optional[str]:
    if value is None:
        if allow_none:
            return None
        raise ExecError(f"{field}: required non-null str (got None)")
    if not isinstance(value, str):
        raise ExecError(f"{field}: must be a str (got {type(value).__name__})")
    return value


def _require_int(value, *, field: str, allow_none: bool = False) -> Optional[int]:
    if value is None:
        if allow_none:
            return None
        raise ExecError(f"{field}: required non-null int (got None)")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecError(f"{field}: must be an int, bool excluded "
                        f"(got {type(value).__name__})")
    return value


def _require_bool(value, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ExecError(f"{field}: must be a bool (got {type(value).__name__})")
    return value


def _require_id(value, *, field: str, prefixes, allow_none: bool = False) -> Optional[str]:
    """§P.3 id format guard: presence + prefix only — minting is the owners' job."""
    if value is None:
        if allow_none:
            return None
        raise ExecError(f"{field}: required id (got None)")
    if not isinstance(value, str) or not any(
            value.startswith(p) and len(value) > len(p) for p in prefixes):
        raise ExecError(f"{field}: must be a str id with prefix in "
                        f"{tuple(prefixes)} (got {value!r})")
    return value


def _require_side(value, *, field: str) -> str:
    return require_member(_SIDES, value, what=field)


def _str_list_sorted(values, *, field: str) -> list:
    if isinstance(values, (str, bytes)) or isinstance(values, Mapping):
        raise ExecError(f"{field}: must be a list of strings")
    out = []
    for item in values:
        if not isinstance(item, str):
            raise ExecError(f"{field}: members must be strings (got {item!r})")
        out.append(item)
    return sorted(out)


def _reject_reasons(values) -> list:
    """reasons ⊆ PREFLIGHT_REASONS ∪ EXTRA_REJECT_REASONS, journaled sorted;
    out-of-vocab AND reserved-in-M5 both raise (require_reason)."""
    out = []
    for code in values:
        if not isinstance(code, str):
            raise ExecError(f"reject reasons: members must be strings (got {code!r})")
        if code in EXTRA_REJECT_REASONS:
            out.append(code)
        else:
            out.append(require_reason(code))
    return sorted(out)


def _stage_list(values, *, field: str) -> list:
    """stages_skipped keeps LADDER order (never sorted — §2.2 byte-exactness)."""
    if isinstance(values, (str, bytes)):
        raise ExecError(f"{field}: must be a list of stages")
    return [require_stage(stage) for stage in values]


def _provenance(value, *, field: str, book_hash: bool = False,
                allow_none: bool = False) -> Optional[dict]:
    """The frozen §P.2 provenance sub-dict — EXACT key set, typed slots; the
    `book_hash` variant (modeled fill quote) carries the §I depth provenance."""
    if value is None:
        if allow_none:
            return None
        raise ExecError(f"{field}: required provenance dict (got None)")
    if not isinstance(value, Mapping):
        raise ExecError(f"{field}: must be a Mapping (got {type(value).__name__})")
    expected = _PROVENANCE_KEYS + (("book_hash",) if book_hash else ())
    if set(value.keys()) != set(expected):
        raise ExecError(f"{field}: key set must be exactly {sorted(expected)}, "
                        f"got {sorted(value.keys())}")
    out = {}
    for key in ("dataset", "schema", "ts_event_utc", "ts_recv_utc"):
        out[key] = _require_str(value[key], field=f"{field}.{key}")
    out["seen_at_ms"] = _require_int(value["seen_at_ms"], field=f"{field}.seen_at_ms")
    out["reconnect_epoch"] = _require_int(value["reconnect_epoch"],
                                          field=f"{field}.reconnect_epoch")
    out["vendor_seq"] = _require_int(value["vendor_seq"],
                                     field=f"{field}.vendor_seq", allow_none=True)
    if book_hash:
        out["book_hash"] = _require_str(value["book_hash"],
                                        field=f"{field}.book_hash", allow_none=True)
    return out


def _fee_block(value, *, field: str) -> dict:
    """{model_version, sec_usd, taf_usd, total_usd} — a Mapping with EXACTLY
    those keys, or a §J `FeeAssumption`-shaped object exposing them."""
    if isinstance(value, Mapping):
        if set(value.keys()) != set(_FEE_KEYS):
            raise ExecError(f"{field}: key set must be exactly {sorted(_FEE_KEYS)}, "
                            f"got {sorted(value.keys())}")
        get = value.__getitem__
    elif value is None:
        raise ExecError(f"{field}: required fee block (got None)")
    else:
        def get(key, _obj=value, _field=field):
            try:
                return getattr(_obj, key)
            except AttributeError:
                raise ExecError(f"{_field}: fee block missing attribute {key!r}")
    return {
        "model_version": _require_str(get("model_version"),
                                      field=f"{field}.model_version"),
        "sec_usd": _exact_decimal(get("sec_usd"), field=f"{field}.sec_usd"),
        "taf_usd": _exact_decimal(get("taf_usd"), field=f"{field}.taf_usd"),
        "total_usd": _exact_decimal(get("total_usd"), field=f"{field}.total_usd"),
    }


def _detail_block(value) -> dict:
    """reject.detail {risk_reasons|null, quote_reasons|null, broker_code|null,
    broker_message|null} — exact key set; the M4 risk reasons ride here verbatim
    (never the §2.1 vocabulary)."""
    if not isinstance(value, Mapping):
        raise ExecError(f"detail: must be a Mapping (got {type(value).__name__})")
    if set(value.keys()) != set(_DETAIL_KEYS):
        raise ExecError(f"detail: key set must be exactly {sorted(_DETAIL_KEYS)}, "
                        f"got {sorted(value.keys())}")
    risk_reasons = value["risk_reasons"]
    quote_reasons = value["quote_reasons"]
    return {
        "risk_reasons": None if risk_reasons is None
        else _str_list_sorted(risk_reasons, field="detail.risk_reasons"),
        "quote_reasons": None if quote_reasons is None
        else _str_list_sorted(quote_reasons, field="detail.quote_reasons"),
        "broker_code": _require_int(value["broker_code"],
                                    field="detail.broker_code", allow_none=True),
        "broker_message": _require_str(value["broker_message"],
                                       field="detail.broker_message",
                                       allow_none=True),
    }


def _order_intent(value) -> dict:
    """order_intent {order_type:"marketable_limit", tif:"day", limit_price} —
    the two literals are §P.2-pinned; limit_price is a required exact Decimal
    (FD-M5-1: no limit=None ever reaches a submit attempt)."""
    if not isinstance(value, Mapping):
        raise ExecError(f"order_intent: must be a Mapping (got {type(value).__name__})")
    if set(value.keys()) != set(_INTENT_KEYS):
        raise ExecError(f"order_intent: key set must be exactly {sorted(_INTENT_KEYS)}, "
                        f"got {sorted(value.keys())}")
    if value["order_type"] != "marketable_limit":
        raise ExecError(f"order_intent.order_type: must be 'marketable_limit' "
                        f"(got {value['order_type']!r})")
    if value["tif"] != "day":
        raise ExecError(f"order_intent.tif: must be 'day' (got {value['tif']!r})")
    return {
        "order_type": "marketable_limit",
        "tif": "day",
        "limit_price": _exact_decimal(value["limit_price"],
                                      field="order_intent.limit_price"),
    }


def _levels(value, *, field: str) -> Optional[list]:
    """levels_consumed: EXACT (px, qty) list-of-pairs in WALK order (the §I
    consumed prefix — order is meaningful, never sorted); None for tob/unfillable."""
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping):
        raise ExecError(f"{field}: must be a sequence of (px, qty) pairs")
    out = []
    for i, level in enumerate(value):
        pair = list(level) if isinstance(level, (list, tuple)) else None
        if pair is None or len(pair) != 2:
            raise ExecError(f"{field}[{i}]: must be a (px, qty) 2-pair")
        out.append([
            _exact_decimal(pair[0], field=f"{field}[{i}].px"),
            _exact_decimal(pair[1], field=f"{field}[{i}].qty"),
        ])
    return out


def _attr(obj, name: str, *, what: str):
    try:
        return getattr(obj, name)
    except AttributeError:
        raise ExecError(f"{what}: missing attribute {name!r} "
                        f"(got {type(obj).__name__})")


class ExecLedger:
    """One kwarg-only record_* method per §P.2 event type (10 on orders.jsonl,
    4 on fills.jsonl, 4 on positions.jsonl); each validates the EXACT field set
    (kwarg-only signatures: missing/extra => TypeError), the per-field vocab /
    type rules, the money-lineage walls, and refuses `_RESERVED` collisions —
    all BEFORE anything is written (a raise leaves the stream untouched)."""

    def __init__(self, *, orders: EventWriter, fills: EventWriter,
                 positions: EventWriter, rules_hash: str) -> None:
        self._orders = orders
        self._fills = fills
        self._positions = positions
        self._rules_hash = _require_str(rules_hash, field="rules_hash")

    def _record(self, writer, event_type: str, body: dict, *,
                decision_id=None, order_id=None) -> dict:
        fields = {"v": EXEC_LEDGER_VERSION, **body, "rules_hash": self._rules_hash}
        collisions = _RESERVED & set(fields)
        if collisions:
            raise ExecError(f"payload keys collide with journal reserved keys: "
                            f"{sorted(collisions)}")
        return writer.record(event_type, fields,
                             decision_id=decision_id, order_id=order_id)

    # --- journal/orders.jsonl ------------------------------------------------

    def record_strategy_decision(self, *, symbol, instrument_id, strategy_id,
                                 strategy_kind, action, side, qty,
                                 strategy_limit, score, paper_eligible,
                                 position_id, event_basis, decision_ts_utc,
                                 decision_seen_at_ms, quote_a,
                                 decision_id) -> dict:
        action = require_member(STRATEGY_DECISION_ACTIONS, action,
                                what="strategy decision action")
        if action == "would_close":
            position_id = _require_id(position_id, field="position_id",
                                      prefixes=_POSITION_ID_PREFIXES)
        elif position_id is not None:
            raise ExecError("position_id: must be None on would_open "
                            "(§P.2: set on would_close)")
        body = {
            "symbol": _require_str(symbol, field="symbol"),
            "instrument_id": _require_int(instrument_id, field="instrument_id"),
            "strategy_id": _require_str(strategy_id, field="strategy_id"),
            "strategy_kind": require_member(_STRATEGY_KINDS, strategy_kind,
                                            what="strategy_kind"),
            "action": action,
            "side": _require_side(side, field="side"),
            "qty": _exact_decimal(qty, field="qty"),
            "strategy_limit": _exact_decimal(strategy_limit,
                                             field="strategy_limit",
                                             allow_none=True),
            "score": _exact_decimal(score, field="score", allow_none=True),
            "paper_eligible": _require_bool(paper_eligible,
                                            field="paper_eligible"),
            "position_id": position_id,
            "event_basis": _require_str(event_basis, field="event_basis"),
            "decision_ts_utc": _require_str(decision_ts_utc,
                                            field="decision_ts_utc"),
            "decision_seen_at_ms": _require_int(decision_seen_at_ms,
                                                field="decision_seen_at_ms"),
            "quote_a": _provenance(quote_a, field="quote_a"),
        }
        return self._record(
            self._orders, EVT_STRATEGY_DECISION, body,
            decision_id=_require_id(decision_id, field="decision_id",
                                    prefixes=_DECISION_ID_PREFIXES))

    def record_reject(self, *, symbol, instrument_id, strategy_id, stage,
                      reasons, stages_skipped, detail, preflight_id,
                      capped_limit, token_kind, kill_state, kill_generation,
                      quote_b, decision_id, order_id=None) -> dict:
        body = {
            "symbol": _require_str(symbol, field="symbol"),
            "instrument_id": _require_int(instrument_id, field="instrument_id"),
            "strategy_id": _require_str(strategy_id, field="strategy_id"),
            "stage": None if stage is None else require_stage(stage),
            "reasons": _reject_reasons(reasons),
            "stages_skipped": _stage_list(stages_skipped,
                                          field="stages_skipped"),
            "detail": _detail_block(detail),
            "preflight_id": _require_id(preflight_id, field="preflight_id",
                                        prefixes=_PREFLIGHT_ID_PREFIXES,
                                        allow_none=True),
            "capped_limit": _exact_decimal(capped_limit, field="capped_limit",
                                           allow_none=True),
            "token_kind": require_member(_TOKEN_KINDS, token_kind,
                                         what="token_kind"),
            "kill_state": _require_str(kill_state, field="kill_state"),
            "kill_generation": _require_int(kill_generation,
                                            field="kill_generation"),
            "quote_b": _provenance(quote_b, field="quote_b", allow_none=True),
        }
        return self._record(
            self._orders, EVT_REJECT, body,
            decision_id=_require_id(decision_id, field="decision_id",
                                    prefixes=_DECISION_ID_PREFIXES),
            order_id=_require_id(order_id, field="order_id",
                                 prefixes=_ORDER_ID_PREFIXES, allow_none=True))

    def record_order_submit_attempt(self, *, client_order_id, preflight_id,
                                    risk_verdict_id, strategy_id, symbol,
                                    instrument_id, side, qty, order_intent,
                                    token_kind, kill_generation, quote_b,
                                    decision_id, order_id) -> dict:
        order_id = _require_id(order_id, field="order_id",
                               prefixes=_ORDER_ID_PREFIXES)
        if client_order_id != order_id:
            raise ExecError(f"client_order_id must equal order_id (FD-M5-7): "
                            f"{client_order_id!r} != {order_id!r}")
        body = {
            "client_order_id": client_order_id,
            "preflight_id": _require_id(preflight_id, field="preflight_id",
                                        prefixes=_PREFLIGHT_ID_PREFIXES,
                                        allow_none=True),  # null on reduce path
            "risk_verdict_id": _require_id(risk_verdict_id,
                                           field="risk_verdict_id",
                                           prefixes=_RISK_VERDICT_ID_PREFIXES,
                                           allow_none=True),
            "strategy_id": _require_str(strategy_id, field="strategy_id"),
            "symbol": _require_str(symbol, field="symbol"),
            "instrument_id": _require_int(instrument_id, field="instrument_id"),
            "side": _require_side(side, field="side"),
            "qty": _exact_decimal(qty, field="qty"),
            "order_intent": _order_intent(order_intent),
            "token_kind": require_member(_TOKEN_KINDS, token_kind,
                                         what="token_kind"),
            "kill_generation": _require_int(kill_generation,
                                            field="kill_generation"),
            "quote_b": _provenance(quote_b, field="quote_b", allow_none=True),
        }
        return self._record(
            self._orders, EVT_ORDER_SUBMIT_ATTEMPT, body,
            decision_id=_require_id(decision_id, field="decision_id",
                                    prefixes=_DECISION_ID_PREFIXES),
            order_id=order_id)

    def record_order_submitted(self, *, client_order_id, broker_order_id,
                               state, raw_status, ts_broker_utc, source,
                               decision_id, order_id) -> dict:
        order_id = _require_id(order_id, field="order_id",
                               prefixes=_ORDER_ID_PREFIXES)
        if client_order_id != order_id:
            raise ExecError(f"client_order_id must equal order_id (FD-M5-7): "
                            f"{client_order_id!r} != {order_id!r}")
        body = {
            "client_order_id": client_order_id,
            "broker_order_id": _require_str(broker_order_id,
                                            field="broker_order_id"),
            "state": require_member(ORDER_STATES, state, what="order state"),
            "raw_status": _require_str(raw_status, field="raw_status"),
            "ts_broker_utc": _require_str(ts_broker_utc, field="ts_broker_utc",
                                          allow_none=True),
            "source": require_member(FILL_SOURCES, source, what="fill source"),
        }
        return self._record(
            self._orders, EVT_ORDER_SUBMITTED, body,
            decision_id=_require_id(decision_id, field="decision_id",
                                    prefixes=_DECISION_ID_PREFIXES),
            order_id=order_id)

    def record_broker_order_update(self, *, broker_order_id, from_state,
                                   to_state, raw_status, filled_qty,
                                   filled_avg_price, ts_broker_utc,
                                   order_id) -> dict:
        body = {
            "broker_order_id": _require_str(broker_order_id,
                                            field="broker_order_id"),
            "from_state": require_member(ORDER_STATES, from_state,
                                         what="order state"),
            "to_state": require_member(ORDER_STATES, to_state,
                                       what="order state"),
            "raw_status": _require_str(raw_status, field="raw_status"),
            "filled_qty": _exact_decimal(filled_qty, field="filled_qty"),
            "filled_avg_price": _exact_decimal(filled_avg_price,
                                               field="filled_avg_price",
                                               allow_none=True),
            "ts_broker_utc": _require_str(ts_broker_utc, field="ts_broker_utc",
                                          allow_none=True),
        }
        return self._record(
            self._orders, EVT_BROKER_ORDER_UPDATE, body,
            order_id=_require_id(order_id, field="order_id",
                                 prefixes=_ORDER_ID_PREFIXES))

    def record_broker_reject(self, *, broker_order_id, http_status,
                             broker_code, message, pdt_marker_matched,
                             decision_id, order_id) -> dict:
        body = {
            "broker_order_id": _require_str(broker_order_id,
                                            field="broker_order_id",
                                            allow_none=True),
            "http_status": _require_int(http_status, field="http_status",
                                        allow_none=True),
            "broker_code": _require_int(broker_code, field="broker_code",
                                        allow_none=True),
            "message": _require_str(message, field="message"),
            "pdt_marker_matched": _require_bool(pdt_marker_matched,
                                                field="pdt_marker_matched"),
        }
        return self._record(
            self._orders, EVT_BROKER_REJECT, body,
            decision_id=_require_id(decision_id, field="decision_id",
                                    prefixes=_DECISION_ID_PREFIXES),
            order_id=_require_id(order_id, field="order_id",
                                 prefixes=_ORDER_ID_PREFIXES))

    def record_order_submit_unconfirmed(self, *, client_order_id, error,
                                        attempts, resolution,
                                        order_id) -> dict:
        order_id = _require_id(order_id, field="order_id",
                               prefixes=_ORDER_ID_PREFIXES)
        if client_order_id != order_id:
            raise ExecError(f"client_order_id must equal order_id (FD-M5-7): "
                            f"{client_order_id!r} != {order_id!r}")
        body = {
            "client_order_id": client_order_id,
            "error": _require_str(error, field="error"),
            "attempts": _require_int(attempts, field="attempts"),
            "resolution": require_member(SUBMIT_RESOLUTIONS, resolution,
                                         what="submit resolution"),
        }
        return self._record(self._orders, EVT_ORDER_SUBMIT_UNCONFIRMED, body,
                            order_id=order_id)

    def record_post_submit_cancel_attempt(self, *, broker_order_id, cause,
                                          outcome, broker_state_at_attempt,
                                          order_id) -> dict:
        body = {
            "broker_order_id": _require_str(broker_order_id,
                                            field="broker_order_id",
                                            allow_none=True),
            "cause": require_member(CANCEL_CAUSES, cause, what="cancel cause"),
            "outcome": require_member(CANCEL_OUTCOMES, outcome,
                                      what="cancel outcome"),
            "broker_state_at_attempt": None if broker_state_at_attempt is None
            else require_member(ORDER_STATES, broker_state_at_attempt,
                                what="order state"),
        }
        return self._record(
            self._orders, EVT_POST_SUBMIT_CANCEL_ATTEMPT, body,
            order_id=_require_id(order_id, field="order_id",
                                 prefixes=_ORDER_ID_PREFIXES))

    def record_order_state_alert(self, *, broker_order_id, raw_status, note,
                                 order_id) -> dict:
        body = {
            "broker_order_id": _require_str(broker_order_id,
                                            field="broker_order_id",
                                            allow_none=True),
            "raw_status": _require_str(raw_status, field="raw_status"),
            "note": _require_str(note, field="note"),
        }
        return self._record(
            self._orders, EVT_ORDER_STATE_ALERT, body,
            order_id=_require_id(order_id, field="order_id",
                                 prefixes=_ORDER_ID_PREFIXES))

    def record_order_terminal(self, *, terminal_state, filled_qty,
                              cum_notional_usd, ts_broker_utc, decision_id,
                              order_id) -> dict:
        body = {
            "terminal_state": require_member(TERMINAL_STATES, terminal_state,
                                             what="terminal state"),
            "filled_qty": _exact_decimal(filled_qty, field="filled_qty"),
            "cum_notional_usd": _broker_usd(cum_notional_usd,
                                            field="cum_notional_usd"),
            "ts_broker_utc": _require_str(ts_broker_utc, field="ts_broker_utc",
                                          allow_none=True),
        }
        return self._record(
            self._orders, EVT_ORDER_TERMINAL, body,
            decision_id=_require_id(decision_id, field="decision_id",
                                    prefixes=_DECISION_ID_PREFIXES),
            order_id=_require_id(order_id, field="order_id",
                                 prefixes=_ORDER_ID_PREFIXES))

    # --- journal/fills.jsonl -------------------------------------------------

    def record_broker_fill(self, *, delta, cur, position_id, liquidity_flag,
                           venue, decision_id, order_id) -> dict:
        """Journal one emitted fill increment. `delta` is the §F `FillDelta`
        and `cur` the `BrokerOrder` snapshot it was computed from (duck-typed —
        resolution 1). `fill_id` is computed HERE (the ONE home, M5C-T7):
        `"bf-" + row_hash({order_id, cum_filled_qty_after, cum_notional_after})`
        with `cum_notional_after = cur.filled_qty x cur.filled_avg_price` —
        the exact FD-M5-18 telescoped total, journaled on the row."""
        order_id = _require_id(order_id, field="order_id",
                               prefixes=_ORDER_ID_PREFIXES)
        delta_cost_usd = _broker_usd(_attr(delta, "delta_cost_usd",
                                           what="delta"),
                                     field="delta_cost_usd")
        delta_qty = _exact_decimal(_attr(delta, "delta_qty", what="delta"),
                                   field="delta_qty")
        if delta_qty <= 0:
            raise ExecError(f"delta_qty: must be > 0 (FD-M5-18), got {delta_qty}")
        cum_filled_qty = _exact_decimal(
            _attr(delta, "cum_filled_qty", what="delta"), field="cum_filled_qty")
        filled_avg_price_after = _exact_decimal(
            _attr(delta, "filled_avg_price_after", what="delta"),
            field="filled_avg_price_after")
        cur_filled_qty = _exact_decimal(_attr(cur, "filled_qty", what="cur"),
                                        field="cur.filled_qty")
        cur_avg = _exact_decimal(_attr(cur, "filled_avg_price", what="cur"),
                                 field="cur.filled_avg_price")
        # The delta must have been computed FROM this snapshot — byte-identical
        # operands keep the journaled row and the id derivation in lockstep.
        if str(cum_filled_qty) != str(cur_filled_qty):
            raise ExecError(f"delta.cum_filled_qty {cum_filled_qty} != "
                            f"cur.filled_qty {cur_filled_qty} (the delta was "
                            f"not computed from this snapshot)")
        if str(filled_avg_price_after) != str(cur_avg):
            raise ExecError(f"delta.filled_avg_price_after "
                            f"{filled_avg_price_after} != cur.filled_avg_price "
                            f"{cur_avg}")
        with localcontext(_DECIMAL_CTX):
            cum_notional_after = cur_filled_qty * cur_avg  # EXACT (§P.3, frozen)
        fill_id = "bf-" + row_hash({
            "order_id": order_id,
            "cum_filled_qty_after": cur_filled_qty,
            "cum_notional_after": cum_notional_after,
        })
        body = {
            "fill_id": fill_id,
            "broker_order_id": _require_str(
                _attr(cur, "broker_order_id", what="cur"),
                field="cur.broker_order_id"),
            "position_id": _require_id(position_id, field="position_id",
                                       prefixes=_POSITION_ID_PREFIXES,
                                       allow_none=True),
            "symbol": _require_str(_attr(cur, "symbol", what="cur"),
                                   field="cur.symbol"),
            "side": _require_side(_attr(cur, "side", what="cur"),
                                  field="cur.side"),
            "delta_qty": delta_qty,
            "delta_cost_usd": delta_cost_usd,
            "cum_filled_qty": cum_filled_qty,
            "filled_avg_price_after": filled_avg_price_after,
            "cum_notional_after": cum_notional_after,
            "liquidity_flag": _require_str(liquidity_flag,
                                           field="liquidity_flag",
                                           allow_none=True),
            "venue": _require_str(venue, field="venue", allow_none=True),
            "ts_broker_utc": _require_str(_attr(cur, "ts_broker_utc",
                                                what="cur"),
                                          field="cur.ts_broker_utc",
                                          allow_none=True),
            "source": require_member(FILL_SOURCES,
                                     _attr(cur, "source", what="cur"),
                                     what="fill source"),
        }
        return self._record(
            self._fills, EVT_BROKER_FILL, body,
            decision_id=_require_id(decision_id, field="decision_id",
                                    prefixes=_DECISION_ID_PREFIXES),
            order_id=order_id)

    def record_modeled_execution_fill(self, *, modeled_fill_id, model,
                                      realism_class, requested_qty,
                                      modeled_fillable_qty, modeled_vwap,
                                      worst_price, touch_price,
                                      levels_consumed, slippage_vs_mid_bps,
                                      modeled_cost_usd, fees_assumed, quote,
                                      reasons, decision_id, order_id) -> dict:
        body = {
            "modeled_fill_id": _require_id(modeled_fill_id,
                                           field="modeled_fill_id",
                                           prefixes=_MODELED_FILL_ID_PREFIXES),
            "model": require_member(MODELED_FILL_MODELS, model,
                                    what="modeled fill model"),
            "realism_class": require_member(REALISM_CLASSES, realism_class,
                                            what="realism class"),
            "requested_qty": _exact_decimal(requested_qty,
                                            field="requested_qty"),
            "modeled_fillable_qty": _exact_decimal(modeled_fillable_qty,
                                                   field="modeled_fillable_qty"),
            "modeled_vwap": _exact_decimal(modeled_vwap, field="modeled_vwap",
                                           allow_none=True),
            "worst_price": _exact_decimal(worst_price, field="worst_price",
                                          allow_none=True),
            "touch_price": _exact_decimal(touch_price, field="touch_price",
                                          allow_none=True),
            "levels_consumed": _levels(levels_consumed,
                                       field="levels_consumed"),
            "slippage_vs_mid_bps": _exact_decimal(slippage_vs_mid_bps,
                                                  field="slippage_vs_mid_bps",
                                                  allow_none=True),
            "modeled_cost_usd": _opt_modeled_usd(modeled_cost_usd,
                                                 field="modeled_cost_usd"),
            "fees_assumed": _fee_block(fees_assumed, field="fees_assumed"),
            "quote": _provenance(quote, field="quote", book_hash=True),
            "reasons": _str_list_sorted(reasons, field="reasons"),
        }
        return self._record(
            self._fills, EVT_MODELED_EXECUTION_FILL, body,
            decision_id=_require_id(decision_id, field="decision_id",
                                    prefixes=_DECISION_ID_PREFIXES),
            order_id=_require_id(order_id, field="order_id",
                                 prefixes=_ORDER_ID_PREFIXES))

    def record_fill_divergence(self, *, side, broker_cost_usd,
                               modeled_cost_usd, divergence_usd,
                               divergence_bps, flag, order_id) -> dict:
        flag = require_member(DIVERGENCE_FLAGS, flag, what="divergence flag")
        modeled_cost_usd = _opt_modeled_usd(modeled_cost_usd,
                                            field="modeled_cost_usd")
        divergence_usd = _exact_decimal(divergence_usd, field="divergence_usd",
                                        allow_none=True)
        divergence_bps = _exact_decimal(divergence_bps, field="divergence_bps",
                                        allow_none=True)
        # FD-M5-20: unassessed <=> the modeled side is null (resolution 7).
        unassessed = modeled_cost_usd is None
        if ((flag == "unassessed") != unassessed
                or (divergence_usd is None) != unassessed
                or (divergence_bps is None) != unassessed):
            raise ExecError("fill_divergence invariant broken (FD-M5-20): "
                            "flag == 'unassessed' <=> modeled side null <=> "
                            "divergence fields null")
        body = {
            "side": _require_side(side, field="side"),
            "broker_cost_usd": _broker_usd(broker_cost_usd,
                                           field="broker_cost_usd"),
            "modeled_cost_usd": modeled_cost_usd,
            "divergence_usd": divergence_usd,   # plain Decimal (the FD-M5-20 seam)
            "divergence_bps": divergence_bps,
            "flag": flag,
        }
        return self._record(
            self._fills, EVT_FILL_DIVERGENCE, body,
            order_id=_require_id(order_id, field="order_id",
                                 prefixes=_ORDER_ID_PREFIXES))

    def record_divergence_alert(self, *, divergence_usd, divergence_bps,
                                order_id) -> dict:
        body = {
            "divergence_usd": _exact_decimal(divergence_usd,
                                             field="divergence_usd"),
            "divergence_bps": _exact_decimal(divergence_bps,
                                             field="divergence_bps"),
            "threshold_bps": _THRESHOLD_BPS,  # §P.2-pinned literal
        }
        return self._record(
            self._fills, EVT_DIVERGENCE_ALERT, body,
            order_id=_require_id(order_id, field="order_id",
                                 prefixes=_ORDER_ID_PREFIXES))

    # --- journal/positions.jsonl ----------------------------------------------

    def record_position_open(self, *, position_id, symbol, instrument_id,
                             side, qty, broker_cost_usd, modeled_cost_usd,
                             fee_assumption, opening_order_id, strategy_id,
                             opened_ts_utc, decision_id, order_id) -> dict:
        if side != _POSITION_SIDE:
            raise ExecError(f"position side: must be {_POSITION_SIDE!r} in M5 "
                            f"(§P.2), got {side!r}")
        body = {
            "position_id": _require_id(position_id, field="position_id",
                                       prefixes=_POSITION_ID_PREFIXES),
            "symbol": _require_str(symbol, field="symbol"),
            "instrument_id": _require_int(instrument_id, field="instrument_id"),
            "side": side,
            "qty": _exact_decimal(qty, field="qty"),
            "broker_cost_usd": _broker_usd(broker_cost_usd,
                                           field="broker_cost_usd"),
            "modeled_cost_usd": _opt_modeled_usd(modeled_cost_usd,
                                                 field="modeled_cost_usd"),
            "fee_assumption": _fee_block(fee_assumption,
                                         field="fee_assumption"),
            "opening_order_id": _require_id(opening_order_id,
                                            field="opening_order_id",
                                            prefixes=_ORDER_ID_PREFIXES),
            "strategy_id": _require_str(strategy_id, field="strategy_id"),
            "opened_ts_utc": _require_str(opened_ts_utc,
                                          field="opened_ts_utc"),
        }
        return self._record(
            self._positions, EVT_POSITION_OPEN, body,
            decision_id=_require_id(decision_id, field="decision_id",
                                    prefixes=_DECISION_ID_PREFIXES),
            order_id=_require_id(order_id, field="order_id",
                                 prefixes=_ORDER_ID_PREFIXES))

    def record_mark(self, *, position_id, mark_price, mark_source, quote,
                    unrealized_broker_usd, unrealized_modeled_usd,
                    bar_key) -> dict:
        body = {
            "position_id": _require_id(position_id, field="position_id",
                                       prefixes=_POSITION_ID_PREFIXES),
            "mark_price": _exact_decimal(mark_price, field="mark_price"),
            "mark_source": require_member(_MARK_SOURCES, mark_source,
                                          what="mark source"),
            "quote": _provenance(quote, field="quote"),
            # quantize-only provenance (§P.2): recomputed live, fold-exempt —
            # any finite Decimal is accepted, no lineage wall here.
            "unrealized_broker_usd": _exact_decimal(
                unrealized_broker_usd, field="unrealized_broker_usd"),
            "unrealized_modeled_usd": _exact_decimal(
                unrealized_modeled_usd, field="unrealized_modeled_usd",
                allow_none=True),
            "bar_key": _require_str(bar_key, field="bar_key"),
        }
        return self._record(self._positions, EVT_MARK, body)

    def record_pnl_snapshot(self, *, position_id, broker_account_pnl,
                            execution_realistic_pnl, divergence_flag,
                            bar_key) -> dict:
        body = {
            "position_id": _require_id(position_id, field="position_id",
                                       prefixes=_POSITION_ID_PREFIXES),
            "broker_account_pnl": _broker_usd(broker_account_pnl,
                                              field="broker_account_pnl"),
            "execution_realistic_pnl": _opt_modeled_usd(
                execution_realistic_pnl, field="execution_realistic_pnl"),
            "divergence_flag": require_member(DIVERGENCE_FLAGS,
                                              divergence_flag,
                                              what="divergence flag"),
            "basis": dict(_PNL_BASIS),                       # §P.2-pinned
            "used_for_strategy_evaluation": _USED_FOR_STRATEGY_EVALUATION,
            "bar_key": _require_str(bar_key, field="bar_key"),
        }
        return self._record(self._positions, EVT_PNL_SNAPSHOT, body)

    def record_position_close(self, *, position_id, closing_order_id,
                              exit_qty, broker_exit_notional_usd,
                              closed_slice_broker_cost_usd,
                              residual_broker_cost_usd,
                              closed_slice_modeled_cost_usd,
                              residual_modeled_cost_usd, realized_broker_pnl,
                              realized_modeled_pnl, fees_assessed, reason,
                              decision_id, order_id) -> dict:
        body = {
            "position_id": _require_id(position_id, field="position_id",
                                       prefixes=_POSITION_ID_PREFIXES),
            "closing_order_id": _require_id(closing_order_id,
                                            field="closing_order_id",
                                            prefixes=_ORDER_ID_PREFIXES),
            "exit_qty": _exact_decimal(exit_qty, field="exit_qty"),
            "broker_exit_notional_usd": _broker_usd(
                broker_exit_notional_usd, field="broker_exit_notional_usd"),
            # the EX-4 allocation outputs: plain EXACT Decimals (the §K
            # quantize/subtract arithmetic strips the newtype by construction)
            "closed_slice_broker_cost_usd": _exact_decimal(
                closed_slice_broker_cost_usd,
                field="closed_slice_broker_cost_usd"),
            "residual_broker_cost_usd": _exact_decimal(
                residual_broker_cost_usd, field="residual_broker_cost_usd"),
            "closed_slice_modeled_cost_usd": _exact_decimal(
                closed_slice_modeled_cost_usd,
                field="closed_slice_modeled_cost_usd", allow_none=True),
            "residual_modeled_cost_usd": _exact_decimal(
                residual_modeled_cost_usd, field="residual_modeled_cost_usd",
                allow_none=True),
            "realized_broker_pnl": _broker_usd(realized_broker_pnl,
                                               field="realized_broker_pnl"),
            "realized_modeled_pnl": _opt_modeled_usd(
                realized_modeled_pnl, field="realized_modeled_pnl"),
            "fees_assessed": _fee_block(fees_assessed, field="fees_assessed"),
            "reason": require_member(CLOSE_REASONS, reason,
                                     what="close reason"),
        }
        return self._record(
            self._positions, EVT_POSITION_CLOSE, body,
            decision_id=_require_id(decision_id, field="decision_id",
                                    prefixes=_DECISION_ID_PREFIXES),
            order_id=_require_id(order_id, field="order_id",
                                 prefixes=_ORDER_ID_PREFIXES))


# --- replay (S3 — the shared code path, semantics inherited verbatim) ----------

def replay_orders(path) -> list:
    """Re-read journal/orders.jsonl hash-verified (truncated-tail dropped,
    complete corrupt line => JournalCorruption — journal.py semantics)."""
    return replay_stream(path)


def replay_fills(path) -> list:
    return replay_stream(path)


def replay_positions(path) -> list:
    return replay_stream(path)


# --- rehydrate (pure fold by ascending seq; §P.1) -------------------------------

def _group_position_rows(position_rows, fill_rows) -> dict:
    """Default `book_rehydrate` pass-through (resolution 8): rows grouped per
    `position_id` in ascending seq —
    {position_id: {"position_rows": [...], "fill_rows": [...]}}. Rows without a
    `position_id` payload field (or with a null one, e.g. a pre-position
    broker_fill) are not grouped. The paper_book wave replaces this with
    `PaperBook.rehydrate`."""
    grouped: dict = {}
    for row in position_rows:
        pid = row.get("position_id")
        if pid is None:
            continue
        slot = grouped.setdefault(pid, {"position_rows": [], "fill_rows": []})
        slot["position_rows"].append(row)
    for row in fill_rows:
        pid = row.get("position_id")
        if pid is None:
            continue
        slot = grouped.setdefault(pid, {"position_rows": [], "fill_rows": []})
        slot["fill_rows"].append(row)
    return grouped


def rehydrate_exec_state(order_rows, fill_rows, position_rows, *, run_id,
                         book_rehydrate=None) -> dict:
    """PURE fold by ascending journal `seq` (§P.1) ->
    {"open_orders": {order_id -> latest state row},
     "positions": book_rehydrate(position_rows, fill_rows),
     "open_deny": tuple of symbols}.

    - `open_orders`: latest row per order_id; an order leaves the set on
      `order_terminal` / `broker_reject` / `reject`-with-order_id (resolution
      9). Unconfirmed orders stay (presumed-live until reconciled, FD-M5-17).
    - `open_deny` is PER-RUN: rebuilt from `order_submit_unconfirmed` rows of
      THIS `run_id` only, resolutions `not_found`/`offline_orphan` (§P.2);
      symbols resolve via the order's write-ahead `order_submit_attempt` row
      (any run — recovery rows reference prior-run orders under the new
      run_id). Sorted, deduped, deterministic.
    - `run_id`/`book_rehydrate` are documented additions to the §P.1 sketch
      (module docstring, resolution 8)."""
    run_id = _require_str(run_id, field="run_id")
    orders_sorted = sorted(order_rows, key=lambda r: r["seq"])
    fills_sorted = sorted(fill_rows, key=lambda r: r["seq"])
    positions_sorted = sorted(position_rows, key=lambda r: r["seq"])

    latest: dict = {}
    closed = set()
    attempt_symbol: dict = {}
    deny = set()
    for row in orders_sorted:
        order_id = row.get("order_id")
        if order_id is None:
            continue
        event_type = row.get("event_type")
        if event_type == EVT_ORDER_SUBMIT_ATTEMPT:
            attempt_symbol[order_id] = row["symbol"]
        latest[order_id] = row
        if event_type in _ORDER_CLOSING_EVENTS:
            closed.add(order_id)
        if (event_type == EVT_ORDER_SUBMIT_UNCONFIRMED
                and row.get("run_id") == run_id
                and row.get("resolution") in _DENY_RESOLUTIONS):
            if order_id not in attempt_symbol:
                raise ExecError(
                    f"order_submit_unconfirmed for {order_id!r} has no "
                    f"preceding order_submit_attempt row (write-ahead protocol "
                    f"violated — FD-M5-17)")
            deny.add(attempt_symbol[order_id])

    fold = book_rehydrate if book_rehydrate is not None else _group_position_rows
    return {
        "open_orders": {oid: row for oid, row in latest.items()
                        if oid not in closed},
        "positions": fold(positions_sorted, fills_sorted),
        "open_deny": tuple(sorted(deny)),
    }
