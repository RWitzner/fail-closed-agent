"""Per-symbol L2 depth ladder + trade tape (contract §C).

``EquityBookState`` is the ONLY input to ``book_hash`` (§D), so its determinism is
load-bearing: identical event sequences => identical state => identical book_hash.
Pure / no IO / no clock / no float.

mbp-10 is treated as a FULL snapshot: ``apply`` REPLACES the ladder
(replace-on-apply, frozen for offline fixtures). The documented delta-apply seam
(``_apply_delta``) is where a real HISTORICAL mbp-10 pull would switch to
incremental deltas (tier-2 task §P 2a) — it is intentionally unused offline.

A crossed/locked book (best_bid >= best_ask) is RECORDED and surfaced via
``snapshot().crossed`` — NEVER silently normalized; the recorder turns that flag
into a ``data_quality_alert`` (§G). Sizes/prices arrive already quantized from the
parser (§B), so this module does no requantization.
"""
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple

from recorder.event import MAX_DEPTH_LEVELS, DepthEvent, DepthLevel, QuoteEvent, TradeEvent


class BookStateError(Exception):
    """Crossed/locked book surfaced (not auto-corrected), or a ladder-invariant violation."""


@dataclass(frozen=True)
class BookSnapshot:
    symbol: str
    instrument_id: int
    bids: Tuple[DepthLevel, ...]   # canonical: sorted px DESC (best first)
    asks: Tuple[DepthLevel, ...]   # canonical: sorted px ASC (best first)
    crossed: bool                  # best_bid >= best_ask (recorded, NOT silently fixed)
    # BookSnapshot is the ONLY input to book_hash. Nothing time-varying is included.


def _best_first(levels, *, descending: bool) -> Tuple[DepthLevel, ...]:
    """Sort a side best-first (bids DESC, asks ASC). Price is the sole sort key.

    A stable sort keeps duplicate-price vendor splits in their relative order; the
    book_hash canonicalizer (§D) is what coalesces/drops — snapshot preserves the
    ladder as received (only ordered), so a crossed/padded book stays visible.
    """
    return tuple(sorted(levels, key=lambda lvl: lvl.px, reverse=descending))


class EquityBookState:
    """Per-symbol L2 depth ladder (<=10 levels/side). Decimal px/sz, int ct only."""

    def __init__(self, symbol: str, instrument_id: int) -> None:
        self.symbol = symbol
        self.instrument_id = instrument_id
        self._bids: Tuple[DepthLevel, ...] = ()
        self._asks: Tuple[DepthLevel, ...] = ()

    def _check_identity(self, prov) -> None:
        if prov.symbol != self.symbol:
            raise BookStateError(
                f"event symbol {prov.symbol!r} != book symbol {self.symbol!r}"
            )
        if prov.instrument_id != self.instrument_id:
            raise BookStateError(
                f"event instrument_id {prov.instrument_id} != book instrument_id {self.instrument_id}"
            )

    @staticmethod
    def _check_levels(levels, *, side: str) -> None:
        if len(levels) > MAX_DEPTH_LEVELS:
            raise BookStateError(
                f"{side} exceeds mbp-10 max {MAX_DEPTH_LEVELS} levels ({len(levels)})"
            )

    def apply(self, ev: "DepthEvent") -> None:
        """Replace the ladder from an mbp-10 snapshot (replace-on-apply).

        Documented delta-apply seam: a real HISTORICAL mbp-10 pull that proves
        incremental deltas would route through ``_apply_delta`` instead (tier-2,
        §C/§O/§P 2a). Offline fixtures honor replace-on-apply. A crossed book is
        recorded and surfaced via ``snapshot().crossed`` — NOT auto-corrected.
        """
        self._check_identity(ev.provenance)
        self._check_levels(ev.bids, side="bids")
        self._check_levels(ev.asks, side="asks")
        self._bids = _best_first(ev.bids, descending=True)
        self._asks = _best_first(ev.asks, descending=False)

    def _apply_delta(self, ev: "DepthEvent") -> None:  # pragma: no cover - tier-2 seam
        """Documented incremental-delta seam (§C). Unused offline (replace-on-apply
        is frozen for M1 fixtures); a HISTORICAL-verified pull (§P 2a) would wire it."""
        raise NotImplementedError("delta-apply lands in M1 tier-2 (HISTORICAL-verified, §P 2a)")

    def apply_quote(self, ev: "QuoteEvent") -> None:
        """Update the 1-level ladder from an L1 tbbo/bbo event."""
        self._check_identity(ev.provenance)
        self._bids = (DepthLevel(px=ev.bid_px, sz=ev.bid_sz, ct=0),)
        self._asks = (DepthLevel(px=ev.ask_px, sz=ev.ask_sz, ct=0),)

    def best_bid(self) -> Optional[Tuple[Decimal, Decimal]]:
        if not self._bids:
            return None
        best = self._bids[0]
        return (best.px, best.sz)

    def best_ask(self) -> Optional[Tuple[Decimal, Decimal]]:
        if not self._asks:
            return None
        best = self._asks[0]
        return (best.px, best.sz)

    def _crossed(self) -> bool:
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is None or ask is None:
            return False
        return bid[0] >= ask[0]

    def snapshot(self) -> BookSnapshot:
        """Return the canonical BookSnapshot — the exact input to book_hash (§D)."""
        return BookSnapshot(
            symbol=self.symbol,
            instrument_id=self.instrument_id,
            bids=self._bids,
            asks=self._asks,
            crossed=self._crossed(),
        )


class TradeTape:
    """Append-only ring of recent TradeEvent for the modeled-fill VWAP input (read-only here)."""

    def __init__(self, symbol: str, maxlen: int = 1024) -> None:
        self.symbol = symbol
        self._trades: "deque[TradeEvent]" = deque(maxlen=maxlen)

    def record(self, ev: "TradeEvent") -> None:
        self._trades.append(ev)

    def recent(self, n: int) -> Tuple["TradeEvent", ...]:
        """Newest-last, at most ``n`` most-recent trades."""
        if n <= 0:
            return ()
        trades = tuple(self._trades)
        return trades[-n:]

    def last(self) -> Optional["TradeEvent"]:
        if not self._trades:
            return None
        return self._trades[-1]
