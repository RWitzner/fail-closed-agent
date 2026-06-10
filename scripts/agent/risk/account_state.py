"""M4 §B — broker account/portfolio read models. The broker's numbers are GROUND TRUTH
(FD-M4-2): this module copies, validates, and stamps them — it never recomputes them.
Validation is constructive: an invalid payload never becomes a snapshot; it becomes
`AccountInvalid` (S2 at the account seam, FD-M4-25).

Freshness TTLs are CODE CONSTANTS with shorten-only clamps at every override surface
(FD-M4-22, the market_state_cache HIGH-3 pattern); staleness boundaries are strict `>`
on the injected monotonic ms clock; `now_ms < seen_at_ms` is SKEW — clock regression is
DATA (reject-opens), never an exception (R12).
"""
from dataclasses import dataclass
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Mapping, Optional, Protocol, Tuple, Union, runtime_checkable

from agent.risk.reasons import require_account_status
from agent.serializer import BrokerUSD, row_hash

ACCOUNT_FRESHNESS_TTL_MS = 5000     # CODE CONSTANTS (FD-M4-22); overrides may only SHORTEN
PORTFOLIO_FRESHNESS_TTL_MS = 5000   #   (HIGH-3 clamp, market_state_cache.py:52-62)
MARK_FRESHNESS_TTL_MS = 2000        # mirrors DEFAULT_FRESHNESS_TTL_MS (market_state_cache.py:34)
USD_QUANTUM = Decimal("0.01")       # account_snapshot-row-ONLY quantum, quantize-only (FD-M4-25/LD-R5)
MID_QUANTUM = Decimal("0.000001")   # the Mark.mid grid (mirrors quote_quality.MID_QUANTUM, FD-M4-16)

# Pinned context for grid checks on the read path (harden round 1, M4-DET-2).
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)

ACCOUNT_SOURCES = frozenset({"alpaca_paper", "alpaca_live", "fixture", "spy"})  # closed vocab
MARK_SOURCES = frozenset({"quote_mid"})  # frozen vocab in M4 (§B)

_REQUIRED_MONEY_FIELDS = ("equity", "last_equity", "cash", "buying_power", "maintenance_margin")
_NONNEGATIVE_FIELDS = frozenset({"buying_power", "maintenance_margin"})


@dataclass(frozen=True)
class BrokerAccountRead:
    """One reconciled broker account() read, parsed from a raw dict payload.
    REQUIRED money fields are exact BrokerUSD (no quantization at parse, FD-M4-25)."""

    equity: BrokerUSD                   # REQUIRED, finite
    last_equity: BrokerUSD              # REQUIRED, finite — the daily-loss base (FD-M4-18)
    cash: BrokerUSD                     # REQUIRED, finite
    buying_power: BrokerUSD             # REQUIRED, finite, >= 0
    maintenance_margin: BrokerUSD       # REQUIRED, finite, >= 0 ("margin to be maintained", V3)
    multiplier: Optional[Decimal]       # provenance only
    daytrading_buying_power: Optional[BrokerUSD]
    pattern_day_trader: Optional[bool]  # strict bool or None (absence is pdt_compat evidence)
    daytrade_count: Optional[int]       # provenance only — NEVER an input to any gate (FD-M4-2)
    source: str                         # ∈ ACCOUNT_SOURCES
    ts_read_utc: str                    # broker/report wall stamp (provenance, never compared)
    seen_at_ms: int                     # injected monotonic receipt stamp
    account_snapshot_id: str            # "as-" + row_hash(§K.3 canonical dict)


@dataclass(frozen=True)
class AccountInvalid:
    reason: str       # e.g. "missing_field:buying_power", "non_finite:equity",
                      # "float_typed:cash", "bool_typed:equity", "negative:buying_power"
    source: str
    seen_at_ms: int


def _parse_money_value(value, *, field: str):
    """Returns (Decimal, None) or (None, invalid_reason). NEVER raises on the read path
    (S2: validation is constructive, FD-M4-25)."""
    if isinstance(value, bool):
        return None, f"bool_typed:{field}"
    if isinstance(value, float):
        return None, f"float_typed:{field}"
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return None, f"unparseable:{field}"
    else:
        return None, f"invalid_type:{field}"
    if not parsed.is_finite():
        return None, f"non_finite:{field}"
    return Decimal(str(parsed)), None


def parse_account_payload(payload: Mapping, *, source: str,
                          seen_at_ms: int, ts_read_utc: str
                          ) -> Union[BrokerAccountRead, AccountInvalid]:
    """The ONE parser chokepoint for raw broker account payloads (LD8). An out-of-vocab
    `source` is a PROGRAMMING error (raises); every payload defect is DATA
    (`AccountInvalid`, never an exception and never a constructed snapshot)."""
    if source not in ACCOUNT_SOURCES:
        raise ValueError(f"out-of-vocabulary account source: {source!r}")

    def invalid(reason: str) -> AccountInvalid:
        return AccountInvalid(reason=reason, source=source, seen_at_ms=int(seen_at_ms))

    money = {}
    for field in _REQUIRED_MONEY_FIELDS:
        if field not in payload:
            return invalid(f"missing_field:{field}")
        parsed, reason = _parse_money_value(payload[field], field=field)
        if reason is not None:
            return invalid(reason)
        if field in _NONNEGATIVE_FIELDS and parsed < 0:
            return invalid(f"negative:{field}")
        money[field] = BrokerUSD(parsed)

    multiplier: Optional[Decimal] = None
    if payload.get("multiplier") is not None:
        parsed, reason = _parse_money_value(payload["multiplier"], field="multiplier")
        if reason is not None:
            return invalid(reason)
        multiplier = parsed

    dtbp: Optional[BrokerUSD] = None
    if payload.get("daytrading_buying_power") is not None:
        parsed, reason = _parse_money_value(
            payload["daytrading_buying_power"], field="daytrading_buying_power")
        if reason is not None:
            return invalid(reason)
        dtbp = BrokerUSD(parsed)

    pdt = payload.get("pattern_day_trader")
    if pdt is not None and not isinstance(pdt, bool):
        return invalid("non_bool:pattern_day_trader")

    daytrade_count = payload.get("daytrade_count")
    if daytrade_count is not None and (
            isinstance(daytrade_count, bool) or not isinstance(daytrade_count, int)):
        return invalid("non_int:daytrade_count")

    # §K.3: deterministic identity — NO seen_at_ms, NO ts_read_utc (M4C-6/F9).
    snapshot_id = "as-" + row_hash({
        "equity": money["equity"],
        "last_equity": money["last_equity"],
        "cash": money["cash"],
        "buying_power": money["buying_power"],
        "maintenance_margin": money["maintenance_margin"],
        "daytrading_buying_power": dtbp,
        "pattern_day_trader": pdt,
        "daytrade_count": daytrade_count,
        "multiplier": multiplier,
        "source": source,
    })

    return BrokerAccountRead(
        equity=money["equity"],
        last_equity=money["last_equity"],
        cash=money["cash"],
        buying_power=money["buying_power"],
        maintenance_margin=money["maintenance_margin"],
        multiplier=multiplier,
        daytrading_buying_power=dtbp,
        pattern_day_trader=pdt,
        daytrade_count=daytrade_count,
        source=source,
        ts_read_utc=ts_read_utc,
        seen_at_ms=int(seen_at_ms),
        account_snapshot_id=snapshot_id,
    )


@dataclass(frozen=True)
class AccountRead:
    """can_open's third positional argument: the latest read + the freshness verdict,
    computed by AccountStore.get at snapshot time (LD5: can_open re-checks nothing)."""

    status: str                          # ∈ ACCOUNT_STATUSES
    read: Optional[BrokerAccountRead]    # present iff status ∈ {"fresh","stale"}
    age_ms: Optional[int]
    invalid_reason: Optional[str]        # set iff status == "invalid"

    def __post_init__(self):
        require_account_status(self.status)


class AccountStore:
    """Freshness-gated, NON-BLOCKING (MarketStateCache mirror: strict '>' boundary,
    degrade-to-reject-opens, no inline refresh)."""

    def __init__(self, *, clock, ttl_ms: int = ACCOUNT_FRESHNESS_TTL_MS) -> None:
        if ttl_ms > ACCOUNT_FRESHNESS_TTL_MS:
            raise ValueError(
                "ttl_ms may only SHORTEN account freshness (FD-M4-22 clamp): "
                f"{ttl_ms} > ACCOUNT_FRESHNESS_TTL_MS={ACCOUNT_FRESHNESS_TTL_MS}")
        self._clock = clock
        self._ttl_ms = int(ttl_ms)
        self._latest: Optional[Union[BrokerAccountRead, AccountInvalid]] = None
        self._latest_read: Optional[BrokerAccountRead] = None

    def put(self, result: Union[BrokerAccountRead, AccountInvalid]) -> None:
        if isinstance(result, BrokerAccountRead):
            self._latest_read = result
        elif not isinstance(result, AccountInvalid):
            raise ValueError(
                f"AccountStore.put takes BrokerAccountRead | AccountInvalid, "
                f"got {type(result).__name__}")
        self._latest = result

    def get(self, *, now_ms: Optional[int] = None) -> AccountRead:
        """never put -> "missing"; last put AccountInvalid -> "invalid";
        now_ms < seen_at_ms -> "skew"; age > ttl (strict) -> "stale"; else "fresh"."""
        now = self._clock.now_ms() if now_ms is None else int(now_ms)
        latest = self._latest
        if latest is None:
            return AccountRead(status="missing", read=None, age_ms=None, invalid_reason=None)
        if isinstance(latest, AccountInvalid):
            age = now - latest.seen_at_ms if now >= latest.seen_at_ms else None
            return AccountRead(status="invalid", read=None, age_ms=age,
                               invalid_reason=latest.reason)
        if now < latest.seen_at_ms:
            return AccountRead(status="skew", read=None, age_ms=None, invalid_reason=None)
        age = now - latest.seen_at_ms
        status = "stale" if age > self._ttl_ms else "fresh"
        return AccountRead(status=status, read=latest, age_ms=age, invalid_reason=None)

    def latest_unsafe(self) -> Optional[BrokerAccountRead]:
        """Last-known read regardless of freshness. SOLE legitimate consumer: the CALLER
        assembling the kill/flatten annotation input for RiskKillSwitch.trigger's
        keyword-only `account` kwarg (M4C-1; FD-M4-3 — staleness never blocks a reduce).
        can_open MUST NOT call this (test-asserted)."""
        return self._latest_read


def _require_finite_decimal_field(value, *, field: str, allow_none: bool = False):
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{field}: required Decimal, got None")
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, Decimal):
        raise ValueError(f"{field}: must be a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{field}: non-finite Decimal not allowed: {value!r}")
    return value


@dataclass(frozen=True)
class PositionRead:
    symbol: str
    qty: Decimal                  # SIGNED, finite, != 0 (zero-qty rows are dropped at parse)
    market_value: BrokerUSD       # REQUIRED signed broker-reported MV (FD-M4-17)
    avg_entry_price: Optional[Decimal] = None
    instrument_id: Optional[int] = None  # broker payloads carry none; mapped when the caller can

    def __post_init__(self):
        qty = _require_finite_decimal_field(self.qty, field="qty")
        if qty == 0:
            raise ValueError("PositionRead.qty must be non-zero (flat is not held)")
        _require_finite_decimal_field(self.market_value, field="market_value")
        _require_finite_decimal_field(self.avg_entry_price, field="avg_entry_price",
                                      allow_none=True)


@dataclass(frozen=True)
class PortfolioRead:
    positions: Tuple[PositionRead, ...]   # sorted by symbol; duplicate symbol -> ValueError
    source: str
    seen_at_ms: int
    stale: bool                           # precomputed by the caller against PORTFOLIO TTL
    unreconciled_drift: bool = False      # M6 sets True on a KNOWN reconcile mismatch

    def __post_init__(self):
        symbols = [p.symbol for p in self.positions]
        if len(set(symbols)) != len(symbols):
            raise ValueError("PortfolioRead: duplicate position symbol")
        if list(symbols) != sorted(symbols):
            raise ValueError("PortfolioRead: positions must be sorted by symbol")

    def qty_for(self, symbol: str) -> Decimal:
        for position in self.positions:
            if position.symbol == symbol:
                return position.qty
        return Decimal("0")


def parse_positions_payload(rows, *, source: str, seen_at_ms: int,
                            stale: bool = False) -> PortfolioRead:
    """Malformed row (float/non-finite/missing market_value/duplicate symbol) -> ValueError;
    qty==0 rows are SILENTLY DROPPED at parse (flat is not held — deterministic filter,
    LD-R4/M4C-7). The caller maps a failed parse to portfolio_missing (fail-closed)."""
    if source not in ACCOUNT_SOURCES:
        raise ValueError(f"out-of-vocabulary positions source: {source!r}")
    positions = []
    seen = set()
    for i, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"positions[{i}]: row must be a mapping")
        for key in ("symbol", "qty", "market_value"):
            if key not in row:
                raise ValueError(f"positions[{i}]: missing required field {key!r}")
        symbol = row["symbol"]
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f"positions[{i}]: symbol must be a non-empty string")
        if symbol in seen:
            raise ValueError(f"positions[{i}]: duplicate symbol {symbol!r}")
        seen.add(symbol)
        qty, reason = _parse_money_value(row["qty"], field="qty")
        if reason is not None:
            raise ValueError(f"positions[{i}]: {reason}")
        mv, reason = _parse_money_value(row["market_value"], field="market_value")
        if reason is not None:
            raise ValueError(f"positions[{i}]: {reason}")
        if qty == 0:
            continue  # LD-R4: flat is not held — silently dropped
        avg_entry = None
        if row.get("avg_entry_price") is not None:
            avg_entry, reason = _parse_money_value(row["avg_entry_price"],
                                                   field="avg_entry_price")
            if reason is not None:
                raise ValueError(f"positions[{i}]: {reason}")
        instrument_id = row.get("instrument_id")
        if instrument_id is not None and (
                isinstance(instrument_id, bool) or not isinstance(instrument_id, int)):
            raise ValueError(f"positions[{i}]: instrument_id must be an int or None")
        positions.append(PositionRead(
            symbol=symbol, qty=qty, market_value=BrokerUSD(mv),
            avg_entry_price=avg_entry, instrument_id=instrument_id))
    positions.sort(key=lambda p: p.symbol)
    return PortfolioRead(positions=tuple(positions), source=source,
                         seen_at_ms=int(seen_at_ms), stale=bool(stale))


def portfolio_is_stale(seen_at_ms: int, now_ms: int, *,
                       ttl_ms: int = PORTFOLIO_FRESHNESS_TTL_MS) -> bool:
    """Strict '>' boundary; the M5 caller MUST use this helper to precompute
    PortfolioRead.stale — the TTL arithmetic is owned and tested HERE (FD-M4-22)."""
    if ttl_ms > PORTFOLIO_FRESHNESS_TTL_MS:
        raise ValueError(
            "ttl_ms may only SHORTEN portfolio freshness (FD-M4-22 clamp): "
            f"{ttl_ms} > PORTFOLIO_FRESHNESS_TTL_MS={PORTFOLIO_FRESHNESS_TTL_MS}")
    return (int(now_ms) - int(seen_at_ms)) > int(ttl_ms)


@dataclass(frozen=True)
class Mark:
    """Optional conservative notional tightener (FD-M4-16). mid sourced from
    QuoteVerdict.mid (quote_quality.py:101-103, MID_QUANTUM-quantized)."""

    symbol: str
    instrument_id: int
    mid: Decimal                 # finite, > 0, MID_QUANTUM grid
    seen_at_ms: int
    source: str                  # frozen vocab {"quote_mid"} in M4

    def __post_init__(self):
        mid = _require_finite_decimal_field(self.mid, field="mid")
        if mid <= 0:
            raise ValueError(f"Mark.mid must be > 0, got {mid}")
        # Pinned context (harden round 1, M4-DET-2): a caller-mutated ambient context
        # must never make a VALID on-grid Mark fail to construct (FD-M4-16: a mark is
        # an optional tightener, never a crash source).
        with localcontext(_DECIMAL_CTX):
            off_grid = mid.quantize(MID_QUANTUM, ROUND_HALF_EVEN) != mid
        if off_grid:
            raise ValueError(f"Mark.mid {mid} is off the MID_QUANTUM grid {MID_QUANTUM}")
        if self.source not in MARK_SOURCES:
            raise ValueError(f"out-of-vocabulary mark source: {self.source!r}")


@runtime_checkable
class AccountReadProvider(Protocol):
    """The broker seam (LD8). Returns RAW payloads; parsing has ONE chokepoint above.
    M4 ships only FakeAccountProvider (tests/lib/risk_fixtures.py); M5 binds Alpaca."""

    def account_payload(self) -> Mapping: ...
    def positions_payload(self) -> list: ...
