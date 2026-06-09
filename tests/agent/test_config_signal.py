"""M3 §M.10 — committed `agent_rules.signal` block + `SignalConfig` parsing (S1, D6/FD-7).

The signal block is IMMUTABLE under overlay: every leaf is a string / list-of-strings /
dict thereof, and `config.tighten_only_merge` keeps base for all non-bool/non-numeric
leaves (config.py:43), so a hostile overlay cannot tighten, loosen, or reinterpret any
signal parameter. A changed model is a new commit => new `rules_hash`.
"""
import hashlib
import json
import unittest
from decimal import Decimal
from pathlib import Path

from agent import config as agent_config
from agent.serializer import dumps
from agent.signal_config import FEATURE_NAMES, SignalConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_RULES = REPO_ROOT / "config" / "agent_rules.json"


def _committed_config() -> dict:
    return agent_config.load(AGENT_RULES)


def _assert_string_leaves(node, path="signal"):
    """Recursive FD-7 type assertion: str | list[str] | dict thereof."""
    if isinstance(node, str):
        return
    if isinstance(node, list):
        for i, item in enumerate(node):
            if not isinstance(item, str):
                raise AssertionError(f"{path}[{i}] is {type(item).__name__}, not str")
        return
    if isinstance(node, dict):
        for key, value in node.items():
            _assert_string_leaves(value, f"{path}.{key}")
        return
    raise AssertionError(f"{path} is {type(node).__name__}; FD-7 allows str/list[str]/dict only")


class TestCommittedSignalBlock(unittest.TestCase):
    def setUp(self):
        self.config = _committed_config()

    def test_signal_block_present_with_frozen_keys(self):
        signal = self.config["signal"]
        self.assertEqual(
            set(signal.keys()),
            {
                "interval", "feature_windows", "rsi_period", "z_window", "vol_window",
                "horizons", "threshold_k", "spread_bps_max", "quote_staleness_ms_max",
                "feature_staleness_ms_max", "bar_lag_max_intervals", "refresh_cadence_ms",
                "prob_bins", "min_reference_samples", "model",
            },
        )

    def test_every_leaf_is_string_list_or_dict(self):
        _assert_string_leaves(self.config["signal"])

    def test_run_gates_still_identity_false(self):
        # Extends, never replaces, the M0 canary: adding the signal block must not
        # touch the run gates.
        self.assertIs(self.config["enabled"], False)
        self.assertIs(self.config["paper_trading"]["enabled"], False)

    def test_hostile_overlay_cannot_alter_signal_block(self):
        overlay = {
            "signal": {
                "interval": "1s",
                "feature_windows": ["1"],
                "horizons": ["1m", "2m", "3m"],
                "threshold_k": "0.5",
                "spread_bps_max": "5000",
                "quote_staleness_ms_max": "999999",
                "prob_bins": "2",
                "min_reference_samples": "1",
                "model": {
                    "model_version": "evil-v9",
                    "standardization": "zscore",
                    "coefficients": {
                        "5m": {"intercept": "99", "z_ret_21": "99", "momentum_9": "99",
                               "momentum_21": "99", "rsi14_centered": "99",
                               "ema_gap_9_21": "99", "sma_gap_21_50": "99",
                               "realized_vol_21": "99"},
                    },
                },
                "smuggled_new_key": "1",
            }
        }
        merged = agent_config.tighten_only_merge(self.config, overlay)
        self.assertEqual(merged["signal"], self.config["signal"])

    def test_rules_hash_changes_when_a_signal_leaf_changes(self):
        base_hash = agent_config.rules_hash(self.config)
        self.assertEqual(base_hash, agent_config.rules_hash(_committed_config()))
        mutated = json.loads(json.dumps(self.config))
        mutated["signal"]["model"]["coefficients"]["5m"]["momentum_9"] = "1.1"
        self.assertNotEqual(base_hash, agent_config.rules_hash(mutated))

    def test_model_artifact_hash_matches_independent_sha256(self):
        parsed = SignalConfig.from_config(self.config)
        canon = dumps(self.config["signal"]["model"])
        expected = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        self.assertEqual(parsed.model_artifact_hash, expected)

    def test_rules_hash_matches_agent_config(self):
        parsed = SignalConfig.from_config(self.config)
        self.assertEqual(parsed.rules_hash, agent_config.rules_hash(self.config))


class TestSignalConfigParsing(unittest.TestCase):
    def setUp(self):
        self.config = _committed_config()

    def _signal(self):
        return json.loads(json.dumps(self.config))

    def test_parses_committed_config_to_typed_values(self):
        parsed = SignalConfig.from_config(self.config)
        self.assertEqual(parsed.interval, "1m")
        self.assertEqual(parsed.feature_windows, (9, 21, 50))
        self.assertEqual(parsed.rsi_period, 14)
        self.assertEqual(parsed.z_window, 21)
        self.assertEqual(parsed.vol_window, 21)
        self.assertEqual(parsed.horizons, ("5m", "30m"))
        self.assertEqual(parsed.horizon_minutes, {"5m": 5, "30m": 30})
        self.assertEqual(parsed.threshold_k, Decimal("0"))
        self.assertEqual(parsed.spread_bps_max, Decimal("50"))
        self.assertEqual(parsed.quote_staleness_ms_max, 2000)
        self.assertEqual(parsed.feature_staleness_ms_max, 5000)
        self.assertEqual(parsed.bar_lag_max_intervals, 2)
        self.assertEqual(parsed.refresh_cadence_ms, 1000)
        self.assertEqual(parsed.prob_bins, 10)
        self.assertEqual(parsed.min_reference_samples, 30)
        self.assertEqual(parsed.model_version, "logit-mom-v1")
        self.assertEqual(parsed.standardization, "identity")
        self.assertEqual(set(parsed.coefficients.keys()), {"5m", "30m"})
        for horizon in ("5m", "30m"):
            self.assertEqual(
                set(parsed.coefficients[horizon].keys()),
                set(("intercept",) + FEATURE_NAMES),
            )
            self.assertEqual(parsed.coefficients[horizon]["momentum_9"], Decimal("1.0"))

    def test_unknown_key_raises(self):
        cfg = self._signal()
        cfg["signal"]["surprise"] = "1"
        with self.assertRaises(ValueError):
            SignalConfig.from_config(cfg)

    def test_missing_key_raises(self):
        cfg = self._signal()
        del cfg["signal"]["horizons"]
        with self.assertRaises(ValueError):
            SignalConfig.from_config(cfg)

    def test_missing_signal_block_raises(self):
        cfg = self._signal()
        del cfg["signal"]
        with self.assertRaises(ValueError):
            SignalConfig.from_config(cfg)

    def test_malformed_horizon_raises(self):
        cfg = self._signal()
        cfg["signal"]["horizons"] = ["5m", "1h"]
        with self.assertRaises(ValueError):
            SignalConfig.from_config(cfg)

    def test_nonpositive_window_raises(self):
        cfg = self._signal()
        cfg["signal"]["feature_windows"] = ["0", "21", "50"]
        with self.assertRaises(ValueError):
            SignalConfig.from_config(cfg)

    def test_changed_window_without_model_rename_raises(self):
        # harden round 1, M3-R1-004: the v1 FEATURE_NAMES hard-bind the windows;
        # a changed-but-validating window set must fail LOUD, never run silently.
        for key, hostile in (("feature_windows", ["9", "21", "60"]),
                             ("rsi_period", "21"),
                             ("z_window", "30"),
                             ("vol_window", "10")):
            cfg = self._signal()
            cfg["signal"][key] = hostile
            with self.assertRaises(ValueError):
                SignalConfig.from_config(cfg)

    def test_prob_bins_must_be_10_in_m3(self):
        cfg = self._signal()
        cfg["signal"]["prob_bins"] = "20"
        with self.assertRaises(ValueError):
            SignalConfig.from_config(cfg)

    def test_standardization_must_be_identity(self):
        cfg = self._signal()
        cfg["signal"]["model"]["standardization"] = "zscore"
        with self.assertRaises(ValueError):
            SignalConfig.from_config(cfg)

    def test_coefficient_key_mismatch_raises(self):
        cfg = self._signal()
        del cfg["signal"]["model"]["coefficients"]["5m"]["momentum_9"]
        with self.assertRaises(ValueError):
            SignalConfig.from_config(cfg)
        cfg = self._signal()
        cfg["signal"]["model"]["coefficients"]["5m"]["extra"] = "1"
        with self.assertRaises(ValueError):
            SignalConfig.from_config(cfg)

    def test_coefficients_must_cover_every_horizon(self):
        cfg = self._signal()
        del cfg["signal"]["model"]["coefficients"]["30m"]
        with self.assertRaises(ValueError):
            SignalConfig.from_config(cfg)

    def test_non_string_leaf_raises(self):
        cfg = self._signal()
        cfg["signal"]["spread_bps_max"] = 50
        with self.assertRaises(ValueError):
            SignalConfig.from_config(cfg)


if __name__ == "__main__":
    unittest.main()
