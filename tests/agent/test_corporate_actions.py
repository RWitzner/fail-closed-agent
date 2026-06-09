"""M2 §D — corporate_actions: multi-source CA cross-validation, tri-state, fail-closed (S7).

Offline, stdlib-only. Drives cross_validate / CorporateActionFeed / BrokerAdjustDetector off
the pinned §H.4–§H.10 fixtures and asserts:
  - >=2 independent sources (distinct CaSource AND distinct source_ca_id), agreeing + complete -> CONFIRMED,
  - a lone source -> SINGLE_SOURCE_BLACKOUT (never clears),
  - sources disagreeing on factor / type / incomplete required field -> CONFLICTING_BLACKOUT,
  - a type disagreement on the SAME (durable_key, ex_date) is grouped together (LOW-3) -> CONFLICTING,
  - a shrinking lead/trail window override RAISES (HIGH-3 never-loosen clamp),
  - mirrored source_ca_id / same-source-twice -> NOT 2 independent -> blacked out (S7-3),
  - the closed-inclusive blackout window + open-ended non-CONFIRMED blackout (MED-6),
  - durable CUSIP/FIGI identity survives a ticker change; a ticker-only identity is rejected,
  - ANY unexplained broker qty delta -> FreezeSignal(immediate_reconcile=True) + frozen (S7-1),
  - a missing baseline observe RAISES (S7-2) and a CA-implied qty cannot override the broker baseline,
  - no float / no frozenset ever reaches the serializer (DET-2 / S2).
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path

from agent.corporate_actions import (
    AdjustmentEvent,
    BrokerAdjustDetector,
    BrokerAdjustedDuringBlackout,
    CaProvenance,
    CaSource,
    CaType,
    CorporateActionError,
    CorporateActionFeed,
    DurableId,
    FreezeReason,
    FreezeSignal,
    SourceObservation,
    ValidationStatus,
    cross_validate,
)
from agent.serializer import dumps, row_hash
from agent.status_ledger import ca_to_row

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "corporate_actions"


def _load_jsonl(name):
    rows = []
    for line in (_FIXTURE_DIR / name).read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _load_json(name):
    return json.loads((_FIXTURE_DIR / name).read_text())


def _obs_from_row(row, *, ts_recv_utc="2026-06-20T12:10:00.000000Z"):
    """Build a SourceObservation from a fixture JSON object (Decimal-as-string -> Decimal)."""
    durable = DurableId(cusip=row["cusip"], figi=row["figi"], ticker=row["ticker"])
    factor = Decimal(row["factor"]) if row["factor"] is not None else None
    cash = Decimal(row["cash_amount"]) if row["cash_amount"] is not None else None
    prov = CaProvenance(
        source=CaSource(row["source"]),
        source_ca_id=row["source_ca_id"],
        announced_ts_utc=row["announced_ts_utc"],
        ts_recv_utc=ts_recv_utc,
    )
    return SourceObservation(
        source=CaSource(row["source"]),
        durable_id=durable,
        ca_type=CaType(row["ca_type"]),
        ex_date_et=row["ex_date_et"],
        factor=factor,
        cash_amount=cash,
        provenance=prov,
    )


def _obs_tuple(name):
    return tuple(_obs_from_row(r) for r in _load_jsonl(name))


class TestCrossValidateTriState(unittest.TestCase):
    def test_two_independent_sources_confirm(self):
        ev = cross_validate(_obs_tuple("two_source_confirmed.jsonl"))
        self.assertEqual(ev.validation_status, ValidationStatus.CONFIRMED)
        self.assertTrue(ev.provenance_independent)
        self.assertFalse(ev.blackout)  # CONFIRMED -> indefinite flag is False

    def test_single_source_stays_blacked_out(self):
        ev = cross_validate(_obs_tuple("single_source_blackout.jsonl"))
        self.assertEqual(ev.validation_status, ValidationStatus.SINGLE_SOURCE_BLACKOUT)
        self.assertTrue(ev.blackout)
        # is_blacked_out via the event window: on the ex-date it is blacked out.
        self.assertTrue(_is_blacked_out_single(ev, ev.ex_date_et))

    def test_conflicting_sources_blackout(self):
        ev = cross_validate(_obs_tuple("conflicting_blackout.jsonl"))
        self.assertEqual(ev.validation_status, ValidationStatus.CONFLICTING_BLACKOUT)
        self.assertTrue(ev.blackout)

    def test_type_disagreement_same_exdate_conflicts(self):
        # Two sources, same (durable_key, ex_date), DIFFERENT ca_type -> grouped together (LOW-3)
        # -> CONFLICTING_BLACKOUT, not two single-source groups.
        rows = _load_jsonl("two_source_confirmed.jsonl")
        rows[1]["ca_type"] = "dividend"
        rows[1]["factor"] = None
        rows[1]["cash_amount"] = "0.2500"
        obs = tuple(_obs_from_row(r) for r in rows)
        ev = cross_validate(obs)
        self.assertEqual(ev.validation_status, ValidationStatus.CONFLICTING_BLACKOUT)

    def test_window_override_cannot_shrink(self):
        obs = _obs_tuple("two_source_confirmed.jsonl")
        with self.assertRaises(CorporateActionError):
            cross_validate(obs, lead_days=0)
        with self.assertRaises(CorporateActionError):
            cross_validate(obs, trail_days=0)
        # A WIDER window is accepted.
        ev = cross_validate(obs, lead_days=2, trail_days=3)
        self.assertEqual(ev.blackout_from_et, "2026-06-29")
        self.assertEqual(ev.blackout_to_et, "2026-07-04")

    def test_incomplete_required_field_does_not_clear(self):
        ev = cross_validate(_obs_tuple("incomplete_split.jsonl"))
        self.assertEqual(ev.validation_status, ValidationStatus.CONFLICTING_BLACKOUT)
        self.assertTrue(ev.blackout)

    def test_mirrored_source_ca_id_not_two_independent(self):
        ev = cross_validate(_obs_tuple("mirrored_source_ca_id.jsonl"))
        self.assertFalse(ev.provenance_independent)
        self.assertNotEqual(ev.validation_status, ValidationStatus.CONFIRMED)
        self.assertTrue(ev.blackout)

    def test_whitespace_mirrored_source_ca_id_not_two_independent(self):
        rows = _load_jsonl("mirrored_source_ca_id.jsonl")
        rows[1]["source_ca_id"] = "  " + rows[1]["source_ca_id"] + "  "
        ev = cross_validate(tuple(_obs_from_row(r) for r in rows))
        self.assertFalse(ev.provenance_independent)
        self.assertNotEqual(ev.validation_status, ValidationStatus.CONFIRMED)
        self.assertTrue(ev.blackout)

    def test_same_source_twice_not_two_independent(self):
        rows = _load_jsonl("two_source_confirmed.jsonl")
        # force both observations onto the SAME source (alpaca twice), distinct source_ca_id.
        rows[1]["source"] = "alpaca"
        obs = tuple(_obs_from_row(r) for r in rows)
        ev = cross_validate(obs)
        self.assertFalse(ev.provenance_independent)
        self.assertNotEqual(ev.validation_status, ValidationStatus.CONFIRMED)
        self.assertTrue(ev.blackout)

    def test_validate_is_pure_function_of_provenance_set(self):
        obs = _obs_tuple("two_source_confirmed.jsonl")
        a = cross_validate(obs)
        b = cross_validate(tuple(reversed(obs)))  # ordering must not matter
        self.assertEqual(a.validation_status, b.validation_status)
        self.assertEqual(a.provenance_independent, b.provenance_independent)
        self.assertEqual(a.blackout_from_et, b.blackout_from_et)
        self.assertEqual(a.blackout_to_et, b.blackout_to_et)
        self.assertEqual(a.factor, b.factor)

    def test_zero_observations_raises(self):
        with self.assertRaises(CorporateActionError):
            cross_validate(())

    def test_unknown_ca_type_raises(self):
        with self.assertRaises(ValueError):
            CaType("not_a_real_ca_type")

    def test_factor_must_be_decimal_and_round_trip(self):
        # A float factor must fail loud at serialize (S2) — never coerced.
        ev = cross_validate(_obs_tuple("two_source_confirmed.jsonl"))
        with self.assertRaises(ValueError):
            dumps({"factor": float(ev.factor)})
        # A non-round-tripping factor (too many dp for FACTOR_QUANTUM) raises in cross_validate.
        rows = _load_jsonl("two_source_confirmed.jsonl")
        rows[0]["factor"] = "4.000000001"  # 9 dp, beyond 8dp FACTOR_QUANTUM
        rows[1]["factor"] = "4.000000001"
        with self.assertRaises(CorporateActionError):
            cross_validate(tuple(_obs_from_row(r) for r in rows))


class TestEmittedFieldDeterminism(unittest.TestCase):
    def test_conflicting_emitted_fields_are_order_independent(self):
        # harden OFFLINE-1: equivalent observation SETS in different INPUT order must produce an
        # IDENTICAL persisted row (and row_hash), not merely an identical validation_status. In the
        # CONFLICTING path the emitted ca_type/factor/cash/durable_id/symbol must be CANONICAL, not
        # "whichever observation came first" — otherwise status.jsonl bytes/hashes are order-dependent.
        a, b = (_obs_from_row(r) for r in _load_jsonl("conflicting_blackout.jsonl"))
        ev_ab = cross_validate((a, b))
        ev_ba = cross_validate((b, a))
        self.assertEqual(ev_ab.validation_status, ValidationStatus.CONFLICTING_BLACKOUT)
        self.assertEqual(ca_to_row(ev_ab), ca_to_row(ev_ba))
        self.assertEqual(row_hash(ca_to_row(ev_ab)), row_hash(ca_to_row(ev_ba)))

    def test_same_source_same_id_emitted_fields_order_independent(self):
        # harden CA-1: two records from the SAME source under the SAME source_ca_id (a vendor duplicate /
        # amended record) but a DIFFERENT factor are ONE observation SET; cross_validate must emit an
        # IDENTICAL row + row_hash regardless of input order. Round 1's (source, source_ca_id) sort key was
        # NOT total, so it tie-broke on input order. S7 status stays CONFLICTING_BLACKOUT throughout.
        from itertools import permutations

        did = DurableId(cusip="123456789", figi=None, ticker="ABC")

        def _obs(factor):
            return SourceObservation(
                source=CaSource.ALPACA, durable_id=did, ca_type=CaType.SPLIT, ex_date_et="2026-06-10",
                factor=Decimal(factor), cash_amount=None,
                provenance=CaProvenance(
                    source=CaSource.ALPACA, source_ca_id="A1",
                    announced_ts_utc="2026-06-01T00:00:00.000000Z",
                    ts_recv_utc="2026-06-02T00:00:00.000000Z",
                ),
            )

        members = [_obs("2.00000000"), _obs("5.00000000"), _obs("3.00000000")]
        hashes = {row_hash(ca_to_row(cross_validate(p))) for p in permutations(members)}
        statuses = {cross_validate(p).validation_status for p in permutations(members)}
        self.assertEqual(statuses, {ValidationStatus.CONFLICTING_BLACKOUT})
        self.assertEqual(len(hashes), 1, f"row_hash must be order-independent; got {len(hashes)} distinct")


class TestBlackoutWindow(unittest.TestCase):
    def test_blackout_window_is_closed_inclusive(self):
        ev = cross_validate(_obs_tuple("two_source_confirmed.jsonl"))  # CONFIRMED
        # blackout window = [ex_date-1, ex_date+1] = [2026-06-30, 2026-07-02]
        self.assertEqual(ev.blackout_from_et, "2026-06-30")
        self.assertEqual(ev.blackout_to_et, "2026-07-02")
        self.assertTrue(_is_blacked_out_single(ev, "2026-06-30"))  # from edge
        self.assertTrue(_is_blacked_out_single(ev, "2026-07-01"))  # ex-date
        self.assertTrue(_is_blacked_out_single(ev, "2026-07-02"))  # to edge
        # CONFIRMED self-clears the day AFTER the window.
        self.assertFalse(_is_blacked_out_single(ev, "2026-07-03"))

    def test_non_confirmed_blackout_is_open_ended(self):
        ev = cross_validate(_obs_tuple("single_source_blackout.jsonl"))  # SINGLE_SOURCE
        # open-ended: still blacked out far past the window's trailing edge.
        self.assertTrue(_is_blacked_out_single(ev, ev.blackout_from_et))
        self.assertTrue(_is_blacked_out_single(ev, "2027-01-01"))
        # but NOT before the window starts.
        self.assertFalse(_is_blacked_out_single(ev, "2026-01-01"))


class TestDurableIdentity(unittest.TestCase):
    def test_durable_identity_survives_ticker_change(self):
        obs = _obs_tuple("ticker_change.jsonl")  # FB -> META, same CUSIP/FIGI
        ev = cross_validate(obs)
        self.assertEqual(obs[0].durable_id.key(), obs[1].durable_id.key())
        self.assertEqual(ev.durable_id.key(), "BBG000MM2P62")  # figi preferred
        # both sources land in the same group despite different tickers.
        self.assertTrue(ev.provenance_independent)

    def test_durable_key_prefers_figi_then_cusip(self):
        self.assertEqual(DurableId(cusip="C", figi="F", ticker="X").key(), "F")
        self.assertEqual(DurableId(cusip="C", figi=None, ticker="X").key(), "C")
        # A blank FIGI is not "present"; fall back to a non-blank CUSIP instead of
        # creating an empty durable_key that can collide across tickers.
        self.assertEqual(DurableId(cusip="C", figi="", ticker="X").key(), "C")

    def test_durable_identifier_whitespace_is_canonical_in_rows_and_hashes(self):
        clean_did = DurableId(cusip="TESTAAPL1", figi="BBG000B9XRY4", ticker="AAPL")
        dirty_did = DurableId(cusip=" TESTAAPL1 ", figi=" BBG000B9XRY4\t", ticker="AAPL")

        def _obs(source, did):
            return SourceObservation(
                source=source,
                durable_id=did,
                ca_type=CaType.SPLIT,
                ex_date_et="2026-07-01",
                factor=Decimal("4.00000000"),
                cash_amount=None,
                provenance=CaProvenance(
                    source=source,
                    source_ca_id="ALP-1" if source == CaSource.ALPACA else "DV-1",
                    announced_ts_utc="2026-06-20T12:00:00.000000Z",
                    ts_recv_utc="2026-06-20T12:00:01.000000Z",
                ),
            )

        clean_row = ca_to_row(cross_validate((_obs(CaSource.ALPACA, clean_did), _obs(CaSource.DATA_VENDOR, clean_did))))
        dirty_row = ca_to_row(cross_validate((_obs(CaSource.ALPACA, dirty_did), _obs(CaSource.DATA_VENDOR, dirty_did))))

        self.assertEqual(dirty_row["cusip"], "TESTAAPL1")
        self.assertEqual(dirty_row["figi"], "BBG000B9XRY4")
        self.assertEqual(dirty_row["durable_key"], "BBG000B9XRY4")
        self.assertEqual(dirty_row, clean_row)
        self.assertEqual(row_hash(dirty_row), row_hash(clean_row))

    def test_ticker_only_identity_rejected(self):
        with self.assertRaises(CorporateActionError):
            DurableId(cusip=None, figi=None, ticker="AAPL").key()
        with self.assertRaises(CorporateActionError):
            DurableId(cusip="", figi=None, ticker="AAPL").key()
        with self.assertRaises(CorporateActionError):
            DurableId(cusip=" ", figi="\t", ticker="AAPL").key()

    def test_source_observation_must_match_persisted_provenance_source(self):
        did = DurableId(cusip="TESTAAPL1", figi="BBG000B9XRY4", ticker="AAPL")
        obs = (
            SourceObservation(
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
            ),
            SourceObservation(
                # The transient field claims DATA_VENDOR, but the durable/persisted
                # provenance says ALPACA. This is missing/corrupt provenance and must
                # not manufacture two independent sources.
                source=CaSource.DATA_VENDOR,
                durable_id=did,
                ca_type=CaType.SPLIT,
                ex_date_et="2026-07-01",
                factor=Decimal("4.00000000"),
                cash_amount=None,
                provenance=CaProvenance(
                    source=CaSource.ALPACA,
                    source_ca_id="DV-9",
                    announced_ts_utc="2026-06-20T12:05:00.000000Z",
                    ts_recv_utc="2026-06-20T12:05:01.000000Z",
                ),
            ),
        )
        with self.assertRaises(CorporateActionError):
            cross_validate(obs)

    def test_blank_source_ca_id_is_rejected(self):
        did = DurableId(cusip="TESTAAPL1", figi="BBG000B9XRY4", ticker="AAPL")
        obs = (
            SourceObservation(
                source=CaSource.ALPACA,
                durable_id=did,
                ca_type=CaType.SPLIT,
                ex_date_et="2026-07-01",
                factor=Decimal("4.00000000"),
                cash_amount=None,
                provenance=CaProvenance(
                    source=CaSource.ALPACA,
                    source_ca_id="",
                    announced_ts_utc="2026-06-20T12:00:00.000000Z",
                    ts_recv_utc="2026-06-20T12:00:01.000000Z",
                ),
            ),
            SourceObservation(
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
            ),
        )
        with self.assertRaises(CorporateActionError):
            cross_validate(obs)


class TestBrokerAdjustDetector(unittest.TestCase):
    def _fixture(self):
        f = _load_json("broker_silent_adjust.json")
        durable = DurableId(cusip=f["cusip"], figi=f["figi"], ticker=f["ticker"])
        return f, durable

    def test_any_unexplained_broker_delta_freezes(self):
        f, durable = self._fixture()
        det = BrokerAdjustDetector()
        det.seed_baseline(durable, Decimal(f["seed_qty"]))
        sig = det.observe_broker_qty(durable, Decimal(f["observed_qty"]), blacked_out=False)
        self.assertIsInstance(sig, FreezeSignal)
        self.assertTrue(sig.immediate_reconcile)
        self.assertEqual(sig.reason, FreezeReason.BROKER_ADJUSTED_NO_KNOWN_CA)
        self.assertEqual(sig.prev_qty, Decimal("100"))
        self.assertEqual(sig.curr_qty, Decimal("400"))
        self.assertTrue(det.is_frozen(durable))

    def test_broker_delta_during_blackout_labels_reason(self):
        f, durable = self._fixture()
        det = BrokerAdjustDetector()
        det.seed_baseline(durable, Decimal(f["seed_qty"]))
        sig = det.observe_broker_qty(durable, Decimal(f["observed_qty"]), blacked_out=True)
        self.assertEqual(sig.reason, FreezeReason.BROKER_ADJUSTED_DURING_BLACKOUT)
        self.assertTrue(sig.immediate_reconcile)

    def test_missing_baseline_observe_raises(self):
        _, durable = self._fixture()
        det = BrokerAdjustDetector()  # NO seed_baseline -> restart mid-CA
        with self.assertRaises(BrokerAdjustedDuringBlackout):
            det.observe_broker_qty(durable, Decimal("400"), blacked_out=True)

    def test_ca_implied_qty_cannot_override_broker_baseline(self):
        # Seeding is ONLY from the broker position-of-record. After a broker seed of 100,
        # a CA-implied/modeled qty (e.g. 400 from a 4:1 split) must NOT silently become the
        # baseline — observing it still freezes (the baseline stays the broker's 100).
        _, durable = self._fixture()
        det = BrokerAdjustDetector()
        det.seed_baseline(durable, Decimal("100"))  # broker truth
        sig = det.observe_broker_qty(durable, Decimal("400"), blacked_out=True)  # CA-implied 4:1
        self.assertIsInstance(sig, FreezeSignal)
        self.assertEqual(sig.prev_qty, Decimal("100"))  # baseline NEVER moved to 400

    def test_qty_equal_baseline_is_no_freeze(self):
        _, durable = self._fixture()
        det = BrokerAdjustDetector()
        det.seed_baseline(durable, Decimal("100"))
        self.assertIsNone(det.observe_broker_qty(durable, Decimal("100"), blacked_out=False))
        self.assertFalse(det.is_frozen(durable))


class _ScriptedFetcher:
    """A deterministic per-source CaFetcher fake (no network, no SDK). Returns only the
    observations whose source matches this fetcher's source (mirrors per-source vendor APIs)."""

    def __init__(self, source, observations):
        self._source = source
        self._obs = tuple(o for o in observations if o.source == source)

    def fetch(self, durable_id):
        return tuple(o for o in self._obs if o.durable_id.key() == durable_id.key())


class _BuggyFetcher:
    """Returns all supplied observations regardless of the fetcher/source boundary."""

    def __init__(self, observations):
        self._obs = tuple(observations)

    def fetch(self, durable_id):
        return tuple(o for o in self._obs if o.durable_id.key() == durable_id.key())


def _feed_for(fixture_name):
    obs = _obs_tuple(fixture_name)
    fetchers = {
        CaSource.ALPACA: _ScriptedFetcher(CaSource.ALPACA, obs),
        CaSource.DATA_VENDOR: _ScriptedFetcher(CaSource.DATA_VENDOR, obs),
    }
    return CorporateActionFeed(fetchers), obs[0].durable_id


class TestCorporateActionFeed(unittest.TestCase):
    def test_feed_confirms_two_sources(self):
        feed, durable = _feed_for("two_source_confirmed.jsonl")
        evs = feed.adjustments_for(durable, ts_recv_utc="2026-06-20T12:10:00.000000Z")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].validation_status, ValidationStatus.CONFIRMED)
        # CONFIRMED -> bounded window: blacked out on the edges, clears after.
        self.assertTrue(feed.is_blacked_out(durable, on_date_et="2026-06-30"))
        self.assertTrue(feed.is_blacked_out(durable, on_date_et="2026-07-02"))
        self.assertFalse(feed.is_blacked_out(durable, on_date_et="2026-07-03"))

    def test_feed_single_source_open_ended_blackout(self):
        feed, durable = _feed_for("single_source_blackout.jsonl")
        evs = feed.adjustments_for(durable, ts_recv_utc="2026-07-20T12:10:00.000000Z")
        self.assertEqual(evs[0].validation_status, ValidationStatus.SINGLE_SOURCE_BLACKOUT)
        self.assertTrue(feed.is_blacked_out(durable, on_date_et="2027-01-01"))  # never self-clears
        self.assertFalse(feed.is_blacked_out(durable, on_date_et="2026-01-01"))  # before window start

    def test_feed_is_deterministic_given_fixed_fetchers(self):
        feed, durable = _feed_for("two_source_confirmed.jsonl")
        a = feed.adjustments_for(durable, ts_recv_utc="2026-06-20T12:10:00.000000Z")
        b = feed.adjustments_for(durable, ts_recv_utc="2026-06-20T12:10:00.000000Z")
        self.assertEqual(a[0].validation_status, b[0].validation_status)
        self.assertEqual(a[0].provenance_independent, b[0].provenance_independent)

    def test_fetcher_cannot_self_report_another_source(self):
        obs = _obs_tuple("two_source_confirmed.jsonl")
        feed = CorporateActionFeed({CaSource.ALPACA: _BuggyFetcher(obs)})
        with self.assertRaises(CorporateActionError):
            feed.adjustments_for(obs[0].durable_id, ts_recv_utc="2026-06-20T12:10:00.000000Z")


class TestSerializerWall(unittest.TestCase):
    def test_frozenset_field_never_serialized_directly(self):
        with self.assertRaises(TypeError):
            dumps({"x": frozenset({"alpaca", "data_vendor"})})


def _is_blacked_out_single(ev: AdjustmentEvent, on_date_et: str) -> bool:
    """Mirror CorporateActionFeed.is_blacked_out for a single event (MED-6 coherent rule)."""
    if ev.validation_status == ValidationStatus.CONFIRMED:
        return ev.blackout_from_et <= on_date_et <= ev.blackout_to_et
    return on_date_et >= ev.blackout_from_et


if __name__ == "__main__":
    unittest.main()
