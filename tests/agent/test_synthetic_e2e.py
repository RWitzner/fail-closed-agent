"""M5 §R 14 — the synthetic open→mark→close E2E + the byte goldens (S9/S1/S3/S6).

The full scripted run rides ``tests.lib.exec_fixtures.run_synthetic_golden``:
the REAL orchestrator over a FakeBroker (``partial_then_full``), the in-memory
permissive fixture config (FD-M5-3), the §Q ``status_script`` TRADABLE
injection (M5C-T4), EX-12-density quote delivery, and an explicit on-grid
limit on the open-driving script row (FD-M4-16: a limit-less open is
``unpriceable_candidate`` at ``can_open``).

GOLDEN REGENERATION DISCIPLINE (M5C-T5 — the M3 ``signal_pipeline`` mechanism
verbatim): the committed goldens under ``tests/fixtures/execution/golden/``
(``orders.jsonl`` / ``fills.jsonl`` / ``positions.jsonl``) are produced by
``run_synthetic_golden`` with its FROZEN constants (``GOLDEN_RUN_ID =
"run-m5-golden-v1"``, the pinned EventWriter row clock, the pinned script).
To regenerate after an intentional contract change: run the helper into a tmp
dir and COPY THE BYTES — never hand-edit a golden, never bake machine-local
bytes (no wall clock / host / pid ever reaches a journaled row).
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent import execution_preflight
from agent.exec_ledger import (
    rehydrate_exec_state,
    replay_fills,
    replay_orders,
    replay_positions,
)
from agent.paper_book import PaperBook
from agent.strategies.synthetic import ScriptedSyntheticStrategy

from tests.lib.exec_fixtures import (
    ExecPipeline,
    GOLDEN_DIR,
    GOLDEN_RUN_ID,
    run_synthetic_golden,
)

_STREAMS = ("orders", "fills", "positions")


def _open_kind_authorizations():
    """S1 registry introspection (the test_risk_kill / preflight pattern):
    the module-private registry must hold ZERO open-kind entries at exit."""
    return [auth for auth in execution_preflight._authorizations.values()
            if auth.kind == "open"]


class TestSyntheticGoldenE2E(unittest.TestCase):
    """One fresh deterministic run, asserted against the committed goldens."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        cls.journal_dir = Path(cls._tmp.name) / "golden"
        cls.pipeline = run_synthetic_golden(cls.journal_dir)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # -- S3/S6: byte goldens ------------------------------------------------

    def test_streams_byte_identical_to_committed_goldens(self):
        for stream in _STREAMS:
            fresh = (self.journal_dir / f"{stream}.jsonl").read_bytes()
            committed = (GOLDEN_DIR / f"{stream}.jsonl").read_bytes()
            self.assertEqual(
                fresh, committed,
                f"{stream}.jsonl bytes diverge from the committed golden — "
                "regeneration discipline: run run_synthetic_golden and copy "
                "bytes (M5C-T5)")

    # -- the §R 14 lifecycle ------------------------------------------------

    def test_all_four_streams_journaled(self):
        for stream in ("orders", "fills", "positions", "risk"):
            self.assertTrue(self.pipeline.rows(stream),
                            f"{stream}.jsonl should not be empty")

    def test_open_mark_close_lifecycle(self):
        decisions = self.pipeline.rows_of("orders", "strategy_decision")
        self.assertEqual([row["action"] for row in decisions],
                         ["would_open", "would_close"])
        # partial_then_full: each order walks accepted -> partially_filled
        # -> filled and lands order_terminal{filled}.
        updates = self.pipeline.rows_of("orders", "broker_order_update")
        self.assertEqual([row["to_state"] for row in updates],
                         ["partially_filled", "filled"] * 2)
        terminals = self.pipeline.rows_of("orders", "order_terminal")
        self.assertEqual([row["terminal_state"] for row in terminals],
                         ["filled", "filled"])
        fills = self.pipeline.rows_of("fills", "broker_fill")
        self.assertEqual(len(fills), 4)          # two slices per order
        # EC-1: a modeled_execution_fill for BOTH the open buy AND the close
        # sell (the close path now feeds a sell-side ModeledFill — §J/§K/EX-9).
        self.assertEqual(
            len(self.pipeline.rows_of("fills", "modeled_execution_fill")), 2)
        divergences = self.pipeline.rows_of("fills", "fill_divergence")
        self.assertEqual(len(divergences), 2)
        # EC-1/EX-3: the close (sell) divergence now carries a REAL side-aware
        # flag (not the dead hardcoded "unassessed").
        sell_div = [row for row in divergences if row["side"] == "sell"]
        self.assertEqual(len(sell_div), 1)
        self.assertNotEqual(sell_div[0]["flag"], "unassessed")
        opens = self.pipeline.rows_of("positions", "position_open")
        closes = self.pipeline.rows_of("positions", "position_close")
        self.assertEqual(len(opens), 1)
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["reason"], "synthetic_script")
        self.assertTrue(self.pipeline.rows_of("positions", "mark"))
        self.assertTrue(self.pipeline.rows_of("positions", "pnl_snapshot"))

    def test_correlation_chain_joins(self):
        """S6: decision_id -> risk_verdict -> order -> fill -> position."""
        decisions = self.pipeline.rows_of("orders", "strategy_decision")
        open_decision = decisions[0]["decision_id"]
        verdicts = self.pipeline.rows_of("risk", "risk_verdict")
        self.assertEqual([row["decision_id"] for row in verdicts],
                         [open_decision])
        self.assertIs(verdicts[0]["allowed"], True)
        attempts = self.pipeline.rows_of("orders", "order_submit_attempt")
        self.assertEqual(attempts[0]["decision_id"], open_decision)
        open_order = attempts[0]["order_id"]
        open_fills = [row for row in self.pipeline.rows_of("fills", "broker_fill")
                      if row["order_id"] == open_order]
        self.assertEqual(len(open_fills), 2)
        position_open = self.pipeline.rows_of("positions", "position_open")[0]
        self.assertEqual(position_open["opening_order_id"], open_order)
        self.assertEqual({row["position_id"] for row in open_fills},
                         {position_open["position_id"]})

    # -- FD-M5-28 + FD-M5-7 -------------------------------------------------

    def test_order_ids_synthetic_prefix_and_client_id_identity(self):
        attempts = self.pipeline.rows_of("orders", "order_submit_attempt")
        self.assertEqual(len(attempts), 2)
        for row in self.pipeline.rows("orders"):
            order_id = row.get("order_id")
            if order_id is not None:
                self.assertTrue(
                    order_id.startswith("synthetic-o-"),
                    f"FD-M5-28: synthetic order_id must ride the prefix, "
                    f"got {order_id!r}")
        for row in attempts:
            # FD-M5-7 (client_order_id == order_id); the M0 kill actuator's
            # flatten-<symbol> exception is NOT exercised in this E2E.
            self.assertEqual(row["client_order_id"], row["order_id"])

    # -- S1: registry hygiene ------------------------------------------------

    def test_registry_empty_of_open_kind_authorizations_at_exit(self):
        self.assertEqual(_open_kind_authorizations(), [])

    # -- S3: rehydrate == live -----------------------------------------------

    def test_rehydrate_reproduces_the_live_book_byte_exact(self):
        state = rehydrate_exec_state(
            replay_orders(self.journal_dir / "orders.jsonl"),
            replay_fills(self.journal_dir / "fills.jsonl"),
            replay_positions(self.journal_dir / "positions.jsonl"),
            run_id=GOLDEN_RUN_ID,
            book_rehydrate=PaperBook.rehydrate)
        live = self.pipeline.orch.book._positions
        self.assertEqual(state["positions"], live)
        # byte-exact, not merely numerically equal: Decimal("10") ==
        # Decimal("10.0") but their journal bytes differ — compare reprs.
        self.assertEqual(
            {pid: repr(pos) for pid, pos in state["positions"].items()},
            {pid: repr(pos) for pid, pos in live.items()})
        self.assertEqual(state["open_orders"], {})
        self.assertEqual(state["open_deny"], ())


class TestGateFailSkip(unittest.TestCase):
    """§R 14 / M5C-B2: a tick whose snapshot assembly gate-fails reaches no
    scan call, journals nothing on the strategy path, and does NOT advance the
    script ordinal."""

    def test_gate_fail_tick_no_scan_no_rows_ordinal_frozen(self):
        strategy = ScriptedSyntheticStrategy([
            # on_bar=1: due on the FIRST successful scan ordinal; explicit
            # on-grid limit (FD-M4-16 — limit-less opens are unpriceable).
            {"on_bar": 1, "action": "open", "symbol": "AAPL", "qty": "10",
             "limit": "210.00"},
        ])
        with TemporaryDirectory() as tmp:
            pipeline = ExecPipeline(
                journal_dir=Path(tmp) / "journal", run_id="run-m5-gatefail",
                strategy=strategy, exit_provider=strategy,
                fill_policy="partial_then_full")
            try:
                # bar 10: only 11 bars exist as-of -> the 51-bar feature gate
                # fails -> GateFail at snapshot assembly.
                pipeline.tick_on_bar(10)
                self.assertEqual(strategy._scan_count, 0)   # no scan call
                self.assertEqual(
                    pipeline.rows_of("orders", "strategy_decision"), [],
                    "a GateFail tick must journal nothing on the strategy path")
                self.assertEqual(
                    pipeline.rows_of("risk", "risk_verdict"), [])
                self.assertFalse(
                    (Path(tmp) / "journal" / "orders.jsonl").exists(),
                    "no strategy-path row may exist after a GateFail tick")

                # The ordinal did NOT advance: the on_bar=1 row fires on the
                # FIRST assembled tick (feature-complete bar 50).
                pipeline.tick_on_bar(50)
                self.assertEqual(strategy._scan_count, 1)
                decisions = pipeline.rows_of("orders", "strategy_decision")
                self.assertEqual(len(decisions), 1)
                self.assertEqual(decisions[0]["action"], "would_open")
            finally:
                pipeline.close()
        self.assertEqual(_open_kind_authorizations(), [])


if __name__ == "__main__":
    unittest.main()
