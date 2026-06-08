"""S1: on the COMMITTED config, nothing opens — at the Broker boundary.

This is the load-bearing safety canary. It loads the real committed config files
(not a fixture) and proves: the run gates are identity-`False`, no opening order
can be minted, a simulated decision loop never reaches the broker, and a
tighten-only overlay cannot loosen the committed gates.
"""
import unittest
from decimal import Decimal
from pathlib import Path

from agent.config import load, tighten_only_merge
from agent.execution_preflight import PreflightRejected, mint_open_token
from agent.gates import live_allowed, opening_allowed
from agent.broker.base import OrderIntent
from tests.lib.fakes import SpyBroker

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / "config"
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


def _committed_config():
    return {
        "agent_rules": load(_CONFIG / "agent_rules.json"),
        "risk_rules": load(_CONFIG / "risk_rules.json"),
    }


class TestCommittedConfigCanary(unittest.TestCase):
    def test_committed_gates_are_identity_false(self):
        cfg = _committed_config()
        self.assertIs(cfg["agent_rules"]["enabled"], False)
        self.assertIs(cfg["agent_rules"]["paper_trading"]["enabled"], False)
        self.assertIs(cfg["risk_rules"]["live_trading"]["enabled"], False)

    def test_committed_config_forbids_opening_and_live(self):
        cfg = _committed_config()
        self.assertFalse(opening_allowed(cfg))
        self.assertFalse(live_allowed(cfg))

    def test_no_opening_submit_and_zero_total_on_committed_config(self):
        cfg = _committed_config()
        broker = SpyBroker()
        for sym in ["AAPL", "MSFT", "NVDA"]:
            intent = OrderIntent(symbol=sym, side="buy", qty=Decimal("1"), intent_id=f"o-{sym}")
            try:
                token = mint_open_token(cfg, intent)
            except PreflightRejected:
                continue  # cannot open -> the broker is never reached
            broker.submit_order(intent, token)  # unreachable on committed config
        self.assertEqual(broker.calls, [])
        self.assertEqual(broker.submitted, [])

    def test_malformed_live_block_reads_as_off(self):
        # live_trading.enabled = "true" (a string) must read False (identity-strict).
        malformed = load(_FIXTURES / "malformed_live_block.json")
        self.assertFalse(live_allowed(malformed))

    def test_armed_overlay_cannot_loosen_committed_via_tighten_only(self):
        overlay = load(_FIXTURES / "local_armed_overlay.json")
        merged = tighten_only_merge(_committed_config(), overlay)
        self.assertFalse(opening_allowed(merged))
        self.assertFalse(live_allowed(merged))


if __name__ == "__main__":
    unittest.main()
