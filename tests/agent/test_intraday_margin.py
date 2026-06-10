"""M4 §M test 4 — the CANONICAL FINRA 26-10 model: one test family per §E S10 row.

Invariants: S10 (IML-reducing / deficit calc / bd15 / bd5+90cd freeze / minor),
S3 + R9 (rehydrate byte-exact), R3 (broker ground truth, no re-derivation constants).
"""
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from agent.market_calendar import UnknownSessionDate
from agent.risk.intraday_margin import (
    FREEZE_CALENDAR_DAYS,
    MINOR_DEFICIT_EQUITY_PCT,
    MINOR_DEFICIT_USD,
    OUTSTANDING_WINDOW_BD,
    SATISFACTION_DEADLINE_BD,
    DeficitRecord,
    FreezeState,
    IntradayMarginModel,
    MarginRead,
    ScanLimitExceeded,
    add_business_days,
    classify_iml_reducing,
    minor_deficit,
    observation_from_read,
)
from agent.risk.reasons import RiskError
from agent.risk.risk_ledger import (
    EVT_DEFICIT_EXPIRED,
    EVT_DEFICIT_SATISFIED,
    EVT_DEFICIT_UNBASELINED,
    EVT_FREEZE_END,
    EVT_FREEZE_START,
    EVT_MARGIN_DEFICIT,
    EVT_MARGIN_WINDOW_UNRESOLVED,
    RiskLedger,
    replay_risk,
)
from agent.serializer import row_hash
from recorder.persistence import EventWriter
from tests.lib.risk_fixtures import (
    deficit_boundary_cases,
    freeze_timeline,
    margin_calendar_provider,
    margin_day,
)

_CLOCK = lambda: "2026-06-08T20:00:00.000000+00:00"  # noqa: E731
_TL = freeze_timeline()


def _model(tmpdir=None, calendar=None):
    calendar = calendar or margin_calendar_provider()
    if tmpdir is None:
        return IntradayMarginModel(calendar=calendar, ledger=None), None
    path = Path(tmpdir) / "risk.jsonl"
    ledger = RiskLedger(EventWriter(path, "run-1", clock=_CLOCK), rules_hash="rh")
    return IntradayMarginModel(calendar=calendar, ledger=ledger), path


def _deficient_obs(date, *, equity="17000", maintenance="18500", after=True, eod=False):
    return margin_day([(equity, maintenance, after, eod)], session_date_et=date)[0]


def _eod(date, *, equity="17000", maintenance="18500"):
    return margin_day([(equity, maintenance, False, True)], session_date_et=date)[0]


def _eod_clean(date):
    return _eod(date, equity="100000", maintenance="30000")


def _run_closes(model, dates, *, eod_builder=_eod):
    for date in dates:
        model.close_of_day(date, eod_builder(date))


class TestClassifyImlReducing(unittest.TestCase):
    def test_total_function_matrix(self):
        D = Decimal
        cases = [
            ("buy", D("10"), D("0"), True),     # purchase, not covering
            ("buy", D("10"), D("5"), True),     # add to long
            ("buy", D("100"), D("-100"), False),  # cover
            ("buy", D("150"), D("-100"), True),   # flip remainder increases exposure
            ("sell", D("10"), D("10"), False),    # close
            ("sell", D("5"), D("10"), False),     # partial close
            ("sell", D("11"), D("10"), True),     # over-close -> short establish
            ("sell", D("1"), D("0"), True),       # short establish vs flat
            ("sell", D("1"), D("-5"), True),      # short increase
        ]
        for side, qty, held, expected in cases:
            self.assertIs(classify_iml_reducing(side, qty, held), expected,
                          (side, qty, held))

    def test_out_of_vocab_side_raises(self):
        with self.assertRaises(RiskError):
            classify_iml_reducing("hold", Decimal("1"), Decimal("0"))

    def test_scripted_fill_sequence_with_held_bookkeeping(self):
        held = Decimal("0")
        # deposit: no transaction -> no observation point (nothing to classify)
        self.assertIs(classify_iml_reducing("buy", Decimal("100"), held), True)  # open-buy
        held += Decimal("100")
        self.assertIs(classify_iml_reducing("sell", Decimal("100"), held), False)  # close
        held -= Decimal("100")
        self.assertIs(classify_iml_reducing("sell", Decimal("100"), held), True)  # short
        held -= Decimal("100")
        self.assertIs(classify_iml_reducing("buy", Decimal("100"), held), False)  # cover
        self.assertIs(classify_iml_reducing("buy", Decimal("150"), held), True)   # flip


class TestDeficitCalculation(unittest.TestCase):
    def test_equality_boundary_no_deficit(self):
        model, _ = _model()
        obs = _deficient_obs("2026-06-08", equity="18000", maintenance="18000")
        self.assertIsNone(model.observe(obs))
        self.assertEqual(model.outstanding("2026-06-08"), ())

    def test_one_cent_under_is_a_deficit(self):
        model, _ = _model()
        obs = _deficient_obs("2026-06-08", equity="17999.99", maintenance="18000")
        record = model.observe(obs)
        self.assertEqual(record.amount, Decimal("0.01"))
        self.assertEqual(str(record.amount), "0.01")  # exact

    def test_max_merge_100_250_180_yields_250_with_two_rows(self):
        with TemporaryDirectory() as tmpdir:
            model, path = _model(tmpdir)
            for maint in ("18100", "18250", "18180"):
                model.observe(_deficient_obs("2026-06-08", equity="18000",
                                             maintenance=maint))
            records = model.outstanding("2026-06-08")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].amount, Decimal("250"))
            rows = [r for r in replay_risk(path) if r["event_type"] == EVT_MARGIN_DEFICIT]
            self.assertEqual([r["cause"] for r in rows], ["opened", "increased"])
            self.assertEqual(rows[0]["amount"], "100")
            self.assertEqual(rows[1]["amount"], "250")
            self.assertEqual(rows[0]["deficit_id"], rows[1]["deficit_id"])

    def test_dip_before_any_iml_reducing_observation_is_no_record(self):
        model, _ = _model()
        obs = _deficient_obs("2026-06-08", after=False)  # deficient but not post-IML-reducing
        self.assertIsNone(model.observe(obs))
        self.assertEqual(model.outstanding("2026-06-08"), ())

    def test_single_eod_run_equals_continuous_when_eod_is_worst(self):
        continuous, _ = _model()
        continuous.observe(_deficient_obs("2026-06-08", maintenance="18100",
                                          equity="18000"))
        continuous.close_of_day("2026-06-08",
                                _deficient_obs("2026-06-08", maintenance="18250",
                                               equity="18000", eod=True))
        single, _ = _model()
        single.close_of_day("2026-06-08",
                            _deficient_obs("2026-06-08", maintenance="18250",
                                           equity="18000", eod=True))
        amount_a = continuous.outstanding("2026-06-08")[0].amount
        amount_b = single.outstanding("2026-06-08")[0].amount
        self.assertEqual(amount_a, amount_b)
        self.assertEqual(amount_a, Decimal("250"))

    def test_broker_ground_truth_flips_detection(self):
        # R3: mutating ONLY equity/maintenance flips the outcome.
        model, _ = _model()
        self.assertIsNone(model.observe(
            _deficient_obs("2026-06-08", equity="18000", maintenance="18000")))
        model2, _ = _model()
        self.assertIsNotNone(model2.observe(
            _deficient_obs("2026-06-08", equity="18000", maintenance="18000.01")))

    def test_deficit_id_is_run_independent_date_hash(self):
        model, _ = _model()
        record = model.observe(_deficient_obs("2026-06-08"))
        self.assertEqual(record.deficit_id,
                         "imd-" + row_hash({"session_date_et": "2026-06-08"}))

    def test_detection_after_own_eod_raises(self):
        model, _ = _model()
        model.close_of_day("2026-06-08", _eod_clean("2026-06-08"))
        with self.assertRaises(RiskError):
            model.observe(_deficient_obs("2026-06-08"))

    def test_observe_rejects_eod_observation_and_close_rerun_raises(self):
        model, _ = _model()
        with self.assertRaises(RiskError):
            model.observe(_eod_clean("2026-06-08"))
        model.close_of_day("2026-06-08", _eod_clean("2026-06-08"))
        with self.assertRaises(RiskError):
            model.close_of_day("2026-06-08", _eod_clean("2026-06-08"))


class TestBusinessDays(unittest.TestCase):
    def test_bd5_and_bd15_over_the_new_calendar(self):
        calendar = margin_calendar_provider()
        self.assertEqual(add_business_days("2026-06-08", SATISFACTION_DEADLINE_BD,
                                           calendar=calendar), _TL["bd5"])
        # the 06-19 Juneteenth holiday shifts bd15 from 06-29 to 06-30
        self.assertEqual(add_business_days("2026-06-08", OUTSTANDING_WINDOW_BD,
                                           calendar=calendar), _TL["bd15"])

    def test_weekend_and_holiday_skips(self):
        calendar = margin_calendar_provider()
        self.assertEqual(add_business_days("2026-06-12", 1, calendar=calendar),
                         "2026-06-15")  # over a weekend
        self.assertEqual(add_business_days("2026-06-18", 1, calendar=calendar),
                         "2026-06-22")  # over the holiday + weekend

    def test_coverage_exhaustion_raises_unknown_session_date(self):
        calendar = margin_calendar_provider()
        with self.assertRaises(UnknownSessionDate):
            add_business_days("2026-07-30", 10, calendar=calendar)

    def test_scan_limit_raises(self):
        never_open = SimpleNamespace(
            is_trading_day=lambda date: False,
            schedule_for=lambda date: None,
            calendar_pin=lambda: "stub")
        with self.assertRaises(ScanLimitExceeded):
            add_business_days("2026-06-08", 1, calendar=never_open)

    def test_regulatory_constants_pinned(self):
        self.assertEqual(SATISFACTION_DEADLINE_BD, 5)
        self.assertEqual(OUTSTANDING_WINDOW_BD, 15)
        self.assertEqual(FREEZE_CALENDAR_DAYS, 90)
        self.assertEqual(MINOR_DEFICIT_USD, Decimal("1000"))
        self.assertEqual(MINOR_DEFICIT_EQUITY_PCT, Decimal("0.05"))


class TestOutstandingWindow(unittest.TestCase):
    def test_bd15_outstanding_then_expired(self):
        with TemporaryDirectory() as tmpdir:
            model, path = _model(tmpdir)
            record = model.observe(_deficient_obs(_TL["deficit_date"]))
            self.assertEqual(record.expires_after_et, _TL["bd15"])
            # outstanding right through the bd15 date
            self.assertEqual(len(model.outstanding(_TL["bd15"])), 1)
            self.assertEqual(len(model.read(_TL["bd15"]).outstanding_nonminor), 1)
            # expire at the bd15 close ("immediately after the close")
            model.close_of_day(_TL["deficit_date"], _eod(_TL["deficit_date"]))
            model.close_of_day(_TL["bd15"], _eod(_TL["bd15"]))
            rows = [r for r in replay_risk(path) if r["event_type"] == EVT_DEFICIT_EXPIRED]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["expires_after_et"], _TL["bd15"])
            self.assertEqual(model.outstanding("2026-07-01"), ())

    def test_satisfaction_on_bd3_clears_earlier(self):
        with TemporaryDirectory() as tmpdir:
            model, path = _model(tmpdir)
            model.observe(_deficient_obs(_TL["deficit_date"]))   # amount 1500, IML -1500
            model.close_of_day(_TL["deficit_date"], _eod(_TL["deficit_date"]))
            _run_closes(model, [_TL["bd1"], _TL["bd2"]])
            self.assertEqual(len(model.outstanding(_TL["bd2"])), 1)
            # bd3 close: IML 0 -> delta exactly 1500 >= amount 1500 (boundary qualifies)
            model.close_of_day(_TL["bd3"], _eod(_TL["bd3"], equity="18500"))
            self.assertEqual(model.outstanding(_TL["bd3"]), ())
            rows = [r for r in replay_risk(path)
                    if r["event_type"] == EVT_DEFICIT_SATISFIED]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["satisfied_on_et"], _TL["bd3"])
            self.assertEqual(rows[0]["iml_eod_d"], "-1500")
            self.assertEqual(rows[0]["iml_eod_e"], "0")
            self.assertEqual(rows[0]["basis"], "eod_iml_delta")

    def test_same_day_satisfaction_never_evaluated(self):
        model, _ = _model()
        model.observe(_deficient_obs(_TL["deficit_date"]))
        # an EOD on D itself with a huge IML must NOT satisfy ("subsequent day" only)
        model.close_of_day(_TL["deficit_date"],
                           _eod(_TL["deficit_date"], equity="100000",
                                maintenance="30000"))
        self.assertEqual(len(model.outstanding(_TL["deficit_date"])), 1)
        record = model.outstanding(_TL["deficit_date"])[0]
        self.assertIsNone(record.satisfied_on_et)


class TestFreezeTrigger(unittest.TestCase):
    def _seed_deficit(self, model, date=None):
        date = date or _TL["deficit_date"]
        model.observe(_deficient_obs(date))
        model.close_of_day(date, _eod(date))

    def test_satisfied_bd4_never_frozen(self):
        model, _ = _model()
        self._seed_deficit(model)
        _run_closes(model, [_TL["bd1"], _TL["bd2"], _TL["bd3"]])
        model.close_of_day(_TL["bd4"], _eod(_TL["bd4"], equity="18500"))  # satisfies
        model.close_of_day(_TL["bd5"], _eod(_TL["bd5"]))
        self.assertIs(model.freeze_state().active, False)

    def test_bd5_eod_delta_qualifying_is_not_frozen(self):
        # RM-9: satisfaction is only determinable AT the close — step 1 runs before
        # the freeze trigger inside the same close.
        model, _ = _model()
        self._seed_deficit(model)
        _run_closes(model, [_TL["bd1"], _TL["bd2"], _TL["bd3"], _TL["bd4"]])
        model.close_of_day(_TL["bd5"], _eod(_TL["bd5"], equity="18500"))
        self.assertIs(model.freeze_state().active, False)

    def test_unsatisfied_at_bd5_close_freezes_with_exact_dates(self):
        with TemporaryDirectory() as tmpdir:
            model, path = _model(tmpdir)
            self._seed_deficit(model)
            _run_closes(model, [_TL["bd1"], _TL["bd2"], _TL["bd3"], _TL["bd4"],
                                _TL["bd5"]])
            freeze = model.freeze_state()
            self.assertIs(freeze.active, True)
            self.assertEqual(freeze.effective_from_et, _TL["effective_from"])
            self.assertEqual(freeze.expires_on_et, _TL["expires_on"])
            rows = [r for r in replay_risk(path) if r["event_type"] == EVT_FREEZE_START]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["effective_from_et"], _TL["effective_from"])

    def test_skipped_bd5_close_catch_up_keeps_deadline_anchor(self):
        # LD-R6/safety-F2: close_of_day first run on bd6 -> STILL frozen,
        # effective_from anchored to the bd5 deadline.
        model, _ = _model()
        self._seed_deficit(model)
        _run_closes(model, [_TL["bd1"], _TL["bd2"], _TL["bd3"], _TL["bd4"]])
        model.close_of_day(_TL["bd6"], _eod(_TL["bd6"]))  # bd5 close was skipped
        freeze = model.freeze_state()
        self.assertIs(freeze.active, True)
        self.assertEqual(freeze.effective_from_et, _TL["effective_from"])
        self.assertEqual(freeze.expires_on_et, _TL["expires_on"])

    def test_minor_deficit_at_bd5_freezes_too(self):
        # LD-R2: V9's exception scopes only the practice prong, not the bd5 prong.
        with TemporaryDirectory() as tmpdir:
            model, path = _model(tmpdir)
            model.observe(_deficient_obs(_TL["deficit_date"], equity="17750",
                                         maintenance="18000"))  # amount 250 -> minor
            model.close_of_day(_TL["deficit_date"],
                               _eod(_TL["deficit_date"], equity="17750",
                                    maintenance="18000"))
            for date in (_TL["bd1"], _TL["bd2"], _TL["bd3"], _TL["bd4"], _TL["bd5"]):
                model.close_of_day(date, _eod(date, equity="17750",
                                              maintenance="18000"))
            self.assertIs(model.freeze_state().active, True)
            # ... while still absent from outstanding_nonminor (rung 11 stays
            # non-minor-scoped)
            self.assertEqual(model.read(_TL["bd5"]).outstanding_nonminor, ())
            self.assertEqual(len(model.outstanding(_TL["bd5"])), 1)
            self.assertEqual(model.practice_count(), 0)  # minor never enters (RM-10)

    def test_day_90_91_boundary(self):
        freeze = FreezeState(active=True, trigger_deficit_id="imd-x",
                             effective_from_et=_TL["effective_from"],
                             expires_on_et=_TL["expires_on"])
        self.assertIs(freeze.active_on("2026-06-16"), True)   # first frozen day
        self.assertIs(freeze.active_on("2026-09-13"), True)   # calendar day 90
        self.assertIs(freeze.active_on("2026-09-14"), False)  # day 91 = expires_on
        self.assertIs(freeze.active_on("2026-06-15"), False)  # before effective_from

    def test_second_trigger_max_merges_end_keeps_original_anchor(self):
        with TemporaryDirectory() as tmpdir:
            model, path = _model(tmpdir)
            self._seed_deficit(model)                       # D1 = 06-08, deadline 06-15
            model.observe(_deficient_obs("2026-06-10"))     # D2, deadline 06-17
            for date in (_TL["bd1"], "2026-06-10", _TL["bd3"], _TL["bd4"], _TL["bd5"],
                         _TL["bd6"], "2026-06-17"):
                model.close_of_day(date, _eod(date))
            freeze = model.freeze_state()
            self.assertIs(freeze.active, True)
            d1_id = "imd-" + row_hash({"session_date_et": "2026-06-08"})
            self.assertEqual(freeze.trigger_deficit_id, d1_id)            # original kept
            self.assertEqual(freeze.effective_from_et, _TL["effective_from"])
            self.assertEqual(freeze.expires_on_et, "2026-09-16")          # extended end
            rows = [r for r in replay_risk(path) if r["event_type"] == EVT_FREEZE_START]
            self.assertEqual(len(rows), 2)                                # F13 merge row
            self.assertEqual(rows[1]["trigger_deficit_id"], d1_id)
            self.assertEqual(rows[1]["effective_from_et"], _TL["effective_from"])
            self.assertEqual(rows[1]["expires_on_et"], "2026-09-16")
            self.assertEqual(model.practice_count(), 2)  # two non-minor missed bd5 closes

    def test_freeze_end_emitted_by_first_close_past_expiry(self):
        with TemporaryDirectory() as tmpdir:
            model, path = _model(tmpdir)
            self._seed_deficit(model)
            _run_closes(model, [_TL["bd1"], _TL["bd2"], _TL["bd3"], _TL["bd4"],
                                _TL["bd5"]])
            model.close_of_day(_TL["expires_on"], _eod(_TL["expires_on"]))
            model.close_of_day("2026-09-15", _eod("2026-09-15"))
            rows = [r for r in replay_risk(path) if r["event_type"] == EVT_FREEZE_END]
            self.assertEqual(len(rows), 1)  # once
            self.assertEqual(rows[0]["expires_on_et"], _TL["expires_on"])
            # active_on stays the authority: already inactive on expires_on
            self.assertIs(model.freeze_state().active_on(_TL["expires_on"]), False)

    def test_reduce_path_independent_while_frozen(self):
        # FD-M4-3 paired proof: a reduce mint still succeeds under an active freeze.
        from agent.broker.base import OrderIntent
        from agent.execution_preflight import mint_reduce_only_token

        model, _ = _model()
        self._seed_deficit(model)
        _run_closes(model, [_TL["bd1"], _TL["bd2"], _TL["bd3"], _TL["bd4"], _TL["bd5"]])
        self.assertIs(model.freeze_state().active_on(_TL["bd6"]), True)
        held = SimpleNamespace(symbol="AAPL", qty=Decimal("10"))
        intent = OrderIntent(symbol="AAPL", side="sell", qty=Decimal("10"),
                             is_reducing=True, intent_id="r-freeze")
        token = mint_reduce_only_token(held, intent)
        self.assertIsNotNone(token)


class TestMinorClassification(unittest.TestCase):
    def test_boundary_triples(self):
        for equity, amount, expected in deficit_boundary_cases():
            self.assertIs(minor_deficit(Decimal(amount), Decimal(equity)), expected,
                          (equity, amount))

    def test_invalid_equity_is_not_minor(self):
        self.assertIs(minor_deficit(Decimal("1"), None), False)
        self.assertIs(minor_deficit(Decimal("1"), Decimal("NaN")), False)

    def test_increase_flips_minor_to_non_minor(self):
        model, _ = _model()
        record = model.observe(_deficient_obs("2026-06-08", equity="18000",
                                              maintenance="18900"))  # 900 -> minor
        self.assertIs(record.minor, True)
        record = model.observe(_deficient_obs("2026-06-08", equity="18000",
                                              maintenance="19500"))  # 1500 -> not minor
        self.assertIs(record.minor, False)
        self.assertEqual(len(model.read("2026-06-08").outstanding_nonminor), 1)

    def test_minor_latch_is_one_way(self):
        # safety-F3: non-minor at low equity stays non-minor after a high-equity
        # increase, and still freezes at bd5.
        model, _ = _model()
        record = model.observe(_deficient_obs("2026-06-08", equity="17000",
                                              maintenance="18500"))  # 1500 -> not minor
        self.assertIs(record.minor, False)
        # increase observed at much higher equity: 1600 <= min(100000*0.05, 1000)?
        # No — but use an amount under BOTH minor bounds to prove the latch holds:
        record = model.observe(_deficient_obs("2026-06-08", equity="100000",
                                              maintenance="101600"))  # amount 1600
        self.assertIs(record.minor, False)
        model.close_of_day("2026-06-08", _eod("2026-06-08"))
        _run_closes(model, [_TL["bd1"], _TL["bd2"], _TL["bd3"], _TL["bd4"], _TL["bd5"]])
        self.assertIs(model.freeze_state().active, True)


class TestUnbaselinedAndWindowFailure(unittest.TestCase):
    def test_crash_before_eod_is_unsatisfiable_but_still_expires_and_freezes(self):
        # M4C-2/RM-6: close_of_day(D) never ran -> iml_eod_d None -> the satisfaction
        # scan SKIPS it (DATA, never a raise); one unbaselined marker row; the record
        # still freezes at bd5 and still expires at bd15.
        with TemporaryDirectory() as tmpdir:
            model, path = _model(tmpdir)
            model.observe(_deficient_obs(_TL["deficit_date"]))
            # crash: no close_of_day(D). Resume at bd1 with a HUGE IML — must not satisfy.
            _run_closes(model, [_TL["bd1"], _TL["bd2"], _TL["bd3"], _TL["bd4"],
                                _TL["bd5"]],
                        eod_builder=lambda d: _eod(d, equity="100000",
                                                   maintenance="30000"))
            record = model.outstanding(_TL["bd5"])[0]
            self.assertIsNone(record.iml_eod_d)
            self.assertIsNone(record.satisfied_on_et)
            self.assertIs(model.freeze_state().active, True)   # still freezes at bd5
            rows = [r for r in replay_risk(path)
                    if r["event_type"] == EVT_DEFICIT_UNBASELINED]
            self.assertEqual(len(rows), 1)                     # exactly once
            self.assertEqual(rows[0]["noted_at_close_et"], _TL["bd1"])
            # still expires at bd15
            for date in ("2026-06-17", "2026-06-18", "2026-06-22", "2026-06-23",
                         "2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29",
                         _TL["bd15"]):
                model.close_of_day(date, _eod(date, equity="100000",
                                              maintenance="30000"))
            self.assertEqual(model.outstanding("2026-07-01"), ())

    def test_window_failure_at_detection_journals_detected_first(self):
        # safety-F11: detection row FIRST, then margin_window_unresolved; the record is
        # outstanding-NON-MINOR (even when its minor flag computed True) with null
        # windows; the exception propagates.
        with TemporaryDirectory() as tmpdir:
            model, path = _model(tmpdir)
            with self.assertRaises(UnknownSessionDate):
                model.observe(_deficient_obs("2026-07-31", equity="17750",
                                             maintenance="18000"))  # minor-sized
            rows = replay_risk(path)
            events = [r["event_type"] for r in rows]
            self.assertIn(EVT_MARGIN_DEFICIT, events)
            self.assertIn(EVT_MARGIN_WINDOW_UNRESOLVED, events)
            self.assertLess(events.index(EVT_MARGIN_DEFICIT),
                            events.index(EVT_MARGIN_WINDOW_UNRESOLVED))
            detected = rows[events.index(EVT_MARGIN_DEFICIT)]
            self.assertIsNone(detected["satisfaction_deadline_et"])
            self.assertIsNone(detected["expires_after_et"])
            unresolved = rows[events.index(EVT_MARGIN_WINDOW_UNRESOLVED)]
            self.assertEqual(unresolved["error"], "unknown_session_date")
            # outstanding-NON-MINOR treatment overrides the minor flag (safety-F11)
            read = model.read("2026-07-31")
            self.assertEqual(len(read.outstanding_nonminor), 1)
            self.assertIsNone(read.outstanding_nonminor[0].satisfaction_deadline_et)

    def test_later_successful_recomputation_resolves_windows(self):
        class FlakyCalendar:
            def __init__(self, inner):
                self._inner = inner
                self.broken = True

            def is_trading_day(self, date):
                if self.broken:
                    raise UnknownSessionDate(date)
                return self._inner.is_trading_day(date)

            def schedule_for(self, date):
                return self._inner.schedule_for(date)

            def calendar_pin(self):
                return self._inner.calendar_pin()

        with TemporaryDirectory() as tmpdir:
            calendar = FlakyCalendar(margin_calendar_provider())
            model, path = _model(tmpdir, calendar=calendar)
            with self.assertRaises(UnknownSessionDate):
                model.observe(_deficient_obs("2026-06-08"))
            calendar.broken = False
            record = model.observe(_deficient_obs("2026-06-08", maintenance="19000"))
            self.assertEqual(record.satisfaction_deadline_et, _TL["bd5"])
            self.assertEqual(record.expires_after_et, _TL["bd15"])
            unresolved_rows = [r for r in replay_risk(path)
                               if r["event_type"] == EVT_MARGIN_WINDOW_UNRESOLVED]
            self.assertEqual(len(unresolved_rows), 1)  # once per record


class _GrowableWeekdayCalendar:
    """Weekday-session stub whose coverage horizon can be extended mid-test —
    drives the harden-round-1 window-heal / compute-then-latch scenarios."""

    def __init__(self, covered_until: str):
        self._until = covered_until

    def extend(self, until: str) -> None:
        self._until = until

    def is_trading_day(self, session_date_et: str) -> bool:
        if session_date_et > self._until:
            raise UnknownSessionDate(session_date_et)
        from datetime import date as _d
        return _d.fromisoformat(session_date_et).weekday() < 5

    def schedule_for(self, session_date_et: str):  # pragma: no cover - unused
        raise NotImplementedError

    def calendar_pin(self) -> str:
        return "stub:weekday-growable"


def _weekdays(start: str, end: str):
    from datetime import date as _d, timedelta as _td
    current = _d.fromisoformat(start)
    stop = _d.fromisoformat(end)
    while current <= stop:
        if current.weekday() < 5:
            yield current.isoformat()
        current += _td(days=1)


class TestHardenRound1(unittest.TestCase):
    """Repro-gated fixes from the M4 adversarial review (harden round 1)."""

    def test_bd5_resolves_independently_and_freeze_engages(self):
        # M4-R1 (major): on the committed fixture (coverage ends 2026-07-31) a
        # 2026-07-20 deficit gets bd15 FAILURE but bd5 SUCCESS (07-27) at
        # detection; unsatisfied at the true bd5 close => the 90-day freeze MUST
        # engage [2026-07-28, 2026-10-26) and later satisfaction must NOT lift it.
        with TemporaryDirectory() as tmpdir:
            model, path = _model(tmpdir)
            with self.assertRaises(UnknownSessionDate):
                model.observe(_deficient_obs("2026-07-20"))
            for date in ("2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23",
                         "2026-07-24", "2026-07-27"):
                model.close_of_day(date, _eod(date))
            freeze = model.freeze_state()
            self.assertTrue(freeze.active)
            self.assertEqual(freeze.effective_from_et, "2026-07-28")
            self.assertEqual(freeze.expires_on_et, "2026-10-26")
            self.assertTrue(freeze.active_on("2026-07-30"))
            # satisfied AFTER the deadline: no early lift (FD-M4-11)
            model.close_of_day("2026-07-28", _eod_clean("2026-07-28"))
            self.assertTrue(model.freeze_state().active_on("2026-07-30"))
            self.assertEqual(model.practice_count(), 1)
            # M4-R2: practice_count + freeze survive rehydrate identically
            rehydrated = IntradayMarginModel(
                calendar=margin_calendar_provider(), ledger=None)
            rehydrated.rehydrate(replay_risk(path))
            self.assertEqual(rehydrated.freeze_state(), model.freeze_state())
            self.assertEqual(rehydrated.practice_count(), 1)

    def test_close_time_heal_and_compute_then_latch(self):
        # M4-EDGE-1 (minor): the trigger-time effective_from computation fails =>
        # flags stay UNLATCHED (compute-then-latch), the close raises fail-loud,
        # and the NEXT close (after coverage extends) installs the freeze.
        # Also exercises the close-time window self-heal (M4-R1).
        calendar = _GrowableWeekdayCalendar("2026-06-15")
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "risk.jsonl"
            ledger = RiskLedger(EventWriter(path, "run-1", clock=_CLOCK),
                                rules_hash="rh")
            model = IntradayMarginModel(calendar=calendar, ledger=ledger)
            with self.assertRaises(UnknownSessionDate):
                model.observe(_deficient_obs("2026-06-08"))  # bd15 fails, bd5=06-15 OK
            for date in ("2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11",
                         "2026-06-12"):
                model.close_of_day(date, _eod(date))
            # the deadline close: effective_from = bd(06-15)+1 = 06-16 > coverage
            with self.assertRaises(UnknownSessionDate):
                model.close_of_day("2026-06-15", _eod("2026-06-15"))
            self.assertFalse(model.freeze_state().active)   # nothing latched
            calendar.extend("2026-12-31")
            # catch-up close AFTER the deadline: the trigger retries and the
            # freeze anchors to the DEADLINE (LD-R6), not to E.
            model.close_of_day("2026-06-16", _eod("2026-06-16"))
            freeze = model.freeze_state()
            self.assertTrue(freeze.active)
            self.assertEqual(freeze.effective_from_et, "2026-06-16")
            # the close-time heal journaled the resolved windows for rehydrate
            resolved = [r for r in replay_risk(path)
                        if r["event_type"] == EVT_MARGIN_DEFICIT
                        and r["cause"] == "window_resolved"]
            self.assertGreaterEqual(len(resolved), 1)
            self.assertIsNotNone(resolved[-1]["expires_after_et"])

    def test_contiguous_second_trigger_merges_never_lifts(self):
        # M4-R1-F1 (major): a second trigger whose effective_from lands exactly ON
        # the active freeze's expiry must MERGE (original anchor kept, end
        # extended) — active_on may never flip True->False inside the window.
        calendar = _GrowableWeekdayCalendar("2027-06-30")
        model = IntradayMarginModel(calendar=calendar, ledger=None)
        model.observe(_deficient_obs("2026-06-08"))
        for date in _weekdays("2026-06-08", "2026-09-03"):
            model.close_of_day(date, _eod(date))
        first = model.freeze_state()
        self.assertTrue(first.active)
        self.assertEqual(first.effective_from_et, "2026-06-16")
        self.assertEqual(first.expires_on_et, "2026-09-14")
        original_id = first.trigger_deficit_id
        # D2: bd5 deadline 2026-09-11; effective_from2 = 2026-09-14 == expires_on1
        model.observe(_deficient_obs("2026-09-04"))
        for date in _weekdays("2026-09-04", "2026-09-11"):
            model.close_of_day(date, _eod(date))
        merged = model.freeze_state()
        self.assertTrue(merged.active_on("2026-09-11"))           # never loosened
        self.assertEqual(merged.trigger_deficit_id, original_id)  # anchor kept
        self.assertEqual(merged.effective_from_et, "2026-06-16")
        self.assertEqual(merged.expires_on_et, "2026-12-13")      # extended end


class TestSourceScan(unittest.TestCase):
    def test_no_rederivation_constants_under_risk(self):
        # R3/FD-M4-2: no $25k, no maintenance-margin percentage table.
        risk_dir = Path(__file__).resolve().parents[2] / "scripts" / "agent" / "risk"
        for source_file in sorted(risk_dir.glob("*.py")):
            text = source_file.read_text(encoding="utf-8")
            self.assertNotIn("25000", text, source_file.name)
            self.assertNotIn("25_000", text, source_file.name)
            for pct in ('"0.25"', '"0.30"', '"0.3"'):
                self.assertNotIn(pct, text, source_file.name)


class TestRehydrate(unittest.TestCase):
    def test_rehydrated_state_equals_live_state(self):
        # R9/S3/LD-R5: replayed risk.jsonl reproduces outstanding/freeze state
        # BYTE-identical to the live model, incl. the no-eod-row -> iml_eod_d=None join.
        with TemporaryDirectory() as tmpdir:
            model, path = _model(tmpdir)
            model.observe(_deficient_obs(_TL["deficit_date"],
                                         equity="17999.995", maintenance="18500"))
            model.close_of_day(_TL["deficit_date"],
                               _eod(_TL["deficit_date"], equity="17999.995",
                                    maintenance="18500"))
            model.observe(_deficient_obs("2026-06-10"))   # NO close_of_day -> unbaselined
            # bd2 == 2026-06-10 is deliberately SKIPPED (crash on D2's own date)
            _run_closes(model, [_TL["bd1"], _TL["bd3"], _TL["bd4"], _TL["bd5"]])
            rows = replay_risk(path)
            rehydrated = IntradayMarginModel(calendar=margin_calendar_provider(),
                                             ledger=None)
            rehydrated.rehydrate(rows)
            for asof in (_TL["bd1"], _TL["bd5"], _TL["bd15"], "2026-07-01"):
                live = model.read(asof)
                replayed = rehydrated.read(asof)
                self.assertEqual(live, replayed, asof)
                for a, b in zip(live.outstanding_nonminor, replayed.outstanding_nonminor):
                    self.assertEqual(str(a.amount), str(b.amount))         # byte-exact
                    self.assertEqual(str(a.equity_at_detection),
                                     str(b.equity_at_detection))
            self.assertEqual(model.freeze_state(), rehydrated.freeze_state())
            d2 = [r for r in rehydrated.outstanding(_TL["bd5"])
                  if r.session_date_et == "2026-06-10"][0]
            self.assertIsNone(d2.iml_eod_d)  # the M4C-2 join
            d1 = [r for r in rehydrated.outstanding(_TL["bd1"])
                  if r.session_date_et == _TL["deficit_date"]][0]
            self.assertEqual(str(d1.iml_eod_d), "-500.005")  # exact unquantized


if __name__ == "__main__":
    unittest.main()
