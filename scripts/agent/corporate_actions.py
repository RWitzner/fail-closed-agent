"""M2 §D — multi-source corporate-action cross-validation, tri-state, fail-closed (S7 centerpiece).

The S7 heart. Cross-validates an Alpaca CA against a data-vendor CA; **>=2 INDEPENDENT sources
(distinct ``CaSource`` AND distinct ``source_ca_id``) are required to CLEAR a blackout**; a lone,
conflicting, or incomplete source stays blacked out; an **any-unexplained-broker-qty-delta detector**
freezes the symbol and forces an **IMMEDIATE (not EOD) reconcile**; **durable CUSIP/FIGI identity**
holds under unstable tickers.

Mirrors (verified at HEAD ``3e270eb``):
  - frozen dataclass + composed provenance (``recorder/event.py:52-60``),
  - closed-vocabulary enum, out-of-vocab FATAL (``recorder/event.py:33``),
  - graded exception tree rooted in ValueError (``recorder/event.py:37-49``),
  - ``Decimal(str(x))`` + quantize round-trip chokepoint (``recorder/event.py`` ``_quantize_checked``),
  - identity checked on every apply via the durable key (``recorder/book_state.py:55-64``).

OFFLINE PURITY (hard constraint): NO module-scope ``import databento`` / ``import alpaca`` /
``import exchange_calendars``. The live per-source fetchers are injected behind the ``CaFetcher``
Protocol; their real-SDK reads live behind the lazy/credentialed seam (deferred, §M). Pure, no I/O,
no clock, no float here.

DETERMINISM (DET-2): ``provenance_set`` is an IN-MEMORY frozenset ONLY — it must NEVER reach the
serializer (``agent.serializer.dumps`` raises ``TypeError`` on a frozenset). The single persisted form
of the source set is ``sorted(s.value for s in provenance_set)``, produced by ``ca_to_row`` (§E).
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from enum import Enum
from typing import (
    Dict,
    FrozenSet,
    List,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

FACTOR_QUANTUM = Decimal("0.00000001")   # 8dp split/adjust factor quantum (event.py:30 pattern)
CASH_QUANTUM = Decimal("0.0001")         # 4dp dividend/cash amounts (matches PRICE_QUANTUM, event.py:30)
MIN_INDEPENDENT_SOURCES = 2              # CODE CONSTANT (>=2 to clear; smaller == more-permissive -> NOT overlayable, §G)
BLACKOUT_LEAD_DAYS = 1                   # CODE CONSTANT (wider == safer -> NOT overlayable, §G)
BLACKOUT_TRAIL_DAYS = 1                  # CODE CONSTANT (T+1 settlement context, spec §8.7)


class CaType(str, Enum):                 # closed vocabulary; out-of-vocab -> FATAL (event.py:33 pattern)
    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    DIVIDEND = "dividend"
    SPECIAL_DIVIDEND = "special_dividend"
    SPINOFF = "spinoff"
    MERGER = "merger"
    SYMBOL_CHANGE = "symbol_change"
    OTHER = "other"


CA_TYPES: FrozenSet[str] = frozenset(t.value for t in CaType)

# CA types that REQUIRE a factor / cash to be a COMPLETE observation (S7-6 None-handling):
_FACTOR_REQUIRED = frozenset({CaType.SPLIT, CaType.REVERSE_SPLIT})
_CASH_REQUIRED = frozenset({CaType.DIVIDEND, CaType.SPECIAL_DIVIDEND})


class ValidationStatus(str, Enum):       # tri-state (spec §5 Tier-2 / S7)
    CONFIRMED = "confirmed_by_N_sources"               # >=2 INDEPENDENT sources agree -> blackout may CLEAR
    SINGLE_SOURCE_BLACKOUT = "single_source_blackout"  # exactly 1 independent source -> STAYS blacked out
    CONFLICTING_BLACKOUT = "conflicting_blackout"      # >=2 sources DISAGREE/incomplete -> STAYS blacked out (tightest)


class CaSource(str, Enum):
    ALPACA = "alpaca"
    DATA_VENDOR = "data_vendor"          # two DISTINCT enum members == two candidate-independent sources


# --- graded exception tree (mirrors event.py:37-49) ---
class CorporateActionError(ValueError):
    """Base: any CA processing failure is fail-closed (S7)."""


class UnvalidatedAdjustment(CorporateActionError):
    """An adjustment lacking >=2 independent confirming sources was treated as applied -> reject (S7)."""


class ConflictingSources(CorporateActionError):
    """Two+ sources disagree on type/factor/ex-date (or an incomplete required field) -> conflicting_blackout (S7)."""


class BrokerAdjustedDuringBlackout(CorporateActionError):
    """Broker position qty changed without an agent-originated fill -> FREEZE + IMMEDIATE reconcile (S7)."""


def _quantize_checked(raw, quantum: Decimal, *, field: str) -> Decimal:
    """``Decimal``-strict round-trip chokepoint (mirrors event._quantize_checked).

    Rejects float/None (fail-loud); ``q = d.quantize(quantum, ROUND_HALF_EVEN)``; a value that does
    NOT round-trip through its quantum raises ``CorporateActionError`` (the quantize silently changed
    it — e.g. a 9-dp factor against the 8-dp FACTOR_QUANTUM).
    """
    if raw is None or isinstance(raw, float):
        raise CorporateActionError(f"{field}: float/None not allowed (got {raw!r}); use Decimal")
    if not isinstance(raw, Decimal):
        raise CorporateActionError(f"{field}: must be a Decimal (got {type(raw).__name__})")
    if not raw.is_finite():
        raise CorporateActionError(f"{field}: non-finite value not allowed ({raw!r})")
    try:
        q = raw.quantize(quantum, ROUND_HALF_EVEN)
    except InvalidOperation as exc:
        raise CorporateActionError(f"{field}: not quantizable ({raw!r})") from exc
    if q != raw:
        raise CorporateActionError(f"{field}: {raw!r} does not round-trip through quantum {quantum}")
    return q


@dataclass(frozen=True)
class DurableId:
    """Durable identity under unstable tickers (spec §5 Tier-2 'CUSIP/FIGI identity'). ``figi``/``cusip``
    are the stable keys; ``ticker`` is the mutable display label. ``key()`` == figi if present else cusip;
    raises ``CorporateActionError`` if BOTH are None (a ticker-only identity is NOT durable -> fail-closed).
    Checked on EVERY apply (mirrors book_state._check_identity, book_state.py:55-64) so a ticker reuse
    cannot cross-contaminate CA state."""

    cusip: Optional[str]
    figi: Optional[str]
    ticker: str

    def key(self) -> str:
        if self.figi is not None:
            return self.figi
        if self.cusip is not None:
            return self.cusip
        raise CorporateActionError(
            f"ticker-only identity is not durable (cusip/figi both None, ticker={self.ticker!r})"
        )


@dataclass(frozen=True)
class CaProvenance:
    """Composed (NOT inlined) provenance, mirroring event.Provenance (event.py:52-60). ``source_ca_id``
    is the vendor's own CA id — NAMED ``source_ca_id``, NEVER ``seq`` (journal _RESERVED, journal.py:21;
    BLOCKER-1 lesson, event.py:14). Wall-clock fields are PERSISTED in the row (DET-1) but EXCLUDED from
    any hash payload that must re-derive across runs."""

    source: CaSource
    source_ca_id: str
    announced_ts_utc: str
    ts_recv_utc: str


@dataclass(frozen=True)
class SourceObservation:
    """ONE source's report of a CA. Decimal factors/cash only, via ``Decimal(str(x)).quantize``
    (event._quantize_checked chokepoint); a float fails loud at serialize."""

    source: CaSource
    durable_id: DurableId
    ca_type: CaType
    ex_date_et: str                  # "YYYY-MM-DD" ET ex-date
    factor: Optional[Decimal]        # split/adjust factor (FACTOR_QUANTUM); None for pure cash div / symbol_change
    cash_amount: Optional[Decimal]   # dividend/cash (CASH_QUANTUM); None otherwise
    provenance: CaProvenance


@dataclass(frozen=True)
class AdjustmentEvent:
    """One cross-validated corporate action (replays identically). ``provenance_set`` is the in-memory
    FROZENSET of contributing ``CaSource``; ``provenance`` is the ORDERED tuple of full ``CaProvenance``
    (what gets persisted, DET-1). ``validation_status`` is the tri-state.

    NOTE (DET-2): ``provenance_set`` is an IN-MEMORY field ONLY. A set/frozenset must NEVER reach the
    serializer (serializer._default raises TypeError on a frozenset). The single persisted form of the
    source set is ``provenance_sources = sorted(s.value for s in provenance_set)``, produced by
    ``ca_to_row`` (§E)."""

    durable_id: DurableId
    symbol: str                          # current ticker label at emit time (durable_id is truth)
    ca_type: CaType
    ex_date_et: str
    factor: Optional[Decimal]
    cash_amount: Optional[Decimal]
    provenance_set: FrozenSet[CaSource]  # in-memory ONLY (never serialized directly)
    provenance: Tuple[CaProvenance, ...]  # one per contributing source — PERSISTED in full for exact round-trip
    validation_status: ValidationStatus
    provenance_independent: bool         # True iff >=2 distinct CaSource AND >=2 distinct source_ca_id (S7-3)
    blackout_from_et: str                # ex_date - BLACKOUT_LEAD_DAYS (inclusive)
    blackout_to_et: str                  # ex_date + BLACKOUT_TRAIL_DAYS (inclusive)

    @property
    def blackout(self) -> bool:
        """INDEFINITE (validation) blackout flag: True unless ``validation_status == CONFIRMED``. A CONFIRMED
        event imposes only a date-BOUNDED ex-date-window blackout (handled by ``is_blacked_out``), so its
        indefinite flag is False; a non-CONFIRMED event imposes an OPEN-ENDED blackout. This is NOT 'is this
        date blacked out' — that is ``CorporateActionFeed.is_blacked_out(on_date_et)`` (the two were
        conflated; MED-6)."""
        return self.validation_status != ValidationStatus.CONFIRMED


@runtime_checkable
class CaFetcher(Protocol):
    """Injectable per-source CA fetcher seam (mirrors databento raw_source injection, databento.py:46).
    Offline tests inject a scripted fake (tests/lib/fakes style); the live impl lazily reads the
    broker/vendor API behind the same lazy/credentialed seam (deferred, §M)."""

    def fetch(self, durable_id: DurableId) -> Tuple[SourceObservation, ...]:
        ...


def _shift_iso_date(ex_date_et: str, *, days: int) -> str:
    try:
        d = date.fromisoformat(ex_date_et)
    except ValueError as exc:
        raise CorporateActionError(f"ex_date_et is not an ISO date: {ex_date_et!r}") from exc
    return (d + timedelta(days=days)).isoformat()


def _norm_factor(obs: SourceObservation) -> Optional[Decimal]:
    if obs.factor is None:
        return None
    return _quantize_checked(obs.factor, FACTOR_QUANTUM, field="factor")


def _norm_cash(obs: SourceObservation) -> Optional[Decimal]:
    if obs.cash_amount is None:
        return None
    return _quantize_checked(obs.cash_amount, CASH_QUANTUM, field="cash_amount")


def _is_complete(obs: SourceObservation) -> bool:
    """S7-6: a SPLIT/REVERSE_SPLIT with no factor, or a DIVIDEND/SPECIAL_DIVIDEND with no cash, is
    INCOMPLETE and cannot contribute to CONFIRMED."""
    if obs.ca_type in _FACTOR_REQUIRED and obs.factor is None:
        return False
    if obs.ca_type in _CASH_REQUIRED and obs.cash_amount is None:
        return False
    return True


def _obs_total_key(obs, norm_factor, norm_cash):
    """TOTAL order over a SourceObservation (CA-1): ties on (source, source_ca_id) are broken by the FULL
    normalized payload, so the EMITTED reference fields, the persisted provenance order, and the row_hash
    never depend on input order — even for same-source-same-source_ca_id duplicates (a real vendor data
    defect: one source emitting two/amended records under one CA id). Decimals are compared as strings via
    the already-quantized norm_factor/norm_cash (None -> "" sentinel to avoid a None-vs-str ordering error)."""
    p = obs.provenance
    d = obs.durable_id
    return (
        p.source.value, p.source_ca_id, obs.ca_type.value, obs.ex_date_et,
        "" if norm_factor is None else str(norm_factor),
        "" if norm_cash is None else str(norm_cash),
        d.cusip or "", d.figi or "", d.ticker,
        p.announced_ts_utc, p.ts_recv_utc,
    )


def cross_validate(observations: Tuple[SourceObservation, ...], *,
                   lead_days: int = BLACKOUT_LEAD_DAYS,
                   trail_days: int = BLACKOUT_TRAIL_DAYS) -> AdjustmentEvent:
    """PURE tri-state validation — a function of the observation/provenance set ONLY (referentially
    transparent: same set => same status => hand-auditable).

      - All observations must share ONE ``(durable_id.key(), ex_date_et)`` group — the "same CA event"
        grain. ``ca_type`` is NOT in the grouping key (LOW-3): two sources reporting a DIFFERENT ca_type
        for the same (durable_id, ex_date) land in the SAME group so the disagreement is VISIBLE ->
        CONFLICTING_BLACKOUT (grouping BY ca_type would split them into two single-source groups and HIDE
        the conflict). ``CorporateActionFeed.adjustments_for`` splits the multi-group fetch into per-group
        calls; ``cross_validate`` validates ONE group.
      - 0 observations -> CorporateActionError (nothing to validate).
      - independent sources = distinct ``CaSource`` members WHOSE ``source_ca_id`` values are ALSO distinct
        (S7-3: same source twice, OR two sources mirroring the same source_ca_id, does NOT count as 2).
      - INCOMPLETE observation (S7-6): a SPLIT/REVERSE_SPLIT factor=None or DIVIDEND/SPECIAL_DIVIDEND
        cash=None cannot contribute to CONFIRMED -> treated as a disagreement -> CONFLICTING_BLACKOUT.
      - any two sources DISAGREE on (ca_type, ex_date, factor-within-FACTOR_QUANTUM, cash-within-
        CASH_QUANTUM) -> CONFLICTING_BLACKOUT (disagreement DOMINATES agreement).
      - elif (>= MIN_INDEPENDENT_SOURCES independent) AND they AGREE AND all complete -> CONFIRMED.
      - else (exactly 1 independent / incomplete singletons) -> SINGLE_SOURCE_BLACKOUT.

    factor/cash compared after .quantize(..., ROUND_HALF_EVEN); a non-round-tripping value raises.
    NEVER-LOOSEN CLAMP (HIGH-3): lead_days/trail_days exist ONLY so tests can exercise window arithmetic;
    cross_validate RAISES CorporateActionError unless lead_days >= BLACKOUT_LEAD_DAYS AND trail_days >=
    BLACKOUT_TRAIL_DAYS — a caller may only WIDEN, never SHRINK, the blackout.
    blackout window = [ex_date-lead_days, ex_date+trail_days] ET, CLOSED/INCLUSIVE on both edges (S7-4).
    """
    if lead_days < BLACKOUT_LEAD_DAYS or trail_days < BLACKOUT_TRAIL_DAYS:
        raise CorporateActionError(
            f"window override may only WIDEN, never shrink (lead_days={lead_days} trail_days={trail_days}; "
            f"min lead={BLACKOUT_LEAD_DAYS} trail={BLACKOUT_TRAIL_DAYS})"
        )
    if not observations:
        raise CorporateActionError("cross_validate requires >=1 observation (nothing to validate)")

    # All observations must belong to one (durable_key, ex_date) group — the feed splits before calling.
    keys = {(o.durable_id.key(), o.ex_date_et) for o in observations}
    if len(keys) != 1:
        raise CorporateActionError(
            f"cross_validate received observations spanning multiple (durable_key, ex_date) groups: {sorted(keys)}"
        )

    # Quantize-check every factor/cash up-front (fail-loud on a non-round-tripping value).
    norm = [(o, _norm_factor(o), _norm_cash(o)) for o in observations]
    # harden OFFLINE-1 + CA-1: deterministic reference via a TOTAL order. Ties on (source, source_ca_id)
    # are broken by the full normalized payload so the EMITTED ca_type/factor/cash/durable_id/symbol AND
    # the persisted provenance order (hence row_hash) are order-INDEPENDENT for ANY observation set,
    # including same-source-same-source_ca_id duplicates. Conflict/independence/completeness stay set-based.
    norm.sort(key=lambda t: _obs_total_key(t[0], t[1], t[2]))

    # Reference observation (canonical first after sort) — disagreement is measured pairwise against it.
    ref_obs, ref_factor, ref_cash = norm[0]
    conflicting = False
    all_complete = _is_complete(ref_obs)
    for obs, factor, cash in norm[1:]:
        all_complete = all_complete and _is_complete(obs)
        if obs.ca_type != ref_obs.ca_type:
            conflicting = True
        if obs.ex_date_et != ref_obs.ex_date_et:
            conflicting = True
        if factor != ref_factor:
            conflicting = True
        if cash != ref_cash:
            conflicting = True

    # Independence: distinct CaSource members AND distinct source_ca_id values (S7-3).
    distinct_sources = {o.source for o in observations}
    distinct_ca_ids = {o.provenance.source_ca_id for o in observations}
    independent = (
        len(distinct_sources) >= MIN_INDEPENDENT_SOURCES
        and len(distinct_ca_ids) >= MIN_INDEPENDENT_SOURCES
    )

    if conflicting:
        status = ValidationStatus.CONFLICTING_BLACKOUT
    elif not all_complete:
        # complete agreement on type/ex_date/factor/cash but a REQUIRED field is missing -> never clears.
        status = ValidationStatus.CONFLICTING_BLACKOUT
    elif independent:
        status = ValidationStatus.CONFIRMED
    else:
        status = ValidationStatus.SINGLE_SOURCE_BLACKOUT

    blackout_from = _shift_iso_date(ref_obs.ex_date_et, days=-lead_days)
    blackout_to = _shift_iso_date(ref_obs.ex_date_et, days=trail_days)

    provenance_set: FrozenSet[CaSource] = frozenset(distinct_sources)
    # Provenance order derived from the SAME total ordering as the reference (CA-1): consistent and
    # order-independent for ANY input order. (norm is already sorted by _obs_total_key above.)
    provenance = tuple(t[0].provenance for t in norm)

    return AdjustmentEvent(
        durable_id=ref_obs.durable_id,
        symbol=ref_obs.durable_id.ticker,
        ca_type=ref_obs.ca_type,
        ex_date_et=ref_obs.ex_date_et,
        factor=ref_factor,
        cash_amount=ref_cash,
        provenance_set=provenance_set,
        provenance=provenance,
        validation_status=status,
        provenance_independent=independent,
        blackout_from_et=blackout_from,
        blackout_to_et=blackout_to,
    )


class CorporateActionFeed:
    """Multi-source CA aggregator with INJECTABLE fetchers (offline = scripted fakes; NO network, NO SDK
    import at module scope). Builds ``SourceObservation``s and runs ``cross_validate``; it NEVER applies an
    adjustment to a position (M2 is the decider, not the executor). ``adjustments_for``/``is_blacked_out``
    are deterministic ONLY given fixed fetcher outputs (DET-4)."""

    def __init__(self, fetchers: Dict[CaSource, CaFetcher]) -> None:
        self._fetchers = dict(fetchers)

    def adjustments_for(self, durable_id: DurableId, *, ts_recv_utc: str) -> Tuple[AdjustmentEvent, ...]:
        """Fetch every injected source, group by ``(durable_key, ex_date_et)``, ``cross_validate`` each
        group; one ``AdjustmentEvent`` per group (LOW-3: ca_type is NOT a group key)."""
        observations: List[SourceObservation] = []
        for source in sorted(self._fetchers, key=lambda s: s.value):
            fetcher = self._fetchers[source]
            observations.extend(fetcher.fetch(durable_id))

        groups: Dict[Tuple[str, str], List[SourceObservation]] = {}
        for obs in observations:
            groups.setdefault((obs.durable_id.key(), obs.ex_date_et), []).append(obs)

        events = [
            cross_validate(tuple(group))
            for _, group in sorted(groups.items(), key=lambda kv: kv[0])
        ]
        return tuple(events)

    def is_blacked_out(self, durable_id: DurableId, *, on_date_et: str) -> bool:
        """ONE coherent rule (MED-6). True iff, for ANY ``AdjustmentEvent`` E of this durable_id, EITHER:
          - E.validation_status == CONFIRMED AND blackout_from_et <= on_date_et <= blackout_to_et
              (even a CONFIRMED CA blacks out its own ex-date WINDOW; it CLEARS once on_date_et passes
               blackout_to_et); OR
          - E.validation_status != CONFIRMED AND on_date_et >= blackout_from_et
              (an unconfirmed/single-source/conflicting/incomplete CA imposes an OPEN-ENDED blackout from
               its window start that NEVER self-clears; only a >=2-independent-source upgrade to CONFIRMED
               bounds it, S7-1/4, fail-closed).
        So CONFIRMED -> bounded window; non-CONFIRMED -> open-ended from window start (strictly more
        conservative)."""
        for event in self.adjustments_for(durable_id, ts_recv_utc=""):
            if event.validation_status == ValidationStatus.CONFIRMED:
                if event.blackout_from_et <= on_date_et <= event.blackout_to_et:
                    return True
            else:
                if on_date_et >= event.blackout_from_et:
                    return True
        return False


class FreezeReason(str, Enum):
    BROKER_ADJUSTED_DURING_BLACKOUT = "broker_adjusted_during_blackout"  # delta during a known CA blackout
    BROKER_ADJUSTED_NO_KNOWN_CA = "broker_adjusted_no_known_ca"          # delta with NO CA in the feed (S7-1)


@dataclass(frozen=True)
class FreezeSignal:
    durable_id: DurableId
    symbol: str
    immediate_reconcile: bool        # ALWAYS True for an unexplained broker qty delta (NOT deferred to EOD)
    prev_qty: Decimal
    curr_qty: Decimal
    reason: FreezeReason


class BrokerAdjustDetector:
    """Detects an UNEXPLAINED broker position-qty change (S7-1). Compares the BROKER qty (broker truth;
    a PLAIN ``Decimal`` share count sourced ONLY from ``broker.positions()`` — NOT BrokerUSD, which is USD;
    DET-3/OP-3). ANY qty != baseline that the agent did not originate is a freeze, regardless of blackout
    state — the blackout/known-CA state only LABELS the reason (FreezeReason). Until M5 fills exist there
    is no agent-originated-fill ledger to net against, so M2's posture is: any broker qty delta -> freeze
    + immediate reconcile.

    SEEDING (frozen, fail-closed S7-2): the baseline MUST be seeded EXPLICITLY via ``seed_baseline()`` from
    the broker position-of-record BEFORE any observe. ``observe_broker_qty`` with NO prior baseline for a
    symbol RAISES ``BrokerAdjustedDuringBlackout`` (it MUST NOT silently seed the first observation — a
    restart mid-CA would otherwise absorb an adjusted qty as the baseline). The precise SOD seeding sequence
    lands in M6 reconcile (§M)."""

    def __init__(self) -> None:
        self._baseline: Dict[str, Decimal] = {}   # durable_id.key() -> last-known BROKER qty (plain Decimal shares)
        self._frozen: set = set()

    @staticmethod
    def _require_decimal(qty, *, field: str) -> Decimal:
        if isinstance(qty, bool) or not isinstance(qty, Decimal):
            raise CorporateActionError(f"{field} must be a Decimal share count (got {type(qty).__name__})")
        if not qty.is_finite():
            raise CorporateActionError(f"{field}: non-finite qty not allowed ({qty!r})")
        return qty

    def seed_baseline(self, durable_id: DurableId, broker_qty: Decimal) -> None:
        """Set the baseline from the broker position-of-record (a CA-implied/modeled qty MUST NEVER seed
        or override this — the invariant tested by test_ca_implied_qty_cannot_override_broker_baseline)."""
        qty = self._require_decimal(broker_qty, field="broker_qty")
        self._baseline[durable_id.key()] = qty

    def observe_broker_qty(self, durable_id: DurableId, broker_qty: Decimal, *,
                           blacked_out: bool) -> Optional["FreezeSignal"]:
        """No baseline -> raise ``BrokerAdjustedDuringBlackout`` (S7-2). broker_qty != baseline -> return a
        ``FreezeSignal(immediate_reconcile=True)``, reason BROKER_ADJUSTED_DURING_BLACKOUT if blacked_out
        else BROKER_ADJUSTED_NO_KNOWN_CA (S7-1), and mark frozen. Else (qty == baseline) return None.
        broker_qty MUST be a Decimal (whole shares); a float raises (fail-closed)."""
        qty = self._require_decimal(broker_qty, field="broker_qty")
        key = durable_id.key()
        if key not in self._baseline:
            raise BrokerAdjustedDuringBlackout(
                f"no seeded baseline for {key!r} (ticker={durable_id.ticker!r}); refuse to silently seed "
                f"the first observation (S7-2 — a restart mid-CA must not absorb an adjusted qty)"
            )
        baseline = self._baseline[key]
        if qty != baseline:
            self._frozen.add(key)
            reason = (
                FreezeReason.BROKER_ADJUSTED_DURING_BLACKOUT
                if blacked_out
                else FreezeReason.BROKER_ADJUSTED_NO_KNOWN_CA
            )
            return FreezeSignal(
                durable_id=durable_id,
                symbol=durable_id.ticker,
                immediate_reconcile=True,
                prev_qty=baseline,
                curr_qty=qty,
                reason=reason,
            )
        return None

    def is_frozen(self, durable_id: DurableId) -> bool:
        return durable_id.key() in self._frozen
