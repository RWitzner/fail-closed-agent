"""L2 MBP-10 canonical depth-ladder hash (contract §D) — the correctness centerpiece.

This is the hash ``replay.py`` re-derives and ``test_replay_hashes.py`` pins
against ``replay_expected_hashes.json``. It MUST be byte-stable across runs,
machines, Python builds, and dict/vendor-insertion order. It reuses
``agent.serializer.row_hash`` so there is exactly ONE hashing convention in the repo.

Why this is deterministic & replay-stable:
  * Decimal-as-string only; float entry is impossible (``row_hash``->``dumps``
    RAISES on any float / non-finite Decimal — it fails loud, never hashes silently).
  * Ordering is intrinsic to price (``_canonical_side`` sorts by price) plus
    ``dumps(sort_keys=True)`` — vendor/dict/insertion order cannot change the bytes.
  * Quantization-at-parse (§B) makes ``str(Decimal)`` canonical, closing
    ``1.5``/``1.50`` (prices) AND ``300.0``/``300`` (sizes, MAJOR 3).
  * Zero-size + duplicate-price normalization collapses two vendor encodings of the
    same economic book to one hash.
  * Identity (symbol+instrument_id) is INCLUDED; all provenance (ts/vendor_seq/
    epoch) AND per-level ``ct`` are EXCLUDED, so the SAME physical book re-derives
    the SAME hash on replay, on another host, and after a reconnect.
  * Versioned (``"v":2``) isolates the L2 hash from a future L3/MBO hash.
"""
from typing import List, Sequence

from agent.serializer import row_hash  # reuse the ONE M0 hashing primitive (serializer.py:53)
from recorder.event import (  # C3/D1: requantize off-parse-path levels via the parser's guard
    PRICE_QUANTUM,
    SIZE_QUANTUM,
    _quantize_checked,
)

MAX_LEVELS = 10
BOOK_HASH_VERSION = 2  # 2 = L2 MBP-10 ladder; isolates from any future L3/MBO hash


def _canonical_side(levels: Sequence["DepthLevel"], *, descending: bool) -> List[list]:
    """Normalize one side to a deterministic list of ``[px_str, sz_str]``.

    1. Drop levels with ``sz == 0`` (an empty level == no level; vendor padding/
       omission collapse to one form).
    2. Coalesce duplicate prices by SUMMING sizes (vendor may split one price).
    3. Sort by price: bids DESCENDING, asks ASCENDING. Price is the SOLE sort key
       (ties impossible post-coalesce).
    4. Truncate to MAX_LEVELS.
    5. Render px and sz as ``str(Decimal)``. C3/D1 (defense-in-depth, fail loud
       symmetrically): px is requantized to PRICE_QUANTUM and sz to SIZE_QUANTUM here
       via the SAME ``event._quantize_checked`` round-trip guard the parser uses, so the
       hash is canonical regardless of how the DepthLevel was built — a hand-authored
       reference ladder ('300.0' vs '300', '201.15' vs '201.1500') hashes identically and
       does NOT yield a false reconcile mismatch — AND a sub-quantum off-parse-path value
       (e.g. a sub-$1 sub-penny '0.00005' price or a '1.5' fractional share) RAISES
       PrecisionLoss instead of silently collapsing to '0.0000' (D1). The parse->record
       path already emits canonical Decimals, so this is a no-op there (record-path hashes
       UNCHANGED); it only closes the off-parse-path bypass.

    ``ct`` is NOT included. Raises if any px/sz is not a finite Decimal or sz < 0,
    or if a px/sz does not round-trip through its quantum (PrecisionLoss).
    """
    coalesced = {}  # px(Decimal) -> summed sz(Decimal)
    for level in levels:
        px = level.px
        sz = level.sz
        if not _is_finite_decimal(px):
            raise ValueError(f"book_hash: non-Decimal/non-finite price {px!r}")
        if not _is_finite_decimal(sz):
            raise ValueError(f"book_hash: non-Decimal/non-finite size {sz!r}")
        if sz < 0:
            raise ValueError(f"book_hash: negative size {sz!r}")
        # C3/D1: requantize off-parse-path values through the parser's round-trip guard
        # so str() is stable AND a sub-quantum value fails loud (PrecisionLoss), never
        # silently collapses to 0.0000.
        px = _quantize_checked(px, PRICE_QUANTUM, field="book_hash.px")
        sz = _quantize_checked(sz, SIZE_QUANTUM, field="book_hash.sz")
        if sz == 0:
            continue  # drop empty/padding level
        coalesced[px] = coalesced.get(px, sz.__class__(0)) + sz
    ordered = sorted(coalesced.items(), key=lambda item: item[0], reverse=descending)
    ordered = ordered[:MAX_LEVELS]
    return [[str(px), str(sz)] for px, sz in ordered]


def _is_finite_decimal(value) -> bool:
    is_finite = getattr(value, "is_finite", None)
    if is_finite is None:
        return False  # not a Decimal (e.g. float/int) -> reject; serializer would too
    return bool(is_finite())


def canonical_book_payload(snapshot: "BookSnapshot") -> dict:
    """The EXACT dict fed to ``row_hash``.

    Exposed separately so a test can assert the pre-hash structure WITHOUT depending
    on the sha256 output (white-box determinism test).
    """
    return {
        "v": BOOK_HASH_VERSION,
        "symbol": snapshot.symbol,
        "instrument_id": snapshot.instrument_id,
        "bids": _canonical_side(snapshot.bids, descending=True),
        "asks": _canonical_side(snapshot.asks, descending=False),
    }


def book_hash(snapshot: "BookSnapshot") -> str:
    """Deterministic sha256 hex of the canonical L2 ladder (see module docstring)."""
    return row_hash(canonical_book_payload(snapshot))
