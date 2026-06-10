"""M5 §I — modeled fill (PURE; honest L1 scope) + FD-M5-20 divergence.

Honesty pins (FD-M5-6): the recorded primary feed is EQUS.MINI L1 (`tbbo`) — what
M5 can model EVERYWHERE is top-of-book v1 (`tob_l1_v1`). Depth-VWAP v2
(`depth_vwap_l2_v2`) is fully built and tested against the committed
`mbp10_depth_sample.jsonl` fixture, but the `DepthView` seam stays `None` in
observe/paper modes until L2 recording is provisioned. Absence of depth
**degrades, never upgrades**; the two model classes are never pooled in any
report. Conservative displayed-liquidity fillability, NOT queue position (L3/MBO
is the documented out-of-scope upgrade). No probabilistic queue claims anywhere.

This module prices and labels; it never submits (§3 import discipline). All
modeled money is `ModeledUSD`; `as_broker_usd` makes writing it into a ledger
field a `TypeError` (S5, serializer.py:58-64). `assess_divergence` is the ONE
sanctioned cross-newtype comparison seam — its outputs are plain `Decimal`.

Documented resolutions (faithful to the contract; none contradict a frozen pin):

1. `modeled_fill_id` binding (§P.3 vs §I): the §P.3 id derivation
   `"mf-" + row_hash({order_id, model, quote_b_seen_at_ms, vendor_seq|null})`
   needs `order_id`, but `model_fill`'s FROZEN signature (§I) carries no
   `order_id` — the modeled fill is computed from quote B at submit time, before
   it is joined to an order row. Resolution: `model_fill` constructs the
   `ModeledFill` with the UNBOUND sentinel `modeled_fill_id = ""`
   (`UNBOUND_MODELED_FILL_ID`), and the CALLER binds the §P.3 id via
   `bind_modeled_fill_id(fill, order_id=...)` (exact key set, serializer
   `row_hash`). Re-binding an already-bound fill raises `ExecError`.
2. `assess_divergence`'s cap operand (FD-M5-20): the re-integration remainder is
   priced at `capped_limit`, which `ModeledFill` does not carry as its own field.
   Per §I, `worst_price` IS "the cap for remainder": the remainder term is
   nonzero only when `filled_qty > modeled_fillable_qty`, which (given the
   guarded invariant `filled_qty <= requested_qty`) implies the modeled fill
   itself had a remainder — exactly the case where §I pins
   `worst_price == capped_limit`. So `worst_price` is the cap operand, totally.
   `filled_qty > requested_qty` raises `ExecError` (a broker cannot fill more
   than was requested; malformed collaborator input).
3. `DEPTH_FRESHNESS_TTL_MS` / `DIVERGENCE_ALERT_BPS` are imported from their §B
   one-home (`agent.execution_config`) rather than duplicated here. The §3
   bracket row for this module omits that dependency, but §B pins the constants'
   home and the one-vocabulary/one-home discipline forbids a second copy;
   `agent.execution_config` is itself in the pure family ([stdlib,
   agent.serializer]) and the import violates none of §3's forbidden
   modules/tokens.
4. `DepthSnapshot.schema` is validated AT CONSTRUCTION (`__post_init__` raises
   `ExecError` unless `schema == "mbp-10"`), so a wrong-schema snapshot is
   unrepresentable; depth IDENTITY (symbol/instrument_id vs quote B) is checked
   at use inside `model_fill` (the quote is not known at construction).
5. `worst_price` is quantized `MID_QUANTUM` (§P.2's quantize-only provenance
   row) — exact for tick-grid caps and 4dp touches, so resolution 2 loses
   nothing. `touch_price` stays UNQUANTIZED (the EX-2 re-integration operand);
   `modeled_cost_usd` and `levels_consumed` stay EXACT.
6. An unusable quote B (`quote_b_verdict.ok is not True`) => `modeled_unfillable`
   with EMPTY `reasons` (FD-M5-19 "no usable quote"): the ModeledFill reason
   vocabulary is closed over {depth_stale, depth_epoch_mismatch,
   no_liquidity_at_cap} and the quote-quality reasons already ride the verdict /
   the preflight reject row — they are never re-keyed here.
7. A depth-model unfillable result carries `levels_consumed = None` (nothing was
   consumed; "all money fields None" extends to the walk prefix).
8. Degrade reasons are collected (both `depth_stale` AND `depth_epoch_mismatch`
   may be recorded on one fill) — the M2/M4 collect-all discipline.
9. `ModeledFill.quote` is wrapped in `MappingProxyType` (read-only view) so the
   frozen dataclass's provenance cannot be mutated after construction.
"""
from dataclasses import dataclass, replace
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from types import MappingProxyType
from typing import Mapping, Optional, Protocol, Tuple, runtime_checkable

from agent.exec_reasons import (
    DIVERGENCE_FLAGS,
    ExecError,
    MODELED_FILL_MODELS,
    REALISM_CLASSES,
    require_member,
)
from agent.execution_config import DEPTH_FRESHNESS_TTL_MS, DIVERGENCE_ALERT_BPS
from agent.quote_quality import BPS_QUANTUM, MID_QUANTUM, QuoteSnapshot, QuoteVerdict
from agent.serializer import BrokerUSD, ModeledUSD, as_broker_usd, row_hash

# Pinned context for derived values (quote_quality.py:23 precedent; §S "VWAP (§I)").
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)

DEPTH_SCHEMA = "mbp-10"            # the ONLY accepted DepthSnapshot schema (§I)
MAX_DEPTH_LEVELS = 10              # mbp-10 ladder bound (recorder book_state mirror)
UNBOUND_MODELED_FILL_ID = ""       # sentinel until bind_modeled_fill_id (resolution 1)

# Closed ModeledFill reason vocabulary (§I; out-of-vocab => ExecError).
MODELED_FILL_REASONS = frozenset(
    {"depth_stale", "depth_epoch_mismatch", "no_liquidity_at_cap"})

# Frozen §P.2 provenance key set (+ book_hash, the §I depth provenance).
QUOTE_PROVENANCE_KEYS = (
    "dataset", "schema", "ts_event_utc", "ts_recv_utc",
    "seen_at_ms", "reconnect_epoch", "vendor_seq", "book_hash")

_SIDES = ("buy", "sell")
_TEN_THOUSAND = Decimal("10000")
_ZERO = Decimal("0")
_QUOTE_FIELDS = ("bid", "ask", "bid_sz", "ask_sz")
_TOB_MODEL = "tob_l1_v1"
_DEPTH_MODEL = "depth_vwap_l2_v2"


# --- validation helpers (order_pricing.py S2 posture: ExecError, a ValueError) ---

def _reject_float(value, *, field: str) -> None:
    if isinstance(value, float):
        raise ExecError(f"{field}: float not allowed; use Decimal (S2)")


def _require_side(side: str) -> None:
    if side not in _SIDES:
        raise ExecError(f"side must be one of {_SIDES}, got {side!r}")


def _require_int(value, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecError(
            f"{field}: must be an int (bool excluded), got {type(value).__name__}")
    return value


def _require_str(value, *, field: str) -> str:
    if not isinstance(value, str):
        raise ExecError(f"{field}: must be a str, got {type(value).__name__}")
    return value


def _require_decimal(value, *, field: str, positive: bool) -> Decimal:
    if isinstance(value, bool):
        raise ExecError(f"{field}: bool not allowed; use Decimal (S2)")
    _reject_float(value, field=field)
    if not isinstance(value, Decimal):
        raise ExecError(f"{field}: must be a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ExecError(f"{field}: non-finite Decimal not allowed: {value}")
    if positive and value <= 0:
        raise ExecError(f"{field}: must be > 0, got {value}")
    elif not positive and value < 0:
        raise ExecError(f"{field}: must be >= 0, got {value}")
    return value


def _require_modeled_usd_or_none(value, *, field: str):
    """Modeled money slots accept ONLY ModeledUSD (BrokerUSD/plain Decimal/float
    all raise) — the S5 lineage wall, structural at construction."""
    if value is None:
        return None
    if not isinstance(value, ModeledUSD):
        raise ExecError(
            f"{field}: must be ModeledUSD or None, got {type(value).__name__}")
    if not value.is_finite():
        raise ExecError(f"{field}: non-finite ModeledUSD not allowed")
    return value


def _usable(value: Optional[Decimal]) -> bool:
    """A touch/size is usable iff present, Decimal, finite, > 0."""
    return isinstance(value, Decimal) and value.is_finite() and value > 0


# --- §I typed depth objects -------------------------------------------------

@dataclass(frozen=True)
class DepthSnapshot:
    """One L2 ladder observation, shaped from `EquityBookState.snapshot()`
    ((px, sz) Decimal pairs, best-first: bids px DESC, asks px ASC; vendor
    duplicate-price splits and zero-size padding levels are preserved by the
    recorder and tolerated here — the walk skips zero-size levels).

    schema MUST be "mbp-10" — enforced at CONSTRUCTION (resolution 4)."""

    symbol: str
    instrument_id: int
    bids: Tuple[Tuple[Decimal, Decimal], ...]   # (px, sz) best-first
    asks: Tuple[Tuple[Decimal, Decimal], ...]
    book_hash: str                               # recorder book_hash — provenance gate
    seen_at_ms: int
    reconnect_epoch: int
    dataset: str
    schema: str                                  # MUST be "mbp-10" (else ExecError)

    def __post_init__(self) -> None:
        _require_str(self.symbol, field="DepthSnapshot.symbol")
        if not self.symbol:
            raise ExecError("DepthSnapshot.symbol: must be non-empty")
        _require_int(self.instrument_id, field="DepthSnapshot.instrument_id")
        _require_str(self.book_hash, field="DepthSnapshot.book_hash")
        _require_int(self.seen_at_ms, field="DepthSnapshot.seen_at_ms")
        _require_int(self.reconnect_epoch, field="DepthSnapshot.reconnect_epoch")
        _require_str(self.dataset, field="DepthSnapshot.dataset")
        _require_str(self.schema, field="DepthSnapshot.schema")
        if self.schema != DEPTH_SCHEMA:
            raise ExecError(
                f"DepthSnapshot.schema: must be {DEPTH_SCHEMA!r}, got {self.schema!r}")
        self._validate_side(self.bids, side="bids", descending=True)
        self._validate_side(self.asks, side="asks", descending=False)

    @staticmethod
    def _validate_side(levels, *, side: str, descending: bool) -> None:
        if not isinstance(levels, tuple):
            raise ExecError(f"DepthSnapshot.{side}: must be a tuple of (px, sz) pairs")
        if len(levels) > MAX_DEPTH_LEVELS:
            raise ExecError(
                f"DepthSnapshot.{side}: exceeds mbp-10 max {MAX_DEPTH_LEVELS} "
                f"levels ({len(levels)})")
        prev_px: Optional[Decimal] = None
        for i, level in enumerate(levels):
            if not isinstance(level, tuple) or len(level) != 2:
                raise ExecError(
                    f"DepthSnapshot.{side}[{i}]: must be a (px, sz) 2-tuple")
            px, sz = level
            _require_decimal(px, field=f"DepthSnapshot.{side}[{i}].px", positive=True)
            _require_decimal(sz, field=f"DepthSnapshot.{side}[{i}].sz", positive=False)
            if prev_px is not None:
                ordered = (px <= prev_px) if descending else (px >= prev_px)
                if not ordered:
                    raise ExecError(
                        f"DepthSnapshot.{side}: not best-first at index {i} "
                        f"({prev_px} -> {px})")
            prev_px = px


@runtime_checkable
class DepthView(Protocol):
    """The depth seam — stays None in observe/paper until L2 recording is
    provisioned (FD-M5-6)."""

    def latest_book(self, symbol: str, instrument_id: int) -> Optional[DepthSnapshot]:
        ...


# --- §I ModeledFill ----------------------------------------------------------

@dataclass(frozen=True)
class ModeledFill:
    """The execution-realism label (NEVER the ledger; S5). Frozen field list (rev 2)."""

    modeled_fill_id: str                         # "" until bind_modeled_fill_id (res. 1)
    model: str                                   # ∈ MODELED_FILL_MODELS
    realism_class: str                           # ∈ REALISM_CLASSES (FD-M5-19)
    requested_qty: Decimal
    modeled_fillable_qty: Decimal                # displayed-size-bounded qty at/inside the touch
    modeled_vwap: Optional[ModeledUSD]           # per-share, quantized MID_QUANTUM (provenance)
    worst_price: Optional[ModeledUSD]            # deepest level consumed, or the cap for remainder
    touch_price: Optional[ModeledUSD]            # UNQUANTIZED opposite-touch price used by the
                                                 #   model (buy: quote_b.ask) — journaled so the
                                                 #   FD-M5-20 re-integration is replay-recomputable
    levels_consumed: Optional[Tuple[Tuple[Decimal, Decimal], ...]]
                                                 # depth_vwap_l2_v2 ONLY: the (px, qty-taken)
                                                 #   prefix the walk consumed (EXACT, best-first);
                                                 #   None for tob_l1_v1
    slippage_vs_mid_bps: Optional[Decimal]       # frozen EX-8 formula; BPS_QUANTUM;
                                                 #   None iff modeled_vwap or mid is None
    modeled_cost_usd: Optional[ModeledUSD]       # FULL-qty integrated cost (FD-M5-19), EXACT
    quote: Mapping                               # frozen provenance keys (§P.2) + book_hash|null
    reasons: Tuple[str, ...]                     # sorted ⊆ MODELED_FILL_REASONS

    def __post_init__(self) -> None:
        _require_str(self.modeled_fill_id, field="ModeledFill.modeled_fill_id")
        require_member(MODELED_FILL_MODELS, self.model, what="modeled fill model")
        require_member(REALISM_CLASSES, self.realism_class, what="realism class")
        _require_decimal(self.requested_qty, field="ModeledFill.requested_qty",
                         positive=True)
        _require_decimal(self.modeled_fillable_qty,
                         field="ModeledFill.modeled_fillable_qty", positive=False)
        _require_modeled_usd_or_none(self.modeled_vwap, field="ModeledFill.modeled_vwap")
        _require_modeled_usd_or_none(self.worst_price, field="ModeledFill.worst_price")
        _require_modeled_usd_or_none(self.touch_price, field="ModeledFill.touch_price")
        _require_modeled_usd_or_none(self.modeled_cost_usd,
                                     field="ModeledFill.modeled_cost_usd")
        if self.slippage_vs_mid_bps is not None:
            # Any sign is legal (adverse-positive convention, but not enforced
            # here); float/bool/non-Decimal/non-finite raise.
            value = self.slippage_vs_mid_bps
            if isinstance(value, bool):
                raise ExecError("ModeledFill.slippage_vs_mid_bps: bool not allowed")
            _reject_float(value, field="ModeledFill.slippage_vs_mid_bps")
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ExecError(
                    "ModeledFill.slippage_vs_mid_bps: must be a finite Decimal or None")

        # reasons: sorted tuple, closed vocabulary, no duplicates.
        if not isinstance(self.reasons, tuple):
            raise ExecError("ModeledFill.reasons: must be a tuple")
        for reason in self.reasons:
            if reason not in MODELED_FILL_REASONS:
                raise ExecError(f"out-of-vocab modeled-fill reason: {reason!r}")
        if list(self.reasons) != sorted(set(self.reasons)):
            raise ExecError("ModeledFill.reasons: must be sorted and deduped")

        # levels_consumed discipline (§I): tob => None; depth => non-empty tuple
        # unless unfillable (resolution 7).
        if self.model == _TOB_MODEL:
            if self.levels_consumed is not None:
                raise ExecError("ModeledFill.levels_consumed: must be None for tob_l1_v1")
        else:
            if self.realism_class == "modeled_unfillable":
                if self.levels_consumed is not None:
                    raise ExecError(
                        "ModeledFill.levels_consumed: must be None when unfillable")
            else:
                if (not isinstance(self.levels_consumed, tuple)
                        or not self.levels_consumed):
                    raise ExecError(
                        "ModeledFill.levels_consumed: depth model requires the "
                        "non-empty consumed prefix")
                for i, level in enumerate(self.levels_consumed):
                    if not isinstance(level, tuple) or len(level) != 2:
                        raise ExecError(
                            f"ModeledFill.levels_consumed[{i}]: must be a (px, qty) "
                            "2-tuple")
                    px, taken = level
                    _require_decimal(px, field=f"levels_consumed[{i}].px", positive=True)
                    _require_decimal(taken, field=f"levels_consumed[{i}].qty",
                                     positive=True)

        # unfillable consistency: all money fields None, fillable 0; fillable
        # fills carry the full money set (fail-closed against hand-built drift).
        money = (self.modeled_vwap, self.worst_price, self.touch_price,
                 self.modeled_cost_usd, self.slippage_vs_mid_bps)
        if self.realism_class == "modeled_unfillable":
            if any(v is not None for v in money):
                raise ExecError(
                    "ModeledFill: modeled_unfillable requires ALL money fields None")
            if self.modeled_fillable_qty != 0:
                raise ExecError(
                    "ModeledFill: modeled_unfillable requires modeled_fillable_qty == 0")
        else:
            if any(v is None for v in (self.modeled_vwap, self.worst_price,
                                       self.touch_price, self.modeled_cost_usd)):
                raise ExecError(
                    "ModeledFill: a fillable class requires vwap/worst/touch/cost present")

        # quote provenance: the EXACT §P.2 key set (+ book_hash), read-only view.
        if not isinstance(self.quote, Mapping):
            raise ExecError("ModeledFill.quote: must be a Mapping")
        keys = set(self.quote.keys())
        expected = set(QUOTE_PROVENANCE_KEYS)
        if keys != expected:
            raise ExecError(
                f"ModeledFill.quote: key set must be exactly {sorted(expected)}, "
                f"got {sorted(keys)}")
        _require_int(self.quote["seen_at_ms"], field="quote.seen_at_ms")
        _require_int(self.quote["reconnect_epoch"], field="quote.reconnect_epoch")
        if self.quote["vendor_seq"] is not None:
            _require_int(self.quote["vendor_seq"], field="quote.vendor_seq")
        if self.quote["book_hash"] is not None:
            _require_str(self.quote["book_hash"], field="quote.book_hash")
        for key in ("dataset", "schema", "ts_event_utc", "ts_recv_utc"):
            _require_str(self.quote[key], field=f"quote.{key}")
        if not isinstance(self.quote, MappingProxyType):
            object.__setattr__(self, "quote", MappingProxyType(dict(self.quote)))


# --- model_fill (frozen algorithm, §I) ----------------------------------------

def model_fill(*, side: str, qty: Decimal, capped_limit: Decimal,
               quote_b: QuoteSnapshot, quote_b_verdict: QuoteVerdict,
               depth: Optional[DepthSnapshot], now_ms: int) -> ModeledFill:
    """Computed ONCE from quote B (the quote the preflight validated) at submit
    time — never quote A, never re-shopped after broker fills land (no hindsight).

    Structural typed-object gate (S5): only the typed QuoteSnapshot/DepthSnapshot
    are accepted; a bare number/dict raises ExecError; a float in any slot raises
    (S2). Depth identity or schema mismatch => ExecError. Stale/epoch-mismatched
    depth => degrade to tob_l1_v1 with the reason recorded — never a reject by
    itself, never an upgrade."""
    _require_side(side)
    _require_decimal(qty, field="qty", positive=True)
    _require_decimal(capped_limit, field="capped_limit", positive=True)
    if not isinstance(quote_b, QuoteSnapshot):
        raise ExecError(
            f"quote_b: must be a QuoteSnapshot, got {type(quote_b).__name__}")
    for field in _QUOTE_FIELDS:
        _reject_float(getattr(quote_b, field), field=f"quote_b.{field}")
    if not isinstance(quote_b_verdict, QuoteVerdict):
        raise ExecError(
            f"quote_b_verdict: must be a QuoteVerdict, got "
            f"{type(quote_b_verdict).__name__}")
    if depth is not None and not isinstance(depth, DepthSnapshot):
        raise ExecError(
            f"depth: must be a DepthSnapshot or None, got {type(depth).__name__}")
    _require_int(now_ms, field="now_ms")

    # Model selection (frozen): depth USED iff present AND epoch matches quote B
    # AND now_ms - seen_at_ms <= DEPTH_FRESHNESS_TTL_MS (strict '>' => stale).
    reasons = set()
    depth_used = False
    if depth is not None:
        if (depth.symbol != quote_b.symbol
                or depth.instrument_id != quote_b.instrument_id):
            raise ExecError(
                "depth identity mismatch: "
                f"depth {depth.symbol!r}/{depth.instrument_id} vs "
                f"quote_b {quote_b.symbol!r}/{quote_b.instrument_id}")
        usable = True
        if depth.reconnect_epoch != quote_b.reconnect_epoch:
            reasons.add("depth_epoch_mismatch")
            usable = False
        if now_ms - depth.seen_at_ms > DEPTH_FRESHNESS_TTL_MS:
            reasons.add("depth_stale")
            usable = False
        depth_used = usable

    model = _DEPTH_MODEL if depth_used else _TOB_MODEL
    quote_prov = {
        "dataset": quote_b.dataset,
        "schema": quote_b.schema,
        "ts_event_utc": quote_b.ts_event_utc,
        "ts_recv_utc": quote_b.ts_recv_utc,
        "seen_at_ms": quote_b.seen_at_ms,
        "reconnect_epoch": quote_b.reconnect_epoch,
        "vendor_seq": quote_b.vendor_seq,
        "book_hash": depth.book_hash if depth_used else None,
    }

    buy = side == "buy"

    def _unfillable(extra_reason: Optional[str] = None) -> ModeledFill:
        if extra_reason is not None:
            reasons.add(extra_reason)
        return ModeledFill(
            modeled_fill_id=UNBOUND_MODELED_FILL_ID,
            model=model,
            realism_class="modeled_unfillable",
            requested_qty=qty,
            modeled_fillable_qty=_ZERO,
            modeled_vwap=None,
            worst_price=None,
            touch_price=None,
            levels_consumed=None,
            slippage_vs_mid_bps=None,
            modeled_cost_usd=None,
            quote=quote_prov,
            reasons=tuple(sorted(reasons)),
        )

    # FD-M5-19: no usable quote B => modeled_unfillable (resolution 6).
    if quote_b_verdict.ok is not True:
        return _unfillable()
    touch = quote_b.ask if buy else quote_b.bid
    touch_sz = quote_b.ask_sz if buy else quote_b.bid_sz
    if not _usable(touch) or not _usable(touch_sz):
        return _unfillable()  # belt-and-braces; an ok verdict implies usable sides

    levels: Optional[Tuple[Tuple[Decimal, Decimal], ...]] = None
    if depth_used:
        # depth_vwap_l2_v2: walk levels priced at-or-inside the cap best-first,
        # integrating min(remaining, displayed_sz); remainder at the cap (FD-M5-19).
        ladder = depth.asks if buy else depth.bids
        consumed = []
        remaining = qty
        with localcontext(_DECIMAL_CTX):
            cost = _ZERO
            for px, sz in ladder:
                if remaining <= 0:
                    break
                inside = (px <= capped_limit) if buy else (px >= capped_limit)
                if not inside:
                    break  # best-first ladder: deeper levels are only worse
                take = min(remaining, sz)
                if take > 0:  # zero-size padding levels consume nothing
                    consumed.append((px, take))
                    cost += take * px
                    remaining -= take
            fillable = qty - remaining
            if fillable == 0:
                return _unfillable("no_liquidity_at_cap")
            if remaining > 0:
                cost += remaining * capped_limit
                worst = capped_limit          # the cap for remainder (§I)
            else:
                worst = consumed[-1][0]       # deepest level consumed
            vwap = (cost / qty).quantize(MID_QUANTUM, ROUND_HALF_EVEN)
        levels = tuple(consumed)
        remainder = remaining
    else:
        # tob_l1_v1: min(qty, displayed opposite size) at the touch iff the touch
        # is at-or-inside the cap; remainder at the cap (FD-M5-19).
        marketable = (touch <= capped_limit) if buy else (touch >= capped_limit)
        if not marketable:
            return _unfillable("no_liquidity_at_cap")
        with localcontext(_DECIMAL_CTX):
            fillable = min(qty, touch_sz)
            remainder = qty - fillable
            cost = fillable * touch + remainder * capped_limit
            vwap = (cost / qty).quantize(MID_QUANTUM, ROUND_HALF_EVEN)
            worst = capped_limit if remainder > 0 else touch

    # EX-8 (frozen): adverse-positive slippage vs quote B's mid (the ONE quote
    # decider's quantized mid); None iff vwap or mid is None.
    mid = quote_b_verdict.mid
    slippage: Optional[Decimal] = None
    if mid is not None:
        with localcontext(_DECIMAL_CTX):
            raw = ((vwap - mid) / mid * _TEN_THOUSAND) if buy \
                else ((mid - vwap) / mid * _TEN_THOUSAND)
            slippage = raw.quantize(BPS_QUANTUM, ROUND_HALF_EVEN)

    return ModeledFill(
        modeled_fill_id=UNBOUND_MODELED_FILL_ID,
        model=model,
        realism_class="modeled_full" if remainder == 0 else "modeled_partial",
        requested_qty=qty,
        modeled_fillable_qty=fillable,
        modeled_vwap=ModeledUSD(vwap),
        worst_price=ModeledUSD(worst.quantize(MID_QUANTUM, ROUND_HALF_EVEN)),
        touch_price=ModeledUSD(touch),               # UNQUANTIZED (EX-2 operand)
        levels_consumed=levels,
        slippage_vs_mid_bps=slippage,
        modeled_cost_usd=ModeledUSD(cost),           # EXACT
        quote=quote_prov,
        reasons=tuple(sorted(reasons)),
    )


def bind_modeled_fill_id(fill: ModeledFill, *, order_id: str) -> ModeledFill:
    """Bind the §P.3 deterministic id (resolution 1):
    `"mf-" + row_hash({order_id, model, quote_b_seen_at_ms, vendor_seq|null})`.

    The caller (which alone knows the order_id) binds AFTER model_fill; the
    operands ride the fill's own provenance mapping. Single-use: re-binding an
    already-bound fill raises ExecError."""
    if not isinstance(fill, ModeledFill):
        raise ExecError(f"fill: must be a ModeledFill, got {type(fill).__name__}")
    if not isinstance(order_id, str) or not order_id:
        raise ExecError("order_id: must be a non-empty str")
    if fill.modeled_fill_id != UNBOUND_MODELED_FILL_ID:
        raise ExecError("modeled_fill_id already bound (single-use binding)")
    modeled_fill_id = "mf-" + row_hash({
        "order_id": order_id,
        "model": fill.model,
        "quote_b_seen_at_ms": fill.quote["seen_at_ms"],
        "vendor_seq": fill.quote["vendor_seq"],
    })
    return replace(fill, modeled_fill_id=modeled_fill_id)


# --- assess_divergence (FD-M5-20, frozen) --------------------------------------

@dataclass(frozen=True)
class DivergenceResult:
    """Plain-Decimal outputs — the ONE sanctioned cross-newtype comparison seam."""

    divergence_usd: Optional[Decimal]   # broker − modeled-over-filled-qty, EXACT
    divergence_bps: Optional[Decimal]   # / broker × 10000, BPS_QUANTUM
    flag: str                           # ∈ DIVERGENCE_FLAGS (side-aware, EX-3)
    alert: bool                         # |divergence_usd| > broker × 10bps (STRICT)

    def __post_init__(self) -> None:
        require_member(DIVERGENCE_FLAGS, self.flag, what="divergence flag")


def assess_divergence(*, side: str, broker_cost_usd: BrokerUSD, filled_qty: Decimal,
                      modeled: ModeledFill) -> DivergenceResult:
    """FD-M5-20 verbatim. `modeled_cost_over_filled_qty` is EXACT re-integration —
    `modeled_vwap × filled_qty` is FORBIDDEN (the vwap is MID_QUANTUM-quantized
    provenance; the two differ beyond DIVERGENCE_ALERT_BPS on realistic partials,
    EX-2). tob: min(filled_qty, modeled_fillable_qty) × touch_price + remainder ×
    cap; depth: integrate the FIRST filled_qty shares of the recorded walk prefix
    + remainder × cap. The cap operand is `worst_price` (resolution 2). Flag is
    side-aware: broker_optimistic iff the broker number favors us (buy:
    divergence < 0; sell, where broker_cost_usd is sale PROCEEDS: divergence > 0)."""
    _require_side(side)
    as_broker_usd(broker_cost_usd)  # TypeError unless BrokerUSD (lineage wall)
    if broker_cost_usd <= 0:
        raise ExecError(f"broker_cost_usd: must be > 0, got {broker_cost_usd}")
    if not isinstance(modeled, ModeledFill):
        raise ExecError(
            f"modeled: must be a ModeledFill, got {type(modeled).__name__}")
    _require_decimal(filled_qty, field="filled_qty", positive=True)

    if modeled.modeled_cost_usd is None:  # modeled side null => unassessed
        return DivergenceResult(divergence_usd=None, divergence_bps=None,
                                flag="unassessed", alert=False)

    if filled_qty > modeled.requested_qty:
        raise ExecError(
            f"filled_qty {filled_qty} exceeds requested_qty "
            f"{modeled.requested_qty} (broker cannot fill more than requested)")

    with localcontext(_DECIMAL_CTX):
        if modeled.model == _TOB_MODEL:
            fillable_part = min(filled_qty, modeled.modeled_fillable_qty)
            cost = fillable_part * modeled.touch_price
            remainder = filled_qty - fillable_part
            if remainder > 0:
                # remainder > 0 => filled > fillable => (filled <= requested)
                # requested > fillable => the model had its own remainder =>
                # worst_price == capped_limit (§I; resolution 2).
                cost += remainder * modeled.worst_price
        else:  # depth_vwap_l2_v2: first filled_qty shares of the walk prefix
            remaining = filled_qty
            cost = _ZERO
            for px, level_qty in modeled.levels_consumed:
                if remaining <= 0:
                    break
                take = min(remaining, level_qty)
                cost += take * px
                remaining -= take
            if remaining > 0:
                cost += remaining * modeled.worst_price  # the cap (resolution 2)

        divergence_usd = broker_cost_usd - cost          # plain Decimal (the seam)
        divergence_bps = (divergence_usd / broker_cost_usd * _TEN_THOUSAND
                          ).quantize(BPS_QUANTUM, ROUND_HALF_EVEN)
        threshold = broker_cost_usd * DIVERGENCE_ALERT_BPS / _TEN_THOUSAND
        alert = abs(divergence_usd) > threshold          # STRICT (FD-M5-6/20)

    if divergence_usd == 0:
        flag = "aligned"
    elif side == "buy":
        flag = "broker_optimistic" if divergence_usd < 0 else "broker_conservative"
    else:  # sell: broker_cost_usd is sale proceeds — higher favors us
        flag = "broker_optimistic" if divergence_usd > 0 else "broker_conservative"

    return DivergenceResult(divergence_usd=divergence_usd,
                            divergence_bps=divergence_bps,
                            flag=flag, alert=alert)
