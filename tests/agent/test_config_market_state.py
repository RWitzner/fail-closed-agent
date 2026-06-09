"""M2 §G — market_state config is PROVENANCE-only; safety numbers are code constants.

The contract's load-bearing config stance (§G): every M2 safety quantity
(MIN_INDEPENDENT_SOURCES, blackout lead/trail days, freshness TTL, ...) is a CODE
CONSTANT, never a config key, because `tighten_only_merge` takes `min()` of two
numerics (`config.py:35-41`) — correct only when smaller==safer, which is the
WRONG polarity for these. Only provenance STRINGS (calendar pin / MIC) live in
config. These tests prove the committed config honors that, and that no overlay
can loosen it (mirrors `test_config_canary.py:60-64`).
"""
import json
import unittest
from pathlib import Path

from agent.config import load, rules_hash, tighten_only_merge
from agent.corporate_actions import MIN_INDEPENDENT_SOURCES

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / "config"


def _committed():
    return {
        "agent_rules": load(_CONFIG / "agent_rules.json"),
        "risk_rules": load(_CONFIG / "risk_rules.json"),
    }


class TestMarketStateConfig(unittest.TestCase):
    def test_committed_market_state_block_is_provenance_strings_only(self):
        ms = load(_CONFIG / "agent_rules.json")["market_state"]
        self.assertEqual(ms["calendar_pin"], "4.13.2")
        self.assertEqual(ms["calendar_mic"], "XNYS")
        # Provenance STRINGS only — no numeric safety threshold may live here.
        for value in ms.values():
            self.assertIsInstance(value, str)

    def test_safety_numbers_are_code_constants_not_in_config(self):
        self.assertEqual(MIN_INDEPENDENT_SOURCES, 2)  # the code constant, not config
        flat = json.dumps(load(_CONFIG / "agent_rules.json"))
        self.assertNotIn("min_independent_sources", flat)
        self.assertNotIn("blackout_lead_days", flat)
        self.assertNotIn("freshness_ttl", flat)

    def test_min_ca_sources_not_overlayable_below_two(self):
        # An overlay injecting a smaller threshold is an overlay-ONLY key (absent from
        # base) -> dropped by tighten_only_merge; the code constant stays 2.
        overlay = {"agent_rules": {"market_state": {"min_independent_sources": 1}}}
        merged = tighten_only_merge(_committed(), overlay)
        self.assertNotIn("min_independent_sources", merged["agent_rules"]["market_state"])
        self.assertEqual(MIN_INDEPENDENT_SOURCES, 2)

    def test_market_state_overlay_cannot_loosen(self):
        # Mirrors the committed-config canary: a tighten-only overlay can never ADD a
        # permission or flip a gate on. An overlay flipping `enabled` and injecting a
        # market_state safety knob is neutralized (AND -> False; overlay-only key dropped).
        overlay = {"agent_rules": {"enabled": True, "market_state": {"blackout_lead_days": 0}}}
        merged = tighten_only_merge(_committed(), overlay)
        self.assertIs(merged["agent_rules"]["enabled"], False)
        self.assertNotIn("blackout_lead_days", merged["agent_rules"]["market_state"])

    def test_calendar_pin_string_not_min_merged(self):
        # A string hits the 'type mismatch / unhandled kind' branch -> base kept, so an
        # overlay can never swap the pin (strings are never min()-merged; config.py:42).
        overlay = {"agent_rules": {"market_state": {"calendar_pin": "0.0.1"}}}
        merged = tighten_only_merge(_committed(), overlay)
        self.assertEqual(merged["agent_rules"]["market_state"]["calendar_pin"], "4.13.2")

    def test_rules_hash_over_assembled_dict_not_per_file(self):
        cfg = _committed()
        self.assertEqual(rules_hash(cfg), rules_hash(_committed()))  # deterministic
        # The hash is over the ASSEMBLED {agent_rules, risk_rules} dict: dropping a file
        # changes it (so a per-file hash is not what is carried into rows).
        self.assertNotEqual(rules_hash(cfg), rules_hash({"agent_rules": cfg["agent_rules"]}))


if __name__ == "__main__":
    unittest.main()
