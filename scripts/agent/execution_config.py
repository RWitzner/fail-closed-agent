"""ExecutionConfig: the ONE parser of `agent_rules.execution` + `latency_budget_ms`
(M5 contract §B — SignalConfig posture: closed key sets, fail-loud ValueError at
startup, parsed once).

`from_config` takes the ASSEMBLED {"agent_rules": ..., "risk_rules": ...} dict (the
M4 RiskConfig shape; the M4C-9 note transfers: this `rules_hash` matches RiskConfig's,
not M3's — funnel joins key on run_id/decision_id). Both the parse and the hash run
over the PRE-substitution dict (M5C-S4): the caller passes the committed(+overlay)
assembly BEFORE any §O.2 run-gates substitution, and this module just hashes what it
receives — identical committed config => identical `rules_hash` on every journaled
row whether the run-gates file is present-true or absent.

Polarity discipline (FD-M5-29; the M2 §G / M4 FD-M4-22 table): the four `execution`
knobs are JSON ints whose tighten-only min()-merge is CORRECT (smaller = tighter /
fresher = safer). Everything with INVERTED polarity or safety-quantum semantics is a
CODE CONSTANT below — in particular `latency_budget_ms` (committed top-level JSON
int) min()-merges the WRONG way (lowering it loosens realism), so the parser FLOORS
it at LATENCY_BUDGET_MIN_MS for values that parse and raises ValueError for values
that don't (0 / negative; the M5C-T2 pin).

`quote_staleness_ms_max` / `spread_bps_max` are re-read from the committed signal
block (ONE source) — they are FD-7 strings there, parsed exactly like SignalConfig
parses them.
"""
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

LATENCY_BUDGET_MIN_MS = 250        # CODE CONSTANT — floors the committed value (FD-M5-10:
                                   #   min()-merge polarity is INVERTED for latency_budget_ms)
MIN_REQUOTE_DELTA_MS = 1           # quote B must post-date quote A by >= 1 ms (§2.2 stage 7)
OPEN_TOKEN_TTL_MS = 2000           # consume-time TTL (FD-M5-13); strict '>'
RISK_VERDICT_TTL_MS = 2000         # mint-time verdict freshness (FD-M5-30); strict '>'.
                                   # Fixture-design constraint (EX-12): under ReplayClock, t1 jumps to
                                   # the next RECORDED event; any fixture driving a successful open
                                   # must land >=1 quote event in recorded (t0+effective_latency_budget_ms,
                                   # t0+RISK_VERDICT_TTL_MS] after each scripted decision bar (see §Q).
ORDER_POLL_INTERVAL_MS_MAX = 1000  # ceiling clamp: effective = min(parsed, 1000)
SUBMIT_RECOVERY_ATTEMPTS = 3       # FD-M5-17
DIVERGENCE_ALERT_BPS = Decimal("10")   # FD-M5-6
FLATTEN_CAP_BPS = Decimal("100")       # kill-path wide cap (FD-M5-26); strategy closes use slippage_cap_bps
DEPTH_FRESHNESS_TTL_MS = 2000          # DepthSnapshot age bound (strict '>'); stale => degrade to tob

_EXECUTION_KEYS = frozenset({"slippage_cap_bps", "order_poll_interval_ms",
                             "account_refresh_interval_ms", "max_open_orders"})

# Parse-time guard: account refresh must outrun ACCOUNT_FRESHNESS_TTL_MS (§B table).
_ACCOUNT_REFRESH_LIMIT_MS = 5000


def _require_json_int(block: dict, key: str, *, path: str) -> int:
    """A committed JSON int (bool excluded) > 0 — fail-loud ValueError otherwise."""
    if key not in block:
        raise ValueError(f"{path}.{key}: missing required key")
    value = block[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{path}.{key}: must be a JSON int (bool excluded), got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{path}.{key}: must be > 0, got {value}")
    return value


def _require_str(block: dict, key: str, *, path: str) -> str:
    """An FD-7 string leaf of the signal block (the signal_config.py form)."""
    if key not in block:
        raise ValueError(f"{path}.{key}: missing required signal key")
    value = block[key]
    if not isinstance(value, str):
        raise ValueError(
            f"{path}.{key}: must be a string (FD-7), got {type(value).__name__}")
    return value


def _parse_int(raw: str, *, path: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{path}: not an integer string: {raw!r}")
    if value <= 0:
        raise ValueError(f"{path}: must be > 0, got {value}")
    return value


def _parse_decimal(raw: str, *, path: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{path}: not a Decimal string: {raw!r}")
    if not value.is_finite():
        raise ValueError(f"{path}: non-finite Decimal not allowed: {raw!r}")
    return value


@dataclass(frozen=True)
class ExecutionConfig:
    """Typed, validated view of `agent_rules.execution` (+ latency + the two
    signal-block freshness knobs). Immutable; parsed once at startup."""

    slippage_cap_bps: Decimal              # from JSON int; > 0
    order_poll_interval_ms: int            # ceiling-clamped at ORDER_POLL_INTERVAL_MS_MAX
    account_refresh_interval_ms: int       # must be < 5000 (ACCOUNT_FRESHNESS_TTL_MS) at parse
    max_open_orders: int                   # parser REJECTS != 1 in M5 (FD-M5-21)
    effective_latency_budget_ms: int       # max(agent_rules.latency_budget_ms, LATENCY_BUDGET_MIN_MS)
    quote_staleness_ms_max: int            # re-read from signal block (one source)
    spread_bps_max: Decimal                # re-read from signal block (one source)
    rules_hash: str                        # of the WHOLE assembled config (config.py:17 semantics)
                                           #   — computed over the PRE-substitution dict (M5C-S4)

    @classmethod
    def from_config(cls, config: dict) -> "ExecutionConfig":
        """`config` = the assembled {"agent_rules": ..., "risk_rules": ...} dict,
        PRE-substitution (M5C-S4). Unknown/missing keys in agent_rules.execution ->
        ValueError; ints must be JSON ints (bool excluded) > 0; latency_budget_ms
        must be a JSON int > 0 (0 or negative => ValueError at startup, fail-loud —
        the floor only applies to values that PARSE; M5C-T2)."""
        if not isinstance(config, dict) or "agent_rules" not in config:
            raise ValueError("config has no 'agent_rules' block (assembled dict expected)")
        agent_rules = config["agent_rules"]
        if not isinstance(agent_rules, dict):
            raise ValueError("'agent_rules' must be a dict")

        if "execution" not in agent_rules:
            raise ValueError("agent_rules has no 'execution' block")
        execution = agent_rules["execution"]
        if not isinstance(execution, dict):
            raise ValueError("agent_rules.execution: must be a dict")
        unknown = set(execution) - _EXECUTION_KEYS
        if unknown:
            raise ValueError(f"agent_rules.execution: unknown keys {sorted(unknown)}")
        missing = _EXECUTION_KEYS - set(execution)
        if missing:
            raise ValueError(f"agent_rules.execution: missing keys {sorted(missing)}")

        path = "agent_rules.execution"
        slippage_cap_bps = Decimal(_require_json_int(execution, "slippage_cap_bps", path=path))
        poll_parsed = _require_json_int(execution, "order_poll_interval_ms", path=path)
        account_refresh = _require_json_int(execution, "account_refresh_interval_ms", path=path)
        max_open_orders = _require_json_int(execution, "max_open_orders", path=path)
        if max_open_orders != 1:
            raise ValueError(
                "agent_rules.execution.max_open_orders: must be exactly 1 in M5 "
                f"(FD-M5-21), got {max_open_orders}")
        if account_refresh >= _ACCOUNT_REFRESH_LIMIT_MS:
            raise ValueError(
                "agent_rules.execution.account_refresh_interval_ms: must be < "
                f"{_ACCOUNT_REFRESH_LIMIT_MS} (ACCOUNT_FRESHNESS_TTL_MS), got {account_refresh}")

        # FD-M5-10 / M5C-T2: parse fail-loud first (0/negative/non-int raises),
        # THEN floor — the min()-merge polarity for this one value is INVERTED.
        latency_parsed = _require_json_int(agent_rules, "latency_budget_ms", path="agent_rules")
        effective_latency_budget_ms = max(latency_parsed, LATENCY_BUDGET_MIN_MS)

        order_poll_interval_ms = min(poll_parsed, ORDER_POLL_INTERVAL_MS_MAX)

        # One source (§B): the freshness/spread knobs live in the signal block as
        # FD-7 strings; re-read them the way SignalConfig does, never duplicated.
        if "signal" not in agent_rules or not isinstance(agent_rules["signal"], dict):
            raise ValueError("agent_rules has no 'signal' block (one-source re-read, §B)")
        signal = agent_rules["signal"]
        quote_staleness_ms_max = _parse_int(
            _require_str(signal, "quote_staleness_ms_max", path="agent_rules.signal"),
            path="agent_rules.signal.quote_staleness_ms_max")
        spread_bps_max = _parse_decimal(
            _require_str(signal, "spread_bps_max", path="agent_rules.signal"),
            path="agent_rules.signal.spread_bps_max")

        # rules_hash over the WHOLE assembled config as received (PRE-substitution;
        # M5C-S4) — byte-identical to agent.config.rules_hash (config.py:17
        # semantics, allow_nan=False fail-loud), computed inline because this
        # module's §3 import budget is [stdlib, agent.serializer] (the
        # signal_config.py:205-208 precedent).
        rules_hash = hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False)
            .encode("utf-8")
        ).hexdigest()

        return cls(
            slippage_cap_bps=slippage_cap_bps,
            order_poll_interval_ms=order_poll_interval_ms,
            account_refresh_interval_ms=account_refresh,
            max_open_orders=max_open_orders,
            effective_latency_budget_ms=effective_latency_budget_ms,
            quote_staleness_ms_max=quote_staleness_ms_max,
            spread_bps_max=spread_bps_max,
            rules_hash=rules_hash,
        )
