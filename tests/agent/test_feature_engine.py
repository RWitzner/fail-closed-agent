"""M3 §M.3 — FeatureEngine: frozen feature math, FD-14 boundary, S3 exclusion. [S2, S3]

Golden numbers are computed in-test by an INDEPENDENT straightforward implementation
(statistics module + explicit loops) of the §C frozen procedures, then pushed through
the same boundary quantization — pinning the engine to the contract, not to itself.
"""
import math
import statistics
import unittest
from decimal import Decimal, ROUND_HALF_EVEN

from agent.bar_series import MidBar, MidBarSeriesReader
from agent.feature_engine import (
    FEATURE_NAMES,
    FEATURE_QUANTUM,
    FeatureEngine,
    FeatureView,
    sanitize_feature,
)
from agent.forecast import NonFiniteFeature
from agent.signal_config import SignalConfig
from agent import config as agent_config
from pathlib import Path

from tests.lib.fakes import FakeClock

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = agent_config.load(REPO_ROOT / "config" / "agent_rules.json")
SIGNAL = SignalConfig.from_config(CONFIG)
PIN = "EQUS.MINI:tbbo:1m:fixture:test-v1"


def _bars(mids, *, start_minute=0, symbol="AAPL", instrument_id=1001,
          watermark_offset_s=59):
    """1m bars on 2026-06-15 starting 13:30 UTC + start_minute, mid path given."""
    bars = []
    for i, mid in enumerate(mids):
        minute = start_minute + i
        hh = 13 + (30 + minute) // 60
        mm = (30 + minute) % 60
        start = f"2026-06-15T{hh:02d}:{mm:02d}:00.000000Z"
        end_minute = minute + 1
        ehh = 13 + (30 + end_minute) // 60
        emm = (30 + end_minute) % 60
        end = f"2026-06-15T{ehh:02d}:{emm:02d}:00.000000Z"
        watermark = f"2026-06-15T{hh:02d}:{mm:02d}:{watermark_offset_s:02d}.000000Z"
        mid_d = Decimal(mid).quantize(Decimal("0.000001"))
        bars.append(MidBar(
            symbol=symbol, instrument_id=instrument_id, interval="1m",
            bucket_start_utc=start, bucket_end_utc=end, session_date_et="2026-06-15",
            bid=mid_d, ask=mid_d, mid=mid_d, watermark_utc=watermark,
            source_dataset="EQUS.MINI", source_schema="tbbo", data_pin=PIN,
            quote_provenance={"ts_event_utc": start, "ts_recv_utc": watermark,
                              "reconnect_epoch": 0, "vendor_seq": i},
        ))
    return bars


def _mids_path(n=51):
    """A deterministic, non-trivial positive path."""
    mids = []
    px = 100.0
    for i in range(n):
        px = px * (1.0 + (0.0008 if (i * 7) % 3 == 0 else -0.0005) + 0.0001 * ((i * 13) % 5))
        mids.append(f"{px:.4f}")
    return mids


def _expected_features(closes):
    """Independent reference implementation of §C (floats; quantized at the end)."""
    c = [float(x) for x in closes]
    r = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]

    def sma(values, w):
        return sum(values[-w:]) / w

    def ema(values, w):
        seed = sum(values[:w]) / w
        alpha = 2.0 / (w + 1)
        out = seed
        for v in values[w:]:
            out = alpha * v + (1 - alpha) * out
        return out

    window = r[-21:]
    mu = statistics.fmean(window)
    sigma = statistics.pstdev(window)
    z = 0.0 if sigma == 0 else (r[-1] - mu) / sigma

    deltas = [c[i] - c[i - 1] for i in range(1, len(c))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:14]) / 14
    avg_loss = sum(losses[:14]) / 14
    for g, l in zip(gains[14:], losses[14:]):
        avg_gain = (avg_gain * 13 + g) / 14
        avg_loss = (avg_loss * 13 + l) / 14
    if avg_gain == 0 and avg_loss == 0:
        rsi = 50.0
    elif avg_loss == 0:
        rsi = 100.0
    elif avg_gain == 0:
        rsi = 0.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - 100.0 / (1.0 + rs)

    raw = {
        "z_ret_21": z,
        "momentum_9": c[-1] / c[-1 - 9] - 1,
        "momentum_21": c[-1] / c[-1 - 21] - 1,
        "rsi14_centered": (rsi - 50.0) / 50.0,
        "ema_gap_9_21": (ema(c, 9) - ema(c, 21)) / c[-1],
        "sma_gap_21_50": (sma(c, 21) - sma(c, 50)) / c[-1],
        "realized_vol_21": sigma,
    }
    return {
        name: str(Decimal(repr(value)).quantize(FEATURE_QUANTUM, ROUND_HALF_EVEN))
        for name, value in raw.items()
    }


class TestFeatureMath(unittest.TestCase):
    def setUp(self):
        self.mids = _mids_path(51)
        self.reader = MidBarSeriesReader(_bars(self.mids))
        self.engine = FeatureEngine(reader=self.reader, config=SIGNAL,
                                    clock=FakeClock(start_ms=10_000))
        self.as_of = "2026-06-15T14:30:00.000000Z"  # past all 51 bars + watermarks

    def test_matches_independent_reference_implementation(self):
        snapshot = self.engine.compute(symbol="AAPL", instrument_id=1001,
                                       as_of_utc=self.as_of)
        self.assertTrue(snapshot.available)
        self.assertEqual(snapshot.n_bars, 51)
        self.assertEqual(set(snapshot.features), set(FEATURE_NAMES))
        expected = _expected_features(self.mids)
        self.assertEqual(snapshot.features, expected)

    def test_all_values_quantized_to_8dp_strings(self):
        snapshot = self.engine.compute(symbol="AAPL", instrument_id=1001,
                                       as_of_utc=self.as_of)
        for name, value in snapshot.features.items():
            self.assertIsInstance(value, str)
            self.assertEqual(Decimal(value).as_tuple().exponent, -8, name)

    def test_snapshot_metadata(self):
        snapshot = self.engine.compute(symbol="AAPL", instrument_id=1001,
                                       as_of_utc=self.as_of)
        self.assertEqual(snapshot.feature_cutoff_bar_end_utc,
                         "2026-06-15T14:21:00.000000Z")  # 51st bar ends 13:30+51m
        self.assertEqual(snapshot.interval, "1m")
        self.assertEqual(snapshot.data_pin, PIN)
        self.assertEqual(snapshot.rules_hash, SIGNAL.rules_hash)
        self.assertEqual(snapshot.refreshed_at_ms, 10_000)
        self.assertTrue(snapshot.feature_snapshot_id.startswith("fs-"))

    def test_feature_snapshot_id_deterministic(self):
        one = self.engine.compute(symbol="AAPL", instrument_id=1001, as_of_utc=self.as_of)
        two = self.engine.compute(symbol="AAPL", instrument_id=1001, as_of_utc=self.as_of)
        self.assertEqual(one.feature_snapshot_id, two.feature_snapshot_id)


class TestAvailabilityBoundary(unittest.TestCase):
    def test_50_bars_unavailable_51_available(self):
        as_of = "2026-06-15T14:30:00.000000Z"
        fifty = FeatureEngine(reader=MidBarSeriesReader(_bars(_mids_path(50))),
                              config=SIGNAL, clock=FakeClock())
        snap50 = fifty.compute(symbol="AAPL", instrument_id=1001, as_of_utc=as_of)
        self.assertFalse(snap50.available)
        self.assertEqual(snap50.features, {})
        self.assertEqual(snap50.n_bars, 50)

        fiftyone = FeatureEngine(reader=MidBarSeriesReader(_bars(_mids_path(51))),
                                 config=SIGNAL, clock=FakeClock())
        snap51 = fiftyone.compute(symbol="AAPL", instrument_id=1001, as_of_utc=as_of)
        self.assertTrue(snap51.available)

    def test_zero_bars_yields_null_fields_and_deterministic_id(self):
        engine = FeatureEngine(reader=MidBarSeriesReader([]), config=SIGNAL,
                               clock=FakeClock())
        snap = engine.compute(symbol="AAPL", instrument_id=1001,
                              as_of_utc="2026-06-15T14:30:00.000000Z")
        self.assertFalse(snap.available)
        self.assertEqual(snap.n_bars, 0)
        self.assertIsNone(snap.feature_cutoff_bar_end_utc)
        self.assertIsNone(snap.watermark_utc)
        again = engine.compute(symbol="AAPL", instrument_id=1001,
                               as_of_utc="2026-06-15T14:30:00.000000Z")
        self.assertEqual(snap.feature_snapshot_id, again.feature_snapshot_id)

    def test_zero_variance_series_all_defined_finite(self):
        engine = FeatureEngine(
            reader=MidBarSeriesReader(_bars(["100.0000"] * 51)),
            config=SIGNAL, clock=FakeClock(),
        )
        snap = engine.compute(symbol="AAPL", instrument_id=1001,
                              as_of_utc="2026-06-15T14:30:00.000000Z")
        self.assertTrue(snap.available)
        self.assertEqual(Decimal(snap.features["z_ret_21"]), Decimal("0"))
        self.assertEqual(Decimal(snap.features["realized_vol_21"]), Decimal("0"))
        # both-zero Wilder guard (rev2 MATH-Q3): rsi=50 -> centered exactly 0.
        self.assertEqual(Decimal(snap.features["rsi14_centered"]), Decimal("0"))
        for value in snap.features.values():
            self.assertTrue(Decimal(value).is_finite())


class TestS3Exclusion(unittest.TestCase):
    def test_future_received_bar_excluded_and_features_identical(self):
        mids = _mids_path(52)
        clean = _bars(mids[:51])
        # bar #52 is future-received: watermark 30 minutes after its bucket.
        leaked = _bars(mids, watermark_offset_s=59)
        leaked[51] = MidBar(
            **{**leaked[51].__dict__, "watermark_utc": "2026-06-15T15:00:00.000000Z"}
        )
        as_of = "2026-06-15T14:22:30.000000Z"  # after bar 52's bucket end (14:22), before its watermark
        snap_clean = FeatureEngine(
            reader=MidBarSeriesReader(clean), config=SIGNAL, clock=FakeClock()
        ).compute(symbol="AAPL", instrument_id=1001, as_of_utc=as_of)
        snap_leaked = FeatureEngine(
            reader=MidBarSeriesReader(leaked), config=SIGNAL, clock=FakeClock()
        ).compute(symbol="AAPL", instrument_id=1001, as_of_utc=as_of)
        self.assertEqual(snap_clean.features, snap_leaked.features)
        self.assertEqual(snap_clean.feature_cutoff_bar_end_utc,
                         snap_leaked.feature_cutoff_bar_end_utc)


class TestBoundaryInjection(unittest.TestCase):
    def test_nonfinite_injection_raises(self):
        with self.assertRaises(NonFiniteFeature):
            sanitize_feature("z_ret_21", float("inf"))
        with self.assertRaises(NonFiniteFeature):
            sanitize_feature("z_ret_21", float("nan"))

    def test_window_helpers_guard_short_input(self):
        # harden round 1, M3-05: silent wrong-divisor SMA/EMA/RSI is impossible.
        from agent.feature_engine import _ema, _sma, _wilder_rsi
        with self.assertRaises(ValueError):
            _sma([1.0, 2.0], 9)
        with self.assertRaises(ValueError):
            _ema([1.0, 2.0], 9)
        with self.assertRaises(ValueError):
            _wilder_rsi([1.0] * 14, 14)

    def test_sanitize_immune_to_ambient_decimal_context(self):
        # harden round 1, M3-R4.
        import decimal
        baseline = sanitize_feature("momentum_9", 0.001234567849)
        original = decimal.getcontext()
        try:
            decimal.setcontext(decimal.Context(prec=4, rounding=decimal.ROUND_DOWN))
            self.assertEqual(sanitize_feature("momentum_9", 0.001234567849), baseline)
        finally:
            decimal.setcontext(original)

    def test_finite_value_sanitizes_to_8dp_string(self):
        self.assertEqual(sanitize_feature("momentum_9", 0.001234567849),
                         "0.00123457")


class TestFeatureView(unittest.TestCase):
    def test_refresh_and_latest(self):
        engine = FeatureEngine(reader=MidBarSeriesReader(_bars(_mids_path(51))),
                               config=SIGNAL, clock=FakeClock())
        view = FeatureView(engine=engine, clock=FakeClock())
        self.assertIsNone(view.latest("AAPL", 1001))
        snap = view.refresh(symbol="AAPL", instrument_id=1001,
                            as_of_utc="2026-06-15T14:30:00.000000Z")
        self.assertIs(view.latest("AAPL", 1001), snap)
        self.assertIsNone(view.latest("MSFT", 2002))


if __name__ == "__main__":
    unittest.main()
