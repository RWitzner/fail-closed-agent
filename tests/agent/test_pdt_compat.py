"""M4 §M test 5 — LegacyPdtCompatMode: mirror-only detection, durable rejection latch,
tighten-only gate consumption.

Invariants: S10-compat (no $25k / no day-trade arithmetic), R13.
"""
import ast
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.risk.account_state import parse_account_payload
from agent.risk.pdt_compat import (
    PDT_REJECTION_CODES,
    PDT_REJECTION_MARKERS,
    BrokerRejectionObservation,
    LegacyPdtCompatMode,
    PdtRead,
)
from agent.risk.risk_ledger import EVT_PDT_TRANSITION, RiskLedger, replay_risk
from recorder.persistence import EventWriter
from tests.lib.risk_fixtures import pdt_payloads

_CLOCK = lambda: "2026-06-08T20:00:00.000000+00:00"  # noqa: E731


def _read(payload):
    return parse_account_payload(payload, source="fixture", seen_at_ms=0,
                                 ts_read_utc="2026-06-08T14:00:00.000000Z")


def _ledgered(tmpdir):
    path = Path(tmpdir) / "risk.jsonl"
    return RiskLedger(EventWriter(path, "run-1", clock=_CLOCK), rules_hash="rh"), path


class TestDetectionMatrix(unittest.TestCase):
    def test_flagged_clean_absent(self):
        payloads = pdt_payloads()
        mode = LegacyPdtCompatMode(ledger=None)
        self.assertEqual(mode.read(), PdtRead(state="unknown", evidence=None,
                                              rejection_latched=False))
        read = mode.observe_account(_read(payloads["pdt_flagged"]))
        self.assertEqual(read.state, "enforcing_legacy_pdt")
        self.assertEqual(read.evidence, "account_flag")
        self.assertIs(read.rejection_latched, False)
        read = mode.observe_account(_read(payloads["pdt_clean"]))
        self.assertEqual(read.state, "not_enforcing")
        self.assertIsNone(read.evidence)
        read = mode.observe_account(_read(payloads["pdt_fields_absent"]))
        self.assertEqual(read.state, "unknown")  # None => UNKNOWN, never a block by itself


class TestRejectionLatch(unittest.TestCase):
    def test_latch_via_code_and_via_each_marker(self):
        payloads = pdt_payloads()
        mode = LegacyPdtCompatMode(ledger=None)
        read = mode.observe_broker_rejection(BrokerRejectionObservation(
            code=payloads["rejection_pdt_code"]["code"],
            message=payloads["rejection_pdt_code"]["message"],
            ts_utc="2026-06-08T15:00:00Z"))
        self.assertEqual(read.state, "enforcing_legacy_pdt")
        self.assertEqual(read.evidence, "broker_rejection")
        self.assertIs(read.rejection_latched, True)
        for marker in PDT_REJECTION_MARKERS:
            mode = LegacyPdtCompatMode(ledger=None)
            read = mode.observe_broker_rejection(BrokerRejectionObservation(
                code=None, message=f"REJECTED: {marker.upper()} limit",
                ts_utc="2026-06-08T15:00:00Z"))  # case-insensitive substring
            self.assertIs(read.rejection_latched, True, marker)

    def test_non_pdt_rejection_does_not_latch(self):
        payloads = pdt_payloads()
        mode = LegacyPdtCompatMode(ledger=None)
        read = mode.observe_broker_rejection(BrokerRejectionObservation(
            code=payloads["rejection_other"]["code"],
            message=payloads["rejection_other"]["message"],
            ts_utc="2026-06-08T15:00:00Z"))
        self.assertEqual(read.state, "unknown")
        self.assertIs(read.rejection_latched, False)

    def test_latch_survives_later_false_flag_within_run(self):
        payloads = pdt_payloads()
        mode = LegacyPdtCompatMode(ledger=None)
        mode.observe_broker_rejection(BrokerRejectionObservation(
            code=40310100, message="pdt", ts_utc="t"))
        read = mode.observe_account(_read(payloads["pdt_clean"]))  # False flag
        self.assertEqual(read.state, "enforcing_legacy_pdt")        # NEVER unlatched
        self.assertIs(read.rejection_latched, True)

    def test_latch_durable_across_runs_via_rehydrated_state(self):
        # LD-R3: ctor rehydrated_state with rejection_latched=True RE-LATCHES.
        mode = LegacyPdtCompatMode(
            ledger=None,
            rehydrated_state={"state": "enforcing_legacy_pdt", "rejection_latched": True})
        read = mode.read()
        self.assertEqual(read.state, "enforcing_legacy_pdt")
        self.assertIs(read.rejection_latched, True)
        unlatched = LegacyPdtCompatMode(
            ledger=None,
            rehydrated_state={"state": "not_enforcing", "rejection_latched": False})
        self.assertEqual(unlatched.read().state, "unknown")  # only the latch is durable

    def test_transitions_journaled(self):
        with TemporaryDirectory() as tmpdir:
            ledger, path = _ledgered(tmpdir)
            payloads = pdt_payloads()
            mode = LegacyPdtCompatMode(ledger=ledger)
            mode.observe_account(_read(payloads["pdt_flagged"]))
            mode.observe_account(_read(payloads["pdt_flagged"]))   # no change, no row
            mode.observe_account(_read(payloads["pdt_clean"]))
            mode.observe_broker_rejection(BrokerRejectionObservation(
                code=40310100, message="pdt", ts_utc="t"))
            rows = [r for r in replay_risk(path) if r["event_type"] == EVT_PDT_TRANSITION]
            self.assertEqual([(r["from_state"], r["to_state"], r["evidence"])
                              for r in rows],
                             [("unknown", "enforcing_legacy_pdt", "account_flag"),
                              ("enforcing_legacy_pdt", "not_enforcing", "account_flag"),
                              ("not_enforcing", "enforcing_legacy_pdt",
                               "broker_rejection")])
            self.assertEqual(rows[2]["rejection_code"], 40310100)
            self.assertIs(rows[0]["pattern_day_trader"], True)
            self.assertEqual(rows[0]["daytrade_count"], 4)

    def test_detection_total_never_raises_on_weird_payloads(self):
        mode = LegacyPdtCompatMode(ledger=None)
        read = mode.observe_broker_rejection(BrokerRejectionObservation(
            code=None, message="", ts_utc=""))
        self.assertIs(read.rejection_latched, False)

    def test_frozen_detection_vocab(self):
        self.assertEqual(PDT_REJECTION_CODES, frozenset({40310100}))
        self.assertEqual(PDT_REJECTION_MARKERS,
                         ("pattern day trad", "day trading buying power", "day-trade"))


class TestTightenOnly(unittest.TestCase):
    def test_read_output_feeds_only_reason_adding_branches(self):
        # White-box: pdt_compat exposes ONLY a PdtRead; it carries no allow/suppress
        # surface (no reasons-removal API, no allowed field).
        read = LegacyPdtCompatMode(ledger=None).read()
        self.assertEqual(set(read.__dataclass_fields__),
                         {"state", "evidence", "rejection_latched"})

    def test_source_scan_no_constants_no_daytrade_arithmetic(self):
        source_path = (Path(__file__).resolve().parents[2] / "scripts" / "agent"
                       / "risk" / "pdt_compat.py")
        text = source_path.read_text(encoding="utf-8")
        self.assertNotIn("25000", text)
        # daytrade_count is provenance-only: never compared/computed against.
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and sub.attr == "daytrade_count":
                        self.fail("daytrade_count used in a comparison (FD-M4-2)")


if __name__ == "__main__":
    unittest.main()
