"""M4 §M test 8 — RiskEngine.can_open: the single pre-trade chokepoint.

Invariants: S1 (committed config terminates at run_gates; zero-caps second wall),
R1 (fail-closed staleness + reduce-path pairing), R2, R4 (determinism), R7 (vocab
guard), R8 (ladder order, collect-all, allowed <=> reasons==()), R10, R12.
"""
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from agent.candidate import Candidate, Leg
from agent.config import load
from agent.market_state import Tradability
from agent.risk.account_state import AccountStore, Mark, parse_account_payload
from agent.risk.can_open import LegRead, RiskEngine, RiskVerdict
from agent.risk.intraday_margin import DeficitRecord, FreezeState, MarginRead
from agent.risk.locate import DenyAllLocate, LocateCheck
from agent.risk.loss_limits import LossRead
from agent.risk.pdt_compat import PdtRead
from agent.risk.reasons import GATE_STAGES, RESERVED_REASONS, RiskError
from agent.risk.risk_config import RiskConfig
from agent.risk.risk_kill import RiskKillSwitch
from agent.risk.risk_ledger import RiskLedger, replay_risk
from agent.serializer import BrokerUSD
from recorder.persistence import EventWriter
from tests.lib.fakes import FakeClock, SpyBroker
from tests.lib.risk_fixtures import (
    account_payload,
    gates_on_fixture_config,
    permissive_fixture_config,
    portfolio_fixture,
    verdict_fixture,
)

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / "config"
_DATE = "2026-06-08"
_CLOCK = lambda: "2026-06-08T20:00:00.000000+00:00"  # noqa: E731


def _committed_config():
    return {
        "agent_rules": load(_CONFIG / "agent_rules.json"),
        "risk_rules": load(_CONFIG / "risk_rules.json"),
    }


def _engine(config=None, run_id="run-1"):
    config = config or permissive_fixture_config()
    return RiskEngine(cfg=RiskConfig.from_config(config), gates_config=config,
                      run_id=run_id)


def _fresh_account(**overrides):
    store = AccountStore(clock=FakeClock(start_ms=0))
    store.put(parse_account_payload(account_payload(**overrides), source="fixture",
                                    seen_at_ms=0, ts_read_utc="2026-06-08T14:00:00Z"))
    return store.get()


def _degraded_account(status):
    if status == "missing":
        return AccountStore(clock=FakeClock()).get()
    clock = FakeClock(start_ms=0)
    store = AccountStore(clock=clock)
    if status == "stale":
        store.put(parse_account_payload(account_payload(), source="fixture",
                                        seen_at_ms=0, ts_read_utc="t"))
        clock.advance(5001)
    elif status == "invalid":
        store.put(parse_account_payload(account_payload(equity="NaN"), source="fixture",
                                        seen_at_ms=0, ts_read_utc="t"))
    elif status == "skew":
        store.put(parse_account_payload(account_payload(), source="fixture",
                                        seen_at_ms=100, ts_read_utc="t"))
    return store.get()


def _leg(symbol="AAPL", side="buy", qty="10", limit="190.00", instrument_id=1001):
    return Leg(symbol=symbol, instrument_id=instrument_id, side=side,
               qty=Decimal(qty), limit_price=None if limit is None else Decimal(limit))


def _candidate(*legs, paper_eligible=True, strategy_id="s1"):
    legs = legs or (_leg(),)
    return Candidate(strategy_id=strategy_id, legs=tuple(legs),
                     paper_eligible=paper_eligible, score=None)


def _margin_read(asof=_DATE, outstanding=(), freeze=None):
    freeze = freeze or FreezeState(active=False, trigger_deficit_id=None,
                                   effective_from_et=None, expires_on_et=None)
    return MarginRead(outstanding_nonminor=tuple(outstanding), freeze=freeze,
                      asof_session_date_et=asof)


def _loss_read(hwm="100000.00"):
    return LossRead(hwm_equity=None if hwm is None else BrokerUSD(hwm),
                    daily_loss_usd=None, drawdown_usd=None, breaches=())


def _kwargs(**overrides):
    base = dict(
        market_state={"AAPL": verdict_fixture("AAPL", Tradability.TRADABLE,
                                              session_date_et=_DATE)},
        marks={},
        kill_state="monitoring",
        kill_generation=0,
        margin_read=_margin_read(),
        pdt_read=PdtRead(state="unknown", evidence=None, rejection_latched=False),
        loss_read=_loss_read(),
        now_ms=10_000,
        decision_id="d-1",
    )
    base.update(overrides)
    return base


class TestCommittedConfigCanary(unittest.TestCase):
    def test_every_call_terminates_at_run_gates_with_frozen_terminal_shape(self):
        engine = _engine(_committed_config())
        sweep_candidates = [
            _candidate(),
            _candidate(_leg(side="sell")),
            _candidate(_leg(), _leg(symbol="MSFT", instrument_id=1002)),
            _candidate(_leg(limit=None)),
        ]
        sweep_portfolios = [portfolio_fixture("flat"), portfolio_fixture("long_short"),
                            None]
        sweep_accounts = [_fresh_account(), _degraded_account("missing"),
                          _degraded_account("invalid")]
        for candidate in sweep_candidates:
            for portfolio in sweep_portfolios:
                for account in sweep_accounts:
                    verdict = engine.can_open(candidate, portfolio, account,
                                              **_kwargs())
                    self.assertIs(verdict.allowed, False)
                    self.assertEqual(verdict.reasons, ("run_gates_off",))
                    self.assertEqual(verdict.gate_stage, "run_gates")
                    self.assertEqual(verdict.stages_skipped, GATE_STAGES[1:])
                    # LD-R1 frozen terminal shape, asserted byte-exact:
                    self.assertEqual(verdict.legs, ())
                    self.assertEqual(verdict.gross_notional, Decimal("0"))
                    self.assertEqual(str(verdict.gross_notional), "0")
                    self.assertEqual(verdict.caps_used, ())
                    self.assertIsNone(verdict.session_date_et)

    def test_committed_config_zero_submits_at_broker_boundary(self):
        engine = _engine(_committed_config())
        broker = SpyBroker()
        verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                  _fresh_account(), **_kwargs())
        self.assertIs(verdict.allowed, False)
        self.assertEqual(broker.calls, [])
        self.assertEqual(broker.submitted, [])


class TestSecondWall(unittest.TestCase):
    def test_canonical_second_wall_exact_frozen_tuple(self):
        # RM-5/§J: gates-ON fixture config with committed zero caps + empty universe.
        engine = _engine(gates_on_fixture_config())
        verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                  _fresh_account(), **_kwargs())
        self.assertIs(verdict.allowed, False)
        self.assertIsNone(verdict.gate_stage)   # phase 2 reached
        self.assertEqual(verdict.reasons, (
            "beta_unknown", "gross_exposure_cap_exceeded", "net_exposure_cap_exceeded",
            "position_cap_exceeded", "sector_unknown", "universe_excluded"))

    def test_pass_path_reachable_only_under_permissive_fixture(self):
        engine = _engine()
        verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                  _fresh_account(), **_kwargs())
        self.assertIs(verdict.allowed, True)
        self.assertEqual(verdict.reasons, ())
        self.assertIsNone(verdict.gate_stage)
        self.assertEqual(verdict.gross_notional, Decimal("1900.00"))
        self.assertEqual(verdict.session_date_et, _DATE)
        self.assertEqual(len(verdict.legs), 1)
        self.assertEqual(verdict.legs[0].classification, "opening_long")
        self.assertEqual(verdict.legs[0].cap_notional, Decimal("1900.00"))
        self.assertIs(verdict.legs[0].mark_used, False)
        names = [row[0] for row in verdict.caps_used]
        self.assertEqual(names, sorted(names))   # full union sorted by name ONCE
        self.assertIn("buying_power", names)
        self.assertIn("max_daily_loss_usd", names)
        self.assertIn("max_drawdown_usd", names)
        self.assertIn("max_position_usd:AAPL", names)
        self.assertIsNotNone(verdict.account_snapshot_id)


class TestPhase1Ladder(unittest.TestCase):
    def test_kill_stage_rejects_for_flattening_and_halted(self):
        engine = _engine()
        for state in ("flattening", "halted"):
            verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                      _fresh_account(),
                                      **_kwargs(kill_state=state, kill_generation=1))
            self.assertEqual(verdict.reasons, ("kill_switch_halted",))
            self.assertEqual(verdict.gate_stage, "kill")
            self.assertEqual(verdict.stages_skipped, GATE_STAGES[2:])
            self.assertEqual(verdict.kill_state, state)
            self.assertEqual(verdict.kill_generation, 1)

    def test_kill_stage_consumes_a_rehydrated_halted_switch(self):
        # S8 drill case 7 composition: rung 2 rejects on the REHYDRATED latch.
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "risk.jsonl"
            ledger = RiskLedger(EventWriter(path, "run-1", clock=_CLOCK),
                                rules_hash="rh")
            tripped = RiskKillSwitch(cfg=RiskConfig.from_config(
                permissive_fixture_config()), ledger=ledger)
            tripped.trigger("drill", SpyBroker(), portfolio_fixture("long_only"))
            fresh = RiskKillSwitch(cfg=RiskConfig.from_config(
                permissive_fixture_config()), ledger=None)
            fresh.rehydrate(replay_risk(path))
            engine = _engine()
            verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                      _fresh_account(),
                                      **_kwargs(kill_state=fresh.state,
                                                kill_generation=fresh.generation))
            self.assertEqual(verdict.reasons, ("kill_switch_halted",))

    def test_margin_freeze_stage_pinned_to_marginread_asof(self):
        engine = _engine()
        freeze = FreezeState(active=True, trigger_deficit_id="imd-x",
                             effective_from_et="2026-06-01",
                             expires_on_et="2026-08-30")
        verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                  _fresh_account(),
                                  **_kwargs(margin_read=_margin_read(freeze=freeze)))
        self.assertEqual(verdict.reasons, ("margin_freeze_active",))
        self.assertEqual(verdict.gate_stage, "margin_freeze")
        # same freeze, asof outside the window -> not terminal at this rung
        outside = _margin_read(asof="2026-09-01", freeze=freeze)
        market_state = {"AAPL": verdict_fixture("AAPL", Tradability.TRADABLE,
                                                session_date_et="2026-09-01")}
        verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                  _fresh_account(),
                                  **_kwargs(margin_read=outside,
                                            market_state=market_state))
        self.assertNotIn("margin_freeze_active", verdict.reasons)

    def test_account_stage_each_degraded_status_with_paired_reduce_mint(self):
        from agent.broker.base import OrderIntent
        from agent.execution_preflight import mint_reduce_only_token

        engine = _engine()
        expected = {"missing": "account_missing", "stale": "account_stale",
                    "invalid": "account_invalid", "skew": "account_clock_skew"}
        for status, reason in expected.items():
            account = _degraded_account(status)
            verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                      account, **_kwargs())
            self.assertEqual(verdict.reasons, (reason,), status)
            self.assertEqual(verdict.gate_stage, "account", status)
            self.assertEqual(verdict.stages_skipped, GATE_STAGES[4:], status)
            # R1 pairing: the reduce path is untouched under this exact condition
            held = portfolio_fixture("long_only").positions[0]
            intent = OrderIntent(symbol="AAPL", side="sell", qty=Decimal("1"),
                                 is_reducing=True, intent_id=f"r-{status}")
            self.assertIsNotNone(mint_reduce_only_token(held, intent), status)

    def test_portfolio_stage(self):
        engine = _engine()
        verdict = engine.can_open(_candidate(), None, _fresh_account(), **_kwargs())
        self.assertEqual(verdict.reasons, ("portfolio_missing",))
        self.assertEqual(verdict.gate_stage, "portfolio")
        self.assertEqual(verdict.stages_skipped, GATE_STAGES[5:])
        stale = portfolio_fixture("flat", stale=True)
        verdict = engine.can_open(_candidate(), stale, _fresh_account(), **_kwargs())
        self.assertEqual(verdict.reasons, ("portfolio_stale",))
        drifted = portfolio_fixture("flat")
        drifted = type(drifted)(positions=drifted.positions, source=drifted.source,
                                seen_at_ms=0, stale=False, unreconciled_drift=True)
        verdict = engine.can_open(_candidate(), drifted, _fresh_account(), **_kwargs())
        self.assertEqual(verdict.reasons, ("portfolio_unreconciled",))
        both = type(drifted)(positions=drifted.positions, source=drifted.source,
                             seen_at_ms=0, stale=True, unreconciled_drift=True)
        verdict = engine.can_open(_candidate(), both, _fresh_account(), **_kwargs())
        self.assertEqual(verdict.reasons, ("portfolio_stale", "portfolio_unreconciled"))

    def test_multi_fault_trips_earliest_terminal_stage_only(self):
        engine = _engine()
        verdict = engine.can_open(_candidate(), None, _degraded_account("stale"),
                                  **_kwargs(kill_state="halted", kill_generation=2))
        self.assertEqual(verdict.reasons, ("kill_switch_halted",))   # earliest wins
        self.assertEqual(verdict.gate_stage, "kill")
        self.assertIn("account", verdict.stages_skipped)
        self.assertIn("portfolio", verdict.stages_skipped)


class TestPhase2Stages(unittest.TestCase):
    def test_classification_table_with_stage6_reasons(self):
        engine = _engine()
        portfolio = portfolio_fixture("long_short")   # AAPL +10, MSFT -5
        ms = {
            "AAPL": verdict_fixture("AAPL", session_date_et=_DATE),
            "MSFT": verdict_fixture("MSFT", instrument_id=1002, session_date_et=_DATE),
            "XOM": verdict_fixture("XOM", instrument_id=77, session_date_et=_DATE),
        }
        cases = [
            (_leg("AAPL", "buy", "5"), "opening_long", None),
            (_leg("MSFT", "buy", "5", instrument_id=1002), "reducing",
             "reduce_path_not_can_open"),                        # cover
            (_leg("MSFT", "buy", "8", instrument_id=1002), "short_or_flip",
             "reduce_path_not_can_open"),                        # flip buy (LD-R1)
            (_leg("AAPL", "sell", "10"), "reducing", "reduce_path_not_can_open"),
            (_leg("AAPL", "sell", "11"), "short_or_flip", "short_side_disabled"),
            (_leg("XOM", "sell", "1", instrument_id=77), "short_or_flip",
             "short_side_disabled"),
        ]
        for leg, classification, reason in cases:
            verdict = engine.can_open(_candidate(leg), portfolio, _fresh_account(),
                                      **_kwargs(market_state=ms))
            self.assertEqual(verdict.legs[0].classification, classification,
                             (leg.symbol, leg.side, leg.qty))
            if reason is None:
                self.assertNotIn("reduce_path_not_can_open", verdict.reasons)
                self.assertNotIn("short_side_disabled", verdict.reasons)
            else:
                self.assertIn(reason, verdict.reasons, (leg.symbol, leg.side))

    def test_unpriceable_and_not_identity_paper_eligible(self):
        engine = _engine()
        for limit in (None, "0", "-1"):
            verdict = engine.can_open(_candidate(_leg(limit=limit)),
                                      portfolio_fixture("flat"), _fresh_account(),
                                      **_kwargs())
            self.assertIn("unpriceable_candidate", verdict.reasons, limit)
            # caps/margin skipped iff every opening leg is unpriceable
            self.assertIn("caps", verdict.stages_skipped, limit)
            self.assertIn("margin", verdict.stages_skipped, limit)
            self.assertEqual(verdict.gross_notional, Decimal("0"))
        verdict = engine.can_open(_candidate(paper_eligible=1),
                                  portfolio_fixture("flat"), _fresh_account(),
                                  **_kwargs())
        self.assertIn("strategy_not_paper_eligible", verdict.reasons)  # identity check

    def test_unpriceable_out_of_universe_leg_still_poisons_sector_beta(self):
        # Harden round 1, M4-R3: an UNPRICEABLE out-of-universe opening leg must
        # poison the sector/beta projections exactly like a priceable one — the
        # *_unknown reasons fire and the sector/beta caps_used rows are suppressed
        # (FD-M4-17: never a partial sum).
        engine = _engine()
        ms = {
            "AAPL": verdict_fixture("AAPL", session_date_et=_DATE),
            "ZZZZ": verdict_fixture("ZZZZ", instrument_id=9999,
                                    session_date_et=_DATE),
        }
        candidate = _candidate(
            _leg("AAPL", "buy", "10", limit="100.00"),
            _leg("ZZZZ", "buy", "5", limit=None, instrument_id=9999),
        )
        verdict = engine.can_open(candidate, portfolio_fixture("flat"),
                                  _fresh_account(), **_kwargs(market_state=ms))
        self.assertIn("sector_unknown", verdict.reasons)
        self.assertIn("beta_unknown", verdict.reasons)
        self.assertIn("universe_excluded", verdict.reasons)
        self.assertIn("unpriceable_candidate", verdict.reasons)
        cap_names = [name for name, _, _ in verdict.caps_used]
        self.assertFalse(any(name.startswith("max_sector_exposure_usd")
                             for name in cap_names), cap_names)
        self.assertNotIn("max_abs_beta_notional_usd", cap_names)

    def test_market_state_stage(self):
        engine = _engine()
        verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                  _fresh_account(), **_kwargs(market_state={}))
        self.assertIn("market_state_missing", verdict.reasons)
        for trad in (Tradability.REDUCE_ONLY, Tradability.NOT_TRADABLE):
            ms = {"AAPL": verdict_fixture("AAPL", trad, session_date_et=_DATE)}
            verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                      _fresh_account(), **_kwargs(market_state=ms))
            self.assertIn("market_state_not_tradable", verdict.reasons, trad)
            self.assertNotIn("market_state_stale_default", verdict.reasons, trad)
        # the cache safe default fires BOTH reasons from this stage (R10)
        ms = {"AAPL": verdict_fixture("AAPL", stale_default=True,
                                      session_date_et=_DATE)}
        verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                  _fresh_account(), **_kwargs(market_state=ms))
        stage8 = [r for r in verdict.reasons if r.startswith("market_state")]
        self.assertEqual(stage8, ["market_state_not_tradable",
                                  "market_state_stale_default"])

    def test_market_state_identity_or_date_mismatch_raises(self):
        engine = _engine()
        wrong_instrument = {"AAPL": verdict_fixture("AAPL", instrument_id=9999,
                                                    session_date_et=_DATE)}
        with self.assertRaises(RiskError):
            engine.can_open(_candidate(), portfolio_fixture("flat"), _fresh_account(),
                            **_kwargs(market_state=wrong_instrument))
        # leg verdict date != margin_read.asof (safety-F6): RiskError, never an allow
        skewed_date = {"AAPL": verdict_fixture("AAPL", session_date_et="2026-06-09")}
        with self.assertRaises(RiskError):
            engine.can_open(_candidate(), portfolio_fixture("flat"), _fresh_account(),
                            **_kwargs(market_state=skewed_date))
        # cross-leg session-date disagreement
        ms = {"AAPL": verdict_fixture("AAPL", session_date_et=_DATE),
              "MSFT": verdict_fixture("MSFT", instrument_id=1002,
                                      session_date_et="2026-06-09")}
        with self.assertRaises(RiskError):
            engine.can_open(
                _candidate(_leg(), _leg(symbol="MSFT", instrument_id=1002)),
                portfolio_fixture("flat"), _fresh_account(),
                **_kwargs(market_state=ms))

    def test_margin_stage_boundaries_and_outstanding(self):
        config = permissive_fixture_config()
        config["risk_rules"]["caps"]["max_position_usd"] = 1000000
        config["risk_rules"]["caps"]["max_gross_exposure_usd"] = 1000000
        config["risk_rules"]["caps"]["max_net_exposure_usd"] = 1000000
        config["risk_rules"]["caps"]["max_abs_beta_notional_usd"] = 1000000
        config["risk_rules"]["caps"]["max_sector_exposure_usd"] = 1000000
        engine = _engine(config)
        # buying_power = 200000; Σ == BP passes (buffer 0), +0.01 rejects
        at_bp = _candidate(_leg(qty="1", limit="200000"))
        verdict = engine.can_open(at_bp, portfolio_fixture("flat"), _fresh_account(),
                                  **_kwargs())
        self.assertNotIn("intraday_margin_insufficient", verdict.reasons)
        over_bp = _candidate(_leg(qty="1", limit="200000.01"))
        verdict = engine.can_open(over_bp, portfolio_fixture("flat"), _fresh_account(),
                                  **_kwargs())
        self.assertIn("intraday_margin_insufficient", verdict.reasons)
        self.assertIn(("buying_power", "200000.01", "200000.00"), verdict.caps_used)
        # any non-minor outstanding deficit
        deficit = DeficitRecord(
            deficit_id="imd-x", session_date_et=_DATE, amount=Decimal("1500"),
            minor=False, equity_at_detection=BrokerUSD("17000"), iml_eod_d=None,
            satisfaction_deadline_et="2026-06-15", expires_after_et="2026-06-30",
            satisfied_on_et=None)
        verdict = engine.can_open(
            _candidate(), portfolio_fixture("flat"), _fresh_account(),
            **_kwargs(margin_read=_margin_read(outstanding=(deficit,))))
        self.assertIn("intraday_margin_deficit_outstanding", verdict.reasons)

    def test_pdt_stage(self):
        engine = _engine()
        latched = PdtRead(state="enforcing_legacy_pdt", evidence="broker_rejection",
                          rejection_latched=True)
        verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                  _fresh_account(), **_kwargs(pdt_read=latched))
        self.assertIn("pdt_compat_blocked", verdict.reasons)
        enforcing = PdtRead(state="enforcing_legacy_pdt", evidence="account_flag",
                            rejection_latched=False)
        # Σ 1900.00 == dtbp passes (boundary)
        verdict = engine.can_open(
            _candidate(), portfolio_fixture("flat"),
            _fresh_account(daytrading_buying_power="1900.00"),
            **_kwargs(pdt_read=enforcing))
        self.assertNotIn("pdt_compat_dtbp_exceeded", verdict.reasons)
        self.assertNotIn("pdt_compat_blocked", verdict.reasons)
        verdict = engine.can_open(
            _candidate(), portfolio_fixture("flat"),
            _fresh_account(daytrading_buying_power="1899.99"),
            **_kwargs(pdt_read=enforcing))
        self.assertIn("pdt_compat_dtbp_exceeded", verdict.reasons)
        # unknown blocks nothing (FD-M4-15)
        verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                  _fresh_account(daytrading_buying_power="1"),
                                  **_kwargs())
        self.assertNotIn("pdt_compat_dtbp_exceeded", verdict.reasons)

    def test_loss_stage(self):
        config = permissive_fixture_config()
        engine = _engine(config)
        verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                  _fresh_account(equity="98500.00"),
                                  **_kwargs(loss_read=_loss_read("103000.00")))
        self.assertIn("daily_loss_breached", verdict.reasons)   # 1500 > 1000
        self.assertIn("drawdown_breached", verdict.reasons)     # 4500 > 2000
        self.assertIn(("max_daily_loss_usd", "1500.00", "1000"), verdict.caps_used)
        self.assertIn(("max_drawdown_usd", "4500.00", "2000"), verdict.caps_used)
        verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                  _fresh_account(), **_kwargs(loss_read=_loss_read(None)))
        self.assertIn("loss_baseline_unavailable", verdict.reasons)

    def test_phase2_multi_fault_collects_all_sorted(self):
        engine = _engine(gates_on_fixture_config())   # zero caps + empty universe
        ms = {"AAPL": verdict_fixture("AAPL", Tradability.REDUCE_ONLY,
                                      session_date_et=_DATE)}
        verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                  _fresh_account(), **_kwargs(market_state=ms))
        self.assertEqual(verdict.reasons, (
            "beta_unknown", "gross_exposure_cap_exceeded", "market_state_not_tradable",
            "net_exposure_cap_exceeded", "position_cap_exceeded", "sector_unknown",
            "universe_excluded"))
        self.assertEqual(verdict.reasons, tuple(sorted(verdict.reasons)))
        self.assertIsNone(verdict.gate_stage)

    def test_marks_tighten_notional_with_provenance(self):
        engine = _engine()
        mark = Mark(symbol="AAPL", instrument_id=1001, mid=Decimal("195.000000"),
                    seen_at_ms=10_000, source="quote_mid")
        verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                  _fresh_account(), **_kwargs(marks={"AAPL": mark}))
        self.assertEqual(verdict.gross_notional, Decimal("1950.000000"))
        self.assertIs(verdict.legs[0].mark_used, True)
        mismatched_key = {"MSFT": mark}
        with self.assertRaises(RiskError):
            engine.can_open(_candidate(), portfolio_fixture("flat"), _fresh_account(),
                            **_kwargs(marks=mismatched_key))


class TestInvariants(unittest.TestCase):
    def test_risk_error_on_invariant_breaks(self):
        engine = _engine()
        with self.assertRaises(RiskError):
            engine.can_open(SimpleNamespace(legs=()), portfolio_fixture("flat"),
                            _fresh_account(), **_kwargs())   # non-Candidate
        with self.assertRaises(RiskError):
            engine.can_open(_candidate(), portfolio_fixture("flat"), None,
                            **_kwargs())                     # account None
        with self.assertRaises(RiskError):
            engine.can_open(_candidate(), portfolio_fixture("flat"), _fresh_account(),
                            **_kwargs(kill_state="exploded"))

    def test_vocab_guard_monkeypatched_stage_raises(self):
        engine = _engine()
        with mock.patch.object(RiskEngine, "_stage_universe",
                               return_value={"bogus_reason"}):
            with self.assertRaises(RiskError):
                engine.can_open(_candidate(), portfolio_fixture("flat"),
                                _fresh_account(), **_kwargs())

    def test_determinism_same_inputs_identical_verdict(self):
        verdict_a = _engine(run_id="run-det").can_open(
            _candidate(), portfolio_fixture("flat"), _fresh_account(), **_kwargs())
        verdict_b = _engine(run_id="run-det").can_open(
            _candidate(), portfolio_fixture("flat"), _fresh_account(), **_kwargs())
        self.assertEqual(verdict_a, verdict_b)
        self.assertEqual(verdict_a.verdict_id, verdict_b.verdict_id)
        self.assertTrue(verdict_a.verdict_id.startswith("rv-"))
        verdict_c = _engine(run_id="run-det").can_open(
            _candidate(), portfolio_fixture("flat"), _fresh_account(),
            **_kwargs(decision_id="d-other"))
        self.assertNotEqual(verdict_a.verdict_id, verdict_c.verdict_id)

    def test_allowed_iff_reasons_empty_over_sweep_and_no_reserved(self):
        engine = _engine(gates_on_fixture_config())
        permissive = _engine()
        sweep = [
            (engine, _candidate(), portfolio_fixture("flat"), _fresh_account(),
             _kwargs()),
            (permissive, _candidate(), portfolio_fixture("flat"), _fresh_account(),
             _kwargs()),
            (permissive, _candidate(_leg(limit=None)), portfolio_fixture("long_short"),
             _fresh_account(), _kwargs()),
            (permissive, _candidate(), None, _fresh_account(), _kwargs()),
            (permissive, _candidate(), portfolio_fixture("flat"),
             _degraded_account("stale"), _kwargs()),
        ]
        for eng, candidate, portfolio, account, kwargs in sweep:
            verdict = eng.can_open(candidate, portfolio, account, **kwargs)
            self.assertEqual(verdict.allowed, verdict.reasons == ())
            self.assertEqual(set(verdict.reasons) & RESERVED_REASONS, set())

    def test_caller_journals_verdict_via_ledger(self):
        # FD-M4-5: can_open writes nothing; the CALLER journals via the ledger.
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "risk.jsonl"
            ledger = RiskLedger(EventWriter(path, "run-1", clock=_CLOCK),
                                rules_hash="rh")
            engine = _engine(_committed_config())
            verdict = engine.can_open(_candidate(), portfolio_fixture("flat"),
                                      _fresh_account(), **_kwargs())
            self.assertEqual(replay_risk(path), [])   # can_open journaled NOTHING
            ledger.record_risk_verdict(verdict, decision_id="d-1")
            rows = replay_risk(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["verdict_id"], verdict.verdict_id)
            self.assertEqual(rows[0]["decision_id"], "d-1")

    def test_can_open_never_touches_latest_unsafe_or_locate(self):
        source = (Path(__file__).resolve().parents[2] / "scripts" / "agent" / "risk"
                  / "can_open.py").read_text(encoding="utf-8")
        self.assertNotIn("latest_unsafe", source)   # caller-only annotation surface
        self.assertNotIn("locate", source)          # DenyAllLocate NOT called (FD-M4-1)

    def test_deny_all_locate_stub(self):
        locate = DenyAllLocate()
        self.assertIsInstance(locate, LocateCheck)
        self.assertEqual(locate.locate("AAPL", Decimal("1")),
                         (False, "short_side_disabled"))

    def test_skew_account_is_data_not_exception(self):
        # R12: clock regression -> account_clock_skew verdict, never a raise.
        verdict = _engine().can_open(_candidate(), portfolio_fixture("flat"),
                                     _degraded_account("skew"), **_kwargs())
        self.assertEqual(verdict.reasons, ("account_clock_skew",))
        self.assertIsInstance(verdict, RiskVerdict)
        self.assertIsInstance(verdict.legs, tuple)
        self.assertEqual(verdict.legs, ())

    def test_leg_read_fields_frozen(self):
        self.assertEqual(
            set(LegRead.__dataclass_fields__),
            {"symbol", "instrument_id", "side", "classification", "qty",
             "limit_price", "cap_notional", "mark_used"})


if __name__ == "__main__":
    unittest.main()
