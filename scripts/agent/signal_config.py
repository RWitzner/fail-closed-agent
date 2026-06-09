"""M3 §J — `SignalConfig`: the ONE parser of `agent_rules.signal` (contract rev2 REPO-F7).

DEPENDENCY-FREE within M3 (imports only stdlib + `agent.serializer`): both
`feature_engine` and `signal_snapshot` import THIS module, never each other, which
breaks the import cycle the critic pass flagged. `FEATURE_NAMES` therefore lives here
(re-exported by `feature_engine` so the contract-§C surface holds).

FD-7 posture: every value in the committed `agent_rules.signal` block is a string,
list of strings, or dict thereof — `config.tighten_only_merge` keeps base for all of
them, so overlays cannot alter any signal parameter at all. This parser converts the
strings ONCE into typed values and fails LOUD (ValueError) on any unknown/missing key
or malformed value — at startup, before any tick.
"""
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, Tuple

from agent.serializer import dumps

# Frozen order — also the model input order (contract §C/§F).
FEATURE_NAMES: Tuple[str, ...] = (
    "z_ret_21", "momentum_9", "momentum_21", "rsi14_centered",
    "ema_gap_9_21", "sma_gap_21_50", "realized_vol_21",
)

_TOP_KEYS = frozenset({
    "interval", "feature_windows", "rsi_period", "z_window", "vol_window",
    "horizons", "threshold_k", "spread_bps_max", "quote_staleness_ms_max",
    "feature_staleness_ms_max", "bar_lag_max_intervals", "refresh_cadence_ms",
    "prob_bins", "min_reference_samples", "model",
})
_MODEL_KEYS = frozenset({"model_version", "standardization", "coefficients"})
_COEFFICIENT_KEYS = frozenset(("intercept",) + FEATURE_NAMES)


def _require_str(block: dict, key: str, *, path: str) -> str:
    if key not in block:
        raise ValueError(f"{path}.{key}: missing required signal key")
    value = block[key]
    if not isinstance(value, str):
        raise ValueError(
            f"{path}.{key}: must be a string (FD-7), got {type(value).__name__}"
        )
    return value


def _parse_int(raw: str, *, path: str, positive: bool = True) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{path}: not an integer string: {raw!r}")
    if positive and value <= 0:
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


def _parse_horizon_minutes(horizon: str, *, path: str) -> int:
    """A horizon is `<int>m` — exact minute arithmetic only in M3."""
    if not horizon.endswith("m") or len(horizon) < 2:
        raise ValueError(f"{path}: horizon must be '<int>m', got {horizon!r}")
    return _parse_int(horizon[:-1], path=path)


@dataclass(frozen=True)
class SignalConfig:
    """Typed, validated view of `agent_rules.signal`. Immutable; carries the
    `model_artifact_hash` = sha256 of the canonical JSON of the model block (FD-4)."""

    interval: str
    feature_windows: Tuple[int, ...]
    rsi_period: int
    z_window: int
    vol_window: int
    horizons: Tuple[str, ...]
    horizon_minutes: Dict[str, int]
    threshold_k: Decimal
    spread_bps_max: Decimal
    quote_staleness_ms_max: int
    feature_staleness_ms_max: int
    bar_lag_max_intervals: int
    refresh_cadence_ms: int
    prob_bins: int
    min_reference_samples: int
    model_version: str
    standardization: str
    coefficients: Dict[str, Dict[str, Decimal]]
    model_artifact_hash: str
    rules_hash: str                # of the WHOLE assembled config (config.py:17 semantics)

    @classmethod
    def from_config(cls, config: dict) -> "SignalConfig":
        if not isinstance(config, dict) or "signal" not in config:
            raise ValueError("config has no 'signal' block")
        signal = config["signal"]
        if not isinstance(signal, dict):
            raise ValueError("'signal' must be a dict")

        unknown = set(signal) - _TOP_KEYS
        if unknown:
            raise ValueError(f"signal: unknown keys {sorted(unknown)}")
        missing = _TOP_KEYS - set(signal)
        if missing:
            raise ValueError(f"signal: missing keys {sorted(missing)}")

        interval = _require_str(signal, "interval", path="signal")
        if interval != "1m":
            raise ValueError(f"signal.interval: only '1m' is built in M3 (FD-1), got {interval!r}")

        # FROZEN-NAME/WINDOW COUPLING (harden round 1, M3-R1-004): the v1 feature
        # NAMES hard-bind the windows (z_ret_21, momentum_9/21, rsi14_centered,
        # ema_gap_9_21, sma_gap_21_50, realized_vol_21). A config that changes a
        # window without renaming the model is silently-wrong — fail LOUD instead.
        frozen = {"feature_windows": ["9", "21", "50"], "rsi_period": "14",
                  "z_window": "21", "vol_window": "21"}
        for key, expected in frozen.items():
            if signal.get(key) != expected:
                raise ValueError(
                    f"signal.{key}: frozen to {expected!r} for model logit-mom-v1 "
                    f"(the FEATURE_NAMES encode these windows), got {signal.get(key)!r}")

        raw_windows = signal["feature_windows"]
        if not isinstance(raw_windows, list) or not raw_windows:
            raise ValueError("signal.feature_windows: must be a non-empty list of strings")
        windows = []
        for i, raw in enumerate(raw_windows):
            if not isinstance(raw, str):
                raise ValueError(f"signal.feature_windows[{i}]: must be a string (FD-7)")
            windows.append(_parse_int(raw, path=f"signal.feature_windows[{i}]"))

        raw_horizons = signal["horizons"]
        if not isinstance(raw_horizons, list) or not raw_horizons:
            raise ValueError("signal.horizons: must be a non-empty list of strings")
        horizons = []
        horizon_minutes: Dict[str, int] = {}
        for i, raw in enumerate(raw_horizons):
            if not isinstance(raw, str):
                raise ValueError(f"signal.horizons[{i}]: must be a string (FD-7)")
            horizon_minutes[raw] = _parse_horizon_minutes(raw, path=f"signal.horizons[{i}]")
            horizons.append(raw)
        if len(set(horizons)) != len(horizons):
            raise ValueError("signal.horizons: duplicate horizon")

        prob_bins = _parse_int(_require_str(signal, "prob_bins", path="signal"), path="signal.prob_bins")
        if prob_bins != 10:
            raise ValueError(f"signal.prob_bins: must be '10' in M3 (FD-11), got {prob_bins}")

        model = signal["model"]
        if not isinstance(model, dict):
            raise ValueError("signal.model: must be a dict")
        unknown = set(model) - _MODEL_KEYS
        if unknown:
            raise ValueError(f"signal.model: unknown keys {sorted(unknown)}")
        missing = _MODEL_KEYS - set(model)
        if missing:
            raise ValueError(f"signal.model: missing keys {sorted(missing)}")
        model_version = _require_str(model, "model_version", path="signal.model")
        standardization = _require_str(model, "standardization", path="signal.model")
        if standardization != "identity":
            raise ValueError(
                "signal.model.standardization: frozen to 'identity' (FD-4), "
                f"got {standardization!r}"
            )

        raw_coefficients = model["coefficients"]
        if not isinstance(raw_coefficients, dict):
            raise ValueError("signal.model.coefficients: must be a dict")
        if set(raw_coefficients) != set(horizons):
            raise ValueError(
                "signal.model.coefficients: horizon keys "
                f"{sorted(raw_coefficients)} != configured horizons {sorted(horizons)}"
            )
        coefficients: Dict[str, Dict[str, Decimal]] = {}
        for horizon, coeff_block in raw_coefficients.items():
            path = f"signal.model.coefficients.{horizon}"
            if not isinstance(coeff_block, dict):
                raise ValueError(f"{path}: must be a dict")
            if set(coeff_block) != _COEFFICIENT_KEYS:
                raise ValueError(
                    f"{path}: keys must be exactly ('intercept',) + FEATURE_NAMES; "
                    f"got {sorted(coeff_block)}"
                )
            coefficients[horizon] = {
                name: _parse_decimal(
                    _require_str(coeff_block, name, path=path), path=f"{path}.{name}"
                )
                for name in coeff_block
            }

        # FD-4: model_artifact_hash = sha256 of the canonical JSON of the model block.
        model_artifact_hash = hashlib.sha256(dumps(model).encode("utf-8")).hexdigest()
        # rules_hash over the WHOLE assembled config (mirrors agent.config.rules_hash
        # exactly — allow_nan=False fail-loud); carried onto every snapshot/row.
        rules_hash = hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False)
            .encode("utf-8")
        ).hexdigest()

        return cls(
            interval=interval,
            feature_windows=tuple(windows),
            rsi_period=_parse_int(_require_str(signal, "rsi_period", path="signal"), path="signal.rsi_period"),
            z_window=_parse_int(_require_str(signal, "z_window", path="signal"), path="signal.z_window"),
            vol_window=_parse_int(_require_str(signal, "vol_window", path="signal"), path="signal.vol_window"),
            horizons=tuple(horizons),
            horizon_minutes=horizon_minutes,
            threshold_k=_parse_decimal(_require_str(signal, "threshold_k", path="signal"), path="signal.threshold_k"),
            spread_bps_max=_parse_decimal(_require_str(signal, "spread_bps_max", path="signal"), path="signal.spread_bps_max"),
            quote_staleness_ms_max=_parse_int(_require_str(signal, "quote_staleness_ms_max", path="signal"), path="signal.quote_staleness_ms_max"),
            feature_staleness_ms_max=_parse_int(_require_str(signal, "feature_staleness_ms_max", path="signal"), path="signal.feature_staleness_ms_max"),
            bar_lag_max_intervals=_parse_int(_require_str(signal, "bar_lag_max_intervals", path="signal"), path="signal.bar_lag_max_intervals"),
            refresh_cadence_ms=_parse_int(_require_str(signal, "refresh_cadence_ms", path="signal"), path="signal.refresh_cadence_ms"),
            prob_bins=prob_bins,
            min_reference_samples=_parse_int(_require_str(signal, "min_reference_samples", path="signal"), path="signal.min_reference_samples"),
            model_version=model_version,
            standardization=standardization,
            coefficients=coefficients,
            model_artifact_hash=model_artifact_hash,
            rules_hash=rules_hash,
        )
