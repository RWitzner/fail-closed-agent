"""Preflight chokepoint, fail-closed by type (spec §5 Tier 6, invariant S4 contract).

`submit_order` is reachable only with a non-forgeable, single-use preflight token.
Tokens are split by intent: `OpenPreflightToken` (opening/increasing) is reject-all
in M0 (full preflight in M5), while `ReduceOnlyPreflightToken` is mintable only for
an existing held position + a position-decreasing order, so flatten always works.
"""
import copy
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
    is_authentic,
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

    def test_failed_require_does_not_consume(self):
        token = mint_reduce_only_token(_held("AAPL"), _reduce_intent("AAPL"))
        wrong = OrderIntent(symbol="MSFT", side="sell", qty=Decimal("1"), is_reducing=True, intent_id="x")
        with self.assertRaises(PreflightForgery):
            require_token(wrong, token)  # symbol mismatch must not consume
        require_token(_reduce_intent("AAPL"), token)  # still usable for the right intent


class TestReduceOnlyValidation(unittest.TestCase):
    """A reduce-only token must reflect the position, not the caller's self-assertion."""

    def test_buy_tagged_reducing_is_rejected(self):
        bad = OrderIntent(symbol="AAPL", side="buy", qty=Decimal("1"), is_reducing=True, intent_id="x")
        with self.assertRaises(PreflightRejected):
            mint_reduce_only_token(_held("AAPL", "10"), bad)

    def test_oversized_reduce_is_rejected(self):
        big = OrderIntent(symbol="AAPL", side="sell", qty=Decimal("1000"), is_reducing=True, intent_id="x")
        with self.assertRaises(PreflightRejected):
            mint_reduce_only_token(_held("AAPL", "10"), big)

    def test_cross_symbol_reduce_is_rejected(self):
        sell_aapl = OrderIntent(symbol="AAPL", side="sell", qty=Decimal("1"), is_reducing=True, intent_id="x")
        with self.assertRaises(PreflightRejected):
            mint_reduce_only_token(_held("MSFT", "10"), sell_aapl)

    def test_short_position_reduces_by_buying(self):
        cover = OrderIntent(symbol="AAPL", side="buy", qty=Decimal("5"), is_reducing=True, intent_id="x")
        token = mint_reduce_only_token(_held("AAPL", "-10"), cover)
        self.assertIsInstance(token, ReduceOnlyPreflightToken)

    def test_short_position_cannot_be_reduced_by_selling(self):
        sell = OrderIntent(symbol="AAPL", side="sell", qty=Decimal("5"), is_reducing=True, intent_id="x")
        with self.assertRaises(PreflightRejected):
            mint_reduce_only_token(_held("AAPL", "-10"), sell)

    def test_token_qty_is_rechecked_by_require(self):
        token = mint_reduce_only_token(_held("AAPL", "10"), _reduce_intent("AAPL"))  # sell 1
        other = OrderIntent(symbol="AAPL", side="sell", qty=Decimal("2"), is_reducing=True, intent_id="x")
        with self.assertRaises(PreflightForgery):
            require_token(other, token)


class TestSingleUseHardening(unittest.TestCase):
    def test_copy_is_forbidden(self):
        token = mint_reduce_only_token(_held(), _reduce_intent())
        with self.assertRaises(PreflightForgery):
            copy.copy(token)

    def test_deepcopy_is_forbidden(self):
        token = mint_reduce_only_token(_held(), _reduce_intent())
        with self.assertRaises(PreflightForgery):
            copy.deepcopy(token)

    def test_object_new_forgery_is_not_authentic(self):
        forged = object.__new__(OpenPreflightToken)
        self.assertFalse(is_authentic(forged))


if __name__ == "__main__":
    unittest.main()
