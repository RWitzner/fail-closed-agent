"""M5 §R 16 — the observe E2E over the COMMITTED fixture + byte goldens (S3/S1).

The run rides ``tests.lib.exec_fixtures.run_observe_golden``: the REAL
orchestrator driven by a ``ReplayQuoteFeed`` over the COMMITTED §Q fixture
``tests/fixtures/execution/observe_session_tbbo.jsonl`` + the REAL committed
config (gates OFF — observe mints nothing and constructs NO Broker-Protocol
instance, FD-M5-4), with the §N/M5C-T4 status-injection seam supplying
TRADABLE windows (EQUS.MINI has no status schema; without injection every
probe tick gate-fails ``market_state_not_tradable`` and the funnel never
reaches a forecast).

GOLDEN REGENERATION DISCIPLINE (M5C-T5 — the M3 ``signal_pipeline`` mechanism
verbatim): the committed goldens under ``tests/fixtures/execution/golden/``
(``observe_decisions.jsonl`` / ``observe_scored.jsonl`` /
``observe_report.json``) are produced by ``run_observe_golden`` with its
FROZEN constants (``GOLDEN_RUN_ID = "run-m5-golden-v1"``, the pinned
EventWriter row clock, the pinned ``GOLDEN_GENERATED_TS`` report timestamp)
over the committed fixture, itself produced by
``exec_fixtures.write_observe_session_fixture`` (pinned run_id + row clock).
To regenerate after an intentional contract change: run the helper(s) and
COPY THE BYTES — never hand-edit a golden or the fixture, never bake
machine-local bytes (no wall clock / host / pid ever reaches a journaled row).
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.journal import replay
from recorder.event import QuoteEvent, parse
from recorder.event_row import to_row
from recorder.persistence import replay_stream

from tests.lib.exec_fixtures import (
    GOLDEN_DIR,
    OBSERVE_FIXTURE_PATH,
    run_observe_golden,
    write_events_jsonl,
    write_observe_session_fixture,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_VENDOR_SAMPLE = (REPO_ROOT / "tests" / "fixtures" / "databento"
                     / "equs_mini_tbbo_sample.jsonl")


def _broker_like(obj) -> bool:
    """A Broker-Protocol instance: any BrokerBase, or any non-class object
    exposing the chokepoint order surface (submit_order + order_status)."""
    from agent.broker.base import BrokerBase

    if isinstance(obj, BrokerBase):
        return True
    if isinstance(obj, type):
        return False
    return (callable(getattr(obj, "submit_order", None))
            and callable(getattr(obj, "order_status", None)))


def _walk_object_graph(root) -> list:
    """The §R object-graph walk: every object reachable from ``root`` through
    instance attributes and stdlib containers; returns the broker-like hits."""
    seen, stack, found = set(), [root], []
    while stack:
        obj = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if _broker_like(obj):
            found.append(obj)
            continue
        if isinstance(obj, dict):
            stack.extend(obj.keys())
            stack.extend(obj.values())
        elif isinstance(obj, (list, tuple, set, frozenset)):
            stack.extend(obj)
        attrs = getattr(obj, "__dict__", None)
        if isinstance(attrs, dict):
            stack.extend(attrs.values())
    return found


class TestCommittedObserveFixture(unittest.TestCase):
    """The §Q fixture-row obligations, asserted against the COMMITTED bytes."""

    def test_builder_reproduces_the_committed_bytes(self):
        with TemporaryDirectory() as tmp:
            regenerated = write_observe_session_fixture(
                Path(tmp) / "observe_session_tbbo.jsonl")
            self.assertEqual(regenerated.read_bytes(),
                             OBSERVE_FIXTURE_PATH.read_bytes(),
                             "the committed fixture must be byte-reproducible "
                             "by the reviewed builder (M5C-T5)")

    def test_fixture_shape_satisfies_the_q_row(self):
        rows = replay_stream(OBSERVE_FIXTURE_PATH)   # hash-verified envelope
        self.assertTrue(rows)
        # one symbol
        self.assertEqual(sorted({row["symbol"] for row in rows}), ["AAPL"])
        # >= 60 one-minute buckets (the 51-bar feature gate opens)
        buckets = {row["ts_event_utc"][:16] for row in rows}
        self.assertGreaterEqual(len(buckets), 60)
        # >= 2 whole-second ts_recv_utc rows (EX-5 mixed-ISO discipline)
        whole_second = [row for row in rows if "." not in row["ts_recv_utc"]]
        self.assertGreaterEqual(len(whole_second), 2)
        # an epoch flip MID-session: both epochs present, flip strictly inside
        epochs = [row["reconnect_epoch"] for row in rows]
        self.assertEqual(sorted(set(epochs)), [0, 1])
        flip_at = epochs.index(1)
        self.assertGreater(flip_at, 0)
        self.assertLess(flip_at, len(rows) - 1)
        self.assertTrue(all(epoch == 1 for epoch in epochs[flip_at:]))


class TestObserveGoldenE2E(unittest.TestCase):
    """One fresh deterministic observe run vs the committed goldens."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        cls.journal_dir = Path(cls._tmp.name) / "observe"
        cls.result = run_observe_golden(cls.journal_dir)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_decisions_and_scored_byte_identical_to_goldens(self):
        pairs = (("decisions.jsonl", "observe_decisions.jsonl"),
                 ("forecast_scored.jsonl", "observe_scored.jsonl"))
        for fresh_name, golden_name in pairs:
            fresh = (self.journal_dir / fresh_name).read_bytes()
            committed = (GOLDEN_DIR / golden_name).read_bytes()
            self.assertEqual(
                fresh, committed,
                f"{fresh_name} bytes diverge from {golden_name} — "
                "regeneration discipline: run run_observe_golden and copy "
                "bytes (M5C-T5)")

    def test_report_byte_identical_to_golden(self):
        self.assertEqual(
            self.result["report_path"].read_bytes(),
            (GOLDEN_DIR / "observe_report.json").read_bytes())
        # the returned dict is the same report that was written
        written = json.loads(self.result["report_path"].read_text(
            encoding="utf-8"))
        self.assertEqual(written["run_id"], "run-m5-golden-v1")

    def test_probe_decisions_and_scored_rows_nonempty(self):
        decisions = replay(self.journal_dir / "decisions.jsonl")
        scored = replay(self.journal_dir / "forecast_scored.jsonl")
        self.assertGreater(len(decisions), 0)
        self.assertGreater(len(scored), 0)
        self.assertGreater(self.result["report"]["funnel"]["forecasts"], 0)
        self.assertGreater(self.result["report"]["funnel"]["scored"], 0)

    def test_observe_journals_no_exec_or_risk_stream(self):
        """Observe mints nothing: the exec/risk ledgers never receive a row,
        so their stream files are never even created."""
        for stream in ("orders", "fills", "positions", "risk",
                       "reconcile_alerts"):
            self.assertFalse(
                (self.journal_dir / f"{stream}.jsonl").exists(),
                f"observe wrote {stream}.jsonl")

    def test_no_broker_in_the_object_graph(self):
        orch = self.result["orchestrator"]
        self.assertEqual(orch.mode, "observe")
        self.assertIsNone(orch.broker)
        hits = _walk_object_graph(orch)
        self.assertEqual(
            hits, [],
            "observe composed a Broker-Protocol instance (FD-M5-4 violation): "
            f"{[type(hit).__name__ for hit in hits]}")


class TestObserveCliSubprocess(unittest.TestCase):
    """The lazy mode-select import (M5C-T10), witnessed in a FRESH process:
    a full observe CLI run never pulls agent.broker.alpaca (or any SDK) into
    sys.modules. Fixed command array, PYTHONPATH=scripts — the
    test_no_network_no_creds subprocess pattern."""

    def test_observe_cli_run_keeps_alpaca_out_of_sys_modules(self):
        with TemporaryDirectory() as tmp:
            code = (
                "import sys\n"
                "from agent.__main__ import main\n"
                "rc = main(['observe', '--events', sys.argv[1],\n"
                "           '--journal-dir', sys.argv[2]])\n"
                "assert rc == 0, f'observe CLI exit {rc}'\n"
                "assert 'agent.broker.alpaca' not in sys.modules, \\\n"
                "    'observe pulled the alpaca adapter (M5C-T10)'\n"
                "assert 'agent.broker.fake' not in sys.modules\n"
                "assert 'agent.broker.flatten_proxy' not in sys.modules\n"
                "assert 'alpaca' not in sys.modules\n"
                "assert 'databento' not in sys.modules\n"
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(REPO_ROOT / "scripts")
            argv = [sys.executable, "-c", code,
                    str(OBSERVE_FIXTURE_PATH), tmp]   # fixed array, no shell
            completed = subprocess.run(argv, env=env, capture_output=True,
                                       text=True)
            self.assertEqual(
                completed.returncode, 0,
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}")


class TestRawVendorSampleParserSmoke(unittest.TestCase):
    """§O.1 / M5C-5: the 2-row equs_mini_tbbo_sample.jsonl is RAW vendor shape
    — it has no journal envelope and CANNOT feed replay_stream. It stays a
    parser smoke input: the M1 parser path parses it green; enveloped and fed
    to observe it yields ZERO decisions and a clean exit 0 (two rows in one
    minute never complete a bar, so the probe never runs)."""

    def test_m1_parser_path_zero_decisions_clean_exit(self):
        records = [json.loads(line) for line
                   in RAW_VENDOR_SAMPLE.read_text(encoding="utf-8").splitlines()
                   if line]
        self.assertEqual(len(records), 2)
        events = []
        for record in records:
            event = parse(record, dataset=record["dataset"],
                          schema=record["schema"], reconnect_epoch=0,
                          ts_recv_utc=record["ts_recv"])
            self.assertIsInstance(event, QuoteEvent)   # the M1 parser path
            events.append(event)

        with TemporaryDirectory() as tmp:
            events_path = write_events_jsonl(
                Path(tmp) / "smoke_events.jsonl",
                [to_row(event) for event in events])
            journal_dir = Path(tmp) / "journal"
            from agent.__main__ import main
            rc = main(["observe", "--events", str(events_path),
                       "--journal-dir", str(journal_dir)])
            self.assertEqual(rc, 0)                    # clean exit
            self.assertEqual(replay(journal_dir / "decisions.jsonl"), [],
                             "the 2-row sample must yield zero decisions")
            self.assertEqual(replay(journal_dir / "forecast_scored.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
