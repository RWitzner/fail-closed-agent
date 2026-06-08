"""Two-key arming seam (spec §12). Live needs BOTH keys, independently.

Key A = committed config flag (`risk_rules.live_trading.enabled` True). Key B = a
runtime secret never committed. A live-capable broker cannot be constructed with
only one key. (The live broker itself lands in M8 — here only the seam exists.)
"""
import unittest

from agent.arming import ArmingError, construct_live_broker, two_key_armed

COMMITTED = {"risk_rules": {"live_trading": {"enabled": False}}}
KEY_A_ON = {"risk_rules": {"live_trading": {"enabled": True}}}


class TestTwoKeyArmed(unittest.TestCase):
    def test_committed_config_is_not_armed(self):
        self.assertFalse(two_key_armed(COMMITTED, "a-secret"))

    def test_key_a_only_is_not_armed(self):
        self.assertFalse(two_key_armed(KEY_A_ON, ""))
        self.assertFalse(two_key_armed(KEY_A_ON, None))

    def test_key_b_only_is_not_armed(self):
        self.assertFalse(two_key_armed(COMMITTED, "a-secret"))

    def test_both_keys_arms(self):
        self.assertTrue(two_key_armed(KEY_A_ON, "a-secret"))

    def test_config_flag_must_be_identity_true(self):
        self.assertFalse(two_key_armed({"risk_rules": {"live_trading": {"enabled": "true"}}}, "s"))


class TestConstructLiveBroker(unittest.TestCase):
    def test_one_key_cannot_construct_live_broker(self):
        with self.assertRaises(ArmingError):
            construct_live_broker(KEY_A_ON, None)  # missing key B
        with self.assertRaises(ArmingError):
            construct_live_broker(COMMITTED, "a-secret")  # missing key A

    def test_two_keys_pass_arming_but_live_broker_is_m8(self):
        # Both keys present: the arming seam passes, but the live broker is not
        # built until M8 — proving the seam is reachable only when fully armed.
        with self.assertRaises(NotImplementedError):
            construct_live_broker(KEY_A_ON, "a-secret")


if __name__ == "__main__":
    unittest.main()
