"""M6 §I file 1 (wave-1 half) — broker_reconcile: the frozen §3 vocabularies,
`ReconcileError`, the §A dataclasses/constants, and the `make_finding` typed
boundary (M6C-17, FD-M6-10).

Wave-1 cases (§J W1): 1 (out-of-vocab kind/action/note/phase raise
`ReconcileError`, nothing constructed), 21 and 22 where they apply to wave-1
surfaces (hostile ambient Decimal context immunity of `make_finding`; the
None-coerced sort keys, M6C-33), and 23 (ModeledUSD/float/bool/NaN into any
money/qty slot of `make_finding` => TypeError, nothing constructed). The diff
core (`diff_positions`/`diff_cash`/`resolve_order_probe`/`identity_note` —
cases 2-20, 24-26) is WAVE 2 and is deliberately absent here (no placeholders).

Invariants: S5, S2, DET.
"""
import decimal
import unittest
from dataclasses import FrozenInstanceError
from decimal import Context, Decimal, ROUND_DOWN, ROUND_HALF_EVEN

from agent.broker_reconcile import (
    COST_TOLERANCE_PER_SHARE,
    DRIFT_KINDS,
    FINDING_FIELDS,
    IDENTITY_TOLERANCE_USD,
    NOT_FOUND,
    ORDER_STATE_TOKENS,
    PROBE_FAILED,
    RECONCILE_ACTIONS,
    RECONCILE_NOTES,
    RECONCILE_PHASES,
    DriftFinding,
    PlannedAdjust,
    ProbeResolution,
    ReconcileError,
    ReconcilePassResult,
    TerminalResolution,
    _DECIMAL_CTX,
    _finding_key,
    _note_key,
    canonical_decimal_str,
    make_finding,
    require_action,
    require_kind,
    require_note,
    require_phase,
)
from agent.exec_reasons import ORDER_STATES, ExecError
from agent.serializer import BrokerUSD, ModeledUSD


def _qty_finding(**over):
    base = dict(kind="position_qty", symbol="AAPL", field="qty",
                local=Decimal("10"), broker=Decimal("12"), action="adjusted")
    base.update(over)
    return make_finding(**base)


class TestVocabularies(unittest.TestCase):
    def test_closed_sets_pinned_exactly(self):
        # §3 verbatim — the frozen vocabularies
        self.assertEqual(RECONCILE_PHASES,
                         frozenset({"sod", "eod", "immediate", "cli"}))
        self.assertEqual(DRIFT_KINDS, frozenset({
            "position_qty", "position_avg_cost", "position_unknown_broker",
            "position_missing_broker", "short_unrepresentable", "cash",
            "order_state", "fills_missed", "ca_silent_adjust"}))
        self.assertEqual(RECONCILE_ACTIONS, frozenset({
            "adjusted", "adjust_deferred", "resolved_terminal", "alert_only",
            "rebaselined", "latched_operator", "frozen_immediate"}))
        self.assertEqual(RECONCILE_NOTES, frozenset({
            "cost_unverifiable", "durable_id_missing", "broker_read_failed",
            "order_probe_failed", "order_probe_unknown",
            "broker_internal_inconsistency", "adjust_deferred_inflight",
            "cash_skipped_inflight", "baseline_seeded",
            "reconcile_skipped_no_broker", "flatten_probe_result"}))
        # §B.1a closed field set + the §3-table-derived order-state tokens
        self.assertEqual(FINDING_FIELDS, frozenset({
            "qty", "avg_cost", "cash", "order_state", "fills"}))
        self.assertEqual(ORDER_STATE_TOKENS,
                         frozenset(ORDER_STATES)
                         | frozenset({"not_found", "open", "unconfirmed"}))

    def test_out_of_vocab_raises_reconcile_error(self):
        # case 1 — kind/action/note/phase each raise; members pass through
        for guard, member in ((require_phase, "sod"),
                              (require_kind, "position_qty"),
                              (require_action, "adjusted"),
                              (require_note, "cost_unverifiable")):
            self.assertEqual(guard(member), member)
            with self.assertRaises(ReconcileError):
                guard("bogus")
            with self.assertRaises(ReconcileError):
                guard(None)

    def test_reconcile_error_is_exec_error(self):
        self.assertTrue(issubclass(ReconcileError, ExecError))
        self.assertTrue(issubclass(ReconcileError, ValueError))

    def test_constants_pinned(self):
        # §F — code constants, no config (FD-M6-5)
        self.assertEqual(COST_TOLERANCE_PER_SHARE, Decimal("0.005"))
        self.assertEqual(IDENTITY_TOLERANCE_USD, Decimal("0.01"))
        self.assertEqual(_DECIMAL_CTX.prec, 28)
        self.assertEqual(_DECIMAL_CTX.rounding, ROUND_HALF_EVEN)
        # §A probe sentinels: distinct opaque objects
        self.assertIsNot(NOT_FOUND, PROBE_FAILED)
        self.assertIsNotNone(NOT_FOUND)
        self.assertIsNotNone(PROBE_FAILED)


class TestMakeFindingTypedBoundary(unittest.TestCase):
    # case 23 (+ the case-1 make_finding arm) — the ONE typed boundary (M6C-17)

    def test_out_of_vocab_kind_action_field_nothing_constructed(self):
        with self.assertRaises(ReconcileError):
            _qty_finding(kind="bogus")
        with self.assertRaises(ReconcileError):
            _qty_finding(action="bogus")
        with self.assertRaises(ReconcileError):
            _qty_finding(field="bogus")

    def test_modeled_usd_raises_typeerror_in_money_and_qty_slots(self):
        # FD-M6-10: the lineage wall lives HERE (erased at stringification)
        for slot in ("local", "broker"):
            with self.assertRaises(TypeError):
                _qty_finding(**{slot: ModeledUSD("10")})
            with self.assertRaises(TypeError):
                make_finding(kind="cash", symbol=None, field="cash",
                             local=BrokerUSD("10") if slot != "local"
                             else ModeledUSD("10"),
                             broker=BrokerUSD("10") if slot != "broker"
                             else ModeledUSD("10"),
                             action="latched_operator")

    def test_float_bool_nan_and_untyped_money_raise_typeerror(self):
        with self.assertRaises(TypeError):
            _qty_finding(local=10.0)                       # float
        with self.assertRaises(TypeError):
            _qty_finding(broker=True)                      # bool
        with self.assertRaises(TypeError):
            _qty_finding(local=Decimal("NaN"))             # non-finite qty
        with self.assertRaises(TypeError):
            make_finding(kind="cash", symbol=None, field="cash",
                         local=Decimal("NaN"), broker=BrokerUSD("1"),
                         action="latched_operator")        # NaN money slot
        with self.assertRaises(TypeError):
            make_finding(kind="cash", symbol=None, field="cash",
                         local=Decimal("10"), broker=BrokerUSD("9"),
                         action="latched_operator")        # plain Decimal money
        with self.assertRaises(TypeError):
            _qty_finding(local=10)                         # int is not Decimal

    def test_money_renders_canonical_strings_and_diff(self):
        f = make_finding(kind="cash", symbol=None, field="cash",
                         local=BrokerUSD("1000.00"), broker=BrokerUSD("990.00"),
                         action="latched_operator")
        self.assertIsInstance(f, DriftFinding)
        self.assertEqual(f.local, "1000.00")
        self.assertEqual(f.broker, "990.00")
        self.assertEqual(f.diff, "-10.00")                 # broker - local
        self.assertEqual(f.kind, "cash")
        self.assertIsNone(f.symbol)
        self.assertIsNone(f.position_id)

    def test_qty_decimal_values_render_and_diff(self):
        f = _qty_finding(position_id="pos-1")
        self.assertEqual((f.local, f.broker, f.diff), ("10", "12", "2"))
        self.assertEqual(f.position_id, "pos-1")
        # rendering goes through the serializer path, never str() ad hoc
        self.assertEqual(canonical_decimal_str(Decimal("1.50")), "1.50")

    def test_none_side_and_state_tokens_yield_none_diff(self):
        f = _qty_finding(kind="position_unknown_broker", local=None)
        self.assertIsNone(f.local)
        self.assertEqual(f.broker, "12")
        self.assertIsNone(f.diff)
        g = make_finding(kind="order_state", symbol="MSFT", field="order_state",
                         local="open", broker="filled",
                         action="resolved_terminal", local_order_id="o-1")
        self.assertEqual((g.local, g.broker), ("open", "filled"))
        self.assertIsNone(g.diff)

    def test_state_tokens_closed_and_order_state_only(self):
        with self.assertRaises(ReconcileError):
            _qty_finding(local="open")                     # token on a qty row
        with self.assertRaises(ReconcileError):
            make_finding(kind="order_state", symbol="MSFT",
                         field="order_state", local="vibes", broker="filled",
                         action="resolved_terminal")       # out-of-vocab token
        for token in sorted(ORDER_STATE_TOKENS):           # every member legal
            f = make_finding(kind="order_state", symbol="MSFT",
                             field="order_state", local=token, broker=token,
                             action="latched_operator")
            self.assertEqual(f.local, token)

    def test_symbol_none_only_for_cash(self):
        with self.assertRaises(ReconcileError):
            _qty_finding(symbol=None)
        with self.assertRaises(ReconcileError):
            _qty_finding(symbol="")
        f = make_finding(kind="cash", symbol=None, field="cash",
                         local=BrokerUSD("1"), broker=BrokerUSD("2"),
                         action="latched_operator")
        self.assertIsNone(f.symbol)

    def test_finding_is_frozen(self):
        f = _qty_finding()
        with self.assertRaises(FrozenInstanceError):
            f.kind = "cash"

    def test_result_dataclasses_frozen_shapes(self):
        # §A pinned shapes (M6C-30) — constructible and frozen
        res = TerminalResolution(decision_id="d-1", order_id="o-1",
                                 terminal_state="expired",
                                 filled_qty=Decimal("0"),
                                 cum_notional_usd=BrokerUSD("0"),
                                 ts_broker_utc=None)
        probe = ProbeResolution(findings=(), notes=(),
                                terminal_resolutions=(res,),
                                defer_symbols=("AAPL",))
        plan = PlannedAdjust(position_id="pos-1", symbol="AAPL",
                             prev_qty=Decimal("10"), adjusted_qty=Decimal("12"),
                             prev_broker_cost_usd=BrokerUSD("1000"),
                             adjusted_broker_cost_usd=BrokerUSD("1200"))
        result = ReconcilePassResult(reconcile_id="rc-1", phase="cli",
                                     session_date_et="2026-06-10",
                                     completed=True, findings=(),
                                     adjustments=(plan,), notes=(), clean=False)
        self.assertEqual(probe.terminal_resolutions[0].terminal_state, "expired")
        self.assertEqual(result.adjustments[0].symbol, "AAPL")
        for frozen in (res, probe, plan, result):
            with self.assertRaises(FrozenInstanceError):
                frozen.bogus = 1


class TestEngineDeterminism(unittest.TestCase):
    def test_hostile_ambient_context_byte_identical_outputs(self):
        # case 21 (wave-1 surface): make_finding's diff arithmetic runs under
        # the pinned _DECIMAL_CTX, never the ambient context (M4-DET-1)
        kwargs = dict(kind="position_avg_cost", symbol="AAPL", field="avg_cost",
                      local=BrokerUSD("123.456789"),
                      broker=BrokerUSD("987.654321"), action="adjusted",
                      position_id="pos-1")
        baseline = make_finding(**kwargs)
        original = decimal.getcontext()
        try:
            decimal.setcontext(Context(prec=3, rounding=ROUND_DOWN))
            hostile = make_finding(**kwargs)
        finally:
            decimal.setcontext(original)
        self.assertEqual(hostile, baseline)
        self.assertEqual(hostile.diff, "864.197532")  # needs prec 9 > 3

    def test_sort_keys_none_coerced_total_order(self):
        # case 22 (wave-1 surface): the §A.2 None-coerced keys (M6C-33) —
        # None-bearing slots mixed with str ones, no TypeError, one total order
        f_cash = make_finding(kind="cash", symbol=None, field="cash",
                              local=BrokerUSD("10"), broker=BrokerUSD("9"),
                              action="latched_operator")
        f_pos_none = _qty_finding()                       # position_id None
        f_pos = _qty_finding(position_id="pos-1")
        f_ord = make_finding(kind="order_state", symbol="MSFT",
                             field="order_state", local="open", broker="filled",
                             action="resolved_terminal", local_order_id="o-1")
        expected = [f_cash, f_ord, f_pos_none, f_pos]
        for permutation in ([f_pos, f_ord, f_cash, f_pos_none],
                            [f_pos_none, f_cash, f_pos, f_ord]):
            self.assertEqual(sorted(permutation, key=_finding_key), expected)
        notes = [("cost_unverifiable", "AAPL", ""),
                 ("cash_skipped_inflight", None, ""),
                 ("broker_read_failed", None, "payload shape")]
        expected_notes = [("broker_read_failed", None, "payload shape"),
                          ("cash_skipped_inflight", None, ""),
                          ("cost_unverifiable", "AAPL", "")]
        for permutation in (notes, list(reversed(notes))):
            self.assertEqual(sorted(permutation, key=_note_key), expected_notes)


if __name__ == "__main__":
    unittest.main()
