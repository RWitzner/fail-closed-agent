"""M5 §R test 3 — `evaluate_preflight` + the rebuilt `mint_open_token` (S1, S4).

One test per phase-1/phase-2 PREFLIGHT_REASONS member (37; the 2 consume-time
members are owned by test_preflight_token, the 3 RESERVED members get the single
require_reason negative test — M5C-T8), built as a one-bad-input matrix over the
golden-good `PreflightInputs` builder below. The risk-binding tests use the REAL
M4 `RiskEngine.can_open` verdict + a REAL `RiskLedger`-journaled row (FD-M5-30).

This module also EXPORTS `golden_inputs`/`golden_candidate` for the FD-M5-14
legacy-call-site rewrites (test_preflight_token / test_config_canary /
test_alpaca_spy).
"""
import atexit
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from agent import execution_preflight, quote_quality
from agent.candidate import Candidate, Leg
from agent.config import load
from agent.exec_reasons import (
    ExecError,
    PREFLIGHT_STAGES,
    RESERVED_PREFLIGHT_REASONS,
    require_reason,
)
from agent.execution_config import ExecutionConfig
from agent.execution_preflight import (
    DecisionStamp,
    PreflightInputs,
    PreflightPass,
    PreflightReject,
    PreflightRejected,
    evaluate_preflight,
    mint_open_token,
)
from agent.market_state import (
    HaltState,
    LuldState,
    SessionState,
    SsrState,
    Tradability,
    Verdict,
)
from agent.market_state_cache import MarketStateCache
from agent.quote_quality import QuoteSnapshot
from tests.lib.risk_fixtures import (
    account_payload,
    gates_on_fixture_config,
    permissive_fixture_config,
    portfolio_fixture,
    verdict_fixture,
)

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / "config"

_T0 = 10_000     # decision_seen_at_ms (t0)
_NOW = 10_250    # preflight now_ms == quote_b.seen_at_ms (latency == budget exactly)

_SKIP_AFTER_RUN_GATES = PREFLIGHT_STAGES[1:]
_SKIP_AFTER_KILL = PREFLIGHT_STAGES[2:]
_SKIP_AFTER_STAMP = PREFLIGHT_STAGES[3:]

_LEDGER_CLOCK = lambda: "2026-06-08T20:00:00.000000+00:00"  # noqa: E731
_RISK_FIXTURE = {}


def _dec(value):
    """Decimal coercion with RAW passthrough (so the float-injection test can plant
    a float in a money slot — S2)."""
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        return Decimal(value)
    return value


def _quote(*, symbol="AAPL", instrument_id=1001, bid="189.90", ask="189.94",
           bid_sz="5", ask_sz="5", ts_event_utc="2026-06-08T14:30:00.000000Z",
           ts_recv_utc="2026-06-08T14:30:00.010000Z", seen_at_ms=_T0,
           reconnect_epoch=0, vendor_seq=1, dataset="EQUS.MINI", schema="tbbo"):
    return QuoteSnapshot(
        symbol=symbol, instrument_id=instrument_id, bid=_dec(bid), ask=_dec(ask),
        bid_sz=_dec(bid_sz), ask_sz=_dec(ask_sz), ts_event_utc=ts_event_utc,
        ts_recv_utc=ts_recv_utc, seen_at_ms=seen_at_ms,
        reconnect_epoch=reconnect_epoch, vendor_seq=vendor_seq,
        dataset=dataset, schema=schema)


def quote_a():
    return _quote()


def quote_b(**overrides):
    base = dict(bid="189.92", ask="189.96",
                ts_event_utc="2026-06-08T14:30:00.250000Z",
                ts_recv_utc="2026-06-08T14:30:00.260000Z",
                seen_at_ms=_NOW, vendor_seq=2)
    base.update(overrides)
    return _quote(**base)


def golden_candidate(*, symbol="AAPL", instrument_id=1001, side="buy", qty="10",
                     limit="190.00"):
    return Candidate(
        strategy_id="s1",
        legs=(Leg(symbol=symbol, instrument_id=instrument_id, side=side,
                  qty=Decimal(qty),
                  limit_price=None if limit is None else Decimal(limit)),),
        paper_eligible=True, score=None)


def _exec_assembly() -> dict:
    """Gates-ON fixture assembly with the §B execution/signal/latency keys so
    ExecutionConfig parses (FD-M5-3: in-memory permissive fixture, never committed)."""
    config = permissive_fixture_config()
    config["agent_rules"].update({
        "latency_budget_ms": 250,
        "signal": {"quote_staleness_ms_max": "2000", "spread_bps_max": "50"},
        "execution": {"slippage_cap_bps": 25, "order_poll_interval_ms": 500,
                      "account_refresh_interval_ms": 2500, "max_open_orders": 1},
    })
    return config


def committed_assembly() -> dict:
    """The REAL committed config files (the S1 canary input)."""
    return {"agent_rules": load(_CONFIG / "agent_rules.json"),
            "risk_rules": load(_CONFIG / "risk_rules.json")}


def _build_risk_fixture() -> dict:
    """REAL M4 verdicts + REAL RiskLedger-journaled rows (FD-M5-30 binding tests)."""
    from agent.risk.account_state import AccountStore, parse_account_payload
    from agent.risk.can_open import RiskEngine
    from agent.risk.intraday_margin import FreezeState, MarginRead
    from agent.risk.loss_limits import LossRead
    from agent.risk.pdt_compat import PdtRead
    from agent.risk.risk_config import RiskConfig
    from agent.risk.risk_ledger import RiskLedger
    from agent.serializer import BrokerUSD
    from recorder.persistence import EventWriter
    from tests.lib.fakes import FakeClock

    tmpdir = TemporaryDirectory()
    atexit.register(tmpdir.cleanup)
    ledger = RiskLedger(
        EventWriter(Path(tmpdir.name) / "risk.jsonl", "run-m5", clock=_LEDGER_CLOCK),
        rules_hash="rh-m5")

    def fresh_account():
        store = AccountStore(clock=FakeClock(start_ms=0))
        store.put(parse_account_payload(
            account_payload(), source="fixture", seen_at_ms=0,
            ts_read_utc="2026-06-08T14:00:00Z"))
        return store.get()

    def can_open_kwargs(symbol, decision_id):
        return dict(
            market_state={symbol: verdict_fixture(symbol,
                                                  session_date_et="2026-06-08")},
            marks={}, kill_state="monitoring", kill_generation=0,
            margin_read=MarginRead(outstanding_nonminor=(),
                                   freeze=FreezeState(False, None, None, None),
                                   asof_session_date_et="2026-06-08"),
            pdt_read=PdtRead(state="unknown", evidence=None,
                             rejection_latched=False),
            loss_read=LossRead(hwm_equity=BrokerUSD("100000.00"),
                               daily_loss_usd=None, drawdown_usd=None, breaches=()),
            now_ms=10_000, decision_id=decision_id)

    def make(config, candidate, decision_id):
        engine = RiskEngine(cfg=RiskConfig.from_config(config),
                            gates_config=config, run_id="run-m5")
        verdict = engine.can_open(
            candidate, portfolio_fixture("flat"), fresh_account(),
            **can_open_kwargs(candidate.legs[0].symbol, decision_id))
        row = ledger.record_risk_verdict(verdict, decision_id=decision_id)
        return verdict, row

    permissive = permissive_fixture_config()
    allowed, allowed_row = make(permissive, golden_candidate(), "d-1")
    if allowed.allowed is not True:
        raise AssertionError(f"fixture verdict must be allowed: {allowed.reasons}")
    other_decision, other_decision_row = make(permissive, golden_candidate(), "d-2")
    msft, msft_row = make(permissive, golden_candidate(symbol="MSFT"), "d-1")
    denied, denied_row = make(gates_on_fixture_config(), golden_candidate(), "d-1")
    if denied.allowed is not False or denied.gate_stage is not None:
        raise AssertionError("fixture denied verdict must be a phase-2 denial")
    return {
        "allowed": allowed, "allowed_row": allowed_row,
        "other_decision": other_decision, "other_decision_row": other_decision_row,
        "msft": msft, "msft_row": msft_row,
        "denied": denied, "denied_row": denied_row,
    }


def _risk_fixture() -> dict:
    if not _RISK_FIXTURE:
        _RISK_FIXTURE.update(_build_risk_fixture())
    return _RISK_FIXTURE


def golden_inputs(**overrides) -> PreflightInputs:
    """The golden-good PreflightInputs: every stage passes (PreflightPass with
    capped_limit 190.00). Overrides replace whole fields; quote_b_verdict is
    recomputed via the REAL quote_quality.evaluate (same now_ms — §2.2 stage 8)
    unless explicitly overridden."""
    fixture = _risk_fixture()
    config = _exec_assembly()
    exec_config = ExecutionConfig.from_config(config)
    base = dict(
        run_id="run-m5",
        stamp=DecisionStamp(decision_id="d-1",
                            decision_ts_utc="2026-06-08T14:30:00.000000Z",
                            decision_seen_at_ms=_T0, quote_a=quote_a()),
        candidate=golden_candidate(),
        strategy_id="s1",
        strategy_is_synthetic=False,
        quote_b=quote_b(),
        feed_epoch_now=0,
        market_state=verdict_fixture("AAPL", session_date_et="2026-06-08"),
        risk_verdict=fixture["allowed"],
        risk_verdict_row=fixture["allowed_row"],
        risk_verdict_now_ms=_T0,
        kill_state="monitoring",
        kill_generation=0,
        open_orders_in_flight=0,
        artifact_check=SimpleNamespace(status="ok"),
        broker_kind="alpaca_paper",
        gates_config=config,
        exec_config=exec_config,
        now_ms=_NOW,
    )
    verdict_overridden = "quote_b_verdict" in overrides
    base.update(overrides)
    if not verdict_overridden:
        if base["quote_b"] is None:
            base["quote_b_verdict"] = None
        else:
            base["quote_b_verdict"] = quote_quality.evaluate(
                base["quote_b"], now_ms=base["now_ms"],
                spread_bps_max=base["exec_config"].spread_bps_max,
                staleness_ms_max=base["exec_config"].quote_staleness_ms_max)
    return PreflightInputs(**base)


def purge_open_authorizations():
    """Teardown hygiene: other suites (test_risk_kill case 5) assert the registry
    holds no open-kind authorization — never leak one."""
    for nonce, auth in list(execution_preflight._authorizations.items()):
        if auth.kind == "open":
            del execution_preflight._authorizations[nonce]


def _ms_verdict(**overrides) -> Verdict:
    """A healthy M2 verdict (halt=NONE, luld=NORMAL, RTH, TRADABLE, no blackout)
    with targeted overrides for the stage-9 member tests."""
    base = dict(symbol="AAPL", instrument_id=1001,
                session_state=SessionState.RTH, tradability=Tradability.TRADABLE,
                halt=HaltState.NONE, luld=LuldState.NORMAL, ssr=SsrState.INACTIVE,
                two_sided_nbbo=True, short_allowed=True, reasons=(),
                ca_blackout=False, session_date_et="2026-06-08")
    base.update(overrides)
    return Verdict(**base)


class _PreflightCase(unittest.TestCase):
    """Shared asserts."""

    def assertPhase2Reject(self, inputs, expected_reasons,
                           expected_skipped=()):
        result = evaluate_preflight(inputs)
        self.assertIsInstance(result, PreflightReject)
        self.assertEqual(result.reasons, expected_reasons)
        self.assertIsNone(result.gate_stage)
        self.assertEqual(result.stages_skipped, expected_skipped)
        self.assertTrue(result.preflight_id.startswith("pf-"))
        return result

    def assertTerminal(self, inputs, stage, reasons, skipped):
        result = evaluate_preflight(inputs)
        self.assertIsInstance(result, PreflightReject)
        self.assertEqual(result.reasons, reasons)
        self.assertEqual(result.gate_stage, stage)
        self.assertEqual(result.stages_skipped, skipped)
        self.assertIsNone(result.capped_limit)
        self.assertEqual(result.detail,
                         {"risk_reasons": None, "quote_reasons": None})
        return result


class TestPhase1Terminals(_PreflightCase):
    def test_run_gates_off_on_committed_config(self):
        self.assertTerminal(golden_inputs(gates_config=committed_assembly()),
                            "run_gates", ("run_gates_off",), _SKIP_AFTER_RUN_GATES)

    def test_run_gates_off_identity_strict_on_string_true(self):
        hostile = {"agent_rules": {"enabled": "true",
                                   "paper_trading": {"enabled": "true"}}}
        self.assertTerminal(golden_inputs(gates_config=hostile),
                            "run_gates", ("run_gates_off",), _SKIP_AFTER_RUN_GATES)

    def test_kill_switch_halted_for_halted_and_flattening(self):
        for state in ("halted", "flattening"):
            self.assertTerminal(golden_inputs(kill_state=state),
                                "kill", ("kill_switch_halted",), _SKIP_AFTER_KILL)

    def test_out_of_vocab_kill_state_raises_exec_error(self):
        with self.assertRaises(ExecError):
            evaluate_preflight(golden_inputs(kill_state="exploded"))

    def test_missing_decision_stamp_on_each_missing_component(self):
        good = golden_inputs().stamp
        bad_stamps = [
            None,
            DecisionStamp(decision_id="d-1", decision_ts_utc=good.decision_ts_utc,
                          decision_seen_at_ms=_T0, quote_a=None),
            DecisionStamp(decision_id="d-1", decision_ts_utc=good.decision_ts_utc,
                          decision_seen_at_ms="10000", quote_a=good.quote_a),
            DecisionStamp(decision_id="d-1", decision_ts_utc=good.decision_ts_utc,
                          decision_seen_at_ms=True, quote_a=good.quote_a),
            DecisionStamp(decision_id="d-1", decision_ts_utc="not-a-timestamp",
                          decision_seen_at_ms=_T0, quote_a=good.quote_a),
            DecisionStamp(decision_id="d-1",
                          decision_ts_utc="2026-06-08T14:30:00",  # naive, not UTC
                          decision_seen_at_ms=_T0, quote_a=good.quote_a),
        ]
        for stamp in bad_stamps:
            self.assertTerminal(golden_inputs(stamp=stamp),
                                "stamp", ("missing_decision_stamp",),
                                _SKIP_AFTER_STAMP)

    def test_multi_fault_trips_the_earliest_stage(self):
        # gates off + kill halted + stamp missing -> run_gates wins
        self.assertTerminal(
            golden_inputs(gates_config=committed_assembly(), kill_state="halted",
                          stamp=None),
            "run_gates", ("run_gates_off",), _SKIP_AFTER_RUN_GATES)
        # gates on + kill halted + stamp missing -> kill wins
        self.assertTerminal(golden_inputs(kill_state="halted", stamp=None),
                            "kill", ("kill_switch_halted",), _SKIP_AFTER_KILL)


class TestStage4Candidate(_PreflightCase):
    def test_order_matrix_unsupported_on_sell_open(self):
        # limit None keeps stage 10 marketable (sell cap vs bid), isolating the member.
        self.assertPhase2Reject(
            golden_inputs(candidate=golden_candidate(side="sell", limit=None)),
            ("order_matrix_unsupported",))

    def test_invalid_lot_on_fractional_qty(self):
        # qty 10.5 also unbinds the journaled verdict (qty binding) — both collected.
        self.assertPhase2Reject(
            golden_inputs(candidate=golden_candidate(qty="10.5")),
            ("invalid_lot", "risk_verdict_mismatch"))

    def test_invalid_lot_on_sub_one_share(self):
        self.assertPhase2Reject(
            golden_inputs(candidate=golden_candidate(qty="0.5")),
            ("invalid_lot", "risk_verdict_mismatch"))

    def test_multi_leg_candidate_raises_exec_error(self):
        leg = golden_candidate().legs[0]
        leg2 = Leg(symbol="MSFT", instrument_id=1002, side="buy",
                   qty=Decimal("1"), limit_price=Decimal("100.00"))
        candidate = Candidate(strategy_id="s1", legs=(leg, leg2),
                              paper_eligible=True, score=None)
        with self.assertRaises(ExecError):
            evaluate_preflight(golden_inputs(candidate=candidate))


class TestStage5StrategyGate(_PreflightCase):
    def test_strategy_not_paper_eligible_identity_strict(self):
        candidate = Candidate(strategy_id="s1", legs=golden_candidate().legs,
                              paper_eligible=False, score=None)
        self.assertPhase2Reject(golden_inputs(candidate=candidate),
                                ("strategy_not_paper_eligible",))

    def test_backtest_artifact_missing(self):
        self.assertPhase2Reject(
            golden_inputs(artifact_check=SimpleNamespace(status="missing")),
            ("backtest_artifact_missing",))

    def test_artifact_key_mismatch(self):
        self.assertPhase2Reject(
            golden_inputs(artifact_check=SimpleNamespace(status="key_mismatch")),
            ("artifact_key_mismatch",))

    def test_artifact_hash_invalid(self):
        self.assertPhase2Reject(
            golden_inputs(artifact_check=SimpleNamespace(status="hash_invalid")),
            ("artifact_hash_invalid",))

    def test_synthetic_requires_fake_broker(self):
        self.assertPhase2Reject(
            golden_inputs(strategy_is_synthetic=True, broker_kind="alpaca_paper"),
            ("synthetic_requires_fake_broker",))

    def test_fake_broker_requires_synthetic(self):
        self.assertPhase2Reject(golden_inputs(broker_kind="fake"),
                                ("fake_broker_requires_synthetic",))

    def test_synthetic_with_fake_broker_does_not_consult_artifact(self):
        result = evaluate_preflight(golden_inputs(
            strategy_is_synthetic=True, broker_kind="fake",
            artifact_check=SimpleNamespace(status="missing")))
        self.assertIsInstance(result, PreflightPass)

    def test_out_of_vocab_artifact_status_raises(self):
        with self.assertRaises(ExecError):
            evaluate_preflight(
                golden_inputs(artifact_check=SimpleNamespace(status="weird")))

    def test_out_of_vocab_broker_kind_raises(self):
        with self.assertRaises(ExecError):
            evaluate_preflight(golden_inputs(broker_kind="robinhood"))


class TestStage6Inflight(_PreflightCase):
    def test_open_order_in_flight(self):
        self.assertPhase2Reject(golden_inputs(open_orders_in_flight=1),
                                ("open_order_in_flight",))


class TestStage7Latency(_PreflightCase):
    def test_latency_passes_at_exactly_the_budget(self):
        self.assertIsInstance(evaluate_preflight(golden_inputs()), PreflightPass)

    def test_latency_not_elapsed_at_budget_minus_one(self):
        self.assertPhase2Reject(
            golden_inputs(quote_b=quote_b(seen_at_ms=_T0 + 249)),
            ("latency_not_elapsed",))

    def test_requote_not_later_on_identical_provenance_reserve(self):
        # Same (vendor_seq, ts_event_utc) as quote A: a re-served quote A is not a
        # second quote, even with a later seen_at_ms.
        reserve = quote_b(vendor_seq=1,
                          ts_event_utc="2026-06-08T14:30:00.000000Z")
        self.assertPhase2Reject(golden_inputs(quote_b=reserve),
                                ("requote_not_later",))

    def test_requote_not_later_on_clock_not_strictly_later(self):
        # seen_at == quote_a's: also inside the latency budget by construction.
        self.assertPhase2Reject(
            golden_inputs(quote_b=quote_b(seen_at_ms=_T0)),
            ("latency_not_elapsed", "requote_not_later"))

    def test_epoch_changed_between_a_and_b(self):
        self.assertPhase2Reject(
            golden_inputs(quote_b=quote_b(reconnect_epoch=1), feed_epoch_now=1),
            ("epoch_changed",))

    def test_epoch_changed_between_b_and_feed_now(self):
        self.assertPhase2Reject(golden_inputs(feed_epoch_now=1),
                                ("epoch_changed",))


class TestStage8Quote(_PreflightCase):
    def test_quote_missing_skips_latency_and_order(self):
        result = self.assertPhase2Reject(
            golden_inputs(quote_b=None), ("quote_missing",),
            expected_skipped=("latency", "order"))
        self.assertIsNone(result.capped_limit)
        self.assertEqual(result.detail,
                         {"risk_reasons": None, "quote_reasons": None})

    def test_quote_stale_strict_boundary(self):
        # age 2001 at now=12251; risk verdict kept fresh at exactly its boundary.
        result = self.assertPhase2Reject(
            golden_inputs(now_ms=12_251, risk_verdict_now_ms=10_251),
            ("quote_stale",), expected_skipped=("order",))
        self.assertIsNone(result.capped_limit)
        self.assertEqual(result.detail["quote_reasons"], ["quote_stale"])

    def test_quote_crossed_embedded_verbatim(self):
        result = self.assertPhase2Reject(
            golden_inputs(quote_b=quote_b(bid="190.00", ask="189.90")),
            ("quote_crossed",), expected_skipped=("order",))
        self.assertEqual(result.detail["quote_reasons"], ["quote_crossed"])

    def test_quote_locked(self):
        self.assertPhase2Reject(
            golden_inputs(quote_b=quote_b(bid="189.96", ask="189.96")),
            ("quote_locked",), expected_skipped=("order",))

    def test_quote_one_sided(self):
        self.assertPhase2Reject(
            golden_inputs(quote_b=quote_b(ask=None, ask_sz=None)),
            ("quote_one_sided",), expected_skipped=("order",))

    def test_quote_nonfinite(self):
        self.assertPhase2Reject(
            golden_inputs(quote_b=quote_b(ask="NaN")),
            ("quote_nonfinite",), expected_skipped=("order",))

    def test_quote_nonpositive(self):
        self.assertPhase2Reject(
            golden_inputs(quote_b=quote_b(bid="0")),
            ("quote_nonpositive",), expected_skipped=("order",))

    def test_spread_too_wide(self):
        self.assertPhase2Reject(
            golden_inputs(quote_b=quote_b(bid="189.00", ask="190.00")),
            ("spread_too_wide",), expected_skipped=("order",))

    def test_quote_b_without_verdict_raises_exec_error(self):
        inputs = golden_inputs(quote_b_verdict=None)
        with self.assertRaises(ExecError):
            evaluate_preflight(inputs)


class TestStage9MarketState(_PreflightCase):
    def test_real_safe_default_verdict_fires_exactly_five_reasons(self):
        # The REAL fail-closed default (market_state_cache.py:112-124), via the
        # real function — pinned to EXACTLY five sorted reasons (M5C-3).
        safe_default = MarketStateCache.safe_default_verdict(
            "AAPL", 1001, "2026-06-08")
        result = self.assertPhase2Reject(
            golden_inputs(market_state=safe_default),
            ("ca_blackout", "halt_luld_auction", "market_state_not_rth",
             "market_state_not_tradable", "market_state_stale_default"))
        # quote/order stages were healthy: the cap stays derivable on the reject row.
        self.assertEqual(result.capped_limit, Decimal("190.00"))

    def test_healthy_verdict_fires_zero_stage9_reasons(self):
        # halt=NONE, luld=NORMAL, RTH, TRADABLE, no blackout (M5C-2).
        result = evaluate_preflight(golden_inputs(market_state=_ms_verdict()))
        self.assertIsInstance(result, PreflightPass)

    def test_market_state_not_tradable_includes_reduce_only(self):
        self.assertPhase2Reject(
            golden_inputs(market_state=verdict_fixture(
                "AAPL", Tradability.REDUCE_ONLY, session_date_et="2026-06-08")),
            ("market_state_not_tradable",))

    def test_market_state_stale_default_member(self):
        self.assertPhase2Reject(
            golden_inputs(market_state=_ms_verdict(
                reasons=("cache_stale_safe_default",))),
            ("market_state_stale_default",))

    def test_market_state_not_rth(self):
        for state in (SessionState.PRE, SessionState.POST, SessionState.CLOSED):
            self.assertPhase2Reject(
                golden_inputs(market_state=_ms_verdict(session_state=state)),
                ("market_state_not_rth",))

    def test_halt_luld_auction_on_any_halt(self):
        for halt in (HaltState.HALTED, HaltState.PAUSED_LULD, HaltState.RESUMING,
                     HaltState.UNKNOWN):
            self.assertPhase2Reject(
                golden_inputs(market_state=_ms_verdict(halt=halt)),
                ("halt_luld_auction",))

    def test_halt_luld_auction_on_luld_real_vocabulary(self):
        # LuldState.NORMAL is the ONLY non-firing member (M5C-B1 note: there is no
        # LuldState.NONE) — LIMIT/PAUSED/UNKNOWN all fire.
        for luld in (LuldState.LIMIT, LuldState.PAUSED, LuldState.UNKNOWN):
            self.assertPhase2Reject(
                golden_inputs(market_state=_ms_verdict(luld=luld)),
                ("halt_luld_auction",))

    def test_halt_luld_auction_on_auction_session(self):
        self.assertPhase2Reject(
            golden_inputs(market_state=_ms_verdict(
                session_state=SessionState.AUCTION)),
            ("halt_luld_auction", "market_state_not_rth"))

    def test_ca_blackout(self):
        self.assertPhase2Reject(
            golden_inputs(market_state=_ms_verdict(ca_blackout=True)),
            ("ca_blackout",))

    def test_identity_mismatch_raises_exec_error(self):
        with self.assertRaises(ExecError):
            evaluate_preflight(golden_inputs(
                market_state=verdict_fixture("MSFT",
                                             session_date_et="2026-06-08")))
        with self.assertRaises(ExecError):
            evaluate_preflight(golden_inputs(
                market_state=_ms_verdict(instrument_id=9999)))


class TestStage10Order(_PreflightCase):
    def test_unpriceable_candidate_on_nonpositive_strategy_limit(self):
        result = self.assertPhase2Reject(
            golden_inputs(candidate=golden_candidate(limit="0")),
            ("unpriceable_candidate",))
        self.assertIsNone(result.capped_limit)

    def test_invalid_tick_on_off_grid_strategy_limit(self):
        result = self.assertPhase2Reject(
            golden_inputs(candidate=golden_candidate(limit="190.005")),
            ("invalid_tick",))
        self.assertIsNone(result.capped_limit)

    def test_not_marketable_keeps_the_derivable_cap(self):
        result = self.assertPhase2Reject(
            golden_inputs(candidate=golden_candidate(limit="189.90")),
            ("not_marketable",))
        self.assertEqual(result.capped_limit, Decimal("189.90"))

    def test_latency_lost_edge_on_quantized_bps_form(self):
        # adverse A->B = (190.42-189.94)/189.94*1e4 = 25.27 bps > 25 (strict, on
        # the quantized value — EX-1); limit None keeps the cap marketable.
        result = self.assertPhase2Reject(
            golden_inputs(candidate=golden_candidate(limit=None),
                          quote_b=quote_b(bid="190.38", ask="190.42")),
            ("latency_lost_edge",))
        self.assertEqual(result.capped_limit, Decimal("190.89"))

    def test_float_in_a_money_slot_raises_exec_error(self):
        clean = golden_inputs()
        poisoned = golden_inputs(quote_b=quote_b(ask=189.96),
                                 quote_b_verdict=clean.quote_b_verdict)
        with self.assertRaises(ExecError):
            evaluate_preflight(poisoned)


class TestStage11Risk(_PreflightCase):
    def test_risk_verdict_missing_on_each_missing_component(self):
        for overrides in ({"risk_verdict": None}, {"risk_verdict_row": None},
                          {"risk_verdict_now_ms": None}):
            self.assertPhase2Reject(golden_inputs(**overrides),
                                    ("risk_verdict_missing",))

    def test_risk_verdict_stale_strict_at_2001(self):
        self.assertPhase2Reject(golden_inputs(now_ms=12_001),
                                ("risk_verdict_stale",))

    def test_risk_verdict_fresh_at_exactly_2000(self):
        self.assertIsInstance(evaluate_preflight(golden_inputs(now_ms=12_000)),
                              PreflightPass)

    def test_risk_verdict_mismatch_on_verdict_id(self):
        fixture = _risk_fixture()
        self.assertPhase2Reject(
            golden_inputs(risk_verdict=fixture["other_decision"],
                          risk_verdict_row=fixture["allowed_row"]),
            ("risk_verdict_mismatch",))

    def test_risk_verdict_mismatch_on_decision_id(self):
        fixture = _risk_fixture()
        self.assertPhase2Reject(
            golden_inputs(risk_verdict=fixture["other_decision"],
                          risk_verdict_row=fixture["other_decision_row"]),
            ("risk_verdict_mismatch",))

    def test_risk_verdict_mismatch_on_symbol(self):
        fixture = _risk_fixture()
        self.assertPhase2Reject(
            golden_inputs(risk_verdict=fixture["msft"],
                          risk_verdict_row=fixture["msft_row"]),
            ("risk_verdict_mismatch",))

    def test_risk_verdict_mismatch_on_qty(self):
        self.assertPhase2Reject(
            golden_inputs(candidate=golden_candidate(qty="5")),
            ("risk_verdict_mismatch",))

    def test_can_open_denied_carries_m4_reasons_in_detail_only(self):
        fixture = _risk_fixture()
        result = self.assertPhase2Reject(
            golden_inputs(risk_verdict=fixture["denied"],
                          risk_verdict_row=fixture["denied_row"]),
            ("can_open_denied",))
        self.assertEqual(result.detail["risk_reasons"],
                         list(fixture["denied"].reasons))
        # the M4 strings never leak into the preflight vocabulary
        self.assertEqual(set(result.reasons)
                         & set(fixture["denied"].reasons), set())

    def test_kill_generation_changed_on_stale_world_verdict(self):
        self.assertPhase2Reject(golden_inputs(kill_generation=1),
                                ("kill_generation_changed",))

    def test_tampered_row_hash_raises_exec_error(self):
        fixture = _risk_fixture()
        tampered = dict(fixture["allowed_row"])
        tampered["gross_notional"] = Decimal("1.00")
        with self.assertRaises(ExecError):
            evaluate_preflight(golden_inputs(risk_verdict_row=tampered))
        stripped = dict(fixture["allowed_row"])
        stripped.pop("hash")
        with self.assertRaises(ExecError):
            evaluate_preflight(golden_inputs(risk_verdict_row=stripped))


class TestCollectAllUnion(_PreflightCase):
    def test_sorted_deduped_union_across_stages(self):
        safe_default = MarketStateCache.safe_default_verdict(
            "AAPL", 1001, "2026-06-08")
        candidate = Candidate(strategy_id="s1", legs=golden_candidate().legs,
                              paper_eligible=False, score=None)
        self.assertPhase2Reject(
            golden_inputs(market_state=safe_default, open_orders_in_flight=2,
                          candidate=candidate),
            ("ca_blackout", "halt_luld_auction", "market_state_not_rth",
             "market_state_not_tradable", "market_state_stale_default",
             "open_order_in_flight", "strategy_not_paper_eligible"))


class TestPassShape(_PreflightCase):
    def test_golden_pass_fields(self):
        fixture = _risk_fixture()
        result = evaluate_preflight(golden_inputs())
        self.assertIsInstance(result, PreflightPass)
        self.assertTrue(result.preflight_id.startswith("pf-"))
        self.assertEqual(result.symbol, "AAPL")
        self.assertEqual(result.instrument_id, 1001)
        self.assertEqual(result.side, "buy")
        self.assertEqual(result.qty, Decimal("10"))
        self.assertEqual(result.capped_limit, Decimal("190.00"))
        self.assertEqual(result.latency_observed_ms, 250)
        self.assertEqual(result.kill_generation, 0)
        self.assertEqual(result.risk_verdict_id, fixture["allowed"].verdict_id)
        self.assertEqual(result.quote_b_provenance, {
            "dataset": "EQUS.MINI", "schema": "tbbo",
            "ts_event_utc": "2026-06-08T14:30:00.250000Z",
            "ts_recv_utc": "2026-06-08T14:30:00.260000Z",
            "seen_at_ms": _NOW, "reconnect_epoch": 0, "vendor_seq": 2})


class TestCommittedConfigCanary(_PreflightCase):
    """S1: on the REAL committed JSON every preflight terminates at run_gates with
    the byte-exact terminal shape, for a sweep of otherwise-good (and even
    malformed) inputs — and the mint never issues an authorization."""

    def tearDown(self):
        purge_open_authorizations()

    def _sweep(self):
        committed = committed_assembly()
        leg = golden_candidate().legs[0]
        two_leg = Candidate(
            strategy_id="s1",
            legs=(leg, Leg(symbol="MSFT", instrument_id=1002, side="buy",
                           qty=Decimal("1"), limit_price=Decimal("100.00"))),
            paper_eligible=True, score=None)
        return [
            golden_inputs(gates_config=committed),
            golden_inputs(gates_config=committed, kill_state="halted"),
            golden_inputs(gates_config=committed, kill_state="exploded"),
            golden_inputs(gates_config=committed, stamp=None, quote_b=None,
                          risk_verdict=None, risk_verdict_row=None,
                          risk_verdict_now_ms=None),
            golden_inputs(gates_config=committed,
                          candidate=golden_candidate(side="sell", qty="10.5",
                                                     limit=None)),
            golden_inputs(gates_config=committed, candidate=two_leg),
            golden_inputs(gates_config=committed, broker_kind="fake",
                          strategy_is_synthetic=True,
                          open_orders_in_flight=3),
        ]

    def test_sweep_terminates_at_run_gates_byte_exact(self):
        for inputs in self._sweep():
            self.assertTerminal(inputs, "run_gates", ("run_gates_off",),
                                _SKIP_AFTER_RUN_GATES)

    def test_sweep_mint_rejects_and_issues_nothing(self):
        for inputs in self._sweep():
            with self.assertRaises(PreflightRejected) as caught:
                mint_open_token(inputs)
            reject = caught.exception.reject
            self.assertEqual(reject.reasons, ("run_gates_off",))
            self.assertEqual(reject.gate_stage, "run_gates")
            self.assertEqual(reject.stages_skipped, _SKIP_AFTER_RUN_GATES)
            self.assertIsNone(reject.capped_limit)
        open_kinds = [auth for auth in
                      execution_preflight._authorizations.values()
                      if auth.kind == "open"]
        self.assertEqual(open_kinds, [])


class TestMintOpenToken(_PreflightCase):
    def tearDown(self):
        purge_open_authorizations()

    def test_mint_on_pass_issues_bound_open_authorization(self):
        inputs = golden_inputs()
        token, pass_ = mint_open_token(inputs)
        self.assertIsInstance(pass_, PreflightPass)
        self.assertTrue(execution_preflight.is_authentic(token))
        auth = execution_preflight.authorization_of(token)
        self.assertEqual(auth.kind, "open")
        self.assertEqual(auth.symbol, "AAPL")
        self.assertEqual(auth.side, "buy")
        self.assertEqual(auth.qty, Decimal("10"))
        self.assertEqual(auth.limit_price, pass_.capped_limit)
        self.assertEqual(auth.kill_generation, 0)
        self.assertEqual(auth.minted_at_ms, inputs.now_ms)

    def test_mint_on_reject_raises_with_reject_attached(self):
        inputs = golden_inputs(open_orders_in_flight=1)
        before = sum(1 for auth in
                     execution_preflight._authorizations.values()
                     if auth.kind == "open")
        with self.assertRaises(PreflightRejected) as caught:
            mint_open_token(inputs)
        self.assertEqual(caught.exception.reject, evaluate_preflight(inputs))
        after = sum(1 for auth in
                    execution_preflight._authorizations.values()
                    if auth.kind == "open")
        self.assertEqual(after, before)


class TestPurityAndDeterminism(_PreflightCase):
    def test_same_inputs_identical_pass_and_preflight_id(self):
        first = evaluate_preflight(golden_inputs())
        second = evaluate_preflight(golden_inputs())
        self.assertEqual(first, second)
        self.assertEqual(first.preflight_id, second.preflight_id)

    def test_same_inputs_identical_reject_and_preflight_id(self):
        first = evaluate_preflight(golden_inputs(open_orders_in_flight=1))
        second = evaluate_preflight(golden_inputs(open_orders_in_flight=1))
        self.assertEqual(first, second)
        self.assertEqual(first.preflight_id, second.preflight_id)

    def test_pass_and_reject_ids_differ(self):
        pass_id = evaluate_preflight(golden_inputs()).preflight_id
        reject_id = evaluate_preflight(
            golden_inputs(open_orders_in_flight=1)).preflight_id
        self.assertNotEqual(pass_id, reject_id)

    def test_module_reads_no_wall_clock(self):
        # FD-M5-14: evaluate_preflight takes no clock and the module never reads a
        # wall clock — the ONLY clock in the module is the orchestrator-INJECTED
        # consume-time runtime (clock.now_ms()), which is not a wall-clock read.
        source = (Path(execution_preflight.__file__)
                  .read_text(encoding="utf-8"))
        for token in ("time.time", "time.monotonic", "perf_counter",
                      "datetime.now", "utcnow", "import time"):
            self.assertNotIn(token, source)


class TestReservedMembers(unittest.TestCase):
    def test_reserved_members_refuse_emission_in_m5(self):
        self.assertEqual(RESERVED_PREFLIGHT_REASONS,
                         frozenset({"ssr_short_blocked", "locate_unavailable",
                                    "extended_hours_blocked"}))
        for code in sorted(RESERVED_PREFLIGHT_REASONS):
            with self.assertRaises(ExecError):
                require_reason(code)


if __name__ == "__main__":
    unittest.main()
