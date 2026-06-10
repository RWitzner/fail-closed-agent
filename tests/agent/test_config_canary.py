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
        # FD-M5-14: the mint takes PreflightInputs; on the committed config every
        # preflight terminates at run_gates with the byte-exact terminal shape (S1).
        from agent.exec_reasons import PREFLIGHT_STAGES
        from tests.agent.test_execution_preflight_m5 import (
            golden_candidate,
            golden_inputs,
        )

        cfg = _committed_config()
        broker = SpyBroker()
        for sym in ["AAPL", "MSFT", "NVDA"]:
            intent = OrderIntent(symbol=sym, side="buy", qty=Decimal("1"), intent_id=f"o-{sym}")
            try:
                token, _ = mint_open_token(golden_inputs(
                    gates_config=cfg,
                    candidate=golden_candidate(symbol=sym, qty="1")))
            except PreflightRejected as caught:
                reject = caught.reject
                self.assertEqual(reject.reasons, ("run_gates_off",))
                self.assertEqual(reject.gate_stage, "run_gates")
                self.assertEqual(reject.stages_skipped, PREFLIGHT_STAGES[1:])
                self.assertIsNone(reject.capped_limit)
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


class TestRiskRulesCanary(unittest.TestCase):
    """M4 §M test 10 extension (S1, R5): the committed risk_rules additions stay
    fully shut, and a hostile overlay merges back to the committed values."""

    _CAP_KEYS = (
        "max_position_usd", "max_gross_exposure_usd", "max_net_exposure_usd",
        "max_daily_loss_usd", "max_drawdown_usd", "max_sector_exposure_usd",
        "max_abs_beta_notional_usd",
    )

    def test_committed_caps_all_integer_zero(self):
        caps = _committed_config()["risk_rules"]["caps"]
        self.assertEqual(set(caps), set(self._CAP_KEYS))
        for name in self._CAP_KEYS:
            self.assertIsInstance(caps[name], int, name)
            self.assertNotIsInstance(caps[name], bool, name)
            self.assertEqual(caps[name], 0, name)

    def test_committed_short_selling_identity_false_and_universe_empty(self):
        risk = _committed_config()["risk_rules"]["risk"]
        self.assertIs(risk["short_selling"]["enabled"], False)
        self.assertEqual(risk["universe"], {})

    def test_committed_gates_still_identity_false_with_risk_block_present(self):
        # extends, never replaces, the M0 assertions
        cfg = _committed_config()
        self.assertIs(cfg["agent_rules"]["enabled"], False)
        self.assertIs(cfg["agent_rules"]["paper_trading"]["enabled"], False)
        self.assertIs(cfg["risk_rules"]["live_trading"]["enabled"], False)
        self.assertFalse(opening_allowed(cfg))
        self.assertFalse(live_allowed(cfg))

    def test_risk_armed_overlay_merges_back_to_committed_values(self):
        overlay = load(_FIXTURES / "risk_armed_overlay.json")
        committed = _committed_config()
        merged = tighten_only_merge(committed, overlay)
        self.assertFalse(opening_allowed(merged))
        self.assertFalse(live_allowed(merged))
        caps = merged["risk_rules"]["caps"]
        self.assertEqual(set(caps), set(self._CAP_KEYS))  # injected key dropped
        for name in self._CAP_KEYS:
            self.assertEqual(caps[name], 0, name)         # raised caps refused
        risk = merged["risk_rules"]["risk"]
        self.assertIs(risk["short_selling"]["enabled"], False)
        self.assertEqual(risk["universe"], {})            # altered universe refused
        self.assertNotIn("locate_provider", risk)         # injected key dropped
        self.assertEqual(merged, committed)               # byte-equal merge-back

    def test_committed_config_can_open_always_terminates_at_run_gates(self):
        # S1 composition at the can_open chokepoint, on the REAL committed JSON.
        from agent.risk.account_state import AccountStore, parse_account_payload
        from agent.risk.can_open import RiskEngine
        from agent.risk.intraday_margin import FreezeState, MarginRead
        from agent.risk.loss_limits import LossRead
        from agent.risk.pdt_compat import PdtRead
        from agent.risk.risk_config import RiskConfig
        from agent.candidate import Candidate, Leg
        from agent.serializer import BrokerUSD
        from tests.lib.fakes import FakeClock
        from tests.lib.risk_fixtures import account_payload, portfolio_fixture

        cfg = _committed_config()
        engine = RiskEngine(cfg=RiskConfig.from_config(cfg), gates_config=cfg,
                            run_id="run-canary")
        store = AccountStore(clock=FakeClock(start_ms=0))
        store.put(parse_account_payload(account_payload(), source="fixture",
                                        seen_at_ms=0, ts_read_utc="t"))
        candidate = Candidate(strategy_id="s1", legs=(
            Leg(symbol="AAPL", instrument_id=1001, side="buy",
                qty=Decimal("1"), limit_price=Decimal("190.00")),),
            paper_eligible=True, score=None)
        verdict = engine.can_open(
            candidate, portfolio_fixture("flat"), store.get(),
            market_state={}, kill_state="monitoring", kill_generation=0,
            margin_read=MarginRead(outstanding_nonminor=(),
                                   freeze=FreezeState(False, None, None, None),
                                   asof_session_date_et="2026-06-08"),
            pdt_read=PdtRead(state="unknown", evidence=None, rejection_latched=False),
            loss_read=LossRead(hwm_equity=BrokerUSD("100000.00"), daily_loss_usd=None,
                               drawdown_usd=None, breaches=()),
            now_ms=0)
        self.assertIs(verdict.allowed, False)
        self.assertEqual(verdict.gate_stage, "run_gates")
        self.assertEqual(verdict.reasons, ("run_gates_off",))
        self.assertEqual(verdict.legs, ())
        self.assertEqual(verdict.gross_notional, Decimal("0"))
        self.assertEqual(verdict.caps_used, ())
        self.assertIsNone(verdict.session_date_et)


if __name__ == "__main__":
    unittest.main()
