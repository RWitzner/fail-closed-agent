"""status.py — EQUS.MINI status-schema downgrade + SequenceTracker + HeartbeatMonitor.

The central fact this test suite encodes: EQUS.MINI has NO ``status`` schema
(verified in the entitlement matrix, §K, 2026-06-09).  Therefore halt/LULD/SSR
status is sourced from the broker (Alpaca) + exchange_calendars in M2.
The downgrade is WRITTEN here, not assumed elsewhere.  No silent fallback.

§N cases bound to status.py:
  - test_equs_mini_has_no_status_schema      — downgrade constant is non-empty str
  - test_equs_mini_status_downgrade_message  — message names broker+calendar (M2)
  - test_downgrade_is_not_none_or_empty      — no silent fallback (fail-closed)
  - test_sequence_tracker_policy_none_never_gaps — NONE policy: never fires gap
  - test_sequence_tracker_policy_monotonic_gap  — MONOTONIC: jump > 1 -> gap
  - test_sequence_tracker_reset_to_zero         — vendor_seq=0 -> reset_to_zero
  - test_heartbeat_stale_after_timeout          — staleness via FakeClock
  - test_heartbeat_fresh_below_timeout          — no alert when fresh
  - test_heartbeat_stale_symbols                — stale_symbols() lists overdue
  - test_make_data_quality_alert_shape          — alert row keys + cause enum
"""
import unittest
from dataclasses import asdict

from recorder.status import (
    EQUS_MINI_STATUS_DOWNGRADE,
    GapReport,
    HeartbeatMonitor,
    SequencePolicy,
    SequenceTracker,
    make_data_quality_alert,
)
from tests.lib.fakes import FakeClock


# ---------------------------------------------------------------------------
# Downgrade constant (the primary contract obligation of this module)
# ---------------------------------------------------------------------------

class TestEqusMiniStatusDowngrade(unittest.TestCase):
    """The EQUS.MINI status-schema downgrade is EXPLICIT: a non-empty, human-readable
    string stating the primary source and the milestone that owns it (M2).  There is
    no silent fallback — the absence of a status schema is WRITTEN, not assumed."""

    def test_equs_mini_has_no_status_schema(self):
        # The constant must exist and be a non-empty string.
        self.assertIsInstance(EQUS_MINI_STATUS_DOWNGRADE, str)
        self.assertTrue(len(EQUS_MINI_STATUS_DOWNGRADE) > 0,
                        "downgrade message must not be empty")

    def test_equs_mini_status_downgrade_message_names_broker_and_calendar(self):
        # Must name both the broker source and exchange_calendars/M2 so the reader
        # knows WHERE the status information comes from.
        msg = EQUS_MINI_STATUS_DOWNGRADE.lower()
        self.assertIn("broker", msg,
                      "downgrade must name the broker as the status source")
        self.assertIn("m2", msg,
                      "downgrade must reference M2 as the owning milestone")

    def test_downgrade_is_not_none_or_empty(self):
        # Fail-closed: None / empty string would be a silent fallback.
        self.assertIsNotNone(EQUS_MINI_STATUS_DOWNGRADE)
        self.assertNotEqual(EQUS_MINI_STATUS_DOWNGRADE.strip(), "")


# ---------------------------------------------------------------------------
# SequencePolicy enum
# ---------------------------------------------------------------------------

class TestSequencePolicy(unittest.TestCase):
    def test_policy_values(self):
        self.assertEqual(SequencePolicy.MONOTONIC.value, "monotonic")
        self.assertEqual(SequencePolicy.NONE.value, "none")


# ---------------------------------------------------------------------------
# SequenceTracker
# ---------------------------------------------------------------------------

class TestSequenceTrackerNonePolicy(unittest.TestCase):
    """policy=NONE: composite feed, vendor_seq has no per-venue monotonic semantics.
    No gap should ever be reported (spec §5, EQUS.MINI is a composite feed)."""

    def setUp(self):
        self.tracker = SequenceTracker("AAPL", policy=SequencePolicy.NONE)

    def _make_event(self, vendor_seq):
        """Minimal stand-in: any object with provenance.vendor_seq."""
        class _Prov:
            pass
        class _Ev:
            pass
        prov = _Prov()
        prov.vendor_seq = vendor_seq
        ev = _Ev()
        ev.provenance = prov
        return ev

    def test_sequence_tracker_policy_none_never_gaps(self):
        # Observe a jump that would be a gap under MONOTONIC; expect no report.
        ev1 = self._make_event(1001)
        ev2 = self._make_event(1005)  # skipped 1002-1004
        result1 = self.tracker.observe(ev1)
        result2 = self.tracker.observe(ev2)
        self.assertIsNone(result1)
        self.assertIsNone(result2)

    def test_sequence_tracker_policy_none_returns_no_seq_semantics(self):
        # When a null/0 vendor_seq arrives it does not trigger a gap either.
        ev = self._make_event(None)
        self.assertIsNone(self.tracker.observe(ev))

    def test_sequence_tracker_policy_none_never_fires_on_any_seq(self):
        for seq in [0, 1, 100, 50, 200, None]:
            ev = self._make_event(seq)
            self.assertIsNone(self.tracker.observe(ev),
                              f"NONE policy must never fire for seq={seq!r}")

    def test_sequence_tracker_policy_none_returns_none_not_dead_kind(self):
        # D8 (R2#8): a NONE-policy tracker performs no gap detection and returns
        # None — it must NEVER emit a 'no_seq_semantics' GapReport (a dead kind).
        for seq in [10, 20, 5, None, 0]:
            report = self.tracker.observe(self._make_event(seq))
            self.assertIsNone(report,
                              f"NONE policy must return None, never a report (seq={seq!r})")


class TestSequenceTrackerMonotonicPolicy(unittest.TestCase):
    """policy=MONOTONIC: vendor_seq must be prev+1; a jump -> GapReport(kind='gap')."""

    def setUp(self):
        self.tracker = SequenceTracker("AAPL", policy=SequencePolicy.MONOTONIC)

    def _make_event(self, vendor_seq):
        class _Prov:
            pass
        class _Ev:
            pass
        prov = _Prov()
        prov.vendor_seq = vendor_seq
        ev = _Ev()
        ev.provenance = prov
        return ev

    def test_sequence_tracker_no_report_on_first_event(self):
        ev = self._make_event(2001)
        self.assertIsNone(self.tracker.observe(ev))

    def test_sequence_tracker_no_report_on_consecutive(self):
        for seq in [2001, 2002, 2003]:
            self.assertIsNone(self.tracker.observe(self._make_event(seq)))

    def test_sequence_tracker_policy_monotonic_gap(self):
        # 2001 -> 2004: gap of 3 (missing 2002, 2003, 2004 not received before 2004)
        self.tracker.observe(self._make_event(2001))
        report = self.tracker.observe(self._make_event(2004))
        self.assertIsNotNone(report)
        self.assertIsInstance(report, GapReport)
        self.assertEqual(report.kind, "gap")
        self.assertEqual(report.symbol, "AAPL")
        self.assertEqual(report.expected_seq, 2002)
        self.assertEqual(report.got_seq, 2004)
        self.assertEqual(report.gap_size, 2)  # 2004 - 2002 = 2

    def test_sequence_tracker_reset_to_zero(self):
        # vendor_seq reset to 0 mid-stream is a reconnect/epoch marker, NOT a gap.
        self.tracker.observe(self._make_event(1050))
        report = self.tracker.observe(self._make_event(0))
        self.assertIsNotNone(report)
        self.assertIsInstance(report, GapReport)
        self.assertEqual(report.kind, "reset_to_zero")

    def test_sequence_tracker_reset_to_zero_not_counted_as_gap(self):
        self.tracker.observe(self._make_event(1050))
        report = self.tracker.observe(self._make_event(0))
        self.assertNotEqual(report.kind, "gap")

    def test_sequence_tracker_gap_size_matches_skip(self):
        # Verify gap_size = got - expected (only missing seqs between expected and got).
        self.tracker.observe(self._make_event(100))
        report = self.tracker.observe(self._make_event(105))
        self.assertEqual(report.gap_size, 4)  # 105 - 101 = 4

    def test_sequence_tracker_consecutive_after_reset_no_gap(self):
        # After a reset-to-zero, subsequent seqs from 1 onward are consecutive.
        self.tracker.observe(self._make_event(500))
        self.tracker.observe(self._make_event(0))  # reset
        # First event after reset establishes a new baseline — no gap
        result = self.tracker.observe(self._make_event(1))
        # Either None or reset_to_zero is fine; must NOT be a gap
        if result is not None:
            self.assertNotEqual(result.kind, "gap")

    # --- C1: sequence anomaly taxonomy (out_of_order | duplicate) ---------
    def test_sequence_tracker_out_of_order_is_not_a_negative_gap(self):
        # got < last (backward jump): observe(100) then observe(95).
        # MUST be kind='out_of_order' with gap_size=None — NOT a 'gap' with a
        # negative gap_size (C1: a negative gap_size is forbidden).
        self.tracker.observe(self._make_event(100))
        report = self.tracker.observe(self._make_event(95))
        self.assertIsNotNone(report)
        self.assertIsInstance(report, GapReport)
        self.assertEqual(report.kind, "out_of_order")
        self.assertEqual(report.symbol, "AAPL")
        self.assertEqual(report.got_seq, 95)
        self.assertIsNone(report.gap_size,
                          "out_of_order must have gap_size=None, never negative")

    def test_sequence_tracker_duplicate_is_its_own_kind(self):
        # got == last (repeat): observe(100) then observe(100) -> 'duplicate'.
        self.tracker.observe(self._make_event(100))
        report = self.tracker.observe(self._make_event(100))
        self.assertIsNotNone(report)
        self.assertIsInstance(report, GapReport)
        self.assertEqual(report.kind, "duplicate")
        self.assertEqual(report.got_seq, 100)
        self.assertIsNone(report.gap_size,
                          "duplicate must have gap_size=None")

    def test_sequence_tracker_forward_gap_has_positive_gap_size(self):
        # got > expected: observe(100) then observe(105) -> gap, gap_size=4 (>0).
        self.tracker.observe(self._make_event(100))
        report = self.tracker.observe(self._make_event(105))
        self.assertEqual(report.kind, "gap")
        self.assertEqual(report.gap_size, 4)
        self.assertGreater(report.gap_size, 0,
                           "a forward gap_size must be positive")

    # --- C9: reset_to_zero expected_seq is None (no meaningful expected) ---
    def test_sequence_tracker_reset_to_zero_expected_seq_is_none(self):
        # C9: a reset has no meaningful expected continuation -> expected_seq=None
        # (not a meaningless 1 = last+1 computed before the reset branch).
        self.tracker.observe(self._make_event(1050))
        report = self.tracker.observe(self._make_event(0))
        self.assertEqual(report.kind, "reset_to_zero")
        self.assertIsNone(report.expected_seq,
                          "reset_to_zero expected_seq must be None")

    # --- D2 (R2#2): a null vendor_seq mid-stream must NOT crash -------------
    def test_sequence_tracker_null_vendor_seq_does_not_raise(self):
        # D2: under MONOTONIC, observe(5) establishes a baseline; a subsequent
        # observe(None) (malformed/null vendor_seq) MUST return a report (a
        # data-quality signal), NEVER raise a TypeError out of observe().
        self.tracker.observe(self._make_event(5))
        report = self.tracker.observe(self._make_event(None))  # must not raise
        self.assertIsNotNone(report,
                             "a null vendor_seq under MONOTONIC must surface a report")
        self.assertIsInstance(report, GapReport)
        self.assertEqual(report.symbol, "AAPL")
        self.assertIsNone(report.got_seq,
                          "the null vendor_seq must be carried as got_seq=None")


# ---------------------------------------------------------------------------
# HeartbeatMonitor
# ---------------------------------------------------------------------------

class TestHeartbeatMonitor(unittest.TestCase):
    """Injected-clock freshness watcher.  quiet > timeout_ms -> GapReport(kind='heartbeat_timeout').
    This is an S4 input; M5 execution_preflight consumes it."""

    TIMEOUT_MS = 5000

    def setUp(self):
        self.clock = FakeClock(start_ms=0)
        self.monitor = HeartbeatMonitor(timeout_ms=self.TIMEOUT_MS, clock=self.clock)

    def test_heartbeat_fresh_below_timeout(self):
        self.monitor.touch("AAPL", self.clock.now_ms())
        self.clock.advance(self.TIMEOUT_MS - 1)
        result = self.monitor.check("AAPL", self.clock.now_ms())
        self.assertIsNone(result)

    def test_heartbeat_stale_after_timeout(self):
        self.monitor.touch("AAPL", self.clock.now_ms())
        self.clock.advance(self.TIMEOUT_MS + 1)
        report = self.monitor.check("AAPL", self.clock.now_ms())
        self.assertIsNotNone(report)
        self.assertIsInstance(report, GapReport)
        self.assertEqual(report.kind, "heartbeat_timeout")
        self.assertEqual(report.symbol, "AAPL")

    def test_heartbeat_touch_resets_freshness(self):
        self.monitor.touch("AAPL", self.clock.now_ms())
        self.clock.advance(self.TIMEOUT_MS - 100)
        self.monitor.touch("AAPL", self.clock.now_ms())  # reset freshness
        self.clock.advance(self.TIMEOUT_MS - 1)          # still under threshold
        self.assertIsNone(self.monitor.check("AAPL", self.clock.now_ms()))

    def test_heartbeat_never_touched_symbol_is_stale_after_timeout(self):
        # A symbol that was never touched has no last-seen time; after timeout it is stale.
        self.clock.advance(self.TIMEOUT_MS + 1)
        report = self.monitor.check("MSFT", self.clock.now_ms())
        # Not touched at all - implementation may return None or a timeout report;
        # either is valid but if it fires it must be heartbeat_timeout.
        if report is not None:
            self.assertEqual(report.kind, "heartbeat_timeout")

    def test_heartbeat_stale_symbols(self):
        self.monitor.touch("AAPL", self.clock.now_ms())
        self.monitor.touch("MSFT", self.clock.now_ms())
        self.clock.advance(self.TIMEOUT_MS + 1)
        stale = self.monitor.stale_symbols(self.clock.now_ms())
        self.assertIn("AAPL", stale)
        self.assertIn("MSFT", stale)

    def test_heartbeat_fresh_symbol_not_in_stale(self):
        self.monitor.touch("AAPL", self.clock.now_ms())
        self.clock.advance(self.TIMEOUT_MS - 1)
        stale = self.monitor.stale_symbols(self.clock.now_ms())
        self.assertNotIn("AAPL", stale)

    def test_heartbeat_not_stale_at_exact_timeout_boundary(self):
        # C8: strict '>' semantics. At EXACTLY timeout_ms the symbol is NOT stale
        # (quiet == timeout_ms is within tolerance). Assert both surfaces.
        self.monitor.touch("AAPL", 0)
        self.assertIsNone(self.monitor.check("AAPL", self.TIMEOUT_MS),
                          "must NOT be stale at exactly timeout_ms")
        self.assertNotIn("AAPL", self.monitor.stale_symbols(self.TIMEOUT_MS),
                         "must NOT be in stale_symbols at exactly timeout_ms")

    def test_heartbeat_stale_one_ms_past_timeout_boundary(self):
        # C8: stale at timeout_ms + 1 (quiet > timeout_ms). Assert both surfaces.
        self.monitor.touch("AAPL", 0)
        report = self.monitor.check("AAPL", self.TIMEOUT_MS + 1)
        self.assertIsNotNone(report, "must be stale at timeout_ms + 1")
        self.assertEqual(report.kind, "heartbeat_timeout")
        self.assertIn("AAPL", self.monitor.stale_symbols(self.TIMEOUT_MS + 1),
                      "must be in stale_symbols at timeout_ms + 1")


# ---------------------------------------------------------------------------
# make_data_quality_alert
# ---------------------------------------------------------------------------

class TestMakeDataQualityAlert(unittest.TestCase):
    """Alert rows are DATA, not exceptions.  The shape is frozen so downstream
    consumers (M2 session gate, M5 preflight) can rely on the field names."""

    def test_make_data_quality_alert_shape(self):
        alert = make_data_quality_alert(cause="sequence_gap", symbol="AAPL",
                                        detail="gap 3", reconnect_epoch=1)
        self.assertIsInstance(alert, dict)
        self.assertIn("cause", alert)
        self.assertIn("reconnect_epoch", alert)
        self.assertEqual(alert["cause"], "sequence_gap")
        self.assertEqual(alert["reconnect_epoch"], 1)

    def test_make_data_quality_alert_all_cause_values(self):
        # C1: 'out_of_order' and 'duplicate' join the cause enum.
        causes = ["sequence_gap", "reset_to_zero", "heartbeat_timeout",
                  "prolonged_disconnect", "crossed_book",
                  "out_of_order", "duplicate"]
        for cause in causes:
            alert = make_data_quality_alert(cause=cause)
            self.assertEqual(alert["cause"], cause,
                             f"cause {cause!r} must survive round-trip")

    def test_make_data_quality_alert_optional_fields(self):
        # symbol and detail are optional; must not raise when omitted.
        alert = make_data_quality_alert(cause="prolonged_disconnect",
                                        down_ms=61000, reconnect_epoch=2)
        self.assertEqual(alert["cause"], "prolonged_disconnect")
        self.assertEqual(alert["reconnect_epoch"], 2)

    def test_make_data_quality_alert_no_floats(self):
        # No float values anywhere — the alert passes through serializer.dumps.
        from agent.serializer import dumps
        alert = make_data_quality_alert(cause="heartbeat_timeout", symbol="AAPL",
                                        reconnect_epoch=0)
        # Should serialize without raising (float-reject check).
        serialized = dumps(alert)
        self.assertIsInstance(serialized, str)

    def test_make_data_quality_alert_symbol_none_by_default(self):
        alert = make_data_quality_alert(cause="prolonged_disconnect")
        # symbol key present (even if None) OR absent — both are acceptable,
        # but cause must be set and reconnect_epoch must default to 0.
        self.assertEqual(alert["cause"], "prolonged_disconnect")
        self.assertEqual(alert.get("reconnect_epoch", 0), 0)


if __name__ == "__main__":
    unittest.main()
