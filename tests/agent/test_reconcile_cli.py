"""M6 W5: `agent reconcile` CLI surface, exit codes, and paper SOD mapping."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from agent import __main__ as cli
from agent.broker.alpaca import AlpacaPaperBroker
from agent.exec_ledger import ExecLedger
from agent.execution_config import ExecutionConfig
from agent.paper_book import PaperBook
from agent.reconcile_ledger import ReconcileLedger, replay_reconcile
from agent.run_lock import LOCK_FILENAME, RunLock
from agent.serializer import BrokerUSD, row_hash
from recorder.persistence import EventWriter
from tests.lib.alpaca_fixtures import (
    ScriptedOrderApi,
    account_payload,
    http_error,
    positions_payload,
)
from tests.lib.exec_fixtures import (
    CALENDAR_FIXTURE_PATH,
    FIXED_WRITER_TS,
    REPO_ROOT,
    permissive_paper_fixture_config,
)

_ROW_CLOCK = lambda: FIXED_WRITER_TS  # noqa: E731


def _write_credentials(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "alpaca_paper.json").write_text(json.dumps({
        "key_id": "k",
        "secret_key": "s",
        "base_url": "https://paper-api.alpaca.markets",
    }), encoding="utf-8")


def _broker_position(*, qty="12", avg="200.00", symbol="AAPL") -> dict:
    row = dict(positions_payload()[0])
    row.update({
        "symbol": symbol,
        "qty": qty,
        "avg_entry_price": avg,
        "market_value": str(Decimal(qty) * Decimal(avg)),
        "cost_basis": str(Decimal(qty) * Decimal(avg)),
    })
    return row


def _seed_open_position(journal_dir: Path, *, qty="10", avg="200.00") -> str:
    journal_dir.mkdir(parents=True, exist_ok=True)
    config = permissive_paper_fixture_config()
    rules_hash = ExecutionConfig.from_config(config).rules_hash
    ledger = ExecLedger(
        orders=EventWriter(journal_dir / "orders.jsonl", "run-prior",
                           clock=_ROW_CLOCK),
        fills=EventWriter(journal_dir / "fills.jsonl", "run-prior",
                          clock=_ROW_CLOCK),
        positions=EventWriter(journal_dir / "positions.jsonl", "run-prior",
                              clock=_ROW_CLOCK),
        rules_hash=rules_hash)
    book = PaperBook(ledger=ledger, run_id="run-prior",
                     quote_staleness_ms_max=2000,
                     spread_bps_max=Decimal("50"))
    fill = type("Fill", (), {
        "delta_qty": Decimal(qty),
        "delta_cost_usd": BrokerUSD(Decimal(qty) * Decimal(avg)),
    })()
    return book.open_position(
        decision_id="d-" + row_hash({"m6-cli-open": qty}),
        order_id="o-" + row_hash({"m6-cli-open": qty}),
        symbol="AAPL", instrument_id=1001, strategy_id="stub.real_v1",
        fills=[fill], modeled=None,
        opened_ts_utc="2026-06-15T13:45:00.000000Z").position_id


def _seed_cash_baseline(journal_dir: Path, *, cash="40000.00") -> None:
    journal_dir.mkdir(parents=True, exist_ok=True)
    ledger = ReconcileLedger(
        EventWriter(journal_dir / "reconcile_alerts.jsonl", "run-prior",
                    clock=_ROW_CLOCK),
        rules_hash=ExecutionConfig.from_config(
            permissive_paper_fixture_config()).rules_hash)
    ledger.record_reconcile_baseline(
        reconcile_id="rc-baseline", session_date_et="2026-06-15",
        cash_usd=BrokerUSD(cash), equity_usd=BrokerUSD(cash),
        buying_power_usd=BrokerUSD("200000.00"), fills_seq_watermark=0,
        positions=[], durable_seeded=[])


class TestReconcileCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="m6-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    @contextlib.contextmanager
    def patched_cli(self, script):
        secrets = self.tmp / "secrets"
        _write_credentials(secrets)
        api = ScriptedOrderApi(script)

        def broker_factory(*, credentials_loader=None, **kwargs):
            self.assertIsNotNone(credentials_loader)
            self.assertEqual(kwargs, {})
            return AlpacaPaperBroker(order_api=api)

        with mock.patch.object(cli, "_SECRETS", secrets), \
                mock.patch("agent.broker.alpaca.AlpacaPaperBroker",
                           broker_factory):
            yield api

    def run_main(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = cli.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def reconcile_argv(self, journal_dir, *extra):
        return [
            "reconcile",
            "--journal-dir", str(journal_dir),
            "--calendar-fixture", str(CALENDAR_FIXTURE_PATH),
            *extra,
        ]

    def test_parser_exposes_reconcile_flags(self):
        overlay = self.tmp / "overlay.json"
        args = cli.build_parser().parse_args(self.reconcile_argv(
            self.tmp / "journal", "--overlay", str(overlay),
            "--rebaseline-cash"))
        self.assertEqual(args.command, "reconcile")
        self.assertEqual(args.overlay, str(overlay))
        self.assertTrue(args.rebaseline_cash)

    def test_clean_reconcile_cli_exits_0_and_prints_summary(self):
        journal = self.tmp / "clean"
        with self.patched_cli({
            "get_account": [account_payload()],
            "list_positions": [[]],
        }):
            rc, out, err = self.run_main(self.reconcile_argv(journal))
        self.assertEqual(rc, 0, err)
        self.assertIn("reconcile_id=", out)
        self.assertIn("phase=cli", out)
        rows = replay_reconcile(journal / "reconcile_alerts.jsonl")
        self.assertEqual(rows[-1]["event_type"], "reconcile_run")
        self.assertTrue(rows[-1]["clean"])

    def test_drift_reconcile_cli_exits_1_after_adjustment(self):
        journal = self.tmp / "drift"
        _seed_open_position(journal, qty="10")
        from agent.execution_preflight import _authorizations
        before = set(_authorizations)
        with self.patched_cli({
            "get_account": [account_payload(equity="42400.00", cash="40000.00")],
            "list_positions": [[_broker_position(qty="12")]],
        }) as api:
            rc, out, err = self.run_main(self.reconcile_argv(journal))
        self.assertEqual(rc, 1, err)
        self.assertEqual(api.submit_calls, [])
        self.assertEqual(api.cancel_calls, [])
        self.assertIn("drift_count=1", out)
        rows = replay_reconcile(journal / "reconcile_alerts.jsonl")
        self.assertEqual(rows[0]["kind"], "position_qty")
        self.assertEqual(rows[0]["action"], "adjusted")
        self.assertEqual(set(_authorizations) - before, set())

    def test_broker_unreadable_cli_exits_3(self):
        journal = self.tmp / "unreadable"
        with self.patched_cli({
            "get_account": [http_error("order_rejected_insufficient_bp")],
            "list_positions": [[]],
        }):
            rc, out, err = self.run_main(self.reconcile_argv(journal))
        self.assertEqual(rc, 3, err)
        self.assertIn("completed=false", out)
        rows = replay_reconcile(journal / "reconcile_alerts.jsonl")
        self.assertEqual(rows[0]["note"], "broker_read_failed")
        self.assertFalse(rows[-1]["completed"])

    def test_journal_corruption_cli_exits_3_without_reconcile_row(self):
        journal = self.tmp / "corrupt"
        journal.mkdir(parents=True)
        (journal / "orders.jsonl").write_text("{bad-json}\n", encoding="utf-8")
        with self.patched_cli({
            "get_account": [account_payload()],
            "list_positions": [[]],
        }):
            rc, out, err = self.run_main(self.reconcile_argv(journal))
        self.assertEqual(rc, 3)
        self.assertEqual(out, "")
        self.assertIn("journal corruption", err)
        self.assertFalse((journal / "reconcile_alerts.jsonl").exists())

    def test_credentials_missing_degrades_and_exits_3(self):
        journal = self.tmp / "missing-creds"
        with mock.patch.object(cli, "_SECRETS", self.tmp / "absent-secrets"):
            rc, out, err = self.run_main(self.reconcile_argv(journal))
        self.assertEqual(rc, 3, err)
        self.assertIn("completed=false", out)
        rows = replay_reconcile(journal / "reconcile_alerts.jsonl")
        self.assertEqual(rows[0]["note"], "reconcile_skipped_no_broker")

    def test_run_lock_held_maps_to_2_for_reconcile_and_paper(self):
        for command in ("reconcile", "paper"):
            journal = self.tmp / f"locked-{command}"
            journal.mkdir(parents=True)
            (journal / LOCK_FILENAME).write_text("not-a-pid\n", encoding="ascii")
            with self.patched_cli({}):
                if command == "reconcile":
                    argv = self.reconcile_argv(journal)
                else:
                    argv = [
                        "paper", "--journal-dir", str(journal),
                        "--calendar-fixture", str(CALENDAR_FIXTURE_PATH),
                    ]
                rc, _out, err = self.run_main(argv)
            self.assertEqual(rc, 2, command)
            self.assertIn("run lock", err)
            self.assertFalse((journal / "reconcile_alerts.jsonl").exists())

    def test_successful_cli_releases_run_lock_on_exit(self):
        journal = self.tmp / "lock-release"
        with self.patched_cli({
            "get_account": [account_payload()],
            "list_positions": [[]],
        }):
            rc, _out, err = self.run_main(self.reconcile_argv(journal))
        self.assertEqual(rc, 0, err)
        self.assertFalse((journal / LOCK_FILENAME).exists())
        lock = RunLock(journal)
        self.assertFalse(lock.acquire())
        lock.release()

    def test_pre_existing_latch_clears_on_completed_clean_cli_pass(self):
        journal = self.tmp / "latched-clean"
        journal.mkdir(parents=True, exist_ok=True)
        ledger = ReconcileLedger(
            EventWriter(journal / "reconcile_alerts.jsonl", "run-prior",
                        clock=_ROW_CLOCK),
            rules_hash=ExecutionConfig.from_config(
                permissive_paper_fixture_config()).rules_hash)
        ledger.record_reconcile(
            reconcile_id="rc-prior", drift_id="rd-prior",
            kind="position_unknown_broker", symbol="AAPL", field="qty",
            local="0", broker="10", diff="10", action="latched_operator",
            position_id=None, local_order_id=None, broker_order_id=None)
        ledger.record_reconcile_run(
            reconcile_id="rc-prior", phase="cli",
            session_date_et="2026-06-15", trigger_durable_key=None,
            broker_source="fixture", checked_symbols=["AAPL"],
            drift_count=1, adjusted_count=0, note_count=0,
            completed=True, clean=False)
        with self.patched_cli({
            "get_account": [account_payload()],
            "list_positions": [[]],
        }):
            rc, out, err = self.run_main(self.reconcile_argv(journal))
        self.assertEqual(rc, 0, err)
        self.assertIn("latched=false", out)

    def test_paper_sod_reconcile_maps_drift_to_1(self):
        journal = self.tmp / "paper-drift"
        _seed_open_position(journal, qty="10")
        with self.patched_cli({
            "get_account": [account_payload(equity="42400.00", cash="40000.00")],
            "list_positions": [[_broker_position(qty="12")]],
        }):
            rc, out, err = self.run_main([
                "paper", "--journal-dir", str(journal),
                "--calendar-fixture", str(CALENDAR_FIXTURE_PATH),
            ])
        self.assertEqual(rc, 1, err)
        self.assertIn("phase=sod", out)

    def test_paper_sod_completed_false_maps_to_3(self):
        journal = self.tmp / "paper-unreadable"
        with self.patched_cli({
            "get_account": [http_error("order_rejected_insufficient_bp")],
            "list_positions": [[]],
        }):
            rc, out, err = self.run_main([
                "paper", "--journal-dir", str(journal),
                "--calendar-fixture", str(CALENDAR_FIXTURE_PATH),
            ])
        self.assertEqual(rc, 3, err)
        self.assertIn("completed=false", out)

    def test_paper_degraded_observe_skips_sod_and_keeps_startup_zero(self):
        journal = self.tmp / "paper-degraded"
        with mock.patch.object(cli, "_SECRETS", self.tmp / "absent-secrets"):
            rc, out, err = self.run_main([
                "paper", "--journal-dir", str(journal),
                "--calendar-fixture", str(CALENDAR_FIXTURE_PATH),
            ])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")
        self.assertFalse((journal / "reconcile_alerts.jsonl").exists())

    def test_rebaseline_cash_flag_reanchors_cash_residue(self):
        journal = self.tmp / "rebaseline"
        _seed_cash_baseline(journal, cash="40000.00")
        with self.patched_cli({
            "get_account": [account_payload(equity="39900.00", cash="39900.00")],
            "list_positions": [[]],
        }):
            rc, out, err = self.run_main(self.reconcile_argv(
                journal, "--rebaseline-cash"))
        self.assertEqual(rc, 1, err)
        self.assertIn("drift_count=1", out)
        rows = replay_reconcile(journal / "reconcile_alerts.jsonl")
        cash_rows = [row for row in rows if row.get("kind") == "cash"]
        self.assertEqual(cash_rows[-1]["action"], "rebaselined")
        baselines = [row for row in rows
                     if row["event_type"] == "reconcile_baseline"]
        self.assertEqual(baselines[-1]["cash_usd"], "39900.00")

        with self.patched_cli({
            "get_account": [account_payload(equity="39900.00", cash="39900.00")],
            "list_positions": [[]],
        }):
            rc, out, err = self.run_main(self.reconcile_argv(journal))
        self.assertEqual(rc, 0, err)
        self.assertIn("latched=false", out)

    def test_cash_residue_without_rebaseline_carries_baseline_forward(self):
        journal = self.tmp / "cash-latched"
        _seed_cash_baseline(journal, cash="40000.00")
        with self.patched_cli({
            "get_account": [account_payload(equity="39900.00", cash="39900.00")],
            "list_positions": [[]],
        }):
            rc, out, err = self.run_main(self.reconcile_argv(journal))
        self.assertEqual(rc, 1, err)
        self.assertIn("latched=true", out)
        rows = replay_reconcile(journal / "reconcile_alerts.jsonl")
        cash_rows = [row for row in rows if row.get("kind") == "cash"]
        self.assertEqual(cash_rows[-1]["action"], "latched_operator")
        baselines = [row for row in rows
                     if row["event_type"] == "reconcile_baseline"]
        self.assertEqual(baselines[-1]["cash_usd"], "40000.00")

    def test_runbook_documents_exit_codes_and_cash_rebaseline(self):
        text = (REPO_ROOT / "docs" / "runbooks" / "m6-reconcile.md").read_text(
            encoding="utf-8")
        for needle in ("--rebaseline-cash", "| 0 |", "| 1 |", "| 2 |", "| 3 |"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
