"""M4 §M test 1 — RiskConfig parser + tighten-only merge posture + frozen constants.

Invariants: S1 (zero caps / shorts off on the committed config), R5 (overlay can
lower, never raise; shorts/universe immutable under overlay).
"""
import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path

from agent.config import load, rules_hash, tighten_only_merge
from agent.risk.reasons import (
    ACCUMULATED_REASONS,
    GATE_STAGES,
    KILL_CAUSES,
    KILL_STATES,
    RESERVED_KILL_CAUSES,
    RESERVED_REASONS,
    RISK_REASONS,
    TERMINAL_REASONS,
    RiskError,
    require_kill_cause,
    require_reason,
)
from agent.risk.risk_config import (
    INTRADAY_MARGIN_BUFFER_USD,
    SHORTS_SUPPORTED,
    RiskConfig,
    SymbolRisk,
)
from tests.lib.risk_fixtures import permissive_fixture_config

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / "config"


def _committed_config():
    return {
        "agent_rules": load(_CONFIG / "agent_rules.json"),
        "risk_rules": load(_CONFIG / "risk_rules.json"),
    }


class TestVocabularies(unittest.TestCase):
    def test_risk_reasons_has_exactly_34_members(self):
        self.assertEqual(len(TERMINAL_REASONS), 10)
        self.assertEqual(len(ACCUMULATED_REASONS), 22)
        self.assertEqual(len(RESERVED_REASONS), 2)
        self.assertEqual(len(RISK_REASONS), 34)

    def test_gate_stages_frozen_order(self):
        self.assertEqual(GATE_STAGES, (
            "run_gates", "kill", "margin_freeze", "account", "portfolio",
            "candidate", "universe", "market_state", "short", "caps",
            "margin", "pdt", "loss"))

    def test_kill_vocabularies(self):
        self.assertEqual(KILL_STATES, frozenset({"monitoring", "flattening", "halted"}))
        self.assertEqual(KILL_CAUSES, frozenset(
            {"daily_loss_cap", "drawdown_cap", "operator_manual", "drill"}))
        self.assertEqual(RESERVED_KILL_CAUSES, frozenset({"live_gate_flip"}))

    def test_validators_raise_risk_error(self):
        with self.assertRaises(RiskError):
            require_reason("bogus_reason")
        with self.assertRaises(RiskError):
            require_reason("locate_unavailable")  # reserved, never emitted in M4
        with self.assertRaises(RiskError):
            require_kill_cause("live_gate_flip")  # reserved kill cause (M8)


class TestRiskConfigParser(unittest.TestCase):
    def test_committed_json_parses(self):
        cfg = RiskConfig.from_config(_committed_config())
        for name in ("max_position_usd", "max_gross_exposure_usd", "max_net_exposure_usd",
                     "max_daily_loss_usd", "max_drawdown_usd", "max_sector_exposure_usd",
                     "max_abs_beta_notional_usd"):
            value = getattr(cfg, name)
            self.assertIsInstance(value, Decimal)
            self.assertEqual(value, Decimal("0"))
        self.assertIs(cfg.short_selling_enabled, False)
        self.assertEqual(dict(cfg.universe), {})
        self.assertEqual(cfg.rules_hash, rules_hash(_committed_config()))

    def test_permissive_fixture_parses_with_universe(self):
        cfg = RiskConfig.from_config(permissive_fixture_config())
        self.assertEqual(cfg.max_position_usd, Decimal("10000"))
        self.assertIn("AAPL", cfg.universe)
        self.assertEqual(cfg.universe["AAPL"], SymbolRisk(sector="tech", beta=Decimal("1.2")))
        self.assertIs(cfg.short_selling_enabled, False)  # SHORTS_SUPPORTED ANDs it off

    def test_caps_must_be_ints_geq_zero(self):
        for bad in (True, 1.5, "100", -1, None):
            config = permissive_fixture_config()
            config["risk_rules"]["caps"]["max_position_usd"] = bad
            with self.assertRaises(ValueError, msg=repr(bad)):
                RiskConfig.from_config(config)

    def test_unknown_or_missing_keys_raise(self):
        config = permissive_fixture_config()
        config["risk_rules"]["caps"]["max_leverage"] = 2
        with self.assertRaises(ValueError):
            RiskConfig.from_config(config)
        config = permissive_fixture_config()
        del config["risk_rules"]["caps"]["max_drawdown_usd"]
        with self.assertRaises(ValueError):
            RiskConfig.from_config(config)
        config = permissive_fixture_config()
        config["risk_rules"]["risk"]["locate"] = {}
        with self.assertRaises(ValueError):
            RiskConfig.from_config(config)
        config = permissive_fixture_config()
        del config["risk_rules"]["risk"]["universe"]
        with self.assertRaises(ValueError):
            RiskConfig.from_config(config)
        with self.assertRaises(ValueError):
            RiskConfig.from_config({"risk_rules": permissive_fixture_config()["risk_rules"]})

    def test_universe_entry_validation(self):
        for bad_entry in (
            {"beta": "1.0"},                                  # missing sector
            {"sector": "tech"},                               # missing beta
            {"sector": "tech", "beta": "abc"},                # non-Decimal beta
            {"sector": "tech", "beta": "NaN"},                # non-finite beta
            {"sector": "tech", "beta": 1.0},                  # float beta (FD-7 strings)
            {"sector": "", "beta": "1.0"},                    # empty sector
            {"sector": "tech", "beta": "1.0", "weight": "1"},  # extra key
        ):
            config = permissive_fixture_config()
            config["risk_rules"]["risk"]["universe"]["BAD"] = bad_entry
            with self.assertRaises(ValueError, msg=repr(bad_entry)):
                RiskConfig.from_config(config)

    def test_short_selling_must_be_strict_bool(self):
        config = permissive_fixture_config()
        config["risk_rules"]["risk"]["short_selling"]["enabled"] = "true"
        with self.assertRaises(ValueError):
            RiskConfig.from_config(config)


class TestFrozenConstants(unittest.TestCase):
    def test_shorts_supported_pinned_false(self):
        self.assertIs(SHORTS_SUPPORTED, False)

    def test_intraday_margin_buffer_pinned_zero(self):
        # FD-M4-6: inverted polarity under min()-merge — a CODE constant, never config.
        self.assertEqual(INTRADAY_MARGIN_BUFFER_USD, Decimal("0"))

    def test_short_selling_true_in_config_still_disabled(self):
        config = permissive_fixture_config()
        config["risk_rules"]["risk"]["short_selling"]["enabled"] = True
        cfg = RiskConfig.from_config(config)
        self.assertIs(cfg.short_selling_enabled, False)  # ANDed with SHORTS_SUPPORTED


class TestMergePosture(unittest.TestCase):
    def test_overlay_can_lower_never_raise_a_cap(self):
        base = permissive_fixture_config()
        lowered = tighten_only_merge(base, {"risk_rules": {"caps": {"max_position_usd": 1}}})
        self.assertEqual(lowered["risk_rules"]["caps"]["max_position_usd"], 1)
        raised = tighten_only_merge(base, {"risk_rules": {"caps": {"max_position_usd": 999999}}})
        self.assertEqual(raised["risk_rules"]["caps"]["max_position_usd"], 10000)

    def test_short_selling_cannot_be_enabled_by_overlay(self):
        merged = tighten_only_merge(
            _committed_config(),
            {"risk_rules": {"risk": {"short_selling": {"enabled": True}}}})
        self.assertIs(merged["risk_rules"]["risk"]["short_selling"]["enabled"], False)

    def test_universe_metadata_immutable_under_overlay(self):
        base = permissive_fixture_config()
        merged = tighten_only_merge(base, {"risk_rules": {"risk": {"universe": {
            "AAPL": {"sector": "memecoins", "beta": "99"},
            "EVIL": {"sector": "evil", "beta": "1"},
        }}}})
        # strings/dicts: tighten_only_merge keeps base; overlay-only keys dropped.
        self.assertEqual(merged["risk_rules"]["risk"]["universe"],
                         base["risk_rules"]["risk"]["universe"])

    def test_changing_any_risk_leaf_changes_rules_hash(self):
        base = _committed_config()
        baseline = RiskConfig.from_config(base).rules_hash
        mutated = copy.deepcopy(base)
        mutated["risk_rules"]["risk"]["universe"]["AAPL"] = {"sector": "tech", "beta": "1.2"}
        self.assertNotEqual(RiskConfig.from_config(mutated).rules_hash, baseline)
        mutated = copy.deepcopy(base)
        mutated["risk_rules"]["caps"]["max_position_usd"] = 1
        self.assertNotEqual(RiskConfig.from_config(mutated).rules_hash, baseline)


class TestCommittedFileShape(unittest.TestCase):
    def test_committed_caps_are_json_integers_zero(self):
        raw = json.loads((_CONFIG / "risk_rules.json").read_text(encoding="utf-8"))
        caps = raw["caps"]
        self.assertEqual(len(caps), 7)
        for name, value in caps.items():
            self.assertIsInstance(value, int, name)
            self.assertNotIsInstance(value, bool, name)
            self.assertEqual(value, 0, name)


if __name__ == "__main__":
    unittest.main()
