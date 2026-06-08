"""Preflight chokepoint, fail-closed by type (spec §5 Tier 6, invariant S4 contract).

`submit_order` is reachable only with a non-forgeable, single-use preflight token.
Tokens are split by intent: `OpenPreflightToken` (opening/increasing) is reject-all
in M0 (full preflight in M5), while `ReduceOnlyPreflightToken` is mintable only for
an existing held position + a position-decreasing order, so flatten always works.
"""
import types
import unittest
from decimal import Decimal

from agent.broker.base import OrderIntent, require_token
from agent.execution_preflight import (
    OpenPreflightToken,
    PreflightForgery,
    PreflightRejected,
    PreflightToken,
    ReduceOnlyPreflightToken,
    mint_open_token,
    mint_reduce_only_token,
)

COMMITTED = {
    "agent_rules": {"enabled": False, "paper_trading": {"enabled": False}},
    "risk_rules": {"live_trading": {"enabled": False}},
}


def _open_intent(symbol="AAPL"):
    return OrderIntent(symbol=symbol, side="buy", qty=Decimal("1"), intent_id="i-open")


def _reduce_intent(symbol="AAPL"):
    return OrderIntent(symbol=symbol, side="sell", qty=Decimal("1"), is_reducing=True, intent_id="i-red")


def _held(symbol="AAPL", qty="10"):
    return types.SimpleNamespace(symbol=symbol, qty=Decimal(qty))


class TestNonForgeable(unittest.TestCase):
    def test_token_cannot_be_constructed_directly(self):
        with self.assertRaises(PreflightForgery):
            PreflightToken(key=object(), symbol="AAPL", intent_id="x")

    def test_subclass_cannot_be_constructed_directly(self):
        with self.assertRaises(PreflightForgery):
            OpenPreflightToken(key=object(), symbol="AAPL", intent_id="x")


class TestOpenIsRejectAllInM0(unittest.TestCase):
    def test_mint_open_token_rejects_even_on_committed_config(self):
        with self.assertRaises(PreflightRejected):
            mint_open_token(COMMITTED, _open_intent())

    def test_mint_open_token_rejects_even_if_gates_were_on(self):
        armed = {"agent_rules": {"enabled": True, "paper_trading": {"enabled": True}}}
        with self.assertRaises(PreflightRejected):
            mint_open_token(armed, _open_intent())


class TestReduceOnlyMinting(unittest.TestCase):
    def test_requires_a_held_position(self):
        with self.assertRaises(PreflightRejected):
            mint_reduce_only_token(None, _reduce_intent())

    def test_requires_positive_held_qty(self):
        with self.assertRaises(PreflightRejected):
            mint_reduce_only_token(_held(qty="0"), _reduce_intent())

    def test_requires_a_decreasing_order(self):
        with self.assertRaises(PreflightRejected):
            mint_reduce_only_token(_held(), _open_intent())  # not reducing

    def test_mints_for_held_decreasing(self):
        token = mint_reduce_only_token(_held(), _reduce_intent())
        self.assertIsInstance(token, ReduceOnlyPreflightToken)


class TestRequireToken(unittest.TestCase):
    def test_rejects_forged_object(self):
        fake = types.SimpleNamespace(symbol="AAPL")
        with self.assertRaises(PreflightForgery):
            require_token(_reduce_intent(), fake)

    def test_rejects_none(self):
        with self.assertRaises(PreflightForgery):
            require_token(_reduce_intent(), None)

    def test_opening_intent_needs_open_token(self):
        reduce_token = mint_reduce_only_token(_held(), _reduce_intent())
        with self.assertRaises(PreflightForgery):
            require_token(_open_intent(), reduce_token)

    def test_symbol_must_match(self):
        token = mint_reduce_only_token(_held("AAPL"), _reduce_intent("AAPL"))
        with self.assertRaises(PreflightForgery):
            require_token(_reduce_intent("MSFT"), token)

    def test_valid_token_is_single_use(self):
        token = mint_reduce_only_token(_held(), _reduce_intent())
        require_token(_reduce_intent(), token)  # first use ok
        with self.assertRaises(PreflightForgery):
            require_token(_reduce_intent(), token)  # consumed


if __name__ == "__main__":
    unittest.main()
