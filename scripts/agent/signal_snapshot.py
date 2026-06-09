"""M3 §D — SignalSnapshot: frozen bundle of everything the probe knew at decision
time; `assemble()` is a PURE function returning either a snapshot or the frozen
gate-failure verdict. Gate ORDER is frozen in contract §1 — funnel attribution
depends on it: identity -> features -> quote -> market_state; the per-horizon gate
(`horizon_gate`) runs AFTER assembly and never stops sibling horizons.

Within the failing stage ALL applicable mapped reasons are collected and sorted
(rev2 SAFETY-F7). All timestamp comparisons are parse-then-compare; the horizon
arithmetic re-mints the canonical surface form (rev2 SAFETY-F2).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Union

from decimal import Decimal

from agent.feature_engine import FeatureSnapshot
from agent.market_calendar import SessionSchedule
from agent.market_state import SessionState, Tradability, Verdict
from agent.quote_quality import QuoteSnapshot, QuoteVerdict, evaluate as evaluate_quote
from agent.signal_config import SignalConfig

UTC = timezone.utc

GATE_STAGES = ("identity", "features", "quote", "market_state", "horizon")

# Frozen reason-code vocabulary (contract §1, closed set; ledger-validated).
REASONS = frozenset({
    "identity_mismatch", "feature_stale", "feature_cutoff_mismatch",
    "bar_lag_exceeded", "features_unavailable",
    "quote_missing", "quote_nonfinite", "quote_nonpositive", "quote_crossed",
    "quote_locked", "quote_one_sided", "quote_stale", "spread_too_wide",
    "market_state_not_tradable", "market_state_stale_default", "market_state_not_rth",
    "calendar_unknown", "session_horizon_crosses_close",
})


def _parse_utc(ts: str) -> datetime:
    raw = ts
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        raise ValueError(f"cannot parse UTC timestamp: {raw!r}")
    if dt.tzinfo is None:
        raise ValueError(f"timestamp is not timezone-aware: {raw!r}")
    return dt.astimezone(UTC)


def _canonical_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


@dataclass(frozen=True)
class GateFail:
    stage: str                       # ∈ GATE_STAGES
    reasons: Tuple[str, ...]         # sorted, frozen vocabulary (contract §1)
    horizon: Optional[str]           # set only for stage == "horizon"


@dataclass(frozen=True)
class SignalSnapshot:
    symbol: str
    instrument_id: int
    decision_ts_utc: str
    decision_seen_at_ms: int
    rules_hash: str
    feature: FeatureSnapshot                     # carries cutoff/watermark/id/pin
    quote: QuoteSnapshot
    quote_verdict: QuoteVerdict
    market_state: Verdict                        # M2 verdict, embedded as-is
    calendar_pin: str
    session_date_et: str                         # := market_state.session_date_et (rev3)
    event_start_bar_end_utc: str                 # == feature.feature_cutoff_bar_end_utc (enforced)
    horizons: Tuple[str, ...]
    threshold_k: Decimal


def assemble(*, symbol, instrument_id, decision_ts_utc, decision_seen_at_ms,
             event_start_bar_end_utc: str,
             feature: Optional[FeatureSnapshot], quote: Optional[QuoteSnapshot],
             market_state: Verdict, calendar_pin: str,
             config: SignalConfig, now_ms: int) -> Union[SignalSnapshot, GateFail]:
    # --- gate 1: identity (present objects must carry the requested identity) ---
    identity_holds = market_state.symbol == symbol and market_state.instrument_id == instrument_id
    if feature is not None:
        identity_holds = identity_holds and (
            feature.symbol == symbol and feature.instrument_id == instrument_id)
    if quote is not None:
        identity_holds = identity_holds and (
            quote.symbol == symbol and quote.instrument_id == instrument_id)
    if not identity_holds:
        return GateFail(stage="identity", reasons=("identity_mismatch",), horizon=None)

    # --- gate 2: features (collect ALL applicable, sorted) ---
    reasons = set()
    if feature is None:
        reasons.add("features_unavailable")
    else:
        if not feature.available:
            reasons.add("features_unavailable")
        # rev2 SAFETY-F5: the tick's bar IS the decision key; a lagging FeatureView
        # can never re-key a decision to an old bar.
        if feature.feature_cutoff_bar_end_utc is None or (
            _parse_utc(feature.feature_cutoff_bar_end_utc)
            != _parse_utc(event_start_bar_end_utc)
        ):
            reasons.add("feature_cutoff_mismatch")
        if now_ms - feature.refreshed_at_ms > config.feature_staleness_ms_max:
            reasons.add("feature_stale")
        if feature.feature_cutoff_bar_end_utc is not None:
            lag = _parse_utc(decision_ts_utc) - _parse_utc(feature.feature_cutoff_bar_end_utc)
            budget = timedelta(minutes=config.bar_lag_max_intervals)  # interval == 1m (FD-1)
            if lag > budget:
                reasons.add("bar_lag_exceeded")
    if reasons:
        return GateFail(stage="features", reasons=tuple(sorted(reasons)), horizon=None)

    # --- gate 3: quote quality (§A; reasons map through unchanged) ---
    if quote is None:
        return GateFail(stage="quote", reasons=("quote_missing",), horizon=None)
    quote_verdict = evaluate_quote(
        quote, now_ms=now_ms,
        spread_bps_max=config.spread_bps_max,
        staleness_ms_max=config.quote_staleness_ms_max,
    )
    if not quote_verdict.ok:
        return GateFail(stage="quote", reasons=quote_verdict.reasons, horizon=None)

    # --- gate 4: market-state (FD-13; collect ALL applicable, sorted) ---
    reasons = set()
    if "cache_stale_safe_default" in market_state.reasons:
        reasons.add("market_state_stale_default")
    if market_state.session_state != SessionState.RTH:
        reasons.add("market_state_not_rth")
    if market_state.tradability != Tradability.TRADABLE:
        reasons.add("market_state_not_tradable")
    if reasons:
        return GateFail(stage="market_state", reasons=tuple(sorted(reasons)), horizon=None)

    return SignalSnapshot(
        symbol=symbol,
        instrument_id=instrument_id,
        decision_ts_utc=decision_ts_utc,
        decision_seen_at_ms=decision_seen_at_ms,
        rules_hash=config.rules_hash,
        feature=feature,
        quote=quote,
        quote_verdict=quote_verdict,
        market_state=market_state,
        calendar_pin=calendar_pin,
        session_date_et=market_state.session_date_et,
        event_start_bar_end_utc=event_start_bar_end_utc,
        horizons=config.horizons,
        threshold_k=config.threshold_k,
    )


def horizon_gate(snapshot: SignalSnapshot, horizon: str,
                 schedule: Optional[SessionSchedule]) -> Union[str, GateFail]:
    """Per-horizon session check (gate 5). Returns the canonical
    `resolve_bar_end_utc`, or `GateFail(stage="horizon", horizon=horizon)`.

    `schedule is None` (UnknownSessionDate at fetch) => `calendar_unknown` —
    attributed HERE, per horizon (rev2 BUILD-F2/SAFETY-F3). A failing horizon never
    suppresses sibling horizons (the probe loops; rev2 SAFETY-F4).
    """
    if horizon not in snapshot.horizons:
        raise ValueError(f"horizon {horizon!r} not in configured horizons {snapshot.horizons}")
    if schedule is None or schedule.rth_close_utc is None:
        return GateFail(stage="horizon", reasons=("calendar_unknown",), horizon=horizon)
    minutes = int(horizon[:-1])  # validated '<int>m' by SignalConfig
    resolve_end = _parse_utc(snapshot.event_start_bar_end_utc) + timedelta(minutes=minutes)
    if resolve_end > _parse_utc(schedule.rth_close_utc):
        return GateFail(stage="horizon",
                        reasons=("session_horizon_crosses_close",), horizon=horizon)
    return _canonical_utc(resolve_end)
