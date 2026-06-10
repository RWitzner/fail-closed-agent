"""Preflight chokepoint, fail-closed by type (spec §5 Tier 6, invariant S4 contract).

`submit_order` is reachable only with a non-forgeable, single-use preflight token.
Tokens are split by intent: `OpenPreflightToken` is mintable ONLY through the M5
`evaluate_preflight` ladder (reject-all on the committed config — S1), while
`ReduceOnlyPreflightToken` is mintable only for an existing held position + a
position-decreasing order, so flatten always works (FD-M4-3 — the §R 4 paired
tests prove the reduce path ignores EVERY M5 consume-time re-check).
"""
import copy
import types
import unittest
from decimal import Decimal

from agent.broker.base import BrokerBase, OrderIntent, require_token
from agent.exec_reasons import ExecError, PREFLIGHT_STAGES
from agent.execution_preflight import (
    OpenPreflightToken,
    PreflightForgery,
    PreflightRejected,
    PreflightStale,
    PreflightToken,
    ReduceOnlyPreflightToken,
    bind_runtime,
    is_authentic,
    mint_open_token,
    mint_reduce_only_token,
    unbind_runtime,
    void_token,
)
from tests.agent.test_execution_preflight_m5 import (
    golden_inputs,
    purge_open_authorizations,
)
from tests.lib.fakes import FakeClock

COMMITTED = {
    "agent_rules": {"enabled": False, "paper_trading": {"enabled": False}},
    "risk_rules": {"live_trading": {"enabled": False}},
}


class _PlaceCountingBroker(BrokerBase):
    """BrokerBase double: counts `_place` calls (the §R 4 zero-`_place` asserts)."""

    def __init__(self):
        self.placed = []

    def _place(self, intent):
        self.placed.append(intent)
        return {"order_id": intent.intent_id, "status": "accepted_test"}


def _open_order_intent(pass_, intent_id="o-open", **overrides):
    fields = dict(symbol=pass_.symbol, side=pass_.side, qty=pass_.qty,
                  limit_price=pass_.capped_limit, intent_id=intent_id)
    fields.update(overrides)
    return OrderIntent(**fields)


def _open_intent(symbol="AAPL"):
    return OrderIntent(symbol=symbol, side="buy", qty=Decimal("1"), intent_id="i-open")


def _reduce_intent(symbol="AAPL"):
    return OrderIntent(symbol=symbol, side="sell", qty=Decimal("1"), is_reducing=True, intent_id="i-red")


def _held(symbol="AAPL", qty="10"):
    return types.SimpleNamespace(symbol=symbol, qty=Decimal(qty))


class TestNonForgeable(unittest.TestCase):
    def test_token_cannot_be_constructed_with_wrong_mint(self):
        with self.assertRaises(PreflightForgery):
            PreflightToken(mint=object())

    def test_subclass_cannot_be_constructed_with_wrong_mint(self):
        with self.assertRaises(PreflightForgery):
            OpenPreflightToken(mint=object())

    def test_open_token_built_with_real_mint_is_unauthorized(self):
        # Even importing the module-private mint key, a directly-constructed open
        # token has no stored authorization, so it can never open an order (C1).
        from agent.execution_preflight import _MINT

        forged = OpenPreflightToken(mint=_MINT)
        self.assertFalse(is_authentic(forged))
        intent = OrderIntent(symbol="AAPL", side="buy", qty=Decimal("1"), intent_id="x")
        with self.assertRaises(PreflightForgery):
            require_token(intent, forged)

    def test_reduce_token_attributes_cannot_be_mutated_to_rebind(self):
        # Mutating a reduce token must not rebind it to another order (C2): the
        # authorization lives in an immutable registry, not on the token object.
        token = mint_reduce_only_token(_held("AAPL", "10"), _reduce_intent("AAPL"))  # AAPL sell 1
        try:
            token.symbol = "MSFT"
            token.side = "sell"
            token.qty = Decimal("5")
        except AttributeError:
            pass  # token is immutable (no such slots) — also acceptable
        rebind = OrderIntent(symbol="MSFT", side="sell", qty=Decimal("5"), is_reducing=True, intent_id="x")
        with self.assertRaises(PreflightForgery):
            require_token(rebind, token)


class TestOpenMintRejectAllOnCommittedConfig(unittest.TestCase):
    """FD-M5-14: the legacy 2-positional reject-all mint is REPLACED by the ladder;
    on the committed gates the reject is the byte-exact `run_gates` terminal (S1
    unchanged in substance — reject-all by ladder instead of by stub)."""

    def tearDown(self):
        purge_open_authorizations()

    def test_mint_open_token_rejects_at_run_gates_on_committed_config(self):
        with self.assertRaises(PreflightRejected) as caught:
            mint_open_token(golden_inputs(gates_config=COMMITTED))
        reject = caught.exception.reject
        self.assertEqual(reject.reasons, ("run_gates_off",))
        self.assertEqual(reject.gate_stage, "run_gates")
        self.assertEqual(reject.stages_skipped, PREFLIGHT_STAGES[1:])
        self.assertIsNone(reject.capped_limit)

    def test_mint_open_token_rejects_unless_gates_identity_true(self):
        # non-identity True ("true" strings) still reads False (identity-strict)
        hostile = {"agent_rules": {"enabled": "true",
                                   "paper_trading": {"enabled": "true"}}}
        with self.assertRaises(PreflightRejected) as caught:
            mint_open_token(golden_inputs(gates_config=hostile))
        self.assertEqual(caught.exception.reject.reasons, ("run_gates_off",))
        self.assertEqual(caught.exception.reject.gate_stage, "run_gates")


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


class _RuntimeCase(unittest.TestCase):
    """Base for the M5 mint/consume tests: registry + runtime teardown hygiene
    (the M0 spy default is UNBOUND — other suites rely on it)."""

    def setUp(self):
        self.addCleanup(purge_open_authorizations)
        self.addCleanup(unbind_runtime)
        self.clock = FakeClock(start_ms=10_250)   # == golden_inputs().now_ms
        self.generation = {"value": 0}

    def _bind(self):
        bind_runtime(clock=self.clock,
                     kill_generation_source=lambda: self.generation["value"])


class TestOpenMintBindsTheAuthorization(_RuntimeCase):
    def test_mint_on_pass_binds_side_qty_limit(self):
        from agent.execution_preflight import authorization_of

        token, pass_ = mint_open_token(golden_inputs())
        self.assertIsInstance(token, OpenPreflightToken)
        auth = authorization_of(token)
        self.assertEqual(
            (auth.kind, auth.symbol, auth.side, auth.qty, auth.limit_price),
            ("open", "AAPL", "buy", Decimal("10"), pass_.capped_limit))

    def test_intent_mutation_after_mint_is_a_forgery(self):
        self._bind()
        token, pass_ = mint_open_token(golden_inputs())
        broker = _PlaceCountingBroker()
        mutations = [
            _open_order_intent(pass_, qty=Decimal("11")),
            _open_order_intent(pass_,
                               limit_price=pass_.capped_limit + Decimal("0.01")),
            _open_order_intent(pass_, limit_price=None),
            _open_order_intent(pass_, symbol="MSFT"),
        ]
        for mutated in mutations:
            with self.assertRaises(PreflightForgery):
                broker.submit_order(mutated, token)
        self.assertEqual(broker.placed, [])
        # failed validation precedes consumption: the un-mutated intent still works
        ack = broker.submit_order(_open_order_intent(pass_), token)
        self.assertEqual(len(broker.placed), 1)
        self.assertIn("order_id", ack)


class TestConsumeTimeToctou(_RuntimeCase):
    def test_generation_bump_between_mint_and_submit_is_stale_inside_submit(self):
        self._bind()
        token, pass_ = mint_open_token(golden_inputs())
        self.generation["value"] = 1                  # kill trip after mint
        broker = _PlaceCountingBroker()
        with self.assertRaises(PreflightStale) as caught:
            broker.submit_order(_open_order_intent(pass_), token)
        self.assertEqual(caught.exception.reason, "kill_generation_changed")
        self.assertEqual(broker.placed, [])           # zero _place calls
        self.assertFalse(is_authentic(token))         # authorization revoked

    def test_token_age_2001_expires_and_2000_passes(self):
        self._bind()
        broker = _PlaceCountingBroker()
        token, pass_ = mint_open_token(golden_inputs())
        self.clock.advance(2001)
        with self.assertRaises(PreflightStale) as caught:
            broker.submit_order(_open_order_intent(pass_), token)
        self.assertEqual(caught.exception.reason, "open_token_expired")
        self.assertEqual(broker.placed, [])
        self.assertFalse(is_authentic(token))
        # strict '>': exactly OPEN_TOKEN_TTL_MS still consumes
        self.clock = FakeClock(start_ms=10_250)
        unbind_runtime()
        self._bind()
        token2, pass2 = mint_open_token(golden_inputs())
        self.clock.advance(2000)
        broker.submit_order(_open_order_intent(pass2, intent_id="o-2"), token2)
        self.assertEqual(len(broker.placed), 1)

    def test_unbound_runtime_rejects_every_open_consume(self):
        token, pass_ = mint_open_token(golden_inputs())   # mint needs no runtime
        broker = _PlaceCountingBroker()
        with self.assertRaises(PreflightStale) as caught:
            broker.submit_order(_open_order_intent(pass_), token)
        self.assertEqual(caught.exception.reason, "preflight_runtime_unbound")
        self.assertEqual(broker.placed, [])
        self.assertFalse(is_authentic(token))

    def test_preflight_stale_is_a_preflight_rejected(self):
        self.assertTrue(issubclass(PreflightStale, PreflightRejected))


class TestRuntimeBinding(_RuntimeCase):
    def test_bind_runtime_twice_raises_exec_error(self):
        self._bind()
        with self.assertRaises(ExecError):
            self._bind()

    def test_unbind_then_rebind_is_legal(self):
        self._bind()
        unbind_runtime()
        self._bind()


class TestVoidToken(_RuntimeCase):
    def test_void_token_is_idempotent_and_revokes(self):
        token, _ = mint_open_token(golden_inputs())
        void_token(token, "kill_generation_changed")
        self.assertFalse(is_authentic(token))
        void_token(token, "kill_generation_changed")   # idempotent no-op
        with self.assertRaises(PreflightForgery):
            require_token(_open_intent(), token)

    def test_void_token_accepts_extra_reject_reasons(self):
        token, _ = mint_open_token(golden_inputs())
        void_token(token, "no_price_for_cap")
        self.assertFalse(is_authentic(token))

    def test_void_token_out_of_vocab_reason_raises_and_keeps_the_auth(self):
        token, _ = mint_open_token(golden_inputs())
        with self.assertRaises(ExecError):
            void_token(token, "because_reasons")
        self.assertTrue(is_authentic(token))           # validated BEFORE deletion
        void_token(token, "open_token_expired")


class TestReducePathIgnoresM5Rechecks(_RuntimeCase):
    """Paired FD-M4-3 tests: the reduce mint + consume succeed unchanged under
    EVERY M5 consume-time condition — no M5 code path may block a reduction."""

    def test_reduce_succeeds_under_unbound_runtime(self):
        token = mint_reduce_only_token(_held(), _reduce_intent())
        broker = _PlaceCountingBroker()
        broker.submit_order(_reduce_intent(), token)   # no runtime bound
        self.assertEqual(len(broker.placed), 1)

    def test_reduce_succeeds_under_expired_clock(self):
        self._bind()
        token = mint_reduce_only_token(_held(), _reduce_intent())
        self.clock.advance(1_000_000)
        broker = _PlaceCountingBroker()
        broker.submit_order(_reduce_intent(), token)
        self.assertEqual(len(broker.placed), 1)

    def test_reduce_succeeds_under_bumped_generation(self):
        self._bind()
        token = mint_reduce_only_token(_held(), _reduce_intent())
        self.generation["value"] = 7
        broker = _PlaceCountingBroker()
        broker.submit_order(_reduce_intent(), token)
        self.assertEqual(len(broker.placed), 1)


if __name__ == "__main__":
    unittest.main()
