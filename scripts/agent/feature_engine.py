"""M3 §C — FeatureEngine: rolling per-(symbol, instrument_id) mid-bar history and the
frozen feature vector as-of an explicit UTC instant. `scan()` never touches this class
— the probe reads the immutable FeatureSnapshot a `FeatureView.refresh` produced.

Frozen math (contract §C rev2): float internals, ONE sanitized boundary
(`math.isfinite` then `Decimal(repr(v)).quantize(FEATURE_QUANTUM, ROUND_HALF_EVEN)`).
- r_t = ln(c_t/c_{t-1}); closes c := mids (FD-3).
- SMA_w = arithmetic mean of last w closes. EMA_w seeded with SMA over the OLDEST w
  closes of the 51-bar window, then recursive alpha=2/(w+1).
- momentum_w = c_t/c_{t-w} - 1.
- Wilder RSI(14): seed = simple means of first 14 gains/losses, then Wilder smoothing;
  guards on the FINAL averages: both zero => 50 (rev2 MATH-Q3); avg_loss==0 => 100;
  avg_gain==0 => 0. rsi14_centered = (rsi14-50)/50.
- z_ret_21 / realized_vol_21: population stats (ddof=0) over the 21 most recent
  returns INCLUSIVE of r_t (rev2 MATH-Q2); std==0 => z=0, vol=0 (FD-14).
- n_bars < max(windows)+1 = 51 => available=False, features={} (S2: nothing
  half-computed leaks); n_bars == 0 => both timestamp fields None (rev2 BUILD-F16).
"""
import math
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Dict, Optional

from agent.bar_series import MidBarSeriesReader
from agent.forecast import NonFiniteFeature
from agent.serializer import row_hash
from agent.signal_config import FEATURE_NAMES, SignalConfig

__all__ = [
    "FEATURE_NAMES", "FEATURE_QUANTUM", "FeatureSnapshot", "FeatureEngine",
    "FeatureView", "sanitize_feature",
]

FEATURE_QUANTUM = Decimal("0.00000001")

# Pinned context: the boundary quantize must not depend on (or raise under) a
# caller-mutated ambient decimal context (harden round 1, M3-R4).
_DECIMAL_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


def sanitize_feature(name: str, value: float) -> str:
    """The S2 float->Decimal boundary chokepoint. Non-finite -> NonFiniteFeature
    (guards upstream make this unreachable; the injection test asserts the raise)."""
    if not math.isfinite(value):
        raise NonFiniteFeature(f"feature {name!r} computed non-finite: {value!r}")
    with localcontext(_DECIMAL_CTX):
        return str(Decimal(repr(value)).quantize(FEATURE_QUANTUM, ROUND_HALF_EVEN))


@dataclass(frozen=True)
class FeatureSnapshot:
    symbol: str
    instrument_id: int
    interval: str
    feature_cutoff_bar_end_utc: Optional[str]  # None iff n_bars == 0 (rev2 BUILD-F16)
    watermark_utc: Optional[str]               # None iff n_bars == 0
    features: Dict[str, str]                   # name -> Decimal-string, FEATURE_QUANTUM
    available: bool                            # False iff history < 51 bars (FD-14)
    n_bars: int
    data_pin: str
    rules_hash: str
    feature_snapshot_id: str                   # "fs-" + row_hash(canonical §I input)
    refreshed_at_ms: int                       # injected monotonic stamp


def _population_std(values) -> float:
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def _sma(closes, w: int) -> float:
    if len(closes) < w:  # defensive: FD-14's 51-bar gate makes this unreachable
        raise ValueError(f"SMA_{w} requires >= {w} closes, got {len(closes)}")
    return sum(closes[-w:]) / w


def _ema(closes, w: int) -> float:
    if len(closes) < w:  # defensive (harden round 1, M3-05)
        raise ValueError(f"EMA_{w} requires >= {w} closes, got {len(closes)}")
    seed = sum(closes[:w]) / w
    alpha = 2.0 / (w + 1)
    out = seed
    for value in closes[w:]:
        out = alpha * value + (1.0 - alpha) * out
    return out


def _wilder_rsi(closes, period: int) -> float:
    if len(closes) < period + 1:  # defensive (harden round 1, M3-05)
        raise ValueError(f"RSI({period}) requires >= {period + 1} closes, got {len(closes)}")
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    # Guards on the FINAL Wilder-smoothed averages (rev2 MATH-Q3).
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


class FeatureEngine:
    """Pure given (reader, config, clock): no wall clock, no I/O."""

    def __init__(self, *, reader: MidBarSeriesReader, config: SignalConfig, clock) -> None:
        self._reader = reader
        self._config = config
        self._clock = clock
        self._required_bars = max(config.feature_windows) + 1  # FD-14: 51

    def compute(self, *, symbol: str, instrument_id: int, as_of_utc: str) -> FeatureSnapshot:
        bars = self._reader.eligible_history(
            symbol, instrument_id, as_of_utc=as_of_utc, max_bars=self._required_bars
        )
        n_bars = len(bars)
        available = n_bars >= self._required_bars
        latest = bars[-1] if bars else None

        features: Dict[str, str] = {}
        if available:
            closes = [float(bar.mid) for bar in bars]
            returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]

            z_window = returns[-self._config.z_window:]          # inclusive of r_t
            sigma = _population_std(z_window)
            r_t = returns[-1]
            mean = sum(z_window) / len(z_window)
            z_ret = 0.0 if sigma == 0.0 else (r_t - mean) / sigma

            vol_window = returns[-self._config.vol_window:]
            realized_vol = _population_std(vol_window)

            rsi = _wilder_rsi(closes, self._config.rsi_period)

            raw = {
                "z_ret_21": z_ret,
                "momentum_9": closes[-1] / closes[-1 - 9] - 1.0,
                "momentum_21": closes[-1] / closes[-1 - 21] - 1.0,
                "rsi14_centered": (rsi - 50.0) / 50.0,
                "ema_gap_9_21": (_ema(closes, 9) - _ema(closes, 21)) / closes[-1],
                "sma_gap_21_50": (_sma(closes, 21) - _sma(closes, 50)) / closes[-1],
                "realized_vol_21": realized_vol,
            }
            features = {name: sanitize_feature(name, raw[name]) for name in FEATURE_NAMES}

        cutoff = latest.bucket_end_utc if latest is not None else None
        watermark = latest.watermark_utc if latest is not None else None
        data_pin = latest.data_pin if latest is not None else ""
        snapshot_id = "fs-" + row_hash({
            "symbol": symbol,
            "instrument_id": instrument_id,
            "interval": self._config.interval,
            "feature_cutoff_bar_end_utc": cutoff,
            "watermark_utc": watermark,
            "features": features,
            "data_pin": data_pin,
            "rules_hash": self._config.rules_hash,
        })
        return FeatureSnapshot(
            symbol=symbol,
            instrument_id=instrument_id,
            interval=self._config.interval,
            feature_cutoff_bar_end_utc=cutoff,
            watermark_utc=watermark,
            features=features,
            available=available,
            n_bars=n_bars,
            data_pin=data_pin,
            rules_hash=self._config.rules_hash,
            feature_snapshot_id=snapshot_id,
            refreshed_at_ms=self._clock.now_ms(),
        )


class FeatureView:
    """The probe-facing cached view (background-refresh owner; M3 tests drive
    refresh() explicitly — scheduling is the M5 runner's job, §N)."""

    def __init__(self, *, engine: FeatureEngine, clock) -> None:
        self._engine = engine
        self._clock = clock
        self._latest: Dict[tuple, FeatureSnapshot] = {}

    def refresh(self, *, symbol: str, instrument_id: int, as_of_utc: str) -> FeatureSnapshot:
        snapshot = self._engine.compute(
            symbol=symbol, instrument_id=instrument_id, as_of_utc=as_of_utc
        )
        self._latest[(symbol, instrument_id)] = snapshot
        return snapshot

    def latest(self, symbol: str, instrument_id: int) -> Optional[FeatureSnapshot]:
        return self._latest.get((symbol, instrument_id))
