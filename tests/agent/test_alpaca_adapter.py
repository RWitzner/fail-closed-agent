"""M5 §R test 9 — the three-mode AlpacaPaperBroker + FakeBroker + flatten proxy
(S1, S4-broker, FD-M5-1/5/7/8/16/28).

Covers: spy default byte-identical to M0 (the test_alpaca_spy assertions re-run
against the grown class); wall 2 in spy AND order_api modes incl. the white-box
forged-path-token attempt; the frozen §G wire payload byte-shape; the
`limit_price=None` local rejection BEFORE any api call (FD-M5-1 structural — no
market-order payload shape exists); 403/422/rejected fixtures as
BrokerHttpError-as-DATA with codes; the pdt fixture through `classify_rejection`
(the FD-M4-15 latch FORWARD is the orchestrator's — deferred to §R 10);
base_url != paper host => ValueError at construction (before any SDK import);
cancel/order_status pass-through + the no-token assertion; FakeBroker's four
deterministic fill policies, the REV-2 reverse wall in both directions
(reductions NEVER namespace-gated — the M5C-1/M5C-S1 blocker pin), unmapped
symbols resting accepted, and account()/positions() parsing green through the
REAL M4 parsers; PriceCappedFlattenBroker pricing (hand-computed reduce_cap),
stale-quote acceptance, FlattenUnpriced on unmapped/unquotable, token
pass-through, and its deliberate NON-membership of the Broker Protocol (M5C-S7).

SyntheticConfinementError home (documented resolution): the class is defined in
`agent.broker.alpaca` exactly as §G pins; `fake.py` raises the SAME class via a
lazy import inside its `_place` raise branch (so `agent.broker.alpaca` never
even enters sys.modules on a healthy synthetic run) — §3's fake.py import row
stays satisfied at module scope.
"""
import sys
import types
import unittest
from decimal import Decimal
from unittest import mock

from agent import execution_preflight
from agent.broker.alpaca import (
    _PAPER_HOST,
    AlpacaAccountProvider,
    AlpacaPaperBroker,
    BrokerHttpError,
    SyntheticConfinementError,
)
from agent.broker.base import Broker, BrokerBase, OrderIntent
from agent.broker.fake import FakeBroker
from agent.broker.flatten_proxy import FlattenUnpriced, PriceCappedFlattenBroker
from agent.broker.order_state import classify_rejection, parse_order_payload
from agent.exec_reasons import BROKER_KINDS, ExecError
from agent.execution_preflight import (
    PreflightForgery,
    PreflightRejected,
    authorization_of,
    mint_open_token,
    mint_reduce_only_token,
    unbind_runtime,
    void_token,
)
from agent.quote_quality import QuoteSnapshot
from agent.risk.account_state import (
    BrokerAccountRead,
    parse_account_payload,
    parse_positions_payload,
)
from tests.lib.alpaca_fixtures import (
    BrokerTimeout,
    ScriptedOrderApi,
    account_payload,
    http_error,
    order_payload,
    order_pending_cancel_payload,
    positions_payload,
    submit_then_found_script,
)
from tests.lib.fakes import FakeClock, SpyBroker

COMMITTED = {"agent_rules": {"enabled": False, "paper_trading": {"enabled": False}}}


def _reduce_intent(*, symbol="AAPL", side="sell", qty="1", limit=None,
                   intent_id="r1"):
    return OrderIntent(
        symbol=symbol, side=side, qty=Decimal(qty),
        limit_price=None if limit is None else Decimal(limit),
        is_reducing=True, intent_id=intent_id)


def _held(*, symbol="AAPL", qty="10"):
    return types.SimpleNamespace(symbol=symbol, qty=Decimal(qty))


def _snap(*, symbol="AAPL", instrument_id=1001, bid="189.90", ask="190.00",
          seen_at_ms=10_250):
    return QuoteSnapshot(
        symbol=symbol, instrument_id=instrument_id,
        bid=None if bid is None else Decimal(bid),
        ask=None if ask is None else Decimal(ask),
        bid_sz=Decimal("5"), ask_sz=Decimal("5"),
        ts_event_utc="2026-06-08T14:30:00.000000Z",
        ts_recv_utc="2026-06-08T14:30:00.010000Z",
        seen_at_ms=seen_at_ms, reconnect_epoch=0, vendor_seq=1,
        dataset="EQUS.MINI", schema="tbbo")


class _QuoteViewStub:
    """quote_view.latest(symbol, instrument_id) over a fixed dict."""

    def __init__(self, quotes):
        self._quotes = dict(quotes)

    def latest(self, symbol, instrument_id):
        return self._quotes.get((symbol, instrument_id))


def _fake(*, quotes=None, instrument_ids=None, fill_policy="immediate_full",
          starting_cash=Decimal("100000")):
    return FakeBroker(
        quote_view=_QuoteViewStub(
            {("AAPL", 1001): _snap()} if quotes is None else quotes),
        clock=FakeClock(start_ms=10_250),
        instrument_ids={"AAPL": 1001} if instrument_ids is None else instrument_ids,
        starting_cash=starting_cash,
        fill_policy=fill_policy)


# --------------------------------------------------------------------------- #
# Spy default mode: byte-identical to M0 (the test_alpaca_spy assertions
# re-run against the grown class) + kind.
# --------------------------------------------------------------------------- #


class TestSpyDefaultByteIdenticalToM0(unittest.TestCase):
    def test_submit_without_token_raises(self):
        b = AlpacaPaperBroker()
        with self.assertRaises(PreflightForgery):
            b.submit_order(_reduce_intent(), None)
        self.assertEqual(b.submitted, [])

    def test_submit_with_forged_token_raises(self):
        b = AlpacaPaperBroker()
        with self.assertRaises(PreflightForgery):
            b.submit_order(_reduce_intent(), types.SimpleNamespace(symbol="AAPL"))
        self.assertEqual(b.submitted, [])

    def test_records_accepted_reduce_only_order_with_m0_ack_shape(self):
        b = AlpacaPaperBroker()
        intent = _reduce_intent()
        token = mint_reduce_only_token(_held(), intent)
        ack = b.submit_order(intent, token)
        self.assertEqual(len(b.submitted), 1)
        self.assertIs(b.submitted[0], intent)
        # The M0 ack dict, byte-identical.
        self.assertEqual(ack, {"order_id": "r1", "status": "accepted_paper_stub"})

    def test_positions_and_account_keep_m0_shapes(self):
        b = AlpacaPaperBroker()
        positions = b.positions()
        self.assertEqual(positions, {})
        self.assertIsNot(positions, b._positions)  # M0 returned a copy
        self.assertEqual(b.account(),
                         {"equity": Decimal("0"), "buying_power": Decimal("0")})

    def test_cannot_open_on_committed_config(self):
        # FD-M5-14 / S1: the mint terminates at run_gates byte-exactly.
        from agent.exec_reasons import PREFLIGHT_STAGES
        from tests.agent.test_execution_preflight_m5 import golden_inputs

        with self.assertRaises(PreflightRejected) as caught:
            mint_open_token(golden_inputs(gates_config=COMMITTED))
        reject = caught.exception.reject
        self.assertEqual(reject.reasons, ("run_gates_off",))
        self.assertEqual(reject.gate_stage, "run_gates")
        self.assertEqual(reject.stages_skipped, PREFLIGHT_STAGES[1:])
        self.assertIsNone(reject.capped_limit)

    def test_kind_is_alpaca_paper_and_in_vocabulary(self):
        self.assertEqual(AlpacaPaperBroker.kind, "alpaca_paper")
        self.assertIn(AlpacaPaperBroker.kind, BROKER_KINDS)

    def test_spy_mode_cancel_and_status_are_not_implemented(self):
        b = AlpacaPaperBroker()
        with self.assertRaises(NotImplementedError):
            b.cancel_order("o-1")
        with self.assertRaises(NotImplementedError):
            b.order_status("o-1")

    def test_module_does_not_import_alpaca_sdk(self):
        self.assertNotIn("alpaca", sys.modules)


# --------------------------------------------------------------------------- #
# Wall 2 (FD-M5-8/28): first line of _place in ALL modes — the position-of-
# record seam refuses the synthetic namespace even against a forged-path token.
# --------------------------------------------------------------------------- #


class TestWall2SyntheticRefusal(unittest.TestCase):
    def test_spy_mode_refuses_synthetic_intent_with_real_reduce_token(self):
        # White-box forged-path attempt: a REAL reduce token for a held position
        # (reduce auth binds symbol/side/qty only, never intent_id) carrying a
        # synthetic-namespaced intent. AlpacaPaperBroker's wall is UNCONDITIONAL
        # (even reducing intents): the synthetic namespace never reaches the
        # position-of-record seam, in any mode.
        b = AlpacaPaperBroker()
        intent = _reduce_intent(intent_id="synthetic-r1")
        token = mint_reduce_only_token(_held(), intent)
        with self.assertRaises(SyntheticConfinementError):
            b.submit_order(intent, token)
        self.assertEqual(b.submitted, [])               # zero _place effect
        self.assertIsNone(authorization_of(token))      # token spent, not reusable

    def test_order_api_mode_refuses_synthetic_intent_before_any_wire_call(self):
        api = ScriptedOrderApi({"submit": [order_payload()]})
        b = AlpacaPaperBroker(order_api=api)
        intent = _reduce_intent(intent_id="synthetic-r1", limit="100.25")
        token = mint_reduce_only_token(_held(), intent)
        with self.assertRaises(SyntheticConfinementError):
            b.submit_order(intent, token)
        self.assertEqual(api.calls, [])                 # nothing touched the wire

    def test_wall_precedes_the_unpriceable_local_rejection(self):
        # Ordering pin: wall 2 is the FIRST line — a synthetic intent with
        # limit_price=None raises, it is never returned as a BrokerHttpError.
        api = ScriptedOrderApi()
        b = AlpacaPaperBroker(order_api=api)
        intent = _reduce_intent(intent_id="synthetic-r1", limit=None)
        token = mint_reduce_only_token(_held(), intent)
        with self.assertRaises(SyntheticConfinementError):
            b.submit_order(intent, token)
        self.assertEqual(api.calls, [])

    def test_error_class_is_exec_error_subclass(self):
        self.assertTrue(issubclass(SyntheticConfinementError, ExecError))


# --------------------------------------------------------------------------- #
# §G wire payload (A8 frozen shape) + the FD-M5-1 structural no-market rule.
# --------------------------------------------------------------------------- #


class TestWirePayload(unittest.TestCase):
    def test_wire_payload_byte_shape(self):
        ack_fixture = order_payload(client_order_id="o-w-1")
        api = ScriptedOrderApi({"submit": [ack_fixture]})
        b = AlpacaPaperBroker(order_api=api)
        intent = _reduce_intent(qty="5", limit="100.25", intent_id="o-w-1")
        token = mint_reduce_only_token(_held(), intent)
        ack = b.submit_order(intent, token)
        self.assertEqual(api.submit_calls, [{
            "symbol": "AAPL",
            "qty": "5",                       # int-string
            "side": "sell",
            "type": "limit",                  # the ONLY order type in the codebase
            "time_in_force": "day",
            "limit_price": "100.25",          # Decimal-string
            "extended_hours": False,
            "client_order_id": "o-w-1",       # = our deterministic order_id (FD-M5-7)
        }])
        # Frozen key SET exactly — no extra keys can ride along.
        self.assertEqual(
            set(api.submit_calls[0]),
            {"symbol", "qty", "side", "type", "time_in_force", "limit_price",
             "extended_hours", "client_order_id"})
        self.assertIs(ack, ack_fixture)        # RAW ack returned untouched

    def test_qty_renders_as_int_string_for_decimal_qty_forms(self):
        api = ScriptedOrderApi({"submit": [order_payload()]})
        b = AlpacaPaperBroker(order_api=api)
        intent = _reduce_intent(qty="10", limit="1.01", intent_id="o-w-2")
        token = mint_reduce_only_token(_held(), intent)
        b.submit_order(intent, token)
        self.assertEqual(api.submit_calls[0]["qty"], "10")
        self.assertIsInstance(api.submit_calls[0]["qty"], str)

    def test_limit_price_none_is_locally_rejected_before_any_api_call(self):
        # FD-M5-1 structural: a limit_price=None intent is unserializable — the
        # local BrokerHttpError-shaped rejection ("unpriceable") is returned as
        # DATA before any wire attempt.
        api = ScriptedOrderApi({"submit": [order_payload()]})
        b = AlpacaPaperBroker(order_api=api)
        intent = _reduce_intent(limit=None, intent_id="o-w-3")
        token = mint_reduce_only_token(_held(), intent)
        result = b.submit_order(intent, token)
        self.assertIsInstance(result, BrokerHttpError)
        self.assertEqual(result.message, "unpriceable")
        self.assertEqual(result.status_code, 0)   # local: no HTTP exchange happened
        self.assertIsNone(result.code)
        self.assertEqual(api.calls, [])           # ZERO submit (or any wire) calls
        self.assertEqual(api.submit_calls, [])


# --------------------------------------------------------------------------- #
# Order-api networking discipline: BrokerHttpError is returned as DATA.
# --------------------------------------------------------------------------- #


class TestBrokerHttpErrorAsData(unittest.TestCase):
    def _submit(self, script_step, *, intent_id="o-e-1"):
        api = ScriptedOrderApi({"submit": [script_step]})
        b = AlpacaPaperBroker(order_api=api)
        intent = _reduce_intent(limit="100.25", intent_id=intent_id)
        token = mint_reduce_only_token(_held(), intent)
        return b.submit_order(intent, token), api

    def test_403_insufficient_bp_fixture_returned_as_data_with_codes(self):
        result, _ = self._submit(http_error("order_rejected_insufficient_bp"))
        self.assertIsInstance(result, BrokerHttpError)
        self.assertEqual(result.status_code, 403)
        self.assertEqual(result.code, 40310000)
        self.assertIn("Buying power", result.message)

    def test_422_subpenny_fixture_returned_as_data_with_codes(self):
        result, _ = self._submit(http_error("order_rejected_subpenny"))
        self.assertIsInstance(result, BrokerHttpError)
        self.assertEqual(result.status_code, 422)
        self.assertEqual(result.code, 42210000)

    def test_rejected_status_payload_is_raw_data_through_the_parse_chokepoint(self):
        rejected = order_payload(status="rejected", client_order_id="o-e-2")
        result, _ = self._submit(rejected, intent_id="o-e-2")
        self.assertIs(result, rejected)            # raw ack dict, returned as-is
        parsed = parse_order_payload(result, source="alpaca_paper")
        self.assertEqual(parsed.state, "rejected")

    def test_pdt_fixture_classifies_with_marker_matched(self):
        # The FD-M4-15 latch FORWARD is the orchestrator's (§R 10) — here we pin
        # that the adapter's data + classify_rejection carry the marker.
        result, _ = self._submit(http_error("order_rejected_pdt"))
        self.assertIsInstance(result, BrokerHttpError)
        rejection = classify_rejection(
            http_status=result.status_code, code=result.code,
            message=result.message)
        self.assertTrue(rejection.pdt_marker_matched)
        self.assertEqual(rejection.code, 40310100)

    def test_broker_timeout_propagates_never_swallowed_as_data(self):
        # FD-M5-17: a lost response is AMBIGUOUS — the adapter must not coerce it
        # into a rejection; the orchestrator owns the recovery query.
        api = ScriptedOrderApi(submit_then_found_script(client_order_id="o-t-1"))
        b = AlpacaPaperBroker(order_api=api)
        intent = _reduce_intent(limit="100.25", intent_id="o-t-1")
        token = mint_reduce_only_token(_held(), intent)
        with self.assertRaises(BrokerTimeout):
            b.submit_order(intent, token)
        self.assertEqual(len(api.submit_calls), 1)
        # The recovery script's found-order answer is still queued for the caller.
        self.assertEqual(
            api.get_by_client_order_id("o-t-1")["client_order_id"], "o-t-1")


# --------------------------------------------------------------------------- #
# Ctor modes (§G): the paper-host pin, the SDK confinement, mode exclusivity.
# --------------------------------------------------------------------------- #


class TestCtorModes(unittest.TestCase):
    def test_credentials_loader_with_wrong_base_url_raises_at_construction(self):
        loader = lambda: {"key_id": "k", "secret": "s",  # noqa: E731
                          "base_url": "https://api.alpaca.markets"}
        with self.assertRaises(ValueError):
            AlpacaPaperBroker(credentials_loader=loader)
        self.assertNotIn("alpaca", sys.modules)   # rejected BEFORE any SDK import

    def test_credentials_loader_without_base_url_raises_at_construction(self):
        with self.assertRaises(ValueError):
            AlpacaPaperBroker(credentials_loader=lambda: {"key_id": "k",
                                                          "secret": "s"})
        self.assertNotIn("alpaca", sys.modules)

    def test_both_order_api_and_credentials_loader_raise(self):
        with self.assertRaises(ValueError):
            AlpacaPaperBroker(order_api=ScriptedOrderApi(),
                              credentials_loader=lambda: {})

    def test_paper_host_constant_is_pinned(self):
        self.assertEqual(_PAPER_HOST, "https://paper-api.alpaca.markets")


# --------------------------------------------------------------------------- #
# cancel_order / order_status: tokenless pass-through (FD-M5-23).
# --------------------------------------------------------------------------- #


class TestCancelAndOrderStatus(unittest.TestCase):
    def test_order_status_pass_through_raw(self):
        found = order_payload(client_order_id="o-c-1")
        api = ScriptedOrderApi({"get_by_client_order_id": [found]})
        b = AlpacaPaperBroker(order_api=api)
        self.assertIs(b.order_status("o-c-1"), found)
        self.assertEqual(api.get_calls, ["o-c-1"])

    def test_cancel_order_resolves_broker_id_then_cancels_raw(self):
        found = order_payload(client_order_id="o-c-2")
        pending = order_pending_cancel_payload(client_order_id="o-c-2")
        api = ScriptedOrderApi({"get_by_client_order_id": [found],
                                "cancel": [pending]})
        b = AlpacaPaperBroker(order_api=api)
        result = b.cancel_order("o-c-2")
        self.assertIs(result, pending)
        self.assertEqual(api.cancel_calls, [found["id"]])

    def test_cancel_and_status_never_mint_and_never_require_token(self):
        # FD-M5-23: cancel is risk-reducing-or-neutral and must NEVER be gated —
        # spy on the module functions and assert zero calls.
        found = order_payload(client_order_id="o-c-3")
        pending = order_pending_cancel_payload(client_order_id="o-c-3")
        api = ScriptedOrderApi({
            "get_by_client_order_id": [found, order_payload(client_order_id="o-c-3")],
            "cancel": [pending]})
        b = AlpacaPaperBroker(order_api=api)
        with mock.patch("agent.broker.base.require_token") as require_spy, \
                mock.patch("agent.execution_preflight.consume") as consume_spy, \
                mock.patch("agent.execution_preflight.mint_open_token") as open_spy, \
                mock.patch("agent.execution_preflight.mint_reduce_only_token") as reduce_spy:
            b.cancel_order("o-c-3")
            b.order_status("o-c-3")
        self.assertEqual(require_spy.call_count, 0)
        self.assertEqual(consume_spy.call_count, 0)
        self.assertEqual(open_spy.call_count, 0)
        self.assertEqual(reduce_spy.call_count, 0)

    def test_http_error_on_cancel_lookup_returned_as_data(self):
        api = ScriptedOrderApi(
            {"get_by_client_order_id": [http_error("order_rejected_insufficient_bp")]})
        b = AlpacaPaperBroker(order_api=api)
        result = b.cancel_order("o-c-4")
        self.assertIsInstance(result, BrokerHttpError)
        self.assertEqual(api.cancel_calls, [])


# --------------------------------------------------------------------------- #
# AlpacaAccountProvider (LD8 near-passthrough) -> the REAL M4 parsers.
# --------------------------------------------------------------------------- #


class TestAlpacaAccountProvider(unittest.TestCase):
    def test_account_payload_raw_and_parses_green(self):
        fixture = account_payload()
        api = ScriptedOrderApi({"get_account": [fixture]})
        provider = AlpacaAccountProvider(api)
        raw = provider.account_payload()
        self.assertIs(raw, fixture)
        read = parse_account_payload(raw, source="alpaca_paper", seen_at_ms=0,
                                     ts_read_utc="2026-06-10T14:00:00Z")
        self.assertIsInstance(read, BrokerAccountRead)
        self.assertEqual(read.equity, Decimal("100000.00"))

    def test_positions_payload_raw_and_parses_green(self):
        fixture = positions_payload()
        api = ScriptedOrderApi({"list_positions": [fixture]})
        provider = AlpacaAccountProvider(api)
        raw = provider.positions_payload()
        self.assertIs(raw, fixture)
        portfolio = parse_positions_payload(raw, source="alpaca_paper",
                                            seen_at_ms=0)
        self.assertEqual(len(portfolio.positions), 1)
        self.assertEqual(portfolio.positions[0].symbol, "AAPL")
        self.assertEqual(portfolio.positions[0].qty, Decimal("10"))

    def test_broker_positions_and_account_return_raw_in_order_api_mode(self):
        account_fixture = account_payload()
        positions_fixture = positions_payload()
        api = ScriptedOrderApi({"get_account": [account_fixture],
                                "list_positions": [positions_fixture]})
        b = AlpacaPaperBroker(order_api=api)
        self.assertIs(b.account(), account_fixture)
        self.assertIs(b.positions(), positions_fixture)


# --------------------------------------------------------------------------- #
# FakeBroker (§H.1 rev 2) — the REV-2 reverse wall, both directions.
# --------------------------------------------------------------------------- #


class TestFakeBrokerReverseWall(unittest.TestCase):
    """Open-token cases need the consume-time runtime bound (FD-M5-13)."""

    def setUp(self):
        unbind_runtime()
        execution_preflight.bind_runtime(
            clock=FakeClock(start_ms=10_250), kill_generation_source=lambda: 0)

    def tearDown(self):
        unbind_runtime()
        from tests.agent.test_execution_preflight_m5 import purge_open_authorizations
        purge_open_authorizations()

    def _open_token(self):
        from tests.agent.test_execution_preflight_m5 import golden_inputs
        token, ppass = mint_open_token(golden_inputs())
        # Preconditions for the bound intent below.
        self.assertEqual(ppass.symbol, "AAPL")
        self.assertEqual(ppass.qty, Decimal("10"))
        self.assertEqual(ppass.capped_limit, Decimal("190.00"))
        return token

    def _open_intent(self, intent_id):
        return OrderIntent(symbol="AAPL", side="buy", qty=Decimal("10"),
                           limit_price=Decimal("190.00"), is_reducing=False,
                           intent_id=intent_id)

    def test_opening_non_synthetic_intent_is_refused(self):
        fake = _fake()
        token = self._open_token()
        with self.assertRaises(SyntheticConfinementError):
            fake.submit_order(self._open_intent("o-real-1"), token)
        self.assertEqual(fake.positions(), [])           # zero _place effect
        self.assertIsNone(authorization_of(token))       # token spent either way

    def test_synthetic_prefixed_opening_is_accepted_and_fills(self):
        fake = _fake()  # ask 190.00; limit 190.00 — boundary-equal IS marketable
        token = self._open_token()
        ack = fake.submit_order(self._open_intent("synthetic-o-1"), token)
        self.assertEqual(ack["status"], "filled")
        self.assertEqual(ack["filled_qty"], "10")
        self.assertEqual(Decimal(ack["filled_avg_price"]), Decimal("190.00"))
        self.assertEqual(ack["client_order_id"], "synthetic-o-1")

    def test_reducing_unprefixed_intent_is_accepted_rev2_blocker_pin(self):
        # The M5C-1/M5C-S1 blocker fix: reductions are NEVER namespace-gated —
        # the M0 kill actuator's `flatten-<symbol>` intent_ids must pass.
        fake = _fake()
        intent = _reduce_intent(qty="4", limit="189.90", intent_id="flatten-AAPL")
        token = mint_reduce_only_token(_held(), intent)
        ack = fake.submit_order(intent, token)
        self.assertEqual(ack["status"], "filled")        # sell at the bid 189.90
        self.assertEqual(Decimal(ack["filled_avg_price"]), Decimal("189.90"))

    def test_wall_precedes_unmapped_symbol_handling(self):
        # Ordering pin: an opening un-prefixed intent on an UNMAPPED symbol is
        # refused by the wall, never rested.
        fake = _fake(instrument_ids={})
        token = self._open_token()
        with self.assertRaises(SyntheticConfinementError):
            fake.submit_order(self._open_intent("o-real-2"), token)

    def test_fake_broker_raises_the_same_class_as_the_alpaca_home(self):
        # The documented home resolution: ONE class, defined in agent.broker.alpaca
        # (§G), raised by fake.py via lazy import — identity, not a lookalike.
        fake = _fake()
        token = self._open_token()
        try:
            fake.submit_order(self._open_intent("o-real-3"), token)
        except SyntheticConfinementError as exc:
            self.assertIs(type(exc), SyntheticConfinementError)
        else:
            self.fail("reverse wall did not fire")


class TestFakeBrokerLifecycles(unittest.TestCase):
    def test_token_gating_holds_on_the_fake(self):
        # Extends BrokerBase => even the FakeBroker is token-gated (S1).
        fake = _fake()
        with self.assertRaises(PreflightForgery):
            fake.submit_order(_reduce_intent(intent_id="flatten-AAPL"), None)

    def test_kind_is_fake_and_in_vocabulary(self):
        self.assertEqual(FakeBroker.kind, "fake")
        self.assertIn(FakeBroker.kind, BROKER_KINDS)
        self.assertIsInstance(_fake(), BrokerBase)

    def test_ctor_rejects_out_of_vocab_fill_policy(self):
        with self.assertRaises(ExecError):
            _fake(fill_policy="lucky_dip")

    def test_ctor_rejects_float_starting_cash(self):
        with self.assertRaises(ExecError):
            _fake(starting_cash=100000.0)

    def _submit_reduce(self, fake, *, qty="10", limit="189.90",
                       intent_id="flatten-AAPL"):
        intent = _reduce_intent(qty=qty, limit=limit, intent_id=intent_id)
        token = mint_reduce_only_token(_held(qty=qty), intent)
        return fake.submit_order(intent, token)

    def test_immediate_full_marketable_sell_fills_at_the_bid(self):
        fake = _fake(fill_policy="immediate_full")
        ack = self._submit_reduce(fake)   # limit 189.90 <= bid 189.90: marketable
        self.assertEqual(ack["status"], "filled")
        self.assertEqual(ack["filled_qty"], "10")
        self.assertEqual(Decimal(ack["filled_avg_price"]), Decimal("189.90"))
        # Terminal state is stable under re-polls.
        again = fake.order_status("flatten-AAPL")
        self.assertEqual(again["status"], "filled")

    def test_immediate_full_unmarketable_rests_accepted(self):
        fake = _fake(fill_policy="immediate_full")
        ack = self._submit_reduce(fake, limit="195.00")  # sell above the bid
        self.assertEqual(ack["status"], "new")
        self.assertEqual(ack["filled_qty"], "0")
        self.assertEqual(fake.order_status("flatten-AAPL")["status"], "new")

    def test_partial_then_full_is_scripted_30_percent_then_remainder(self):
        fake = _fake(fill_policy="partial_then_full")
        ack = self._submit_reduce(fake)
        self.assertEqual(ack["status"], "new")
        self.assertEqual(ack["filled_qty"], "0")
        poll1 = fake.order_status("flatten-AAPL")
        self.assertEqual(poll1["status"], "partially_filled")
        self.assertEqual(poll1["filled_qty"], "3")       # floor(10 * 0.30)
        self.assertEqual(Decimal(poll1["filled_avg_price"]), Decimal("189.90"))
        poll2 = fake.order_status("flatten-AAPL")
        self.assertEqual(poll2["status"], "filled")
        self.assertEqual(poll2["filled_qty"], "10")
        self.assertEqual(Decimal(poll2["filled_avg_price"]), Decimal("189.90"))
        poll3 = fake.order_status("flatten-AAPL")        # stable after terminal
        self.assertEqual(poll3["status"], "filled")
        self.assertEqual(poll3["filled_qty"], "10")

    def test_never_fill_rests_until_canceled(self):
        fake = _fake(fill_policy="never_fill")
        ack = self._submit_reduce(fake)                  # marketable but never fills
        self.assertEqual(ack["status"], "new")
        self.assertEqual(fake.order_status("flatten-AAPL")["status"], "new")
        self.assertEqual(fake.order_status("flatten-AAPL")["status"], "new")
        canceled = fake.cancel_order("flatten-AAPL")
        self.assertEqual(canceled["status"], "canceled")
        self.assertEqual(canceled["filled_qty"], "0")
        self.assertEqual(fake.order_status("flatten-AAPL")["status"], "canceled")

    def test_reject_all_returns_rejected_payloads(self):
        fake = _fake(fill_policy="reject_all")
        ack = self._submit_reduce(fake)
        self.assertEqual(ack["status"], "rejected")
        self.assertEqual(ack["filled_qty"], "0")
        parsed = parse_order_payload(ack, source="fake")
        self.assertEqual(parsed.state, "rejected")

    def test_unmapped_symbol_rests_accepted_never_a_synthesized_price(self):
        fake = _fake(instrument_ids={})                  # M5C-B3
        ack = self._submit_reduce(fake)                  # would be marketable if quotable
        self.assertEqual(ack["status"], "new")
        self.assertEqual(fake.order_status("flatten-AAPL")["status"], "new")
        self.assertIsNone(fake.order_status("flatten-AAPL")["filled_avg_price"])

    def test_missing_quote_rests_accepted(self):
        fake = _fake(quotes={})                          # mapped, but no quote
        ack = self._submit_reduce(fake)
        self.assertEqual(ack["status"], "new")
        self.assertEqual(fake.order_status("flatten-AAPL")["status"], "new")

    def test_cancel_after_terminal_returns_the_terminal_payload(self):
        fake = _fake(fill_policy="immediate_full")
        self._submit_reduce(fake)
        result = fake.cancel_order("flatten-AAPL")
        self.assertEqual(result["status"], "filled")     # already terminal

    def test_acks_parse_through_the_one_order_chokepoint(self):
        fake = _fake(fill_policy="immediate_full")
        ack = self._submit_reduce(fake)
        parsed = parse_order_payload(ack, source="fake")
        self.assertEqual(parsed.state, "filled")
        self.assertEqual(parsed.filled_qty, Decimal("10"))
        self.assertEqual(parsed.client_order_id, "flatten-AAPL")


class TestFakeBrokerAccountAndPositions(unittest.TestCase):
    """M4-wire-shaped account()/positions() — green through the REAL M4 parsers
    (the §H.1 'identical code paths' pin). Parsed with source='fixture' (the
    in-memory synthetic-mode source; ∈ M4 ACCOUNT_SOURCES)."""

    def setUp(self):
        unbind_runtime()
        execution_preflight.bind_runtime(
            clock=FakeClock(start_ms=10_250), kill_generation_source=lambda: 0)

    def tearDown(self):
        unbind_runtime()
        from tests.agent.test_execution_preflight_m5 import purge_open_authorizations
        purge_open_authorizations()

    def _seeded_fake(self):
        from tests.agent.test_execution_preflight_m5 import golden_inputs
        fake = _fake(fill_policy="immediate_full")
        token, _ = mint_open_token(golden_inputs())
        intent = OrderIntent(symbol="AAPL", side="buy", qty=Decimal("10"),
                             limit_price=Decimal("190.00"), is_reducing=False,
                             intent_id="synthetic-o-seed")
        ack = fake.submit_order(intent, token)
        self.assertEqual(ack["status"], "filled")
        return fake

    def test_account_parses_green_with_exact_bookkeeping(self):
        fake = self._seeded_fake()
        payload = fake.account()
        read = parse_account_payload(payload, source="fixture", seen_at_ms=0,
                                     ts_read_utc="2026-06-10T14:00:00Z")
        self.assertIsInstance(read, BrokerAccountRead)
        self.assertEqual(read.cash, Decimal("98100.00"))       # 100000 - 10x190.00
        self.assertEqual(read.equity, Decimal("100000.00"))    # cash + cost basis
        self.assertEqual(read.last_equity, Decimal("100000"))  # starting cash
        self.assertEqual(read.buying_power, Decimal("98100.00"))

    def test_positions_parse_green_with_exact_bookkeeping(self):
        fake = self._seeded_fake()
        portfolio = parse_positions_payload(fake.positions(), source="fixture",
                                            seen_at_ms=0)
        self.assertEqual(len(portfolio.positions), 1)
        position = portfolio.positions[0]
        self.assertEqual(position.symbol, "AAPL")
        self.assertEqual(position.qty, Decimal("10"))
        self.assertEqual(position.market_value, Decimal("1900.00"))
        self.assertEqual(position.avg_entry_price, Decimal("190"))
        self.assertEqual(position.instrument_id, 1001)

    def test_flat_book_emits_no_position_rows(self):
        fake = _fake()
        self.assertEqual(fake.positions(), [])
        read = parse_account_payload(fake.account(), source="fixture",
                                     seen_at_ms=0,
                                     ts_read_utc="2026-06-10T14:00:00Z")
        self.assertEqual(read.equity, Decimal("100000"))

    def test_reduce_fill_moves_cash_and_shrinks_the_position(self):
        fake = self._seeded_fake()
        intent = _reduce_intent(qty="4", limit="189.90", intent_id="flatten-AAPL")
        token = mint_reduce_only_token(_held(), intent)
        fake.submit_order(intent, token)                 # sells 4 at bid 189.90
        read = parse_account_payload(fake.account(), source="fixture",
                                     seen_at_ms=0,
                                     ts_read_utc="2026-06-10T14:00:00Z")
        self.assertEqual(read.cash, Decimal("98859.60"))  # 98100 + 4x189.90
        portfolio = parse_positions_payload(fake.positions(), source="fixture",
                                            seen_at_ms=0)
        self.assertEqual(portfolio.positions[0].qty, Decimal("6"))
        self.assertEqual(portfolio.positions[0].market_value,
                         Decimal("1140.00"))             # 1900 - 4x190 cost basis


# --------------------------------------------------------------------------- #
# PriceCappedFlattenBroker (§H.2 rev 2) — the FD-M5-1 actuator.
# --------------------------------------------------------------------------- #


class _InnerStub:
    """Sentinel-returning inner for the pass-through asserts."""

    def __init__(self):
        self.positions_sentinel = object()
        self.account_sentinel = object()

    def submit_order(self, intent, token):
        raise AssertionError("not used")

    def positions(self):
        return self.positions_sentinel

    def account(self):
        return self.account_sentinel


class TestPriceCappedFlattenBroker(unittest.TestCase):
    def _proxy(self, *, inner=None, quotes=None, instrument_ids=None,
               cap_bps=Decimal("25")):
        return PriceCappedFlattenBroker(
            inner=SpyBroker() if inner is None else inner,
            quote_view=_QuoteViewStub(
                {("XY", 7): _snap(symbol="XY", instrument_id=7, bid="1.02",
                                  ask="1.04")} if quotes is None else quotes),
            instrument_ids={"XY": 7} if instrument_ids is None else instrument_ids,
            cap_bps=cap_bps)

    def _flatten_intent(self, *, symbol="XY", qty="2"):
        return OrderIntent(symbol=symbol, side="sell", qty=Decimal(qty),
                           is_reducing=True, intent_id=f"flatten-{symbol}")

    def test_caps_a_none_limit_reduce_intent_hand_computed(self):
        # EX-7 pin: sell, bid=1.02, bps=25 => raw 1.017450 -> ROUND_DOWN grid 0.01
        # (away from the touch, one tick past the budget) => 1.01.
        inner = SpyBroker()
        proxy = self._proxy(inner=inner)
        intent = self._flatten_intent()
        token = mint_reduce_only_token(_held(symbol="XY", qty="2"), intent)
        ack = proxy.submit_order(intent, token)
        self.assertEqual(len(inner.submitted), 1)
        sent = inner.submitted[0]
        self.assertEqual(sent.limit_price, Decimal("1.01"))
        # Everything else carried verbatim — the reduce auth binds symbol/side/qty
        # only, so the SAME token stayed valid and was consumed by the inner.
        self.assertEqual((sent.symbol, sent.side, sent.qty, sent.is_reducing,
                          sent.intent_id, sent.order_type, sent.tif),
                         ("XY", "sell", Decimal("2"), True, "flatten-XY",
                          intent.order_type, intent.tif))
        self.assertIsNone(authorization_of(token))       # consumed by the inner
        self.assertEqual(ack["status"], "accepted_spy")

    def test_stale_quote_still_prices(self):
        # FD-M4-3: staleness never blocks a reduce — the proxy reads
        # quote_view.latest and never checks freshness.
        inner = SpyBroker()
        proxy = self._proxy(
            inner=inner,
            quotes={("XY", 7): _snap(symbol="XY", instrument_id=7, bid="1.02",
                                     ask="1.04", seen_at_ms=0)})  # ancient
        intent = self._flatten_intent()
        token = mint_reduce_only_token(_held(symbol="XY", qty="2"), intent)
        proxy.submit_order(intent, token)
        self.assertEqual(inner.submitted[0].limit_price, Decimal("1.01"))

    def test_unmapped_symbol_raises_flatten_unpriced(self):
        # M5C-S8: an unmapped symbol resolves like an unpriceable quote — never
        # a gate; the message string IS the journaled reason.
        inner = SpyBroker()
        proxy = self._proxy(inner=inner, instrument_ids={})
        intent = self._flatten_intent()
        token = mint_reduce_only_token(_held(symbol="XY", qty="2"), intent)
        with self.assertRaises(FlattenUnpriced) as caught:
            proxy.submit_order(intent, token)
        self.assertEqual(str(caught.exception), "no_price_for_cap")
        self.assertEqual(inner.calls, [])                # nothing reached the inner
        self.assertIsNotNone(authorization_of(token))    # token NOT consumed
        void_token(token, "no_price_for_cap")            # registry hygiene

    def test_missing_quote_raises_flatten_unpriced(self):
        inner = SpyBroker()
        proxy = self._proxy(inner=inner, quotes={})
        intent = self._flatten_intent()
        token = mint_reduce_only_token(_held(symbol="XY", qty="2"), intent)
        with self.assertRaises(FlattenUnpriced) as caught:
            proxy.submit_order(intent, token)
        self.assertEqual(str(caught.exception), "no_price_for_cap")
        self.assertEqual(inner.calls, [])
        void_token(token, "no_price_for_cap")

    def test_missing_bid_side_raises_flatten_unpriced(self):
        # reduce_cap returns None when the needed side is missing.
        inner = SpyBroker()
        proxy = self._proxy(
            inner=inner,
            quotes={("XY", 7): _snap(symbol="XY", instrument_id=7, bid=None,
                                     ask="1.04")})
        intent = self._flatten_intent()
        token = mint_reduce_only_token(_held(symbol="XY", qty="2"), intent)
        with self.assertRaises(FlattenUnpriced) as caught:
            proxy.submit_order(intent, token)
        self.assertEqual(str(caught.exception), "no_price_for_cap")
        self.assertEqual(inner.calls, [])
        void_token(token, "no_price_for_cap")

    def test_positions_and_account_pass_through(self):
        inner = _InnerStub()
        proxy = self._proxy(inner=inner)
        self.assertIs(proxy.positions(), inner.positions_sentinel)
        self.assertIs(proxy.account(), inner.account_sentinel)

    def test_proxy_is_not_a_broker_protocol_member(self):
        # M5C-S7: a submit-only shim — no kind, no cancel_order/order_status,
        # not a BrokerBase, never a journaled fill source.
        proxy = self._proxy()
        self.assertFalse(isinstance(proxy, Broker))
        self.assertFalse(isinstance(proxy, BrokerBase))
        self.assertFalse(hasattr(proxy, "kind"))
        self.assertFalse(hasattr(proxy, "cancel_order"))
        self.assertFalse(hasattr(proxy, "order_status"))

    def test_ctor_rejects_float_cap_bps(self):
        with self.assertRaises(ExecError):
            self._proxy(cap_bps=25.0)


# --------------------------------------------------------------------------- #
# SpyBroker growth (§3 table): kind + tokenless recorders.
# --------------------------------------------------------------------------- #


class TestSpyBrokerGrowth(unittest.TestCase):
    def test_kind_is_spy_and_protocol_member(self):
        spy = SpyBroker()
        self.assertEqual(spy.kind, "spy")
        self.assertIn(spy.kind, BROKER_KINDS)
        self.assertTrue(isinstance(spy, Broker))

    def test_cancel_and_status_record_without_touching_calls(self):
        spy = SpyBroker()
        self.assertEqual(spy.cancel_order("o-1"), {})
        self.assertEqual(spy.order_status("o-2"), {})
        self.assertEqual(spy.cancel_calls, ["o-1"])
        self.assertEqual(spy.status_calls, ["o-2"])
        self.assertEqual(spy.calls, [])      # submits-only (the S1 canary's list)
        self.assertEqual(spy.submitted, [])


if __name__ == "__main__":
    unittest.main()
