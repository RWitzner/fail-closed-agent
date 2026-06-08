"""Identity-strict, fail-closed run gates (spec §4.7, §12).

A gate is open only when the value is exactly `True`. Anything else — a truthy
int/string, a missing key, a typo — reads False. Opening requires BOTH run gates;
live requires its own flag. The committed config (all gates false) opens nothing.
"""
import unittest

from agent.gates import gate, live_allowed, opening_allowed, strict_bool

COMMITTED = {
    "agent_rules": {"enabled": False, "paper_trading": {"enabled": False}},
    "risk_rules": {"live_trading": {"enabled": False}},
}


class TestStrictBool(unittest.TestCase):
    def test_only_true_is_true(self):
        self.assertIs(strict_bool(True), True)

    def test_truthy_non_bool_is_false(self):
        for v in (1, "true", "True", [1], object(), 0.0):
            self.assertIs(strict_bool(v), False)

    def test_none_and_false_are_false(self):
        self.assertIs(strict_bool(None), False)
        self.assertIs(strict_bool(False), False)


class TestGateReader(unittest.TestCase):
    def test_missing_path_is_false(self):
        self.assertIs(gate({}, "agent_rules", "enabled"), False)

    def test_non_bool_value_is_false(self):
        self.assertIs(gate({"agent_rules": {"enabled": 1}}, "agent_rules", "enabled"), False)

    def test_true_value_is_true(self):
        self.assertIs(gate({"agent_rules": {"enabled": True}}, "agent_rules", "enabled"), True)


class TestOpeningAllowed(unittest.TestCase):
    def test_committed_config_forbids_opening(self):
        self.assertIs(opening_allowed(COMMITTED), False)

    def test_requires_both_run_gates(self):
        only_enabled = {"agent_rules": {"enabled": True, "paper_trading": {"enabled": False}}}
        only_paper = {"agent_rules": {"enabled": False, "paper_trading": {"enabled": True}}}
        self.assertIs(opening_allowed(only_enabled), False)
        self.assertIs(opening_allowed(only_paper), False)

    def test_both_run_gates_true_allows_opening(self):
        armed = {"agent_rules": {"enabled": True, "paper_trading": {"enabled": True}}}
        self.assertIs(opening_allowed(armed), True)


class TestLiveAllowed(unittest.TestCase):
    def test_committed_config_forbids_live(self):
        self.assertIs(live_allowed(COMMITTED), False)

    def test_missing_is_false(self):
        self.assertIs(live_allowed({}), False)

    def test_flag_true_allows(self):
        self.assertIs(live_allowed({"risk_rules": {"live_trading": {"enabled": True}}}), True)


if __name__ == "__main__":
    unittest.main()
