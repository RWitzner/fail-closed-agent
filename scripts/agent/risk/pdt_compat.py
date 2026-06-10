"""M4 §F — LegacyPdtCompatMode: mirror what the broker ACTUALLY enforces during the
26-10 phase-in (V10, until 2027-10-20) — detected, never assumed, never re-derived
(FD-M4-2/15). Tighten-only by construction: this module can only ADD reasons; nothing
here can mark a candidate allowed or suppress another gate's reason.

The rejection latch is DURABLE across runs (LD-R3): it survives rehydrate via the
`rehydrated_state` ctor arg and clears only by deliberate operator action (a fresh /
rotated journal after review), never by data. `pattern_day_trader is None` => UNKNOWN
— journaled context, NOT a reject by itself (FD-M4-15). `daytrade_count` is carried as
journal provenance only.
"""
from dataclasses import dataclass
from typing import Mapping, Optional

from agent.risk.account_state import BrokerAccountRead
from agent.risk.reasons import require_pdt_state

PDT_REJECTION_CODES = frozenset({40310100})   # Alpaca's documented PDT-protection code
PDT_REJECTION_MARKERS = ("pattern day trad", "day trading buying power", "day-trade")
                                              # lowercase substring match (frozen)


@dataclass(frozen=True)
class PdtRead:
    state: str                  # ∈ PDT_STATES (reasons.py)
    evidence: Optional[str]     # ∈ {"account_flag", "broker_rejection"} when enforcing
    rejection_latched: bool     # True once any rejection evidence seen this run

    def __post_init__(self):
        require_pdt_state(self.state)


@dataclass(frozen=True)
class BrokerRejectionObservation:
    """Frozen NOW so detection is fixture-testable; M5's order path produces these."""

    code: Optional[int]
    message: str
    ts_utc: str


def _is_pdt_rejection(obs: BrokerRejectionObservation) -> bool:
    """Pure, total — never raises on weird payloads."""
    try:
        if obs.code is not None and obs.code in PDT_REJECTION_CODES:
            return True
        message = str(obs.message or "").lower()
        return any(marker in message for marker in PDT_REJECTION_MARKERS)
    except Exception:  # noqa: BLE001 — detection must be total (FD-M4-15)
        return False


class LegacyPdtCompatMode:
    def __init__(self, *, ledger=None,
                 rehydrated_state: Optional[Mapping] = None) -> None:
        """`rehydrated_state` = rehydrate_risk_state(rows)["pdt"] (LD-R3): a rehydrated
        rejection_latched=True RE-LATCHES enforcing_legacy_pdt — only the latch is
        durable (an account-flag state is re-derived on the next observe_account).
        M5's orchestrator MUST seed this (§O)."""
        self._ledger = ledger
        self._state = "unknown"
        self._evidence: Optional[str] = None
        self._latched = False
        self._pattern_day_trader: Optional[bool] = None
        self._daytrade_count: Optional[int] = None
        if rehydrated_state is not None and rehydrated_state.get("rejection_latched") is True:
            self._latched = True
            self._transition("enforcing_legacy_pdt", evidence="broker_rejection",
                             rejection_code=None)

    def _transition(self, to_state: str, *, evidence: str,
                    rejection_code: Optional[int] = None) -> None:
        from_state = self._state
        self._state = require_pdt_state(to_state)
        self._evidence = evidence if to_state == "enforcing_legacy_pdt" else None
        if self._ledger is not None:
            self._ledger.record_pdt_transition(
                from_state=from_state,
                to_state=to_state,
                evidence=evidence,
                rejection_code=rejection_code,
                pattern_day_trader=self._pattern_day_trader,
                daytrade_count=self._daytrade_count)

    def observe_account(self, read: BrokerAccountRead) -> PdtRead:
        self._pattern_day_trader = read.pattern_day_trader
        self._daytrade_count = read.daytrade_count
        if self._latched:
            return self.read()  # the latch is never undone by a flag read
        flag = read.pattern_day_trader
        if flag is True:
            target = "enforcing_legacy_pdt"
        elif flag is False:
            target = "not_enforcing"
        else:
            target = "unknown"
        if target != self._state:
            self._transition(target, evidence="account_flag")
        return self.read()

    def observe_broker_rejection(self, obs: BrokerRejectionObservation) -> PdtRead:
        if _is_pdt_rejection(obs) and not self._latched:
            self._latched = True
            self._transition("enforcing_legacy_pdt", evidence="broker_rejection",
                             rejection_code=obs.code)
        return self.read()

    def read(self) -> PdtRead:
        evidence = self._evidence
        if self._latched:
            evidence = "broker_rejection"
        return PdtRead(state=self._state, evidence=evidence,
                       rejection_latched=self._latched)
