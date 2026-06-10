"""M4 §J — RiskEngine.can_open: the single pre-trade chokepoint (parent §5 Tier 5).

PURE (LD5): no I/O, no clock read, no fetch, no journal write — every collaborator
read arrives as an input; `now_ms` touches ONLY mark freshness. NEVER raises on a
rejectable condition (restriction is DATA — the M2 decider posture); raises RiskError
only on invariant breaks. NEVER consulted on the reduce path (FD-M4-3). The CALLER
journals every verdict via `RiskLedger.record_risk_verdict` immediately after the
call (FD-M4-5).

Ladder (§2.2, frozen): phase 1 terminal short-circuits
`run_gates → kill → margin_freeze → account → portfolio` (stop at first hit, later
stages in `stages_skipped`); phase 2 collect-ALL
`candidate → universe → market_state → short → caps → margin → pdt → loss`
(sorted, deduped union; `allowed ⟺ reasons == ()` — no "allow with warnings").
Rung 1 is the literal M0 gate function, so on the committed config EVERY verdict
terminates at `run_gates` with the frozen LD-R1 terminal shape.
"""
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Mapping, Optional, Tuple

from agent.candidate import Candidate
from agent.gates import opening_allowed
from agent.risk.account_state import AccountRead, Mark, PortfolioRead
from agent.risk.exposure import leg_cap_notional, project_caps
from agent.risk.intraday_margin import MarginRead, classify_iml_reducing
from agent.risk.loss_limits import LossRead
from agent.risk.pdt_compat import PdtRead
from agent.risk.reasons import (
    GATE_STAGES,
    RiskError,
    require_account_status,
    require_classification,
    require_kill_state,
    require_pdt_state,
    require_reason,
)
from agent.risk.risk_config import INTRADAY_MARGIN_BUFFER_USD, RiskConfig
from agent.serializer import row_hash

_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)

_ACCOUNT_REASONS = {
    "missing": "account_missing",
    "stale": "account_stale",
    "invalid": "account_invalid",
    "skew": "account_clock_skew",
}
_OPENING_CLASSIFICATIONS = frozenset({"opening_long", "short_or_flip"})


@dataclass(frozen=True)
class LegRead:
    """Per-leg provenance carried on the verdict + journal row."""

    symbol: str
    instrument_id: int
    side: str
    classification: str            # ∈ LEG_CLASSIFICATIONS (the frozen §A mapping — LD-R1)
    qty: Decimal
    limit_price: Optional[Decimal]
    cap_notional: Optional[Decimal]   # None iff unpriceable (or reducing — no notional)
    mark_used: bool


@dataclass(frozen=True)
class RiskVerdict:
    """Frozen field set. Named RiskVerdict (not Verdict) to avoid colliding with
    market_state.Verdict."""

    allowed: bool                       # INVARIANT: allowed is True <=> reasons == ()
    reasons: Tuple[str, ...]            # sorted, deduped, ⊆ RISK_REASONS
    gate_stage: Optional[str]           # the terminal stage name, or None (phase 2)
    stages_skipped: Tuple[str, ...]     # frozen GATE_STAGES members not evaluated
    strategy_id: str
    legs: Tuple[LegRead, ...]           # () on phase-1 terminal verdicts (LD-R1)
    gross_notional: Decimal             # Σ cap_notional over priceable OPENING legs
    caps_used: Tuple[Tuple[str, str, str], ...]   # §D format; () when not reached
    account_snapshot_id: Optional[str]  # None iff account missing/invalid
    kill_state: str                     # consumed value (TOCTOU provenance)
    kill_generation: int
    session_date_et: Optional[str]      # from the leg verdicts; None if not reached
    rules_hash: str
    verdict_id: str                     # §K.3 deterministic id


class RiskEngine:
    def __init__(self, *, cfg: RiskConfig, gates_config: dict, run_id: str) -> None:
        # gates_config = the assembled committed(+overlay) dict fed to opening_allowed.
        self._cfg = cfg
        self._gates_config = gates_config
        self._run_id = run_id

    # --- the chokepoint -----------------------------------------------------------

    def can_open(
        self,
        candidate: Candidate,
        portfolio: Optional[PortfolioRead],
        account: AccountRead,
        *,
        market_state: Mapping,                               # leg symbol -> M2 Verdict
        marks: Mapping[str, Mark] = {},                      # optional tighteners
        kill_state: str,                                     # RiskKillSwitch.state
        kill_generation: int,
        margin_read: MarginRead,                             # IntradayMarginModel.read(...)
        pdt_read: PdtRead,                                   # LegacyPdtCompatMode.read()
        loss_read: LossRead,                                 # LossLimitsMonitor.read()
        now_ms: int,                                         # used ONLY for mark freshness
        decision_id: Optional[str] = None,                   # rides into verdict_id (S6)
    ) -> RiskVerdict:
        if not isinstance(candidate, Candidate):
            raise RiskError(
                f"can_open requires a Candidate, got {type(candidate).__name__}")
        if account is None:
            raise RiskError("can_open requires an AccountRead (account is None)")
        require_kill_state(kill_state)
        require_pdt_state(pdt_read.state)
        snapshot_id = (account.read.account_snapshot_id
                       if account.read is not None else None)

        # --- phase 1: terminal short-circuits (frozen order) ----------------------
        terminal = None
        if not opening_allowed(self._gates_config):
            terminal = ("run_gates", ("run_gates_off",))
        elif kill_state != "monitoring":
            terminal = ("kill", ("kill_switch_halted",))
        elif margin_read.freeze.active_on(margin_read.asof_session_date_et):
            terminal = ("margin_freeze", ("margin_freeze_active",))
        elif require_account_status(account.status) != "fresh":
            terminal = ("account", (_ACCOUNT_REASONS[account.status],))
        else:
            portfolio_reasons = []
            if portfolio is None:
                portfolio_reasons.append("portfolio_missing")
            else:
                if portfolio.stale is True:
                    portfolio_reasons.append("portfolio_stale")
                if portfolio.unreconciled_drift is True:
                    portfolio_reasons.append("portfolio_unreconciled")
            if portfolio_reasons:
                terminal = ("portfolio", tuple(sorted(portfolio_reasons)))

        if terminal is not None:
            stage, reasons = terminal
            for code in reasons:
                require_reason(code)
            stage_index = GATE_STAGES.index(stage)
            return self._finish(
                candidate=candidate, allowed=False, reasons=reasons,
                gate_stage=stage, stages_skipped=GATE_STAGES[stage_index + 1:],
                legs=(), gross_notional=Decimal("0"), caps_used=(),
                account_snapshot_id=snapshot_id, kill_state=kill_state,
                kill_generation=kill_generation, session_date_et=None,
                decision_id=decision_id)

        # --- phase 2: collect-ALL accumulation (frozen order, no short-circuit) ----
        classifications = self._classify(candidate, portfolio)
        reasons = set()
        reasons |= self._stage_candidate(candidate, portfolio, classifications)
        reasons |= self._stage_universe(candidate)
        market_reasons, session_date_et = self._stage_market_state(
            candidate, market_state, margin_read)
        reasons |= market_reasons
        reasons |= self._stage_short()

        notionals, leg_reads, gross_notional = self._price_legs(
            candidate, classifications, marks, now_ms)
        caps_rows: list = []
        stages_skipped: Tuple[str, ...] = ()
        if notionals:
            # Poisoning scans ALL opening legs, priceable or not (harden M4-R3).
            opening_indexes = tuple(
                index for index, classification in enumerate(classifications)
                if classification in _OPENING_CLASSIFICATIONS)
            cap_reasons, rows = project_caps(candidate, notionals, portfolio,
                                             self._cfg,
                                             opening_indexes=opening_indexes)
            reasons |= set(cap_reasons)
            caps_rows.extend(rows)
            margin_reasons, margin_rows = self._stage_margin(
                gross_notional, account, margin_read)
            reasons |= margin_reasons
            caps_rows.extend(margin_rows)
        else:
            # skipped iff every opening leg is unpriceable (§2.2 stages 10-11)
            stages_skipped = ("caps", "margin")
        pdt_reasons, pdt_rows = self._stage_pdt(
            pdt_read, gross_notional if notionals else None, account)
        reasons |= pdt_reasons
        caps_rows.extend(pdt_rows)
        loss_reasons, loss_rows = self._stage_loss(account, loss_read)
        reasons |= loss_reasons
        caps_rows.extend(loss_rows)

        for code in reasons:
            require_reason(code)   # R7: out-of-vocab => RiskError, never coerced
        reasons_tuple = tuple(sorted(reasons))
        return self._finish(
            candidate=candidate, allowed=(reasons_tuple == ()),
            reasons=reasons_tuple, gate_stage=None, stages_skipped=stages_skipped,
            legs=leg_reads, gross_notional=gross_notional,
            caps_used=tuple(sorted(caps_rows)),   # full union sorted by name ONCE
            account_snapshot_id=snapshot_id, kill_state=kill_state,
            kill_generation=kill_generation, session_date_et=session_date_et,
            decision_id=decision_id)

    # --- stages (named methods so the R7 vocab-guard test can patch them) ----------

    @staticmethod
    def _classify(candidate: Candidate, portfolio: PortfolioRead) -> Tuple[str, ...]:
        """The frozen §A total mapping (LD-R1), validated against the HELD position."""
        out = []
        for leg in candidate.legs:
            if leg.side not in ("buy", "sell"):
                raise RiskError(f"out-of-vocabulary leg side: {leg.side!r}")
            held = portfolio.qty_for(leg.symbol)
            if not classify_iml_reducing(leg.side, leg.qty, held):
                classification = "reducing"
            elif leg.side == "buy" and held >= 0:
                classification = "opening_long"
            else:
                classification = "short_or_flip"
            out.append(require_classification(classification))
        return tuple(out)

    def _stage_candidate(self, candidate, portfolio, classifications) -> set:
        out = set()
        if candidate.paper_eligible is not True:   # identity — the S9 placeholder wall
            out.add("strategy_not_paper_eligible")
        for leg, classification in zip(candidate.legs, classifications):
            held = portfolio.qty_for(leg.symbol)
            if leg.side == "buy":
                if held < 0:
                    # cover OR flip — wrong chokepoint either way (§2.2 stage 6)
                    out.add("reduce_path_not_can_open")
            else:
                if held > 0 and leg.qty <= held:
                    out.add("reduce_path_not_can_open")   # close — wrong chokepoint
                else:
                    out.add("short_side_disabled")        # FD-M4-1, structural
            if classification in _OPENING_CLASSIFICATIONS and (
                    leg.limit_price is None or leg.limit_price <= 0):
                out.add("unpriceable_candidate")          # FD-M4-16/RM-3
        return out

    def _stage_universe(self, candidate) -> set:
        out = set()
        for leg in candidate.legs:
            if leg.symbol not in self._cfg.universe:
                out.add("universe_excluded")   # committed {} => always fires
        return out

    def _stage_market_state(self, candidate, market_state, margin_read):
        out = set()
        session_date_et: Optional[str] = None
        for leg in candidate.legs:
            verdict = market_state.get(leg.symbol)
            if verdict is None:
                out.add("market_state_missing")
                continue
            if verdict.symbol != leg.symbol or verdict.instrument_id != leg.instrument_id:
                raise RiskError(
                    f"market-state verdict identity mismatch for {leg.symbol!r}: "
                    f"({verdict.symbol!r}, {verdict.instrument_id}) vs "
                    f"({leg.symbol!r}, {leg.instrument_id})")
            if session_date_et is None:
                session_date_et = verdict.session_date_et
            elif verdict.session_date_et != session_date_et:
                raise RiskError(
                    "cross-leg session_date_et disagreement: "
                    f"{verdict.session_date_et!r} vs {session_date_et!r}")
            if verdict.session_date_et != margin_read.asof_session_date_et:
                # stale collaborator wiring is a BUG, not data (safety-F6/RM-12)
                raise RiskError(
                    f"leg verdict session_date_et {verdict.session_date_et!r} != "
                    f"margin_read.asof_session_date_et "
                    f"{margin_read.asof_session_date_et!r}")
            if verdict.tradability.value != "tradable":
                out.add("market_state_not_tradable")   # REDUCE_ONLY blocks opens too
            if "cache_stale_safe_default" in verdict.reasons:
                out.add("market_state_stale_default")
        return out, session_date_et

    def _stage_short(self) -> set:
        # Structural slot for the short-side milestone (FD-M4-1): every
        # short-establishing leg was already rejected at stage 6; the deny-all seam
        # is NOT called here. Reserved reasons attach here in a later milestone.
        return set()

    def _price_legs(self, candidate, classifications, marks, now_ms):
        for key, mark in marks.items():
            if mark.symbol != key:
                raise RiskError(
                    f"marks entry key/identity mismatch: key {key!r} carries "
                    f"symbol {mark.symbol!r}")
        notionals = {}
        leg_reads = []
        with localcontext(_DECIMAL_CTX):
            gross = Decimal("0")
            for index, (leg, classification) in enumerate(
                    zip(candidate.legs, classifications)):
                cap_notional = None
                mark_used = False
                if classification in _OPENING_CLASSIFICATIONS:
                    cap_notional, mark_used = leg_cap_notional(
                        leg, marks.get(leg.symbol), now_ms=now_ms)
                    if cap_notional is not None:
                        notionals[index] = cap_notional
                        gross += cap_notional
                leg_reads.append(LegRead(
                    symbol=leg.symbol, instrument_id=leg.instrument_id,
                    side=leg.side, classification=classification, qty=leg.qty,
                    limit_price=leg.limit_price, cap_notional=cap_notional,
                    mark_used=mark_used))
        return notionals, tuple(leg_reads), gross

    def _stage_margin(self, gross_notional, account, margin_read):
        out = set()
        rows = []
        with localcontext(_DECIMAL_CTX):
            buying_power = Decimal(account.read.buying_power)
            rows.append(("buying_power", str(gross_notional), str(buying_power)))
            if gross_notional > buying_power - INTRADAY_MARGIN_BUFFER_USD:
                out.add("intraday_margin_insufficient")
        if margin_read.outstanding_nonminor:
            # deliberately stricter than the rule's bd5 clock (tighten-only)
            out.add("intraday_margin_deficit_outstanding")
        return out, rows

    def _stage_pdt(self, pdt_read, gross_notional, account):
        out = set()
        rows = []
        if pdt_read.rejection_latched is True:
            out.add("pdt_compat_blocked")
        dtbp = account.read.daytrading_buying_power
        if (pdt_read.state == "enforcing_legacy_pdt" and dtbp is not None
                and gross_notional is not None):
            rows.append(("daytrading_buying_power", str(gross_notional), str(dtbp)))
            if gross_notional > dtbp:
                out.add("pdt_compat_dtbp_exceeded")
        return out, rows

    def _stage_loss(self, account, loss_read):
        out = set()
        rows = []
        read = account.read
        with localcontext(_DECIMAL_CTX):
            daily_loss = Decimal(read.last_equity) - Decimal(read.equity)
            rows.append(("max_daily_loss_usd", str(daily_loss),
                         str(self._cfg.max_daily_loss_usd)))
            if daily_loss > self._cfg.max_daily_loss_usd:
                out.add("daily_loss_breached")
            if loss_read.hwm_equity is None:
                out.add("loss_baseline_unavailable")   # fail-closed baseline
            else:
                drawdown = Decimal(loss_read.hwm_equity) - Decimal(read.equity)
                rows.append(("max_drawdown_usd", str(drawdown),
                             str(self._cfg.max_drawdown_usd)))
                if drawdown > self._cfg.max_drawdown_usd:
                    out.add("drawdown_breached")
        return out, rows

    # --- verdict assembly -----------------------------------------------------------

    def _finish(self, *, candidate, allowed, reasons, gate_stage, stages_skipped,
                legs, gross_notional, caps_used, account_snapshot_id, kill_state,
                kill_generation, session_date_et, decision_id) -> RiskVerdict:
        verdict_id = "rv-" + row_hash({
            "run_id": self._run_id,
            "strategy_id": candidate.strategy_id,
            "symbols": sorted(leg.symbol for leg in candidate.legs),
            "gross_notional": gross_notional,
            "reasons": list(reasons),
            "gate_stage": gate_stage,
            "account_snapshot_id": account_snapshot_id,
            "rules_hash": self._cfg.rules_hash,
            "decision_id": decision_id,
        })
        return RiskVerdict(
            allowed=allowed, reasons=reasons, gate_stage=gate_stage,
            stages_skipped=tuple(stages_skipped), strategy_id=candidate.strategy_id,
            legs=legs, gross_notional=gross_notional, caps_used=caps_used,
            account_snapshot_id=account_snapshot_id, kill_state=kill_state,
            kill_generation=int(kill_generation), session_date_et=session_date_et,
            rules_hash=self._cfg.rules_hash, verdict_id=verdict_id)
