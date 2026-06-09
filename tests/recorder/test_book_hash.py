"""L2 MBP-10 canonical depth-ladder hash (contract §D; tests §N) — correctness centerpiece.

book_hash MUST be byte-stable across runs, machines, Python builds, and
dict/vendor-insertion order. It reuses agent.serializer.row_hash so there is
exactly ONE hashing convention in the repo. Identity (symbol+instrument_id) is
INCLUDED; all provenance (ts/vendor_seq/epoch) AND per-level ct are EXCLUDED, so
the SAME physical book re-derives the SAME hash on replay / another host / after a
reconnect. Zero-size padding and duplicate-price splits collapse to one form.
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path

from recorder.book_hash import BOOK_HASH_VERSION, MAX_LEVELS, book_hash, canonical_book_payload
from recorder.book_state import BookSnapshot, EquityBookState
from recorder.event import DepthEvent, DepthLevel, PrecisionLoss, Provenance, parse

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "databento"


def _load_jsonl(name):
    path = FIXTURES / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _parse_depth(record):
    return parse(
        record,
        dataset=record["dataset"],
        schema=record["schema"],
        reconnect_epoch=0,
        ts_recv_utc="2026-06-09T13:30:00.000999Z",
    )


def _snapshot(bids, asks, *, symbol="AAPL", instrument_id=1001, crossed=False):
    """bids/asks: lists of (px_str, sz_str, ct). Built directly so tests control the ladder.

    NOTE: Decimals are taken verbatim — use this for ladders that are ALREADY canonical
    (one entry per price, no zero-padding, str-canonical px/sz). For raw vendor encodings
    that must be canonicalized at parse (e.g. '300.0' size, '1.50' price), use
    ``_snapshot_via_parse`` so the quantize-at-parse step (§B) runs — book_hash itself
    does NOT requantize (there is ONE canonicalization path in the repo).
    """
    return BookSnapshot(
        symbol=symbol,
        instrument_id=instrument_id,
        bids=tuple(DepthLevel(px=Decimal(p), sz=Decimal(s), ct=c) for p, s, c in bids),
        asks=tuple(DepthLevel(px=Decimal(p), sz=Decimal(s), ct=c) for p, s, c in asks),
        crossed=crossed,
    )


def _snapshot_via_parse(bids, asks, *, symbol="AAPL", instrument_id=1001):
    """Build a snapshot by routing raw vendor [px_str, sz_str, ct] through parse() ->
    EquityBookState, so quantize-at-parse (§B) canonicalizes px/sz before hashing."""
    record = {
        "dataset": "<DEPTH_DATASET>", "schema": "mbp-10",
        "instrument_id": instrument_id, "symbol": symbol, "vendor_seq": 2001,
        "ts_event": "2026-06-09T13:30:00.500000Z",
        "bids": [list(level) for level in bids],
        "asks": [list(level) for level in asks],
    }
    state = EquityBookState(symbol, instrument_id)
    state.apply(_parse_depth(record))
    return state.snapshot()


class TestCanonicalPayload(unittest.TestCase):
    def test_payload_shape_versioned_identity_str_levels(self):
        snap = _snapshot([("201.1500", "300", 3)], [("201.1600", "200", 2)])
        payload = canonical_book_payload(snap)
        self.assertEqual(payload["v"], BOOK_HASH_VERSION)
        self.assertEqual(BOOK_HASH_VERSION, 2)
        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(payload["instrument_id"], 1001)
        # Levels are [px_str, sz_str] — ct is NOT in the payload.
        self.assertEqual(payload["bids"], [["201.1500", "300"]])
        self.assertEqual(payload["asks"], [["201.1600", "200"]])

    def test_payload_excludes_ct(self):
        a = canonical_book_payload(_snapshot([("201.1500", "300", 3)], [("201.1600", "200", 2)]))
        b = canonical_book_payload(_snapshot([("201.1500", "300", 99)], [("201.1600", "200", 0)]))
        self.assertEqual(a, b)

    def test_payload_drops_zero_size_and_coalesces_duplicates(self):
        # Raw vendor encoding ('300.0', zero-size padding) routed through parse so the
        # size is canonicalized to '300' before hashing (§B owns quantization).
        snap = _snapshot_via_parse(
            [("201.1500", "300.0", 3), ("201.1500", "0", 0), ("201.1400", "200", 2), ("201.1400", "300", 3)],
            [("201.1600", "200", 2)],
        )
        payload = canonical_book_payload(snap)
        # 201.1500 zero-size padding dropped; 201.1400 split (200+300) coalesced -> 500.
        self.assertEqual(payload["bids"], [["201.1500", "300"], ["201.1400", "500"]])

    def test_payload_drops_standalone_unique_price_zero_size_padding(self):
        # G2 (R5): a STANDALONE unique-price zero-size level (no same-price sibling to
        # coalesce-mask the drop) must be dropped, so padding-present == padding-omitted.
        # 199.0000 is a UNIQUE price with sz==0; nothing else sums it away.
        padded = _snapshot(
            [("200.0000", "100", 1), ("199.0000", "0", 0)],
            [("201.0000", "200", 2)],
        )
        omitted = _snapshot(
            [("200.0000", "100", 1)],
            [("201.0000", "200", 2)],
        )
        # The dropped level leaves no trace in the canonical payload...
        self.assertEqual(canonical_book_payload(padded)["bids"], [["200.0000", "100"]])
        self.assertEqual(canonical_book_payload(padded), canonical_book_payload(omitted))
        # ...nor in the hash.
        self.assertEqual(book_hash(padded), book_hash(omitted))

    def test_payload_bids_desc_asks_asc(self):
        snap = _snapshot(
            [("201.1400", "200", 2), ("201.1500", "300", 3)],
            [("201.1700", "400", 4), ("201.1600", "200", 2)],
        )
        payload = canonical_book_payload(snap)
        self.assertEqual([lvl[0] for lvl in payload["bids"]], ["201.1500", "201.1400"])
        self.assertEqual([lvl[0] for lvl in payload["asks"]], ["201.1600", "201.1700"])


class TestDeterminism(unittest.TestCase):
    def test_same_snapshot_same_hash(self):
        s1 = _snapshot([("201.1500", "300", 3)], [("201.1600", "200", 2)])
        s2 = _snapshot([("201.1500", "300", 3)], [("201.1600", "200", 2)])
        self.assertEqual(book_hash(s1), book_hash(s2))

    def test_hash_is_order_independent(self):
        ordered = _snapshot(
            [("201.1500", "300", 3), ("201.1400", "200", 2)],
            [("201.1600", "200", 2), ("201.1700", "400", 4)],
        )
        shuffled = _snapshot(
            [("201.1400", "200", 2), ("201.1500", "300", 3)],
            [("201.1700", "400", 4), ("201.1600", "200", 2)],
        )
        self.assertEqual(book_hash(ordered), book_hash(shuffled))

    def test_hash_is_sha256_hex(self):
        h = book_hash(_snapshot([("201.1500", "300", 3)], [("201.1600", "200", 2)]))
        self.assertEqual(len(h), 64)
        int(h, 16)  # raises if not hex


class TestIdentityInHash(unittest.TestCase):
    def test_changing_symbol_changes_hash(self):
        aapl = _snapshot([("201.1500", "300", 3)], [("201.1600", "200", 2)], symbol="AAPL")
        msft = _snapshot([("201.1500", "300", 3)], [("201.1600", "200", 2)], symbol="MSFT")
        self.assertNotEqual(book_hash(aapl), book_hash(msft))

    def test_changing_instrument_id_changes_hash(self):
        a = _snapshot([("201.1500", "300", 3)], [("201.1600", "200", 2)], instrument_id=1001)
        b = _snapshot([("201.1500", "300", 3)], [("201.1600", "200", 2)], instrument_id=2002)
        self.assertNotEqual(book_hash(a), book_hash(b))


class TestProvenanceAndCtExcluded(unittest.TestCase):
    def test_ct_does_not_change_hash(self):
        a = _snapshot([("201.1500", "300", 3)], [("201.1600", "200", 2)])
        b = _snapshot([("201.1500", "300", 99)], [("201.1600", "200", 1)])
        self.assertEqual(book_hash(a), book_hash(b))

    def test_crossed_flag_does_not_change_hash(self):
        """crossed is recorder-surfaced state, NOT a hash input."""
        a = _snapshot([("201.1700", "100", 1)], [("201.1600", "100", 1)], crossed=True)
        b = _snapshot([("201.1700", "100", 1)], [("201.1600", "100", 1)], crossed=False)
        self.assertEqual(book_hash(a), book_hash(b))

    def test_provenance_not_in_hash_path_via_state(self):
        """Two states with different ts/vendor_seq/epoch but the SAME book hash equal."""
        def _ev(*, vendor_seq, ts_event, epoch):
            return DepthEvent(
                provenance=Provenance(
                    dataset="<DEPTH_DATASET>", schema="mbp-10", instrument_id=1001, symbol="AAPL",
                    vendor_seq=vendor_seq, ts_event_utc=ts_event,
                    ts_recv_utc="2026-06-09T13:30:00.000999Z", reconnect_epoch=epoch,
                ),
                bids=(DepthLevel(px=Decimal("201.1500"), sz=Decimal("300"), ct=3),),
                asks=(DepthLevel(px=Decimal("201.1600"), sz=Decimal("200"), ct=2),),
            )
        s1 = EquityBookState("AAPL", 1001)
        s1.apply(_ev(vendor_seq=2001, ts_event="2026-06-09T13:30:00.500000Z", epoch=0))
        s2 = EquityBookState("AAPL", 1001)
        s2.apply(_ev(vendor_seq=9999, ts_event="2026-06-09T18:00:00.000000Z", epoch=7))
        self.assertEqual(book_hash(s1.snapshot()), book_hash(s2.snapshot()))


class TestNormalizationIntoHash(unittest.TestCase):
    def test_zero_size_and_duplicate_price_hash_identically(self):
        """Padded/omitted + split-price encodings of the SAME book hash identically (fixture row 2001)."""
        padded = _snapshot(
            [("201.1500", "300", 3), ("201.1500", "0", 0), ("201.1400", "200", 2), ("201.1400", "300", 3)],
            [("201.1600", "200", 2)],
        )
        clean = _snapshot(
            [("201.1500", "300", 3), ("201.1400", "500", 5)],
            [("201.1600", "200", 2)],
        )
        self.assertEqual(book_hash(padded), book_hash(clean))

    def test_size_300pt0_and_300_hash_identically(self):
        """MAJOR 3: a '300.0' size and a '300' size produce the SAME hash (canonicalized at parse, §B)."""
        a = _snapshot_via_parse([("201.1500", "300.0", 3)], [("201.1600", "200", 2)])
        b = _snapshot_via_parse([("201.1500", "300", 3)], [("201.1600", "200", 2)])
        self.assertEqual(book_hash(a), book_hash(b))

    def test_price_1pt50_and_1pt5_hash_identically(self):
        # '1.50'/'1.6000' vs '1.5000'/'1.60' canonicalize to the same 4dp string at parse (§B).
        a = _snapshot_via_parse([("1.5000", "300", 3)], [("1.6000", "200", 2)])
        b = _snapshot_via_parse([("1.50", "300", 3)], [("1.60", "200", 2)])
        self.assertEqual(book_hash(a), book_hash(b))


class TestC3SelfCanonicalizingHash(unittest.TestCase):
    """C3 (finding #2): book_hash self-canonicalizes for OFF-PARSE-PATH DepthLevels.

    A DepthLevel built without the parser ('300.0' vs '300', '201.15' vs '201.1500')
    must hash identically, so a hand-authored reference stream in
    reconcile_against_fixture does not yield a FALSE mismatch. Record-path hashes
    (already canonical) MUST NOT change (golden-hash invariance below).
    """

    def test_offparse_size_300pt0_equals_300(self):
        # DepthLevel built DIRECTLY (no parse) with a non-canonical size string.
        a = _snapshot([("201.1500", "300.0", 3)], [("201.1600", "200", 2)])
        b = _snapshot([("201.1500", "300", 3)], [("201.1600", "200", 2)])
        self.assertEqual(book_hash(a), book_hash(b))

    def test_offparse_price_201pt15_equals_201pt1500(self):
        a = _snapshot([("201.15", "300", 3)], [("201.16", "200", 2)])
        b = _snapshot([("201.1500", "300", 3)], [("201.1600", "200", 2)])
        self.assertEqual(book_hash(a), book_hash(b))

    def test_record_path_golden_hashes_unchanged(self):
        # Record-path (parse -> EquityBookState -> book_hash) must remain byte-stable
        # vs the pinned golden file, proving C3 only closes the off-parse bypass.
        expected = json.loads(
            (FIXTURES / "replay_expected_hashes.json").read_text(encoding="utf-8")
        )
        by_seq = {h["vendor_seq"]: h["book_hash"] for h in expected["hashes"]}
        for record in _load_jsonl("mbp10_depth_sample.jsonl"):
            state = EquityBookState("AAPL", 1001)
            state.apply(_parse_depth(record))
            self.assertEqual(book_hash(state.snapshot()), by_seq[record["vendor_seq"]])


class TestD1FailLoudSymmetric(unittest.TestCase):
    """D1 (R2#1): book_hash._canonical_side MUST use the parser's round-trip guard
    (event._quantize_checked), so a sub-quantum off-parse-path value RAISES
    PrecisionLoss instead of silently collapsing to '0.0000'. Record-path hashes
    (already canonical) MUST stay byte-identical to the golden file.
    """

    def test_subpenny_price_offparse_raises_precision_loss(self):
        # A genuine sub-$1 sub-penny price (0.00005 -> 0.0000 under PRICE_QUANTUM)
        # built DIRECTLY (no parse) must RAISE, not silently zero the level.
        snap = _snapshot([("0.00005", "300", 3)], [("201.1600", "200", 2)])
        with self.assertRaises(PrecisionLoss):
            book_hash(snap)

    def test_fractional_share_size_offparse_raises_precision_loss(self):
        # A fractional share (1.5 -> not whole under SIZE_QUANTUM) off-parse-path raises.
        snap = _snapshot([("201.1500", "1.5", 3)], [("201.1600", "200", 2)])
        with self.assertRaises(PrecisionLoss):
            book_hash(snap)

    def test_canonical_300pt0_equals_300_still_holds(self):
        # The round-trip guard does NOT break the existing self-canonicalization:
        # '300.0' and '300' (both round-trip cleanly to whole shares) hash identically.
        a = _snapshot([("201.1500", "300.0", 3)], [("201.1600", "200", 2)])
        b = _snapshot([("201.1500", "300", 3)], [("201.1600", "200", 2)])
        self.assertEqual(book_hash(a), book_hash(b))

    def test_record_path_golden_hashes_unchanged(self):
        # The fix must NOT move record-path hashes: parse -> state -> book_hash must
        # still byte-match the pinned golden file (RECORD-PATH HASHES MUST NOT CHANGE).
        expected = json.loads(
            (FIXTURES / "replay_expected_hashes.json").read_text(encoding="utf-8")
        )
        by_seq = {h["vendor_seq"]: h["book_hash"] for h in expected["hashes"]}
        for record in _load_jsonl("mbp10_depth_sample.jsonl"):
            state = EquityBookState("AAPL", 1001)
            state.apply(_parse_depth(record))
            self.assertEqual(book_hash(state.snapshot()), by_seq[record["vendor_seq"]])


class TestFailLoud(unittest.TestCase):
    def test_float_price_into_payload_raises(self):
        bad = BookSnapshot(
            symbol="AAPL", instrument_id=1001,
            bids=(DepthLevel(px=1.5, sz=Decimal("300"), ct=3),),  # noqa: float on purpose
            asks=(DepthLevel(px=Decimal("201.1600"), sz=Decimal("200"), ct=2),),
            crossed=False,
        )
        with self.assertRaises((ValueError, TypeError)):
            book_hash(bad)


class TestFixtureDriven(unittest.TestCase):
    def test_fixture_rows_produce_stable_hashes(self):
        records = _load_jsonl("mbp10_depth_sample.jsonl")
        hashes = []
        for record in records:
            state = EquityBookState("AAPL", 1001)
            state.apply(_parse_depth(record))
            hashes.append(book_hash(state.snapshot()))
        # Two distinct books -> two distinct hashes; both valid sha256 hex.
        self.assertEqual(len(hashes), 2)
        self.assertNotEqual(hashes[0], hashes[1])
        for h in hashes:
            self.assertEqual(len(h), 64)

    def test_max_levels_constant(self):
        self.assertEqual(MAX_LEVELS, 10)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
