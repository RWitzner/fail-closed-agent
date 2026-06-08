"""Config integrity (spec §4.7): tighten-only merge + rules_hash provenance.

An authoritative overlay may only *tighten* the base config — it can turn a gate
off but never on, and lower a cap but never raise it. `rules_hash` stamps the
effective config for replayable provenance.
"""
import tempfile
import unittest
from pathlib import Path

from agent.config import load, rules_hash, tighten_only_merge


class TestRulesHash(unittest.TestCase):
    def test_deterministic(self):
        cfg = {"a": True, "caps": {"max": 10}}
        self.assertEqual(rules_hash(cfg), rules_hash(dict(cfg)))

    def test_changes_with_content(self):
        self.assertNotEqual(rules_hash({"a": True}), rules_hash({"a": False}))

    def test_is_sha256_hex(self):
        h = rules_hash({"a": 1})
        self.assertEqual(len(h), 64)
        int(h, 16)

    def test_non_finite_config_value_raises(self):
        with self.assertRaises(ValueError):
            rules_hash({"x": float("nan")})


class TestTightenOnlyMerge(unittest.TestCase):
    def test_overlay_cannot_enable_a_disabled_gate(self):
        merged = tighten_only_merge({"enabled": False}, {"enabled": True})
        self.assertIs(merged["enabled"], False)

    def test_overlay_can_disable_an_enabled_gate(self):
        merged = tighten_only_merge({"enabled": True}, {"enabled": False})
        self.assertIs(merged["enabled"], False)

    def test_both_true_stays_true(self):
        merged = tighten_only_merge({"enabled": True}, {"enabled": True})
        self.assertIs(merged["enabled"], True)

    def test_cap_tightens_to_minimum(self):
        merged = tighten_only_merge({"max_position": 100}, {"max_position": 10})
        self.assertEqual(merged["max_position"], 10)

    def test_cap_cannot_be_loosened(self):
        merged = tighten_only_merge({"max_position": 100}, {"max_position": 200})
        self.assertEqual(merged["max_position"], 100)

    def test_recurses_into_nested_dicts(self):
        base = {"paper_trading": {"enabled": True}}
        overlay = {"paper_trading": {"enabled": False}}
        self.assertIs(tighten_only_merge(base, overlay)["paper_trading"]["enabled"], False)

    def test_overlay_only_key_is_ignored(self):
        merged = tighten_only_merge({"a": True}, {"a": True, "sneaky": True})
        self.assertNotIn("sneaky", merged)

    def test_base_only_key_is_kept(self):
        merged = tighten_only_merge({"a": True, "b": 5}, {"a": True})
        self.assertEqual(merged["b"], 5)


class TestLoad(unittest.TestCase):
    def test_loads_json_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text('{"enabled": false}')
            self.assertEqual(load(p), {"enabled": False})


if __name__ == "__main__":
    unittest.main()
