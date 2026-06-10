"""M5 §R test 6 — journal/orders|fills|positions.jsonl: validating facade, frozen
§P.2 field sets, money-lineage walls (M5C-S10), deterministic ids (§P.3, M5C-T7),
replay/rehydrate semantics, write-order-spy witness (M5C-T1), four-stream chain
joins (M5C-B8/T9).

Invariants: S2, S3, S5, S6.

NOTE (M5C-T1, per the build brief): the journal-before-mint write-order case is
exercised here at the EVENTWRITER level — a call-order spy over the two writers
asserts the `risk_verdict` write precedes the `order_submit_attempt` write. The
full orchestrator-integration variant (the real decide->verdict->mint->attempt
pipeline) lands with §R 10's wave (`test_orchestrator.py`).
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from agent.broker.order_state import BrokerOrder, FillDelta, fill_delta
from agent.exec_ledger import (
    EVT_BROKER_FILL,
    EVT_BROKER_ORDER_UPDATE,
    EVT_BROKER_REJECT,
    EVT_DIVERGENCE_ALERT,
    EVT_FILL_DIVERGENCE,
    EVT_MARK,
    EVT_MODELED_EXECUTION_FILL,
    EVT_ORDER_STATE_ALERT,
    EVT_ORDER_SUBMIT_ATTEMPT,
    EVT_ORDER_SUBMIT_UNCONFIRMED,
    EVT_ORDER_SUBMITTED,
    EVT_ORDER_TERMINAL,
    EVT_PNL_SNAPSHOT,
    EVT_POSITION_CLOSE,
    EVT_POSITION_OPEN,
    EVT_POST_SUBMIT_CANCEL_ATTEMPT,
    EVT_REJECT,
    EVT_STRATEGY_DECISION,
    EXEC_LEDGER_VERSION,
    STREAM_FILLS,
    STREAM_ORDERS,
    STREAM_POSITIONS,
    ExecLedger,
    as_modeled_usd,
    rehydrate_exec_state,
    replay_fills,
    replay_orders,
    replay_positions,
)
from agent.exec_reasons import ExecError
from agent.journal import _RESERVED, JournalCorruption
from agent.risk.risk_ledger import RiskLedger
from agent.serializer import BrokerUSD, ModeledUSD, dumps, row_hash
from recorder.persistence import EventWriter

_CLOCK = lambda: "2026-06-10T20:00:00.000000+00:00"  # noqa: E731 — byte-determinism

# §P.3-shaped ids (the ledger validates prefixes only; minting is the owners' job).
DEC = "d-decision1"
DEC2 = "d-decision2"
ORD = "o-order1"
ORD2 = "o-order2"
PF = "pf-preflight1"
RV = "rv-verdict1"
MF = "mf-modeled1"
POS = "pos-position1"

_JOURNAL_KEYS = {"event_type", "run_id", "seq", "ts_utc", "hash",
                 "decision_id", "order_id"}


def _ledger(tmpdir, run_id="run-1"):
    base = Path(tmpdir)
    orders = EventWriter(base / "orders.jsonl", run_id, clock=_CLOCK)
    fills = EventWriter(base / "fills.jsonl", run_id, clock=_CLOCK)
    positions = EventWriter(base / "positions.jsonl", run_id, clock=_CLOCK)
    ledger = ExecLedger(orders=orders, fills=fills, positions=positions,
                        rules_hash="rh-test")
    paths = {"orders": base / "orders.jsonl", "fills": base / "fills.jsonl",
             "positions": base / "positions.jsonl"}
    return ledger, paths


def _prov(**overrides):
    d = {"dataset": "EQUS.MINI", "schema": "tbbo",
         "ts_event_utc": "2026-06-10T14:30:00.000001+00:00",
         "ts_recv_utc": "2026-06-10T14:30:00.000002+00:00",
         "seen_at_ms": 1000, "reconnect_epoch": 0, "vendor_seq": 7}
    d.update(overrides)
    return d


def _prov_bh(**overrides):
    d = _prov(book_hash=None)
    d.update(overrides)
    return d


def _fees():
    return {"model_version": "reg_fees_v1", "sec_usd": Decimal("0.00"),
            "taf_usd": Decimal("0.00"), "total_usd": Decimal("0.00")}


def _cur(**overrides):
    base = dict(broker_order_id="b-1", client_order_id=ORD, symbol="AAPL",
                side="buy", state="partially_filled",
                raw_status="partially_filled", qty=Decimal("100"),
                filled_qty=Decimal("30"), filled_avg_price=Decimal("100.10"),
                limit_price=Decimal("100.25"), ts_broker_utc=None, source="fake")
    base.update(overrides)
    return BrokerOrder(**base)


def _delta():
    delta = fill_delta(None, _cur())
    assert isinstance(delta, FillDelta)
    return delta


# Every record_* method: (stream, method, kwargs). All kwargs are REQUIRED
# except record_reject's order_id ("when one existed", §P.2).
def _good_calls():
    return [
        (STREAM_ORDERS, "record_strategy_decision", dict(
            symbol="AAPL", instrument_id=42, strategy_id="synthetic.scripted_v1",
            strategy_kind="synthetic", action="would_open", side="buy",
            qty=Decimal("100"), strategy_limit=None, score=None,
            paper_eligible=True, position_id=None,
            event_basis="2026-06-10T14:30:00-04:00|1m",
            decision_ts_utc="2026-06-10T18:30:00+00:00",
            decision_seen_at_ms=1000, quote_a=_prov(), decision_id=DEC)),
        (STREAM_ORDERS, "record_reject", dict(
            symbol="AAPL", instrument_id=42, strategy_id="synthetic.scripted_v1",
            stage="run_gates", reasons=["run_gates_off"],
            stages_skipped=["kill", "stamp"],
            detail={"risk_reasons": None, "quote_reasons": None,
                    "broker_code": None, "broker_message": None},
            preflight_id=None, capped_limit=None, token_kind="open",
            kill_state="monitoring", kill_generation=0, quote_b=None,
            decision_id=DEC)),
        (STREAM_ORDERS, "record_order_submit_attempt", dict(
            client_order_id=ORD, preflight_id=PF, risk_verdict_id=RV,
            strategy_id="synthetic.scripted_v1", symbol="AAPL", instrument_id=42,
            side="buy", qty=Decimal("100"),
            order_intent={"order_type": "marketable_limit", "tif": "day",
                          "limit_price": Decimal("100.25")},
            token_kind="open", kill_generation=0, quote_b=_prov(),
            decision_id=DEC, order_id=ORD)),
        (STREAM_ORDERS, "record_order_submitted", dict(
            client_order_id=ORD, broker_order_id="b-1", state="accepted",
            raw_status="new", ts_broker_utc=None, source="fake",
            decision_id=DEC, order_id=ORD)),
        (STREAM_ORDERS, "record_broker_order_update", dict(
            broker_order_id="b-1", from_state="accepted",
            to_state="partially_filled", raw_status="partially_filled",
            filled_qty=Decimal("30"), filled_avg_price=Decimal("100.10"),
            ts_broker_utc=None, order_id=ORD)),
        (STREAM_ORDERS, "record_broker_reject", dict(
            broker_order_id=None, http_status=403, broker_code=40310100,
            message="pattern day trading protection", pdt_marker_matched=True,
            decision_id=DEC, order_id=ORD)),
        (STREAM_ORDERS, "record_order_submit_unconfirmed", dict(
            client_order_id=ORD, error="timeout", attempts=3,
            resolution="not_found", order_id=ORD)),
        (STREAM_ORDERS, "record_post_submit_cancel_attempt", dict(
            broker_order_id="b-1", cause="session_end",
            outcome="cancel_submitted", broker_state_at_attempt="accepted",
            order_id=ORD)),
        (STREAM_ORDERS, "record_order_state_alert", dict(
            broker_order_id="b-1", raw_status="suspended",
            note="unknown_status", order_id=ORD)),
        (STREAM_ORDERS, "record_order_terminal", dict(
            terminal_state="filled", filled_qty=Decimal("100"),
            cum_notional_usd=BrokerUSD("10020.00"), ts_broker_utc=None,
            decision_id=DEC, order_id=ORD)),
        (STREAM_FILLS, "record_broker_fill", dict(
            delta=_delta(), cur=_cur(), position_id=None, liquidity_flag=None,
            venue=None, decision_id=DEC, order_id=ORD)),
        (STREAM_FILLS, "record_modeled_execution_fill", dict(
            modeled_fill_id=MF, model="tob_l1_v1", realism_class="modeled_partial",
            requested_qty=Decimal("100"), modeled_fillable_qty=Decimal("40"),
            modeled_vwap=Decimal("10.0260"), worst_price=Decimal("10.0300"),
            touch_price=Decimal("10.01"), levels_consumed=None,
            slippage_vs_mid_bps=Decimal("12.4"),
            modeled_cost_usd=ModeledUSD("1002.60"), fees_assumed=_fees(),
            quote=_prov_bh(), reasons=[], decision_id=DEC, order_id=ORD)),
        (STREAM_FILLS, "record_fill_divergence", dict(
            side="buy", broker_cost_usd=BrokerUSD("701.54"),
            modeled_cost_usd=ModeledUSD("701.30"),
            divergence_usd=Decimal("0.24"), divergence_bps=Decimal("3.4"),
            flag="broker_conservative", order_id=ORD)),
        (STREAM_FILLS, "record_divergence_alert", dict(
            divergence_usd=Decimal("9.99"), divergence_bps=Decimal("14.2"),
            order_id=ORD)),
        (STREAM_POSITIONS, "record_position_open", dict(
            position_id=POS, symbol="AAPL", instrument_id=42, side="long",
            qty=Decimal("100"), broker_cost_usd=BrokerUSD("10020.00"),
            modeled_cost_usd=ModeledUSD("10018.00"), fee_assumption=_fees(),
            opening_order_id=ORD, strategy_id="synthetic.scripted_v1",
            opened_ts_utc="2026-06-10T18:30:05+00:00",
            decision_id=DEC, order_id=ORD)),
        (STREAM_POSITIONS, "record_mark", dict(
            position_id=POS, mark_price=Decimal("100.15"),
            mark_source="best_bid", quote=_prov(),
            unrealized_broker_usd=Decimal("-5.00"),
            unrealized_modeled_usd=None,
            bar_key="2026-06-10T14:31:00-04:00|1m")),
        (STREAM_POSITIONS, "record_pnl_snapshot", dict(
            position_id=POS, broker_account_pnl=BrokerUSD("-5.00"),
            execution_realistic_pnl=ModeledUSD("-7.30"),
            divergence_flag="unassessed",
            bar_key="2026-06-10T14:31:00-04:00|1m")),
        (STREAM_POSITIONS, "record_position_close", dict(
            position_id=POS, closing_order_id=ORD2, exit_qty=Decimal("100"),
            broker_exit_notional_usd=BrokerUSD("10100.00"),
            closed_slice_broker_cost_usd=Decimal("10020.00"),
            residual_broker_cost_usd=Decimal("0.00"),
            closed_slice_modeled_cost_usd=Decimal("10018.00"),
            residual_modeled_cost_usd=Decimal("0.00"),
            realized_broker_pnl=BrokerUSD("80.00"),
            realized_modeled_pnl=ModeledUSD("73.04"), fees_assessed=_fees(),
            reason="strategy_exit", decision_id=DEC2, order_id=ORD2)),
    ]


_OPTIONAL_KWARGS = {"record_reject": {"order_id"}}

# Frozen §P.2 payload field sets (beyond the common v / rules_hash prefix).
_EXPECTED_PAYLOAD = {
    EVT_STRATEGY_DECISION: {
        "symbol", "instrument_id", "strategy_id", "strategy_kind", "action",
        "side", "qty", "strategy_limit", "score", "paper_eligible",
        "position_id", "event_basis", "decision_ts_utc", "decision_seen_at_ms",
        "quote_a"},
    EVT_REJECT: {
        "symbol", "instrument_id", "strategy_id", "stage", "reasons",
        "stages_skipped", "detail", "preflight_id", "capped_limit",
        "token_kind", "kill_state", "kill_generation", "quote_b"},
    EVT_ORDER_SUBMIT_ATTEMPT: {
        "client_order_id", "preflight_id", "risk_verdict_id", "strategy_id",
        "symbol", "instrument_id", "side", "qty", "order_intent", "token_kind",
        "kill_generation", "quote_b"},
    EVT_ORDER_SUBMITTED: {
        "client_order_id", "broker_order_id", "state", "raw_status",
        "ts_broker_utc", "source"},
    EVT_BROKER_ORDER_UPDATE: {
        "broker_order_id", "from_state", "to_state", "raw_status",
        "filled_qty", "filled_avg_price", "ts_broker_utc"},
    EVT_BROKER_REJECT: {
        "broker_order_id", "http_status", "broker_code", "message",
        "pdt_marker_matched"},
    EVT_ORDER_SUBMIT_UNCONFIRMED: {
        "client_order_id", "error", "attempts", "resolution"},
    EVT_POST_SUBMIT_CANCEL_ATTEMPT: {
        "broker_order_id", "cause", "outcome", "broker_state_at_attempt"},
    EVT_ORDER_STATE_ALERT: {"broker_order_id", "raw_status", "note"},
    EVT_ORDER_TERMINAL: {
        "terminal_state", "filled_qty", "cum_notional_usd", "ts_broker_utc"},
    EVT_BROKER_FILL: {
        "fill_id", "broker_order_id", "position_id", "symbol", "side",
        "delta_qty", "delta_cost_usd", "cum_filled_qty",
        "filled_avg_price_after", "cum_notional_after", "liquidity_flag",
        "venue", "ts_broker_utc", "source"},
    EVT_MODELED_EXECUTION_FILL: {
        "modeled_fill_id", "model", "realism_class", "requested_qty",
        "modeled_fillable_qty", "modeled_vwap", "worst_price", "touch_price",
        "levels_consumed", "slippage_vs_mid_bps", "modeled_cost_usd",
        "fees_assumed", "quote", "reasons"},
    EVT_FILL_DIVERGENCE: {
        "side", "broker_cost_usd", "modeled_cost_usd", "divergence_usd",
        "divergence_bps", "flag"},
    EVT_DIVERGENCE_ALERT: {"divergence_usd", "divergence_bps", "threshold_bps"},
    EVT_POSITION_OPEN: {
        "position_id", "symbol", "instrument_id", "side", "qty",
        "broker_cost_usd", "modeled_cost_usd", "fee_assumption",
        "opening_order_id", "strategy_id", "opened_ts_utc"},
    EVT_MARK: {
        "position_id", "mark_price", "mark_source", "quote",
        "unrealized_broker_usd", "unrealized_modeled_usd", "bar_key"},
    EVT_PNL_SNAPSHOT: {
        "position_id", "broker_account_pnl", "execution_realistic_pnl",
        "divergence_flag", "basis", "used_for_strategy_evaluation", "bar_key"},
    EVT_POSITION_CLOSE: {
        "position_id", "closing_order_id", "exit_qty",
        "broker_exit_notional_usd", "closed_slice_broker_cost_usd",
        "residual_broker_cost_usd", "closed_slice_modeled_cost_usd",
        "residual_modeled_cost_usd", "realized_broker_pnl",
        "realized_modeled_pnl", "fees_assessed", "reason"},
}

_REPLAY = {STREAM_ORDERS: replay_orders, STREAM_FILLS: replay_fills,
           STREAM_POSITIONS: replay_positions}


class _RecordSpy:
    """Call-order spy over an EventWriter (the M5C-T1 mechanism witness)."""

    def __init__(self, inner, name, log):
        self._inner = inner
        self._name = name
        self._log = log
        self.captured_fields = []

    def record(self, event_type, fields, *, decision_id=None, order_id=None):
        self._log.append((self._name, event_type))
        self.captured_fields.append(dict(fields))
        return self._inner.record(event_type, fields,
                                  decision_id=decision_id, order_id=order_id)


def _verdict(**overrides):
    base = dict(
        allowed=True, reasons=(), gate_stage=None, stages_skipped=(),
        strategy_id="synthetic.scripted_v1", legs=(),
        gross_notional=Decimal("10025"), caps_used=(),
        account_snapshot_id="as-1", kill_state="monitoring", kill_generation=0,
        session_date_et="2026-06-10", verdict_id=RV)
    base.update(overrides)
    return SimpleNamespace(**base)


class TestRecordRoundTrips(unittest.TestCase):
    def test_every_record_method_round_trips_hash_verified(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            live = {STREAM_ORDERS: [], STREAM_FILLS: [], STREAM_POSITIONS: []}
            for stream, method, kwargs in _good_calls():
                live[stream].append(getattr(ledger, method)(**kwargs))
            for stream, path in paths.items():
                replayed = _REPLAY[stream](path)  # hash-verified
                self.assertEqual(len(replayed), len(live[stream]))
                for live_row, replayed_row in zip(live[stream], replayed):
                    # byte-identical round trip: the canonical serialization of
                    # the returned row equals the re-read row's serialization.
                    self.assertEqual(dumps(live_row), dumps(replayed_row))
                for row in replayed:
                    self.assertEqual(row["v"], EXEC_LEDGER_VERSION)
                    self.assertEqual(row["rules_hash"], "rh-test")

    def test_payload_field_sets_match_p2_exactly(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            for stream, method, kwargs in _good_calls():
                row = getattr(ledger, method)(**kwargs)
                payload = set(row) - _JOURNAL_KEYS
                self.assertEqual(
                    payload, _EXPECTED_PAYLOAD[row["event_type"]] | {"v", "rules_hash"},
                    f"{method}: payload field set drifted from §P.2")

    def test_pinned_literals(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            alert = ledger.record_divergence_alert(
                divergence_usd=Decimal("9.99"), divergence_bps=Decimal("14.2"),
                order_id=ORD)
            self.assertEqual(alert["threshold_bps"], "10")
            snap = ledger.record_pnl_snapshot(
                position_id=POS, broker_account_pnl=BrokerUSD("0.00"),
                execution_realistic_pnl=None, divergence_flag="unassessed",
                bar_key="bk")
            self.assertEqual(snap["basis"], {"broker": "broker_fills",
                                             "modeled": "modeled_fill_plus_fees"})
            self.assertEqual(snap["used_for_strategy_evaluation"],
                             "execution_realistic_pnl")

    def test_reject_reasons_journal_sorted(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            row = ledger.record_reject(
                symbol="AAPL", instrument_id=42, strategy_id="s1",
                stage="quote", reasons=["quote_stale", "quote_crossed"],
                stages_skipped=["market_state", "order", "risk"],
                detail={"risk_reasons": None, "quote_reasons": ["quote_stale", "quote_crossed"],
                        "broker_code": None, "broker_message": None},
                preflight_id=PF, capped_limit=Decimal("100.25"),
                token_kind="open", kill_state="monitoring", kill_generation=0,
                quote_b=_prov(), decision_id=DEC)
            self.assertEqual(row["reasons"], ["quote_crossed", "quote_stale"])
            self.assertEqual(row["detail"]["quote_reasons"],
                             ["quote_crossed", "quote_stale"])
            # stages_skipped preserves ladder order (NOT sorted — §P.2/§2.2)
            self.assertEqual(row["stages_skipped"], ["market_state", "order", "risk"])

    def test_stream_and_version_constants(self):
        self.assertEqual(EXEC_LEDGER_VERSION, 1)
        self.assertEqual(STREAM_ORDERS, "orders")
        self.assertEqual(STREAM_FILLS, "fills")
        self.assertEqual(STREAM_POSITIONS, "positions")
        self.assertEqual(EVT_STRATEGY_DECISION, "strategy_decision")
        self.assertEqual(EVT_ORDER_SUBMIT_ATTEMPT, "order_submit_attempt")
        self.assertEqual(EVT_BROKER_FILL, "broker_fill")
        self.assertEqual(EVT_MODELED_EXECUTION_FILL, "modeled_execution_fill")
        self.assertEqual(EVT_POSITION_CLOSE, "position_close")


class TestFieldSetExactness(unittest.TestCase):
    def test_missing_kwarg_raises_for_every_event_type(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            for stream, method, kwargs in _good_calls():
                optional = _OPTIONAL_KWARGS.get(method, set())
                for name in kwargs:
                    if name in optional:
                        continue
                    short = dict(kwargs)
                    del short[name]
                    with self.assertRaises(TypeError, msg=f"{method} -{name}"):
                        getattr(ledger, method)(**short)

    def test_extra_kwarg_raises_for_every_event_type(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            for stream, method, kwargs in _good_calls():
                extra = dict(kwargs)
                extra["bogus_field"] = 1
                with self.assertRaises(TypeError, msg=f"{method} +bogus"):
                    getattr(ledger, method)(**extra)

    def test_positional_args_refused(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            with self.assertRaises(TypeError):
                ledger.record_order_state_alert("b-1", "weird", "note", ORD)


class TestVocabularyAndShape(unittest.TestCase):
    def _raises(self, method, kwargs, **bad):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            mutated = dict(kwargs)
            mutated.update(bad)
            with self.assertRaises((ExecError, ValueError, TypeError),
                                   msg=f"{method} {bad}"):
                getattr(ledger, method)(**mutated)

    def _kwargs(self, wanted):
        for stream, method, kwargs in _good_calls():
            if method == wanted:
                return kwargs
        raise AssertionError(wanted)

    def test_out_of_vocab_everything_raises(self):
        cases = [
            ("record_strategy_decision", dict(action="open_now")),
            ("record_strategy_decision", dict(strategy_kind="paper")),
            ("record_strategy_decision", dict(side="hold")),
            ("record_reject", dict(stage="vibes")),
            ("record_reject", dict(reasons=["made_up_reason"])),
            ("record_reject", dict(reasons=["ssr_short_blocked"])),  # reserved-in-M5
            ("record_reject", dict(stages_skipped=["vibes"])),
            ("record_reject", dict(token_kind="both")),
            ("record_order_submit_attempt", dict(token_kind="forged")),
            ("record_order_submitted", dict(state="exploded")),
            ("record_order_submitted", dict(source="robinhood")),
            ("record_broker_order_update", dict(from_state="exploded")),
            ("record_broker_order_update", dict(to_state="exploded")),
            ("record_order_submit_unconfirmed", dict(resolution="maybe")),
            ("record_post_submit_cancel_attempt", dict(cause="boredom")),
            ("record_post_submit_cancel_attempt", dict(outcome="shrug")),
            ("record_post_submit_cancel_attempt",
             dict(broker_state_at_attempt="exploded")),
            ("record_order_terminal", dict(terminal_state="accepted")),  # non-terminal
            ("record_modeled_execution_fill", dict(model="psychic_v9")),
            ("record_modeled_execution_fill", dict(realism_class="optimistic")),
            ("record_fill_divergence", dict(flag="fine")),
            ("record_fill_divergence", dict(side="long")),
            ("record_position_open", dict(side="short")),  # §P.2 pins "long"
            ("record_mark", dict(mark_source="last_trade")),
            ("record_pnl_snapshot", dict(divergence_flag="modeled_full")),  # EX-11
            ("record_position_close", dict(reason="felt_like_it")),
        ]
        for method, bad in cases:
            self._raises(method, self._kwargs(method), **bad)

    def test_id_prefix_validation(self):
        cases = [
            ("record_strategy_decision", dict(decision_id="probe-1")),
            ("record_strategy_decision",
             dict(action="would_close", position_id="position-1")),
            ("record_reject", dict(preflight_id="x-1")),
            ("record_order_submit_attempt", dict(order_id="ord-1",
                                                 client_order_id="ord-1")),
            ("record_order_submit_attempt", dict(preflight_id="o-shouldbepf")),
            ("record_order_submit_attempt", dict(risk_verdict_id="verdict-1")),
            ("record_broker_fill", dict(position_id="p-1")),
            ("record_modeled_execution_fill", dict(modeled_fill_id="")),  # unbound
            ("record_position_open", dict(position_id="position-1")),
            ("record_position_open", dict(opening_order_id="b-1")),
            ("record_position_close", dict(closing_order_id="b-1")),
        ]
        for method, bad in cases:
            self._raises(method, self._kwargs(method), **bad)

    def test_synthetic_order_id_prefix_accepted(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            kwargs = dict(self._kwargs("record_order_submit_unconfirmed"))
            kwargs["order_id"] = "synthetic-o-1"
            kwargs["client_order_id"] = "synthetic-o-1"
            row = ledger.record_order_submit_unconfirmed(**kwargs)
            self.assertEqual(row["order_id"], "synthetic-o-1")

    def test_client_order_id_must_equal_order_id(self):
        # FD-M5-7: client_order_id == order_id, frozen.
        for method in ("record_order_submit_attempt", "record_order_submitted",
                       "record_order_submit_unconfirmed"):
            self._raises(method, self._kwargs(method), client_order_id=ORD2)

    def test_would_close_requires_position_id_and_would_open_refuses_it(self):
        self._raises("record_strategy_decision",
                     self._kwargs("record_strategy_decision"),
                     action="would_close", position_id=None)
        self._raises("record_strategy_decision",
                     self._kwargs("record_strategy_decision"),
                     action="would_open", position_id=POS)

    def test_order_intent_pinned_literals(self):
        good = self._kwargs("record_order_submit_attempt")
        self._raises("record_order_submit_attempt", good, order_intent={
            "order_type": "market", "tif": "day",
            "limit_price": Decimal("100.25")})
        self._raises("record_order_submit_attempt", good, order_intent={
            "order_type": "marketable_limit", "tif": "gtc",
            "limit_price": Decimal("100.25")})
        self._raises("record_order_submit_attempt", good, order_intent={
            "order_type": "marketable_limit", "tif": "day",
            "limit_price": None})
        self._raises("record_order_submit_attempt", good, order_intent={
            "order_type": "marketable_limit", "tif": "day"})  # missing key
        self._raises("record_order_submit_attempt", good, order_intent={
            "order_type": "marketable_limit", "tif": "day",
            "limit_price": Decimal("100.25"), "extended_hours": False})

    def test_provenance_exact_key_set(self):
        good_sd = self._kwargs("record_strategy_decision")
        missing = _prov()
        del missing["vendor_seq"]
        self._raises("record_strategy_decision", good_sd, quote_a=missing)
        extra = _prov()
        extra["book_hash"] = None  # book_hash is NOT on the plain provenance dict
        self._raises("record_strategy_decision", good_sd, quote_a=extra)
        self._raises("record_strategy_decision", good_sd,
                     quote_a=_prov(seen_at_ms="1000"))
        # the modeled-fill quote REQUIRES book_hash (§I depth provenance)
        good_mf = self._kwargs("record_modeled_execution_fill")
        self._raises("record_modeled_execution_fill", good_mf, quote=_prov())

    def test_fee_block_exact_key_set(self):
        good = self._kwargs("record_position_open")
        broken = _fees()
        del broken["taf_usd"]
        self._raises("record_position_open", good, fee_assumption=broken)
        extra = _fees()
        extra["tip_usd"] = Decimal("1.00")
        self._raises("record_position_open", good, fee_assumption=extra)

    def test_detail_block_exact_key_set(self):
        good = self._kwargs("record_reject")
        self._raises("record_reject", good, detail={"risk_reasons": None})
        self._raises("record_reject", good, detail={
            "risk_reasons": None, "quote_reasons": None, "broker_code": None,
            "broker_message": None, "extra": 1})

    def test_unassessed_iff_modeled_side_null_on_fill_divergence(self):
        # FD-M5-20: flag == unassessed <=> the modeled side is null.
        good = self._kwargs("record_fill_divergence")
        self._raises("record_fill_divergence", good, modeled_cost_usd=None)
        self._raises("record_fill_divergence", good, flag="unassessed")
        self._raises("record_fill_divergence", good, divergence_usd=None)
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            row = ledger.record_fill_divergence(
                side="buy", broker_cost_usd=BrokerUSD("701.54"),
                modeled_cost_usd=None, divergence_usd=None,
                divergence_bps=None, flag="unassessed", order_id=ORD)
            self.assertEqual(row["flag"], "unassessed")

    def test_float_and_nonfinite_rejected(self):
        self._raises("record_strategy_decision",
                     self._kwargs("record_strategy_decision"), qty=100.0)
        self._raises("record_order_submit_attempt",
                     self._kwargs("record_order_submit_attempt"),
                     qty=Decimal("NaN"))
        self._raises("record_mark", self._kwargs("record_mark"),
                     mark_price=100.15)
        self._raises("record_broker_order_update",
                     self._kwargs("record_broker_order_update"),
                     filled_qty=True)


class TestReservedCollisions(unittest.TestCase):
    def test_no_payload_key_is_reserved(self):
        # Structural: every §P.2 payload key set is disjoint from journal._RESERVED.
        for event_type, payload_keys in _EXPECTED_PAYLOAD.items():
            self.assertEqual((payload_keys | {"v", "rules_hash"}) & _RESERVED,
                             set(), event_type)

    def test_facade_refuses_a_reserved_payload_key(self):
        # White-box: the facade itself refuses a reserved key before the journal.
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            with self.assertRaises(ExecError):
                ledger._record(ledger._orders, "evil", {"seq": 1})
            self.assertEqual(replay_orders(paths["orders"]), [])


class TestMoneyLineageWall(unittest.TestCase):
    def test_as_modeled_usd_guard(self):
        value = ModeledUSD("1.00")
        self.assertIs(as_modeled_usd(value), value)
        with self.assertRaises(TypeError):
            as_modeled_usd(BrokerUSD("1.00"))  # BrokerUSD included (§P.1)
        with self.assertRaises(TypeError):
            as_modeled_usd(Decimal("1.00"))
        with self.assertRaises(TypeError):
            as_modeled_usd(1.0)
        with self.assertRaises(ValueError):
            as_modeled_usd(ModeledUSD("NaN"))

    def test_modeled_usd_into_delta_cost_usd_no_row_written(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            hostile = FillDelta(delta_qty=Decimal("30"),
                                delta_cost_usd=ModeledUSD("3003.00"),
                                cum_filled_qty=Decimal("30"),
                                filled_avg_price_after=Decimal("100.10"))
            with self.assertRaises(TypeError):
                ledger.record_broker_fill(
                    delta=hostile, cur=_cur(), position_id=None,
                    liquidity_flag=None, venue=None,
                    decision_id=DEC, order_id=ORD)
            # NO row written — the stream stays empty (§R 6).
            self.assertFalse(paths["fills"].exists() and
                             paths["fills"].read_bytes() != b"")
            # plain Decimal mirrors (lineage requires the newtype)
            plain = FillDelta(delta_qty=Decimal("30"),
                              delta_cost_usd=Decimal("3003.00"),
                              cum_filled_qty=Decimal("30"),
                              filled_avg_price_after=Decimal("100.10"))
            with self.assertRaises(TypeError):
                ledger.record_broker_fill(
                    delta=plain, cur=_cur(), position_id=None,
                    liquidity_flag=None, venue=None,
                    decision_id=DEC, order_id=ORD)

    def test_broker_usd_into_modeled_slots_raises(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            base = dict(
                modeled_fill_id=MF, model="tob_l1_v1",
                realism_class="modeled_partial", requested_qty=Decimal("100"),
                modeled_fillable_qty=Decimal("40"),
                modeled_vwap=Decimal("10.0260"), worst_price=Decimal("10.0300"),
                touch_price=Decimal("10.01"), levels_consumed=None,
                slippage_vs_mid_bps=Decimal("12.4"),
                modeled_cost_usd=BrokerUSD("1002.60"),  # WRONG lineage
                fees_assumed=_fees(), quote=_prov_bh(), reasons=[],
                decision_id=DEC, order_id=ORD)
            with self.assertRaises(TypeError):
                ledger.record_modeled_execution_fill(**base)
            with self.assertRaises(TypeError):
                ledger.record_fill_divergence(
                    side="buy", broker_cost_usd=BrokerUSD("701.54"),
                    modeled_cost_usd=BrokerUSD("701.30"),
                    divergence_usd=Decimal("0.24"),
                    divergence_bps=Decimal("3.4"),
                    flag="broker_conservative", order_id=ORD)
            with self.assertRaises(TypeError):
                ledger.record_pnl_snapshot(
                    position_id=POS, broker_account_pnl=BrokerUSD("0.00"),
                    execution_realistic_pnl=BrokerUSD("0.00"),
                    divergence_flag="unassessed", bar_key="bk")
            self.assertFalse(paths["fills"].exists() and
                             paths["fills"].read_bytes() != b"")

    def test_broker_lineage_slots_require_broker_usd(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            with self.assertRaises(TypeError):
                ledger.record_order_terminal(
                    terminal_state="filled", filled_qty=Decimal("100"),
                    cum_notional_usd=Decimal("10020.00"),  # plain Decimal
                    ts_broker_utc=None, decision_id=DEC, order_id=ORD)
            with self.assertRaises(TypeError):
                ledger.record_pnl_snapshot(
                    position_id=POS, broker_account_pnl=ModeledUSD("0.00"),
                    execution_realistic_pnl=None,
                    divergence_flag="unassessed", bar_key="bk")
            with self.assertRaises(TypeError):
                ledger.record_position_close(
                    position_id=POS, closing_order_id=ORD2,
                    exit_qty=Decimal("100"),
                    broker_exit_notional_usd=Decimal("10100.00"),  # plain
                    closed_slice_broker_cost_usd=Decimal("10020.00"),
                    residual_broker_cost_usd=Decimal("0.00"),
                    closed_slice_modeled_cost_usd=None,
                    residual_modeled_cost_usd=None,
                    realized_broker_pnl=BrokerUSD("80.00"),
                    realized_modeled_pnl=None, fees_assessed=_fees(),
                    reason="strategy_exit", decision_id=DEC2, order_id=ORD2)
            with self.assertRaises(TypeError):
                ledger.record_position_open(
                    position_id=POS, symbol="AAPL", instrument_id=42,
                    side="long", qty=Decimal("100"),
                    broker_cost_usd=ModeledUSD("10020.00"),  # WRONG lineage
                    modeled_cost_usd=None, fee_assumption=_fees(),
                    opening_order_id=ORD, strategy_id="s1",
                    opened_ts_utc="t", decision_id=DEC, order_id=ORD)

    def test_plain_decimal_divergence_slots_accept_any_decimal(self):
        # §P.2 pins divergence outputs as plain Decimal — the newtypes are
        # Decimal subclasses, so they pass too (no over-enforcement).
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            for value in (Decimal("0.24"), BrokerUSD("0.24"), ModeledUSD("0.24")):
                ledger.record_fill_divergence(
                    side="buy", broker_cost_usd=BrokerUSD("701.54"),
                    modeled_cost_usd=ModeledUSD("701.30"),
                    divergence_usd=value, divergence_bps=Decimal("3.4"),
                    flag="broker_conservative", order_id=ORD)
            self.assertEqual(len(replay_fills(paths["fills"])), 3)


class TestReplaySemantics(unittest.TestCase):
    def test_truncated_tail_tolerated_corrupt_line_fatal_all_three_streams(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            for stream, method, kwargs in _good_calls():
                getattr(ledger, method)(**kwargs)
            for stream, path in paths.items():
                text = path.read_text(encoding="utf-8")
                n = len(_REPLAY[stream](path))
                self.assertGreater(n, 0)
                # truncated (no-newline) tail -> dropped
                path.write_text(text + '{"half', encoding="utf-8")
                self.assertEqual(len(_REPLAY[stream](path)), n)
                # complete corrupt line -> fatal
                path.write_text(text + '{"bad": 1}\n', encoding="utf-8")
                with self.assertRaises(JournalCorruption):
                    _REPLAY[stream](path)
                path.write_text(text, encoding="utf-8")


class TestDeterministicIds(unittest.TestCase):
    def _write_all(self, run_id):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir, run_id=run_id)
            for stream, method, kwargs in _good_calls():
                getattr(ledger, method)(**kwargs)
            return {stream: [json.dumps(r, sort_keys=True)
                             for r in _REPLAY[stream](path)]
                    for stream, path in paths.items()}

    def test_byte_identical_replay_with_same_run_id(self):
        self.assertEqual(self._write_all("run-x"), self._write_all("run-x"))

    def test_fill_id_derivation_and_operand(self):
        # §P.3 / M5C-T7: fill_id = "bf-" + row_hash({order_id,
        # cum_filled_qty_after, cum_notional_after}); cum_notional_after is
        # FROZEN as cur.filled_qty x cur.filled_avg_price, exact.
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            row = ledger.record_broker_fill(
                delta=_delta(), cur=_cur(), position_id=None,
                liquidity_flag=None, venue=None, decision_id=DEC, order_id=ORD)
            expected_notional = _cur().filled_qty * _cur().filled_avg_price
            self.assertEqual(row["cum_notional_after"], expected_notional)
            self.assertEqual(str(row["cum_notional_after"]),
                             str(expected_notional))  # byte-exact operand
            expected_id = "bf-" + row_hash({
                "order_id": ORD,
                "cum_filled_qty_after": _cur().filled_qty,
                "cum_notional_after": expected_notional,
            })
            self.assertEqual(row["fill_id"], expected_id)

    def test_fill_id_stable_under_polling_re_reads(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            first = ledger.record_broker_fill(
                delta=_delta(), cur=_cur(), position_id=None,
                liquidity_flag=None, venue=None, decision_id=DEC, order_id=ORD)
            again = ledger.record_broker_fill(
                delta=_delta(), cur=_cur(), position_id=None,
                liquidity_flag=None, venue=None, decision_id=DEC, order_id=ORD)
            self.assertEqual(first["fill_id"], again["fill_id"])

    def test_fill_ids_distinct_across_the_avg_drift_sequence(self):
        # The §Q cumulative-aggregate sequence: 30@100.10 -> 70@100.18 ->
        # 100@100.20 — FD-M5-18 telescoping, three distinct fill ids, and the
        # last journaled cum_notional_after equals the broker's final qtyxavg.
        snaps = [
            _cur(filled_qty=Decimal("30"), filled_avg_price=Decimal("100.10")),
            _cur(filled_qty=Decimal("70"), filled_avg_price=Decimal("100.18")),
            _cur(filled_qty=Decimal("100"), filled_avg_price=Decimal("100.20"),
                 state="filled", raw_status="filled"),
        ]
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            prev = None
            rows = []
            for cur in snaps:
                delta = fill_delta(prev, cur)
                self.assertIsInstance(delta, FillDelta)
                rows.append(ledger.record_broker_fill(
                    delta=delta, cur=cur, position_id=None, liquidity_flag=None,
                    venue=None, decision_id=DEC, order_id=ORD))
                prev = cur
            ids = [r["fill_id"] for r in rows]
            self.assertEqual(len(set(ids)), 3)
            total = sum(r["delta_cost_usd"] for r in rows)
            self.assertEqual(total, Decimal("100") * Decimal("100.20"))
            self.assertEqual(rows[-1]["cum_notional_after"], Decimal("10020.00"))

    def test_retried_close_decisions_mint_distinct_ids(self):
        # M5C-S5 (pure §P.3 id-derivation): a canceled close retried on a later
        # tick carries a NEW event_basis (the per-tick ms stamp) => new
        # decision_id => new order_id => client_order_id never reused.
        run_id = "run-x"
        strategy_id = "s1"
        ids = {}
        for ms in (1000, 2000):
            basis = "exit:" + POS + ":" + str(ms)
            decision_id = "d-" + row_hash({
                "run_id": run_id, "strategy_id": strategy_id, "symbol": "AAPL",
                "instrument_id": 42, "event_basis": basis})
            order_id = "o-" + row_hash({
                "run_id": run_id, "decision_id": decision_id,
                "preflight_id": None})  # reduce path: no preflight
            ids[ms] = (decision_id, order_id)
        self.assertNotEqual(ids[1000][0], ids[2000][0])
        self.assertNotEqual(ids[1000][1], ids[2000][1])
        # client_order_id == order_id (FD-M5-7) => never reused either.
        self.assertEqual(len({ids[1000][1], ids[2000][1]}), 2)


class TestWriteOrderWitness(unittest.TestCase):
    def test_risk_verdict_write_precedes_order_submit_attempt_write(self):
        # M5C-T1 mechanism witness at the EventWriter level (see module note:
        # the orchestrator-integration variant lands with §R 10's wave). NO
        # cross-stream seq comparison — the two counters are causally unrelated.
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            log = []
            risk_spy = _RecordSpy(EventWriter(base / "risk.jsonl", "run-1",
                                              clock=_CLOCK), "risk", log)
            orders_spy = _RecordSpy(EventWriter(base / "orders.jsonl", "run-1",
                                                clock=_CLOCK), "orders", log)
            risk_ledger = RiskLedger(risk_spy, rules_hash="rh-test")
            exec_ledger = ExecLedger(
                orders=orders_spy,
                fills=EventWriter(base / "fills.jsonl", "run-1", clock=_CLOCK),
                positions=EventWriter(base / "positions.jsonl", "run-1",
                                      clock=_CLOCK),
                rules_hash="rh-test")
            verdict_row = risk_ledger.record_risk_verdict(_verdict(),
                                                          decision_id=DEC)
            attempt_kwargs = dict(
                client_order_id=ORD, preflight_id=PF,
                risk_verdict_id=verdict_row["verdict_id"], strategy_id="s1",
                symbol="AAPL", instrument_id=42, side="buy", qty=Decimal("100"),
                order_intent={"order_type": "marketable_limit", "tif": "day",
                              "limit_price": Decimal("100.25")},
                token_kind="open", kill_generation=0, quote_b=_prov(),
                decision_id=DEC, order_id=ORD)
            attempt = exec_ledger.record_order_submit_attempt(**attempt_kwargs)
            self.assertEqual(log, [("risk", "risk_verdict"),
                                   ("orders", "order_submit_attempt")])
            # the at-rest binding that makes the ordering checkable (FD-M5-30)
            self.assertEqual(attempt["risk_verdict_id"], verdict_row["verdict_id"])
            # "v" is the FIRST payload key on every exec row (§P.1)
            self.assertEqual(next(iter(orders_spy.captured_fields[0])), "v")


class TestWithinStreamSeq(unittest.TestCase):
    def test_strategy_decision_seq_below_order_submit_attempt_seq(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            calls = {m: k for s, m, k in _good_calls()}
            decision = ledger.record_strategy_decision(
                **calls["record_strategy_decision"])
            attempt = ledger.record_order_submit_attempt(
                **calls["record_order_submit_attempt"])
            self.assertLess(decision["seq"], attempt["seq"])


class TestFourStreamChainJoin(unittest.TestCase):
    def test_chain_joins_across_risk_orders_fills_positions(self):
        # S6 / M5C-B8/T9: decision_id -> risk_verdict -> preflight_id ->
        # order_id/client_order_id -> fill_id/modeled_fill_id -> position_id,
        # each row carrying its upstream id across the FOUR streams.
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            risk_ledger = RiskLedger(EventWriter(base / "risk.jsonl", "run-1",
                                                 clock=_CLOCK),
                                     rules_hash="rh-test")
            ledger, paths = _ledger(tmpdir)
            calls = {m: k for s, m, k in _good_calls()}

            verdict_row = risk_ledger.record_risk_verdict(_verdict(),
                                                          decision_id=DEC)
            ledger.record_strategy_decision(**calls["record_strategy_decision"])
            ledger.record_order_submit_attempt(
                **calls["record_order_submit_attempt"])
            ledger.record_order_submitted(**calls["record_order_submitted"])
            ledger.record_broker_order_update(
                **calls["record_broker_order_update"])
            ledger.record_order_terminal(**calls["record_order_terminal"])
            fill = ledger.record_broker_fill(
                delta=_delta(), cur=_cur(), position_id=POS,
                liquidity_flag=None, venue=None, decision_id=DEC, order_id=ORD)
            modeled = ledger.record_modeled_execution_fill(
                **calls["record_modeled_execution_fill"])
            divergence = ledger.record_fill_divergence(
                **calls["record_fill_divergence"])
            opened = ledger.record_position_open(**calls["record_position_open"])
            closed = ledger.record_position_close(
                **calls["record_position_close"])

            from agent.risk.risk_ledger import replay_risk
            risk_rows = replay_risk(base / "risk.jsonl")
            order_rows = replay_orders(paths["orders"])
            fill_rows = replay_fills(paths["fills"])
            position_rows = replay_positions(paths["positions"])

            # risk -> orders join: same decision_id; attempt binds verdict_id
            self.assertEqual(risk_rows[0]["decision_id"], DEC)
            attempt = next(r for r in order_rows
                           if r["event_type"] == EVT_ORDER_SUBMIT_ATTEMPT)
            self.assertEqual(attempt["decision_id"], DEC)
            self.assertEqual(attempt["risk_verdict_id"],
                             verdict_row["verdict_id"])
            self.assertEqual(attempt["preflight_id"], PF)
            self.assertEqual(attempt["order_id"], ORD)
            self.assertEqual(attempt["client_order_id"], ORD)
            # orders -> fills join: order_id rides every fill-stream row
            for row in fill_rows:
                self.assertEqual(row["order_id"], ORD)
            self.assertEqual(fill["decision_id"], DEC)
            self.assertEqual(fill["position_id"], POS)
            self.assertEqual(modeled["modeled_fill_id"], MF)
            self.assertEqual(divergence["order_id"], ORD)
            # fills -> positions join: opening_order_id + position_id
            self.assertEqual(opened["opening_order_id"], ORD)
            self.assertEqual(opened["position_id"], POS)
            self.assertEqual(opened["decision_id"], DEC)
            self.assertEqual(closed["position_id"], POS)
            self.assertEqual(closed["closing_order_id"], ORD2)
            self.assertEqual(closed["order_id"], ORD2)
            # the four chain-bearing streams all wrote
            self.assertTrue(risk_rows and order_rows and fill_rows
                            and position_rows)


class TestRehydrateExecState(unittest.TestCase):
    def _attempt(self, ledger, order_id, symbol, decision_id=DEC):
        return ledger.record_order_submit_attempt(
            client_order_id=order_id, preflight_id=PF, risk_verdict_id=RV,
            strategy_id="s1", symbol=symbol, instrument_id=42, side="buy",
            qty=Decimal("100"),
            order_intent={"order_type": "marketable_limit", "tif": "day",
                          "limit_price": Decimal("100.25")},
            token_kind="open", kill_generation=0, quote_b=_prov(),
            decision_id=decision_id, order_id=order_id)

    def test_fold_equals_live_byte_exact(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            live_orders = []
            # order A: full lifecycle to terminal -> NOT open
            live_orders.append(self._attempt(ledger, "o-A", "AAPL"))
            live_orders.append(ledger.record_order_submitted(
                client_order_id="o-A", broker_order_id="b-A", state="accepted",
                raw_status="new", ts_broker_utc=None, source="fake",
                decision_id=DEC, order_id="o-A"))
            live_orders.append(ledger.record_order_terminal(
                terminal_state="filled", filled_qty=Decimal("100"),
                cum_notional_usd=BrokerUSD("10020.00"), ts_broker_utc=None,
                decision_id=DEC, order_id="o-A"))
            # order B: submitted, still live -> OPEN, latest row = submitted
            live_orders.append(self._attempt(ledger, "o-B", "MSFT"))
            submitted_b = ledger.record_order_submitted(
                client_order_id="o-B", broker_order_id="b-B", state="accepted",
                raw_status="new", ts_broker_utc=None, source="fake",
                decision_id=DEC, order_id="o-B")
            live_orders.append(submitted_b)
            # order C: unconfirmed not_found -> open-deny + presumed-live
            live_orders.append(self._attempt(ledger, "o-C", "NVDA"))
            unconfirmed_c = ledger.record_order_submit_unconfirmed(
                client_order_id="o-C", error="timeout", attempts=3,
                resolution="not_found", order_id="o-C")
            live_orders.append(unconfirmed_c)
            # order D: write-ahead attempt then consume-time reject -> NOT open
            live_orders.append(self._attempt(ledger, "o-D", "TSLA"))
            live_orders.append(ledger.record_reject(
                symbol="TSLA", instrument_id=42, strategy_id="s1",
                stage="consume", reasons=["kill_generation_changed"],
                stages_skipped=[], detail={"risk_reasons": None,
                                           "quote_reasons": None,
                                           "broker_code": None,
                                           "broker_message": None},
                preflight_id=PF, capped_limit=None, token_kind="open",
                kill_state="halted", kill_generation=1, quote_b=None,
                decision_id=DEC, order_id="o-D"))
            # position + fill rows
            fill = ledger.record_broker_fill(
                delta=_delta(), cur=_cur(), position_id=POS,
                liquidity_flag=None, venue=None, decision_id=DEC, order_id="o-A")
            opened = ledger.record_position_open(
                position_id=POS, symbol="AAPL", instrument_id=42, side="long",
                qty=Decimal("100"), broker_cost_usd=BrokerUSD("10020.00"),
                modeled_cost_usd=None, fee_assumption=_fees(),
                opening_order_id="o-A", strategy_id="s1",
                opened_ts_utc="2026-06-10T18:30:05+00:00",
                decision_id=DEC, order_id="o-A")

            replayed = rehydrate_exec_state(
                replay_orders(paths["orders"]), replay_fills(paths["fills"]),
                replay_positions(paths["positions"]), run_id="run-1")
            from_live = rehydrate_exec_state(
                live_orders, [fill], [opened], run_id="run-1")
            self.assertEqual(dumps(replayed), dumps(from_live))  # byte-exact

            self.assertEqual(set(replayed["open_orders"]), {"o-B", "o-C"})
            self.assertEqual(dumps(replayed["open_orders"]["o-B"]),
                             dumps(submitted_b))
            self.assertEqual(dumps(replayed["open_orders"]["o-C"]),
                             dumps(unconfirmed_c))
            self.assertEqual(replayed["open_deny"], ("NVDA",))
            grouped = replayed["positions"]
            self.assertEqual(set(grouped), {POS})
            self.assertEqual(dumps(grouped[POS]["position_rows"]),
                             dumps([opened]))
            self.assertEqual(dumps(grouped[POS]["fill_rows"]), dumps([fill]))

    def test_fold_is_order_independent_sorted_by_seq(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            self._attempt(ledger, "o-B", "MSFT")
            ledger.record_order_submitted(
                client_order_id="o-B", broker_order_id="b-B", state="accepted",
                raw_status="new", ts_broker_utc=None, source="fake",
                decision_id=DEC, order_id="o-B")
            rows = replay_orders(paths["orders"])
            forward = rehydrate_exec_state(rows, [], [], run_id="run-1")
            backward = rehydrate_exec_state(list(reversed(rows)), [], [],
                                            run_id="run-1")
            self.assertEqual(dumps(forward), dumps(backward))

    def test_open_deny_filters_to_this_run_only(self):
        with TemporaryDirectory() as tmpdir:
            ledger1, paths = _ledger(tmpdir, run_id="run-1")
            self._attempt(ledger1, "o-old", "AAPL")
            ledger1.record_order_submit_unconfirmed(
                client_order_id="o-old", error="timeout", attempts=3,
                resolution="not_found", order_id="o-old")
            ledger2, _ = _ledger(tmpdir, run_id="run-2")
            self._attempt(ledger2, "o-new", "MSFT")
            ledger2.record_order_submit_unconfirmed(
                client_order_id="o-new", error="offline", attempts=0,
                resolution="offline_orphan", order_id="o-new")
            rows = replay_orders(paths["orders"])
            state = rehydrate_exec_state(rows, [], [], run_id="run-2")
            # the prior-run not_found row is EXCLUDED (per-run deny set)
            self.assertEqual(state["open_deny"], ("MSFT",))
            state1 = rehydrate_exec_state(rows, [], [], run_id="run-1")
            self.assertEqual(state1["open_deny"], ("AAPL",))

    def test_adopted_resolution_never_enters_open_deny(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            self._attempt(ledger, "o-B", "MSFT")
            ledger.record_order_submit_unconfirmed(
                client_order_id="o-B", error="timeout", attempts=1,
                resolution="adopted", order_id="o-B")
            state = rehydrate_exec_state(replay_orders(paths["orders"]), [], [],
                                         run_id="run-1")
            self.assertEqual(state["open_deny"], ())

    def test_injectable_book_rehydrate_seam(self):
        # PaperBook.rehydrate lands with the paper_book wave; until then the
        # seam takes any (position_rows, fill_rows) callable (§P.1 / §K).
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            opened = ledger.record_position_open(
                position_id=POS, symbol="AAPL", instrument_id=42, side="long",
                qty=Decimal("100"), broker_cost_usd=BrokerUSD("10020.00"),
                modeled_cost_usd=None, fee_assumption=_fees(),
                opening_order_id=ORD, strategy_id="s1", opened_ts_utc="t",
                decision_id=DEC, order_id=ORD)
            seen = {}

            def fake_book(position_rows, fill_rows):
                seen["positions"] = list(position_rows)
                seen["fills"] = list(fill_rows)
                return "BOOK"

            state = rehydrate_exec_state(
                [], [], replay_positions(paths["positions"]), run_id="run-1",
                book_rehydrate=fake_book)
            self.assertEqual(state["positions"], "BOOK")
            self.assertEqual(dumps(seen["positions"]), dumps([opened]))
            self.assertEqual(seen["fills"], [])

    def test_unconfirmed_without_attempt_row_raises(self):
        # The write-ahead protocol (FD-M5-17) guarantees the attempt row
        # precedes unconfirmed in-stream; its absence is corruption-grade.
        row = {"event_type": EVT_ORDER_SUBMIT_UNCONFIRMED, "run_id": "run-1",
               "seq": 1, "order_id": "o-ghost", "resolution": "not_found",
               "client_order_id": "o-ghost", "error": "timeout", "attempts": 3}
        with self.assertRaises(ExecError):
            rehydrate_exec_state([row], [], [], run_id="run-1")


class TestBrokerFillConsistency(unittest.TestCase):
    def test_delta_cur_mismatch_raises(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            stale_delta = _delta()  # cum 30 @ 100.10
            wrong_cur = _cur(filled_qty=Decimal("70"),
                             filled_avg_price=Decimal("100.18"))
            with self.assertRaises(ExecError):
                ledger.record_broker_fill(
                    delta=stale_delta, cur=wrong_cur, position_id=None,
                    liquidity_flag=None, venue=None,
                    decision_id=DEC, order_id=ORD)

    def test_duck_shaped_inputs_missing_attribute_raises(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            with self.assertRaises(ExecError):
                ledger.record_broker_fill(
                    delta=SimpleNamespace(delta_qty=Decimal("30")),  # incomplete
                    cur=_cur(), position_id=None, liquidity_flag=None,
                    venue=None, decision_id=DEC, order_id=ORD)

    def test_out_of_vocab_source_on_cur_raises(self):
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            hostile = SimpleNamespace(
                broker_order_id="b-1", symbol="AAPL", side="buy",
                filled_qty=Decimal("30"), filled_avg_price=Decimal("100.10"),
                ts_broker_utc=None, source="robinhood")
            with self.assertRaises(ExecError):
                ledger.record_broker_fill(
                    delta=_delta(), cur=hostile, position_id=None,
                    liquidity_flag=None, venue=None,
                    decision_id=DEC, order_id=ORD)


if __name__ == "__main__":
    unittest.main()
