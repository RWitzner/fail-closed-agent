"""M5 §O.2 / §R 13 — the LD-M5-2 dedicated run-gates-file suite, + §M.1 run_lock.

Covers §R 13 COMPLETELY EXCEPT the orchestrator-integration cases, which are
DEFERRED to the orchestrator wave (test_orchestrator/test_config_canary growth):

  - DEFERRED: ``rules_hash`` IDENTICAL on every JOURNALED row with the file
    present-true vs absent (M5C-S4) — needs the orchestrator's writers; the
    pre-substitution pin itself IS asserted here (rules_hash over the committed
    dict is unchanged by assemble_gates_view).
  - DEFERRED: the gates-absent paper run with a rehydrated position +
    credentials that can still cancel/flatten while every open rejects at
    ``run_gates`` (reduce-and-recover — M5C-S3) — needs the full composition.
  - DEFERRED likewise: "observe/synthetic never call the loader (spy)" — a
    startup-path property of the orchestrator's mode select.

Everything here is offline, stdlib-only, and only ever reads INJECTED tmp paths
(R11: ``.secrets/`` is never touched).
"""
import copy
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent import config as agent_config
from agent.gates import opening_allowed
from agent.run_lock import LOCK_FILENAME, RunLock, RunLockHeld
from agent.secrets_runtime import (
    assemble_gates_view,
    load_alpaca_paper_credentials,
    load_run_gates,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

VALID_GATES = {"enabled": True, "paper_trading": {"enabled": True}}
ALL_FALSE = {"present": False, "parse_ok": False,
             "enabled": False, "paper_enabled": False}


def committed_assembled() -> dict:
    """The real committed config in the §M.2 assembled shape."""
    return {
        "agent_rules": agent_config.load(REPO_ROOT / "config" / "agent_rules.json"),
        "risk_rules": agent_config.load(REPO_ROOT / "config" / "risk_rules.json"),
    }


def _deep_diff(a, b, path=()):
    """Set of key-paths at which two JSON-shaped values differ."""
    diffs = set()
    if isinstance(a, dict) and isinstance(b, dict):
        for key in set(a) | set(b):
            if key not in a or key not in b:
                diffs.add(path + (key,))
            else:
                diffs |= _deep_diff(a[key], b[key], path + (key,))
        return diffs
    if a != b:
        diffs.add(path)
    return diffs


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.gates_path = self.root / "run_gates.json"

    def tearDown(self):
        self._dir.cleanup()

    def _write_gates(self, payload) -> Path:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.gates_path.write_text(text, encoding="utf-8")
        return self.gates_path


class TestLoadRunGates(_Tmp):
    def test_absent_file_reads_both_false(self):
        self.assertEqual(load_run_gates(self.gates_path), ALL_FALSE)

    def test_malformed_json_reads_both_false_with_parse_ok_false(self):
        self._write_gates('{"enabled": true,,,')
        self.assertEqual(load_run_gates(self.gates_path),
                         {"present": True, "parse_ok": False,
                          "enabled": False, "paper_enabled": False})

    def test_valid_frozen_shape_reads_both_true(self):
        self._write_gates(VALID_GATES)
        self.assertEqual(load_run_gates(self.gates_path),
                         {"present": True, "parse_ok": True,
                          "enabled": True, "paper_enabled": True})

    def test_non_identity_true_values_read_false(self):
        # §O.2.3: ANY non-identity-True value => both read False (ONE rule).
        for bad in (1, "true", None, "True", 0, [], {}):
            with self.subTest(slot="enabled", value=bad):
                self._write_gates({"enabled": bad,
                                   "paper_trading": {"enabled": True}})
                reading = load_run_gates(self.gates_path)
                self.assertFalse(reading["enabled"])
                self.assertFalse(reading["paper_enabled"])
                self.assertTrue(reading["parse_ok"])
            with self.subTest(slot="paper_trading.enabled", value=bad):
                self._write_gates({"enabled": True,
                                   "paper_trading": {"enabled": bad}})
                reading = load_run_gates(self.gates_path)
                self.assertFalse(reading["enabled"])
                self.assertFalse(reading["paper_enabled"])

    def test_wrong_shape_reads_both_false(self):
        for payload in ([1, 2], "true", {"enabled": True},
                        {"paper_trading": {"enabled": True}},
                        {"enabled": True, "paper_trading": True},
                        {"enabled": True, "paper_trading": [True]}):
            with self.subTest(payload=payload):
                self._write_gates(payload)
                reading = load_run_gates(self.gates_path)
                self.assertFalse(reading["enabled"])
                self.assertFalse(reading["paper_enabled"])
                self.assertTrue(reading["parse_ok"])

    def test_hostile_extra_keys_never_read_out(self):
        # Only the two gate paths are ever read; the reading carries EXACTLY
        # the four provenance keys regardless of what else the file smuggles.
        hostile = dict(VALID_GATES)
        hostile.update({"caps": {"max_position_usd": "1000000"},
                        "universe": {"symbols": ["TSLA"]},
                        "latency_budget_ms": 0})
        self._write_gates(hostile)
        reading = load_run_gates(self.gates_path)
        self.assertEqual(set(reading),
                         {"present", "parse_ok", "enabled", "paper_enabled"})
        self.assertTrue(reading["enabled"])
        self.assertTrue(reading["paper_enabled"])

    def test_path_is_required_with_no_default(self):
        # §O.2.1: NO .secrets/ default — the path is always injected explicitly.
        for func in (load_run_gates, load_alpaca_paper_credentials):
            params = inspect.signature(func).parameters
            self.assertEqual(list(params), ["path"], func.__name__)
            self.assertIs(params["path"].default, inspect.Parameter.empty,
                          func.__name__)


class TestAssembleGatesView(_Tmp):
    def test_view_differs_from_committed_only_at_the_two_gate_keys(self):
        hostile = dict(VALID_GATES)
        hostile.update({"caps": {"max_position_usd": "9999999"},
                        "universe": {"symbols": ["TSLA"]},
                        "latency_budget_ms": 1})
        self._write_gates(hostile)
        committed = committed_assembled()
        view = assemble_gates_view(committed, load_run_gates(self.gates_path))
        self.assertEqual(
            _deep_diff(view, committed),
            {("agent_rules", "enabled"),
             ("agent_rules", "paper_trading", "enabled")})

    def test_valid_file_opens_view_while_committed_stays_false(self):
        self._write_gates(VALID_GATES)
        committed = committed_assembled()
        view = assemble_gates_view(committed, load_run_gates(self.gates_path))
        self.assertTrue(opening_allowed(view))
        self.assertFalse(opening_allowed(committed))   # committed alone: closed

    def test_delete_file_re_closes(self):
        # The uninstall story (§O.2.5c): rm run_gates.json => reject-all again.
        self._write_gates(VALID_GATES)
        committed = committed_assembled()
        view = assemble_gates_view(committed, load_run_gates(self.gates_path))
        self.assertTrue(opening_allowed(view))

        os.unlink(self.gates_path)
        reading = load_run_gates(self.gates_path)
        self.assertEqual(reading, ALL_FALSE)
        view = assemble_gates_view(committed, reading)
        self.assertFalse(opening_allowed(view))

    def test_input_dict_never_mutated_and_rules_hash_unchanged(self):
        # §O.2.7: rules_hash and the parsers consume the PRE-substitution dict;
        # the view is a copy, so the substitution cannot leak into either.
        self._write_gates(VALID_GATES)
        committed = committed_assembled()
        before = copy.deepcopy(committed)
        hash_before = agent_config.rules_hash(committed)

        view = assemble_gates_view(committed, load_run_gates(self.gates_path))
        self.assertTrue(opening_allowed(view))
        self.assertEqual(committed, before)                       # unmutated
        self.assertEqual(agent_config.rules_hash(committed), hash_before)

    def test_reading_values_re_read_identity_strict(self):
        # Defense-in-depth: a tampered reading with truthy non-True values
        # still assembles a CLOSED view.
        committed = committed_assembled()
        view = assemble_gates_view(committed, {"present": True, "parse_ok": True,
                                               "enabled": 1,
                                               "paper_enabled": "true"})
        self.assertFalse(opening_allowed(view))


class TestCredentialsLoader(_Tmp):
    VALID = {"key_id": "PKTEST", "secret_key": "s3cr3t",
             "base_url": "https://paper-api.alpaca.markets"}

    def _write(self, payload):
        path = self.root / "alpaca_paper.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_valid_file_round_trips_exactly_three_keys(self):
        self.assertEqual(load_alpaca_paper_credentials(self._write(self.VALID)),
                         self.VALID)

    def test_fail_loud_on_missing_file_and_bad_shapes(self):
        with self.assertRaises(ValueError):
            load_alpaca_paper_credentials(self.root / "absent.json")
        for payload in ([], "creds",
                        {"key_id": "a", "secret_key": "b"},                # missing
                        dict(self.VALID, extra="x"),                       # extra
                        dict(self.VALID, key_id=""),                       # empty
                        dict(self.VALID, secret_key=123)):                 # non-str
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    load_alpaca_paper_credentials(self._write(payload))


class TestRunLock(_Tmp):
    def _journal_dir(self) -> Path:
        return self.root / "journal"

    def test_acquire_writes_pid_and_release_removes(self):
        lock = RunLock(self._journal_dir())
        reclaimed = lock.acquire()
        self.assertFalse(reclaimed)
        self.assertFalse(lock.reclaimed)
        lock_file = self._journal_dir() / LOCK_FILENAME
        self.assertEqual(lock_file.read_text(encoding="ascii").strip(),
                         str(os.getpid()))
        lock.release()
        self.assertFalse(lock_file.exists())
        lock.release()   # idempotent

    def test_second_acquire_refused_while_held(self):
        with RunLock(self._journal_dir()):
            with self.assertRaises(RunLockHeld):
                RunLock(self._journal_dir()).acquire()
        # Released on clean exit: a fresh acquire now succeeds, un-reclaimed.
        self.assertFalse(RunLock(self._journal_dir()).acquire())

    def test_context_manager_releases_on_exception(self):
        lock_file = self._journal_dir() / LOCK_FILENAME
        with self.assertRaises(RuntimeError):
            with RunLock(self._journal_dir()):
                self.assertTrue(lock_file.exists())
                raise RuntimeError("boom")
        self.assertFalse(lock_file.exists())

    def test_stale_lock_from_dead_pid_reclaimed_and_reported(self):
        # A real, certainly-dead PID: spawn-and-reap a child (fixed argv array).
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        dead_pid = child.pid

        self._journal_dir().mkdir(parents=True)
        (self._journal_dir() / LOCK_FILENAME).write_text(f"{dead_pid}\n",
                                                         encoding="ascii")
        lock = RunLock(self._journal_dir())
        reclaimed = lock.acquire()
        self.assertTrue(reclaimed)        # REPORTED: the orchestrator journals it
        self.assertTrue(lock.reclaimed)
        # The reclaimed lock now carries OUR pid.
        self.assertEqual((self._journal_dir() / LOCK_FILENAME)
                         .read_text(encoding="ascii").strip(), str(os.getpid()))
        lock.release()

    def test_malformed_lock_file_refuses_fail_closed(self):
        # Liveness unverifiable => held (operator removes it manually).
        self._journal_dir().mkdir(parents=True)
        (self._journal_dir() / LOCK_FILENAME).write_text("not-a-pid\n",
                                                         encoding="ascii")
        with self.assertRaises(RunLockHeld):
            RunLock(self._journal_dir()).acquire()


if __name__ == "__main__":
    unittest.main()
