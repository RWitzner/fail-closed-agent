"""M4 §M test 9 — journal/risk.jsonl: row shapes, vocab validation, money discipline
(LD-R5), replay/rehydrate fold.

Invariants: S2, S3, S6, R4, R9.
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from agent.journal import JournalCorruption
from agent.risk.reasons import RiskError
from agent.risk.risk_ledger import (
    EVT_ACCOUNT_SNAPSHOT,
    EVT_FREEZE_START,
    EVT_HWM_UPDATE,
    EVT_IML_OBSERVATION,
    EVT_KILL_TRANSITION,
    EVT_MARGIN_DEFICIT,
    EVT_PDT_TRANSITION,
    EVT_RISK_VERDICT,
    RISK_LEDGER_VERSION,
    STREAM_RISK,
    RiskLedger,
    rehydrate_risk_state,
    replay_risk,
)
from agent.serializer import BrokerUSD
from recorder.persistence import EventWriter
from tests.lib.risk_fixtures import account_payload

from agent.risk.account_state import parse_account_payload

_CLOCK = lambda: "2026-06-08T20:00:00.000000+00:00"  # noqa: E731 — fixed for byte-determinism


def _ledger(tmpdir, run_id="run-1"):
    path = Path(tmpdir) / "risk.jsonl"
    writer = EventWriter(path, run_id, clock=_CLOCK)
    return RiskLedger(writer, rules_hash="rh-test"), path


def _verdict(**overrides):
    base = dict(
        allowed=False,
        reasons=("run_gates_off",),
        gate_stage="run_gates",
        stages_skipped=("kill", "margin_freeze", "account", "portfolio", "candidate",
                        "universe", "market_state", "short", "caps", "margin", "pdt",
                        "loss"),
        strategy_id="s1",
        legs=(),
        gross_notional=Decimal("0"),
        caps_used=(),
        account_snapshot_id=None,
        kill_state="monitoring",
        kill_generation=0,
        session_date_et=None,
        rules_hash="rh-test",
        verdict_id="rv-abc",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestRecordRoundTrip(unittest.TestCase):
    def test_risk_verdict_round_trips_with_decision_id(self):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_risk_verdict(_verdict(), decision_id="d-1")
            rows = replay_risk(path)  # hash-verified
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["event_type"], EVT_RISK_VERDICT)
            self.assertEqual(row["v"], RISK_LEDGER_VERSION)
            self.assertEqual(row["decision_id"], "d-1")  # S6 correlation
            self.assertEqual(row["reasons"], ["run_gates_off"])
            self.assertEqual(row["gate_stage"], "run_gates")
            self.assertEqual(row["verdict_id"], "rv-abc")
            self.assertIs(row["allowed"], False)
            self.assertEqual(row["rules_hash"], "rh-test")
            self.assertEqual(row["gross_notional"], "0")

    def test_every_record_method_round_trips(self):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            read = parse_account_payload(account_payload(), source="fixture",
                                         seen_at_ms=0, ts_read_utc="2026-06-08T14:00:00Z")
            ledger.record_risk_verdict(_verdict(), decision_id="d-2")
            ledger.record_account_snapshot(result=read)
            ledger.record_account_alert(transition="degraded", status="stale", age_ms=6000)
            ledger.record_kill_transition(
                from_state="monitoring", to_state="flattening", cause="daily_loss_cap",
                generation=1, daily_loss_usd=Decimal("2000"), drawdown_usd=None,
                cap_usd=Decimal("1000"), account_snapshot_id=read.account_snapshot_id,
                stale_inputs=False, flattened=(), failed=(), residual=("AAPL",),
                tradability_annotations=())
            ledger.record_kill_retrip(cause="drill", generation=1, current_state="halted")
            ledger.record_kill_retry_residual(
                generation=1, residual_before=("AAPL",), residual_after=(),
                flattened=("AAPL",), failed=())
            ledger.record_kill_flatten_incomplete(generation=1, failed=(),
                                                  residual=("AAPL",))
            ledger.record_kill_eval_skipped(account_status="stale", generation=1)
            ledger.record_iml_observation(
                session_date_et="2026-06-08", ts_market_utc="2026-06-08T15:00:00Z",
                equity=Decimal("100000.005"), maintenance_margin=Decimal("30000"),
                iml=Decimal("70000.005"), deficiency=Decimal("0"),
                after_iml_reducing=True, eod=False, account_snapshot_id="as-x")
            ledger.record_margin_deficit_detected(
                deficit_id="imd-1", cause="opened", session_date_et="2026-06-08",
                amount=Decimal("250.005"), minor=False,
                equity_at_detection=BrokerUSD("18000"),
                satisfaction_deadline_et="2026-06-15", expires_after_et="2026-06-30")
            ledger.record_margin_deficit_satisfied(
                deficit_id="imd-1", session_date_et="2026-06-08",
                satisfied_on_et="2026-06-10", iml_eod_d=Decimal("100"),
                iml_eod_e=Decimal("400"))
            ledger.record_margin_deficit_expired(
                deficit_id="imd-1", session_date_et="2026-06-08",
                expires_after_et="2026-06-30")
            ledger.record_margin_deficit_unbaselined(
                deficit_id="imd-1", session_date_et="2026-06-08",
                noted_at_close_et="2026-06-09")
            ledger.record_margin_window_unresolved(
                deficit_id="imd-1", session_date_et="2026-06-08",
                error="unknown_session_date")
            ledger.record_margin_freeze_start(
                trigger_deficit_id="imd-1", effective_from_et="2026-06-16",
                expires_on_et="2026-09-14")
            ledger.record_margin_freeze_end(trigger_deficit_id="imd-1",
                                            expires_on_et="2026-09-14")
            ledger.record_pdt_transition(
                from_state="unknown", to_state="enforcing_legacy_pdt",
                evidence="broker_rejection", rejection_code=40310100)
            ledger.record_loss_hwm_update(
                session_date_et="2026-06-08", hwm_equity=BrokerUSD("100000.005"),
                equity=BrokerUSD("100000.005"), account_snapshot_id="as-x")
            rows = replay_risk(path)
            self.assertEqual(len(rows), 18)
            for row in rows:
                self.assertEqual(row["v"], RISK_LEDGER_VERSION)
                self.assertEqual(row["rules_hash"], "rh-test")

    def test_money_discipline_quantized_only_on_account_snapshot(self):
        # LD-R5: a sub-cent broker value journals at 2dp ONLY on account_snapshot rows;
        # rehydrate-bearing fields round-trip EXACT/unquantized.
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            read = parse_account_payload(
                account_payload(equity="100000.005"), source="fixture",
                seen_at_ms=0, ts_read_utc="2026-06-08T14:00:00Z")
            ledger.record_account_snapshot(result=read)
            ledger.record_iml_observation(
                session_date_et="2026-06-08", ts_market_utc="2026-06-08T15:00:00Z",
                equity=Decimal("100000.005"), maintenance_margin=Decimal("30000"),
                iml=Decimal("70000.005"), deficiency=Decimal("0"),
                after_iml_reducing=True, eod=False, account_snapshot_id="as-x")
            ledger.record_margin_deficit_detected(
                deficit_id="imd-1", cause="opened", session_date_et="2026-06-08",
                amount=Decimal("0.015"), minor=True,
                equity_at_detection=BrokerUSD("18000.005"),
                satisfaction_deadline_et="2026-06-15", expires_after_et="2026-06-30")
            ledger.record_loss_hwm_update(
                session_date_et="2026-06-08", hwm_equity=BrokerUSD("100000.005"),
                equity=BrokerUSD("100000.005"), account_snapshot_id="as-x")
            rows = replay_risk(path)
            snapshot, iml, deficit, hwm = rows
            self.assertEqual(snapshot["equity"], "100000.00")        # quantize-only, 2dp
            self.assertEqual(iml["equity"], "100000.005")            # EXACT
            self.assertEqual(iml["iml"], "70000.005")                # EXACT
            self.assertEqual(deficit["amount"], "0.015")             # EXACT
            self.assertEqual(deficit["equity_at_detection"], "18000.005")  # EXACT
            self.assertEqual(hwm["hwm_equity"], "100000.005")        # EXACT

    def test_account_snapshot_quantize_immune_to_ambient_decimal_context(self):
        # harden round, M4-DET-1: the account_snapshot quantize must run under the
        # pinned context (prec=28, ROUND_HALF_EVEN), never the ambient one — a
        # caller-shrunk global context must neither raise InvalidOperation on a
        # VALID BrokerAccountRead nor change persisted bytes (the F4 written-on-
        # every-put obligation; codebase pattern: test_calibration.py
        # test_persisted_arithmetic_immune_to_ambient_decimal_context).
        import decimal

        read = parse_account_payload(
            account_payload(equity="100000.123"), source="fixture",
            seen_at_ms=0, ts_read_utc="2026-06-08T14:00:00Z")
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_account_snapshot(result=read)
            baseline_bytes = Path(path).read_bytes()
        original = decimal.getcontext()
        hostile = decimal.Context(prec=3, rounding=decimal.ROUND_UP)
        try:
            decimal.setcontext(hostile)
            with TemporaryDirectory() as tmpdir:
                ledger, path = _ledger(tmpdir)
                ledger.record_account_snapshot(result=read)   # must NOT raise
                hostile_bytes = Path(path).read_bytes()
                row = replay_risk(path)[0]
        finally:
            decimal.setcontext(original)
        self.assertEqual(row["equity"], "100000.12")          # half-even, not ROUND_UP
        self.assertEqual(hostile_bytes, baseline_bytes)       # byte-identical row

    def test_account_snapshot_invalid_put_has_null_id(self):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            invalid = parse_account_payload(
                account_payload(equity="NaN"), source="fixture",
                seen_at_ms=0, ts_read_utc="2026-06-08T14:00:00Z")
            ledger.record_account_snapshot(result=invalid)
            row = replay_risk(path)[0]
            self.assertEqual(row["event_type"], EVT_ACCOUNT_SNAPSHOT)
            self.assertIsNone(row["account_snapshot_id"])
            self.assertEqual(row["status"], "invalid")
            self.assertEqual(row["invalid_reason"], "non_finite:equity")
            self.assertIsNone(row["equity"])


class TestVocabularyValidation(unittest.TestCase):
    def _with_ledger(self, fn):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            fn(ledger)

    def test_out_of_vocab_and_reserved_reasons_raise(self):
        def run(ledger):
            with self.assertRaises(RiskError):
                ledger.record_risk_verdict(_verdict(reasons=("bogus",)))
            with self.assertRaises(RiskError):
                ledger.record_risk_verdict(_verdict(reasons=("locate_unavailable",)))
        self._with_ledger(run)

    def test_out_of_vocab_states_and_causes_raise(self):
        def run(ledger):
            with self.assertRaises(RiskError):
                ledger.record_kill_transition(
                    from_state="monitoring", to_state="exploded", cause="drill",
                    generation=1, stale_inputs=False, flattened=(), failed=(),
                    residual=(), tradability_annotations=())
            with self.assertRaises(RiskError):
                ledger.record_kill_transition(
                    from_state="monitoring", to_state="flattening", cause="live_gate_flip",
                    generation=1, stale_inputs=False, flattened=(), failed=(),
                    residual=(), tradability_annotations=())  # reserved cause refused
            with self.assertRaises(RiskError):
                ledger.record_pdt_transition(from_state="unknown", to_state="pdt_on",
                                             evidence="account_flag")
            with self.assertRaises(RiskError):
                ledger.record_margin_deficit_detected(
                    deficit_id="imd-1", cause="grew", session_date_et="2026-06-08",
                    amount=Decimal("1"), minor=False,
                    equity_at_detection=BrokerUSD("1"),
                    satisfaction_deadline_et=None, expires_after_et=None)
            with self.assertRaises(RiskError):
                ledger.record_margin_window_unresolved(
                    deficit_id="imd-1", session_date_et="2026-06-08", error="oops")
            with self.assertRaises(RiskError):
                ledger.record_account_alert(transition="wobbled", status="stale")
        self._with_ledger(run)

    def test_allowed_iff_reasons_empty_enforced(self):
        def run(ledger):
            with self.assertRaises(RiskError):
                ledger.record_risk_verdict(_verdict(allowed=True))  # reasons non-empty
        self._with_ledger(run)

    def test_decimal_strict_floats_raise(self):
        def run(ledger):
            with self.assertRaises(ValueError):
                ledger.record_loss_hwm_update(
                    session_date_et="2026-06-08", hwm_equity=100000.0,
                    equity=BrokerUSD("100000"), account_snapshot_id="as-x")
            with self.assertRaises(ValueError):
                ledger.record_iml_observation(
                    session_date_et="2026-06-08", ts_market_utc="t",
                    equity=Decimal("NaN"), maintenance_margin=Decimal("1"),
                    iml=Decimal("1"), deficiency=Decimal("0"),
                    after_iml_reducing=True, eod=False, account_snapshot_id="as-x")
        self._with_ledger(run)


class TestReplaySemantics(unittest.TestCase):
    def test_truncated_tail_tolerated_corrupt_line_fatal(self):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_kill_eval_skipped(account_status="stale", generation=0)
            ledger.record_kill_eval_skipped(account_status="missing", generation=0)
            text = path.read_text(encoding="utf-8")
            # truncated (no-newline) tail -> dropped
            path.write_text(text + '{"half', encoding="utf-8")
            self.assertEqual(len(replay_risk(path)), 2)
            # complete corrupt line -> fatal
            path.write_text(text + '{"bad": 1}\n', encoding="utf-8")
            with self.assertRaises(JournalCorruption):
                replay_risk(path)

    def test_byte_identical_replay_with_same_run_id(self):
        rows_a = self._write_sequence("run-x")
        rows_b = self._write_sequence("run-x")
        self.assertEqual(rows_a, rows_b)  # incl. hashes — byte-for-byte (R4)

    def _write_sequence(self, run_id):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir, run_id=run_id)
            ledger.record_risk_verdict(_verdict(), decision_id="d-9")
            ledger.record_margin_deficit_detected(
                deficit_id="imd-1", cause="opened", session_date_et="2026-06-08",
                amount=Decimal("250"), minor=False, equity_at_detection=BrokerUSD("18000"),
                satisfaction_deadline_et="2026-06-15", expires_after_et="2026-06-30")
            return [json.dumps(r, sort_keys=True) for r in replay_risk(path)]


class TestRehydrateFold(unittest.TestCase):
    def test_frozen_key_set_and_defaults(self):
        state = rehydrate_risk_state([])
        self.assertEqual(set(state), {"kill", "margin", "pdt", "loss"})
        self.assertEqual(state["kill"], {"state": "monitoring", "generation": 0,
                                         "residual": []})
        self.assertEqual(state["margin"]["deficits"], {})
        self.assertEqual(state["margin"]["iml_eod"], {})
        self.assertIsNone(state["margin"]["freeze"])
        self.assertEqual(state["pdt"], {"state": "unknown", "rejection_latched": False})
        self.assertEqual(state["loss"], {"hwm_equity": None})

    def test_field_merge_per_deficit_id(self):
        # F13: satisfied/expired/unbaselined rows OVERLAY only their own fields —
        # never whole-row replace (a satisfied row has no amount).
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_margin_deficit_detected(
                deficit_id="imd-1", cause="opened", session_date_et="2026-06-08",
                amount=Decimal("100"), minor=False, equity_at_detection=BrokerUSD("18000"),
                satisfaction_deadline_et="2026-06-15", expires_after_et="2026-06-30")
            ledger.record_margin_deficit_detected(
                deficit_id="imd-1", cause="increased", session_date_et="2026-06-08",
                amount=Decimal("250"), minor=False, equity_at_detection=BrokerUSD("17000"),
                satisfaction_deadline_et="2026-06-15", expires_after_et="2026-06-30")
            ledger.record_margin_deficit_satisfied(
                deficit_id="imd-1", session_date_et="2026-06-08",
                satisfied_on_et="2026-06-10", iml_eod_d=Decimal("100"),
                iml_eod_e=Decimal("400"))
            state = rehydrate_risk_state(replay_risk(path))
            record = state["margin"]["deficits"]["imd-1"]
            self.assertEqual(record["amount"], "250")          # max-merged, kept on overlay
            self.assertEqual(record["satisfied_on_et"], "2026-06-10")
            self.assertEqual(record["equity_at_detection"], "17000")

    def test_kill_fold_latest_row_wins_and_trailing_flattening_folds_halted(self):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_kill_transition(
                from_state="monitoring", to_state="flattening", cause="drill",
                generation=1, stale_inputs=True, flattened=(), failed=(),
                residual=("AAPL", "MSFT"), tradability_annotations=())
            state = rehydrate_risk_state(replay_risk(path))
            # safety-F5: trailing monitoring->flattening folds to HALTED + that residual
            self.assertEqual(state["kill"]["state"], "halted")
            self.assertEqual(state["kill"]["generation"], 1)
            self.assertEqual(state["kill"]["residual"], ["AAPL", "MSFT"])
            ledger.record_kill_transition(
                from_state="flattening", to_state="halted", cause="drill",
                generation=1, stale_inputs=True, flattened=("AAPL",),
                failed=(("MSFT", "boom"),), residual=("MSFT",),
                tradability_annotations=())
            ledger.record_kill_retry_residual(
                generation=1, residual_before=("MSFT",), residual_after=(),
                flattened=("MSFT",), failed=())
            state = rehydrate_risk_state(replay_risk(path))
            self.assertEqual(state["kill"]["state"], "halted")
            self.assertEqual(state["kill"]["residual"], [])

    def test_pdt_and_loss_and_freeze_fold(self):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_pdt_transition(from_state="unknown",
                                         to_state="enforcing_legacy_pdt",
                                         evidence="broker_rejection",
                                         rejection_code=40310100)
            ledger.record_pdt_transition(from_state="enforcing_legacy_pdt",
                                         to_state="enforcing_legacy_pdt",
                                         evidence="account_flag")
            ledger.record_loss_hwm_update(session_date_et="2026-06-08",
                                          hwm_equity=BrokerUSD("101000"),
                                          equity=BrokerUSD("101000"),
                                          account_snapshot_id="as-x")
            ledger.record_margin_freeze_start(trigger_deficit_id="imd-1",
                                              effective_from_et="2026-06-16",
                                              expires_on_et="2026-09-14")
            ledger.record_iml_observation(
                session_date_et="2026-06-08", ts_market_utc="t",
                equity=Decimal("100000"), maintenance_margin=Decimal("30000"),
                iml=Decimal("70000"), deficiency=Decimal("0"),
                after_iml_reducing=False, eod=True, account_snapshot_id="as-x")
            state = rehydrate_risk_state(replay_risk(path))
            self.assertEqual(state["pdt"]["state"], "enforcing_legacy_pdt")
            self.assertIs(state["pdt"]["rejection_latched"], True)  # durable latch (LD-R3)
            self.assertEqual(state["loss"]["hwm_equity"], "101000")
            self.assertEqual(state["margin"]["freeze"]["trigger_deficit_id"], "imd-1")
            self.assertEqual(state["margin"]["freeze"]["expires_on_et"], "2026-09-14")
            self.assertEqual(state["margin"]["iml_eod"], {"2026-06-08": "70000"})

    def test_stream_constant(self):
        self.assertEqual(STREAM_RISK, "risk")
        self.assertEqual(EVT_KILL_TRANSITION, "kill_switch_transition")
        self.assertEqual(EVT_MARGIN_DEFICIT, "margin_deficit_detected")
        self.assertEqual(EVT_FREEZE_START, "margin_freeze_start")
        self.assertEqual(EVT_PDT_TRANSITION, "pdt_regime_transition")
        self.assertEqual(EVT_HWM_UPDATE, "loss_hwm_update")
        self.assertEqual(EVT_IML_OBSERVATION, "iml_observation")


if __name__ == "__main__":
    unittest.main()
