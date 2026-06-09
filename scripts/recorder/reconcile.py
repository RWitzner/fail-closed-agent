"""Dual-hash reconcile of a recorded depth stream against a reference (contract §I).

Reconcile re-derives the per-row ``book_hash`` from a recorded stream AND from a
reference stream (a second recorded stream / golden file) and compares them keyed on
the per-stream ordinal ``(symbol, vendor_seq, occurrence)`` (D5), so null/duplicate
vendor_seq rows each get their own slot. It NEVER silently mutates either side: a hash
that differs is
a ``mismatch``; a key present on one side only is ``missing_in_recorded`` /
``missing_in_reference``. ``ok`` is True iff there is no mismatch and nothing missing
(fail-closed, spec §7 reconcile) — a CLI maps not-ok to a non-zero exit so a reconcile
divergence is never silently swallowed.

Both sides are read hash-verified via ``recorder.replay.replay_book_hashes`` (which
delegates to ``agent.journal.replay``), so the dual-hash here is the M0-canonical row
hash on each side, then the L2 ladder ``book_hash`` re-derived from each. Single-file
in M1 (no rotation; MINOR 9).

The credentialed Databento-historical pull (``reconcile_against_historical``) is a
tier-2 stub: it is NEVER called offline and raises ``NotImplementedError`` in M1.
"""
from dataclasses import dataclass
from typing import Optional, Tuple

from recorder.replay import replay_book_hashes


@dataclass(frozen=True)
class ReconcileReport:
    matched: int
    mismatches: tuple                # ({symbol, vendor_seq, recorded_hash, reference_hash}, ...)
    missing_in_recorded: tuple       # keys present in the reference but absent from the recorded stream
    missing_in_reference: tuple      # keys present in the recorded stream but absent from the reference
    ok: bool                         # True iff no mismatch and nothing missing


def reconcile_against_fixture(recorded_path, reference_path) -> ReconcileReport:
    """OFFLINE dual-hash reconcile: compare the recorder's re-derived hashes against a
    pinned reference fixture (a second recorded stream / golden file). Keyed on
    ``(symbol, vendor_seq)``. ``ok=False`` on ANY mismatch or missing row (fail-closed);
    neither side is mutated. Single-file in M1 (MINOR 9).

    C6 (finding #5): each side is replayed via ``replay_book_hashes``, which verifies
    every persisted ``derived_book_hash`` against its OWN re-derived hash IN ROW ORDER
    (C2). A side that FAILS its own single-stream replay (a stale/wrong persisted
    ``derived_book_hash``) is folded in as a HARD reconcile failure (``ok=False``) even
    when the two streams' re-derived hashes otherwise match — a single-stream divergence
    is never silently swallowed.
    """
    recorded_result = replay_book_hashes(recorded_path)
    reference_result = replay_book_hashes(reference_path)
    recorded = dict(recorded_result.rederived_book_hashes)
    reference = dict(reference_result.rederived_book_hashes)

    mismatches = []
    matched = 0
    for key in sorted(set(recorded) & set(reference), key=_sort_key):
        symbol, vendor_seq = key[0], key[1]  # D5: key is (symbol, vendor_seq, occurrence)
        if recorded[key] == reference[key]:
            matched += 1
        else:
            mismatches.append({
                "symbol": symbol,
                "vendor_seq": vendor_seq,
                "recorded_hash": recorded[key],
                "reference_hash": reference[key],
            })

    missing_in_recorded = tuple(
        {"symbol": k[0], "vendor_seq": k[1], "reference_hash": reference[k]}
        for k in sorted(set(reference) - set(recorded), key=_sort_key)
    )
    missing_in_reference = tuple(
        {"symbol": k[0], "vendor_seq": k[1], "recorded_hash": recorded[k]}
        for k in sorted(set(recorded) - set(reference), key=_sort_key)
    )

    ok = (
        not mismatches
        and not missing_in_recorded
        and not missing_in_reference
        and recorded_result.ok          # C6: a single-stream replay divergence is a HARD failure
        and reference_result.ok
    )
    return ReconcileReport(
        matched=matched,
        mismatches=tuple(mismatches),
        missing_in_recorded=missing_in_recorded,
        missing_in_reference=missing_in_reference,
        ok=ok,
    )


def _sort_key(key: Tuple):
    # Stable ordering with a None-safe vendor_seq so the report is deterministic.
    # D5: key is (symbol, vendor_seq, occurrence); occurrence is the final tie-breaker.
    symbol, vendor_seq, occurrence = key[0], key[1], key[2]
    return (symbol, vendor_seq is None, vendor_seq if vendor_seq is not None else 0, occurrence)


def reconcile_against_historical(recorded_path, historical_loader):
    """Tier-2 STUB. ``historical_loader`` is injected and would lazily build the SDK
    client (HISTORICAL API per the 2026-06-09 access constraint, §P 2a). Offline tests
    NEVER call this. Raises ``NotImplementedError`` in M1 offline scope.
    """
    raise NotImplementedError(
        "credentialed Databento-historical reconcile lands in M1 tier-2 (HISTORICAL-verified, §P 2a)"
    )
