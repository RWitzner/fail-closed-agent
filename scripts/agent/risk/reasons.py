"""M4 §A — closed vocabularies for the risk core. Out-of-vocab anywhere -> RiskError
(FATAL, fail-closed, never coerced) — mirrors MarketStateError (market_state.py:98).

`RISK_REASONS` has exactly 34 members (10 terminal + 22 accumulated + 2 reserved);
emitting a RESERVED_REASONS member in M4 is a test failure (FD-M4-23). Stage names
are journaled as `gate_stage` / `stages_skipped` members and are FROZEN (§2.2).
"""
from typing import FrozenSet, Tuple


class RiskError(ValueError):
    """Risk-core invariant violation (out-of-vocab reason/state/cause, identity mismatch,
    malformed collaborator input) -> FATAL. Restriction is DATA; RiskError is for bugs."""


TERMINAL_REASONS: FrozenSet[str] = frozenset({
    "run_gates_off", "kill_switch_halted", "margin_freeze_active",
    "account_missing", "account_stale", "account_invalid", "account_clock_skew",
    "portfolio_missing", "portfolio_stale", "portfolio_unreconciled",
})
ACCUMULATED_REASONS: FrozenSet[str] = frozenset({
    "strategy_not_paper_eligible", "reduce_path_not_can_open", "short_side_disabled",
    "unpriceable_candidate", "universe_excluded",
    "market_state_missing", "market_state_not_tradable", "market_state_stale_default",
    "position_cap_exceeded", "gross_exposure_cap_exceeded", "net_exposure_cap_exceeded",
    "sector_cap_exceeded", "sector_unknown", "beta_cap_exceeded", "beta_unknown",
    "intraday_margin_insufficient", "intraday_margin_deficit_outstanding",
    "pdt_compat_blocked", "pdt_compat_dtbp_exceeded",
    "daily_loss_breached", "drawdown_breached", "loss_baseline_unavailable",
})
RESERVED_REASONS: FrozenSet[str] = frozenset({"locate_unavailable", "ssr_short_blocked"})
RISK_REASONS: FrozenSet[str] = TERMINAL_REASONS | ACCUMULATED_REASONS | RESERVED_REASONS

GATE_STAGES: Tuple[str, ...] = (
    "run_gates", "kill", "margin_freeze", "account", "portfolio",
    "candidate", "universe", "market_state", "short", "caps", "margin", "pdt", "loss",
)

KILL_STATES: FrozenSet[str] = frozenset({"monitoring", "flattening", "halted"})
KILL_CAUSES: FrozenSet[str] = frozenset({"daily_loss_cap", "drawdown_cap", "operator_manual", "drill"})
RESERVED_KILL_CAUSES: FrozenSet[str] = frozenset({"live_gate_flip"})   # M8 (FD-M4-23)

ACCOUNT_STATUSES: FrozenSet[str] = frozenset({"fresh", "stale", "missing", "invalid", "skew"})
PDT_STATES: FrozenSet[str] = frozenset({"unknown", "not_enforcing", "enforcing_legacy_pdt"})
LEG_CLASSIFICATIONS: FrozenSet[str] = frozenset({"opening_long", "short_or_flip", "reducing"})


def require_reason(code: str) -> str:
    """Validate an ABOUT-TO-BE-EMITTED reason: must be in RISK_REASONS and must NOT be
    reserved (FD-M4-23: reserved codes exist for later milestones, never emitted in M4)."""
    if code not in RISK_REASONS:
        raise RiskError(f"out-of-vocabulary risk reason: {code!r}")
    if code in RESERVED_REASONS:
        raise RiskError(f"reserved risk reason must not be emitted in M4: {code!r}")
    return code


def require_stage(name: str) -> str:
    if name not in GATE_STAGES:
        raise RiskError(f"out-of-vocabulary gate stage: {name!r}")
    return name


def require_kill_state(state: str) -> str:
    if state not in KILL_STATES:
        raise RiskError(f"out-of-vocabulary kill state: {state!r}")
    return state


def require_kill_cause(cause: str) -> str:
    """Reserved causes (live_gate_flip, M8) are refused exactly like unknown ones."""
    if cause in RESERVED_KILL_CAUSES:
        raise RiskError(f"reserved kill cause must not be emitted in M4: {cause!r}")
    if cause not in KILL_CAUSES:
        raise RiskError(f"out-of-vocabulary kill cause: {cause!r}")
    return cause


def require_account_status(status: str) -> str:
    if status not in ACCOUNT_STATUSES:
        raise RiskError(f"out-of-vocabulary account status: {status!r}")
    return status


def require_pdt_state(state: str) -> str:
    if state not in PDT_STATES:
        raise RiskError(f"out-of-vocabulary pdt state: {state!r}")
    return state


def require_classification(classification: str) -> str:
    if classification not in LEG_CLASSIFICATIONS:
        raise RiskError(f"out-of-vocabulary leg classification: {classification!r}")
    return classification
