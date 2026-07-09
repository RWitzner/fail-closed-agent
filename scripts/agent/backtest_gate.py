"""M5 §L.2 — backtest-artifact gate (FD-M5-27; S9).

The artifact is committed + reviewed + hash-bound (no crypto in M5):
`artifacts/backtests/<strategy_id>.json` = `{v, strategy_id, rules_hash, data_pin,
metrics{..., basis: "execution_realistic_pnl"}, created_utc, artifact_hash}` with
`artifact_hash = serializer.row_hash(payload minus artifact_hash)`. M5 ships the
verifier + an EMPTY dir (`.gitkeep`) ⇒ every real strategy rejects fail-closed
(`backtest_artifact_missing`, FD-M5-8); M7 owns artifact production.

`data_pin` uses the M3 frozen format `"{dataset}:{schema}:{interval}:{source_id}"`
(M3 contract §I) and is compared byte-exact — any drift re-closes the gate.

Fail-closed posture: `verify_artifact` NEVER raises on data — an absent file is
`missing`; unreadable / malformed JSON / wrong shape / tampered hash / wrong
metric basis all degrade to `hash_invalid`; a verified-but-stale key triple is
`key_mismatch`. Module-scope imports remain stdlib + `agent.serializer` ONLY
(§3); the canonical paper-phase evaluator is loaded lazily after structural
validation and a successful key-triple match.
"""
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from agent.serializer import row_hash

ARTIFACTS_DIR = "artifacts/backtests"     # shipped EMPTY (.gitkeep) — FD-M5-8

_STATUSES = frozenset({"ok", "missing", "key_mismatch", "hash_invalid"})

# FD-M5-27 frozen payload shape — exactly these keys, nothing else.
_PAYLOAD_KEYS = frozenset({
    "v", "strategy_id", "rules_hash", "data_pin", "metrics", "created_utc",
    "artifact_hash"})

_REQUIRED_BASIS = "execution_realistic_pnl"    # the S9 metric pin
_V2_METRIC_KEYS = frozenset({
    "basis", "pass", "runner_version", "strategy_version", "sample", "pnl",
    "benchmark", "risk", "quality", "thresholds", "provenance",
})
_V2_SAMPLE_KEYS = frozenset({
    "start_utc", "end_utc", "session_count", "decision_count", "trade_count",
    "traded_session_count", "symbols",
})
_V2_PNL_KEYS = frozenset({
    "gross_modeled_usd", "fees_usd", "net_execution_realistic_pnl_usd",
    "avg_trade_bps", "profit_factor",
})
_V2_BENCHMARK_KEYS = frozenset({
    "method", "benchmark_pnl_usd", "active_pnl_usd",
})
_V2_RISK_KEYS = frozenset({
    "max_drawdown_usd", "max_drawdown_pct_allocated", "worst_day_usd",
    "worst_day_pct_allocated", "p95_realism_gap_bps",
    "max_single_fill_divergence_bps",
})
_V2_QUALITY_KEYS = frozenset({
    "future_receipt_count", "missing_bar_count", "ca_blackout_skips",
    "data_quality_skip_count", "unresolved_reconcile_drift_count",
    "s1_canary_breach_count", "live_broker_submit_count",
    "artifact_mismatch_count", "unhandled_exception_count",
})
_V2_THRESHOLD_KEYS = frozenset({
    "min_sessions", "min_trades", "min_traded_sessions",
    "require_positive_net_pnl", "require_positive_active_pnl",
    "profit_factor_min", "max_drawdown_pct_allocated",
    "worst_day_pct_allocated", "p95_realism_gap_bps_max",
    "max_single_fill_divergence_bps",
})
_V2_PROVENANCE_REQUIRED_KEYS = frozenset({
    "input_manifest_hash", "builder_git_commit", "tier",
})
_V2_PROVENANCE_OPTIONAL_KEYS = frozenset({
    "normalizer_id", "calendar_pin", "fee_model_version",
    "pricing_model_version", "realism_gap_model_version",
    "latency_budget_ms", "slippage_cap_bps", "universe_hypothesis_id",
    "universe_selection_rule", "universe_symbols", "horizon",
    # M7c phase-1 cross-sectional: the equal-weight-long benchmark attribution
    # (FD-P1-9, additional provenance only — the pinned benchmark below is
    # unchanged) and a canonical-JSON breadth/leg diagnostics string for the
    # Phase Gate. All values remain strings (see the _string check below).
    "universe_equal_weight_long_benchmark",
    "universe_equal_weight_long_benchmark_pnl_usd",
    "universe_equal_weight_long_active_pnl_usd",
    "cross_sectional_diagnostics",
})
_PINNED_MIN_SESSIONS = 20
_PINNED_MIN_TRADES = 30
_PINNED_MIN_TRADED_SESSIONS = 5
_PINNED_PROFIT_FACTOR_MIN = Decimal("1.10")
_PINNED_MAX_DRAWDOWN_PCT_ALLOCATED = Decimal("0.0150")
_PINNED_WORST_DAY_PCT_ALLOCATED = Decimal("0.0075")
_PINNED_P95_REALISM_GAP_BPS_MAX = Decimal("15")
_PINNED_MAX_SINGLE_FILL_DIVERGENCE_BPS = Decimal("50")


def _finite_decimal_string(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return False
    return parsed.is_finite()


def _nonnegative_int(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _string(value) -> bool:
    return isinstance(value, str) and value != ""


def _decimal_value(value):
    if not _finite_decimal_string(value):
        return None
    return Decimal(value)


def _valid_v2_metrics(metrics: dict, *, strategy_id: str) -> bool:
    if not isinstance(metrics, dict) or set(metrics) != _V2_METRIC_KEYS:
        return False
    if metrics.get("basis") != _REQUIRED_BASIS:
        return False
    if metrics.get("pass") is not True:
        return False
    if not _string(metrics.get("runner_version")):
        return False
    if metrics.get("strategy_version") != strategy_id:
        return False

    sample = metrics["sample"]
    if not isinstance(sample, dict) or set(sample) != _V2_SAMPLE_KEYS:
        return False
    if not _string(sample["start_utc"]) or not _string(sample["end_utc"]):
        return False
    for key in ("session_count", "decision_count", "trade_count",
                "traded_session_count"):
        if not _nonnegative_int(sample[key]):
            return False
    symbols = sample["symbols"]
    if (not isinstance(symbols, list) or not symbols
            or any(not _string(symbol) for symbol in symbols)):
        return False

    pnl = metrics["pnl"]
    if not isinstance(pnl, dict) or set(pnl) != _V2_PNL_KEYS:
        return False
    if any(not _finite_decimal_string(pnl[key]) for key in _V2_PNL_KEYS):
        return False

    benchmark = metrics["benchmark"]
    if not isinstance(benchmark, dict) or set(benchmark) != _V2_BENCHMARK_KEYS:
        return False
    if benchmark["method"] != "exposure_matched_midbar_v1":
        return False
    for key in ("benchmark_pnl_usd", "active_pnl_usd"):
        if not _finite_decimal_string(benchmark[key]):
            return False

    risk = metrics["risk"]
    if not isinstance(risk, dict) or set(risk) != _V2_RISK_KEYS:
        return False
    if any(not _finite_decimal_string(risk[key]) for key in _V2_RISK_KEYS):
        return False

    quality = metrics["quality"]
    if not isinstance(quality, dict) or set(quality) != _V2_QUALITY_KEYS:
        return False
    if any(not _nonnegative_int(quality[key]) for key in _V2_QUALITY_KEYS):
        return False

    thresholds = metrics["thresholds"]
    if not isinstance(thresholds, dict) or set(thresholds) != _V2_THRESHOLD_KEYS:
        return False
    for key in ("min_sessions", "min_trades", "min_traded_sessions"):
        if not _nonnegative_int(thresholds[key]):
            return False
    if thresholds["min_sessions"] < _PINNED_MIN_SESSIONS:
        return False
    if thresholds["min_trades"] < _PINNED_MIN_TRADES:
        return False
    if thresholds["min_traded_sessions"] < _PINNED_MIN_TRADED_SESSIONS:
        return False
    for key in ("require_positive_net_pnl", "require_positive_active_pnl"):
        if thresholds[key] is not True:
            return False
    for key in ("profit_factor_min", "max_drawdown_pct_allocated",
                "worst_day_pct_allocated", "p95_realism_gap_bps_max",
                "max_single_fill_divergence_bps"):
        if not _finite_decimal_string(thresholds[key]):
            return False
    if _decimal_value(thresholds["profit_factor_min"]) < _PINNED_PROFIT_FACTOR_MIN:
        return False
    if (_decimal_value(thresholds["max_drawdown_pct_allocated"])
            > _PINNED_MAX_DRAWDOWN_PCT_ALLOCATED):
        return False
    if (_decimal_value(thresholds["worst_day_pct_allocated"])
            > _PINNED_WORST_DAY_PCT_ALLOCATED):
        return False
    if (_decimal_value(thresholds["p95_realism_gap_bps_max"])
            > _PINNED_P95_REALISM_GAP_BPS_MAX):
        return False
    if (_decimal_value(thresholds["max_single_fill_divergence_bps"])
            > _PINNED_MAX_SINGLE_FILL_DIVERGENCE_BPS):
        return False

    provenance = metrics["provenance"]
    if not isinstance(provenance, dict):
        return False
    keys = set(provenance)
    if not _V2_PROVENANCE_REQUIRED_KEYS <= keys:
        return False
    if keys - (_V2_PROVENANCE_REQUIRED_KEYS | _V2_PROVENANCE_OPTIONAL_KEYS):
        return False
    for key in keys:
        if key == "universe_symbols":
            symbols = provenance[key]
            if (not isinstance(symbols, list) or not symbols
                    or any(not _string(symbol) for symbol in symbols)):
                return False
            continue
        if not _string(provenance[key]):
            return False
    return True


@dataclass(frozen=True)
class ArtifactCheck:
    status: str                  # ∈ {"ok", "missing", "key_mismatch", "hash_invalid"}
    artifact_path: Optional[str]
    artifact_hash: Optional[str]

    def __post_init__(self):
        if self.status not in _STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_STATUSES)}, got {self.status!r}")


def _missing() -> ArtifactCheck:
    return ArtifactCheck(status="missing", artifact_path=None, artifact_hash=None)


def _hash_invalid(path: str, claimed_hash: Optional[str]) -> ArtifactCheck:
    return ArtifactCheck(status="hash_invalid", artifact_path=path,
                         artifact_hash=claimed_hash)


def verify_artifact(strategy_id: str, *, rules_hash: str, data_pin: str,
                    artifacts_dir: str = ARTIFACTS_DIR) -> ArtifactCheck:
    """FD-M5-27 verdict for `(strategy_id, rules_hash, data_pin)` against
    `<artifacts_dir>/<strategy_id>.json`. Production keeps the `ARTIFACTS_DIR`
    default; test BUILDERS take a mandatory artifacts_dir (M5C-S12)."""
    # Defensive: a path-traversal strategy_id cannot escape artifacts_dir; no
    # legitimate artifact can exist under such a name -> missing (fail-closed).
    if (not isinstance(strategy_id, str) or not strategy_id
            or "/" in strategy_id or "\\" in strategy_id or ".." in strategy_id):
        return _missing()
    path = os.path.join(artifacts_dir, strategy_id + ".json")
    if not os.path.isfile(path):
        return _missing()

    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        # unreadable or malformed JSON (UnicodeDecodeError/JSONDecodeError are
        # ValueError subclasses) -> hash_invalid, never a raise (fail-closed)
        return _hash_invalid(path, None)

    if not isinstance(payload, dict):
        return _hash_invalid(path, None)
    claimed_hash = payload.get("artifact_hash")
    if not isinstance(claimed_hash, str) or not claimed_hash:
        claimed_hash = None
    if set(payload) != _PAYLOAD_KEYS:
        return _hash_invalid(path, claimed_hash)
    if claimed_hash is None:
        return _hash_invalid(path, None)

    body = {key: value for key, value in payload.items() if key != "artifact_hash"}
    try:
        computed_hash = row_hash(body)
    except (TypeError, ValueError):
        # floats / non-serializable content in the payload -> hash_invalid
        return _hash_invalid(path, claimed_hash)
    if computed_hash != claimed_hash:
        return _hash_invalid(path, claimed_hash)

    version = payload["v"]
    if isinstance(version, bool) or version not in (1, 2):
        return _hash_invalid(path, claimed_hash)

    metrics = payload["metrics"]
    if not isinstance(metrics, dict) or metrics.get("basis") != _REQUIRED_BASIS:
        return _hash_invalid(path, claimed_hash)   # the S9 metric pin
    if version == 2 and not _valid_v2_metrics(
            metrics, strategy_id=payload["strategy_id"]):
        return _hash_invalid(path, claimed_hash)

    if (payload["strategy_id"], payload["rules_hash"], payload["data_pin"]) != (
            strategy_id, rules_hash, data_pin):
        return ArtifactCheck(status="key_mismatch", artifact_path=path,
                             artifact_hash=claimed_hash)
    if version == 2:
        from agent.paper_phase_criteria import evaluate_paper_phase_criteria
        if not evaluate_paper_phase_criteria(metrics).passed:
            return _hash_invalid(path, claimed_hash)

    return ArtifactCheck(status="ok", artifact_path=path, artifact_hash=claimed_hash)
