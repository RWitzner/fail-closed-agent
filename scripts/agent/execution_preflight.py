"""Pre-submit execution preflight: registry-authorized, single-use tokens (spec §5 Tier 6).

A token is an opaque capability handle. Its authority does NOT live on the token
object (which carries only an opaque nonce and cannot be constructed, copied, or
mutated into authority) — it lives in a module-private registry of **immutable**
authorizations, keyed by the token's nonce and created ONLY by the mint functions.

- `mint_open_token` — M5 (contract §D, FD-M5-14): runs the PURE `evaluate_preflight`
  ladder ITSELF over a `PreflightInputs` and issues an open authorization only on a
  zero-reason pass; on the committed config every preflight terminates at the
  `run_gates` rung (S1 — reject-all by ladder instead of by stub). A directly-
  constructed `OpenPreflightToken` (built by importing the private mint key) still
  has no authorization and cannot open.
- `mint_reduce_only_token` — issues an authorization only for a genuinely
  position-decreasing order, validated against the held position's sign, size, and
  symbol (never the caller's self-asserted flag). The stored side+qty are
  re-checked at `require_token`, so mutating the token cannot rebind it.

M5 consume-time TOCTOU (contract §2.3, FD-M5-13): `consume()` re-checks OPEN-kind
authorizations against the orchestrator-bound runtime (injected clock + kill
generation source) — unbound runtime / expired TTL / bumped generation raise
`PreflightStale` and REVOKE the authorization. The reduce-kind consume path is
byte-identical to M0 (FD-M4-3: no M5 code path may block a reduction).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Mapping, Optional, Tuple, Union

from agent.candidate import Candidate
from agent.exec_reasons import (
    BROKER_KINDS,
    EXTRA_REJECT_REASONS,
    ExecError,
    PREFLIGHT_REASONS,
    PREFLIGHT_STAGES,
    require_member,
    require_reason,
)
from agent.execution_config import (
    ExecutionConfig,
    MIN_REQUOTE_DELTA_MS,
    OPEN_TOKEN_TTL_MS,
    RISK_VERDICT_TTL_MS,
)
from agent.gates import opening_allowed
from agent.market_state import HaltState, LuldState, SessionState, Tradability, Verdict
from agent.order_pricing import marketable_limit_cap
from agent.quote_quality import QuoteSnapshot, QuoteVerdict
from agent.risk.reasons import KILL_STATES
from agent.serializer import row_hash

_MINT = object()  # module-private mint key
_authorizations = {}  # nonce(object) -> _Authorization (immutable); only mint_* writes here

# Orchestrator-bound consume-time runtime (FD-M5-13): None == unbound (the M0 spy
# default) — EVERY open consume then fails `preflight_runtime_unbound`, so a broker
# wired outside the orchestrator can never place an open (fail-closed default).
_runtime: Optional[Tuple[object, Callable[[], int]]] = None


@dataclass(frozen=True)
class _Authorization:
    kind: str  # "open" | "reduce_only"
    symbol: str
    side: Optional[str] = None
    qty: Optional[Decimal] = None
    # M5 (§D): OPTIONAL open-only fields defaulting None so reduce authorizations
    # construct exactly as in M0 (byte-identical reduce behavior is a test).
    limit_price: Optional[Decimal] = None
    kill_generation: Optional[int] = None
    minted_at_ms: Optional[int] = None


class PreflightRejected(Exception):
    """A token was requested but the preflight gates refuse to issue an authorization."""


class PreflightForgery(Exception):
    """A token was constructed/copied directly, reused, or has no/wrong authorization."""


class PreflightStale(PreflightRejected):
    """Raised by consume() on the §2.3 open-branch re-checks (TOCTOU, FD-M5-13). The
    authorization is revoked before raising; the caller journals reject{stage:'consume'}."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class PreflightToken:
    __slots__ = ("_nonce",)

    def __init__(self, *, mint):
        if mint is not _MINT:
            raise PreflightForgery("preflight tokens cannot be constructed directly")
        self._nonce = object()

    def __copy__(self):
        raise PreflightForgery("preflight tokens are single-use; copying is forbidden")

    def __deepcopy__(self, memo):
        raise PreflightForgery("preflight tokens are single-use; copying is forbidden")

    def __reduce__(self):
        raise PreflightForgery("preflight tokens cannot be pickled")


class OpenPreflightToken(PreflightToken):
    """Handle for a single opening / position-increasing order."""


class ReduceOnlyPreflightToken(PreflightToken):
    """Handle for a single position-decreasing order."""


def _issue(token_cls, authorization: _Authorization):
    token = token_cls(mint=_MINT)
    _authorizations[token._nonce] = authorization
    return token


def authorization_of(token) -> Optional[_Authorization]:
    if not isinstance(token, PreflightToken):
        return None
    return _authorizations.get(getattr(token, "_nonce", None))


def is_authentic(token) -> bool:
    return authorization_of(token) is not None


def bind_runtime(*, clock, kill_generation_source: Callable[[], int]) -> None:
    """Set ONCE by the orchestrator at startup (§M.2 step 8). Rebinding without
    `unbind_runtime()` raises ExecError (no silent swap)."""
    global _runtime
    if _runtime is not None:
        raise ExecError("preflight runtime already bound; unbind_runtime() first (no silent swap)")
    _runtime = (clock, kill_generation_source)


def unbind_runtime() -> None:
    """Tests only (teardown hygiene): restore the M0 unbound default. Idempotent."""
    global _runtime
    _runtime = None


def consume(token) -> _Authorization:
    auth = authorization_of(token)
    if auth is None:
        raise PreflightForgery("token is forged, reused, or invalid")
    if auth.kind == "open":
        # §2.3 consume-time re-checks (open kind ONLY; reduce stays byte-identical
        # to M0 — FD-M4-3). A PreflightStale REVOKES the authorization: deleted
        # BEFORE raising, so the token is spent either way (single-use).
        if _runtime is None:
            del _authorizations[token._nonce]
            raise PreflightStale("preflight_runtime_unbound")
        clock, kill_generation_source = _runtime
        if auth.minted_at_ms is None or clock.now_ms() - auth.minted_at_ms > OPEN_TOKEN_TTL_MS:
            del _authorizations[token._nonce]
            raise PreflightStale("open_token_expired")
        if auth.kill_generation is None or kill_generation_source() != auth.kill_generation:
            del _authorizations[token._nonce]
            raise PreflightStale("kill_generation_changed")
    del _authorizations[token._nonce]
    return auth


def void_token(token, reason: str) -> None:
    """Delete an unconsumed authorization (idempotent). `reason` must be a legal
    journal-row code (PREFLIGHT_REASONS ∪ EXTRA_REJECT_REASONS) — validated BEFORE
    any deletion so an out-of-vocab call never half-acts."""
    if reason not in PREFLIGHT_REASONS and reason not in EXTRA_REJECT_REASONS:
        raise ExecError(f"void_token reason out of vocabulary: {reason!r}")
    auth = authorization_of(token)
    if auth is None:
        return  # already consumed/voided/never-issued: idempotent no-op
    del _authorizations[token._nonce]


# --- M5 open-preflight ladder (contract §D, §2.2) -----------------------------------


@dataclass(frozen=True)
class DecisionStamp:
    decision_id: str
    decision_ts_utc: str             # parseable ISO-8601 UTC
    decision_seen_at_ms: int         # injected monotonic stamp at decision time (t0)
    quote_a: QuoteSnapshot           # the quote the strategy decided on (provenance-stamped)


@dataclass(frozen=True)
class PreflightInputs:
    run_id: str
    stamp: Optional[DecisionStamp]
    candidate: Candidate                         # single-leg in M5 (len != 1 -> ExecError)
    strategy_id: str
    strategy_is_synthetic: bool                  # computed by the orchestrator (isinstance)
    quote_b: Optional[QuoteSnapshot]
    quote_b_verdict: Optional[QuoteVerdict]      # quote_quality.evaluate(quote_b, now_ms=now_ms, ...)
    feed_epoch_now: int
    market_state: Verdict                        # FRESH MarketStateCache.get at preflight time
    risk_verdict: Optional[object]               # duck-typed M4 RiskVerdict (§D: NOT imported)
    risk_verdict_row: Optional[dict]             # the journaled row (FD-M5-30)
    risk_verdict_now_ms: Optional[int]           # the now_ms the caller passed to can_open
    kill_state: str
    kill_generation: int
    open_orders_in_flight: int
    artifact_check: object                       # duck-typed backtest_gate.ArtifactCheck (.status)
    broker_kind: str                             # ∈ BROKER_KINDS (defense-in-depth input)
    gates_config: dict                           # the assembled gates view (§O.2 in paper mode)
    exec_config: ExecutionConfig
    now_ms: int


@dataclass(frozen=True)
class PreflightReject:
    reasons: Tuple[str, ...]                     # sorted, deduped, ⊆ PREFLIGHT_REASONS
    gate_stage: Optional[str]                    # terminal stage, or None (phase 2 reached)
    stages_skipped: Tuple[str, ...]              # frozen PREFLIGHT_STAGES members, in order
    preflight_id: str                            # §P.3 deterministic id
    capped_limit: Optional[Decimal]              # set iff stage 'order' was reached and derivable
    detail: Mapping                              # {"risk_reasons": [...]|None, "quote_reasons": [...]|None}


@dataclass(frozen=True)
class PreflightPass:
    preflight_id: str
    symbol: str
    instrument_id: int
    side: str
    qty: Decimal
    capped_limit: Decimal                        # THE submitted limit (on grid; §C)
    quote_b_provenance: Mapping                  # §P.2 frozen provenance key set
    latency_observed_ms: int                     # quote_b.seen_at_ms - decision_seen_at_ms
    kill_generation: int
    risk_verdict_id: str


def _parseable_utc(raw) -> bool:
    """ISO-8601 UTC parse check for DecisionStamp.decision_ts_utc (stage 3). Accepts
    the 'Z' suffix and an explicit +00:00 offset; naive or non-UTC -> not parseable
    AS UTC (fail-closed)."""
    if not isinstance(raw, str) or not raw:
        return False
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def _stamp_missing(stamp: Optional[DecisionStamp]) -> bool:
    """FD-M5-12 (stage 3): the stamp is missing iff absent, quote_a absent,
    decision_seen_at_ms not an int (bool excluded), or decision_ts_utc unparseable."""
    if stamp is None or stamp.quote_a is None:
        return True
    seen = stamp.decision_seen_at_ms
    if isinstance(seen, bool) or not isinstance(seen, int):
        return True
    return not _parseable_utc(stamp.decision_ts_utc)


def _provenance(quote: QuoteSnapshot) -> Mapping:
    """The §P.2 frozen provenance sub-dict key set."""
    return {
        "dataset": quote.dataset,
        "schema": quote.schema,
        "ts_event_utc": quote.ts_event_utc,
        "ts_recv_utc": quote.ts_recv_utc,
        "seen_at_ms": quote.seen_at_ms,
        "reconnect_epoch": quote.reconnect_epoch,
        "vendor_seq": quote.vendor_seq,
    }


def _preflight_id(inputs: PreflightInputs, leg, reasons) -> str:
    """§P.3: 'pf-' + row_hash over the EXACT key set (Decimal values render as
    canonical serializer strings inside row_hash)."""
    stamp = inputs.stamp
    return "pf-" + row_hash({
        "run_id": inputs.run_id,
        "decision_id": stamp.decision_id if stamp is not None else None,
        "symbol": leg.symbol,
        "side": leg.side,
        "qty": leg.qty,
        "reasons": sorted(reasons),
        "quote_b_seen_at_ms": (inputs.quote_b.seen_at_ms
                               if inputs.quote_b is not None else None),
        "kill_generation": inputs.kill_generation,
    })


def _terminal_reject(inputs: PreflightInputs, leg, stage: str,
                     reasons: Tuple[str, ...]) -> PreflightReject:
    for code in reasons:
        require_reason(code)
    stage_index = PREFLIGHT_STAGES.index(stage)
    return PreflightReject(
        reasons=reasons,
        gate_stage=stage,
        stages_skipped=PREFLIGHT_STAGES[stage_index + 1:],
        preflight_id=_preflight_id(inputs, leg, reasons),
        capped_limit=None,
        detail={"risk_reasons": None, "quote_reasons": None},
    )


def evaluate_preflight(inputs: PreflightInputs) -> Union[PreflightPass, PreflightReject]:
    """The S4 open-preflight ladder (§2.2). PURE: no I/O, no clock read, no journal
    write (FD-M5-14) — time enters ONLY via injected `now_ms`/`seen_at_ms` stamps.
    Deterministic: identical inputs => identical result + identical preflight_id.

    ExecError ONLY on invariant breaks (§2.2): out-of-vocab kill state / artifact
    status / broker_kind, market-state identity mismatch, multi-leg candidate,
    tampered risk_verdict_row hash (FD-M5-30), float in a money slot (raised by the
    §C cap math). A rejectable market/account condition is DATA -> PreflightReject.
    """
    candidate = inputs.candidate
    # The deterministic id needs a leg even on phase-1 terminals; the single-leg
    # invariant itself is stage 4's check (§2.2) and is only reached in phase 2.
    leg = candidate.legs[0]

    # --- phase 1: terminal short-circuits (frozen order run_gates -> kill -> stamp) ---
    if not opening_allowed(inputs.gates_config):
        # Rung 1 is the literal gates.opening_allowed (FD-M5-11): on the committed
        # config EVERY preflight terminates here with the byte-exact S1 shape.
        return _terminal_reject(inputs, leg, "run_gates", ("run_gates_off",))
    # Stage 2 (§2.2): the kill-state vocabulary is validated AT its stage — a
    # short-circuit at run_gates already rejected without consulting it.
    if inputs.kill_state not in KILL_STATES:
        raise ExecError(f"out-of-vocab kill state: {inputs.kill_state!r}")
    if inputs.kill_state != "monitoring":
        return _terminal_reject(inputs, leg, "kill", ("kill_switch_halted",))
    if _stamp_missing(inputs.stamp):
        return _terminal_reject(inputs, leg, "stamp", ("missing_decision_stamp",))

    # --- phase 2: collect-ALL (stages 4-11; no short-circuit; sorted deduped union) ---
    stamp = inputs.stamp
    quote_a = stamp.quote_a
    quote_b = inputs.quote_b
    reasons = set()
    skipped = []
    risk_reasons_detail = None
    quote_reasons_detail = None
    capped_limit: Optional[Decimal] = None

    # stage 4 — candidate (side-only matrix per M5C-B1; order_type/tif/is_reducing
    # are STRUCTURAL constants of the §M.4 intent builder, not checked here).
    if len(candidate.legs) != 1:
        raise ExecError(f"M5 preflight is single-leg: got {len(candidate.legs)} legs")
    if leg.side != "buy":
        reasons.add("order_matrix_unsupported")  # long-only opens (FD-M4-1)
    if leg.qty != leg.qty.to_integral_value() or leg.qty < 1:
        reasons.add("invalid_lot")               # whole shares only

    # stage 5 — strategy_gate (S9)
    require_member(BROKER_KINDS, inputs.broker_kind, what="broker_kind")
    if not isinstance(inputs.strategy_is_synthetic, bool):
        raise ExecError("strategy_is_synthetic must be a bool")
    if candidate.paper_eligible is not True:     # identity-strict
        reasons.add("strategy_not_paper_eligible")
    if inputs.strategy_is_synthetic is False:
        status = getattr(inputs.artifact_check, "status", None)
        if status == "missing":
            reasons.add("backtest_artifact_missing")
        elif status == "key_mismatch":
            reasons.add("artifact_key_mismatch")
        elif status == "hash_invalid":
            reasons.add("artifact_hash_invalid")
        elif status != "ok":
            raise ExecError(f"out-of-vocab artifact_check status: {status!r}")
        if inputs.broker_kind == "fake":
            reasons.add("fake_broker_requires_synthetic")
    else:
        # Synthetic: artifact recorded, not consulted (FD-M5-8 defense-in-depth).
        if inputs.broker_kind != "fake":
            reasons.add("synthetic_requires_fake_broker")

    # stage 6 — inflight (FD-M5-21 defense-in-depth)
    if inputs.open_orders_in_flight > 0:
        reasons.add("open_order_in_flight")

    # stage 7 — latency (evaluated iff quote_b present; absence is stage 8's)
    if quote_b is None:
        skipped.append("latency")
    else:
        if (quote_b.seen_at_ms - stamp.decision_seen_at_ms
                < inputs.exec_config.effective_latency_budget_ms):  # strict '<'
            reasons.add("latency_not_elapsed")
        if (quote_b.seen_at_ms < quote_a.seen_at_ms + MIN_REQUOTE_DELTA_MS
                or (quote_b.vendor_seq, quote_b.ts_event_utc)
                == (quote_a.vendor_seq, quote_a.ts_event_utc)):
            reasons.add("requote_not_later")     # a re-served quote A is not a second quote
        if (quote_b.reconnect_epoch != quote_a.reconnect_epoch
                or inputs.feed_epoch_now != quote_b.reconnect_epoch):
            reasons.add("epoch_changed")

    # stage 8 — quote (quote_quality.evaluate is the ONE quote decider; embed verbatim)
    if quote_b is None:
        reasons.add("quote_missing")
    else:
        if inputs.quote_b_verdict is None:
            raise ExecError(
                "quote_b present without quote_b_verdict (the caller must run "
                "quote_quality.evaluate with the SAME now_ms)")
        if inputs.quote_b_verdict.reasons:
            for code in inputs.quote_b_verdict.reasons:
                require_reason(code)
            reasons.update(inputs.quote_b_verdict.reasons)
            quote_reasons_detail = sorted(inputs.quote_b_verdict.reasons)

    # stage 9 — market_state (identity first; the five frozen checks; M5C-2/M5C-3)
    verdict_ms = inputs.market_state
    if verdict_ms.symbol != leg.symbol or verdict_ms.instrument_id != leg.instrument_id:
        raise ExecError(
            f"market_state verdict identity mismatch: verdict "
            f"({verdict_ms.symbol!r}, {verdict_ms.instrument_id}) vs leg "
            f"({leg.symbol!r}, {leg.instrument_id})")
    if verdict_ms.tradability != Tradability.TRADABLE:
        reasons.add("market_state_not_tradable")  # REDUCE_ONLY blocks opens too
    if "cache_stale_safe_default" in verdict_ms.reasons:
        reasons.add("market_state_stale_default")
    if verdict_ms.session_state != SessionState.RTH:
        reasons.add("market_state_not_rth")       # M5 opens are RTH-only (A3)
    if (verdict_ms.halt != HaltState.NONE
            or verdict_ms.luld in (LuldState.LIMIT, LuldState.PAUSED, LuldState.UNKNOWN)
            or verdict_ms.session_state == SessionState.AUCTION):
        reasons.add("halt_luld_auction")          # LuldState.NORMAL is the only non-firing member
    if verdict_ms.ca_blackout:
        reasons.add("ca_blackout")

    # stage 10 — order (skipped iff stage 8 found quote_b missing or unusable)
    if quote_b is None or inputs.quote_b_verdict.ok is False:
        skipped.append("order")
    else:
        cap = marketable_limit_cap(
            side=leg.side, quote_a=quote_a, quote_b=quote_b,
            slippage_cap_bps=inputs.exec_config.slippage_cap_bps,
            strategy_limit=leg.limit_price)
        reasons.update(cap.reasons)
        capped_limit = cap.capped_limit           # set iff derivable (reject rows keep it)

    # stage 11 — risk (structural binding, FD-M5-30)
    risk_verdict = inputs.risk_verdict
    risk_row = inputs.risk_verdict_row
    risk_now_ms = inputs.risk_verdict_now_ms
    if risk_verdict is None or risk_row is None or risk_now_ms is None:
        reasons.add("risk_verdict_missing")       # the caller failed journal-then-mint
    else:
        stored_hash = risk_row.get("hash")
        body = {key: value for key, value in risk_row.items() if key != "hash"}
        if not isinstance(stored_hash, str) or row_hash(body) != stored_hash:
            raise ExecError(
                "tampered risk_verdict_row: journal hash re-verification failed "
                "(FD-M5-30 — a bug, not data)")
        mismatch = (risk_row.get("verdict_id") != risk_verdict.verdict_id
                    or risk_row.get("decision_id") != stamp.decision_id)
        verdict_legs = tuple(getattr(risk_verdict, "legs", ()))
        if (len(verdict_legs) != 1
                or verdict_legs[0].symbol != leg.symbol
                or verdict_legs[0].qty != leg.qty):
            mismatch = True
        if mismatch:
            reasons.add("risk_verdict_mismatch")
        if inputs.now_ms - risk_now_ms > RISK_VERDICT_TTL_MS:  # strict '>'
            reasons.add("risk_verdict_stale")
        if risk_verdict.allowed is not True:      # identity-strict
            reasons.add("can_open_denied")
            risk_reasons_detail = [str(code) for code in risk_verdict.reasons]
        if risk_verdict.kill_generation != inputs.kill_generation:
            reasons.add("kill_generation_changed")

    for code in reasons:
        require_reason(code)                      # out-of-vocab/reserved -> ExecError
    stages_skipped = tuple(stage for stage in PREFLIGHT_STAGES if stage in skipped)
    if reasons:
        sorted_reasons = tuple(sorted(reasons))
        return PreflightReject(
            reasons=sorted_reasons,
            gate_stage=None,                      # phase 2 reached
            stages_skipped=stages_skipped,
            preflight_id=_preflight_id(inputs, leg, sorted_reasons),
            capped_limit=capped_limit,
            detail={"risk_reasons": risk_reasons_detail,
                    "quote_reasons": quote_reasons_detail},
        )

    if capped_limit is None:
        raise ExecError("zero-reason pass without a derivable capped_limit (invariant)")
    return PreflightPass(
        preflight_id=_preflight_id(inputs, leg, ()),
        symbol=leg.symbol,
        instrument_id=leg.instrument_id,
        side=leg.side,
        qty=leg.qty,
        capped_limit=capped_limit,
        quote_b_provenance=_provenance(quote_b),
        latency_observed_ms=quote_b.seen_at_ms - stamp.decision_seen_at_ms,
        kill_generation=inputs.kill_generation,
        risk_verdict_id=risk_verdict.verdict_id,
    )


def mint_open_token(inputs: PreflightInputs) -> Tuple[OpenPreflightToken, PreflightPass]:
    """THE ONLY open-mint path (FD-M5-14). Runs `evaluate_preflight` ITSELF — a caller
    can never assert 'passed'. On reject raises PreflightRejected with `.reject`
    attached (the CALLER journals); on pass issues the open authorization binding
    side+qty+limit_price+kill_generation+minted_at_ms (consume re-checks §2.3)."""
    result = evaluate_preflight(inputs)
    if isinstance(result, PreflightReject):
        rejected = PreflightRejected(
            "open preflight rejected at stage "
            f"{result.gate_stage or 'phase2'}: {','.join(result.reasons)}")
        rejected.reject = result
        raise rejected
    token = _issue(
        OpenPreflightToken,
        _Authorization(
            kind="open",
            symbol=result.symbol,
            side=result.side,
            qty=result.qty,
            limit_price=result.capped_limit,
            kill_generation=inputs.kill_generation,
            minted_at_ms=inputs.now_ms,
        ),
    )
    return token, result


def mint_reduce_only_token(position, intent) -> ReduceOnlyPreflightToken:
    """Issue an authorization only for a genuinely position-decreasing order."""
    raw_qty = getattr(position, "qty", None) if position is not None else None
    if raw_qty is None:
        raise PreflightRejected("reduce-only requires an existing held position")
    held = Decimal(raw_qty)
    if held == 0:
        raise PreflightRejected("reduce-only requires a non-zero held position")
    if getattr(intent, "is_reducing", False) is not True:
        raise PreflightRejected("reduce-only requires an order flagged is_reducing")
    if intent.symbol != getattr(position, "symbol", None):
        raise PreflightRejected("reduce-only order symbol must match the held position")
    required_side = "sell" if held > 0 else "buy"  # long reduces by selling, short by buying
    if intent.side != required_side:
        raise PreflightRejected(f"reduce-only for this position requires side={required_side!r}")
    order_qty = Decimal(intent.qty)
    if order_qty <= 0 or order_qty > abs(held):
        raise PreflightRejected("reduce-only qty must be >0 and <= held size (may flatten, never flip)")
    return _issue(
        ReduceOnlyPreflightToken,
        _Authorization(kind="reduce_only", symbol=position.symbol, side=required_side, qty=order_qty),
    )
