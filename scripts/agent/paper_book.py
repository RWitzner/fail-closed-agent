"""M5 §K — `PaperBook`/`PaperPosition`: position lifecycle + the S5 PnL split.

The PaperBook is journal/evaluation state, never the position-of-record (the
broker is — §0; M6's reconcile diff emits adjustment rows on divergence and the
broker number wins). It books EXACT broker costs (Σ `FillDelta.delta_cost_usd`,
never qty×avg — FD-M5-18 / parent principle 3), carries the FD-M5-19 full-qty
modeled basis beside them, marks long positions at best_bid (conservative
liquidation value), and emits the two-class PnL split: `broker_account_pnl`
(BrokerUSD, fee-free on paper — A6) and `execution_realistic_pnl` (ModeledUSD,
fees per §J) — both journaled on every `pnl_snapshot`, never collapsed (S5).

Typing wall (S5): every broker-basis assignment passes `serializer.as_broker_usd`
(a `ModeledUSD` — or any non-BrokerUSD — into a broker field is a `TypeError`);
every modeled-basis slot passes the local `_as_modeled_usd` mirror (a `BrokerUSD`
into a modeled slot is a `TypeError`). The guards run BEFORE any journal write or
state mutation, so a hostile input leaves both the streams and the book untouched.

Partial-close cost allocation (FROZEN — EX-4; §K): exact proportional slicing is
impossible for non-terminating divisions, so the rule keeps the SUM exact:

    closed_slice_cost = (cost × exit_qty / qty).quantize(CENT, ROUND_HALF_EVEN)
    residual_cost     = cost − closed_slice_cost          (EXACT by construction)

identically for both lineages; the rounding residue stays on the OPEN slice and
washes out at full close (totals telescope). Both the slice AND the residual are
journaled on `position_close` so `rehydrate` folds bytes (LD-R5).

Sign convention (pinned): a SELL `FillDelta.delta_cost_usd` is the POSITIVE sale
proceeds (`order_state.fill_delta` computes `cur qty×avg − prev qty×avg` over the
sell order's fill price and rejects `delta_cost <= 0`), so
`broker_exit_notional_usd = Σ delta_cost` of the closing fills and
`realized_broker_pnl = exit_notional − closed_slice_broker_cost` (gain positive).

Documented resolutions (faithful to the contract; none contradict a frozen pin):

1.  The ledger collaborates DUCK-TYPED: `PaperBook.__init__` takes
    `ledger: "ExecLedger"` (§K) but §3's import row for paper_book.py is
    [stdlib, agent.serializer, agent.quote_quality (types), exec_reasons, fees]
    and omits exec_ledger — so the ledger is ctor-injected and used by attribute
    calls only (`record_position_open` / `record_mark` / `record_pnl_snapshot` /
    `record_position_close`), never imported or isinstance-checked. The same §3
    pattern exec_ledger itself used for `FillDelta` (its resolution 1).
2.  `FillDelta` and `ModeledFill` are duck-typed here for the same reason: the
    pure family must not import `agent.broker*` and the §3 row omits
    `execution_realism`. A fill exposes `delta_qty`/`delta_cost_usd`; a modeled
    fill exposes `modeled_cost_usd`. A missing attribute raises `ExecError`
    (malformed collaborator input); a wrong-lineage value raises `TypeError`.
3.  `apply_fill` journals NOTHING: the ORCHESTRATOR journals `broker_fill` rows
    via `exec_ledger.record_broker_fill` as deltas land (§M.4 WATCH:
    "fill_delta -> broker_fill rows -> PaperBook") and then feeds the book.
    PaperBook journals position_open / mark / pnl_snapshot / position_close ONLY.
4.  The quote budgets are ctor-injected (documented addition to the §K two-arg
    ctor sketch): §K's `mark` pins "staleness/usability re-checked via
    quote_quality.evaluate with the committed budgets", which requires
    `quote_staleness_ms_max` (int ms) and `spread_bps_max` (Decimal bps) to be
    available — the orchestrator passes the committed signal-config values. §3
    annotates this module's quote_quality import "(types)", but §K explicitly
    pins `evaluate` as the re-check — `quote_quality.evaluate` stays the ONE
    quote decider (§2.2 stage 8 discipline); re-deriving it here would fork it.
5.  `close_position` gains a REQUIRED keyword-only `decision_id` (documented
    addition to the §K sketch, which omitted it): the §P.2 `position_close` row
    rides BOTH `decision_id` and `order_id` journal kwargs and the ledger
    requires a "d-"-prefixed decision id; the close path always has one (the
    §P.3 `"exit:..."` event-basis decision).
6.  Fee stamping (§J): `position_open.fee_assumption` = `fees_for(side="buy",
    qty, notional=broker_cost)` — the open-side ZERO assumption (projected close
    fees are NOT pre-charged). A close whose modeled exit is None computes NO
    fee (EX-9) but §P.2 requires a non-null `fees_assessed` block, so the
    explicit CENT-scale zero assumption is journaled (the §J byte form, "0.00").
7.  `fees_assessed_to_date` (the §K pnl_snapshot operand) is the cumulative
    `total_usd` of THIS position's close-side fee assessments (fees are assessed
    per SELL fill as it books — §J; the open side contributes zero). It is
    per-run derived state, recomputed live: mark/pnl/fee state does NOT fold in
    `rehydrate` (§K: "mark/pnl rows are derived state and do NOT fold").
8.  `modeled_cost_usd` is the FD-M5-19 FULL-qty basis stamped at open
    (computed once from quote B); `apply_fill` accumulates broker deltas only
    and never touches the modeled side.
9.  `pnl_snapshot.divergence_flag` (EX-11): defaults `"unassessed"`; the
    orchestrator calls `set_divergence_flag(position_id, flag)` at order-terminal
    (FD-M5-20: "the position's latest flag" — the setter overwrites). The setter
    accepts CLOSED positions too: the closing order's terminal `fill_divergence`
    can postdate the `position_close` booking.
10. `rehydrate` fold conventions (§K: pure fold by ascending per-stream `seq`;
    `seq` is PER-STREAM so no cross-stream comparison is made — M5C-T1):
    - `position_open` = immutable facts (the journaled qty/costs already BAKE IN
      the fills passed to `open_position`).
    - `broker_fill` rows fold iff `position_id` is non-null AND `side == "buy"`:
      a row whose `cum_filled_qty` is <= the position's cum watermark (seeded
      with `position_open.qty` — the FD-M5-18 telescoped total of the baked
      fills) is SKIPPED as baked-in or a duplicated polling re-read; beyond the
      watermark the delta accumulates. SELL fills never fold — their position
      effect IS the journaled `position_close` slice/residual (folding both
      would double-count); a buy fill for an unknown position_id is
      corruption-grade => `ExecError`.
    - `position_close` folds by SUBTRACTING the journaled slice and VERIFYING
      the journaled residual equals the fold's own subtraction (mismatch =>
      `ExecError`, fail-closed — this also trips loudly on journal shapes M5's
      serial pipeline cannot produce, e.g. a buy fill after a close); `qty == 0`
      => status "closed" + terminal-skip (later close rows for the id are
      skipped).
    - mark/pnl_snapshot rows do NOT fold; non-broker_fill fills-stream rows
      (modeled_execution_fill / fill_divergence / divergence_alert) are labels
      and are ignored. Out-of-vocab positions-stream event types => `ExecError`.
    Rehydrated == live byte-exact: every fold-bearing money field is journaled
    EXACT and re-parsed from its canonical string (LD-R5).
11. `mark`/`pnl_snapshot`/`apply_fill`/`close_position` on an unknown or CLOSED
    position raise `ExecError` (orchestration bugs, fail-loud); `pnl_snapshot`
    before the first accepted mark raises `ExecError` (there is no number to
    snapshot); a `mark` whose quote identity (symbol/instrument_id) mismatches
    the position raises `ExecError` (the §2.2 stage-9 identity-first posture).
    A stale/unusable quote is DATA: `mark` returns None, journals nothing, and
    the last mark stands (staleness never closes a position).
"""
from dataclasses import dataclass, replace
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Dict, Mapping, Optional, Sequence

from agent.exec_reasons import CLOSE_REASONS, DIVERGENCE_FLAGS, ExecError, require_member
from agent.fees import CENT, FEE_MODEL_VERSION, FeeAssumption, fees_for
from agent.quote_quality import QuoteSnapshot, evaluate
from agent.serializer import BrokerUSD, ModeledUSD, as_broker_usd, row_hash

__all__ = ["PaperPosition", "PaperBook"]

# Pinned context for derived arithmetic (quote_quality.py:23 / exec_ledger
# precedent — ambient-context immunity).
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)

_STATUSES = ("open", "closed")
_SIDE_LONG = "long"
_MARK_SOURCE_LONG = "best_bid"   # long marks at best_bid (conservative; §K)
_UNASSESSED = "unassessed"
_ZERO_USD = Decimal("0.00")      # the §J CENT-scale zero byte form

# The §P.2 frozen provenance key set (mark.quote) — built from the typed
# QuoteSnapshot, never from a caller dict.
_EVT_POSITION_OPEN = "position_open"
_EVT_MARK = "mark"
_EVT_PNL_SNAPSHOT = "pnl_snapshot"
_EVT_POSITION_CLOSE = "position_close"
_EVT_POSITION_ADJUST = "position_adjust"
_EVT_BROKER_FILL = "broker_fill"


# --- validation helpers (S2 posture; ExecError is a ValueError) -----------------

def _require_str(value, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExecError(f"{field}: must be a non-empty str "
                        f"(got {type(value).__name__})")
    return value


def _require_int(value, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecError(f"{field}: must be an int (bool excluded), "
                        f"got {type(value).__name__}")
    return value


def _require_decimal(value, *, field: str) -> Decimal:
    """A positive finite Decimal; bool/float/non-Decimal raise (S2)."""
    if isinstance(value, bool):
        raise ExecError(f"{field}: bool not allowed; use Decimal")
    if isinstance(value, float):
        raise ExecError(f"{field}: float not allowed; use Decimal")
    if not isinstance(value, Decimal):
        raise ExecError(f"{field}: must be a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ExecError(f"{field}: non-finite Decimal not allowed: {value}")
    if value <= 0:
        raise ExecError(f"{field}: must be > 0, got {value}")
    return value


def _as_modeled_usd(value, *, field: str) -> ModeledUSD:
    """Modeled-lineage wall — mirrors `serializer.as_broker_usd` (the exec_ledger
    `as_modeled_usd` shape; mirrored, not imported — the ledger is duck-typed,
    resolution 1): only a finite `ModeledUSD` is accepted; `BrokerUSD` (a sibling
    Decimal subclass) and plain Decimal both raise TypeError."""
    if not isinstance(value, ModeledUSD):
        raise TypeError(f"{field}: modeled-lineage field requires ModeledUSD "
                        f"(got {type(value).__name__})")
    if not value.is_finite():
        raise ValueError(f"{field}: non-finite ModeledUSD")
    return value


def _broker_usd(value, *, field: str) -> BrokerUSD:
    """Broker-lineage wall: delegate to the ONE serializer guard (S5)."""
    try:
        return as_broker_usd(value)
    except TypeError:
        raise TypeError(f"{field}: broker-lineage field requires BrokerUSD "
                        f"(got {type(value).__name__})")


def _attr(obj, name: str, *, what: str):
    try:
        return getattr(obj, name)
    except AttributeError:
        raise ExecError(f"{what}: missing attribute {name!r} "
                        f"(got {type(obj).__name__})")


def _sum_fills(fills: Sequence, *, what: str):
    """Validate a duck-typed FillDelta sequence (resolution 2) and return
    (Σ delta_qty, Σ delta_cost_usd as BrokerUSD) — EXACT integrated notional
    (FD-M5-18: the deltas already telescope; never qty×avg)."""
    if isinstance(fills, (str, bytes, Mapping)):
        raise ExecError(f"{what}: must be a sequence of FillDelta-shaped fills")
    fills = list(fills)
    if not fills:
        raise ExecError(f"{what}: at least one fill is required")
    qty = Decimal("0")
    cost = Decimal("0")
    with localcontext(_DECIMAL_CTX):
        for i, fill in enumerate(fills):
            delta_qty = _require_decimal(
                _attr(fill, "delta_qty", what=f"{what}[{i}]"),
                field=f"{what}[{i}].delta_qty")
            delta_cost = _broker_usd(
                _attr(fill, "delta_cost_usd", what=f"{what}[{i}]"),
                field=f"{what}[{i}].delta_cost_usd")
            qty += delta_qty
            cost += delta_cost
    return qty, as_broker_usd(BrokerUSD(cost))


def _modeled_cost(modeled, *, what: str) -> Optional[ModeledUSD]:
    """The duck-typed ModeledFill seam (resolution 2): None => unassessed;
    `modeled_cost_usd` must be ModeledUSD or None (TypeError otherwise — the
    white-box hostile-modeled wall)."""
    if modeled is None:
        return None
    value = _attr(modeled, "modeled_cost_usd", what=what)
    if value is None:
        return None
    return _as_modeled_usd(value, field=f"{what}.modeled_cost_usd")


def _zero_fee_assumption() -> FeeAssumption:
    """The §J zero assumption in the CENT-scale byte form (resolution 6)."""
    return FeeAssumption(model_version=FEE_MODEL_VERSION, sec_usd=_ZERO_USD,
                         taf_usd=_ZERO_USD, total_usd=_ZERO_USD)


def _slice_cost(total: Decimal, exit_qty: Decimal, qty: Decimal) -> Decimal:
    """The FROZEN EX-4 allocation: quantize(CENT, ROUND_HALF_EVEN) on the
    proportional slice; the residual is the EXACT subtraction (caller)."""
    with localcontext(_DECIMAL_CTX):
        return (total * exit_qty / qty).quantize(CENT, ROUND_HALF_EVEN)


def _quote_provenance(quote: QuoteSnapshot) -> dict:
    """The frozen §P.2 provenance sub-dict, from the TYPED snapshot only."""
    return {
        "dataset": quote.dataset,
        "schema": quote.schema,
        "ts_event_utc": quote.ts_event_utc,
        "ts_recv_utc": quote.ts_recv_utc,
        "seen_at_ms": quote.seen_at_ms,
        "reconnect_epoch": quote.reconnect_epoch,
        "vendor_seq": quote.vendor_seq,
    }


def _row_decimal(value, *, field: str, allow_none: bool = False) -> Optional[Decimal]:
    """Parse a journaled money field back from a replayed row (canonical string)
    or a live row (Decimal) — byte-preserving, demoted to plain Decimal."""
    if value is None:
        if allow_none:
            return None
        raise ExecError(f"{field}: required non-null journaled Decimal")
    if isinstance(value, bool) or isinstance(value, float):
        raise ExecError(f"{field}: bool/float not allowed in a journaled row")
    if isinstance(value, Decimal):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            raise ExecError(f"{field}: unparseable journaled Decimal {value!r}")
    raise ExecError(f"{field}: must be a Decimal string "
                    f"(got {type(value).__name__})")


def _row_nonnegative_decimal(value, *, field: str) -> Decimal:
    parsed = _row_decimal(value, field=field)
    if not parsed.is_finite():
        raise ExecError(f"{field}: non-finite Decimal not allowed: {parsed}")
    if parsed < 0:
        raise ExecError(f"{field}: must be >= 0, got {parsed}")
    return parsed


# --- §K PaperPosition ------------------------------------------------------------

@dataclass(frozen=True)
class PaperPosition:
    """One booked paper position (§K). qty shrinks on partial closes via
    `dataclasses.replace`; `broker_cost_usd` = Σ FillDelta.delta_cost_usd EXACT;
    `modeled_cost_usd` is the FD-M5-19 full-qty basis (None => unassessed);
    `fee_assumption` is stamped at open (§J, the zero open-side assumption)."""

    position_id: str
    symbol: str
    instrument_id: int
    side: str                                   # "long" only in M5
    qty: Decimal                                # open qty (shrinks on partial closes)
    broker_cost_usd: BrokerUSD                  # Σ FillDelta.delta_cost_usd EXACT
    modeled_cost_usd: Optional[ModeledUSD]      # FD-M5-19 full-qty basis; None => unassessed
    fee_assumption: FeeAssumption               # stamped at open (parent Tier 6)
    opening_order_id: str
    strategy_id: str
    opened_ts_utc: str
    status: str                                 # in {"open", "closed"}

    def __post_init__(self) -> None:
        if self.side != _SIDE_LONG:
            raise ExecError(f"PaperPosition.side: must be {_SIDE_LONG!r} in M5, "
                            f"got {self.side!r}")
        if self.status not in _STATUSES:
            raise ExecError(f"PaperPosition.status: must be in {_STATUSES}, "
                            f"got {self.status!r}")


# --- §K PaperBook ------------------------------------------------------------------

class PaperBook:
    """Position lifecycle + the S5 PnL split (§K). Journals position_open /
    mark / pnl_snapshot / position_close through the injected (duck-typed)
    ExecLedger; the orchestrator owns broker_fill journaling (resolution 3)."""

    def __init__(self, *, ledger, run_id: str, quote_staleness_ms_max: int,
                 spread_bps_max: Decimal) -> None:
        # `ledger` is duck-typed by design (resolution 1) — no type check.
        self._ledger = ledger
        self._run_id = _require_str(run_id, field="run_id")
        staleness = _require_int(quote_staleness_ms_max,
                                 field="quote_staleness_ms_max")
        if staleness <= 0:
            raise ExecError(f"quote_staleness_ms_max: must be > 0, got {staleness}")
        self._quote_staleness_ms_max = staleness
        self._spread_bps_max = _require_decimal(spread_bps_max,
                                                field="spread_bps_max")
        self._positions: Dict[str, PaperPosition] = {}
        self._last_mark_bid: Dict[str, Decimal] = {}
        self._fees_assessed: Dict[str, Decimal] = {}
        self._divergence_flags: Dict[str, str] = {}

    # -- internal accessors ---------------------------------------------------

    def _known(self, position_id: str) -> PaperPosition:
        pos = self._positions.get(_require_str(position_id, field="position_id"))
        if pos is None:
            raise ExecError(f"unknown position_id: {position_id!r}")
        return pos

    def _open(self, position_id: str) -> PaperPosition:
        pos = self._known(position_id)
        if pos.status != "open":
            raise ExecError(f"position {position_id!r} is closed (terminal)")
        return pos

    def position(self, position_id: str) -> PaperPosition:
        """Read accessor (open or closed)."""
        return self._known(position_id)

    # -- lifecycle --------------------------------------------------------------

    def open_position(self, *, decision_id: str, order_id: str, symbol: str,
                      instrument_id: int, strategy_id: str, fills: Sequence,
                      modeled, opened_ts_utc: str) -> PaperPosition:
        """Book a new long position from the opening order's emitted fill deltas
        (EXACT costs) + the FD-M5-19 modeled basis; journals `position_open`."""
        _require_str(decision_id, field="decision_id")
        _require_str(order_id, field="order_id")
        _require_str(symbol, field="symbol")
        _require_int(instrument_id, field="instrument_id")
        _require_str(strategy_id, field="strategy_id")
        _require_str(opened_ts_utc, field="opened_ts_utc")
        qty, broker_cost = _sum_fills(fills, what="fills")
        modeled_cost = _modeled_cost(modeled, what="modeled")
        # §P.3: run-independent (positions are account-level facts) — EXACT key set.
        position_id = "pos-" + row_hash({"symbol": symbol,
                                         "opening_order_id": order_id})
        if position_id in self._positions:
            raise ExecError(f"duplicate position_open for {position_id!r}")
        # §J / resolution 6: the open-side zero assumption, stamped at open.
        fee_assumption = fees_for(side="buy", qty=qty, notional=broker_cost)
        self._ledger.record_position_open(
            position_id=position_id, symbol=symbol, instrument_id=instrument_id,
            side=_SIDE_LONG, qty=qty, broker_cost_usd=broker_cost,
            modeled_cost_usd=modeled_cost, fee_assumption=fee_assumption,
            opening_order_id=order_id, strategy_id=strategy_id,
            opened_ts_utc=opened_ts_utc, decision_id=decision_id,
            order_id=order_id)
        pos = PaperPosition(
            position_id=position_id, symbol=symbol, instrument_id=instrument_id,
            side=_SIDE_LONG, qty=qty, broker_cost_usd=broker_cost,
            modeled_cost_usd=modeled_cost, fee_assumption=fee_assumption,
            opening_order_id=order_id, strategy_id=strategy_id,
            opened_ts_utc=opened_ts_utc, status="open")
        self._positions[position_id] = pos
        self._fees_assessed[position_id] = _ZERO_USD
        self._divergence_flags[position_id] = _UNASSESSED
        return pos

    def apply_fill(self, position_id: str, fill) -> PaperPosition:
        """Accumulate one further emitted FillDelta onto an OPEN position —
        EXACT delta_cost telescoping (FD-M5-18). Journals NOTHING: the
        orchestrator journals `broker_fill` rows (§M.4; resolution 3). The
        modeled side is the full-qty open basis and is not touched
        (resolution 8)."""
        pos = self._open(position_id)
        delta_qty = _require_decimal(_attr(fill, "delta_qty", what="fill"),
                                     field="fill.delta_qty")
        delta_cost = _broker_usd(_attr(fill, "delta_cost_usd", what="fill"),
                                 field="fill.delta_cost_usd")
        with localcontext(_DECIMAL_CTX):
            new_cost = pos.broker_cost_usd + delta_cost
            new_qty = pos.qty + delta_qty
        updated = replace(pos, qty=new_qty,
                          broker_cost_usd=as_broker_usd(BrokerUSD(new_cost)))
        self._positions[position_id] = updated
        return updated

    def apply_position_adjust(self, *, position_id: str, adjusted_qty: Decimal,
                              adjusted_broker_cost_usd: BrokerUSD,
                              adjust_id: str,
                              reconcile_id: str) -> PaperPosition:
        """Apply an M6 broker-truth correction after the ledger write succeeds.
        Only qty and broker_cost_usd move; modeled lineage remains byte-identical
        (FD-M6-10)."""
        pos = self._open(position_id)
        row = self._ledger.record_position_adjust(
            adjust_id=adjust_id, reconcile_id=reconcile_id,
            position_id=position_id, symbol=pos.symbol, prev_qty=pos.qty,
            adjusted_qty=adjusted_qty,
            prev_broker_cost_usd=pos.broker_cost_usd,
            adjusted_broker_cost_usd=adjusted_broker_cost_usd)
        new_qty = _row_nonnegative_decimal(row["adjusted_qty"],
                                           field="adjusted_qty")
        adjusted_cost = _row_decimal(row["adjusted_broker_cost_usd"],
                                     field="adjusted_broker_cost_usd")
        updated = replace(
            pos, qty=new_qty,
            broker_cost_usd=as_broker_usd(BrokerUSD(adjusted_cost)),
            status=("closed" if new_qty == 0 else "open"))
        self._positions[position_id] = updated
        return updated

    # -- marking + PnL ---------------------------------------------------------

    def mark(self, position_id: str, quote, *, now_ms: int,
             bar_key: str) -> Optional[Mapping]:
        """STRUCTURAL price-object gate (parent §4.2): only a typed QuoteSnapshot
        is accepted — a bare number/dict raises. Usability is re-checked via
        `quote_quality.evaluate` with the ctor-injected committed budgets
        (resolution 4); a stale/unusable quote produces NO new mark and journals
        nothing — the last mark stands (staleness never closes a position).
        Long marks at best_bid (conservative); journals the mark row."""
        pos = self._open(position_id)
        if not isinstance(quote, QuoteSnapshot):
            raise ExecError(f"mark: quote must be a typed QuoteSnapshot, got "
                            f"{type(quote).__name__}")
        if quote.symbol != pos.symbol or quote.instrument_id != pos.instrument_id:
            raise ExecError(
                f"mark identity mismatch: quote {quote.symbol!r}/"
                f"{quote.instrument_id} vs position {pos.symbol!r}/"
                f"{pos.instrument_id}")
        _require_int(now_ms, field="now_ms")
        _require_str(bar_key, field="bar_key")
        verdict = evaluate(quote, now_ms=now_ms,
                           spread_bps_max=self._spread_bps_max,
                           staleness_ms_max=self._quote_staleness_ms_max)
        if verdict.ok is not True:
            return None   # stale/unusable: no new mark, last mark stands
        bid = quote.bid   # present, positive, finite — verdict.ok guarantees it
        with localcontext(_DECIMAL_CTX):
            unrealized_broker = pos.qty * bid - pos.broker_cost_usd
            unrealized_modeled = (pos.qty * bid - pos.modeled_cost_usd
                                  if pos.modeled_cost_usd is not None else None)
        row = self._ledger.record_mark(
            position_id=position_id, mark_price=bid,
            mark_source=_MARK_SOURCE_LONG, quote=_quote_provenance(quote),
            unrealized_broker_usd=Decimal(str(unrealized_broker)),
            unrealized_modeled_usd=(None if unrealized_modeled is None
                                    else Decimal(str(unrealized_modeled))),
            bar_key=bar_key)
        self._last_mark_bid[position_id] = bid
        return row

    def pnl_snapshot(self, position_id: str, *, bar_key: str) -> Mapping:
        """The S5 two-class snapshot over the last accepted mark:
        broker_account_pnl = BrokerUSD(qty×mark_bid − broker_cost) — fee-free
        (A6); execution_realistic_pnl = ModeledUSD(qty×mark_bid − modeled_cost
        − fees_assessed_to_date) or None (unassessed). Carries the position's
        latest divergence_flag (EX-11; default "unassessed") + the verbatim
        used_for_strategy_evaluation (emitted by the ledger). Journals the row."""
        pos = self._open(position_id)
        _require_str(bar_key, field="bar_key")
        bid = self._last_mark_bid.get(position_id)
        if bid is None:
            raise ExecError(f"pnl_snapshot: no accepted mark yet for "
                            f"{position_id!r}")
        with localcontext(_DECIMAL_CTX):
            broker_pnl = pos.qty * bid - pos.broker_cost_usd
            realistic = (pos.qty * bid - pos.modeled_cost_usd
                         - self._fees_assessed[position_id]
                         if pos.modeled_cost_usd is not None else None)
        return self._ledger.record_pnl_snapshot(
            position_id=position_id,
            broker_account_pnl=as_broker_usd(BrokerUSD(broker_pnl)),
            execution_realistic_pnl=(None if realistic is None
                                     else ModeledUSD(realistic)),
            divergence_flag=self._divergence_flags.get(position_id, _UNASSESSED),
            bar_key=bar_key)

    def set_divergence_flag(self, position_id: str, flag: str) -> None:
        """FD-M5-20: the orchestrator sets the position's LATEST flag at
        order-terminal (from the fill_divergence row). Closed positions accept
        the setter too (resolution 9)."""
        require_member(DIVERGENCE_FLAGS, flag, what="divergence flag")
        self._known(position_id)
        self._divergence_flags[position_id] = flag

    # -- close ------------------------------------------------------------------

    def close_position(self, *, position_id: str, order_id: str,
                       fills: Sequence, modeled, reason: str,
                       decision_id: str) -> Mapping:
        """Close (partially or fully) per the FROZEN EX-4 allocation; journals
        `position_close` with the slice AND residual of BOTH lineages (LD-R5).
        `decision_id` is a documented addition to the §K sketch (resolution 5)."""
        require_member(CLOSE_REASONS, reason, what="close reason")
        _require_str(order_id, field="order_id")
        _require_str(decision_id, field="decision_id")
        pos = self._open(position_id)
        exit_qty, exit_notional = _sum_fills(fills, what="fills")
        if exit_qty > pos.qty:
            raise ExecError(f"close_position: exit_qty {exit_qty} exceeds held "
                            f"qty {pos.qty} (may flatten, never flip)")
        modeled_exit = _modeled_cost(modeled, what="modeled")

        # EX-4 — broker lineage.
        slice_broker = _slice_cost(pos.broker_cost_usd, exit_qty, pos.qty)
        with localcontext(_DECIMAL_CTX):
            residual_broker = pos.broker_cost_usd - slice_broker
            realized_broker = exit_notional - slice_broker
        # EX-4 — IDENTICAL rule for the modeled lineage (the position's open
        # basis; independent of the closing fill's modeled exit).
        if pos.modeled_cost_usd is not None:
            slice_modeled = _slice_cost(pos.modeled_cost_usd, exit_qty, pos.qty)
            with localcontext(_DECIMAL_CTX):
                residual_modeled = pos.modeled_cost_usd - slice_modeled
        else:
            slice_modeled = None
            residual_modeled = None

        # EX-9: the sell-side fee notional is THE MODELED exit proceeds; a None
        # modeled exit computes NO fee (the zero block is journaled, res. 6)
        # and leaves the realistic side unassessed.
        if modeled_exit is not None:
            fees_assessed = fees_for(side="sell", qty=exit_qty,
                                     notional=modeled_exit)
        else:
            fees_assessed = _zero_fee_assumption()
        if modeled_exit is not None and slice_modeled is not None:
            with localcontext(_DECIMAL_CTX):
                realized_modeled = ModeledUSD(
                    modeled_exit - slice_modeled - fees_assessed.total_usd)
        else:
            realized_modeled = None

        row = self._ledger.record_position_close(
            position_id=position_id, closing_order_id=order_id,
            exit_qty=exit_qty,
            broker_exit_notional_usd=as_broker_usd(BrokerUSD(exit_notional)),
            closed_slice_broker_cost_usd=slice_broker,
            residual_broker_cost_usd=residual_broker,
            closed_slice_modeled_cost_usd=slice_modeled,
            residual_modeled_cost_usd=residual_modeled,
            realized_broker_pnl=as_broker_usd(BrokerUSD(realized_broker)),
            realized_modeled_pnl=realized_modeled,
            fees_assessed=fees_assessed, reason=reason,
            decision_id=decision_id, order_id=order_id)

        # Commit (only after the journal write succeeded — a raise above leaves
        # the book untouched, resolution 11).
        with localcontext(_DECIMAL_CTX):
            new_qty = pos.qty - exit_qty
        updated = replace(
            pos, qty=new_qty,
            broker_cost_usd=as_broker_usd(BrokerUSD(residual_broker)),
            modeled_cost_usd=(None if residual_modeled is None
                              else ModeledUSD(residual_modeled)),
            status=("closed" if new_qty == 0 else "open"))
        self._positions[position_id] = updated
        with localcontext(_DECIMAL_CTX):
            self._fees_assessed[position_id] = (
                self._fees_assessed[position_id] + fees_assessed.total_usd)
        return row

    # -- rehydrate (pure fold; §K) -----------------------------------------------

    @staticmethod
    def rehydrate(position_rows, fill_rows) -> Dict[str, PaperPosition]:
        """PURE fold by ascending per-stream `seq` (resolution 10): position_open
        = immutable facts; post-open buy `broker_fill` deltas accumulate by
        position_id (cum-watermark-gated against the baked-in opening fills and
        duplicate polling re-reads); position_close applies the JOURNALED
        slice/residual (verified against the fold's own subtraction) +
        terminal-skip; mark/pnl rows do NOT fold. Byte-exact vs live (LD-R5).
        Directly pluggable as `rehydrate_exec_state(book_rehydrate=...)`."""
        positions: Dict[str, PaperPosition] = {}
        watermark: Dict[str, Decimal] = {}
        post_fill_rows = []

        for row in sorted(position_rows, key=lambda r: r["seq"]):
            event_type = row.get("event_type")
            if event_type == _EVT_POSITION_OPEN:
                pid = row["position_id"]
                if pid in positions:
                    raise ExecError(f"duplicate position_open for {pid!r}")
                if row["side"] != _SIDE_LONG:
                    raise ExecError(f"position_open.side: must be "
                                    f"{_SIDE_LONG!r}, got {row['side']!r}")
                fee = row["fee_assumption"]
                modeled = _row_decimal(row["modeled_cost_usd"],
                                       field="modeled_cost_usd", allow_none=True)
                positions[pid] = PaperPosition(
                    position_id=pid,
                    symbol=row["symbol"],
                    instrument_id=_require_int(row["instrument_id"],
                                               field="instrument_id"),
                    side=_SIDE_LONG,
                    qty=_row_decimal(row["qty"], field="qty"),
                    broker_cost_usd=as_broker_usd(BrokerUSD(_row_decimal(
                        row["broker_cost_usd"], field="broker_cost_usd"))),
                    modeled_cost_usd=(None if modeled is None
                                      else ModeledUSD(modeled)),
                    fee_assumption=FeeAssumption(
                        model_version=fee["model_version"],
                        sec_usd=_row_decimal(fee["sec_usd"], field="fee.sec_usd"),
                        taf_usd=_row_decimal(fee["taf_usd"], field="fee.taf_usd"),
                        total_usd=_row_decimal(fee["total_usd"],
                                               field="fee.total_usd")),
                    opening_order_id=row["opening_order_id"],
                    strategy_id=row["strategy_id"],
                    opened_ts_utc=row["opened_ts_utc"],
                    status="open")
                # FD-M5-18: the baked fills telescope to position_open.qty.
                watermark[pid] = positions[pid].qty
            elif event_type in (_EVT_MARK, _EVT_PNL_SNAPSHOT):
                continue   # derived state — recomputed live, never folded
            elif event_type in (_EVT_POSITION_CLOSE, _EVT_POSITION_ADJUST):
                post_fill_rows.append(row)
            else:
                raise ExecError(f"out-of-vocab positions-stream event_type: "
                                f"{event_type!r}")

        # Buy fill deltas (the apply_fill increments). M5's serial one-in-flight
        # pipeline (FD-M5-21) guarantees every buy fill precedes every close.
        for row in sorted(fill_rows, key=lambda r: r["seq"]):
            if row.get("event_type") != _EVT_BROKER_FILL:
                continue   # modeled/divergence labels never fold
            pid = row.get("position_id")
            if pid is None:
                continue   # pre-position fill: baked into position_open
            side = row.get("side")
            if side == "sell":
                continue   # exit proceeds: the position effect IS position_close
            if side != "buy":
                raise ExecError(f"broker_fill.side: out-of-vocab {side!r}")
            pos = positions.get(pid)
            if pos is None:
                raise ExecError(f"broker_fill references unknown position "
                                f"{pid!r} (corruption-grade)")
            cum = _row_decimal(row["cum_filled_qty"], field="cum_filled_qty")
            if cum <= watermark[pid]:
                continue   # baked into position_open, or a duplicated re-read
            watermark[pid] = cum
            delta_qty = _row_decimal(row["delta_qty"], field="delta_qty")
            delta_cost = _row_decimal(row["delta_cost_usd"],
                                      field="delta_cost_usd")
            with localcontext(_DECIMAL_CTX):
                new_cost = pos.broker_cost_usd + delta_cost
                new_qty = pos.qty + delta_qty
            positions[pid] = replace(
                pos, qty=new_qty,
                broker_cost_usd=as_broker_usd(BrokerUSD(new_cost)))

        # Closes/adjusts: both are stream-sequenced post-fill position effects.
        for row in sorted(post_fill_rows, key=lambda r: r["seq"]):
            pid = row["position_id"]
            pos = positions.get(pid)
            if pos is None:
                raise ExecError(f"{row.get('event_type')} references unknown "
                                f"position {pid!r}")
            event_type = row.get("event_type")
            if event_type == _EVT_POSITION_CLOSE:
                if pos.status == "closed":
                    continue   # terminal-skip
                exit_qty = _row_decimal(row["exit_qty"], field="exit_qty")
                with localcontext(_DECIMAL_CTX):
                    new_qty = pos.qty - exit_qty
                if new_qty < 0:
                    raise ExecError(f"position_close: exit_qty {exit_qty} "
                                    f"exceeds folded qty {pos.qty} for {pid!r}")
                slice_broker = _row_decimal(
                    row["closed_slice_broker_cost_usd"],
                    field="closed_slice_broker_cost_usd")
                residual_broker = _row_decimal(
                    row["residual_broker_cost_usd"],
                    field="residual_broker_cost_usd")
                with localcontext(_DECIMAL_CTX):
                    folded_broker = pos.broker_cost_usd - slice_broker
                if folded_broker != residual_broker:
                    raise ExecError(
                        f"position_close residual drift for {pid!r}: folded "
                        f"{folded_broker} != journaled {residual_broker} "
                        f"(fail-closed)")
                slice_modeled = _row_decimal(
                    row["closed_slice_modeled_cost_usd"],
                    field="closed_slice_modeled_cost_usd", allow_none=True)
                residual_modeled = _row_decimal(
                    row["residual_modeled_cost_usd"],
                    field="residual_modeled_cost_usd", allow_none=True)
                if (pos.modeled_cost_usd is None) != (slice_modeled is None) or \
                        (slice_modeled is None) != (residual_modeled is None):
                    raise ExecError(f"position_close modeled-lineage nullity "
                                    f"mismatch for {pid!r}")
                if slice_modeled is not None:
                    with localcontext(_DECIMAL_CTX):
                        folded_modeled = pos.modeled_cost_usd - slice_modeled
                    if folded_modeled != residual_modeled:
                        raise ExecError(
                            f"position_close modeled residual drift for "
                            f"{pid!r}: folded {folded_modeled} != journaled "
                            f"{residual_modeled}")
                positions[pid] = replace(
                    pos, qty=new_qty,
                    broker_cost_usd=as_broker_usd(BrokerUSD(residual_broker)),
                    modeled_cost_usd=(None if residual_modeled is None
                                      else ModeledUSD(residual_modeled)),
                    status=("closed" if new_qty == 0 else "open"))
            elif event_type == _EVT_POSITION_ADJUST:
                if pos.status == "closed":
                    raise ExecError(f"position_adjust targets closed position "
                                    f"{pid!r}")
                if row["symbol"] != pos.symbol:
                    raise ExecError(f"position_adjust symbol mismatch for "
                                    f"{pid!r}: {row['symbol']!r} != "
                                    f"{pos.symbol!r}")
                prev_qty = _row_nonnegative_decimal(row["prev_qty"],
                                                    field="prev_qty")
                adjusted_qty = _row_nonnegative_decimal(row["adjusted_qty"],
                                                        field="adjusted_qty")
                prev_cost = _row_decimal(row["prev_broker_cost_usd"],
                                         field="prev_broker_cost_usd")
                adjusted_cost = _row_decimal(
                    row["adjusted_broker_cost_usd"],
                    field="adjusted_broker_cost_usd")
                if prev_qty != pos.qty:
                    raise ExecError(
                        f"position_adjust qty drift for {pid!r}: folded "
                        f"{pos.qty} != journaled prev {prev_qty}")
                if prev_cost != pos.broker_cost_usd:
                    raise ExecError(
                        f"position_adjust cost drift for {pid!r}: folded "
                        f"{pos.broker_cost_usd} != journaled prev "
                        f"{prev_cost}")
                positions[pid] = replace(
                    pos, qty=adjusted_qty,
                    broker_cost_usd=as_broker_usd(BrokerUSD(adjusted_cost)),
                    status=("closed" if adjusted_qty == 0 else "open"))
        return positions
