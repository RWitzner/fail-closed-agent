"""Re-derive ``book_hash`` from recorded depth streams (contract §H; invariant S3).

S3 is the replay-stability invariant: a depth row persisted with a
``derived_book_hash`` MUST re-derive that EXACT hash on replay, byte-for-byte,
from the SAME flat field names persistence wrote. This module is a PURE function
of the recorded events — it rebuilds each ``DepthEvent`` via
``recorder.event_row.from_row`` (BLOCKER 2), feeds a fresh ``EquityBookState``,
and re-derives ``book_hash`` (§D) at each step. Because ``book_hash`` excludes all
provenance (ts / vendor_seq / reconnect_epoch / per-level ct), the SAME physical
book re-derives the SAME hash on replay, on another host, and after a reconnect.

Hash-verification of the underlying rows inherits from ``agent.journal.replay``
(via ``recorder.persistence.replay_stream``): the truncated-tail rule and
``JournalCorruption`` semantics are the SAME code path as M0 — this module never
re-implements them. A re-derived hash that does NOT match the persisted
``derived_book_hash`` is REPORTED (``ok=False`` + ``first_mismatch``), never
silently mutated; ``assert_matches_expected`` raises ``ReplayHashMismatch`` against
the pinned ``replay_expected_hashes.json``.

All derivations key on the per-stream ordinal ``(symbol, vendor_seq, occurrence)``
(D5; BLOCKER 1 vendor_seq rename), so null/duplicate vendor_seq rows each get their
own verified slot instead of last-write-win collapsing. Single-file in M1 (no
rotation; MINOR 9).
"""
import json
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from recorder.book_hash import book_hash
from recorder.book_state import BookSnapshot, EquityBookState
from recorder.event import DepthEvent
from recorder.event_row import from_row
from recorder.persistence import replay_stream


class ReplayHashMismatch(Exception):
    """A re-derived book_hash does not match the recorded/expected hash (S3 failure)."""


@dataclass(frozen=True)
class ReplayResult:
    rows: list                                   # hash-verified persisted rows (via replay_stream); [] for the event path
    rederived_book_hashes: Dict[tuple, str]      # D5: {(symbol, vendor_seq, occurrence) -> book_hash}; one slot per row
    final_snapshots: Dict[str, BookSnapshot]     # {symbol -> last canonical BookSnapshot}
    ok: bool                                     # True iff no persisted-hash mismatch was seen
    first_mismatch: Optional[dict]               # {symbol, vendor_seq, recorded_hash, rederived_hash} or None


def _state_for(states: Dict[str, EquityBookState], ev: DepthEvent) -> EquityBookState:
    prov = ev.provenance
    state = states.get(prov.symbol)
    if state is None:
        state = EquityBookState(prov.symbol, prov.instrument_id)
        states[prov.symbol] = state
    return state


def _derive(depth_events: Iterable[DepthEvent], *, symbols=None,
            recorded_hashes: Optional[list] = None,
            rows: Optional[list] = None) -> ReplayResult:
    """Shared core: feed each DepthEvent through a fresh per-symbol EquityBookState,
    re-derive book_hash, and (when ``recorded_hashes`` is given) compare against the
    persisted derived_book_hash IN ROW ORDER. The first mismatch is recorded but
    derivation continues so the full hash map is always available.

    C2 (finding #6): ``recorded_hashes`` is an ORDERED list parallel to ``depth_events``
    (each element the row's persisted derived_book_hash, or None), NOT a dict keyed by
    (symbol, vendor_seq). Collapsing into such a dict would last-write-win and MASK a
    corrupt/duplicate/null-seq NON-LAST row (an S3 hole). Every row is verified against
    its OWN re-derived hash in row order; a colliding/duplicate key is surfaced, not
    overwritten.

    D5 (R2#5): ``rederived_book_hashes`` is keyed by a per-stream ORDINAL, not by
    (symbol, vendor_seq). The key is ``(symbol, vendor_seq, occurrence)`` where
    ``occurrence`` is the 0-based count of prior rows that shared that
    ``(symbol, vendor_seq)`` — so null/duplicate vendor_seq rows each get their OWN
    verified slot in the map (no last-write-wins collapse to one ``(symbol, None)`` slot,
    which masked a wrong non-last null-seq row in the map ``assert_matches_expected`` /
    ``reconcile`` join on). For the common unique-key case ``occurrence`` is 0, so the
    first/only occurrence at ``(symbol, vendor_seq, 0)`` is what the unique-key expected
    file and an identically-ordered reference stream both produce (§H)."""
    allow = set(symbols) if symbols is not None else None
    states: Dict[str, EquityBookState] = {}
    rederived: Dict[tuple, str] = {}
    occurrences: Dict[tuple, int] = {}  # D5: per-(symbol, vendor_seq) occurrence ordinal
    first_mismatch: Optional[dict] = None
    for idx, ev in enumerate(depth_events):
        prov = ev.provenance
        if allow is not None and prov.symbol not in allow:
            continue
        state = _state_for(states, ev)
        state.apply(ev)
        derived = book_hash(state.snapshot())
        seq_key = (prov.symbol, prov.vendor_seq)
        occurrence = occurrences.get(seq_key, 0)
        occurrences[seq_key] = occurrence + 1
        key = (prov.symbol, prov.vendor_seq, occurrence)  # D5: ordinal slot per row
        rederived[key] = derived
        if recorded_hashes is not None and first_mismatch is None:
            recorded = recorded_hashes[idx]
            if recorded is not None and recorded != derived:
                first_mismatch = {
                    "symbol": prov.symbol,
                    "vendor_seq": prov.vendor_seq,
                    "recorded_hash": recorded,
                    "rederived_hash": derived,
                }
    final_snapshots = {symbol: state.snapshot() for symbol, state in states.items()}
    return ReplayResult(
        rows=rows if rows is not None else [],
        rederived_book_hashes=rederived,
        final_snapshots=final_snapshots,
        ok=first_mismatch is None,
        first_mismatch=first_mismatch,
    )


def replay_book_hashes(depth_stream_path) -> ReplayResult:
    """Re-read a persisted depth stream and re-derive book_hash for each depth row.

    Rows are read hash-verified via ``replay_stream`` (delegating to
    ``agent.journal.replay`` — same truncated-tail / ``JournalCorruption`` semantics
    as M0). Each depth row is rebuilt via ``from_row`` (§B2), fed through a fresh
    ``EquityBookState``, and its book_hash re-derived. The re-derived hash is compared
    BYTE-for-BYTE against the row's persisted ``derived_book_hash``; a divergence is
    REPORTED (``ok=False`` + ``first_mismatch``), never silently mutated (S3). Keyed on
    ``(symbol, vendor_seq)``. Single-file in M1 (MINOR 9).
    """
    rows = replay_stream(depth_stream_path)
    depth_events = []
    recorded_hashes: list = []  # C2: ordered, parallel to depth_events (no key-uniquing)
    for row in rows:
        # E2 (R3#2): SKIP non-event rows. When no alert_writer is injected, the recorder
        # routes data_quality_alert rows (NO 'schema' field) into the events stream;
        # from_row would raise MalformedRecord('missing required flat field schema') and
        # crash the whole replay. Depth-hash verification applies ONLY to schema-bearing
        # event rows.
        if "schema" not in row:
            continue
        ev = from_row(row)
        if not isinstance(ev, DepthEvent):
            continue  # non-depth rows carry no book_hash
        depth_events.append(ev)
        recorded_hashes.append(row.get("derived_book_hash"))
    return _derive(depth_events, recorded_hashes=recorded_hashes, rows=rows)


def derive_book_hashes(events: Iterable, *, symbols=None) -> ReplayResult:
    """Re-derive book_hash directly from already-parsed events (fixture-driven path).

    Non-depth events are ignored. Pure function of the input -> deterministic. Keyed on
    ``(symbol, vendor_seq)``. Used by the fixture-driven S3 test where events are parsed
    in memory rather than read back from a persisted stream.
    """
    depth_events = [ev for ev in events if isinstance(ev, DepthEvent)]
    return _derive(depth_events, symbols=symbols)


def assert_matches_expected(result: ReplayResult, expected_path) -> None:
    """Compare the re-derived hashes against the pinned ``replay_expected_hashes.json``
    byte-for-byte. Raise ``ReplayHashMismatch`` (with symbol + vendor_seq + both hashes)
    on the FIRST divergence, including a key the expected file lists but the result is
    missing.

    D5: ``rederived_book_hashes`` is keyed ``(symbol, vendor_seq, occurrence)``. The
    expected ``hashes`` list is consumed IN ORDER, deriving the same per-
    ``(symbol, vendor_seq)`` occurrence ordinal the stream produced, so each expected
    row joins its OWN re-derived slot (a duplicate/null-seq expected row is NOT masked
    by last-write-wins).
    """
    with open(expected_path, encoding="utf-8") as fh:
        expected = json.loads(fh.read())
    occurrences: Dict[tuple, int] = {}
    for entry in expected["hashes"]:
        seq_key = (entry["symbol"], entry["vendor_seq"])
        occurrence = occurrences.get(seq_key, 0)
        occurrences[seq_key] = occurrence + 1
        key = (entry["symbol"], entry["vendor_seq"], occurrence)
        rederived = result.rederived_book_hashes.get(key)
        recorded = entry["book_hash"]
        if rederived is None:
            raise ReplayHashMismatch(
                f"expected hash for {seq_key} but replay produced none"
            )
        if rederived != recorded:
            raise ReplayHashMismatch(
                f"book_hash mismatch for symbol={entry['symbol']} "
                f"vendor_seq={entry['vendor_seq']}: expected {recorded} got {rederived}"
            )
