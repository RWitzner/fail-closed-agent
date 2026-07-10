"""M5 §R test 15 — the S8 drill through `PriceCappedFlattenBroker`. [S8, S1, FD-M5-1/25]

Covers, verbatim from the contract (rev 2):

- every intent reaching the INNER broker has ``is_reducing=True`` AND a tick-valid
  ``limit_price`` (no ``limit=None`` passes through — the proxy adds a PRICE, §H.2);
- THE SYNTHETIC-COMPOSITION DRILL (the M5C-1/M5C-S1 blocker pin): kill flatten
  through ``PriceCappedFlattenBroker`` over a ``FakeBroker`` inner succeeds with
  ZERO ``failed[]`` — the un-prefixed ``flatten-<symbol>`` intents pass the
  FD-M5-8 rev-2 reverse wall because they are reducing;
- a STALE quote still prices (staleness never blocks a reduce — FD-M4-3);
- a no-price symbol ⇒ ``FlattenUnpriced`` ⇒ ``failed[]``/``residual`` with
  ``no_price_for_cap`` ⇒ ``retry_residual`` succeeds once quotable; an UNMAPPED
  symbol resolves identically (M5C-S8); an unusable touch resolves identically;
- cancel-opens-first ordering (the kill_trip cancel precedes the flatten submits),
  driven via the ORCHESTRATOR's §M.6 sequence;
- the trigger's journaled ``stale_inputs`` reflects ``AccountStore.get`` freshness
  (M5C-B5) — unit-level at the strict-'>' TTL boundary AND through §M.6;
- a canceled opening order filling late post-kill ⇒
  ``order_state_alert{note:"late_fill_post_kill"}`` (M5C-S11);
- the generation bump kills an outstanding open token at consume (FD-M5-13),
  plus the §M.6 step-2 ``void_token`` path for a token outstanding mid-submit;
- zero open-kind authorizations after the drill;
- ``close_position(reason="kill_flatten")`` rows.

Both drive levels per the contract: (a) UNIT — the proxy over a FakeBroker inner
directly under ``RiskKillSwitch.trigger``/``retry_residual``; (b) ORCHESTRATOR —
the synthetic composition with a kill tripping mid-run with an open order in
flight. Compositions are REAL parts only (real ledgers into tmp dirs, the real
preflight registry, FakeClock); no network, no creds, stdlib only.
"""
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import agent.execution_preflight as execution_preflight
from agent.broker.base import OrderIntent, require_token
from agent.broker.fake import FakeBroker
from agent.broker.flatten_proxy import FlattenUnpriced, PriceCappedFlattenBroker
from agent.execution_config import FLATTEN_CAP_BPS
from agent.execution_preflight import (
    PreflightStale,
    bind_runtime,
    consume,
    mint_open_token,
    unbind_runtime,
)
from agent.order_pricing import on_tick_grid, reduce_cap
from agent.quote_quality import evaluate as evaluate_quote
from agent.risk.account_state import AccountStore, parse_account_payload
from agent.risk.risk_config import RiskConfig
from agent.risk.risk_kill import RiskKillSwitch
from agent.risk.risk_ledger import (
    EVT_KILL_FLATTEN_INCOMPLETE,
    EVT_KILL_RETRY,
    EVT_KILL_TRANSITION,
    RiskLedger,
    replay_risk,
)
from agent.strategies.synthetic import ScriptedSyntheticStrategy
from recorder.persistence import EventWriter

from tests.agent.test_execution_preflight_m5 import (
    golden_inputs,
    purge_open_authorizations,
)
from tests.lib.exec_fixtures import (
    ExecPipeline,
    HeldQuoteView,
    RealStrategyStub,
    quote,
)
from tests.lib.fakes import FakeClock, SpyBroker
from tests.lib.risk_fixtures import (
    FakeAccountProvider,
    account_payload,
    permissive_fixture_config,
    portfolio_fixture,
)

_CLOCK = lambda: "2026-06-10T20:00:00.000000+00:00"  # noqa: E731

_AAPL_ID = 1001
_MSFT_ID = 1002


def _cfg() -> RiskConfig:
    return RiskConfig.from_config(permissive_fixture_config())


def _switch(tmpdir, run_id="run-kill-drill"):
    path = Path(tmpdir) / "risk.jsonl"
    ledger = RiskLedger(EventWriter(path, run_id, clock=_CLOCK), rules_hash="rh")
    return RiskKillSwitch(cfg=_cfg(), ledger=ledger), path


def _msft_quote(**overrides):
    fields = dict(symbol="MSFT", instrument_id=_MSFT_ID, bid="309.98",
                  ask="310.02", vendor_seq=2)
    fields.update(overrides)
    return quote(**fields)


def _both_quotes_view() -> HeldQuoteView:
    qv = HeldQuoteView()
    qv.put(quote())            # AAPL 99.99 / 100.01 @ instrument 1001
    qv.put(_msft_quote())
    return qv


def _fake(qv, ids, *, fill_policy="immediate_full") -> FakeBroker:
    return FakeBroker(quote_view=qv, clock=FakeClock(), instrument_ids=ids,
                      fill_policy=fill_policy)


def _proxy(inner, qv, ids) -> PriceCappedFlattenBroker:
    return PriceCappedFlattenBroker(inner=inner, quote_view=qv,
                                    instrument_ids=ids,
                                    cap_bps=FLATTEN_CAP_BPS)


def _record_inner(fake: FakeBroker) -> list:
    """Record every intent reaching the inner broker's `_place` (i.e. AFTER the
    token chokepoint and the FD-M5-8 reverse wall accepted it)."""
    seen = []
    real_place = fake._place

    def spy(intent):
        seen.append(intent)
        return real_place(intent)

    fake._place = spy
    return seen


def _record_proxy(proxy: PriceCappedFlattenBroker) -> list:
    """Record every intent the M0 actuator hands the PROXY (pre-pricing)."""
    seen = []
    real_submit = proxy.submit_order

    def spy(intent, token):
        seen.append(intent)
        return real_submit(intent, token)

    proxy.submit_order = spy
    return seen


def _transitions(path) -> list:
    return [row for row in replay_risk(path)
            if row["event_type"] == EVT_KILL_TRANSITION]


def _no_open_authorizations(case: unittest.TestCase) -> None:
    for auth in execution_preflight._authorizations.values():  # white-box (M4 case-5 mirror)
        case.assertNotEqual(auth.kind, "open")


class _MidSubmitKillBroker:
    """Paper-shaped broker double: the configured hook fires AFTER the intent is
    received but BEFORE the token chokepoint — the 'kill trips while the order is
    on the wire' window the §2.3 consume-time re-checks defend (FD-M5-13)."""

    kind = "alpaca_paper"

    def __init__(self):
        self.on_submit = None
        self.submit_intents = []
        self.status_calls = []

    def submit_order(self, intent, token):
        self.submit_intents.append(intent)
        if self.on_submit is not None:
            hook, self.on_submit = self.on_submit, None
            hook()
        require_token(intent, token)
        raise AssertionError("require_token must raise after the mid-submit kill")

    def cancel_order(self, order_id):
        return {}

    def order_status(self, order_id):
        self.status_calls.append(order_id)
        raise RuntimeError("status unavailable (injected)")

    def positions(self):
        return []

    def account(self):
        return {}


class TestProxyDrillUnit(unittest.TestCase):
    """(a) Unit level: the proxy over a FakeBroker inner, driven by the REAL
    RiskKillSwitch.trigger / retry_residual."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="m5-kill-unit-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_drill_zero_failed_every_inner_intent_reducing_and_priced(self):
        qv = _both_quotes_view()
        ids = {"AAPL": _AAPL_ID, "MSFT": _MSFT_ID}
        fake = _fake(qv, ids)
        proxy = _proxy(fake, qv, ids)
        pre = _record_proxy(proxy)
        post = _record_inner(fake)
        switch, path = _switch(self.tmp)

        report = switch.trigger("drill", proxy, portfolio_fixture("long_short"))

        # ZERO failed[] over the FakeBroker inner — the M5C-1/M5C-S1 blocker pin.
        self.assertEqual(report.failed, ())
        self.assertEqual(report.flattened, ("AAPL", "MSFT"))
        self.assertEqual(report.residual, ())
        self.assertEqual(switch.state, "halted")
        self.assertEqual(switch.generation, 1)

        # The M0 actuator hands the proxy UN-prefixed, UNPRICED reduce intents
        # (kill_switch.py:34-40, unedited — FD-M4-4).
        self.assertEqual([i.intent_id for i in pre],
                         ["flatten-AAPL", "flatten-MSFT"])
        for intent in pre:
            self.assertIs(intent.is_reducing, True)
            self.assertIsNone(intent.limit_price)
            self.assertFalse(intent.intent_id.startswith("synthetic-"))

        # Every intent reaching the INNER broker: is_reducing=True AND a
        # tick-valid limit — no limit=None passes through (§H.2).
        self.assertEqual(len(post), 2)
        by_symbol = {i.symbol: i for i in post}
        for intent in post:
            self.assertIs(intent.is_reducing, True)
            self.assertIsNotNone(intent.limit_price)
            self.assertTrue(on_tick_grid(intent.limit_price))
        self.assertEqual(by_symbol["AAPL"].side, "sell")       # long -> sell
        self.assertEqual(by_symbol["AAPL"].qty, Decimal("10"))
        self.assertEqual(
            by_symbol["AAPL"].limit_price,
            reduce_cap(side="sell", quote=qv.latest("AAPL", _AAPL_ID),
                       cap_bps=FLATTEN_CAP_BPS))
        self.assertEqual(by_symbol["MSFT"].side, "buy")        # short -> cover
        self.assertEqual(by_symbol["MSFT"].qty, Decimal("5"))
        self.assertEqual(
            by_symbol["MSFT"].limit_price,
            reduce_cap(side="buy", quote=qv.latest("MSFT", _MSFT_ID),
                       cap_bps=FLATTEN_CAP_BPS))

        # The reverse wall passed the reducing intents and the fake genuinely
        # placed them (marketable caps fill at the touch).
        self.assertEqual(fake.order_status("flatten-AAPL")["status"], "filled")
        self.assertEqual(fake.order_status("flatten-MSFT")["status"], "filled")

        rows = _transitions(path)
        self.assertEqual([(r["from_state"], r["to_state"]) for r in rows],
                         [("monitoring", "flattening"),
                          ("flattening", "halted")])
        self.assertEqual(rows[1]["flattened"], ["AAPL", "MSFT"])
        self.assertEqual(rows[1]["failed"], [])
        self.assertEqual(rows[1]["residual"], [])
        _no_open_authorizations(self)              # S1 holds through the drill

    def test_stale_quote_still_prices_a_reduce(self):
        # A quote ANCIENT by the open path's standard (strict '>' staleness vs
        # the committed 2000 ms budget) must still price a flatten (FD-M4-3).
        now_ms = 1_000_000
        stale = quote(seen_at_ms=0)
        verdict = evaluate_quote(stale, now_ms=now_ms,
                                 spread_bps_max=Decimal("50"),
                                 staleness_ms_max=2000)
        self.assertIsNot(verdict.ok, True)         # provably stale for an OPEN
        qv = HeldQuoteView()
        qv.put(stale)
        ids = {"AAPL": _AAPL_ID}
        fake = _fake(qv, ids)
        proxy = _proxy(fake, qv, ids)
        post = _record_inner(fake)
        switch, _ = _switch(self.tmp)

        report = switch.trigger("drill", proxy, portfolio_fixture("long_only"))

        self.assertEqual(report.flattened, ("AAPL",))
        self.assertEqual(report.failed, ())
        self.assertEqual(
            post[0].limit_price,
            reduce_cap(side="sell", quote=stale, cap_bps=FLATTEN_CAP_BPS))

    def test_missing_quote_no_price_for_cap_then_retry_once_quotable(self):
        qv = HeldQuoteView()
        qv.put(quote())                            # AAPL only; MSFT unquotable
        ids = {"AAPL": _AAPL_ID, "MSFT": _MSFT_ID}
        fake = _fake(qv, ids)
        proxy = _proxy(fake, qv, ids)
        switch, path = _switch(self.tmp)

        # The proxy raise is typed and its message IS the journaled reason (§H.2).
        with self.assertRaises(FlattenUnpriced) as ctx:
            proxy.submit_order(
                OrderIntent(symbol="MSFT", side="buy", qty=Decimal("5"),
                            is_reducing=True, intent_id="flatten-MSFT"),
                token=None)
        self.assertEqual(str(ctx.exception), "no_price_for_cap")

        report = switch.trigger("drill", proxy, portfolio_fixture("long_short"))
        self.assertEqual(report.flattened, ("AAPL",))
        self.assertEqual(report.failed, (("MSFT", "no_price_for_cap"),))
        self.assertEqual(report.residual, ("MSFT",))
        self.assertEqual(switch.state, "halted")

        rows = _transitions(path)
        self.assertEqual(rows[1]["failed"], [["MSFT", "no_price_for_cap"]])
        incomplete = [row for row in replay_risk(path)
                      if row["event_type"] == EVT_KILL_FLATTEN_INCOMPLETE]
        self.assertEqual(len(incomplete), 1)
        self.assertEqual(incomplete[0]["residual"], ["MSFT"])

        # retry_residual succeeds once quotable; HALTED latch unaffected.
        qv.put(_msft_quote())
        retry = switch.retry_residual(proxy, portfolio_fixture("long_short"))
        self.assertEqual(retry.flattened, ("MSFT",))
        self.assertEqual(retry.failed, ())
        self.assertEqual(retry.residual, ())
        self.assertEqual(switch.state, "halted")
        retries = [row for row in replay_risk(path)
                   if row["event_type"] == EVT_KILL_RETRY]
        self.assertEqual(retries[0]["residual_before"], ["MSFT"])
        self.assertEqual(retries[0]["residual_after"], [])

    def test_unmapped_symbol_resolves_identically(self):
        # M5C-S8: a symbol absent from the proxy's instrument_ids resolves like
        # an unpriceable quote — never a gate; retried with the full map.
        qv = _both_quotes_view()                   # the QUOTE exists for MSFT
        full_ids = {"AAPL": _AAPL_ID, "MSFT": _MSFT_ID}
        fake = _fake(qv, full_ids)
        unmapped_proxy = _proxy(fake, qv, {"AAPL": _AAPL_ID})
        switch, path = _switch(self.tmp)

        report = switch.trigger("drill", unmapped_proxy,
                                portfolio_fixture("long_short"))
        self.assertEqual(report.flattened, ("AAPL",))
        self.assertEqual(report.failed, (("MSFT", "no_price_for_cap"),))
        self.assertEqual(report.residual, ("MSFT",))
        self.assertEqual(_transitions(path)[1]["failed"],
                         [["MSFT", "no_price_for_cap"]])

        # The orchestrator-built universe ∪ held map resolves it (M5C-S8).
        retry = switch.retry_residual(_proxy(fake, qv, full_ids),
                                      portfolio_fixture("long_short"))
        self.assertEqual(retry.flattened, ("MSFT",))
        self.assertEqual(retry.residual, ())
        self.assertEqual(switch.state, "halted")

    def test_unusable_touch_resolves_identically(self):
        # reduce_cap returns None when the NEEDED side is missing (a short cover
        # prices off the ask) — the third no_price_for_cap path in §H.2.
        qv = HeldQuoteView()
        qv.put(quote())
        qv.put(_msft_quote(ask=None))
        ids = {"AAPL": _AAPL_ID, "MSFT": _MSFT_ID}
        fake = _fake(qv, ids)
        proxy = _proxy(fake, qv, ids)
        switch, _ = _switch(self.tmp)

        report = switch.trigger("drill", proxy, portfolio_fixture("long_short"))
        self.assertEqual(report.flattened, ("AAPL",))
        self.assertEqual(report.failed, (("MSFT", "no_price_for_cap"),))
        self.assertEqual(report.residual, ("MSFT",))

        qv.put(_msft_quote())
        retry = switch.retry_residual(proxy, portfolio_fixture("long_short"))
        self.assertEqual(retry.residual, ())

    def test_unpriceable_flatten_voids_its_reduce_token_no_leak(self):
        # SF-2: an unpriceable flatten (unmapped / no quote / no cap) MUST void
        # the already-minted reduce-only authorization before raising
        # FlattenUnpriced — otherwise the token leaks across retry_residual
        # passes. The proxy may not import execution_preflight (§3 import row),
        # so the void path is dependency-injected; the orchestrator passes
        # execution_preflight.void_token (§M.6). "no_price_for_cap" is a legal
        # void reason (EXTRA_REJECT_REASONS).
        self.addCleanup(purge_open_authorizations)
        purge_open_authorizations()
        before = dict(execution_preflight._authorizations)

        class _Held:
            qty = Decimal("5")
            symbol = "MSFT"

        # If the fix is absent the reduce token leaks; drop any authorization
        # this test introduced on teardown so a failing run never poisons
        # sibling suites' registry assertions.
        def _drop_leaked(baseline=before):
            for nonce in list(execution_preflight._authorizations):
                if nonce not in baseline:
                    del execution_preflight._authorizations[nonce]
        self.addCleanup(_drop_leaked)

        # Unmapped symbol => no_price_for_cap on the FIRST proxy check.
        fake = _fake(HeldQuoteView(), {"AAPL": _AAPL_ID})
        proxy = PriceCappedFlattenBroker(
            inner=fake, quote_view=HeldQuoteView(),
            instrument_ids={"AAPL": _AAPL_ID}, cap_bps=FLATTEN_CAP_BPS,
            void_token=execution_preflight.void_token)
        intent = OrderIntent(symbol="MSFT", side="sell", qty=Decimal("5"),
                             is_reducing=True, intent_id="flatten-MSFT")
        token = execution_preflight.mint_reduce_only_token(_Held(), intent)
        # the mint registered exactly one reduce_only authorization.
        self.assertEqual(
            [a.kind for a in execution_preflight._authorizations.values()
             if a not in before.values()],
            ["reduce_only"])

        with self.assertRaises(FlattenUnpriced) as ctx:
            proxy.submit_order(intent, token)
        self.assertEqual(str(ctx.exception), "no_price_for_cap")

        # SF-2: the reduce authorization is GONE — no registry leak.
        self.assertEqual(dict(execution_preflight._authorizations), before)
        self.assertIsNone(execution_preflight.authorization_of(token))

    def test_unpriceable_flatten_without_void_token_is_a_noop_default(self):
        # The injected callable defaults to None (no-op): existing constructions
        # and the M0/M4 unit drills (which mint NO token before the proxy) keep
        # working — SF-2's fix never changes the default surface.
        fake = _fake(HeldQuoteView(), {"AAPL": _AAPL_ID})
        proxy = PriceCappedFlattenBroker(
            inner=fake, quote_view=HeldQuoteView(),
            instrument_ids={"AAPL": _AAPL_ID}, cap_bps=FLATTEN_CAP_BPS)
        intent = OrderIntent(symbol="MSFT", side="buy", qty=Decimal("5"),
                             is_reducing=True, intent_id="flatten-MSFT")
        with self.assertRaises(FlattenUnpriced) as ctx:
            proxy.submit_order(intent, token=None)
        self.assertEqual(str(ctx.exception), "no_price_for_cap")

    def test_stale_inputs_reflects_account_store_get_freshness(self):
        # M5C-B5: trigger's `account` comes straight from AccountStore.get — the
        # journaled stale_inputs is the REAL freshness verdict (strict-'>' TTL
        # boundary: age 5000 == fresh, 5001 == stale). Staleness never blocks.
        store = AccountStore(clock=FakeClock(start_ms=0))
        store.put(parse_account_payload(
            account_payload(), source="fixture", seen_at_ms=0,
            ts_read_utc="2026-06-10T14:00:00Z"))

        for label, now_ms, expected in (("fresh", 5000, False),
                                        ("stale", 5001, True)):
            with self.subTest(label):
                read = store.get(now_ms=now_ms)
                self.assertEqual(read.status, label)
                qv = HeldQuoteView()
                qv.put(quote())
                ids = {"AAPL": _AAPL_ID}
                proxy = _proxy(_fake(qv, ids), qv, ids)
                subdir = self.tmp / label
                subdir.mkdir(parents=True, exist_ok=True)
                switch, path = _switch(subdir)
                report = switch.trigger("drill", proxy,
                                        portfolio_fixture("long_only"),
                                        account=read)
                self.assertEqual(report.flattened, ("AAPL",))  # never blocked
                rows = _transitions(path)
                self.assertEqual(len(rows), 2)
                for row in rows:
                    self.assertIs(row["stale_inputs"], expected)

    def test_generation_bump_kills_outstanding_open_token_at_consume(self):
        # FD-M5-13: an open token minted at generation N is dead at consume once
        # the drill bumps the generation — revoked, PreflightStale, registry clean.
        unbind_runtime()                           # defensive (idempotent)
        self.addCleanup(unbind_runtime)
        self.addCleanup(purge_open_authorizations)
        switch, _ = _switch(self.tmp)
        clock = FakeClock(start_ms=10_250)         # == golden_inputs now_ms
        bind_runtime(clock=clock,
                     kill_generation_source=lambda: switch.generation)
        token, pf = mint_open_token(golden_inputs())
        self.assertIsNotNone(pf.capped_limit)

        qv = HeldQuoteView()
        qv.put(quote())
        ids = {"AAPL": _AAPL_ID}
        report = switch.trigger("drill", _proxy(_fake(qv, ids), qv, ids),
                                portfolio_fixture("long_only"))
        self.assertEqual(report.generation, 1)
        self.assertEqual(report.failed, ())

        with self.assertRaises(PreflightStale) as ctx:
            consume(token)
        self.assertEqual(ctx.exception.reason, "kill_generation_changed")
        _no_open_authorizations(self)              # revoked, not just rejected


class TestKillSequenceOrchestrator(unittest.TestCase):
    """(b) Orchestrator level: the §M.6 sequence over real compositions."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="m5-kill-orch-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(unbind_runtime)            # teardown hygiene
        self.addCleanup(purge_open_authorizations)

    def make_pipeline(self, subdir, **kwargs):
        pipeline = ExecPipeline(journal_dir=self.tmp / subdir, **kwargs)
        self.addCleanup(pipeline.close)
        return pipeline

    @staticmethod
    def _record_broker_calls(broker) -> list:
        """Instance-level spies (type identity preserved — wall 1 already ran):
        every cancel_order / submit_order in arrival order."""
        events = []
        real_submit = broker.submit_order
        real_cancel = broker.cancel_order

        def submit_spy(intent, token):
            events.append(("submit", intent))
            return real_submit(intent, token)

        def cancel_spy(order_id):
            events.append(("cancel", order_id))
            return real_cancel(order_id)

        broker.submit_order = submit_spy
        broker.cancel_order = cancel_spy
        return events

    def _kill_transitions(self, pipeline) -> list:
        return [row for row in pipeline.rows("risk")
                if row["event_type"] == EVT_KILL_TRANSITION]

    def test_synthetic_composition_drill(self):
        """The M5C-1/M5C-S1 blocker pin, end to end: a kill trips mid-run with a
        partially-filled open order in flight, through the REAL §M.6 sequence
        over the step-9-constructed FakeBroker."""
        strategy = ScriptedSyntheticStrategy([
            {"on_bar": 1, "action": "open", "symbol": "AAPL", "qty": "10",
             "limit": "210.00"},
            {"on_bar": 2, "action": "open", "symbol": "AAPL", "qty": "10",
             "limit": "210.00"},                   # the post-kill open attempt
        ])
        provider = FakeAccountProvider(positions_payloads=[[
            {"symbol": "AAPL", "qty": "3", "market_value": "600.00",
             "instrument_id": _AAPL_ID}]])
        pipeline = self.make_pipeline(
            "drill", strategy=strategy, account_provider=provider,
            fill_policy="partial_then_full")
        self.assertEqual(pipeline.orch.mode, "synthetic")
        self.assertIs(type(pipeline.orch.broker), FakeBroker)
        events = self._record_broker_calls(pipeline.orch.broker)

        pipeline.tick_on_bar(50)                   # DECIDED (t0)
        pipeline.tick_quote_only(50)               # REQUOTE -> SUBMIT (rests)
        self.assertTrue(pipeline.orch.in_flight)
        kill_quote = pipeline.tick_quote_only(50, advance_ms=500, shift_ms=900)
        # poll 1: the scripted 30% partial fill — held position + order in flight.
        opens = pipeline.rows_of("positions", "position_open")
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0]["qty"], "3")
        self.assertTrue(pipeline.orch.in_flight)
        open_order_id = pipeline.rows_of(
            "orders", "order_submit_attempt")[0]["order_id"]

        pipeline.orch.trigger_kill("drill")        # the §M.6 sequence

        # Cancel-opens-FIRST ordering: the kill_trip cancel of the in-flight
        # open precedes the flatten submit at the broker boundary.
        self.assertEqual(
            [(kind, getattr(value, "intent_id", value))
             for kind, value in events],
            [("submit", open_order_id),            # the original open (pre-kill)
             ("cancel", open_order_id),            # §M.6 step 1
             ("submit", "flatten-AAPL")])          # §M.6 step 3
        cancels = pipeline.rows_of("orders", "post_submit_cancel_attempt")
        self.assertEqual(
            [(r["cause"], r["outcome"], r["broker_state_at_attempt"])
             for r in cancels],
            [("kill_trip", "cancel_submitted", "partially_filled")])

        # The flatten intent that reached the FakeBroker: un-prefixed, reducing,
        # tick-valid price from the kill-time quote (FLATTEN_CAP_BPS).
        flatten_intent = events[2][1]
        self.assertIs(flatten_intent.is_reducing, True)
        self.assertEqual(flatten_intent.qty, Decimal("3"))
        self.assertIsNotNone(flatten_intent.limit_price)
        self.assertTrue(on_tick_grid(flatten_intent.limit_price))
        self.assertEqual(
            flatten_intent.limit_price,
            reduce_cap(side="sell", quote=kill_quote,
                       cap_bps=FLATTEN_CAP_BPS))
        self.assertFalse(flatten_intent.intent_id.startswith("synthetic-"))

        # A submit ack is not a completed flatten: the synthetic broker's
        # partial_then_full policy initially returns `new`, so residual remains
        # until deterministic status polls prove the terminal full fill.
        self.assertEqual(pipeline.orch.risk_kill.state, "halted")
        self.assertEqual(pipeline.orch.risk_kill.generation, 1)
        rows = self._kill_transitions(pipeline)
        self.assertEqual([(r["from_state"], r["to_state"]) for r in rows],
                         [("monitoring", "flattening"),
                          ("flattening", "halted")])
        self.assertEqual(rows[0]["cause"], "drill")
        self.assertEqual(rows[0]["residual"], ["AAPL"])
        self.assertEqual(rows[1]["flattened"], [])
        self.assertEqual(rows[1]["failed"],
                         [["AAPL", "flatten_pending"]])
        self.assertEqual(rows[1]["residual"], ["AAPL"])
        for row in rows:
            self.assertIs(row["stale_inputs"], False)

        # Qty 3 takes FakeBroker's documented tiny-order branch, whose first
        # deterministic poll applies the single full remainder.
        terminal = pipeline.orch.retry_residual()
        self.assertEqual(terminal.flattened, ("AAPL",))
        self.assertEqual(terminal.residual, ())
        # Retry polls `flatten-AAPL`; it cannot duplicate its submit.
        self.assertEqual(
            [(kind, getattr(value, "intent_id", value))
             for kind, value in events if kind == "submit"],
            [("submit", open_order_id), ("submit", "flatten-AAPL")])

        # close_position(reason="kill_flatten") booked the flatten fill.
        closes = pipeline.rows_of("positions", "position_close")
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["reason"], "kill_flatten")
        self.assertEqual(closes[0]["exit_qty"], "3")
        position = pipeline.orch.book.position(opens[0]["position_id"])
        self.assertEqual(position.status, "closed")
        fills = pipeline.rows_of("fills", "broker_fill")
        self.assertEqual([r["delta_qty"] for r in fills], ["3", "3"])

        # The canceled opening order fills late post-kill (its partial fill is
        # kept by the cancel) => the M5C-S11 tripwire on the terminal poll.
        pipeline.tick_quote_only(50, advance_ms=500, shift_ms=1400)
        terminals = pipeline.rows_of("orders", "order_terminal")
        self.assertEqual(
            [(r["terminal_state"], r["filled_qty"]) for r in terminals],
            [("canceled", "3")])
        alerts = pipeline.rows_of("orders", "order_state_alert")
        self.assertEqual([r["note"] for r in alerts], ["late_fill_post_kill"])
        self.assertFalse(pipeline.orch.in_flight)

        # No opens possible after: the next scripted open refuses at the kill
        # rung; the broker sees no further submits of any kind.
        pipeline.tick_on_bar(51)
        verdicts = pipeline.rows_of("risk", "risk_verdict")
        self.assertEqual(
            [(r["allowed"], r["gate_stage"], r["reasons"]) for r in verdicts],
            [(True, None, []),
             (False, "kill", ["kill_switch_halted"])])
        self.assertEqual(len([e for e in events if e[0] == "submit"]), 2)
        self.assertEqual(
            len(pipeline.rows_of("orders", "order_submit_attempt")), 1)
        _no_open_authorizations(self)              # zero open-kind auths after

    def test_trigger_with_stale_account_read_journals_stale_inputs_true(self):
        # M5C-B5 through §M.6: no wrap() helper — AccountStore.get at kill time
        # carries its REAL (stale) freshness into the journaled row, and the
        # flatten proceeds anyway, priced off a quote that is itself stale.
        provider = FakeAccountProvider(positions_payloads=[[
            {"symbol": "AAPL", "qty": "3", "market_value": "600.00",
             "instrument_id": _AAPL_ID}]])
        pipeline = self.make_pipeline(
            "stale", strategy=ScriptedSyntheticStrategy([]),
            account_provider=provider)
        events = self._record_broker_calls(pipeline.orch.broker)
        last_quote = pipeline.tick_on_bar(50)      # account put at this tick
        pipeline.clock.advance(5001)               # past ACCOUNT_FRESHNESS_TTL_MS
        verdict = evaluate_quote(last_quote, now_ms=pipeline.clock.now_ms(),
                                 spread_bps_max=Decimal("50"),
                                 staleness_ms_max=2000)
        self.assertIsNot(verdict.ok, True)         # the quote is stale too

        pipeline.orch.trigger_kill("drill")

        rows = self._kill_transitions(pipeline)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIs(row["stale_inputs"], True)
        self.assertEqual(rows[1]["flattened"], ["AAPL"])
        self.assertEqual(rows[1]["failed"], [])
        flatten_intent = events[-1][1]
        self.assertEqual(
            (events[-1][0], flatten_intent.intent_id), ("submit", "flatten-AAPL"))
        self.assertIs(flatten_intent.is_reducing, True)
        self.assertEqual(
            flatten_intent.limit_price,
            reduce_cap(side="sell", quote=last_quote,
                       cap_bps=FLATTEN_CAP_BPS))   # stale quote still prices

    def test_mid_submit_generation_bump_rejects_at_consume(self):
        # FD-M5-13 at the orchestrator: the generation bumps while the order is
        # on the wire => consume revokes, PreflightStale, journaled
        # reject{stage:"consume", kill_generation_changed}; never submitted.
        broker = _MidSubmitKillBroker()
        pipeline = self.make_pipeline(
            "bump", broker=broker,
            strategy=RealStrategyStub([{"on_bar": 1, "qty": "10"}]),
            run_gates="valid", artifacts="valid",
            account_provider=FakeAccountProvider())
        broker.on_submit = lambda: pipeline.orch.risk_kill.trigger(
            "drill", SpyBroker(), portfolio_fixture("flat"))

        pipeline.tick_on_bar(50)
        pipeline.tick_quote_only(50)

        attempts = pipeline.rows_of("orders", "order_submit_attempt")
        self.assertEqual(len(attempts), 1)         # write-ahead happened (FD-M5-17)
        rejects = pipeline.rows_of("orders", "reject")
        self.assertEqual(
            [(r["stage"], r["reasons"], r["token_kind"]) for r in rejects],
            [("consume", ["kill_generation_changed"], "open")])
        self.assertLess(attempts[0]["seq"], rejects[0]["seq"])
        self.assertEqual(pipeline.rows_of("orders", "order_submitted"), [])
        self.assertEqual(pipeline.rows_of("orders",
                                          "order_submit_unconfirmed"), [])
        self.assertEqual(broker.status_calls, [])  # a stale token needs no recovery
        self.assertFalse(pipeline.orch.in_flight)
        self.assertEqual(pipeline.orch.risk_kill.state, "halted")
        self.assertEqual(pipeline.orch.risk_kill.generation, 1)
        _no_open_authorizations(self)              # consume REVOKED it

    def test_kill_sequence_voids_outstanding_open_token(self):
        # §M.6 step 2: the FULL kill sequence fires while the token is minted-
        # unconsumed (mid-submit) => void_token + the journaled consume reject;
        # the spent token then resolves as forgery -> FD-M5-17 recovery.
        broker = _MidSubmitKillBroker()
        pipeline = self.make_pipeline(
            "void", broker=broker,
            strategy=RealStrategyStub([{"on_bar": 1, "qty": "10"}]),
            run_gates="valid", artifacts="valid",
            account_provider=FakeAccountProvider())
        broker.on_submit = lambda: pipeline.orch.trigger_kill("drill")

        pipeline.tick_on_bar(50)
        pipeline.tick_quote_only(50)

        rejects = pipeline.rows_of("orders", "reject")
        self.assertEqual(
            [(r["stage"], r["reasons"], r["token_kind"]) for r in rejects],
            [("consume", ["kill_generation_changed"], "open")])
        # The order was never confirmed live: recovery queried x3, denied the
        # symbol; no cancel rows (the task never reached WATCH).
        unconfirmed = pipeline.rows_of("orders", "order_submit_unconfirmed")
        self.assertEqual([(r["resolution"], r["attempts"]) for r in unconfirmed],
                         [("not_found", 3)])
        self.assertEqual(len(broker.status_calls), 3)
        self.assertEqual(pipeline.rows_of("orders",
                                          "post_submit_cancel_attempt"), [])
        self.assertIn("AAPL", pipeline.orch.open_deny)
        self.assertEqual(pipeline.rows_of("orders", "order_submitted"), [])
        self.assertEqual(len(broker.submit_intents), 1)
        self.assertEqual(pipeline.orch.risk_kill.state, "halted")
        self.assertEqual(pipeline.orch.risk_kill.generation, 1)
        rows = self._kill_transitions(pipeline)
        self.assertEqual([(r["from_state"], r["to_state"]) for r in rows],
                         [("monitoring", "flattening"),
                          ("flattening", "halted")])
        _no_open_authorizations(self)              # voided at §M.6 step 2

    def test_kill_with_strategy_close_in_flight_reconciles_single_flatten(self):
        """LC-2: a RiskKillSwitch trip while a strategy CLOSE (reduce sell) order
        is non-terminal. The §M.6 sequence MUST reconcile to a SINGLE flatten of
        record (never two concurrent broker sells on one position), the loop MUST
        survive (the superseded close's terminal poll cannot crash on an
        already-closed position), and the held position MUST end flat.

        Driven via the prompt's repro: open a position, install a close
        _OrderTask in 'watch' for it (a resting non-marketable reduce sell at the
        broker), trigger_kill — pre-fix this closed the position AND left the
        close task alive, so its next terminal poll raised
        `ExecError: position ... is closed (terminal)` and crashed on_tick."""
        from agent.execution_preflight import (
            DecisionStamp,
            mint_reduce_only_token,
        )
        from agent.orchestrator import _OrderTask
        from agent.broker.order_state import fill_delta, parse_order_payload

        strategy = ScriptedSyntheticStrategy([
            {"on_bar": 1, "action": "open", "symbol": "AAPL", "qty": "10",
             "limit": "210.00"}])
        provider = FakeAccountProvider(positions_payloads=[[
            {"symbol": "AAPL", "qty": "10", "market_value": "2000.00",
             "instrument_id": _AAPL_ID}]])
        pipeline = self.make_pipeline(
            "close-inflight", strategy=strategy, account_provider=provider,
            fill_policy="immediate_full")
        orch = pipeline.orch
        events = self._record_broker_calls(orch.broker)

        # open 10 AAPL (immediate_full fills the buy at the ask on the ack).
        pipeline.tick_on_bar(50)
        pipeline.tick_quote_only(50)
        opens = pipeline.rows_of("positions", "position_open")
        self.assertEqual(len(opens), 1)
        pos_id = opens[0]["position_id"]
        position = orch.book.position(pos_id)
        self.assertEqual(position.status, "open")
        self.assertEqual(position.qty, Decimal("10"))

        # install a resting strategy CLOSE in 'watch' for that position: a sell
        # with a limit ABOVE the bid is non-marketable at the FakeBroker, so it
        # rests 'accepted' (a genuine non-terminal reduce on the wire).
        quote_b = orch._quote_view.latest("AAPL", _AAPL_ID)
        rest_limit = (quote_b.bid + Decimal("5.0000"))
        close_order_id = "synthetic-o-closeinflight"
        intent = OrderIntent(
            symbol="AAPL", side="sell", qty=Decimal("10"),
            order_type="marketable_limit", tif="day", limit_price=rest_limit,
            is_reducing=True, intent_id=close_order_id)
        token = mint_reduce_only_token(position, intent)
        ack = orch.broker.submit_order(intent, token)
        self.assertEqual(parse_order_payload(ack, source="fake").state,
                         "accepted")               # rests, non-terminal
        stamp = DecisionStamp(decision_id="d-closeinflight",
                              decision_ts_utc=quote_b.ts_recv_utc,
                              decision_seen_at_ms=orch._clock.now_ms(),
                              quote_a=quote_b)
        close_task = _OrderTask(
            kind="close", state="watch", decision_id="d-closeinflight",
            symbol="AAPL", instrument_id=_AAPL_ID,
            strategy_id=strategy.strategy_id, side="sell", qty=Decimal("10"),
            stamp=stamp, session_date_et="2026-06-15", order_id=close_order_id,
            position_id=pos_id, close_reason="strategy_exit",
            capped_limit=rest_limit, quote_b=quote_b,
            bound_epoch=quote_b.reconnect_epoch,
            last_poll_ms=orch._clock.now_ms())
        orch._task = close_task
        submit_count_before = len([e for e in events if e[0] == "submit"])

        # THE KILL — with the strategy close non-terminal in flight.
        orch.trigger_kill("drill")

        # (1) loop survives + position ends flat after the kill.
        self.assertEqual(orch.risk_kill.state, "halted")
        self.assertEqual(orch.book.position(pos_id).status, "closed")

        # (2) the superseded close task is RETIRED (cleared) — not left alive to
        # crash the next terminal poll.
        self.assertFalse(orch.in_flight)

        # (3) the in-flight close was cancelled (cause=kill_trip) BEFORE any new
        # sell — single flatten of record, no concurrent double-sell.
        cancels = pipeline.rows_of("orders", "post_submit_cancel_attempt")
        self.assertIn(
            ("kill_trip", close_order_id),
            [(c["cause"], c["order_id"]) for c in cancels])
        # the cancelled reduce is terminal at the broker (no more fills).
        self.assertEqual(orch.broker.order_status(close_order_id)["status"],
                         "canceled")
        # exactly ONE new broker sell after the kill: the flatten of record.
        new_submits = [e for e in events[submit_count_before:]
                       if e[0] == "submit"]
        self.assertEqual([getattr(v, "intent_id", v) for _, v in new_submits],
                         ["flatten-AAPL"])

        # (4) the kill flatten booked exactly ONE position_close of record.
        closes = pipeline.rows_of("positions", "position_close")
        self.assertEqual([(c["reason"], c["position_id"]) for c in closes],
                         [("kill_flatten", pos_id)])

        # (5) the loop genuinely survives a SUBSEQUENT tick (the superseded close
        # would otherwise be terminal-polled here and crash on the closed pos).
        pipeline.tick_quote_only(50, advance_ms=500, shift_ms=1400)
        self.assertFalse(orch.in_flight)
        self.assertEqual(orch.book.position(pos_id).status, "closed")


if __name__ == "__main__":
    unittest.main()
