"""Closed vocabularies for the M5 execution tier (contract §A / §2). Out-of-vocab
anywhere -> ExecError (FATAL, fail-closed, never coerced) — mirrors MarketStateError /
RiskError.

`KILL_STATES`/`Tradability`/`SessionState` are NOT duplicated here — they are imported
from their M4/M2 homes by consumers (one vocabulary, one home). Stdlib only; this
module prices nothing, submits nothing, and imports no `agent.*` module.
"""
from typing import FrozenSet, Tuple


class ExecError(ValueError):
    """Execution-tier invariant violation (out-of-vocab reason/state/cause, identity
    mismatch, tampered risk-verdict row, malformed collaborator input) -> FATAL.
    A rejectable market/account condition is DATA and never raises ExecError."""


# --- §2.1 PREFLIGHT_REASONS (closed set; emitting out-of-vocab raises ExecError) ---

TERMINAL_PREFLIGHT_REASONS: FrozenSet[str] = frozenset({
    "run_gates_off", "kill_switch_halted", "missing_decision_stamp"})

# The 34 phase-2 collected reasons, verbatim (sorted-union collect-all; §2.2 stages 4-11).
COLLECTED_PREFLIGHT_REASONS: FrozenSet[str] = frozenset({
    # candidate
    "order_matrix_unsupported", "invalid_lot",
    # strategy_gate
    "strategy_not_paper_eligible", "backtest_artifact_missing",
    "artifact_key_mismatch", "artifact_hash_invalid",
    "synthetic_requires_fake_broker", "fake_broker_requires_synthetic",
    # inflight
    "open_order_in_flight",
    # latency
    "latency_not_elapsed", "requote_not_later", "epoch_changed",
    # quote (M3 strings verbatim)
    "quote_missing", "quote_stale", "quote_crossed", "quote_locked",
    "quote_one_sided", "quote_nonfinite", "quote_nonpositive", "spread_too_wide",
    # market_state
    "market_state_not_tradable", "market_state_stale_default",
    "market_state_not_rth", "halt_luld_auction", "ca_blackout",
    # order
    "unpriceable_candidate", "invalid_tick", "not_marketable", "latency_lost_edge",
    # risk
    "risk_verdict_missing", "risk_verdict_stale", "risk_verdict_mismatch",
    "can_open_denied", "kill_generation_changed",
})

# Consume-time only (raised as PreflightStale; journaled stage="consume").
# kill_generation_changed is a SHARED string — also emittable at stage "risk".
CONSUME_REASONS: FrozenSet[str] = frozenset({
    "preflight_runtime_unbound", "open_token_expired", "kill_generation_changed"})

# RESERVED: in the frozenset, never emitted in M5 (short-side milestone / M6+ matrix).
RESERVED_PREFLIGHT_REASONS: FrozenSet[str] = frozenset({
    "ssr_short_blocked", "locate_unavailable", "extended_hours_blocked"})

# Frozen arithmetic (§2.1): 3 terminal + 34 collected + 2 consume-only + 3 reserved
# = 42 members (kill_generation_changed appears in BOTH collected and consume).
PREFLIGHT_REASONS: FrozenSet[str] = (TERMINAL_PREFLIGHT_REASONS
                                     | COLLECTED_PREFLIGHT_REASONS
                                     | CONSUME_REASONS
                                     | RESERVED_PREFLIGHT_REASONS)   # 42 members

# --- §2.2 ladder stages (frozen ORDER; journaled as gate_stage / stages_skipped) ---

PREFLIGHT_STAGES: Tuple[str, ...] = (
    "run_gates", "kill", "stamp", "candidate", "strategy_gate", "inflight",
    "latency", "quote", "market_state", "order", "risk")

# Reject-row supplements (NOT in PREFLIGHT_REASONS; legal only on their own stages):
# no_price_for_cap on stage="reduce_pricing" (FD-M5-26) and inside kill failed[]
# tuples (FD-M5-1); broker_rejected on stage="broker" rows.
EXTRA_REJECT_REASONS: FrozenSet[str] = frozenset({"no_price_for_cap", "broker_rejected"})
REJECT_STAGES: FrozenSet[str] = (frozenset(PREFLIGHT_STAGES)
                                 | {"consume", "broker", "reduce_pricing"})

# --- §2.4 other frozen vocabularies ---

ORDER_STATES: FrozenSet[str] = frozenset({
    "accepted", "partially_filled", "filled", "canceled", "expired",
    "rejected", "done_for_day", "pending_cancel", "unknown"})
TERMINAL_STATES: FrozenSet[str] = frozenset({
    "filled", "canceled", "expired", "rejected", "done_for_day"})
CANCEL_CAUSES: FrozenSet[str] = frozenset({
    "epoch_changed", "halt_luld_auction", "market_state_not_tradable",
    "market_state_stale_default", "session_end", "kill_trip",
    "unexpected_status", "restart_unknown_state"})
CANCEL_OUTCOMES: FrozenSet[str] = frozenset({
    "cancel_submitted", "already_terminal", "cancel_rejected", "error"})
CLOSE_REASONS: FrozenSet[str] = frozenset({
    "strategy_exit", "kill_flatten", "session_end", "synthetic_script", "operator"})
REALISM_CLASSES: FrozenSet[str] = frozenset({
    "modeled_full", "modeled_partial", "modeled_unfillable"})
MODELED_FILL_MODELS: FrozenSet[str] = frozenset({"tob_l1_v1", "depth_vwap_l2_v2"})
DIVERGENCE_FLAGS: FrozenSet[str] = frozenset({
    "aligned", "broker_optimistic", "broker_conservative", "unassessed"})
FILL_POLICIES: FrozenSet[str] = frozenset({
    "immediate_full", "partial_then_full", "never_fill", "reject_all"})  # FakeBroker
BROKER_KINDS: FrozenSet[str] = frozenset({
    "spy", "fake", "alpaca_paper", "alpaca_live"})  # alpaca_live RESERVED (M8)
STRATEGY_DECISION_ACTIONS: FrozenSet[str] = frozenset({"would_open", "would_close"})
FILL_SOURCES: FrozenSet[str] = frozenset({"alpaca_paper", "fake"})
SUBMIT_RESOLUTIONS: FrozenSet[str] = frozenset({"adopted", "not_found", "offline_orphan"})


def require_reason(code: str) -> str:
    """ExecError on non-membership AND on reserved-in-M5 emission (M5C-T8)."""
    if code not in PREFLIGHT_REASONS:
        raise ExecError(f"out-of-vocab preflight reason: {code!r}")
    if code in RESERVED_PREFLIGHT_REASONS:
        raise ExecError(f"reserved-in-M5 preflight reason emitted: {code!r}")
    return code


def require_stage(name: str) -> str:
    """ExecError unless `name` is a legal journaled reject stage (REJECT_STAGES =
    the 11 ladder stages + consume/broker/reduce_pricing, §2.1)."""
    if name not in REJECT_STAGES:
        raise ExecError(f"out-of-vocab reject stage: {name!r}")
    return name


def require_member(vocab: FrozenSet[str], value: str, *, what: str) -> str:
    """Generic closed-vocabulary guard: ExecError on non-membership, never coerced."""
    if value not in vocab:
        raise ExecError(f"out-of-vocab {what}: {value!r}")
    return value
