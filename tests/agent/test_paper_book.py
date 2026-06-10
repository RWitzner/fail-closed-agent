"""M5 §R test 7 — PaperBook/PaperPosition (§K): exact-integrated-notional vs
qty×avg (FD-M5-18, the 3-partial fixture), the S5 typing wall (`ModeledUSD` into
a broker field => TypeError; hostile modeled inputs leave broker fields
unchanged), the two-class pnl_snapshot + verbatim `used_for_strategy_evaluation`,
fees only on the realistic side (buy open zero; sell close ceil-rounded SEC+TAF
over the MODELED exit proceeds — EX-9 — incl. the $8.30 TAF cap boundary), the
structural mark gate (typed QuoteSnapshot only; stale/unusable => last mark
stands), realized broker/modeled split + the pinned sell sign convention, the
FROZEN EX-4 partial-close allocation (CENT HALF_EVEN slice, exact residual,
telescoping wash-out — hand-verified), and the byte-exact rehydrate fold incl.
partial close through a REAL ExecLedger (the test_exec_ledger construction
pattern).

Invariants: S5, S2, S3.
"""
import unittest
from dataclasses import replace
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from agent.broker.order_state import BrokerOrder, FillDelta, fill_delta, parse_order_payload
from agent.exec_ledger import ExecLedger, rehydrate_exec_state, replay_fills, replay_positions
from agent.exec_reasons import ExecError
from agent.fees import FEE_MODEL_VERSION, TAF_CAP_PER_TRADE_USD
from agent.paper_book import PaperBook, PaperPosition
from agent.quote_quality import QuoteSnapshot
from agent.serializer import BrokerUSD, ModeledUSD, row_hash
from recorder.persistence import EventWriter
from tests.lib.alpaca_fixtures import order_fill_sequence

_CLOCK = lambda: "2026-06-10T20:00:00.000000+00:00"  # noqa: E731 — byte-determinism
_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)
CENT = Decimal("0.01")

# §P.3-shaped ids (the ledger validates prefixes; the book passes them through).
DEC = "d-open1"
DEC2 = "d-close1"
DEC3 = "d-close2"
ORD = "o-open1"
ORD2 = "o-close1"
ORD3 = "o-close2"

# Committed signal budgets, ctor-injected (§K resolution 4).
STALENESS_MS = 2000
SPREAD_BPS = Decimal("50")


def _ledger(tmpdir, run_id="run-1"):
    base = Path(tmpdir)
    ledger = ExecLedger(
        orders=EventWriter(base / "orders.jsonl", run_id, clock=_CLOCK),
        fills=EventWriter(base / "fills.jsonl", run_id, clock=_CLOCK),
        positions=EventWriter(base / "positions.jsonl", run_id, clock=_CLOCK),
        rules_hash="rh-test")
    paths = {"orders": base / "orders.jsonl", "fills": base / "fills.jsonl",
             "positions": base / "positions.jsonl"}
    return ledger, paths


def _book(tmpdir, run_id="run-1"):
    ledger, paths = _ledger(tmpdir, run_id=run_id)
    book = PaperBook(ledger=ledger, run_id=run_id,
                     quote_staleness_ms_max=STALENESS_MS,
                     spread_bps_max=SPREAD_BPS)
    return book, ledger, paths


def _fixture_deltas():
    """The wave-1 FD-M5-18 avg-drift sequence: emitted deltas 3003.00 /
    4009.60 / 3007.40 (exact integrated notional; naive qty×avg is WRONG)."""
    snaps = [parse_order_payload(p, source="fake")
             for p in order_fill_sequence()[1:]]   # skip the 0-filled snapshot
    deltas, prev = [], None
    for cur in snaps:
        delta = fill_delta(prev, cur)
        assert isinstance(delta, FillDelta)
        deltas.append((delta, cur))
        prev = cur
    return deltas


def _sell_cur(*, filled_qty, avg, order_id=ORD2, broker_order_id="b-close"):
    return BrokerOrder(
        broker_order_id=broker_order_id, client_order_id=order_id,
        symbol="AAPL", side="sell", state="filled", raw_status="filled",
        qty=filled_qty, filled_qty=filled_qty, filled_avg_price=avg,
        limit_price=None, ts_broker_utc=None, source="fake")


def _sell_fill(*, qty, proceeds, avg, order_id=ORD2, broker_order_id="b-close"):
    cur = _sell_cur(filled_qty=qty, avg=avg, order_id=order_id,
                    broker_order_id=broker_order_id)
    delta = fill_delta(None, cur)
    assert isinstance(delta, FillDelta)
    assert delta.delta_cost_usd == proceeds   # the pinned sign convention
    return delta, cur


def _fill(qty, cost, *, cum=None, avg=None):
    """A hand-built FillDelta (the book duck-reads delta_qty/delta_cost_usd)."""
    return FillDelta(delta_qty=qty, delta_cost_usd=BrokerUSD(cost),
                     cum_filled_qty=cum if cum is not None else qty,
                     filled_avg_price_after=avg if avg is not None else Decimal("1"))


def _quote(**overrides):
    d = dict(symbol="AAPL", instrument_id=42,
             bid=Decimal("100.30"), ask=Decimal("100.32"),
             bid_sz=Decimal("5"), ask_sz=Decimal("5"),
             ts_event_utc="2026-06-10T14:31:00.000001+00:00",
             ts_recv_utc="2026-06-10T14:31:00.000002+00:00",
             seen_at_ms=1000, reconnect_epoch=0, vendor_seq=7,
             dataset="EQUS.MINI", schema="tbbo")
    d.update(overrides)
    return QuoteSnapshot(**d)


def _modeled(cost):
    """A duck-typed ModeledFill (§3: execution_realism is not imported here)."""
    return SimpleNamespace(modeled_cost_usd=cost)


def _open_aapl(book, *, fills=None, modeled=ModeledUSD("10018.00")):
    fills = fills if fills is not None else [d for d, _ in _fixture_deltas()]
    return book.open_position(
        decision_id=DEC, order_id=ORD, symbol="AAPL", instrument_id=42,
        strategy_id="synthetic.scripted_v1", fills=fills,
        modeled=None if modeled is None else _modeled(modeled),
        opened_ts_utc="2026-06-10T18:31:04+00:00")


class TestPositionIdDeterminism(unittest.TestCase):
    def test_position_id_per_p3_and_run_independent(self):
        # §P.3: position_id = "pos-" + row_hash({symbol, opening_order_id}) —
        # run-INDEPENDENT (account-level facts survive restarts).
        expected = "pos-" + row_hash({"symbol": "AAPL",
                                      "opening_order_id": ORD})
        pids = []
        for run_id in ("run-1", "run-2"):
            with TemporaryDirectory() as tmpdir:
                book, _, _ = _book(tmpdir, run_id=run_id)
                pos = _open_aapl(book, fills=[_fill(Decimal("1"),
                                                    Decimal("10.00"))])
                pids.append(pos.position_id)
        self.assertEqual(pids, [expected, expected])


class TestOpenPositionExactNotional(unittest.TestCase):
    def test_three_partial_fixture_exact_vs_naive_qty_x_avg(self):
        # FD-M5-18 on the wave-1 fixture: deltas 3003.00 / 4009.60 / 3007.40.
        deltas = _fixture_deltas()
        self.assertEqual([d.delta_cost_usd for d, _ in deltas],
                         [Decimal("3003.00"), Decimal("4009.60"),
                          Decimal("3007.40")])
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            pos = _open_aapl(book)
            # exact: telescopes to the broker's final qty×avg
            self.assertEqual(pos.qty, Decimal("100"))
            self.assertEqual(pos.broker_cost_usd,
                             Decimal("100") * Decimal("100.20"))
            self.assertEqual(str(pos.broker_cost_usd), "10020.00")
            # naive Σ delta_qty×avg_after is provably WRONG under avg drift
            naive = sum(d.delta_qty * d.filled_avg_price_after
                        for d, _ in deltas)
            self.assertEqual(naive, Decimal("10016.20"))
            self.assertNotEqual(naive, pos.broker_cost_usd)

    def test_open_with_first_fill_then_apply_fill_matches(self):
        # §M.4: the first fill opens the position; later deltas ride apply_fill.
        deltas = [d for d, _ in _fixture_deltas()]
        with TemporaryDirectory() as tmpdir:
            book, _, paths = _book(tmpdir)
            pos = _open_aapl(book, fills=[deltas[0]])
            self.assertEqual(str(pos.broker_cost_usd), "3003.00")
            rows_before = len(replay_positions(paths["positions"]))
            pos = book.apply_fill(pos.position_id, deltas[1])
            pos = book.apply_fill(pos.position_id, deltas[2])
            self.assertEqual(pos.qty, Decimal("100"))
            self.assertEqual(str(pos.broker_cost_usd), "10020.00")
            # resolution 3: apply_fill journals NOTHING (the orchestrator owns
            # broker_fill rows); the positions stream is unchanged.
            self.assertEqual(len(replay_positions(paths["positions"])),
                             rows_before)
            # the modeled side is the full-qty open basis — untouched
            self.assertEqual(str(pos.modeled_cost_usd), "10018.00")

    def test_position_open_row_carries_exact_costs_and_zero_open_fee(self):
        with TemporaryDirectory() as tmpdir:
            book, _, paths = _book(tmpdir)
            pos = _open_aapl(book)
            (row,) = replay_positions(paths["positions"])
            self.assertEqual(row["event_type"], "position_open")
            self.assertEqual(row["broker_cost_usd"], "10020.00")   # EXACT bytes
            self.assertEqual(row["modeled_cost_usd"], "10018.00")
            self.assertEqual(row["qty"], "100")
            self.assertEqual(row["side"], "long")
            self.assertEqual(row["opening_order_id"], ORD)
            self.assertEqual(row["position_id"], pos.position_id)
            # §J: the open-side ZERO fee assumption, CENT-scale byte form
            self.assertEqual(row["fee_assumption"],
                             {"model_version": FEE_MODEL_VERSION,
                              "sec_usd": "0.00", "taf_usd": "0.00",
                              "total_usd": "0.00"})

    def test_duplicate_open_and_empty_fills_raise(self):
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            _open_aapl(book, fills=[_fill(Decimal("1"), Decimal("10.00"))])
            with self.assertRaises(ExecError):
                _open_aapl(book, fills=[_fill(Decimal("1"), Decimal("10.00"))])
            with self.assertRaises(ExecError):
                book.open_position(
                    decision_id=DEC, order_id="o-other", symbol="MSFT",
                    instrument_id=7, strategy_id="s1", fills=[],
                    modeled=None, opened_ts_utc="t")

    def test_modeled_none_is_unassessed(self):
        with TemporaryDirectory() as tmpdir:
            book, _, paths = _book(tmpdir)
            pos = _open_aapl(book, fills=[_fill(Decimal("1"),
                                                Decimal("10.00"))],
                             modeled=None)
            self.assertIsNone(pos.modeled_cost_usd)
            (row,) = replay_positions(paths["positions"])
            self.assertIsNone(row["modeled_cost_usd"])


class TestTypingWall(unittest.TestCase):
    """S5: ModeledUSD into a broker field => TypeError; hostile modeled inputs
    leave broker fields unchanged (white-box)."""

    def test_modeled_usd_delta_cost_into_open_raises_nothing_written(self):
        with TemporaryDirectory() as tmpdir:
            book, _, paths = _book(tmpdir)
            hostile = FillDelta(delta_qty=Decimal("30"),
                                delta_cost_usd=ModeledUSD("3003.00"),
                                cum_filled_qty=Decimal("30"),
                                filled_avg_price_after=Decimal("100.10"))
            with self.assertRaises(TypeError):
                _open_aapl(book, fills=[hostile])
            self.assertEqual(book._positions, {})          # white-box: untouched
            self.assertEqual(replay_positions(paths["positions"]), [])
            # plain Decimal mirrors (lineage requires the newtype)
            plain = FillDelta(delta_qty=Decimal("30"),
                              delta_cost_usd=Decimal("3003.00"),
                              cum_filled_qty=Decimal("30"),
                              filled_avg_price_after=Decimal("100.10"))
            with self.assertRaises(TypeError):
                _open_aapl(book, fills=[plain])
            self.assertEqual(book._positions, {})

    def test_hostile_apply_fill_leaves_position_unchanged(self):
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            pos = _open_aapl(book, fills=[_fill(Decimal("30"),
                                                Decimal("3003.00"))])
            before = book._positions[pos.position_id]
            hostile = FillDelta(delta_qty=Decimal("40"),
                                delta_cost_usd=ModeledUSD("4009.60"),
                                cum_filled_qty=Decimal("70"),
                                filled_avg_price_after=Decimal("100.18"))
            with self.assertRaises(TypeError):
                book.apply_fill(pos.position_id, hostile)
            self.assertEqual(book._positions[pos.position_id], before)
            self.assertEqual(str(book._positions[pos.position_id]
                                 .broker_cost_usd), "3003.00")

    def test_hostile_modeled_at_open_raises_before_any_write(self):
        with TemporaryDirectory() as tmpdir:
            book, _, paths = _book(tmpdir)
            for hostile_cost in (BrokerUSD("10018.00"), Decimal("10018.00")):
                with self.assertRaises(TypeError):
                    _open_aapl(book, fills=[_fill(Decimal("1"),
                                                  Decimal("10.00"))],
                               modeled=hostile_cost)
                self.assertEqual(book._positions, {})
                self.assertEqual(replay_positions(paths["positions"]), [])

    def test_hostile_modeled_at_close_leaves_broker_fields_unchanged(self):
        with TemporaryDirectory() as tmpdir:
            book, _, paths = _book(tmpdir)
            pos = _open_aapl(book)
            before = book._positions[pos.position_id]
            rows_before = len(replay_positions(paths["positions"]))
            sell, _ = _sell_fill(qty=Decimal("40"), proceeds=Decimal("4040.00"),
                                 avg=Decimal("101.00"))
            with self.assertRaises(TypeError):
                book.close_position(
                    position_id=pos.position_id, order_id=ORD2, fills=[sell],
                    modeled=_modeled(BrokerUSD("4035.00")),   # WRONG lineage
                    reason="strategy_exit", decision_id=DEC2)
            after = book._positions[pos.position_id]
            self.assertEqual(after, before)                   # white-box
            self.assertEqual(str(after.broker_cost_usd), "10020.00")
            self.assertEqual(after.qty, Decimal("100"))
            self.assertEqual(len(replay_positions(paths["positions"])),
                             rows_before)                     # no row written

    def test_modeled_usd_close_fills_raise(self):
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            pos = _open_aapl(book)
            hostile = FillDelta(delta_qty=Decimal("40"),
                                delta_cost_usd=ModeledUSD("4040.00"),
                                cum_filled_qty=Decimal("40"),
                                filled_avg_price_after=Decimal("101.00"))
            with self.assertRaises(TypeError):
                book.close_position(position_id=pos.position_id, order_id=ORD2,
                                    fills=[hostile], modeled=None,
                                    reason="strategy_exit", decision_id=DEC2)

    def test_field_newtypes(self):
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            pos = _open_aapl(book)
            self.assertIsInstance(pos.broker_cost_usd, BrokerUSD)
            self.assertIsInstance(pos.modeled_cost_usd, ModeledUSD)
            self.assertNotIsInstance(pos.broker_cost_usd, ModeledUSD)
            self.assertNotIsInstance(pos.modeled_cost_usd, BrokerUSD)


class TestMark(unittest.TestCase):
    def _opened(self, tmpdir):
        book, ledger, paths = _book(tmpdir)
        pos = _open_aapl(book)
        return book, pos, paths

    def test_bare_numbers_and_dicts_raise(self):
        with TemporaryDirectory() as tmpdir:
            book, pos, _ = self._opened(tmpdir)
            for bad in (Decimal("100.30"), 100, {"bid": Decimal("100.30")},
                        None):
                with self.assertRaises(ExecError):
                    book.mark(pos.position_id, bad, now_ms=1500, bar_key="bk")

    def test_good_mark_journals_best_bid_row(self):
        with TemporaryDirectory() as tmpdir:
            book, pos, paths = self._opened(tmpdir)
            row = book.mark(pos.position_id, _quote(), now_ms=1500,
                            bar_key="2026-06-10T14:31:00-04:00|1m")
            self.assertIsNotNone(row)
            self.assertEqual(row["mark_source"], "best_bid")   # long: conservative
            self.assertEqual(row["mark_price"], Decimal("100.30"))
            # unrealized both classes: 100×100.30 − 10020.00 / − 10018.00
            self.assertEqual(row["unrealized_broker_usd"], Decimal("10.00"))
            self.assertEqual(row["unrealized_modeled_usd"], Decimal("12.00"))
            self.assertEqual(
                set(row["quote"]),
                {"dataset", "schema", "ts_event_utc", "ts_recv_utc",
                 "seen_at_ms", "reconnect_epoch", "vendor_seq"})
            self.assertEqual(len(replay_positions(paths["positions"])), 2)

    def test_stale_quote_no_new_mark_last_mark_stands_strict_boundary(self):
        with TemporaryDirectory() as tmpdir:
            book, pos, paths = self._opened(tmpdir)
            # fresh at exactly the budget (strict '>'): age 2000 == budget
            self.assertIsNotNone(book.mark(pos.position_id, _quote(),
                                           now_ms=1000 + STALENESS_MS,
                                           bar_key="bk1"))
            rows_after_first = len(replay_positions(paths["positions"]))
            # age 2001 > budget: stale — NO new mark, nothing journaled
            stale = _quote(bid=Decimal("999.99"), ask=Decimal("1000.01"))
            self.assertIsNone(book.mark(pos.position_id, stale,
                                        now_ms=1000 + STALENESS_MS + 1,
                                        bar_key="bk2"))
            self.assertEqual(len(replay_positions(paths["positions"])),
                             rows_after_first)
            # the LAST mark stands: pnl still prices at 100.30
            snap = book.pnl_snapshot(pos.position_id, bar_key="bk2")
            self.assertEqual(snap["broker_account_pnl"], Decimal("10.00"))

    def test_unusable_quote_spread_too_wide_no_mark(self):
        with TemporaryDirectory() as tmpdir:
            book, pos, paths = self._opened(tmpdir)
            wide = _quote(bid=Decimal("100.00"), ask=Decimal("101.00"))  # ~99.5 bps
            self.assertIsNone(book.mark(pos.position_id, wide, now_ms=1500,
                                        bar_key="bk"))
            self.assertEqual(len(replay_positions(paths["positions"])), 1)

    def test_identity_mismatch_raises(self):
        with TemporaryDirectory() as tmpdir:
            book, pos, _ = self._opened(tmpdir)
            with self.assertRaises(ExecError):
                book.mark(pos.position_id, _quote(symbol="MSFT"), now_ms=1500,
                          bar_key="bk")
            with self.assertRaises(ExecError):
                book.mark(pos.position_id, _quote(instrument_id=7), now_ms=1500,
                          bar_key="bk")

    def test_mark_unknown_or_closed_position_raises(self):
        with TemporaryDirectory() as tmpdir:
            book, pos, _ = self._opened(tmpdir)
            with self.assertRaises(ExecError):
                book.mark("pos-unknown", _quote(), now_ms=1500, bar_key="bk")
            sell, _ = _sell_fill(qty=Decimal("100"),
                                 proceeds=Decimal("10100.00"),
                                 avg=Decimal("101.00"))
            book.close_position(position_id=pos.position_id, order_id=ORD2,
                                fills=[sell], modeled=None,
                                reason="strategy_exit", decision_id=DEC2)
            with self.assertRaises(ExecError):
                book.mark(pos.position_id, _quote(), now_ms=1500, bar_key="bk")


class TestPnlSnapshot(unittest.TestCase):
    def test_carries_both_classes_and_verbatim_evaluation_basis(self):
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            pos = _open_aapl(book)
            book.mark(pos.position_id, _quote(), now_ms=1500, bar_key="bk")
            snap = book.pnl_snapshot(pos.position_id, bar_key="bk")
            self.assertEqual(snap["broker_account_pnl"], Decimal("10.00"))
            self.assertIsInstance(snap["broker_account_pnl"], BrokerUSD)
            self.assertEqual(snap["execution_realistic_pnl"], Decimal("12.00"))
            self.assertIsInstance(snap["execution_realistic_pnl"], ModeledUSD)
            self.assertEqual(snap["used_for_strategy_evaluation"],
                             "execution_realistic_pnl")        # verbatim
            self.assertEqual(snap["basis"], {"broker": "broker_fills",
                                             "modeled": "modeled_fill_plus_fees"})
            self.assertEqual(snap["divergence_flag"], "unassessed")  # EX-11 default

    def test_modeled_none_position_snapshots_null_realistic_side(self):
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            pos = _open_aapl(book, fills=[_fill(Decimal("100"),
                                                Decimal("10020.00"))],
                             modeled=None)
            book.mark(pos.position_id, _quote(), now_ms=1500, bar_key="bk")
            snap = book.pnl_snapshot(pos.position_id, bar_key="bk")
            self.assertEqual(snap["broker_account_pnl"], Decimal("10.00"))
            self.assertIsNone(snap["execution_realistic_pnl"])

    def test_before_first_mark_raises(self):
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            pos = _open_aapl(book)
            with self.assertRaises(ExecError):
                book.pnl_snapshot(pos.position_id, bar_key="bk")

    def test_divergence_flag_setter_fd_m5_20(self):
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            pos = _open_aapl(book)
            book.mark(pos.position_id, _quote(), now_ms=1500, bar_key="bk")
            book.set_divergence_flag(pos.position_id, "broker_optimistic")
            snap = book.pnl_snapshot(pos.position_id, bar_key="bk")
            self.assertEqual(snap["divergence_flag"], "broker_optimistic")
            # 'the position's LATEST flag' — the setter overwrites
            book.set_divergence_flag(pos.position_id, "aligned")
            snap = book.pnl_snapshot(pos.position_id, bar_key="bk")
            self.assertEqual(snap["divergence_flag"], "aligned")
            with self.assertRaises(ExecError):
                book.set_divergence_flag(pos.position_id, "modeled_full")  # wrong vocab
            with self.assertRaises(ExecError):
                book.set_divergence_flag("pos-unknown", "aligned")
            # a CLOSED position still accepts the setter (order-terminal can
            # postdate the close booking)
            sell, _ = _sell_fill(qty=Decimal("100"),
                                 proceeds=Decimal("10100.00"),
                                 avg=Decimal("101.00"))
            book.close_position(position_id=pos.position_id, order_id=ORD2,
                                fills=[sell], modeled=None,
                                reason="strategy_exit", decision_id=DEC2)
            book.set_divergence_flag(pos.position_id, "broker_conservative")

    def test_fees_enter_only_the_realistic_side_after_partial_close(self):
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            pos = _open_aapl(book)   # 100 sh, broker 10020.00, modeled 10018.00
            sell, _ = _sell_fill(qty=Decimal("40"), proceeds=Decimal("4040.00"),
                                 avg=Decimal("101.00"))
            row = book.close_position(
                position_id=pos.position_id, order_id=ORD2, fills=[sell],
                modeled=_modeled(ModeledUSD("4035.00")),
                reason="strategy_exit", decision_id=DEC2)
            # sell fee over the MODELED proceeds: sec=ceil(4035×rate)=0.12,
            # taf=ceil(40×0.000166)=0.01 -> total 0.13
            self.assertEqual(row["fees_assessed"]["total_usd"], Decimal("0.13"))
            book.mark(pos.position_id, _quote(bid=Decimal("101.20"),
                                              ask=Decimal("101.22")),
                      now_ms=1500, bar_key="bk")
            snap = book.pnl_snapshot(pos.position_id, bar_key="bk")
            # broker side FEE-FREE (A6): 60×101.20 − 6012.00 = 60.00
            self.assertEqual(snap["broker_account_pnl"], Decimal("60.00"))
            # realistic side: 6072.00 − 6010.80 − 0.13 = 61.07
            self.assertEqual(snap["execution_realistic_pnl"], Decimal("61.07"))


class TestClosePosition(unittest.TestCase):
    def test_ex4_partial_close_allocation_hand_verified(self):
        # EX-4 FROZEN fixture: 3 shares, broker_cost 100.01, close 1 =>
        # slice = (100.01×1/3).quantize(CENT, HALF_EVEN) = 33.34, residual
        # = 66.67 EXACT; close the remaining 2 => the residue washes out and
        # the slices telescope to the original cost.
        with localcontext(_CTX):
            hand_slice = (Decimal("100.01") * Decimal("1")
                          / Decimal("3")).quantize(CENT, ROUND_HALF_EVEN)
        self.assertEqual(hand_slice, Decimal("33.34"))            # hand-verified
        self.assertEqual(Decimal("100.01") - hand_slice, Decimal("66.67"))
        with TemporaryDirectory() as tmpdir:
            book, _, paths = _book(tmpdir)
            pos = _open_aapl(book, fills=[_fill(Decimal("3"),
                                                Decimal("100.01"))],
                             modeled=None)
            sell1, _ = _sell_fill(qty=Decimal("1"), proceeds=Decimal("35.00"),
                                  avg=Decimal("35.00"))
            row1 = book.close_position(
                position_id=pos.position_id, order_id=ORD2, fills=[sell1],
                modeled=None, reason="strategy_exit", decision_id=DEC2)
            self.assertEqual(row1["closed_slice_broker_cost_usd"],
                             Decimal("33.34"))
            self.assertEqual(row1["residual_broker_cost_usd"],
                             Decimal("66.67"))
            survivor = book.position(pos.position_id)
            self.assertEqual(survivor.status, "open")             # survives
            self.assertEqual(survivor.qty, Decimal("2"))          # shrunken
            self.assertEqual(str(survivor.broker_cost_usd), "66.67")
            # close the remaining 2: residue washes out, totals telescope
            sell2, _ = _sell_fill(qty=Decimal("2"), proceeds=Decimal("68.00"),
                                  avg=Decimal("34.00"), order_id=ORD3,
                                  broker_order_id="b-close2")
            row2 = book.close_position(
                position_id=pos.position_id, order_id=ORD3, fills=[sell2],
                modeled=None, reason="strategy_exit", decision_id=DEC3)
            self.assertEqual(row2["closed_slice_broker_cost_usd"],
                             Decimal("66.67"))
            self.assertEqual(row2["residual_broker_cost_usd"], Decimal("0.00"))
            self.assertEqual(row1["closed_slice_broker_cost_usd"]
                             + row2["closed_slice_broker_cost_usd"],
                             Decimal("100.01"))                  # telescopes
            closed = book.position(pos.position_id)
            self.assertEqual(closed.status, "closed")
            self.assertEqual(closed.qty, Decimal("0"))
            self.assertEqual(str(closed.broker_cost_usd), "0.00")

    def test_sell_sign_convention_gain_and_loss(self):
        # PINNED: a SELL FillDelta.delta_cost_usd is the POSITIVE sale proceeds
        # => realized_broker_pnl = proceeds − closed_slice (gain positive).
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            pos = _open_aapl(book, fills=[_fill(Decimal("3"),
                                                Decimal("100.01"))],
                             modeled=None)
            gain, _ = _sell_fill(qty=Decimal("1"), proceeds=Decimal("35.00"),
                                 avg=Decimal("35.00"))
            self.assertEqual(gain.delta_cost_usd, Decimal("35.00"))  # proceeds > 0
            row = book.close_position(
                position_id=pos.position_id, order_id=ORD2, fills=[gain],
                modeled=None, reason="strategy_exit", decision_id=DEC2)
            # 35.00 − 33.34 = +1.66 (gain)
            self.assertEqual(row["realized_broker_pnl"], Decimal("1.66"))
            self.assertEqual(row["broker_exit_notional_usd"], Decimal("35.00"))
            loss, _ = _sell_fill(qty=Decimal("1"), proceeds=Decimal("30.00"),
                                 avg=Decimal("30.00"), order_id=ORD3,
                                 broker_order_id="b-close2")
            row = book.close_position(
                position_id=pos.position_id, order_id=ORD3, fills=[loss],
                modeled=None, reason="strategy_exit", decision_id=DEC3)
            # slice = (66.67×1/2).quantize = 33.34 (33.335 -> HALF_EVEN -> 33.34)
            self.assertEqual(row["closed_slice_broker_cost_usd"],
                             Decimal("33.34"))
            self.assertEqual(row["realized_broker_pnl"], Decimal("-3.34"))

    def test_realized_split_broker_vs_modeled_ex9_modeled_fee_basis(self):
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            pos = book.open_position(
                decision_id=DEC, order_id=ORD, symbol="AAPL", instrument_id=42,
                strategy_id="s1",
                fills=[_fill(Decimal("10"), Decimal("1990.00"))],
                modeled=_modeled(ModeledUSD("1985.00")), opened_ts_utc="t")
            sell, _ = _sell_fill(qty=Decimal("10"), proceeds=Decimal("2000.00"),
                                 avg=Decimal("200.00"))
            row = book.close_position(
                position_id=pos.position_id, order_id=ORD2, fills=[sell],
                modeled=_modeled(ModeledUSD("990.00")),
                reason="strategy_exit", decision_id=DEC2)
            # broker side: 2000.00 − 1990.00 = 10.00, fee-free
            self.assertEqual(row["realized_broker_pnl"], Decimal("10.00"))
            self.assertIsInstance(row["realized_broker_pnl"], BrokerUSD)
            # EX-9: the fee notional is the MODELED exit proceeds (990.00):
            # sec = ceil(990×0.0000278) = 0.03 — NOT ceil(2000×rate) = 0.06
            self.assertEqual(row["fees_assessed"]["sec_usd"], Decimal("0.03"))
            self.assertEqual(row["fees_assessed"]["taf_usd"], Decimal("0.01"))
            # realistic: 990.00 − 1985.00 − 0.04 = −995.04
            self.assertEqual(row["realized_modeled_pnl"], Decimal("-995.04"))
            self.assertIsInstance(row["realized_modeled_pnl"], ModeledUSD)
            self.assertEqual(row["closed_slice_modeled_cost_usd"],
                             Decimal("1985.00"))
            self.assertEqual(row["residual_modeled_cost_usd"], Decimal("0.00"))

    def test_taf_cap_boundary_at_exactly_8_30(self):
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            # qty 50000: 50000 × 0.000166 = 8.30 EXACTLY — at the cap, uncapped
            pos = book.open_position(
                decision_id=DEC, order_id=ORD, symbol="AAPL", instrument_id=42,
                strategy_id="s1",
                fills=[_fill(Decimal("50000"), Decimal("500000.00"))],
                modeled=_modeled(ModeledUSD("500000.00")), opened_ts_utc="t")
            sell, _ = _sell_fill(qty=Decimal("50000"),
                                 proceeds=Decimal("505000.00"),
                                 avg=Decimal("10.10"))
            row = book.close_position(
                position_id=pos.position_id, order_id=ORD2, fills=[sell],
                modeled=_modeled(ModeledUSD("502500.00")),
                reason="strategy_exit", decision_id=DEC2)
            self.assertEqual(row["fees_assessed"]["taf_usd"],
                             TAF_CAP_PER_TRADE_USD)               # == 8.30
            # sec over the MODELED proceeds: ceil(502500×0.0000278) = 13.97
            self.assertEqual(row["fees_assessed"]["sec_usd"], Decimal("13.97"))

    def test_taf_capped_one_share_over_the_boundary(self):
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            # qty 50001: ceil-cent(8.30016...) = 8.31 -> CAPPED to 8.30
            pos = book.open_position(
                decision_id=DEC, order_id=ORD, symbol="AAPL", instrument_id=42,
                strategy_id="s1",
                fills=[_fill(Decimal("50001"), Decimal("500010.00"))],
                modeled=_modeled(ModeledUSD("500010.00")), opened_ts_utc="t")
            sell, _ = _sell_fill(qty=Decimal("50001"),
                                 proceeds=Decimal("505010.10"),
                                 avg=Decimal("10.10"))
            row = book.close_position(
                position_id=pos.position_id, order_id=ORD2, fills=[sell],
                modeled=_modeled(ModeledUSD("502510.05")),
                reason="strategy_exit", decision_id=DEC2)
            self.assertEqual(row["fees_assessed"]["taf_usd"],
                             TAF_CAP_PER_TRADE_USD)               # capped

    def test_modeled_exit_none_no_fee_computed_realistic_unassessed(self):
        # EX-9: modeled exit None => NO fee computed (the zero block is
        # journaled), realistic side None — even when the position HAS an
        # open-side modeled cost (the EX-4 slices still journal).
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            pos = _open_aapl(book)   # modeled 10018.00
            sell, _ = _sell_fill(qty=Decimal("40"), proceeds=Decimal("4040.00"),
                                 avg=Decimal("101.00"))
            row = book.close_position(
                position_id=pos.position_id, order_id=ORD2, fills=[sell],
                modeled=None, reason="strategy_exit", decision_id=DEC2)
            self.assertEqual(row["fees_assessed"],
                             {"model_version": FEE_MODEL_VERSION,
                              "sec_usd": Decimal("0.00"),
                              "taf_usd": Decimal("0.00"),
                              "total_usd": Decimal("0.00")})
            self.assertIsNone(row["realized_modeled_pnl"])
            self.assertEqual(row["closed_slice_modeled_cost_usd"],
                             Decimal("4007.20"))
            self.assertEqual(row["residual_modeled_cost_usd"],
                             Decimal("6010.80"))
            self.assertEqual(row["realized_broker_pnl"], Decimal("32.00"))

    def test_close_guards(self):
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            pos = _open_aapl(book, fills=[_fill(Decimal("3"),
                                                Decimal("100.01"))],
                             modeled=None)
            sell_4, _ = _sell_fill(qty=Decimal("4"), proceeds=Decimal("140.00"),
                                   avg=Decimal("35.00"))
            with self.assertRaises(ExecError):   # may flatten, never flip
                book.close_position(position_id=pos.position_id, order_id=ORD2,
                                    fills=[sell_4], modeled=None,
                                    reason="strategy_exit", decision_id=DEC2)
            sell, _ = _sell_fill(qty=Decimal("3"), proceeds=Decimal("105.00"),
                                 avg=Decimal("35.00"))
            with self.assertRaises(ExecError):   # reason in CLOSE_REASONS
                book.close_position(position_id=pos.position_id, order_id=ORD2,
                                    fills=[sell], modeled=None,
                                    reason="felt_like_it", decision_id=DEC2)
            book.close_position(position_id=pos.position_id, order_id=ORD2,
                                fills=[sell], modeled=None,
                                reason="strategy_exit", decision_id=DEC2)
            with self.assertRaises(ExecError):   # already closed (terminal)
                book.close_position(position_id=pos.position_id, order_id=ORD3,
                                    fills=[sell], modeled=None,
                                    reason="strategy_exit", decision_id=DEC3)


class TestRehydrate(unittest.TestCase):
    """§K: pure fold by ascending seq; rehydrated == live BYTE-exact, incl.
    partial close — journaled through a REAL ExecLedger, replayed, folded."""

    def assertPositionBytes(self, live: PaperPosition, rehydrated: PaperPosition):
        self.assertEqual(live, rehydrated)                  # dataclass equality
        self.assertEqual(str(live.qty), str(rehydrated.qty))
        self.assertEqual(str(live.broker_cost_usd),
                         str(rehydrated.broker_cost_usd))   # byte-exact money
        self.assertIsInstance(rehydrated.broker_cost_usd, BrokerUSD)
        if live.modeled_cost_usd is None:
            self.assertIsNone(rehydrated.modeled_cost_usd)
        else:
            self.assertEqual(str(live.modeled_cost_usd),
                             str(rehydrated.modeled_cost_usd))
            self.assertIsInstance(rehydrated.modeled_cost_usd, ModeledUSD)
        for field in ("sec_usd", "taf_usd", "total_usd"):
            self.assertEqual(str(getattr(live.fee_assumption, field)),
                             str(getattr(rehydrated.fee_assumption, field)))

    def _lifecycle(self, tmpdir):
        """Full open -> apply_fill×2 -> mark/pnl -> partial close -> mark/pnl ->
        full close lifecycle, every row through the REAL ExecLedger."""
        ledger, paths = _ledger(tmpdir)
        book = PaperBook(ledger=ledger, run_id="run-1",
                         quote_staleness_ms_max=STALENESS_MS,
                         spread_bps_max=SPREAD_BPS)
        deltas = _fixture_deltas()
        # pre-position opening fill rides position_id=None (the exec_ledger
        # pre-position convention); it is BAKED into position_open
        d1, cur1 = deltas[0]
        ledger.record_broker_fill(delta=d1, cur=cur1, position_id=None,
                                  liquidity_flag=None, venue=None,
                                  decision_id=DEC, order_id=ORD)
        pos = book.open_position(
            decision_id=DEC, order_id=ORD, symbol="AAPL", instrument_id=42,
            strategy_id="synthetic.scripted_v1", fills=[d1],
            modeled=_modeled(ModeledUSD("10018.00")),
            opened_ts_utc="2026-06-10T18:31:04+00:00")
        pid = pos.position_id
        # post-open increments: journal broker_fill (orchestrator duty) + feed
        for delta, cur in deltas[1:]:
            ledger.record_broker_fill(delta=delta, cur=cur, position_id=pid,
                                      liquidity_flag=None, venue=None,
                                      decision_id=DEC, order_id=ORD)
            book.apply_fill(pid, delta)
        # marks/pnl rows (must NOT fold)
        book.mark(pid, _quote(), now_ms=1500, bar_key="bk1")
        book.pnl_snapshot(pid, bar_key="bk1")
        # partial close 40 @ 101.00
        sell1, sell_cur1 = _sell_fill(qty=Decimal("40"),
                                      proceeds=Decimal("4040.00"),
                                      avg=Decimal("101.00"))
        ledger.record_broker_fill(delta=sell1, cur=sell_cur1, position_id=pid,
                                  liquidity_flag=None, venue=None,
                                  decision_id=DEC2, order_id=ORD2)
        book.close_position(position_id=pid, order_id=ORD2, fills=[sell1],
                            modeled=_modeled(ModeledUSD("4035.00")),
                            reason="strategy_exit", decision_id=DEC2)
        # a fills-stream LABEL row (must be ignored by the fold)
        ledger.record_fill_divergence(
            side="sell", broker_cost_usd=BrokerUSD("4040.00"),
            modeled_cost_usd=ModeledUSD("4035.00"),
            divergence_usd=Decimal("5.00"), divergence_bps=Decimal("12.39"),
            flag="broker_optimistic", order_id=ORD2)
        book.set_divergence_flag(pid, "broker_optimistic")
        book.mark(pid, _quote(bid=Decimal("101.20"), ask=Decimal("101.22"),
                              seen_at_ms=2000), now_ms=2500, bar_key="bk2")
        book.pnl_snapshot(pid, bar_key="bk2")
        # full close 60 @ 101.50
        sell2, sell_cur2 = _sell_fill(qty=Decimal("60"),
                                      proceeds=Decimal("6090.00"),
                                      avg=Decimal("101.50"), order_id=ORD3,
                                      broker_order_id="b-close2")
        ledger.record_broker_fill(delta=sell2, cur=sell_cur2, position_id=pid,
                                  liquidity_flag=None, venue=None,
                                  decision_id=DEC3, order_id=ORD3)
        book.close_position(position_id=pid, order_id=ORD3, fills=[sell2],
                            modeled=_modeled(ModeledUSD("6080.00")),
                            reason="strategy_exit", decision_id=DEC3)
        return book, pid, paths

    def test_fold_equals_live_byte_exact_incl_partial_close(self):
        with TemporaryDirectory() as tmpdir:
            book, pid, paths = self._lifecycle(tmpdir)
            live = book.position(pid)
            # live end state, hand-verified: fully closed, residue washed out
            self.assertEqual(live.status, "closed")
            self.assertEqual(str(live.qty), "0")
            self.assertEqual(str(live.broker_cost_usd), "0.00")
            self.assertEqual(str(live.modeled_cost_usd), "0.00")
            rehydrated = PaperBook.rehydrate(
                replay_positions(paths["positions"]),
                replay_fills(paths["fills"]))
            self.assertEqual(set(rehydrated), {pid})
            self.assertPositionBytes(live, rehydrated[pid])

    def test_fold_mid_lifecycle_snapshot_after_partial_close(self):
        # Stop the journal after the PARTIAL close: the fold must show the
        # surviving slice (qty 60, residuals 6012.00 / 6010.80) byte-exact.
        with TemporaryDirectory() as tmpdir:
            ledger, paths = _ledger(tmpdir)
            book = PaperBook(ledger=ledger, run_id="run-1",
                             quote_staleness_ms_max=STALENESS_MS,
                             spread_bps_max=SPREAD_BPS)
            deltas = _fixture_deltas()
            d1, cur1 = deltas[0]
            ledger.record_broker_fill(delta=d1, cur=cur1, position_id=None,
                                      liquidity_flag=None, venue=None,
                                      decision_id=DEC, order_id=ORD)
            pos = book.open_position(
                decision_id=DEC, order_id=ORD, symbol="AAPL", instrument_id=42,
                strategy_id="s1", fills=[d1],
                modeled=_modeled(ModeledUSD("10018.00")), opened_ts_utc="t")
            pid = pos.position_id
            for delta, cur in deltas[1:]:
                ledger.record_broker_fill(delta=delta, cur=cur,
                                          position_id=pid, liquidity_flag=None,
                                          venue=None, decision_id=DEC,
                                          order_id=ORD)
                book.apply_fill(pid, delta)
            sell1, sell_cur1 = _sell_fill(qty=Decimal("40"),
                                          proceeds=Decimal("4040.00"),
                                          avg=Decimal("101.00"))
            ledger.record_broker_fill(delta=sell1, cur=sell_cur1,
                                      position_id=pid, liquidity_flag=None,
                                      venue=None, decision_id=DEC2,
                                      order_id=ORD2)
            close_row = book.close_position(
                position_id=pid, order_id=ORD2, fills=[sell1],
                modeled=_modeled(ModeledUSD("4035.00")),
                reason="strategy_exit", decision_id=DEC2)
            # EX-4 outputs journaled (slice AND residual, both lineages)
            self.assertEqual(close_row["closed_slice_broker_cost_usd"],
                             Decimal("4008.00"))
            self.assertEqual(close_row["residual_broker_cost_usd"],
                             Decimal("6012.00"))
            self.assertEqual(close_row["closed_slice_modeled_cost_usd"],
                             Decimal("4007.20"))
            self.assertEqual(close_row["residual_modeled_cost_usd"],
                             Decimal("6010.80"))
            self.assertEqual(close_row["realized_broker_pnl"], Decimal("32.00"))
            # realistic: 4035.00 − 4007.20 − 0.13 = 27.67
            self.assertEqual(close_row["realized_modeled_pnl"],
                             Decimal("27.67"))
            live = book.position(pid)
            self.assertEqual(live.status, "open")
            rehydrated = PaperBook.rehydrate(
                replay_positions(paths["positions"]),
                replay_fills(paths["fills"]))
            self.assertPositionBytes(live, rehydrated[pid])
            self.assertEqual(str(rehydrated[pid].qty), "60")
            self.assertEqual(str(rehydrated[pid].broker_cost_usd), "6012.00")
            self.assertEqual(str(rehydrated[pid].modeled_cost_usd), "6010.80")

    def test_pluggable_into_rehydrate_exec_state_seam(self):
        # exec_ledger §P.1: the orchestrator passes
        # book_rehydrate=PaperBook.rehydrate once §K lands — prove the fit.
        with TemporaryDirectory() as tmpdir:
            book, pid, paths = self._lifecycle(tmpdir)
            state = rehydrate_exec_state(
                [], replay_fills(paths["fills"]),
                replay_positions(paths["positions"]), run_id="run-1",
                book_rehydrate=PaperBook.rehydrate)
            self.assertEqual(set(state["positions"]), {pid})
            self.assertPositionBytes(book.position(pid),
                                     state["positions"][pid])

    # -- hand-built-row fold edge cases (the §K fold conventions) ---------------

    def _open_row(self, seq, pid, *, qty="30", cost="3003.00", modeled=None):
        return {"event_type": "position_open", "seq": seq, "position_id": pid,
                "symbol": "AAPL", "instrument_id": 42, "side": "long",
                "qty": qty, "broker_cost_usd": cost,
                "modeled_cost_usd": modeled,
                "fee_assumption": {"model_version": FEE_MODEL_VERSION,
                                   "sec_usd": "0.00", "taf_usd": "0.00",
                                   "total_usd": "0.00"},
                "opening_order_id": ORD, "strategy_id": "s1",
                "opened_ts_utc": "t"}

    def _fill_row(self, seq, pid, *, side="buy", delta_qty="40",
                  delta_cost="4009.60", cum="70"):
        return {"event_type": "broker_fill", "seq": seq, "position_id": pid,
                "side": side, "delta_qty": delta_qty,
                "delta_cost_usd": delta_cost, "cum_filled_qty": cum}

    def _close_row(self, seq, pid, *, exit_qty, slice_b, resid_b):
        return {"event_type": "position_close", "seq": seq,
                "position_id": pid, "exit_qty": exit_qty,
                "closed_slice_broker_cost_usd": slice_b,
                "residual_broker_cost_usd": resid_b,
                "closed_slice_modeled_cost_usd": None,
                "residual_modeled_cost_usd": None}

    def test_baked_opening_fill_with_position_id_is_watermark_skipped(self):
        # Even if the orchestrator stamped the OPENING fill with the (derivable)
        # position_id, cum_filled_qty <= position_open.qty skips it: the open
        # row already bakes it in (FD-M5-18 telescoping; no double-count).
        pid = "pos-x"
        rehydrated = PaperBook.rehydrate(
            [self._open_row(1, pid)],
            [self._fill_row(1, pid, delta_qty="30", delta_cost="3003.00",
                            cum="30"),                       # baked (cum == qty)
             self._fill_row(2, pid)])                        # folds (cum 70 > 30)
        self.assertEqual(str(rehydrated[pid].qty), "70")
        self.assertEqual(str(rehydrated[pid].broker_cost_usd), "7012.60")

    def test_duplicate_polling_re_read_row_folds_once(self):
        pid = "pos-x"
        rehydrated = PaperBook.rehydrate(
            [self._open_row(1, pid)],
            [self._fill_row(1, pid), self._fill_row(2, pid)])  # same cum=70 twice
        self.assertEqual(str(rehydrated[pid].qty), "70")
        self.assertEqual(str(rehydrated[pid].broker_cost_usd), "7012.60")

    def test_sell_fills_and_label_rows_never_fold(self):
        pid = "pos-x"
        rehydrated = PaperBook.rehydrate(
            [self._open_row(1, pid)],
            [self._fill_row(1, pid, side="sell", delta_qty="10",
                            delta_cost="1010.00", cum="10"),
             {"event_type": "fill_divergence", "seq": 2, "side": "buy"}])
        self.assertEqual(str(rehydrated[pid].qty), "30")       # unchanged
        self.assertEqual(str(rehydrated[pid].broker_cost_usd), "3003.00")

    def test_buy_fill_for_unknown_position_raises(self):
        with self.assertRaises(ExecError):
            PaperBook.rehydrate([], [self._fill_row(1, "pos-ghost")])

    def test_duplicate_position_open_raises(self):
        pid = "pos-x"
        with self.assertRaises(ExecError):
            PaperBook.rehydrate([self._open_row(1, pid),
                                 self._open_row(2, pid)], [])

    def test_close_for_unknown_position_raises(self):
        with self.assertRaises(ExecError):
            PaperBook.rehydrate(
                [self._close_row(1, "pos-ghost", exit_qty="1",
                                 slice_b="33.34", resid_b="66.67")], [])

    def test_residual_drift_raises_fail_closed(self):
        pid = "pos-x"
        with self.assertRaises(ExecError):
            PaperBook.rehydrate(
                [self._open_row(1, pid, qty="3", cost="100.01"),
                 self._close_row(2, pid, exit_qty="1", slice_b="33.34",
                                 resid_b="66.68")],            # journal says 66.67
                [])

    def test_terminal_skip_after_full_close(self):
        pid = "pos-x"
        rehydrated = PaperBook.rehydrate(
            [self._open_row(1, pid, qty="3", cost="100.01"),
             self._close_row(2, pid, exit_qty="3", slice_b="100.01",
                             resid_b="0.00"),
             # a second close row for the now-terminal position: SKIPPED
             self._close_row(3, pid, exit_qty="3", slice_b="100.01",
                             resid_b="0.00")],
            [])
        self.assertEqual(rehydrated[pid].status, "closed")
        self.assertEqual(str(rehydrated[pid].qty), "0")

    def test_mark_and_pnl_rows_do_not_fold_and_bad_event_type_raises(self):
        pid = "pos-x"
        rehydrated = PaperBook.rehydrate(
            [self._open_row(1, pid),
             {"event_type": "mark", "seq": 2, "position_id": pid},
             {"event_type": "pnl_snapshot", "seq": 3, "position_id": pid}],
            [])
        self.assertEqual(str(rehydrated[pid].broker_cost_usd), "3003.00")
        with self.assertRaises(ExecError):
            PaperBook.rehydrate(
                [{"event_type": "vibes", "seq": 1, "position_id": pid}], [])

    def test_fold_is_order_independent_sorted_by_seq(self):
        pid = "pos-x"
        # open 30 @ 3003.00, fold buy 40 @ 4009.60 -> 70 @ 7012.60, then
        # close 10 with journaled slice 1001.00 -> residual 6011.60.
        rows_p = [self._open_row(1, pid),
                  self._close_row(2, pid, exit_qty="10", slice_b="1001.00",
                                  resid_b="6011.60")]
        rows_f = [self._fill_row(1, pid)]
        forward = PaperBook.rehydrate(rows_p, rows_f)
        backward = PaperBook.rehydrate(list(reversed(rows_p)),
                                       list(reversed(rows_f)))
        self.assertEqual(forward, backward)
        self.assertEqual(str(forward[pid].qty), "60")
        self.assertEqual(str(forward[pid].broker_cost_usd), "6011.60")


class TestBookGuards(unittest.TestCase):
    def test_ctor_validation(self):
        with TemporaryDirectory() as tmpdir:
            ledger, _ = _ledger(tmpdir)
            with self.assertRaises(ExecError):
                PaperBook(ledger=ledger, run_id="", quote_staleness_ms_max=2000,
                          spread_bps_max=SPREAD_BPS)
            with self.assertRaises(ExecError):
                PaperBook(ledger=ledger, run_id="r", quote_staleness_ms_max=0,
                          spread_bps_max=SPREAD_BPS)
            with self.assertRaises(ExecError):
                PaperBook(ledger=ledger, run_id="r", quote_staleness_ms_max=True,
                          spread_bps_max=SPREAD_BPS)
            with self.assertRaises(ExecError):
                PaperBook(ledger=ledger, run_id="r",
                          quote_staleness_ms_max=2000, spread_bps_max=50.0)

    def test_journal_failure_leaves_book_unchanged(self):
        # validate -> journal -> commit: a ledger refusal (bad decision id
        # prefix) must leave the book empty.
        with TemporaryDirectory() as tmpdir:
            book, _, paths = _book(tmpdir)
            with self.assertRaises(ExecError):
                book.open_position(
                    decision_id="probe-1",   # wrong §P.3 prefix => ledger raises
                    order_id=ORD, symbol="AAPL", instrument_id=42,
                    strategy_id="s1",
                    fills=[_fill(Decimal("1"), Decimal("10.00"))],
                    modeled=None, opened_ts_utc="t")
            self.assertEqual(book._positions, {})
            self.assertEqual(replay_positions(paths["positions"]), [])

    def test_apply_fill_guards(self):
        with TemporaryDirectory() as tmpdir:
            book, _, _ = _book(tmpdir)
            with self.assertRaises(ExecError):
                book.apply_fill("pos-unknown", _fill(Decimal("1"),
                                                     Decimal("10.00")))
            pos = _open_aapl(book, fills=[_fill(Decimal("1"),
                                                Decimal("10.00"))],
                             modeled=None)
            with self.assertRaises(ExecError):   # duck shape: missing attribute
                book.apply_fill(pos.position_id,
                                SimpleNamespace(delta_qty=Decimal("1")))
            sell, _ = _sell_fill(qty=Decimal("1"), proceeds=Decimal("11.00"),
                                 avg=Decimal("11.00"))
            book.close_position(position_id=pos.position_id, order_id=ORD2,
                                fills=[sell], modeled=None,
                                reason="strategy_exit", decision_id=DEC2)
            with self.assertRaises(ExecError):   # closed position is terminal
                book.apply_fill(pos.position_id, _fill(Decimal("1"),
                                                       Decimal("10.00")))

    def test_dataclass_structural_guards(self):
        pos_kwargs = dict(
            position_id="pos-x", symbol="AAPL", instrument_id=42, side="long",
            qty=Decimal("1"), broker_cost_usd=BrokerUSD("10.00"),
            modeled_cost_usd=None,
            fee_assumption=None, opening_order_id=ORD, strategy_id="s1",
            opened_ts_utc="t", status="open")
        pos = PaperPosition(**pos_kwargs)
        with self.assertRaises(ExecError):
            replace(pos, side="short")           # long-only in M5
        with self.assertRaises(ExecError):
            replace(pos, status="half_open")     # closed vocabulary


if __name__ == "__main__":
    unittest.main()
