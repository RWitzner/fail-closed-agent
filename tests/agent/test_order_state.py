"""M5 §R test 5 — the ONE order-payload parser chokepoint (§F).

Invariants: S2 at the broker seam (a defective payload never becomes a BrokerOrder —
it becomes OrderInvalid, never an exception on the read path); FD-M5-16 (the frozen
16-string ALPACA_STATUS_MAP is total; anything else is "unknown", which is NEVER
terminal); FD-M5-18/EX-6 (exact integrated notional from polled cumulative
aggregates — `delta_qty x avg` is provably wrong on the committed avg-drift fixture;
`prev` = the snapshot of the last EMITTED FillDelta); EX-10 (`nonpositive_avg_price`,
`nonpositive_delta_cost`).

Watcher/alert/cancel behaviors and the stable `fill_id` are OTHER test files'
property (§R 6 — the id is minted in exec_ledger.record_broker_fill, not here).
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path

from agent.broker.order_state import (
    ALPACA_STATUS_MAP,
    PDT_REJECTION_CODES,
    PDT_REJECTION_MARKERS,
    BrokerOrder,
    BrokerRejection,
    FillDelta,
    OrderApi,
    OrderInvalid,
    classify_rejection,
    fill_delta,
    parse_order_payload,
)
from agent.exec_reasons import ExecError, FILL_SOURCES, ORDER_STATES, TERMINAL_STATES
from agent.serializer import BrokerUSD
from tests.lib.risk_fixtures import OMIT

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alpaca"

# The 16 verified Alpaca status strings (§0.2 A4) — common then rare, verbatim.
A4_STATUSES = (
    "new", "partially_filled", "filled", "done_for_day", "canceled", "expired",
    "replaced", "pending_cancel", "pending_replace",
    "accepted", "pending_new", "accepted_for_bidding", "stopped", "rejected",
    "suspended", "calculated",
)

# The six rare/ambiguous strings FD-M5-16 maps to "unknown".
UNKNOWN_MAPPED = ("replaced", "pending_replace", "suspended",
                  "accepted_for_bidding", "stopped", "calculated")


def _load(name: str):
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def order_payload(**overrides) -> dict:
    """Wire-shaped Alpaca order dict (documented REST field names, money as strings).
    overrides delete (OMIT) / replace keys — the tests/lib/risk_fixtures convention.
    (The shared §Q builder home `tests/lib/alpaca_fixtures.py` is another wave's
    file; this local builder keeps this test file self-contained.)"""
    payload = {
        "id": "61e69015-8549-4bfd-b9c3-01e75843f47d",
        "client_order_id": "o-20260610-AAPL-0001",
        "created_at": "2026-06-10T14:30:00.000000Z",
        "updated_at": "2026-06-10T14:30:01.000000Z",
        "filled_at": None,
        "canceled_at": None,
        "symbol": "AAPL",
        "qty": "100",
        "filled_qty": "30",
        "filled_avg_price": "100.10",
        "type": "limit",
        "side": "buy",
        "time_in_force": "day",
        "limit_price": "100.25",
        "status": "partially_filled",
    }
    for key, value in overrides.items():
        if value is OMIT:
            payload.pop(key, None)
        else:
            payload[key] = value
    return payload


def _parse(payload, *, source="alpaca_paper"):
    return parse_order_payload(payload, source=source)


class TestAlpacaStatusMap(unittest.TestCase):
    def test_map_is_total_over_the_16_a4_strings(self):
        self.assertEqual(len(A4_STATUSES), 16)
        self.assertEqual(set(ALPACA_STATUS_MAP), set(A4_STATUSES))
        self.assertEqual(len(ALPACA_STATUS_MAP), 16)

    def test_map_values_verbatim(self):
        self.assertEqual(ALPACA_STATUS_MAP, {
            "new": "accepted", "accepted": "accepted", "pending_new": "accepted",
            "partially_filled": "partially_filled", "filled": "filled",
            "canceled": "canceled", "pending_cancel": "pending_cancel",
            "expired": "expired", "rejected": "rejected",
            "done_for_day": "done_for_day",
            "replaced": "unknown", "pending_replace": "unknown",
            "suspended": "unknown", "accepted_for_bidding": "unknown",
            "stopped": "unknown", "calculated": "unknown",
        })

    def test_every_mapped_state_is_in_order_states(self):
        for status, state in ALPACA_STATUS_MAP.items():
            self.assertIn(state, ORDER_STATES, status)

    def test_parse_maps_each_of_the_16_statuses(self):
        for status in A4_STATUSES:
            order = _parse(order_payload(status=status))
            self.assertIsInstance(order, BrokerOrder, status)
            self.assertEqual(order.state, ALPACA_STATUS_MAP[status])
            self.assertEqual(order.raw_status, status)

    def test_rare_ambiguous_statuses_map_to_unknown(self):
        for status in UNKNOWN_MAPPED:
            self.assertEqual(ALPACA_STATUS_MAP[status], "unknown")
            self.assertNotIn(ALPACA_STATUS_MAP[status], TERMINAL_STATES)

    def test_unmapped_string_parses_to_unknown(self):
        for status in ("held", "", "FILLED", "Pending_Cancel", "completed"):
            order = _parse(order_payload(status=status))
            self.assertIsInstance(order, BrokerOrder, status)
            self.assertEqual(order.state, "unknown", status)

    def test_unknown_is_never_terminal(self):
        self.assertIn("unknown", ORDER_STATES)
        self.assertNotIn("unknown", TERMINAL_STATES)
        self.assertTrue(TERMINAL_STATES <= ORDER_STATES)
        self.assertEqual(
            TERMINAL_STATES,
            frozenset({"filled", "canceled", "expired", "rejected", "done_for_day"}))


class TestParseOrderPayload(unittest.TestCase):
    def test_good_payload_parses_to_broker_order(self):
        order = _parse(order_payload())
        self.assertIsInstance(order, BrokerOrder)
        self.assertEqual(order.broker_order_id, "61e69015-8549-4bfd-b9c3-01e75843f47d")
        self.assertEqual(order.client_order_id, "o-20260610-AAPL-0001")
        self.assertEqual(order.symbol, "AAPL")
        self.assertEqual(order.side, "buy")
        self.assertEqual(order.state, "partially_filled")
        self.assertEqual(order.raw_status, "partially_filled")
        self.assertEqual(order.qty, Decimal("100"))
        self.assertEqual(order.filled_qty, Decimal("30"))
        self.assertEqual(order.filled_avg_price, Decimal("100.10"))
        self.assertEqual(order.limit_price, Decimal("100.25"))
        self.assertEqual(order.ts_broker_utc, "2026-06-10T14:30:01.000000Z")
        self.assertEqual(order.source, "alpaca_paper")
        for field in ("qty", "filled_qty", "filled_avg_price", "limit_price"):
            value = getattr(order, field)
            self.assertIsInstance(value, Decimal, field)
            self.assertNotIsInstance(value, float, field)

    def test_missing_each_required_key_is_order_invalid(self):
        for key in ("id", "client_order_id", "status", "symbol", "side",
                    "qty", "filled_qty"):
            result = _parse(order_payload(**{key: OMIT}))
            self.assertIsInstance(result, OrderInvalid, key)
            self.assertEqual(result.reason, f"missing_field:{key}")
            self.assertNotIsInstance(result, BrokerOrder)

    def test_empty_payload_is_order_invalid_not_exception(self):
        result = _parse({})
        self.assertIsInstance(result, OrderInvalid)
        self.assertEqual(result.reason, "missing_field:id")

    def test_float_typed_money_is_order_invalid(self):
        cases = {
            "qty": 100.0,
            "filled_qty": 30.0,
            "filled_avg_price": 100.10,
            "limit_price": 100.25,
        }
        for field, value in cases.items():
            result = _parse(order_payload(**{field: value}))
            self.assertIsInstance(result, OrderInvalid, field)
            self.assertEqual(result.reason, f"float_typed:{field}")

    def test_bool_in_money_slot_is_order_invalid(self):
        for field in ("qty", "filled_qty", "filled_avg_price", "limit_price"):
            result = _parse(order_payload(**{field: True}))
            self.assertIsInstance(result, OrderInvalid, field)
            self.assertEqual(result.reason, f"bool_typed:{field}")

    def test_non_finite_money_is_order_invalid(self):
        for field, value in (("qty", "NaN"), ("filled_qty", "Infinity"),
                             ("filled_avg_price", "-Infinity"),
                             ("limit_price", "sNaN")):
            result = _parse(order_payload(**{field: value}))
            self.assertIsInstance(result, OrderInvalid, field)
            self.assertEqual(result.reason, f"non_finite:{field}")

    def test_unparseable_and_wrong_typed_money_is_order_invalid(self):
        result = _parse(order_payload(qty="abc"))
        self.assertIsInstance(result, OrderInvalid)
        self.assertEqual(result.reason, "unparseable:qty")
        result = _parse(order_payload(qty=100))  # int in a money slot (M4 posture)
        self.assertIsInstance(result, OrderInvalid)
        self.assertEqual(result.reason, "invalid_type:qty")

    def test_negative_qty_is_order_invalid(self):
        result = _parse(order_payload(qty="-1"))
        self.assertIsInstance(result, OrderInvalid)
        self.assertEqual(result.reason, "negative:qty")
        result = _parse(order_payload(filled_qty="-1"))
        self.assertIsInstance(result, OrderInvalid)
        self.assertEqual(result.reason, "negative:filled_qty")

    def test_nonpositive_filled_avg_price_is_order_invalid(self):
        for value in ("0", "0.00", "-5"):
            result = _parse(order_payload(filled_avg_price=value))
            self.assertIsInstance(result, OrderInvalid, value)
            self.assertEqual(result.reason, "nonpositive_avg_price")  # EX-10

    def test_non_string_identity_field_is_order_invalid(self):
        for field in ("id", "client_order_id", "status", "symbol", "side"):
            result = _parse(order_payload(**{field: 7}))
            self.assertIsInstance(result, OrderInvalid, field)
            self.assertEqual(result.reason, f"invalid_type:{field}")

    def test_parse_never_raises_on_payload_data(self):
        hostile = [
            {}, {"id": None}, order_payload(qty=object()),
            order_payload(status=None), order_payload(filled_avg_price={}),
            order_payload(id=OMIT, qty="NaN"),
        ]
        for payload in hostile:
            result = _parse(payload)  # must not raise
            self.assertIsInstance(result, OrderInvalid)
            self.assertIs(result.raw, payload)

    def test_optional_prices_absent_or_null_parse_to_none(self):
        for overrides in ({"filled_avg_price": OMIT, "filled_qty": "0"},
                          {"filled_avg_price": None, "filled_qty": "0"}):
            order = _parse(order_payload(status="new", **overrides))
            self.assertIsInstance(order, BrokerOrder)
            self.assertIsNone(order.filled_avg_price)
        order = _parse(order_payload(limit_price=None))
        self.assertIsInstance(order, BrokerOrder)
        self.assertIsNone(order.limit_price)

    def test_ts_broker_utc_best_available(self):
        order = _parse(order_payload())
        self.assertEqual(order.ts_broker_utc, "2026-06-10T14:30:01.000000Z")
        order = _parse(order_payload(
            updated_at=None, filled_at="2026-06-10T14:30:02.000000Z"))
        self.assertEqual(order.ts_broker_utc, "2026-06-10T14:30:02.000000Z")
        order = _parse(order_payload(
            updated_at=OMIT, filled_at=None,
            canceled_at="2026-06-10T14:30:03.000000Z"))
        self.assertEqual(order.ts_broker_utc, "2026-06-10T14:30:03.000000Z")
        order = _parse(order_payload(updated_at=None, filled_at=None,
                                     canceled_at=OMIT))
        self.assertIsNone(order.ts_broker_utc)

    def test_out_of_vocab_source_is_a_caller_bug(self):
        # §2.4 posture: out-of-vocab anywhere => ExecError (FATAL, never coerced);
        # "alpaca_live" is in BROKER_KINDS (reserved M8) but NOT in FILL_SOURCES.
        for source in ("alpaca_live", "spy", "fixture", "bogus", ""):
            with self.assertRaises(ExecError):
                parse_order_payload(order_payload(), source=source)

    def test_both_fill_sources_accepted(self):
        self.assertEqual(FILL_SOURCES, frozenset({"alpaca_paper", "fake"}))
        for source in sorted(FILL_SOURCES):
            order = parse_order_payload(order_payload(), source=source)
            self.assertIsInstance(order, BrokerOrder)
            self.assertEqual(order.source, source)


class TestFillDelta(unittest.TestCase):
    """FD-M5-18: delta_cost = cur.filled_qty x cur.avg - prev.filled_qty x prev.avg,
    EXACT — never delta_qty x avg. The committed avg-drift fixture proves the naive
    form wrong:

      poll 1: 30 @ 100.10  -> d1 = 30x100.10 - 0        = 3003.00 (naive agrees)
      poll 2: 70 @ 100.18  -> d2 = 70x100.18 - 30x100.10 = 7012.60 - 3003.00
                                 = 4009.60;  naive 40x100.18 = 4007.20  (WRONG)
      poll 3: 100 @ 100.20 -> d3 = 100x100.20 - 70x100.18 = 10020.00 - 7012.60
                                 = 3007.40;  naive 30x100.20 = 3006.00  (WRONG)
      sum exact = 3003.00 + 4009.60 + 3007.40 = 10020.00 = 100 x 100.20  (telescopes)
      sum naive = 3003.00 + 4007.20 + 3006.00 = 10016.20 != 10020.00
    """

    def _sequence(self):
        parsed = [parse_order_payload(p, source="alpaca_paper")
                  for p in _load("order_fill_sequence.json")]
        for snapshot in parsed:
            self.assertIsInstance(snapshot, BrokerOrder)
        return parsed

    def test_fixture_sequence_exact_deltas_hand_computed(self):
        s0, s1, s2, s3 = self._sequence()
        self.assertEqual(s0.state, "accepted")
        self.assertIsNone(fill_delta(None, s0))  # no fill yet => no delta

        d1 = fill_delta(None, s1)
        self.assertIsInstance(d1, FillDelta)
        self.assertEqual(d1.delta_qty, Decimal("30"))
        self.assertEqual(d1.delta_cost_usd, Decimal("3003.00"))
        self.assertEqual(d1.cum_filled_qty, Decimal("30"))
        self.assertEqual(d1.filled_avg_price_after, Decimal("100.10"))

        d2 = fill_delta(s1, s2)
        self.assertIsInstance(d2, FillDelta)
        self.assertEqual(d2.delta_qty, Decimal("40"))
        # EXACT integrated notional: 70x100.18 - 30x100.10 = 7012.60 - 3003.00
        self.assertEqual(d2.delta_cost_usd, Decimal("4009.60"))
        # ... and the naive form 40x100.18 = 4007.20 is provably DIFFERENT:
        naive_d2 = d2.delta_qty * s2.filled_avg_price
        self.assertEqual(naive_d2, Decimal("4007.20"))
        self.assertNotEqual(d2.delta_cost_usd, naive_d2)

        d3 = fill_delta(s2, s3)
        self.assertIsInstance(d3, FillDelta)
        self.assertEqual(d3.delta_qty, Decimal("30"))
        self.assertEqual(d3.delta_cost_usd, Decimal("3007.40"))
        self.assertEqual(d3.filled_avg_price_after, Decimal("100.20"))

        # Telescoping: sum of exact deltas == final filled_qty x filled_avg_price.
        total_exact = d1.delta_cost_usd + d2.delta_cost_usd + d3.delta_cost_usd
        self.assertEqual(total_exact, Decimal("10020.00"))
        self.assertEqual(total_exact, s3.filled_qty * s3.filled_avg_price)
        # The naive sum does NOT reach the broker's final notional:
        total_naive = (Decimal("3003.00") + Decimal("4007.20") + Decimal("3006.00"))
        self.assertEqual(total_naive, Decimal("10016.20"))
        self.assertNotEqual(total_naive, total_exact)

    def test_delta_cost_is_broker_usd(self):
        _, s1, s2, _ = self._sequence()
        delta = fill_delta(s1, s2)
        self.assertIsInstance(delta.delta_cost_usd, BrokerUSD)

    def test_filled_qty_regression_is_order_invalid(self):
        _, s1, s2, _ = self._sequence()
        result = fill_delta(s2, s1)
        self.assertIsInstance(result, OrderInvalid)
        self.assertEqual(result.reason, "filled_qty_regression")

    def test_nonpositive_delta_cost_is_order_invalid(self):
        # EX-10: broker avg-correction noise must never journal a <=0-cost buy fill.
        # prev 99 @ 100.00 -> cost 9900.00; cur 100 @ 98.95 -> cost 9895.00:
        # delta_qty = 1 > 0 but delta_cost = -5.00 <= 0.
        prev = _parse(order_payload(filled_qty="99", filled_avg_price="100.00"))
        cur_negative = _parse(order_payload(filled_qty="100",
                                            filled_avg_price="98.95",
                                            status="filled"))
        result = fill_delta(prev, cur_negative)
        self.assertIsInstance(result, OrderInvalid)
        self.assertEqual(result.reason, "nonpositive_delta_cost")
        # Boundary: delta_cost == 0 exactly (100 x 99.00 = 9900.00) is also invalid.
        prev_zero = _parse(order_payload(filled_qty="99", filled_avg_price="100.00"))
        cur_zero = _parse(order_payload(filled_qty="100", filled_avg_price="99.00",
                                        status="filled"))
        result = fill_delta(prev_zero, cur_zero)
        self.assertIsInstance(result, OrderInvalid)
        self.assertEqual(result.reason, "nonpositive_delta_cost")

    def test_avg_only_change_at_same_qty_emits_none(self):
        # EX-6: the CALLER does not advance prev on a None emission — prev stays
        # the snapshot of the last EMITTED FillDelta.
        p1 = _parse(order_payload(filled_qty="30", filled_avg_price="100.10"))
        p2 = _parse(order_payload(filled_qty="30", filled_avg_price="100.12"))
        self.assertIsNone(fill_delta(p1, p2))

    def test_ex6_prev_is_last_emitted_so_sums_telescope(self):
        # Poll walk with an avg-only correction between two qty increases:
        #   emit 30@100.10 (d=3003.00); poll 30@100.12 -> None (prev STAYS 30@100.10);
        #   poll 100@100.20 -> d = 10020.00 - 3003.00 = 7017.00.
        #   sum = 3003.00 + 7017.00 = 10020.00 = final 100 x 100.20. Exact.
        p1 = _parse(order_payload(filled_qty="30", filled_avg_price="100.10"))
        p2 = _parse(order_payload(filled_qty="30", filled_avg_price="100.12"))
        p3 = _parse(order_payload(filled_qty="100", filled_avg_price="100.20",
                                  status="filled"))
        emitted = []
        prev = None
        for cur in (p1, p2, p3):
            delta = fill_delta(prev, cur)
            self.assertNotIsInstance(delta, OrderInvalid)
            if delta is not None:
                emitted.append(delta)
                prev = cur          # advance ONLY on emission (FD-M5-18/EX-6)
        self.assertEqual(len(emitted), 2)
        self.assertEqual(emitted[0].delta_cost_usd, Decimal("3003.00"))
        self.assertEqual(emitted[1].delta_qty, Decimal("70"))
        self.assertEqual(emitted[1].delta_cost_usd, Decimal("7017.00"))
        total = sum(d.delta_cost_usd for d in emitted)
        self.assertEqual(total, p3.filled_qty * p3.filled_avg_price)
        # Negative control: had the caller WRONGLY advanced prev to the avg-only
        # snapshot, the last delta would be 10020.00 - 30x100.12 = 7016.40 and the
        # sum 3003.00 + 7016.40 = 10019.40 would NOT telescope to 10020.00.
        wrong_last = fill_delta(p2, p3)
        self.assertEqual(wrong_last.delta_cost_usd, Decimal("7016.40"))
        self.assertNotEqual(Decimal("3003.00") + wrong_last.delta_cost_usd,
                            p3.filled_qty * p3.filled_avg_price)

    def test_prev_none_first_delta(self):
        cur = _parse(order_payload(filled_qty="30", filled_avg_price="100.10"))
        delta = fill_delta(None, cur)
        self.assertIsInstance(delta, FillDelta)
        self.assertEqual(delta.delta_qty, Decimal("30"))
        self.assertEqual(delta.delta_cost_usd, Decimal("3003.00"))
        self.assertEqual(delta.cum_filled_qty, Decimal("30"))

    def test_idempotent_re_read_emits_none(self):
        snapshot = _parse(order_payload(filled_qty="30", filled_avg_price="100.10"))
        self.assertIsNone(fill_delta(snapshot, snapshot))
        # And a fresh parse of the same payload bytes is equally a non-event:
        again = _parse(order_payload(filled_qty="30", filled_avg_price="100.10"))
        self.assertIsNone(fill_delta(snapshot, again))

    def test_avg_price_without_fill_is_order_invalid(self):
        cur = _parse(order_payload(filled_qty="0", filled_avg_price="100.10",
                                   status="new"))
        self.assertIsInstance(cur, BrokerOrder)  # per-snapshot parse passes...
        result = fill_delta(None, cur)           # ...the cross-check lives here
        self.assertIsInstance(result, OrderInvalid)
        self.assertEqual(result.reason, "avg_price_without_fill")

    def test_fill_without_avg_price_is_order_invalid(self):
        cur = _parse(order_payload(filled_qty="30", filled_avg_price=None))
        self.assertIsInstance(cur, BrokerOrder)
        result = fill_delta(None, cur)
        self.assertIsInstance(result, OrderInvalid)
        self.assertEqual(result.reason, "missing_field:filled_avg_price")

    def test_defective_prev_snapshot_is_order_invalid(self):
        prev = _parse(order_payload(filled_qty="30", filled_avg_price=None))
        cur = _parse(order_payload(filled_qty="70", filled_avg_price="100.18"))
        result = fill_delta(prev, cur)
        self.assertIsInstance(result, OrderInvalid)
        self.assertEqual(result.reason, "missing_field:filled_avg_price")


class TestBrokerRejection(unittest.TestCase):
    def test_subpenny_fixture_is_not_pdt(self):
        raw = _load("order_rejected_subpenny.json")
        rejection = classify_rejection(http_status=raw["status_code"],
                                       code=raw["code"], message=raw["message"])
        self.assertIsInstance(rejection, BrokerRejection)
        self.assertEqual(rejection.http_status, 422)
        self.assertEqual(rejection.code, 42210000)  # A2
        self.assertFalse(rejection.pdt_marker_matched)

    def test_insufficient_bp_fixture_is_not_pdt(self):
        raw = _load("order_rejected_insufficient_bp.json")
        rejection = classify_rejection(http_status=raw["status_code"],
                                       code=raw["code"], message=raw["message"])
        self.assertEqual(rejection.http_status, 403)
        self.assertEqual(rejection.message,
                         "Buying power or shares is not sufficient")  # A8
        self.assertFalse(rejection.pdt_marker_matched)

    def test_pdt_fixture_matches_by_code(self):
        raw = _load("order_rejected_pdt.json")
        rejection = classify_rejection(http_status=raw["status_code"],
                                       code=raw["code"], message=raw["message"])
        self.assertEqual(rejection.code, 40310100)
        self.assertTrue(rejection.pdt_marker_matched)

    def test_pdt_matches_by_marker_case_insensitive(self):
        for message in (
            "Order rejected: insufficient Day Trading Buying Power",
            "trade denied due to PATTERN DAY TRADING protection",
            "day-trade limit reached",
        ):
            rejection = classify_rejection(http_status=403, code=None,
                                           message=message)
            self.assertTrue(rejection.pdt_marker_matched, message)

    def test_unrelated_rejection_does_not_match(self):
        rejection = classify_rejection(http_status=403, code=40010001,
                                       message="insufficient buying power")
        self.assertFalse(rejection.pdt_marker_matched)
        rejection = classify_rejection(http_status=None, code=None, message="")
        self.assertFalse(rejection.pdt_marker_matched)

    def test_marker_set_mirrors_the_m4_fd_m4_15_set(self):
        # Finalized at M5-2a against the real paper API; until then the pinned set
        # must stay byte-identical with the M4 home (risk/pdt_compat.py:18-19).
        from agent.risk.pdt_compat import PDT_REJECTION_CODES as M4_CODES
        from agent.risk.pdt_compat import PDT_REJECTION_MARKERS as M4_MARKERS
        self.assertEqual(PDT_REJECTION_CODES, M4_CODES)
        self.assertEqual(PDT_REJECTION_MARKERS, M4_MARKERS)
        self.assertIn(40310100, PDT_REJECTION_CODES)


class TestOrderApiProtocol(unittest.TestCase):
    def test_runtime_checkable_protocol(self):
        class StubApi:
            def submit(self, payload):
                return {}

            def get_by_client_order_id(self, client_order_id):
                return {}

            def cancel(self, broker_order_id):
                return {}

            def get_account(self):
                return {}

            def list_positions(self):
                return []

            def list_open_orders(self):
                return []

        class MissingCancel:
            def submit(self, payload):
                return {}

            def get_by_client_order_id(self, client_order_id):
                return {}

            def get_account(self):
                return {}

            def list_positions(self):
                return []

            def list_open_orders(self):
                return []

        self.assertIsInstance(StubApi(), OrderApi)
        self.assertNotIsInstance(MissingCancel(), OrderApi)


class TestCommittedFixtures(unittest.TestCase):
    """The §Q committed alpaca payload files parse green through the real
    chokepoints (this build's deliverable; sibling waves consume the same bytes)."""

    def test_order_accepted_fixture(self):
        order = _parse(_load("order_accepted.json"))
        self.assertIsInstance(order, BrokerOrder)
        self.assertEqual(order.state, "accepted")
        self.assertEqual(order.raw_status, "new")
        self.assertEqual(order.qty, Decimal("100"))
        self.assertEqual(order.filled_qty, Decimal("0"))
        self.assertIsNone(order.filled_avg_price)
        self.assertEqual(order.limit_price, Decimal("100.25"))

    def test_order_canceled_fixture(self):
        order = _parse(_load("order_canceled.json"))
        self.assertIsInstance(order, BrokerOrder)
        self.assertEqual(order.state, "canceled")
        self.assertIn(order.state, TERMINAL_STATES)
        self.assertEqual(order.filled_qty, Decimal("0"))

    def test_order_pending_cancel_fixture(self):
        order = _parse(_load("order_pending_cancel.json"))
        self.assertIsInstance(order, BrokerOrder)
        self.assertEqual(order.state, "pending_cancel")  # async cancel, A10
        self.assertNotIn(order.state, TERMINAL_STATES)

    def test_order_unknown_status_fixture(self):
        raw = _load("order_unknown_status.json")
        self.assertNotIn(raw["status"], A4_STATUSES)
        order = _parse(raw)
        self.assertIsInstance(order, BrokerOrder)
        self.assertEqual(order.state, "unknown")
        self.assertNotIn(order.state, TERMINAL_STATES)  # FD-M5-16: keep polling

    def test_fill_sequence_fixture_shape(self):
        rows = _load("order_fill_sequence.json")
        self.assertEqual([r["status"] for r in rows],
                         ["new", "partially_filled", "partially_filled", "filled"])
        self.assertEqual([r["filled_qty"] for r in rows], ["0", "30", "70", "100"])
        self.assertEqual([r["filled_avg_price"] for r in rows],
                         [None, "100.10", "100.18", "100.20"])
        ids = {r["id"] for r in rows}
        client_ids = {r["client_order_id"] for r in rows}
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(client_ids), 1)
        for row in rows:  # money rides the wire as STRINGS
            for field in ("qty", "filled_qty"):
                self.assertIsInstance(row[field], str)

    def test_account_paper_fixture_parses_through_m4_chokepoint(self):
        from agent.risk.account_state import BrokerAccountRead, parse_account_payload
        read = parse_account_payload(
            _load("account_paper.json"), source="alpaca_paper", seen_at_ms=0,
            ts_read_utc="2026-06-10T14:30:00.000000Z")
        self.assertIsInstance(read, BrokerAccountRead)
        self.assertEqual(read.equity, Decimal("100000.00"))
        self.assertEqual(read.buying_power, Decimal("200000.00"))
        self.assertIs(read.pattern_day_trader, False)

    def test_positions_paper_fixture_parses_through_m4_chokepoint(self):
        from agent.risk.account_state import PortfolioRead, parse_positions_payload
        portfolio = parse_positions_payload(
            _load("positions_paper.json"), source="alpaca_paper", seen_at_ms=0)
        self.assertIsInstance(portfolio, PortfolioRead)
        self.assertEqual(portfolio.qty_for("AAPL"), Decimal("10"))
        self.assertEqual(portfolio.positions[0].market_value, Decimal("1900.00"))


if __name__ == "__main__":
    unittest.main()
