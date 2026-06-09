"""M2 §E — status.jsonl + corporate_action rows; replay/rehydrate (S3/S6).

The ``StatusLedger`` is a thin facade over ``recorder.persistence.EventWriter`` (which
wraps ``agent.journal.JournalWriter``): NO new writer, NO new hash. These tests pin the
§E row schemas, the ``ca_to_row``/``ca_from_row`` exact round-trip (DET-1/2), the
canonical-payload version-first determinism, ``rehydrate_state`` fold-by-seq (DET-5), and
the truncated-tail-recoverable vs corrupt-line-fatal replay semantics (S3) — all through
the ONE canonical serialization/journal chain.
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path

from agent.corporate_actions import (
    AdjustmentEvent,
    CaProvenance,
    CaSource,
    CaType,
    DurableId,
    FreezeReason,
    FreezeSignal,
    SourceObservation,
    ValidationStatus,
    cross_validate,
)
from agent.journal import JournalCorruption
from agent.serializer import dumps, row_hash
from agent.status_ledger import (
    EVT_BROKER_ADJUST_FREEZE,
    EVT_CORPORATE_ACTION,
    EVT_HALT_TRANSITION,
    EVT_LULD_TRANSITION,
    EVT_SESSION_TRANSITION,
    EVT_SSR_TRANSITION,
    STATUS_LEDGER_VERSION,
    StatusLedger,
    ca_from_row,
    ca_to_row,
    canonical_status_payload,
    rehydrate_state,
    replay_status,
)
from recorder.persistence import EventWriter

RULES_HASH = "cfg-sha256-deadbeef"
RUN_ID = "agent-2026-06-09"

# A deterministic, monotonic clock so journal ts_utc write-stamps are byte-stable.
_TS_SEQ = [
    "2026-06-15T13:30:00.005000+00:00",
    "2026-06-15T14:05:00.010000+00:00",
    "2026-06-20T20:00:00.020000+00:00",
    "2026-07-01T13:35:00.030000+00:00",
    "2026-07-01T13:40:00.040000+00:00",
    "2026-07-01T13:45:00.050000+00:00",
    "2026-07-01T13:50:00.060000+00:00",
    "2026-07-01T13:55:00.070000+00:00",
]


def _fake_clock():
    stamps = iter(_TS_SEQ + ["2026-07-01T14:00:00.999999+00:00"] * 50)

    def _clock():
        return next(stamps)

    return _clock


def _durable_id():
    return DurableId(cusip="TESTAAPL1", figi="BBG000B9XRY4", ticker="AAPL")


def _single_source_split():
    """A single-source 4:1 split AdjustmentEvent (matches the §E sample CA row)."""
    did = _durable_id()
    obs = SourceObservation(
        source=CaSource.ALPACA,
        durable_id=did,
        ca_type=CaType.SPLIT,
        ex_date_et="2026-07-01",
        factor=Decimal("4.00000000"),
        cash_amount=None,
        provenance=CaProvenance(
            source=CaSource.ALPACA,
            source_ca_id="ALP-1",
            announced_ts_utc="2026-06-20T12:00:00.000000Z",
            ts_recv_utc="2026-06-20T12:00:01.000000Z",
        ),
    )
    return cross_validate((obs,))


def _two_source_confirmed():
    """A two-source CONFIRMED split (distinct CaSource AND source_ca_id) for full-provenance round-trip."""
    did = _durable_id()
    obs_a = SourceObservation(
        source=CaSource.ALPACA,
        durable_id=did,
        ca_type=CaType.SPLIT,
        ex_date_et="2026-07-01",
        factor=Decimal("4.00000000"),
        cash_amount=None,
        provenance=CaProvenance(
            source=CaSource.ALPACA,
            source_ca_id="ALP-1",
            announced_ts_utc="2026-06-20T12:00:00.000000Z",
            ts_recv_utc="2026-06-20T12:00:01.000000Z",
        ),
    )
    obs_b = SourceObservation(
        source=CaSource.DATA_VENDOR,
        durable_id=did,
        ca_type=CaType.SPLIT,
        ex_date_et="2026-07-01",
        factor=Decimal("4.00000000"),
        cash_amount=None,
        provenance=CaProvenance(
            source=CaSource.DATA_VENDOR,
            source_ca_id="DV-9",
            announced_ts_utc="2026-06-20T12:05:00.000000Z",
            ts_recv_utc="2026-06-20T12:05:01.000000Z",
        ),
    )
    return cross_validate((obs_a, obs_b))


def _freeze_signal():
    return FreezeSignal(
        durable_id=_durable_id(),
        symbol="AAPL",
        immediate_reconcile=True,
        prev_qty=Decimal("100"),
        curr_qty=Decimal("400"),
        reason=FreezeReason.BROKER_ADJUSTED_DURING_BLACKOUT,
    )


class _LedgerFixture(unittest.TestCase):
    def _ledger(self, tmp, *, run_id=RUN_ID, clock=None):
        path = Path(tmp) / "status.jsonl"
        writer = EventWriter(path, run_id, clock=clock or _fake_clock())
        return StatusLedger(writer, rules_hash=RULES_HASH), path


class TestTransitionRows(_LedgerFixture):
    def test_transition_rows_round_trip_through_serializer(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ledger, path = self._ledger(tmp)
            session = ledger.record_session_transition(
                symbol="AAPL", instrument_id=1001, from_state="closed",
                to_state="rth", cause="session_open", session_date_et="2026-06-15",
                ts_market_utc="2026-06-15T13:30:00.000000Z",
            )
            halt = ledger.record_halt_transition(
                symbol="AAPL", instrument_id=1001, from_state="none",
                to_state="halted", halt_reason="news_pending",
                ts_market_utc="2026-06-15T14:00:00.000000Z",
            )
            luld = ledger.record_luld_transition(
                symbol="AAPL", instrument_id=1001, from_state="normal",
                to_state="paused", luld_tier="tier1",
                reference_px=Decimal("201.5000"), lower_px=Decimal("200.5000"),
                upper_px=Decimal("202.5000"), doubled=False,
                ts_market_utc="2026-06-15T14:05:00.000000Z",
            )
            ssr = ledger.record_ssr_transition(
                symbol="AAPL", instrument_id=1001, from_state="inactive",
                to_state="active", prior_close_px=Decimal("200.00"),
                ts_market_utc="2026-06-15T14:06:00.000000Z",
            )
        # every returned row survives a canonical re-serialize + re-hash unchanged.
        for row in (session, halt, luld, ssr):
            stored = row["hash"]
            body = {k: v for k, v in row.items() if k != "hash"}
            self.assertEqual(row_hash(body), stored)
            # the journal stamps are present
            for key in ("event_type", "run_id", "seq", "ts_utc", "hash"):
                self.assertIn(key, row)
        self.assertEqual(session["event_type"], EVT_SESSION_TRANSITION)
        self.assertEqual(halt["event_type"], EVT_HALT_TRANSITION)
        self.assertEqual(luld["event_type"], EVT_LULD_TRANSITION)
        self.assertEqual(ssr["event_type"], EVT_SSR_TRANSITION)
        # LULD band prices persisted as Decimal-strings, doubled as bool.
        self.assertEqual(luld["reference_px"], Decimal("201.5000"))
        self.assertEqual(luld["lower_px"], Decimal("200.5000"))
        self.assertEqual(luld["upper_px"], Decimal("202.5000"))
        self.assertFalse(luld["doubled"])
        self.assertEqual(ssr["prior_close_px"], Decimal("200.00"))

    def test_ssr_transition_allows_null_prior_close(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ledger, _ = self._ledger(tmp)
            ssr = ledger.record_ssr_transition(
                symbol="AAPL", instrument_id=1001, from_state="active",
                to_state="inactive", prior_close_px=None,
                ts_market_utc="2026-06-15T14:06:00.000000Z",
            )
        self.assertIsNone(ssr["prior_close_px"])


class TestCorporateActionRow(_LedgerFixture):
    def test_corporate_action_row_roundtrips(self):
        ev = _two_source_confirmed()
        row = ca_to_row(ev)
        # full per-source provenance persisted (DET-1): two sublists, sorted-source order.
        self.assertEqual(len(row["provenance"]), 2)
        self.assertEqual(row["provenance_sources"], ["alpaca", "data_vendor"])
        # provenance_set (frozenset) NEVER serialized (DET-2).
        self.assertNotIn("provenance_set", row)
        rebuilt = ca_from_row(row)
        self.assertEqual(rebuilt, ev)

    def test_corporate_action_row_roundtrips_single_source(self):
        ev = _single_source_split()
        row = ca_to_row(ev)
        self.assertEqual(ca_from_row(row), ev)

    def test_corporate_action_row_carries_instrument_id(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ledger, _ = self._ledger(tmp)
            ca_row = ledger.record_corporate_action(
                adjustment=_single_source_split(), instrument_id=1001,
                ts_market_utc="2026-06-20T20:00:00.000000Z",
            )
            freeze_row = ledger.record_broker_adjust_freeze(
                freeze_signal=_freeze_signal(), instrument_id=1001,
                ts_market_utc="2026-07-01T13:35:00.000000Z",
            )
        self.assertEqual(ca_row["instrument_id"], 1001)
        self.assertEqual(ca_row["event_type"], EVT_CORPORATE_ACTION)
        self.assertEqual(freeze_row["instrument_id"], 1001)
        self.assertEqual(freeze_row["event_type"], EVT_BROKER_ADJUST_FREEZE)
        # freeze row schema fields (§E).
        self.assertEqual(freeze_row["prev_qty"], Decimal("100"))
        self.assertEqual(freeze_row["curr_qty"], Decimal("400"))
        self.assertTrue(freeze_row["immediate_reconcile"])
        self.assertEqual(freeze_row["reason"], "broker_adjusted_during_blackout")
        self.assertEqual(freeze_row["durable_key"], "BBG000B9XRY4")
        self.assertEqual(freeze_row["ticker"], "AAPL")

    def test_corporate_action_row_carries_durable_key_and_blackout(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ledger, _ = self._ledger(tmp)
            ca_row = ledger.record_corporate_action(
                adjustment=_single_source_split(), instrument_id=1001,
                ts_market_utc="2026-06-20T20:00:00.000000Z",
            )
        self.assertEqual(ca_row["durable_key"], "BBG000B9XRY4")
        self.assertEqual(ca_row["cusip"], "TESTAAPL1")
        self.assertEqual(ca_row["figi"], "BBG000B9XRY4")
        self.assertEqual(ca_row["ticker"], "AAPL")
        self.assertEqual(ca_row["ca_type"], "split")
        self.assertEqual(ca_row["validation_status"], "single_source_blackout")
        self.assertTrue(ca_row["blackout"])
        self.assertEqual(ca_row["blackout_from_et"], "2026-06-30")
        self.assertEqual(ca_row["blackout_to_et"], "2026-07-02")
        self.assertEqual(ca_row["factor"], Decimal("4.00000000"))
        self.assertIsNone(ca_row["cash_amount"])
        self.assertFalse(ca_row["provenance_independent"])


class TestLuldBandRequired(_LedgerFixture):
    def test_luld_transition_requires_non_null_band(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ledger, _ = self._ledger(tmp)
            for kwargs in (
                dict(reference_px=None, lower_px=Decimal("200.5"), upper_px=Decimal("202.5")),
                dict(reference_px=Decimal("201.5"), lower_px=None, upper_px=Decimal("202.5")),
                dict(reference_px=Decimal("201.5"), lower_px=Decimal("200.5"), upper_px=None),
            ):
                with self.assertRaises((TypeError, ValueError)):
                    ledger.record_luld_transition(
                        symbol="AAPL", instrument_id=1001, from_state="normal",
                        to_state="paused", luld_tier="tier1", doubled=False,
                        ts_market_utc="2026-06-15T14:05:00.000000Z", **kwargs,
                    )
            # a present-band row round-trips fine.
            row = ledger.record_luld_transition(
                symbol="AAPL", instrument_id=1001, from_state="normal",
                to_state="paused", luld_tier="tier1",
                reference_px=Decimal("201.5000"), lower_px=Decimal("200.5000"),
                upper_px=Decimal("202.5000"), doubled=False,
                ts_market_utc="2026-06-15T14:05:00.000000Z",
            )
        stored = row["hash"]
        body = {k: v for k, v in row.items() if k != "hash"}
        self.assertEqual(row_hash(body), stored)


class TestProvenanceFields(_LedgerFixture):
    def test_rules_hash_carried_on_every_row(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ledger, _ = self._ledger(tmp)
            rows = [
                ledger.record_session_transition(
                    symbol="AAPL", instrument_id=1001, from_state="closed",
                    to_state="rth", cause="session_open", session_date_et="2026-06-15",
                    ts_market_utc="2026-06-15T13:30:00.000000Z",
                ),
                ledger.record_luld_transition(
                    symbol="AAPL", instrument_id=1001, from_state="normal",
                    to_state="paused", luld_tier="tier1",
                    reference_px=Decimal("201.5"), lower_px=Decimal("200.5"),
                    upper_px=Decimal("202.5"), doubled=False,
                    ts_market_utc="2026-06-15T14:05:00.000000Z",
                ),
                ledger.record_corporate_action(
                    adjustment=_single_source_split(), instrument_id=1001,
                    ts_market_utc="2026-06-20T20:00:00.000000Z",
                ),
                ledger.record_broker_adjust_freeze(
                    freeze_signal=_freeze_signal(), instrument_id=1001,
                    ts_market_utc="2026-07-01T13:35:00.000000Z",
                ),
            ]
        for row in rows:
            self.assertEqual(row["rules_hash"], RULES_HASH)
            self.assertEqual(row["v"], STATUS_LEDGER_VERSION)
            self.assertEqual(row["run_id"], RUN_ID)
            self.assertEqual(row["ts_market_utc"][:4], "2026")

    def test_no_reserved_key_collision(self):
        # building every row type with this facade never raises a reserved-key collision.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ledger, _ = self._ledger(tmp)
            ca_row = ledger.record_corporate_action(
                adjustment=_two_source_confirmed(), instrument_id=1001,
                ts_market_utc="2026-06-20T20:00:00.000000Z",
            )
        for reserved in ("seq", "event_type", "run_id", "hash", "ts_utc"):
            # journal-owned keys appear exactly once and are journal-stamped — the
            # payload (ca_to_row) must NOT also carry them.
            self.assertNotIn(reserved, ca_to_row(_two_source_confirmed()))
        self.assertEqual(ca_row["seq"], ca_row["seq"])  # row written without raising


class TestCanonicalPayload(unittest.TestCase):
    def test_canonical_payload_version_first(self):
        payload = canonical_status_payload(version=STATUS_LEDGER_VERSION, body={"symbol": "AAPL", "x": 1})
        self.assertEqual(next(iter(payload)), "v")
        self.assertEqual(payload["v"], STATUS_LEDGER_VERSION)
        self.assertEqual(payload["symbol"], "AAPL")
        # round-trips through the canonical serializer (no floats/sets).
        self.assertIsInstance(dumps(payload), str)


class TestRehydrate(_LedgerFixture):
    def test_rehydrate_state_latest_row_wins(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ledger, path = self._ledger(tmp)
            ledger.record_session_transition(
                symbol="AAPL", instrument_id=1001, from_state="closed",
                to_state="pre", cause="session_open", session_date_et="2026-06-15",
                ts_market_utc="2026-06-15T08:00:00.000000Z",
            )
            ledger.record_session_transition(
                symbol="AAPL", instrument_id=1001, from_state="pre",
                to_state="rth", cause="session_open", session_date_et="2026-06-15",
                ts_market_utc="2026-06-15T13:30:00.000000Z",
            )
            rows = replay_status(path)
        state = rehydrate_state(rows)
        sessions = state["sessions"] if "sessions" in state else state
        # highest-seq session_transition for AAPL wins -> to_state == "rth".
        self.assertEqual(sessions["AAPL"]["to_state"], "rth")

    def test_rehydrate_orders_by_seq_not_ts(self):
        # two transitions stamped at the SAME ts_market_utc; the higher seq must win.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ledger, path = self._ledger(tmp)
            ledger.record_session_transition(
                symbol="AAPL", instrument_id=1001, from_state="rth",
                to_state="halted", cause="halt", session_date_et="2026-06-15",
                ts_market_utc="2026-06-15T14:00:00.000000Z",
            )
            ledger.record_session_transition(
                symbol="AAPL", instrument_id=1001, from_state="halted",
                to_state="rth", cause="resuming", session_date_et="2026-06-15",
                ts_market_utc="2026-06-15T14:00:00.000000Z",
            )
            rows = replay_status(path)
        # deliberately shuffle the rows so a naive in-order fold would be wrong.
        shuffled = [rows[1], rows[0]]
        state = rehydrate_state(shuffled)
        sessions = state["sessions"] if "sessions" in state else state
        self.assertEqual(sessions["AAPL"]["to_state"], "rth")
        self.assertGreater(rows[1]["seq"], rows[0]["seq"])
        self.assertEqual(rows[0]["ts_market_utc"], rows[1]["ts_market_utc"])

    def test_rehydrate_ca_blackout_keyed_by_durable_key(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ledger, path = self._ledger(tmp)
            ledger.record_corporate_action(
                adjustment=_single_source_split(), instrument_id=1001,
                ts_market_utc="2026-06-20T20:00:00.000000Z",
            )
            rows = replay_status(path)
        state = rehydrate_state(rows)
        ca = state["corporate_actions"] if "corporate_actions" in state else None
        self.assertIsNotNone(ca)
        self.assertIn("BBG000B9XRY4", ca)
        self.assertTrue(ca["BBG000B9XRY4"]["blackout"])


class TestReplaySemantics(unittest.TestCase):
    FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "market_state" / "status_replay_sample.jsonl"

    def _valid_rows(self):
        """The fixture's known-good rows: each parses AND hash-verifies (the corrupt /
        truncated lines do not). These were produced through the REAL JournalWriter, so the
        hashes are correct — never hand-authored."""
        good = []
        for ln in self.FIXTURE.read_text(encoding="utf-8").split("\n"):
            if not ln.strip():
                continue
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or "hash" not in row:
                continue
            stored = row.pop("hash")
            if row_hash(row) == stored:
                row["hash"] = stored
                good.append(ln)
        return good

    def test_replay_corrupt_midline_is_fatal(self):
        # reading the whole §H.11 fixture (a newline-terminated corrupt line is NOT the tail)
        # is fatal: JournalCorruption (S3, corrupt-complete-line == fatal).
        with self.assertRaises(JournalCorruption):
            replay_status(self.FIXTURE)

    def test_replay_drops_truncated_tail_not_fatal(self):
        # valid newline-terminated rows + a GENUINELY truncated (incomplete, no-newline)
        # tail -> the tail is dropped (a crash mid-write), the valid rows replay clean
        # (S3, truncated-recoverable). A truncated tail is INCOMPLETE JSON, not merely a
        # complete line missing its newline.
        import tempfile

        valid = self._valid_rows()
        self.assertGreaterEqual(len(valid), 2)
        truncated_tail = valid[-1][: len(valid[-1]) // 2]  # half a row -> unparseable, no newline
        with tempfile.TemporaryDirectory() as tmp:
            body = "".join(ln + "\n" for ln in valid) + truncated_tail
            path = Path(tmp) / "trunc.jsonl"
            path.write_text(body, encoding="utf-8")
            rows = replay_status(path)
        # all valid rows survive; the incomplete tail is dropped.
        self.assertEqual(len(rows), len(valid))
        for row in rows:
            self.assertIn("hash", row)

    def test_replay_detects_tampered_hash(self):
        import tempfile

        valid = self._valid_rows()
        with tempfile.TemporaryDirectory() as tmp:
            first = json.loads(valid[0])
            first.pop("hash")
            first["symbol"] = "TAMPERED"  # mutate a field but keep the OLD hash (re-add it)
            tampered = json.loads(valid[0])  # original incl. its (now-wrong-for-body) hash
            tampered["symbol"] = "TAMPERED"  # body changed, hash NOT recomputed
            path = Path(tmp) / "tamper.jsonl"
            path.write_text(dumps(tampered) + "\n", encoding="utf-8")
            with self.assertRaises(JournalCorruption):
                replay_status(path)


class TestFloatRejected(_LedgerFixture):
    def test_float_in_band_or_factor_raises(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ledger, _ = self._ledger(tmp)
            with self.assertRaises((TypeError, ValueError)):
                ledger.record_luld_transition(
                    symbol="AAPL", instrument_id=1001, from_state="normal",
                    to_state="paused", luld_tier="tier1",
                    reference_px=201.5, lower_px=200.5, upper_px=202.5,  # floats!
                    doubled=False, ts_market_utc="2026-06-15T14:05:00.000000Z",
                )


class TestSharedSeq(unittest.TestCase):
    def test_shared_seq_across_two_writers_same_path(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.jsonl"
            w1 = EventWriter(path, RUN_ID, clock=_fake_clock())
            l1 = StatusLedger(w1, rules_hash=RULES_HASH)
            r1 = l1.record_session_transition(
                symbol="AAPL", instrument_id=1001, from_state="closed",
                to_state="rth", cause="session_open", session_date_et="2026-06-15",
                ts_market_utc="2026-06-15T13:30:00.000000Z",
            )
            # a SECOND writer on the SAME resolved path shares the monotonic seq.
            w2 = EventWriter(path, RUN_ID, clock=_fake_clock())
            l2 = StatusLedger(w2, rules_hash=RULES_HASH)
            r2 = l2.record_halt_transition(
                symbol="AAPL", instrument_id=1001, from_state="none",
                to_state="halted", halt_reason="news_pending",
                ts_market_utc="2026-06-15T14:00:00.000000Z",
            )
        self.assertEqual(r2["seq"], r1["seq"] + 1)


class TestLedgerPriceCanonicalization(unittest.TestCase):
    """harden CROSS-MODULE-1: LULD band prices + SSR prior_close are quantized at the ledger seam, so the
    SAME economic value serializes to ONE canonical Dec-string -> ONE row_hash (replay/determinism)."""

    def _luld_hash(self, px):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            writer = EventWriter(Path(tmp) / "status.jsonl", RUN_ID, clock=_fake_clock())
            ledger = StatusLedger(writer, rules_hash=RULES_HASH)
            row = ledger.record_luld_transition(
                symbol="AAPL", instrument_id=1, from_state="normal", to_state="paused",
                luld_tier="tier1", reference_px=Decimal(px), lower_px=Decimal("200.0000"),
                upper_px=Decimal("210.0000"), doubled=False,
                ts_market_utc="2026-06-15T14:00:00.000000Z",
            )
            return row["hash"]

    def _ssr_hash(self, px):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            writer = EventWriter(Path(tmp) / "status.jsonl", RUN_ID, clock=_fake_clock())
            ledger = StatusLedger(writer, rules_hash=RULES_HASH)
            row = ledger.record_ssr_transition(
                symbol="AAPL", instrument_id=1, from_state="inactive", to_state="active",
                prior_close_px=Decimal(px), ts_market_utc="2026-06-15T14:00:00.000000Z",
            )
            return row["hash"]

    def test_luld_price_precision_canonicalized(self):
        # 201.5 and 201.5000 are the SAME price -> identical row_hash (quantized to 4dp).
        self.assertEqual(self._luld_hash("201.5"), self._luld_hash("201.5000"))

    def test_ssr_prior_close_precision_canonicalized(self):
        self.assertEqual(self._ssr_hash("200"), self._ssr_hash("200.0000"))

    def test_subquantum_price_fails_loud(self):
        # a sub-4dp price does not round-trip -> ValueError (mirrors event PrecisionLoss / CA quantize).
        with self.assertRaises(ValueError):
            self._luld_hash("201.50001")
        with self.assertRaises(ValueError):
            self._ssr_hash("200.00001")


if __name__ == "__main__":
    unittest.main()
