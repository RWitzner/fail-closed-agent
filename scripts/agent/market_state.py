"""M2 §B — SessionState + halt/LULD/SSR + pure ``TradabilityDecider`` (contract §B).

The PURE DECIDER. ALL inputs are injected via ``TradabilityInputs`` (calendar phase,
vendor/broker status, NBBO, CA-blackout/freeze). NO clock, NO IO, NO float, NO network.
Deterministic: identical inputs => identical ``Verdict`` (mirrors ``book_state`` purity).

``SessionState`` (the resolved phase, incl. HALTED/UNKNOWN) and ``Tradability`` (the READ
M4.can_open / M5.execution_preflight consume) are TWO DISTINCT types so a caller can never
read "RTH" as permission to open. M2 PRODUCES the ``Tradability`` read; it never acts on it,
mints no preflight token, and opens nothing.

Closed vocabularies (frozenset membership; out-of-vocab -> FATAL/most-restrictive, mirrors
``event.TRADE_SIDES`` at event.py:33). Decimals only (the serializer rejects float, S2). M2
computes NO LULD percentage and NO SSR 10% trigger — it ingests the vendor band/flag and fails
closed on absence. Tighten-only accumulation via ``merge_severity`` (the verdict-level mirror of
``config.tighten_only_merge``'s never-loosen posture).
"""
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import FrozenSet, Optional, Tuple

from agent.market_calendar import SessionPhase


# --- closed vocabularies (frozenset membership; out-of-vocab -> FATAL/most-restrictive) ---
class SessionState(str, Enum):
    PRE = "pre"
    RTH = "rth"
    POST = "post"
    CLOSED = "closed"
    AUCTION = "auction"
    HALTED = "halted"
    UNKNOWN = "unknown"


SESSION_STATES: FrozenSet[str] = frozenset(s.value for s in SessionState)


class HaltState(str, Enum):
    NONE = "none"
    HALTED = "halted"  # any trading halt (news/regulatory/volatility code)
    PAUSED_LULD = "paused_luld"  # LULD 5-minute trading pause
    RESUMING = "resuming"  # halt clearing via a re-opening (indicative-price) auction (MR-5)
    UNKNOWN = "unknown"  # status feed unavailable/stale -> FAIL-CLOSED == HALTED-equivalent


class LuldState(str, Enum):
    NORMAL = "normal"  # inside the LULD band
    LIMIT = "limit"  # 5-minute limit state (NBBO pinned at a band edge)
    PAUSED = "paused"  # LULD trading pause triggered
    UNKNOWN = "unknown"  # band unknown/stale -> FAIL-CLOSED restrictive


class SsrState(str, Enum):
    INACTIVE = "inactive"
    ACTIVE = "active"  # Reg SHO Rule 201 in effect; rest-of-day + next trading day
    UNKNOWN = "unknown"  # FAIL-CLOSED: treat as ACTIVE for short-side decisions


class LuldTier(str, Enum):
    TIER1 = "tier1"  # S&P 500 / Russell 1000 / select ETPs
    TIER2 = "tier2"  # all other NMS securities
    UNKNOWN = "unknown"


class HaltReason(str, Enum):  # vendor/SIP-driven closed vocab; carried as provenance, not interpreted
    NONE = "none"
    NEWS_PENDING = "news_pending"
    NEWS_DISSEMINATION = "news_dissemination"
    LULD_PAUSE = "luld_pause"
    VOLATILITY = "volatility"
    REGULATORY = "regulatory"
    UNKNOWN = "unknown"


class Tradability(str, Enum):
    """The single tradability READ M4.can_open / M5.execution_preflight consume (spec §8.3-4).
    M2 only PRODUCES this; it never acts on it."""

    TRADABLE = "tradable"  # continuous two-sided trading permitted (caller's own gates still apply)
    REDUCE_ONLY = "reduce_only"  # may decrease an existing position only (never open/increase)
    NOT_TRADABLE = "not_tradable"  # blocked entirely


# severity rank: LARGER == MORE restrictive; tighten-only merge takes the MAX rank (NEVER min()-merged, §G)
_SEVERITY = {Tradability.TRADABLE: 0, Tradability.REDUCE_ONLY: 1, Tradability.NOT_TRADABLE: 2}
_BY_RANK = {rank: trad for trad, rank in _SEVERITY.items()}

# OPEN/CLOSE AVOIDANCE POLICY (MR-1): NOT a microstructure auction boundary. Continuous RTH owns
# the full [09:30:00,16:00:00) session. This is a CONSERVATIVE AGENT POLICY buffer, default OFF.
# DECIDED (Robin, 2026-06-09): stays 0/OFF in M2 — the decider models only market-state FACTS;
# open/close avoidance is a deliberate strategy/risk concern (M4/M7), not a hardcoded NOT_TRADABLE
# window here.
OPEN_CLOSE_BUFFER_S = 0  # 0 == OFF (M2 ships honest); a non-zero value is a deliberate policy knob


class MarketStateError(ValueError):
    """Decider invariant violation (out-of-vocabulary state) -> FATAL (fail-closed)."""


@dataclass(frozen=True)
class LuldBand:
    """Reg-NMS LULD price band. ALL Decimal (serializer rejects float). Bands are VENDOR/SIP-DRIVEN
    and tier/time dependent; M2 STORES the reported band, it does NOT compute the % (unknown band ->
    caller treats as most-restrictive). M2 consumes ONLY the vendor ``doubled`` flag and computes NO
    % and NO window (MR-4/MED-4)."""

    reference_px: Decimal
    lower_px: Decimal
    upper_px: Decimal
    tier: LuldTier
    doubled: bool


@dataclass(frozen=True)
class Nbbo:
    """Injected two-sided NBBO read (from M1 book_state best_bid/best_ask). REPLACES the M1-era
    boolean orderbook flag with a LIVE two-sided check. Prices Decimal (float-reject)."""

    symbol: str
    best_bid: Optional[Decimal]
    best_ask: Optional[Decimal]
    bid_sz: Optional[Decimal]
    ask_sz: Optional[Decimal]
    ts_utc: str

    @property
    def two_sided(self) -> bool:
        """True iff BOTH sides present with positive size AND not crossed/locked (best_bid < best_ask).
        Anything else -> False (fail-closed: one-sided or crossed/locked book is NOT continuously
        tradable)."""
        if self.best_bid is None or self.best_ask is None:
            return False
        if self.bid_sz is None or self.ask_sz is None:
            return False
        if self.bid_sz <= 0 or self.ask_sz <= 0:
            return False
        # not crossed and not locked: strictly bid < ask
        return self.best_bid < self.best_ask


@dataclass(frozen=True)
class StatusFlags:
    """Injected broker/vendor status read (Alpaca + calendar; EQUS.MINI has no status schema,
    status.py:31). Defaults are UNKNOWN == fail-closed. SSR is an INJECTED broker/SIP determination
    — M2 does NOT compute the 10% trigger (MR-3); prior_close is provenance context only."""

    symbol: str
    halt: HaltState = HaltState.UNKNOWN
    halt_reason: HaltReason = HaltReason.UNKNOWN
    luld: LuldState = LuldState.UNKNOWN
    luld_band: Optional[LuldBand] = None
    ssr: SsrState = SsrState.UNKNOWN
    prior_close: Optional[Decimal] = None  # SSR context ONLY (never used to compute SSR); Decimal-as-string
    source: str = "unknown"  # provenance: "alpaca" | "calendar" | "unknown"


@dataclass(frozen=True)
class TradabilityInputs:
    """The COMPLETE injected input set — the decider reads nothing else (pure). The BUILDER of this
    struct is responsible for catching calendar UnknownSessionDate and setting session_phase=UNKNOWN
    (never a tradable phase) — see §A.phase_at and the builder contract."""

    symbol: str
    instrument_id: int  # durable numeric identity (book_state identity discipline)
    ts_utc: str  # the instant being decided (ISO-8601 UTC)
    session_date_et: str  # ET session date for ts_utc (LOW-2): builder supplies via
    #   MarketCalendar.session_date_for(ts_utc) so decide() stays PURE (no calendar
    #   call inside the decider); flows straight into Verdict.session_date_et
    session_phase: SessionPhase  # from MarketCalendar.phase_at (UNKNOWN on a failed lookup)
    status: StatusFlags  # from broker/vendor (UNKNOWN fields fail-closed)
    nbbo: Optional[Nbbo]  # from M1 book_state (None == no live book == NOT_TRADABLE)
    ca_blackout: bool  # from corporate_actions (§D; True == symbol blacked out)
    frozen: bool = False  # from corporate_actions broker-adjust detector (§D); True == hard freeze


@dataclass(frozen=True)
class Verdict:
    """The frozen tradability verdict the cache stores and M4/M5 consume. Identity+economic only;
    wall-clock excluded from canonical_verdict_payload (§E). reasons is an ORDERED, SORTED tuple of
    machine-readable blocker codes."""

    symbol: str
    instrument_id: int
    session_state: SessionState
    tradability: Tradability
    halt: HaltState
    luld: LuldState
    ssr: SsrState
    two_sided_nbbo: bool  # replaces the legacy boolean orderbook flag
    short_allowed: bool  # False if SSR active/unknown OR tradability != TRADABLE (Reg SHO 201, MR-3)
    reasons: Tuple[str, ...]  # e.g. ("ca_blackout","session_auction","status_unknown")
    ca_blackout: bool
    session_date_et: str


def merge_severity(a: Tradability, b: Tradability) -> Tradability:
    """Tighten-only: return the MORE restrictive (higher _SEVERITY rank). Total order -> associative,
    order-independent. Mirrors config tighten_only_merge's never-loosen posture at the verdict level."""
    if a not in _SEVERITY or b not in _SEVERITY:
        raise MarketStateError(f"out-of-vocabulary Tradability in merge_severity: {a!r}, {b!r}")
    return _BY_RANK[max(_SEVERITY[a], _SEVERITY[b])]


class TradabilityDecider:
    """PURE class. NO IO, NO clock, NO float, NO network. Every input is INJECTED via
    ``TradabilityInputs``. Deterministic: identical inputs => identical ``Verdict`` (mirrors
    ``book_state`` purity). M2 computes NO LULD percentage and NO SSR 10% trigger — it ingests the
    vendor band/flag and fails closed on absence."""

    def decide(self, inputs: TradabilityInputs) -> Verdict:
        """Resolve by tighten-only accumulation (start TRADABLE, only tighten via merge_severity).

        The ordered steps 1..11 are the §B contract; an out-of-vocabulary enum reaching here ->
        ``MarketStateError`` (fail-closed, never coerced). The Verdict NEVER opens anything;
        restriction is DATA, not an exception.
        """
        status = inputs.status
        phase = inputs.session_phase

        # --- fail-closed vocabulary guard (out-of-vocab enum -> FATAL) ---
        if not isinstance(phase, SessionPhase) or phase.value not in _SESSION_PHASE_VALUES:
            raise MarketStateError(f"out-of-vocabulary session_phase: {phase!r}")
        if not isinstance(status.halt, HaltState):
            raise MarketStateError(f"out-of-vocabulary halt: {status.halt!r}")
        if not isinstance(status.luld, LuldState):
            raise MarketStateError(f"out-of-vocabulary luld: {status.luld!r}")
        if not isinstance(status.ssr, SsrState):
            raise MarketStateError(f"out-of-vocabulary ssr: {status.ssr!r}")

        tradability = Tradability.TRADABLE
        reasons: list = []
        # session_state is resolved alongside tradability; defaults track the calendar phase.
        session_state = _phase_to_session_state(phase)

        def tighten(to: Tradability, reason: Optional[str] = None) -> None:
            nonlocal tradability
            tradability = merge_severity(tradability, to)
            if reason is not None and reason not in reasons:
                reasons.append(reason)

        # 1. frozen -> NOT_TRADABLE, state HALTED, reason 'ca_frozen' (terminal).
        if inputs.frozen:
            tighten(Tradability.NOT_TRADABLE, "ca_frozen")
            return self._build_verdict(
                inputs, SessionState.HALTED, tradability, reasons
            )

        # 2. ca_blackout -> NOT_TRADABLE, reason 'ca_blackout'.
        if inputs.ca_blackout:
            tighten(Tradability.NOT_TRADABLE, "ca_blackout")

        # 3. session_phase UNKNOWN -> NOT_TRADABLE, state UNKNOWN, reason 'calendar_unknown'.
        if phase == SessionPhase.UNKNOWN:
            tighten(Tradability.NOT_TRADABLE, "calendar_unknown")
            return self._build_verdict(
                inputs, SessionState.UNKNOWN, tradability, reasons
            )

        # 4. session_phase CLOSED -> NOT_TRADABLE, state CLOSED.
        if phase == SessionPhase.CLOSED:
            tighten(Tradability.NOT_TRADABLE, "session_closed")
            return self._build_verdict(
                inputs, SessionState.CLOSED, tradability, reasons
            )

        # 5. halt / luld pause / unknown.
        if status.halt in (HaltState.HALTED, HaltState.PAUSED_LULD, HaltState.UNKNOWN):
            tighten(Tradability.NOT_TRADABLE, "halt")
            session_state = SessionState.HALTED
        elif status.halt == HaltState.RESUMING:
            # halt-resumption re-opening auction interlock; held NOT_TRADABLE until halt==NONE.
            # This is the ONLY producer of SessionState.AUCTION (calendar phase_at never emits it).
            tighten(Tradability.NOT_TRADABLE, "session_auction")
            session_state = SessionState.AUCTION
        if status.luld in (LuldState.PAUSED, LuldState.UNKNOWN):
            tighten(Tradability.NOT_TRADABLE, "luld_paused")
            session_state = SessionState.HALTED

        # If a halt/luld-pause has already forced HALTED/AUCTION + NOT_TRADABLE, stop here:
        # subsequent LULD-band / NBBO / phase steps would only restate the same restriction.
        if session_state in (SessionState.HALTED, SessionState.AUCTION):
            return self._build_verdict(inputs, session_state, tradability, reasons)

        # 5b. LULD-BAND-PRESENCE (S7-5b, fail-closed, HIGH-2): RTH + luld in {NORMAL,LIMIT} but no
        # band -> NOT_TRADABLE, state HALTED, reason 'luld_band_unknown'. An absent band during
        # continuous RTH is an incomplete/inconsistent vendor report and is MORE severe than a
        # known-at-edge band. LULD is an RTH mechanism, so PRE/POST with no band is NOT restricted
        # here (and UNKNOWN luld already routed via step 5).
        if (
            phase == SessionPhase.RTH
            and status.luld in (LuldState.NORMAL, LuldState.LIMIT)
            and status.luld_band is None
        ):
            tighten(Tradability.NOT_TRADABLE, "luld_band_unknown")
            return self._build_verdict(inputs, SessionState.HALTED, tradability, reasons)

        # 6. luld == LIMIT (band present) -> REDUCE_ONLY (allow exit, not open).
        if status.luld == LuldState.LIMIT:
            tighten(Tradability.REDUCE_ONLY, "luld_limit")

        # 7. LULD BAND CROSS-CHECK (S7-5): a live NBB/NBO price AT or THROUGH a band edge
        # (luld_band_check False) -> at least REDUCE_ONLY, reason 'luld_band_edge'.
        if inputs.nbbo is not None and status.luld_band is not None:
            for price in (inputs.nbbo.best_bid, inputs.nbbo.best_ask):
                if price is not None and not self.luld_band_check(
                    price=price, band=status.luld_band
                ):
                    tighten(Tradability.REDUCE_ONLY, "luld_band_edge")
                    break

        # 8. nbbo None OR not two_sided -> NOT_TRADABLE, reason 'no_two_sided_nbbo'.
        if inputs.nbbo is None or not inputs.nbbo.two_sided:
            tighten(Tradability.NOT_TRADABLE, "no_two_sided_nbbo")

        # 9. session_phase in {PRE, POST} -> REDUCE_ONLY (extended hours: conservative).
        if phase in (SessionPhase.PRE, SessionPhase.POST):
            tighten(Tradability.REDUCE_ONLY, "extended_hours")

        # 10. OPEN_CLOSE_BUFFER_S > 0 -> REDUCE_ONLY (POLICY; default OFF). Default OFF means this
        # branch is inert in M2; the decider is PURE (no clock) so the window check is not evaluated.
        if OPEN_CLOSE_BUFFER_S > 0:  # pragma: no cover - policy knob default OFF
            tighten(Tradability.REDUCE_ONLY, "open_close_buffer")

        # 11. else (RTH, two-sided, no halt/blackout/band-edge) -> TRADABLE (already the floor).
        return self._build_verdict(inputs, session_state, tradability, reasons)

    def _build_verdict(
        self,
        inputs: TradabilityInputs,
        session_state: SessionState,
        tradability: Tradability,
        reasons: list,
    ) -> Verdict:
        status = inputs.status
        two_sided = bool(inputs.nbbo is not None and inputs.nbbo.two_sided)
        # short_allowed = final tradability TRADABLE AND ssr INACTIVE (ACTIVE/UNKNOWN -> no short;
        # a CONSERVATIVE blanket block, stricter than Rule 201's at-or-below-NBB test — MR-3).
        short_allowed = tradability == Tradability.TRADABLE and status.ssr == SsrState.INACTIVE
        return Verdict(
            symbol=inputs.symbol,
            instrument_id=inputs.instrument_id,
            session_state=session_state,
            tradability=tradability,
            halt=status.halt,
            luld=status.luld,
            ssr=status.ssr,
            two_sided_nbbo=two_sided,
            short_allowed=short_allowed,
            reasons=tuple(sorted(reasons)),
            ca_blackout=bool(inputs.ca_blackout),
            session_date_et=inputs.session_date_et,
        )

    def luld_band_check(self, *, price: Decimal, band: Optional[LuldBand]) -> bool:
        """TRUE iff price is strictly inside (band.lower_px, band.upper_px). band is None (unknown) ->
        returns False (fail-closed: an unknown band is a band violation). Decimal-only. Wired into
        decide() step 7."""
        if band is None:
            return False
        return band.lower_px < price < band.upper_px


_SESSION_PHASE_VALUES: FrozenSet[str] = frozenset(p.value for p in SessionPhase)


def _phase_to_session_state(phase: SessionPhase) -> SessionState:
    """Map a calendar SessionPhase to the corresponding resolved SessionState (the default before
    halt/auction overrides). AUCTION is never produced by the calendar (§A) so it is not in this map;
    it is set only by the decider on HaltState.RESUMING."""
    return {
        SessionPhase.PRE: SessionState.PRE,
        SessionPhase.RTH: SessionState.RTH,
        SessionPhase.POST: SessionState.POST,
        SessionPhase.CLOSED: SessionState.CLOSED,
        SessionPhase.UNKNOWN: SessionState.UNKNOWN,
    }[phase]
