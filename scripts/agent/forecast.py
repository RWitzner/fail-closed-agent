"""M3 §F — stable logistic over the frozen scale-free input vector, config-pinned
coefficients (FD-4). Float internals; ONE sanitized boundary to Decimal.

Frozen semantics (contract §F, rev2):
- `coefficients` keys = ("intercept",) + FEATURE_NAMES exactly (missing/extra -> ValueError).
- z = b0 + Σ b_i·x_i in float; z clamped to [-30, +30]; branch-stable sigmoid.
- Boundary (rev2 MATH-Q7, mirrors §C verbatim): p_float passes math.isfinite, then
  Decimal(repr(p_float)).quantize(PROB_QUANTUM, ROUND_HALF_EVEN), then clamp to
  [P_MIN, P_MAX] (closed) — 0 < p < 1 always. Quantize-only (p is float-born).
- Inputs re-validated: every FEATURE_NAMES value parses to a finite float, else
  NonFiniteFeature.
"""
import math
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Dict

from agent.signal_config import FEATURE_NAMES

# Pinned context for the float->Decimal boundary (harden round 1, M3-R4).
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)

PROB_QUANTUM = Decimal("0.000001")
P_MIN, P_MAX = Decimal("0.000001"), Decimal("0.999999")   # FD-5 clamp (closed interval)
Z_CLAMP = 30.0


class NonFiniteFeature(ValueError):
    """A non-finite value reached the feature/forecast boundary (S2)."""


@dataclass(frozen=True)
class ForecastEvent:
    horizon: str                     # "5m" | "30m"
    threshold_k: Decimal             # default 0
    event_start_bar_end_utc: str
    resolve_bar_end_utc: str


@dataclass(frozen=True)
class Forecast:
    event: ForecastEvent
    p: Decimal                       # quantized PROB_QUANTUM, clamped to [P_MIN, P_MAX]
    model_version: str
    model_artifact_hash: str


def _sigmoid(z: float) -> float:
    """Branch-stable: never exponentiates a large positive argument."""
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def predict(features: Dict[str, str], *, coefficients: Dict[str, "Decimal"],
            model_version: str, model_artifact_hash: str,
            event: ForecastEvent) -> Forecast:
    expected_keys = set(("intercept",) + FEATURE_NAMES)
    if set(coefficients) != expected_keys:
        raise ValueError(
            f"coefficients keys must be exactly ('intercept',)+FEATURE_NAMES; "
            f"got {sorted(coefficients)}"
        )

    z = float(coefficients["intercept"])
    for name in FEATURE_NAMES:
        if name not in features:
            raise NonFiniteFeature(f"feature {name!r} missing from input vector")
        try:
            value = float(features[name])
        except (TypeError, ValueError):
            raise NonFiniteFeature(f"feature {name!r} unparseable: {features[name]!r}")
        if not math.isfinite(value):
            raise NonFiniteFeature(f"feature {name!r} non-finite: {features[name]!r}")
        z += float(coefficients[name]) * value

    z = max(-Z_CLAMP, min(Z_CLAMP, z))
    p_float = _sigmoid(z)
    if not math.isfinite(p_float):  # unreachable after the clamp; belt-and-suspenders
        raise NonFiniteFeature(f"sigmoid produced non-finite p from z={z!r}")
    with localcontext(_DECIMAL_CTX):
        p = Decimal(repr(p_float)).quantize(PROB_QUANTUM, ROUND_HALF_EVEN)
    if p < P_MIN:
        p = P_MIN
    elif p > P_MAX:
        p = P_MAX

    return Forecast(event=event, p=p, model_version=model_version,
                    model_artifact_hash=model_artifact_hash)
