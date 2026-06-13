"""M7 Wave 1 - orchestrator artifact cache key hardening."""
import unittest
from unittest import mock

from agent import orchestrator as orch_mod
from agent.backtest_gate import ArtifactCheck


class TestArtifactCheckCacheKey(unittest.TestCase):
    def _orchestrator_shell(self):
        orch = object.__new__(orch_mod.Orchestrator)
        orch._artifact_checks = {}
        orch.rules_hash = "rh-1"
        orch._artifacts_dir = "/tmp/not-used"
        return orch

    def test_cache_key_includes_strategy_rules_hash_and_data_pin(self):
        calls = []

        def fake_verify(strategy_id, *, rules_hash, data_pin, artifacts_dir):
            calls.append((strategy_id, rules_hash, data_pin, artifacts_dir))
            return ArtifactCheck(
                status="ok",
                artifact_path=f"/tmp/{strategy_id}-{rules_hash}-{data_pin}.json",
                artifact_hash=f"h-{len(calls)}",
            )

        orch = self._orchestrator_shell()
        with mock.patch.object(orch_mod, "verify_artifact",
                               side_effect=fake_verify):
            pin_a_first = orch._artifact_check("directional.momentum_v1", "pin-a")
            pin_b = orch._artifact_check("directional.momentum_v1", "pin-b")
            pin_a_second = orch._artifact_check("directional.momentum_v1", "pin-a")
            orch.rules_hash = "rh-2"
            pin_a_new_rules = orch._artifact_check(
                "directional.momentum_v1", "pin-a")

        self.assertEqual(calls, [
            ("directional.momentum_v1", "rh-1", "pin-a", "/tmp/not-used"),
            ("directional.momentum_v1", "rh-1", "pin-b", "/tmp/not-used"),
            ("directional.momentum_v1", "rh-2", "pin-a", "/tmp/not-used"),
        ])
        self.assertIs(pin_a_first, pin_a_second)
        self.assertIsNot(pin_a_first, pin_b)
        self.assertIsNot(pin_a_first, pin_a_new_rules)
