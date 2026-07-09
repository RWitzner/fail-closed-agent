"""M4 §I — RiskKillSwitch: trigger evaluation + latched state machine + journaling.

DELEGATES flattening to the M0 actuator `agent.kill_switch.KillSwitch` (FD-M4-4: a
FRESH M0 instance per flatten/retry pass; this module is the SOLE sanctioned importer
of agent.kill_switch — FD-M4-24). It can never emit an opening order: it references no
mint function and no token type; the only reduce-only minting site in the codebase
stays kill_switch.py:41.

One-way per run (FD-M4-19): MONITORING → FLATTENING → HALTED; HALTED latches across
rehydrate; no in-process re-arm API (re-arm = operator-attended NEW run over a journal
that shows the halt). Flatten attempts ALL positions regardless of M2 tradability
(FD-M4-20 — verdicts are annotation only, never a skip). The switch never fires on
unverified numbers (FD-M4-3: degraded account => skip, not trip) — but blindness is
BOUNDED: the orchestrator escalates > MAX_ACCOUNT_BLIND_MS of continuous non-fresh
account reads WITH open positions to `account_blind_cap` (flatten-then-halt), which
consumes no account numbers at all; unbounded blind exposure is the fail-open case.
"""
import threading
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Mapping, Optional, Tuple

from agent.kill_switch import KillSwitch
from agent.risk.account_state import AccountRead, PortfolioRead
from agent.risk.loss_limits import LossRead
from agent.risk.reasons import RiskError, require_kill_cause, require_kill_state
from agent.risk.risk_config import RiskConfig
from agent.risk.risk_ledger import EVT_KILL_TRANSITION, rehydrate_risk_state

_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)

# CODE CONSTANT (the FD-M4-22 posture: safety mechanics are not config-loosenable).
# Continuous non-fresh account reads beyond this, with an open position held, trip
# `account_blind_cap`: with can_open already rejecting on any non-fresh read, the only
# exposure a blind agent still manages is what it HOLDS — flat is strictly safer than
# blind-and-holding. 120s ≈ 48 refresh attempts at the 2.5s cadence, far past any
# transient API blip.
MAX_ACCOUNT_BLIND_MS = 120_000


@dataclass(frozen=True)
class KillEvaluation:
    cause: Optional[str]           # ∈ KILL_CAUSES or None
    skipped: bool                  # True iff account not fresh (no judgment possible)
    daily_loss_usd: Optional[Decimal]
    drawdown_usd: Optional[Decimal]


@dataclass(frozen=True)
class FlattenReport:
    cause: str
    generation: int
    flattened: Tuple[str, ...]                 # symbols successfully submitted
    failed: Tuple[Tuple[str, str], ...]        # (symbol, reason) — M0 failed[] verbatim
    residual: Tuple[str, ...]                  # sorted symbols still presumed held


class RiskKillSwitch:
    def __init__(self, *, cfg: RiskConfig, ledger=None) -> None:
        self._cfg = cfg
        self._ledger = ledger
        self._lock = threading.Lock()
        self._state = "monitoring"
        self._generation = 0
        self._residual: Tuple[str, ...] = ()
        self._cause: Optional[str] = None
        self._last_report: Optional[FlattenReport] = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def generation(self) -> int:
        return self._generation

    # --- evaluation (pure) ------------------------------------------------------

    def evaluate(self, account: AccountRead, loss: LossRead) -> KillEvaluation:
        """PURE. account not fresh => skipped (the switch NEVER fires on unverified
        numbers — FD-M4-3; the caller journals the edge-triggered kill_eval_skipped
        row). Fresh: daily-loss then drawdown, strict '>' vs cfg caps; first breach
        wins ("daily_loss_cap" before "drawdown_cap")."""
        if account.status != "fresh":
            return KillEvaluation(cause=None, skipped=True, daily_loss_usd=None,
                                  drawdown_usd=None)
        read = account.read
        with localcontext(_DECIMAL_CTX):
            daily_loss = Decimal(read.last_equity) - Decimal(read.equity)
            drawdown = (Decimal(loss.hwm_equity) - Decimal(read.equity)
                        if loss.hwm_equity is not None else None)
        cause = None
        if daily_loss > self._cfg.max_daily_loss_usd:
            cause = "daily_loss_cap"
        elif drawdown is not None and drawdown > self._cfg.max_drawdown_usd:
            cause = "drawdown_cap"
        return KillEvaluation(cause=cause, skipped=False, daily_loss_usd=daily_loss,
                              drawdown_usd=drawdown)

    # --- trigger / retry -----------------------------------------------------------

    def trigger(self, cause: str, broker, portfolio: PortfolioRead, *,
                evaluation: Optional[KillEvaluation] = None,
                account: Optional[AccountRead] = None,
                tradability: Mapping = {}) -> FlattenReport:
        """Keyword-only ANNOTATION inputs (M4C-1/safety-F4): row fields derived from
        absent inputs are null; staleness never blocks the flatten — it is journaled
        (`stale_inputs`). M2 verdicts are annotation only, NEVER a skip (FD-M4-20)."""
        require_kill_cause(cause)
        with self._lock:
            if self._state != "monitoring":
                if self._ledger is not None:
                    self._ledger.record_kill_retrip(cause=cause,
                                                    generation=self._generation,
                                                    current_state=self._state)
                if self._last_report is not None:
                    return self._last_report
                # after a rehydrate with no in-process prior report (safety-F5):
                return FlattenReport(cause=cause, generation=self._generation,
                                     flattened=(), failed=(), residual=self._residual)

            self._generation += 1
            self._cause = cause
            at_trigger = tuple(sorted(p.symbol for p in portfolio.positions))
            self._residual = at_trigger

            daily_loss = evaluation.daily_loss_usd if evaluation is not None else None
            drawdown = evaluation.drawdown_usd if evaluation is not None else None
            cap_usd = None
            if evaluation is not None:
                if cause == "daily_loss_cap":
                    cap_usd = self._cfg.max_daily_loss_usd
                elif cause == "drawdown_cap":
                    cap_usd = self._cfg.max_drawdown_usd
            snapshot_id = (account.read.account_snapshot_id
                           if account is not None and account.read is not None else None)
            stale_inputs = account is None or account.status != "fresh"

            self._state = "flattening"
            if self._ledger is not None:
                # this row carries residual[] = ALL at-trigger symbols with
                # flattened[]/failed[] empty — the crash-mid-flatten rehydrate
                # source (safety-F5).
                self._ledger.record_kill_transition(
                    from_state="monitoring", to_state="flattening", cause=cause,
                    generation=self._generation, daily_loss_usd=daily_loss,
                    drawdown_usd=drawdown, cap_usd=cap_usd,
                    account_snapshot_id=snapshot_id, stale_inputs=stale_inputs,
                    flattened=(), failed=(), residual=at_trigger,
                    tradability_annotations=())

            inner = KillSwitch()   # FRESH M0 actuator per pass (FD-M4-4)
            report: Optional[FlattenReport] = None
            try:
                inner.trigger(broker, portfolio.positions)
            finally:
                self._state = "halted"   # ALWAYS halts (S8)
                flattened = tuple(sorted(inner.flattened))
                failed = tuple(sorted((symbol, reason)
                                      for symbol, reason in inner.failed))
                residual = tuple(sorted(set(at_trigger) - set(flattened)))
                self._residual = residual
                annotations = tuple(
                    sorted((symbol, verdict.tradability.value)
                           for symbol, verdict in tradability.items()))
                if self._ledger is not None:
                    self._ledger.record_kill_transition(
                        from_state="flattening", to_state="halted", cause=cause,
                        generation=self._generation, daily_loss_usd=daily_loss,
                        drawdown_usd=drawdown, cap_usd=cap_usd,
                        account_snapshot_id=snapshot_id, stale_inputs=stale_inputs,
                        flattened=flattened, failed=failed, residual=residual,
                        tradability_annotations=annotations)
                    if failed or residual:
                        # exposure remains broker-held; runbook item — never
                        # silently "done".
                        self._ledger.record_kill_flatten_incomplete(
                            generation=self._generation, failed=failed,
                            residual=residual)
                report = FlattenReport(cause=cause, generation=self._generation,
                                       flattened=flattened, failed=failed,
                                       residual=residual)
                self._last_report = report
            return report

    def retry_residual(self, broker, portfolio: PortfolioRead) -> FlattenReport:
        """Legal only in HALTED with residual ≠ ∅; reduce-only by construction (a
        fresh M0 pass over the residual positions); state stays HALTED."""
        with self._lock:
            if self._state != "halted" or not self._residual:
                raise RiskError(
                    "retry_residual is legal only in HALTED with a non-empty residual "
                    f"(state={self._state!r}, residual={self._residual!r})")
            residual_before = self._residual
            keep = set(residual_before)
            subset = tuple(p for p in portfolio.positions if p.symbol in keep)
            inner = KillSwitch()   # FRESH M0 actuator (FD-M4-4)
            try:
                inner.trigger(broker, subset)
            finally:
                flattened = tuple(sorted(inner.flattened))
                failed = tuple(sorted((symbol, reason)
                                      for symbol, reason in inner.failed))
                residual_after = tuple(sorted(keep - set(flattened)))
                self._residual = residual_after
                self._state = "halted"   # stays halted; never re-arms (FD-M4-19)
                if self._ledger is not None:
                    self._ledger.record_kill_retry_residual(
                        generation=self._generation,
                        residual_before=residual_before,
                        residual_after=residual_after,
                        flattened=flattened, failed=failed)
                report = FlattenReport(cause=self._cause or "",
                                       generation=self._generation,
                                       flattened=flattened, failed=failed,
                                       residual=residual_after)
                self._last_report = report
            return report

    def residual_symbols(self) -> Tuple[str, ...]:
        return self._residual

    # --- rehydrate ---------------------------------------------------------------

    def rehydrate(self, rows) -> None:
        """HALTED + generation + residual re-latch (FD-M4-19). A journal ending
        mid-flatten (trailing monitoring→flattening row — the in-process `finally`
        cannot survive a crash) rehydrates to HALTED with residual rebuilt from that
        row and journals a kill_flatten_incomplete marker (safety-F5)."""
        with self._lock:
            slice_ = rehydrate_risk_state(rows)["kill"]
            self._state = require_kill_state(slice_["state"])
            self._generation = int(slice_["generation"])
            self._residual = tuple(sorted(slice_["residual"]))
            self._last_report = None
            last_transition = None
            for row in sorted(rows, key=lambda r: r["seq"]):
                if row.get("event_type") == EVT_KILL_TRANSITION:
                    last_transition = row
            self._cause = (last_transition.get("cause")
                           if last_transition is not None else None)
            # harden round 1, M4-R1-F2: the marker means "exposure remains broker-held".
            # Guard on the REBUILT residual (which already folds kill_retry_residual
            # rows) — after a fully-successful retry the flatten IS complete and a
            # second rehydrate must not journal a spurious empty-residual marker.
            if (last_transition is not None
                    and last_transition["to_state"] == "flattening"
                    and self._residual):
                if self._ledger is not None:
                    self._ledger.record_kill_flatten_incomplete(
                        generation=self._generation, failed=(),
                        residual=self._residual)
