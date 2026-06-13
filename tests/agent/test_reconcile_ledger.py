"""M6 §I file 2 — journal/reconcile_alerts.jsonl: ReconcileLedger row shapes,
per-field validation (§B.1a), replay/rehydrate edge semantics (§B.1b), and the
latch fold `rehydrate_reconcile_state` (FD-M6-6).

Enumerated cases (contract §I file 2, all of them):
  1.  every record_* round-trips hash-verified via replay_reconcile;
  2.  frozen field sets: missing/extra/positional => TypeError; a raise leaves
      the stream byte-untouched;
  3.  "v": 1 first payload key + rules_hash last on every row;
  4.  reserved-key hygiene (V12); fills_seq_watermark accepted;
  5.  each §B.1a validation rule has at least one negative cell;
  6.  byte-identical replay under pinned run_id + row clock;
  7.  truncated-tail dropped; newline-terminated bad line => JournalCorruption;
  8.  rehydrate_reconcile_state fold semantics (latch, baseline latest-wins,
      pass_count, outstanding_cash_residue, M6C-1/12/22, RC-8);
  9.  cross-writer seq sharing on one resolved path;
  10. cash_usd exact-unquantized round-trip (LD-R5).

Invariants: S3, S2, S5, S6.
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.broker_reconcile import ReconcileError
from agent.journal import JournalCorruption, _RESERVED
from agent.reconcile_ledger import (
    EVT_RECONCILE,
    EVT_RECONCILE_BASELINE,
    EVT_RECONCILE_NOTE,
    EVT_RECONCILE_RUN,
    RECONCILE_LEDGER_VERSION,
    STREAM_RECONCILE_ALERTS,
    ReconcileLedger,
    rehydrate_reconcile_state,
    replay_reconcile,
)
from agent.serializer import BrokerUSD, ModeledUSD, dumps
from recorder.persistence import EventWriter

_CLOCK = lambda: "2026-06-10T20:00:00.000000+00:00"  # noqa: E731 — byte-determinism

_ZERO_STATE = {"latched": False, "latest_baseline": None,
               "outstanding_cash_residue": None, "pass_count": 0,
               "drift_in_window": 0}


def _ledger(tmpdir, run_id="run-1"):
    path = Path(tmpdir) / "reconcile_alerts.jsonl"
    writer = EventWriter(path, run_id, clock=_CLOCK)
    return ReconcileLedger(writer, rules_hash="rh-test"), path


def _drift_kwargs(**over):
    base = dict(reconcile_id="rc-1", drift_id="rd-1", kind="position_qty",
                symbol="AAPL", field="qty", local="10", broker="12", diff="2",
                action="adjusted", position_id="pos-1", local_order_id=None,
                broker_order_id=None)
    base.update(over)
    return base


def _cash_drift_kwargs(**over):
    base = dict(reconcile_id="rc-1", drift_id="rd-2", kind="cash", symbol=None,
                field="cash", local="1000.00", broker="990.00", diff="-10.00",
                action="latched_operator", position_id=None,
                local_order_id=None, broker_order_id=None)
    base.update(over)
    return base


def _note_kwargs(**over):
    base = dict(reconcile_id="rc-1", note="cost_unverifiable", symbol="AAPL",
                detail="")
    base.update(over)
    return base


def _baseline_kwargs(**over):
    base = dict(reconcile_id="rc-1", session_date_et="2026-06-10",
                cash_usd=BrokerUSD("100000.00"), equity_usd=BrokerUSD("120000"),
                buying_power_usd=BrokerUSD("200000"), fills_seq_watermark=7,
                positions=[{"symbol": "AAPL", "qty": "10"}],
                durable_seeded=["cusip:TESTAAPL1"])
    base.update(over)
    return base


def _run_kwargs(**over):
    base = dict(reconcile_id="rc-1", phase="cli", session_date_et="2026-06-10",
                trigger_durable_key=None, broker_source="fixture",
                checked_symbols=["AAPL"], drift_count=0, adjusted_count=0,
                note_count=0, completed=True, clean=True)
    base.update(over)
    return base


_GOOD_CALLS = (
    ("record_reconcile", _drift_kwargs),
    ("record_reconcile_note", _note_kwargs),
    ("record_reconcile_baseline", _baseline_kwargs),
    ("record_reconcile_run", _run_kwargs),
)


class _RecordSpy:
    """Captures the fields dicts (and the journal kwargs) handed to the writer —
    the v-first / rules_hash-last construction order is observable HERE, before
    the serializer's sorted-keys rendering erases it."""

    def __init__(self, writer):
        self._writer = writer
        self.captured_fields = []
        self.captured_journal_kwargs = []

    def record(self, event_type, fields, *, decision_id=None, order_id=None):
        self.captured_fields.append(fields)
        self.captured_journal_kwargs.append((decision_id, order_id))
        return self._writer.record(event_type, fields,
                                   decision_id=decision_id, order_id=order_id)


def _write_all_four(ledger):
    ledger.record_reconcile(**_drift_kwargs())
    ledger.record_reconcile_note(**_note_kwargs())
    ledger.record_reconcile_baseline(**_baseline_kwargs())
    ledger.record_reconcile_run(**_run_kwargs(drift_count=1, note_count=1,
                                              clean=False))


class TestRecordRoundTrip(unittest.TestCase):
    def test_every_record_method_round_trips_hash_verified(self):
        # case 1
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            _write_all_four(ledger)
            rows = replay_reconcile(path)  # hash-verified (S3)
            self.assertEqual([r["event_type"] for r in rows],
                             [EVT_RECONCILE, EVT_RECONCILE_NOTE,
                              EVT_RECONCILE_BASELINE, EVT_RECONCILE_RUN])
            self.assertEqual([r["seq"] for r in rows], [1, 2, 3, 4])
            drift, note, baseline, summary = rows
            self.assertEqual(drift["kind"], "position_qty")
            self.assertEqual(drift["local"], "10")
            self.assertEqual(drift["broker"], "12")
            self.assertEqual(drift["diff"], "2")
            self.assertEqual(drift["action"], "adjusted")
            self.assertEqual(drift["drift_id"], "rd-1")
            self.assertEqual(note["note"], "cost_unverifiable")
            self.assertEqual(baseline["fills_seq_watermark"], 7)
            self.assertEqual(baseline["positions"],
                             [{"symbol": "AAPL", "qty": "10"}])
            self.assertEqual(summary["phase"], "cli")
            self.assertIs(summary["completed"], True)
            self.assertIs(summary["clean"], False)

    def test_v_first_and_rules_hash_last_every_row(self):
        # case 3 — construction order observed at the writer seam
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "reconcile_alerts.jsonl"
            spy = _RecordSpy(EventWriter(path, "run-1", clock=_CLOCK))
            ledger = ReconcileLedger(spy, rules_hash="rh-test")
            _write_all_four(ledger)
            self.assertEqual(len(spy.captured_fields), 4)
            for fields in spy.captured_fields:
                keys = list(fields)
                self.assertEqual(keys[0], "v")
                self.assertEqual(keys[-1], "rules_hash")
                self.assertEqual(fields["v"], RECONCILE_LEDGER_VERSION)
                self.assertEqual(fields["rules_hash"], "rh-test")
            # reconcile rows NEVER ride the journal decision_id/order_id kwargs
            # (§B.1 — broker ids ride the payload field broker_order_id)
            self.assertEqual(spy.captured_journal_kwargs, [(None, None)] * 4)
            for row in replay_reconcile(path):
                self.assertEqual(row["v"], 1)
                self.assertEqual(row["rules_hash"], "rh-test")
                self.assertNotIn("decision_id", row)
                self.assertNotIn("order_id", row)

    def test_reserved_key_hygiene_and_watermark_name(self):
        # case 4 — V12: no payload field collides with the journal envelope;
        # the payload-legal name fills_seq_watermark (bare `seq` is reserved)
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "reconcile_alerts.jsonl"
            spy = _RecordSpy(EventWriter(path, "run-1", clock=_CLOCK))
            ledger = ReconcileLedger(spy, rules_hash="rh-test")
            _write_all_four(ledger)
            for fields in spy.captured_fields:
                self.assertEqual(set(fields) & _RESERVED, set())
            baseline = replay_reconcile(path)[2]
            self.assertEqual(baseline["fills_seq_watermark"], 7)
            self.assertEqual(baseline["seq"], 3)  # the journal's own counter

    def test_cash_usd_exact_unquantized_round_trip(self):
        # case 10 — LD-R5: sub-cent broker money round-trips EXACT
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_reconcile_baseline(**_baseline_kwargs(
                cash_usd=BrokerUSD("100000.005"),
                equity_usd=BrokerUSD("119999.9999"),
                buying_power_usd=BrokerUSD("200000.0001")))
            row = replay_reconcile(path)[0]
            self.assertEqual(row["cash_usd"], "100000.005")
            self.assertEqual(row["equity_usd"], "119999.9999")
            self.assertEqual(row["buying_power_usd"], "200000.0001")

    def test_stream_and_version_constants(self):
        self.assertEqual(STREAM_RECONCILE_ALERTS, "reconcile_alerts")
        self.assertEqual(RECONCILE_LEDGER_VERSION, 1)
        self.assertEqual(EVT_RECONCILE, "reconcile")
        self.assertEqual(EVT_RECONCILE_NOTE, "reconcile_note")
        self.assertEqual(EVT_RECONCILE_BASELINE, "reconcile_baseline")
        self.assertEqual(EVT_RECONCILE_RUN, "reconcile_run")

    def test_account_sources_copy_matches_the_one_vocabulary_home(self):
        # the §4 import wall forces a verbatim local copy (the exec_ledger
        # _PROVENANCE_KEYS precedent) — pin it against the source (V2)
        from agent.reconcile_ledger import _ACCOUNT_SOURCES
        from agent.risk.account_state import ACCOUNT_SOURCES
        self.assertEqual(_ACCOUNT_SOURCES, ACCOUNT_SOURCES)


class TestFrozenFieldSets(unittest.TestCase):
    # case 2

    def test_missing_kwarg_raises_for_every_method(self):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            for method, build in _GOOD_CALLS:
                kwargs = build()
                for name in list(kwargs):
                    short = dict(kwargs)
                    del short[name]
                    with self.assertRaises(TypeError, msg=f"{method} -{name}"):
                        getattr(ledger, method)(**short)

    def test_extra_kwarg_raises_for_every_method(self):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            for method, build in _GOOD_CALLS:
                extra = build()
                extra["bogus_field"] = 1
                with self.assertRaises(TypeError, msg=f"{method} +bogus"):
                    getattr(ledger, method)(**extra)

    def test_positional_args_refused(self):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            with self.assertRaises(TypeError):
                ledger.record_reconcile_note("rc-1", "cost_unverifiable",
                                             "AAPL", "")

    def test_raise_leaves_stream_byte_untouched(self):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_reconcile(**_drift_kwargs())
            before = path.read_bytes()
            bad_calls = (
                lambda: ledger.record_reconcile(**_drift_kwargs(kind="bogus")),
                lambda: ledger.record_reconcile(**_drift_kwargs(local=1.5)),
                lambda: ledger.record_reconcile_note(**_note_kwargs(note="nah")),
                lambda: ledger.record_reconcile_baseline(
                    **_baseline_kwargs(cash_usd=Decimal("1"))),
                lambda: ledger.record_reconcile_run(
                    **_run_kwargs(clean=True, completed=False)),
            )
            for bad in bad_calls:
                with self.assertRaises((ReconcileError, TypeError)):
                    bad()
            self.assertEqual(path.read_bytes(), before)  # byte-untouched
            self.assertEqual(len(replay_reconcile(path)), 1)


class TestPerFieldValidation(unittest.TestCase):
    # case 5 — at least one negative cell per §B.1a rule

    def _raises(self, exc, method, kwargs):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            with self.assertRaises(exc, msg=f"{method} {kwargs}"):
                getattr(ledger, method)(**kwargs)
            self.assertEqual(replay_reconcile(path), [])  # NO row written

    def _ok(self, method, kwargs):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            getattr(ledger, method)(**kwargs)
            self.assertEqual(len(replay_reconcile(path)), 1)

    def test_id_prefixes(self):
        self._raises(ReconcileError, "record_reconcile",
                     _drift_kwargs(reconcile_id="x-1"))
        self._raises(ReconcileError, "record_reconcile",
                     _drift_kwargs(drift_id="drift-1"))
        self._raises(ReconcileError, "record_reconcile",
                     _drift_kwargs(reconcile_id="rc-"))  # prefix alone
        self._raises(ReconcileError, "record_reconcile_note",
                     _note_kwargs(reconcile_id="bogus"))
        self._raises(ReconcileError, "record_reconcile_baseline",
                     _baseline_kwargs(reconcile_id="bogus"))
        self._raises(ReconcileError, "record_reconcile_run",
                     _run_kwargs(reconcile_id="bogus"))

    def test_out_of_vocab_members(self):
        self._raises(ReconcileError, "record_reconcile",
                     _drift_kwargs(kind="bogus"))
        self._raises(ReconcileError, "record_reconcile",
                     _drift_kwargs(action="bogus"))
        self._raises(ReconcileError, "record_reconcile",
                     _drift_kwargs(field="bogus"))
        self._raises(ReconcileError, "record_reconcile_note",
                     _note_kwargs(note="bogus"))
        self._raises(ReconcileError, "record_reconcile_run",
                     _run_kwargs(phase="acknowledge"))
        self._raises(ReconcileError, "record_reconcile_run",
                     _run_kwargs(broker_source="bogus"))

    def test_symbol_rules(self):
        # None only where the shape allows: cash rows; non-symbol notes
        self._raises(ReconcileError, "record_reconcile",
                     _drift_kwargs(symbol=None))
        self._raises(ReconcileError, "record_reconcile",
                     _drift_kwargs(symbol=""))
        self._raises(ReconcileError, "record_reconcile_note",
                     _note_kwargs(symbol=""))
        self._ok("record_reconcile", _cash_drift_kwargs())          # cash: None ok
        self._ok("record_reconcile_note", _note_kwargs(symbol=None))

    def test_value_string_structural_rules(self):
        # bool/float/non-finite/unparseable/non-canonical anywhere => raise (S2)
        self._raises(ReconcileError, "record_reconcile", _drift_kwargs(local=1.5))
        self._raises(ReconcileError, "record_reconcile", _drift_kwargs(broker=True))
        self._raises(ReconcileError, "record_reconcile",
                     _drift_kwargs(local=Decimal("10")))   # typed value: facade takes strings
        self._raises(ReconcileError, "record_reconcile", _drift_kwargs(local="abc"))
        self._raises(ReconcileError, "record_reconcile", _drift_kwargs(broker="NaN"))
        self._raises(ReconcileError, "record_reconcile",
                     _drift_kwargs(diff="Infinity"))
        self._raises(ReconcileError, "record_reconcile", _drift_kwargs(local=" 1"))
        # state tokens: legal ONLY on order_state rows, and only closed ones
        self._ok("record_reconcile", _drift_kwargs(
            kind="order_state", field="order_state", local="open",
            broker="filled", diff=None, action="resolved_terminal",
            position_id=None, local_order_id="o-1", broker_order_id="o-1"))
        self._raises(ReconcileError, "record_reconcile",
                     _drift_kwargs(local="open"))  # token on a qty row
        self._raises(ReconcileError, "record_reconcile", _drift_kwargs(
            kind="order_state", field="order_state", local="vibes",
            broker="filled", diff=None, action="resolved_terminal",
            position_id=None, local_order_id="o-1", broker_order_id=None))
        # explicit None accepted on nullable value slots
        self._ok("record_reconcile", _drift_kwargs(
            kind="position_unknown_broker", local=None, diff=None,
            action="latched_operator", position_id=None))

    def test_local_order_id_and_position_id_prefixes(self):
        self._raises(ReconcileError, "record_reconcile",
                     _drift_kwargs(position_id="position-1"))
        self._raises(ReconcileError, "record_reconcile", _drift_kwargs(
            local_order_id="order-1"))
        self._ok("record_reconcile",
                 _drift_kwargs(local_order_id="synthetic-o-1"))
        # broker_order_id is deliberately UN-prefixed (broker UUIDs / flatten ids)
        self._ok("record_reconcile",
                 _drift_kwargs(broker_order_id="flatten-AAPL"))
        self._raises(ReconcileError, "record_reconcile",
                     _drift_kwargs(broker_order_id=42))

    def test_money_lineage_baseline_fields(self):
        # BrokerUSD via as_broker_usd: float / plain Decimal / ModeledUSD => TypeError
        self._raises(TypeError, "record_reconcile_baseline",
                     _baseline_kwargs(cash_usd=0.01))
        self._raises(TypeError, "record_reconcile_baseline",
                     _baseline_kwargs(cash_usd=Decimal("1")))
        self._raises(TypeError, "record_reconcile_baseline",
                     _baseline_kwargs(equity_usd=ModeledUSD("1")))
        self._raises(TypeError, "record_reconcile_baseline",
                     _baseline_kwargs(buying_power_usd=Decimal("1")))

    def test_fills_seq_watermark(self):
        self._raises(ReconcileError, "record_reconcile_baseline",
                     _baseline_kwargs(fills_seq_watermark=True))  # bool rejected
        self._raises(ReconcileError, "record_reconcile_baseline",
                     _baseline_kwargs(fills_seq_watermark=-1))
        self._raises(ReconcileError, "record_reconcile_baseline",
                     _baseline_kwargs(fills_seq_watermark="7"))
        self._ok("record_reconcile_baseline",
                 _baseline_kwargs(fills_seq_watermark=0))

    def test_positions_shape(self):
        self._raises(ReconcileError, "record_reconcile_baseline",
                     _baseline_kwargs(positions=[
                         {"symbol": "MSFT", "qty": "1"},
                         {"symbol": "AAPL", "qty": "2"}]))   # unsorted
        self._raises(ReconcileError, "record_reconcile_baseline",
                     _baseline_kwargs(positions=[
                         {"symbol": "AAPL", "qty": "1"},
                         {"symbol": "AAPL", "qty": "2"}]))   # duplicate
        self._raises(ReconcileError, "record_reconcile_baseline",
                     _baseline_kwargs(positions=[
                         {"symbol": "AAPL", "qty": "1", "extra": 1}]))
        self._raises(ReconcileError, "record_reconcile_baseline",
                     _baseline_kwargs(positions=[{"symbol": "AAPL"}]))
        self._raises(ReconcileError, "record_reconcile_baseline",
                     _baseline_kwargs(positions=[
                         {"symbol": "AAPL", "qty": 1.0}]))
        self._raises(ReconcileError, "record_reconcile_baseline",
                     _baseline_kwargs(positions=[
                         {"symbol": "AAPL", "qty": "abc"}]))
        self._raises(ReconcileError, "record_reconcile_baseline",
                     _baseline_kwargs(positions="AAPL"))
        self._ok("record_reconcile_baseline", _baseline_kwargs(positions=[]))

    def test_sorted_lists_and_counts(self):
        self._raises(ReconcileError, "record_reconcile_run",
                     _run_kwargs(checked_symbols=["MSFT", "AAPL"]))
        self._raises(ReconcileError, "record_reconcile_run",
                     _run_kwargs(checked_symbols=["AAPL", "AAPL"]))
        self._raises(ReconcileError, "record_reconcile_run",
                     _run_kwargs(checked_symbols=[1]))
        self._raises(ReconcileError, "record_reconcile_baseline",
                     _baseline_kwargs(durable_seeded=["b", "a"]))
        self._raises(ReconcileError, "record_reconcile_run",
                     _run_kwargs(drift_count=-1, clean=False))
        self._raises(ReconcileError, "record_reconcile_run",
                     _run_kwargs(note_count=True))
        self._raises(ReconcileError, "record_reconcile_run",
                     _run_kwargs(adjusted_count="0"))

    def test_completed_clean_consistency(self):
        # M6C-22: a buggy clean summary cannot clear a latch its own rows set
        self._raises(ReconcileError, "record_reconcile_run",
                     _run_kwargs(clean=True, completed=False))
        self._raises(ReconcileError, "record_reconcile_run",
                     _run_kwargs(clean=True, drift_count=2))
        self._raises(ReconcileError, "record_reconcile_run",
                     _run_kwargs(completed="yes"))
        self._raises(ReconcileError, "record_reconcile_run",
                     _run_kwargs(clean=1))
        self._ok("record_reconcile_run",
                 _run_kwargs(clean=False, completed=False))

    def test_detail_and_trigger_durable_key(self):
        self._raises(ReconcileError, "record_reconcile_note",
                     _note_kwargs(detail=None))
        self._raises(ReconcileError, "record_reconcile_note",
                     _note_kwargs(detail=12))
        self._raises(ReconcileError, "record_reconcile_note",
                     _note_kwargs(detail="x" * 1001))   # bounded (§B.1a)
        self._raises(ReconcileError, "record_reconcile_run",
                     _run_kwargs(trigger_durable_key=42))
        self._ok("record_reconcile_run",
                 _run_kwargs(phase="immediate", clean=False,
                             trigger_durable_key="cusip:TESTAAPL1"))


class TestReplaySemantics(unittest.TestCase):
    def test_byte_identical_replay_under_pinned_run_id_and_clock(self):
        # case 6 — the determinism pin (S6)
        rows_a = self._write_sequence("run-x")
        rows_b = self._write_sequence("run-x")
        self.assertEqual(rows_a, rows_b)  # incl. hashes — byte-for-byte

    def _write_sequence(self, run_id):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir, run_id=run_id)
            _write_all_four(ledger)
            return [json.dumps(r, sort_keys=True) for r in replay_reconcile(path)]

    def test_truncated_tail_tolerated_corrupt_line_fatal(self):
        # case 7 — §B.1b inherited V12 semantics
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_reconcile(**_drift_kwargs())
            ledger.record_reconcile_run(**_run_kwargs(drift_count=1, clean=False))
            text = path.read_text(encoding="utf-8")
            path.write_text(text + '{"half', encoding="utf-8")     # truncated tail
            self.assertEqual(len(replay_reconcile(path)), 2)
            path.write_text(text + '{"bad": 1}\n', encoding="utf-8")
            with self.assertRaises(JournalCorruption):
                replay_reconcile(path)

    def test_empty_or_missing_stream_replays_empty(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "reconcile_alerts.jsonl"
            self.assertEqual(replay_reconcile(path), [])   # missing
            path.write_text("", encoding="utf-8")
            self.assertEqual(replay_reconcile(path), [])   # empty

    def test_cross_writer_seq_sharing_on_one_resolved_path(self):
        # case 9 — prior-run seeding ledger + fresh ledger share one seq (V12)
        with TemporaryDirectory() as tmpdir:
            prior, path = _ledger(tmpdir, run_id="run-prior")
            prior.record_reconcile(**_drift_kwargs())
            prior.record_reconcile_run(**_run_kwargs(drift_count=1, clean=False))
            fresh = ReconcileLedger(EventWriter(path, "run-live", clock=_CLOCK),
                                    rules_hash="rh-test")
            fresh.record_reconcile(**_drift_kwargs(reconcile_id="rc-2",
                                                   drift_id="rd-9"))
            fresh.record_reconcile_run(**_run_kwargs(reconcile_id="rc-2",
                                                     drift_count=1, clean=False))
            rows = replay_reconcile(path)
            self.assertEqual([r["seq"] for r in rows], [1, 2, 3, 4])
            self.assertEqual([r["run_id"] for r in rows],
                             ["run-prior", "run-prior", "run-live", "run-live"])
            # the fold is total over the shared-seq interleaving (D21: cross-run)
            state = rehydrate_reconcile_state(rows)
            self.assertEqual(state["pass_count"], 2)
            self.assertIs(state["latched"], True)


class TestRehydrateFold(unittest.TestCase):
    # case 8

    def _fold(self, path):
        return rehydrate_reconcile_state(replay_reconcile(path))

    def test_empty_stream_pinned_zero_state(self):
        self.assertEqual(rehydrate_reconcile_state([]), _ZERO_STATE)

    def test_drift_sets_and_completed_clean_clears_per_phase(self):
        for phase in ("sod", "eod", "cli"):
            with TemporaryDirectory() as tmpdir:
                ledger, path = _ledger(tmpdir)
                ledger.record_reconcile(**_drift_kwargs())
                ledger.record_reconcile_run(**_run_kwargs(
                    drift_count=1, clean=False))
                self.assertIs(self._fold(path)["latched"], True, phase)
                ledger.record_reconcile_run(**_run_kwargs(
                    reconcile_id="rc-2", phase=phase))   # clean, completed
                self.assertIs(self._fold(path)["latched"], False, phase)

    def test_immediate_clean_summary_never_clears(self):
        # M6C-1
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_reconcile(**_drift_kwargs())
            ledger.record_reconcile_run(**_run_kwargs(drift_count=1, clean=False))
            ledger.record_reconcile_run(**_run_kwargs(
                reconcile_id="rc-2", phase="immediate"))  # clean+completed, empty window
            self.assertIs(self._fold(path)["latched"], True)
            ledger.record_reconcile_run(**_run_kwargs(reconcile_id="rc-3",
                                                      phase="cli"))
            self.assertIs(self._fold(path)["latched"], False)

    def test_drift_rows_with_clean_summary_in_one_window_keeps_set(self):
        # M6C-22 — fail-closed against an inconsistent (seeded) window: the
        # facade cannot cross-check counts against rows, so the fold must
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_reconcile(**_drift_kwargs())
            ledger.record_reconcile_run(**_run_kwargs())  # clean=true, drift_count=0
            self.assertIs(self._fold(path)["latched"], True)

    def test_trailing_rows_without_summary_fail_closed(self):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_reconcile(**_drift_kwargs())   # mid-pass crash shape
            state = self._fold(path)
            self.assertIs(state["latched"], True)
            self.assertEqual(state["pass_count"], 0)
            self.assertEqual(state["drift_in_window"], 1)

    def test_set_clear_set_sequence(self):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_reconcile(**_drift_kwargs())
            ledger.record_reconcile_run(**_run_kwargs(drift_count=1, clean=False))
            ledger.record_reconcile_run(**_run_kwargs(reconcile_id="rc-2",
                                                      phase="sod"))
            self.assertIs(self._fold(path)["latched"], False)
            ledger.record_reconcile(**_drift_kwargs(reconcile_id="rc-3",
                                                    drift_id="rd-3"))
            self.assertIs(self._fold(path)["latched"], True)

    def test_incomplete_summary_clears_nothing(self):
        # RC-8 / FD-M6-7: completed=false leaves latch AND residue untouched
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_reconcile(**_cash_drift_kwargs())
            ledger.record_reconcile_run(**_run_kwargs(
                drift_count=1, clean=False, completed=False))
            state = self._fold(path)
            self.assertIs(state["latched"], True)
            self.assertIsNotNone(state["outstanding_cash_residue"])
            self.assertEqual(state["pass_count"], 1)

    def test_baseline_latest_wins(self):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_reconcile_baseline(**_baseline_kwargs())
            ledger.record_reconcile_baseline(**_baseline_kwargs(
                reconcile_id="rc-2", fills_seq_watermark=11,
                cash_usd=BrokerUSD("90000.00")))
            state = self._fold(path)
            self.assertEqual(state["latest_baseline"]["fills_seq_watermark"], 11)
            self.assertEqual(state["latest_baseline"]["cash_usd"], "90000.00")
            self.assertEqual(state["latest_baseline"]["reconcile_id"], "rc-2")

    def test_pass_count_counts_all_summaries_completed_or_not(self):
        # M6C-12
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_reconcile_run(**_run_kwargs())
            ledger.record_reconcile_run(**_run_kwargs(
                reconcile_id="rc-2", completed=False, clean=False))
            ledger.record_reconcile_run(**_run_kwargs(reconcile_id="rc-3",
                                                      phase="eod"))
            self.assertEqual(self._fold(path)["pass_count"], 3)

    def test_outstanding_cash_residue_set_and_held_through_skip_window(self):
        # RC-8: set by a latched_operator cash row; held through a cash-bearing
        # completed window (the skip-pass re-journal); latest row content wins
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_reconcile(**_cash_drift_kwargs())
            state = self._fold(path)
            self.assertEqual(state["outstanding_cash_residue"]["drift_id"], "rd-2")
            self.assertEqual(state["outstanding_cash_residue"]["local"], "1000.00")
            ledger.record_reconcile_run(**_run_kwargs(drift_count=1, clean=False))
            self.assertIsNotNone(self._fold(path)["outstanding_cash_residue"])
            # pass N+1: cash lens SKIPPED, carried residue RE-JOURNALED
            # (byte-identical local/broker/diff, fresh drift_id — FD-M6-17)
            ledger.record_reconcile(**_cash_drift_kwargs(reconcile_id="rc-2",
                                                         drift_id="rd-7"))
            ledger.record_reconcile_note(**_note_kwargs(
                reconcile_id="rc-2", note="cash_skipped_inflight", symbol=None))
            ledger.record_reconcile_run(**_run_kwargs(
                reconcile_id="rc-2", drift_count=1, note_count=1, clean=False))
            state = self._fold(path)
            self.assertIs(state["latched"], True)
            self.assertEqual(state["outstanding_cash_residue"]["drift_id"], "rd-7")

    def test_outstanding_cash_residue_cleared_by_rebaselined_row(self):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_reconcile(**_cash_drift_kwargs())
            ledger.record_reconcile_run(**_run_kwargs(drift_count=1, clean=False))
            ledger.record_reconcile(**_cash_drift_kwargs(
                reconcile_id="rc-2", drift_id="rd-8", action="rebaselined"))
            ledger.record_reconcile_run(**_run_kwargs(
                reconcile_id="rc-2", drift_count=1, clean=False))
            state = self._fold(path)
            self.assertIsNone(state["outstanding_cash_residue"])
            self.assertIs(state["latched"], True)  # drift was still found (exit 1)

    def test_outstanding_cash_residue_cleared_by_completed_cash_free_window(self):
        # a completed summary over a window with ZERO kind="cash" drift rows
        # clears the residue (sound per RC-8: a skip re-journals, so a cash-free
        # completed window means evaluated-clean or no residue existed)
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledger(tmpdir)
            ledger.record_reconcile(**_cash_drift_kwargs())
            ledger.record_reconcile_run(**_run_kwargs(drift_count=1, clean=False))
            ledger.record_reconcile(**_drift_kwargs(reconcile_id="rc-2",
                                                    drift_id="rd-9"))
            ledger.record_reconcile_run(**_run_kwargs(
                reconcile_id="rc-2", drift_count=1, clean=False))
            state = self._fold(path)
            self.assertIsNone(state["outstanding_cash_residue"])
            self.assertIs(state["latched"], True)  # position drift still latches

    def test_fold_raises_on_out_of_vocab_event_type(self):
        # fail-closed fold over a closed event vocabulary (the V5 posture)
        with self.assertRaises(ReconcileError):
            rehydrate_reconcile_state([{"event_type": "bogus", "seq": 1}])


if __name__ == "__main__":
    unittest.main()
