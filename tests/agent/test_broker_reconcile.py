"""M6 §I file 1 — broker_reconcile: frozen §3 vocabularies, `ReconcileError`,
the §A dataclasses/constants, the `make_finding` typed boundary (M6C-17,
FD-M6-10), and W2's PURE diff core.

Wave-1 cases (§J W1): 1 (out-of-vocab kind/action/note/phase raise
`ReconcileError`, nothing constructed), 21 and 22 where they apply to wave-1
surfaces (hostile ambient Decimal context immunity of `make_finding`; the
None-coerced sort keys, M6C-33), and 23 (ModeledUSD/float/bool/NaN into any
money/qty slot of `make_finding` => TypeError, nothing constructed). Wave 2
adds cases 2-20 and 24-26 for `diff_positions`, `diff_cash`,
`resolve_order_probe`, and `identity_note`.

Invariants: S5, S2, DET.
"""
import decimal
import unittest
from dataclasses import dataclass
from dataclasses import FrozenInstanceError
from decimal import Context, Decimal, ROUND_DOWN, ROUND_HALF_EVEN

import agent.broker_reconcile as br
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
from agent.broker.order_state import BrokerOrder
from agent.exec_reasons import ORDER_STATES, ExecError
from agent.serializer import BrokerUSD, ModeledUSD
from agent.risk.account_state import PortfolioRead, PositionRead


@dataclass(frozen=True)
class _Pos:
    position_id: str
    symbol: str
    qty: Decimal
    broker_cost_usd: BrokerUSD


def _pos(position_id, symbol, qty, cost):
    return _Pos(position_id=position_id, symbol=symbol, qty=Decimal(qty),
                broker_cost_usd=BrokerUSD(cost))


def _portfolio(*rows):
    positions = tuple(sorted((
        PositionRead(symbol=symbol, qty=Decimal(qty),
                     market_value=BrokerUSD(market_value),
                     avg_entry_price=None if avg_entry_price is None
                     else Decimal(avg_entry_price))
        for symbol, qty, market_value, avg_entry_price in rows
    ), key=lambda p: p.symbol))
    return PortfolioRead(positions=positions, source="fixture", seen_at_ms=1,
                         stale=False)


def _diff(local_positions, portfolio, **overrides):
    opts = dict(frozen_symbols=frozenset(), inflight_symbols=frozenset(),
                adjusts_allowed=True, reconcile_id="rc-test")
    opts.update(overrides)
    return br.diff_positions(local_positions, portfolio, **opts)


def _order(state, *, filled_qty="0", qty="10", symbol="AAPL",
           client_order_id="o-1", broker_order_id="bo-1",
           ts_broker_utc="2026-06-10T14:30:00Z"):
    return BrokerOrder(broker_order_id=broker_order_id,
                       client_order_id=client_order_id, symbol=symbol,
                       side="buy", state=state, raw_status=state,
                       qty=Decimal(qty), filled_qty=Decimal(filled_qty),
                       filled_avg_price=Decimal("10") if filled_qty != "0"
                       else None, limit_price=None,
                       ts_broker_utc=ts_broker_utc, source="fixture")


def _local_order(event_type="order_submitted", *, state="accepted",
                 order_id="o-1", decision_id="d-1", symbol="AAPL",
                 broker_order_id="bo-1"):
    row = {"event_type": event_type, "order_id": order_id,
           "decision_id": decision_id, "symbol": symbol,
           "broker_order_id": broker_order_id}
    if state is not None:
        row["state"] = state
    if event_type == "order_terminal":
        row["terminal_state"] = state
    return row


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


class TestDiffPositionsCore(unittest.TestCase):
    def test_union_absence_and_short_semantics(self):
        local = {"AAPL": (_pos("pos-a", "AAPL", "10", "100"),)}
        portfolio = _portfolio(("MSFT", "5", "50", "10"),
                               ("TSLA", "-5", "-50", "10"))
        findings, plans, notes = _diff(local, portfolio)

        self.assertEqual([f.kind for f in findings], [
            "position_missing_broker",
            "position_unknown_broker",
            "short_unrepresentable",
        ])
        self.assertEqual([f.action for f in findings], [
            "adjusted", "latched_operator", "latched_operator"])
        self.assertEqual([(p.position_id, p.adjusted_qty) for p in plans],
                         [("pos-a", Decimal("0"))])
        self.assertTrue(all(p.adjusted_qty >= 0 for p in plans))
        self.assertEqual(notes, (("cost_unverifiable", "AAPL", ""),))

        empty_findings, empty_plans, empty_notes = _diff({}, _portfolio())
        self.assertEqual((empty_findings, empty_plans, empty_notes), ((), (), ()))

    def test_multi_position_lifo_cascade_and_fixpoint(self):
        local = {
            "AAPL": (_pos("pos-new", "AAPL", "10", "100"),
                     _pos("pos-old", "AAPL", "5", "50")),
        }
        findings, plans, notes = _diff(
            local, _portfolio(("AAPL", "3", "30", "10")))

        self.assertEqual([(f.kind, f.action, f.local, f.broker)
                          for f in findings],
                         [("position_qty", "adjusted", "15", "3")])
        self.assertEqual([(p.position_id, p.prev_qty, p.adjusted_qty,
                           p.adjusted_broker_cost_usd) for p in plans],
                         [("pos-new", Decimal("10"), Decimal("0"),
                           BrokerUSD("0")),
                          ("pos-old", Decimal("5"), Decimal("3"),
                           BrokerUSD("30"))])
        self.assertEqual(notes, ())

        post = {"AAPL": (_pos("pos-old", "AAPL", "3", "30"),)}
        self.assertEqual(_diff(post, _portfolio(("AAPL", "3", "30", "10"))),
                         ((), (), ()))

    def test_positive_delta_accrues_to_newest(self):
        local = {
            "AAPL": (_pos("pos-new", "AAPL", "10", "100"),
                     _pos("pos-old", "AAPL", "5", "50")),
        }
        findings, plans, notes = _diff(
            local, _portfolio(("AAPL", "20", "200", "10")))

        self.assertEqual([(f.kind, f.action) for f in findings],
                         [("position_qty", "adjusted")])
        self.assertEqual([(p.position_id, p.adjusted_qty,
                           p.adjusted_broker_cost_usd) for p in plans],
                         [("pos-new", Decimal("15"), BrokerUSD("150"))])
        self.assertEqual(notes, ())

    def test_frozen_inflight_and_immediate_defer_without_plans(self):
        local = {"AAPL": (_pos("pos-a", "AAPL", "10", "100"),)}
        portfolio = _portfolio(("AAPL", "12", "120", "10"))

        frozen = _diff(local, portfolio, frozen_symbols=frozenset({"AAPL"}))
        self.assertEqual([(f.kind, f.action) for f in frozen[0]],
                         [("position_qty", "latched_operator")])
        self.assertEqual(frozen[1:], ((), ()))

        inflight = _diff(local, portfolio, inflight_symbols=frozenset({"AAPL"}))
        self.assertEqual([(f.kind, f.action) for f in inflight[0]],
                         [("position_qty", "adjust_deferred")])
        self.assertEqual(inflight[1], ())
        self.assertEqual(inflight[2],
                         (("adjust_deferred_inflight", "AAPL", ""),))

        immediate = _diff(local, portfolio, adjusts_allowed=False)
        self.assertEqual([(f.kind, f.action) for f in immediate[0]],
                         [("position_qty", "adjust_deferred")])
        self.assertEqual(immediate[1:], ((), ()))

    def test_decimal_value_compare_and_cost_tolerance_boundary(self):
        local = {"AAPL": (_pos("pos-a", "AAPL", "1.000000000", "100"),)}
        self.assertEqual(_diff(local, _portfolio(("AAPL", "1", "100", "100"))),
                         ((), (), ()))

        boundary = {"AAPL": (_pos("pos-a", "AAPL", "10", "100.05"),)}
        self.assertEqual(
            _diff(boundary, _portfolio(("AAPL", "10", "100", "10"))),
            ((), (), ()))

        over = {"AAPL": (_pos("pos-a", "AAPL", "10", "100.0500001"),)}
        findings, plans, notes = _diff(
            over, _portfolio(("AAPL", "10", "100", "10")))
        self.assertEqual([(f.kind, f.field, f.action) for f in findings],
                         [("position_avg_cost", "avg_cost", "adjusted")])
        self.assertEqual([(p.position_id, p.adjusted_qty,
                           p.adjusted_broker_cost_usd) for p in plans],
                         [("pos-a", Decimal("10"), BrokerUSD("100"))])
        self.assertEqual(notes, ())

    def test_avg_entry_none_keeps_qty_adjusts_and_notes(self):
        local = {"AAPL": (_pos("pos-a", "AAPL", "10", "100"),)}
        findings, plans, notes = _diff(
            local, _portfolio(("AAPL", "12", "120", None)))
        self.assertEqual([(f.kind, f.action) for f in findings],
                         [("position_qty", "adjusted")])
        self.assertEqual([(p.position_id, p.adjusted_qty,
                           p.adjusted_broker_cost_usd) for p in plans],
                         [("pos-a", Decimal("12"), BrokerUSD("100"))])
        self.assertEqual(notes, (("cost_unverifiable", "AAPL", ""),))

    def test_reanchor_moves_to_newest_open_lot_after_boundary_exhaustion(self):
        local = {
            "AAPL": (_pos("pos-new", "AAPL", "10", "100"),
                     _pos("pos-old", "AAPL", "5", "50")),
        }
        findings, plans, notes = _diff(
            local, _portfolio(("AAPL", "5", "60", "12")))
        self.assertEqual([(f.kind, f.action) for f in findings],
                         [("position_qty", "adjusted")])
        self.assertEqual([(p.position_id, p.adjusted_qty,
                           p.adjusted_broker_cost_usd) for p in plans],
                         [("pos-new", Decimal("0"), BrokerUSD("0")),
                          ("pos-old", Decimal("5"), BrokerUSD("60"))])
        self.assertEqual(notes, ())

    def test_no_adjusted_position_qty_finding_without_plan(self):
        local = {"AAPL": (_pos("pos-a", "AAPL", "10", "100"),)}
        for inflight, allowed, expected_action in (
                (frozenset(), True, "adjusted"),
                (frozenset({"AAPL"}), True, "adjust_deferred"),
                (frozenset(), False, "adjust_deferred")):
            findings, plans, _notes = _diff(
                local, _portfolio(("AAPL", "12", "120", "10")),
                inflight_symbols=inflight, adjusts_allowed=allowed)
            self.assertEqual(findings[0].action, expected_action)
            if expected_action == "adjusted":
                self.assertGreaterEqual(len(plans), 1)
            else:
                self.assertEqual(plans, ())


class TestDiffCashCore(unittest.TestCase):
    def test_exact_telescope_and_first_fill_id_dedupe(self):
        rows = (
            {"event_type": "broker_fill", "seq": 11, "fill_id": "bf-1",
             "side": "buy", "delta_cost_usd": "25"},
            {"event_type": "broker_fill", "seq": 12, "fill_id": "bf-1",
             "side": "buy", "delta_cost_usd": "25"},
            {"event_type": "broker_fill", "seq": 13, "fill_id": "bf-2",
             "side": "sell", "delta_cost_usd": "10"},
        )
        self.assertIsNone(br.diff_cash(baseline_cash=BrokerUSD("100"),
                                       fill_rows_since_watermark=rows,
                                       broker_cash=BrokerUSD("85"),
                                       reconcile_id="rc-cash"))

    def test_cash_residue_latches_with_expected_and_broker_values(self):
        rows = ({"event_type": "broker_fill", "seq": 11, "fill_id": "bf-1",
                 "side": "buy", "delta_cost_usd": "25"},)
        finding = br.diff_cash(baseline_cash=BrokerUSD("100"),
                               fill_rows_since_watermark=rows,
                               broker_cash=BrokerUSD("70"),
                               reconcile_id="rc-cash")
        self.assertEqual((finding.kind, finding.field, finding.local,
                          finding.broker, finding.diff, finding.action),
                         ("cash", "cash", "75", "70", "-5",
                          "latched_operator"))


class TestResolveOrderProbeCore(unittest.TestCase):
    def test_terminal_filled_ahead_emits_fills_and_terminal_resolution(self):
        resolution = br.resolve_order_probe(
            _local_order(), _order("filled", filled_qty="7"),
            cum_filled_watermark=Decimal("5"), session_over=False,
            flatten_symbol=None, reconcile_id="rc-order")
        self.assertEqual([(f.kind, f.action, f.local, f.broker)
                          for f in resolution.findings],
                         [("fills_missed", "alert_only", "5", "7"),
                          ("order_state", "resolved_terminal", "open",
                           "filled")])
        self.assertEqual(resolution.terminal_resolutions,
                         (TerminalResolution(decision_id="d-1", order_id="o-1",
                                             terminal_state="filled",
                                             filled_qty=Decimal("7"),
                                             cum_notional_usd=BrokerUSD("70"),
                                             ts_broker_utc=(
                                                 "2026-06-10T14:30:00Z")),))
        self.assertEqual(resolution.defer_symbols, ())

    def test_terminal_and_live_order_resolution_table(self):
        canceled = br.resolve_order_probe(
            _local_order(), _order("canceled"),
            cum_filled_watermark=Decimal("0"), session_over=False,
            flatten_symbol=None, reconcile_id="rc-order")
        self.assertEqual([(f.kind, f.action) for f in canceled.findings],
                         [("order_state", "resolved_terminal")])
        self.assertEqual(canceled.terminal_resolutions[0].terminal_state,
                         "canceled")

        live = br.resolve_order_probe(
            _local_order(), _order("accepted"),
            cum_filled_watermark=Decimal("0"), session_over=False,
            flatten_symbol=None, reconcile_id="rc-order")
        self.assertEqual(live.findings, ())
        self.assertEqual(live.defer_symbols, ("AAPL",))

        partial = br.resolve_order_probe(
            _local_order(), _order("partially_filled", filled_qty="3"),
            cum_filled_watermark=Decimal("1"), session_over=False,
            flatten_symbol=None, reconcile_id="rc-order")
        self.assertEqual([(f.kind, f.action) for f in partial.findings],
                         [("fills_missed", "alert_only")])
        self.assertEqual(partial.terminal_resolutions, ())
        self.assertEqual(partial.defer_symbols, ("AAPL",))

    def test_not_found_branches_for_confirmed_and_unconfirmed_orders(self):
        confirmed = br.resolve_order_probe(
            _local_order("order_submitted"), NOT_FOUND,
            cum_filled_watermark=Decimal("0"), session_over=True,
            flatten_symbol=None, reconcile_id="rc-order")
        self.assertEqual([(f.kind, f.action, f.broker)
                          for f in confirmed.findings],
                         [("order_state", "latched_operator", "not_found")])
        self.assertEqual(confirmed.terminal_resolutions, ())
        self.assertEqual(confirmed.defer_symbols, ("AAPL",))

        expired = br.resolve_order_probe(
            _local_order("order_submit_unconfirmed", state="unconfirmed"),
            NOT_FOUND, cum_filled_watermark=Decimal("2"),
            session_over=True, flatten_symbol=None, reconcile_id="rc-order")
        self.assertEqual([(f.kind, f.action) for f in expired.findings],
                         [("order_state", "resolved_terminal")])
        self.assertEqual(expired.terminal_resolutions,
                         (TerminalResolution(decision_id="d-1", order_id="o-1",
                                             terminal_state="expired",
                                             filled_qty=Decimal("2"),
                                             cum_notional_usd=BrokerUSD("0"),
                                             ts_broker_utc=None),))
        self.assertEqual(expired.defer_symbols, ())

        presumed_live = br.resolve_order_probe(
            _local_order("order_submit_unconfirmed", state="unconfirmed"),
            NOT_FOUND, cum_filled_watermark=Decimal("0"),
            session_over=False, flatten_symbol=None, reconcile_id="rc-order")
        self.assertEqual(presumed_live.findings, ())
        self.assertEqual(presumed_live.terminal_resolutions, ())
        self.assertEqual(presumed_live.defer_symbols, ("AAPL",))

    def test_unknown_probe_and_terminal_local_defensive_case(self):
        unknown = br.resolve_order_probe(
            _local_order(), _order("pending_cancel"),
            cum_filled_watermark=Decimal("0"), session_over=False,
            flatten_symbol=None, reconcile_id="rc-order")
        self.assertEqual(unknown.findings, ())
        self.assertEqual(unknown.notes,
                         (("order_probe_unknown", "AAPL", "pending_cancel"),))
        self.assertEqual(unknown.defer_symbols, ("AAPL",))

        defensive = br.resolve_order_probe(
            _local_order("order_terminal", state="filled"), _order("accepted"),
            cum_filled_watermark=Decimal("10"), session_over=False,
            flatten_symbol=None, reconcile_id="rc-order")
        self.assertEqual([(f.kind, f.action, f.local, f.broker)
                          for f in defensive.findings],
                         [("order_state", "latched_operator", "filled",
                           "accepted")])

    def test_probe_failed_and_flatten_probe_rows(self):
        failed = br.resolve_order_probe(
            _local_order(), PROBE_FAILED, cum_filled_watermark=Decimal("0"),
            session_over=False, flatten_symbol=None, reconcile_id="rc-order")
        self.assertEqual(failed.findings, ())
        self.assertEqual(failed.notes,
                         (("order_probe_failed", "AAPL", ""),))
        self.assertEqual(failed.defer_symbols, ("AAPL",))

        flat_done = br.resolve_order_probe(
            None, _order("filled", symbol="AAPL", client_order_id="flatten-AAPL",
                         broker_order_id="flatten-AAPL", filled_qty="10"),
            cum_filled_watermark=Decimal("0"), session_over=False,
            flatten_symbol="AAPL", reconcile_id="rc-order")
        self.assertEqual(flat_done.findings, ())
        self.assertEqual(flat_done.terminal_resolutions, ())
        self.assertEqual(flat_done.notes,
                         (("flatten_probe_result", "AAPL", "AAPL:filled"),))
        self.assertEqual(flat_done.defer_symbols, ())

        flat_live = br.resolve_order_probe(
            None, _order("accepted", symbol="AAPL",
                         client_order_id="flatten-AAPL",
                         broker_order_id="flatten-AAPL"),
            cum_filled_watermark=Decimal("0"), session_over=False,
            flatten_symbol="AAPL", reconcile_id="rc-order")
        self.assertEqual(flat_live.defer_symbols, ("AAPL",))

        flat_failed = br.resolve_order_probe(
            None, PROBE_FAILED, cum_filled_watermark=Decimal("0"),
            session_over=False, flatten_symbol="AAPL",
            reconcile_id="rc-order")
        self.assertEqual(flat_failed.notes,
                         (("order_probe_failed", "AAPL", ""),))
        self.assertEqual(flat_failed.defer_symbols, ("AAPL",))


class TestIdentityNoteCore(unittest.TestCase):
    def test_identity_note_cent_boundary(self):
        self.assertIsNone(br.identity_note(equity=BrokerUSD("110.01"),
                                           cash=BrokerUSD("100.00"),
                                           market_values=(BrokerUSD("10.00"),)))
        self.assertEqual(
            br.identity_note(equity=BrokerUSD("110.02"),
                             cash=BrokerUSD("100.00"),
                             market_values=(BrokerUSD("10.00"),)),
            ("broker_internal_inconsistency", None,
             "equity=110.02 cash_plus_market_value=110.00 diff=0.02"))


if __name__ == "__main__":
    unittest.main()
