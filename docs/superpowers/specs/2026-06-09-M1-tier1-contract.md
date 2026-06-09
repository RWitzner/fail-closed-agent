## M1 Tier-1 (Offline Data Tier) — FROZEN CONTRACT

**Status:** FROZEN (revised post-critique, 2026-06-09). HEAD 70115f8.

A build agent handed only this document + the repo can TDD any one component below without guessing an interface.

**Lens-merged from 3 proposals** (determinism / fail-closed / testability). Where they diverged, the most deterministic + replay-stable + hand-auditable choice won (see `divergences_resolved`, §O). This revision applies the 2026-06-09 critique (2 blockers + 4 majors + 3 minors) and the new Databento historical-only access constraint; nothing correct from the prior draft was dropped.

---

### 0. Scope, ground rules, repo facts (verified against HEAD `70115f8`)

**Offline-complete only.** No network, no credential reads. Every module here keeps `tests/agent/test_no_network_no_creds.py` green: after importing any M1 module, `"databento"` and `"alpaca"` MUST NOT be in `sys.modules`, and no `socket.socket` is constructed. The credentialed (tier-2) path exists only as a stub that raises `NotImplementedError` and is never reached by an offline test.

**Verified repo facts (read at HEAD `70115f8`, M0 harden round 2, 129 tests green):**
- `scripts/recorder/` does **not** exist yet — this is from-scratch. You MUST create `scripts/recorder/__init__.py` (empty) and `tests/recorder/__init__.py` (empty) so `recorder.*` and `tests.recorder.*` import. (`conftest.py` and `tests/lib/__init__.py` already exist.)
- Import root: `tests/__init__.py:11-14` and repo-root `conftest.py` prepend `<repo>/scripts` to `sys.path`. So `scripts/recorder/event.py` imports as `from recorder.event import ...` and `scripts/agent/...` as `from agent... import ...`. Run tests with `python3 -m unittest discover -s tests -p 'test_*.py' -t .` (the `-t .` is mandatory).
- The M0 transport seam (`scripts/agent/marketdata/base.py:11-15`) is `@runtime_checkable` Protocol `MarketDataTransport` (decorator at `:11`, class at `:12`) with a single member: `def stream(self, symbols) -> AsyncIterator[bytes]` (`:13`). **`FakeTransport` (`tests/lib/fakes.py:9-18`) implements `stream` as an `async def` generator** (`async def stream(self, symbols):` at `:15`, then `yield message`) and the contract test (`tests/agent/test_marketdata_transport.py:9-13`) consumes it with `async for msg in transport.stream(symbols)`. **`isinstance(x, MarketDataTransport)` is satisfied by structural presence of a `stream` attribute** (`test_marketdata_transport.py:17`) — `FakeTransport` is NOT a Protocol subclass. ⇒ `DatabentoTransport.stream` MUST be an `async def` generator (or return an async iterator); declaring it `async def ... yield` is the canonical mirror.
- Serializer (`scripts/agent/serializer.py`): `dumps(row)` (`:47-50`) does `json.dumps(row, sort_keys=True, separators=(",",":"), default=_default)` after calling `_reject_floats(row)`; `_reject_floats` (`:27-35`) recursively raises `ValueError("float not allowed...")` on ANY `float` in dict keys/values/list/tuple elements; `_default` (`:39-44`) stringifies a finite `Decimal` (`:40-43`) and raises `ValueError` on a non-finite Decimal (`:42`), and raises `TypeError` for any other non-JSON type (`:44`). `row_hash(row)` (`:53-55`) = `sha256(dumps(row).encode("utf-8")).hexdigest()`. These are the ONLY hashing/serialization primitives M1 may use — do not re-implement.
- Journal (`scripts/agent/journal.py`): `JournalWriter(path, run_id, clock=_utc_now_iso)` (`:91`); `.append(event_type, fields=None, *, decision_id=None, order_id=None) -> dict` (`:110`). Per-stream lock + monotonic `seq` are keyed by **resolved path** in a module-global registry (`_streams` `:70`, `_state_for` `:74`), so two writers on the same file share one seq. `_RESERVED = {"event_type","run_id","seq","hash","decision_id","order_id","ts_utc"}` (`:21`) — a field colliding with these raises `ValueError` (`:113-114`). On construction it repairs a truncated tail (`_repair_truncated_tail`, `:99`). The actual write is one `fh.write(dumps(row) + "\n")` per row (`:128`). `replay(path)` (`:28`) hash-verifies every row, drops ONLY a non-newline-terminated trailing line (crash mid-write, `:55-56`), and raises `JournalCorruption` (`:24`, `:57`) on (a) a newline-terminated corrupt/unparseable line, (b) a hash mismatch (`:48-49`), (c) a missing `hash` key (`:45-46`). Tampered-hash and corrupt-complete-trailing-line-fatal are tested at `tests/agent/test_journal_replay.py:98,157`.
- **`seq` collision is real and verified empirically:** a top-level vendor field named `seq` intersects `_RESERVED` (`set(quote_fields) & _RESERVED == {'seq'}`), so `JournalWriter.append` raises `ValueError` at `journal.py:114`. **The vendor sequence is therefore named `vendor_seq` EVERYWHERE in M1** (Provenance, to_row/from_row, write_event, every fixture, replay keys, replay_expected_hashes). The journal-owned monotonic `seq` (stamped at `journal.py:120`) and the vendor `vendor_seq` are distinct columns that never collide. See blocker 1 resolution in §B and §E.
- `config/data_retention.json` = `{"paths":{"journal":"journal","data":"data","bars":"data/bars"},"retention_days":30,"rotate":"daily"}`. **Daily rotation is OUT of M1-offline scope** (see minor 9 resolution, §E): M1 fixtures + tests are SINGLE-FILE; daily rotation / cross-file concatenation lands in a later milestone. The per-file writer stays byte-identical to `JournalWriter`.
- `scripts/agent/broker/alpaca.py:1-9` is the precedent for "SDK never imported at module scope" — it imports `from agent.broker.base import BrokerBase` and `from decimal import Decimal` only, never `alpaca-py`; M1's `databento.py` mirrors that discipline.

**Determinism contract reused, not reinvented (this is load-bearing):** every persisted row, every hash, every VWAP flows through `agent.serializer.dumps`/`row_hash`. There is exactly ONE canonicalization path in the repo; M1 adds none.

---

### A. `scripts/agent/marketdata/databento.py` — `MarketDataTransport` impl behind the M0 seam

Implements the exact `MarketDataTransport` Protocol. Offline-testable via an **injected** raw source; the real `databento` SDK is imported **lazily, only inside the credentialed build path**, which no offline test reaches.

```python
# scripts/agent/marketdata/databento.py
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional, Sequence

@dataclass(frozen=True)
class DatabentoConfig:
    dataset: str                 # exact pinned code, e.g. "EQUS.MINI" (placeholder OK offline)
    schema: str                  # exactly ONE schema per subscription (spec §5.1: no mixed query)
    symbols: tuple               # tuple[str], order-preserved as given
    heartbeat_timeout_ms: int = 30000
    backoff_base_ms: int = 250
    backoff_cap_ms: int = 30000          # always-on reconnect backoff CAP (spec §5 tier1)
    disconnect_alert_ms: int = 60000     # prolonged-disconnect threshold -> data_quality_alert

class DatabentoAuthError(RuntimeError):
    """Credential / entitlement failure. LIVE path only — never raised offline."""

class DatabentoTransport:
    """MarketDataTransport for Databento. OFFLINE: driven by an injected `raw_source`
    (a callable taking symbols and returning an async iterator of raw bytes frames).
    The real SDK is imported LAZILY only when `raw_source is None` (credentialed path),
    so importing this module and streaming offline never pulls `databento` into
    sys.modules (keeps test_no_network_no_creds green). Mirrors agent/broker/alpaca.py
    which imports no SDK at module scope."""

    def __init__(
        self,
        config: DatabentoConfig,
        *,
        raw_source: Optional[Callable[[Sequence[str]], "AsyncIterator[bytes]"]] = None,
        credentials_loader: Optional[Callable[[], str]] = None,  # reads .secrets/ in LIVE path ONLY
    ) -> None: ...

    async def stream(self, symbols) -> AsyncIterator[bytes]:
        """ASYNC GENERATOR (mirrors tests/lib/fakes.py:15 FakeTransport.stream). Yields raw
        vendor frames (bytes) for the recorder to parse — NO parsing here, NO timestamping,
        NO ordering change (pure pass-through offline).
        - `symbols` is validated to be a subset of `config.symbols`; an unsubscribed symbol
          raises ValueError BEFORE any source access (fail-closed, no silent widening).
        - If `raw_source` is set: delegate to it (offline) — zero SDK import, zero socket.
        - Else: lazily `import databento`, build a real client from credentials_loader()/.secrets/.
          This branch is the tier-2 STUB and MUST raise NotImplementedError offline (never reached)."""
        ...

    def _build_real_client(self):
        """Tier-2 stub: lazily `import databento` and read `.secrets/`. In M1 offline scope this
        raises NotImplementedError('credentialed Databento client lands in M1 tier-2'). Per the
        2026-06-09 access constraint, the entitled key (`.secrets/databento.json`) is HISTORICAL
        only; a live realtime client is a deferred paid subscription (see §K, §P)."""
        ...
```
**Raises:** `ValueError` (symbol not in subscription, empty symbols); `NotImplementedError` (`_build_real_client` offline); `DatabentoAuthError` (live path only). No exceptions on the happy offline path.

**Note on reconnect placement:** the reconnect/backoff loop lives in `recorder.py` (§F), NOT in `stream`. `stream` offline is a pure pass-through; the recorder owns retry, epoch bumping, and alerting. (This avoids two reconnect implementations.)

---

### B. `scripts/recorder/event.py` — Databento schema → typed parsed event (S2 born-from-vendor seam; S6 contributor)

Pure parser: one raw frame (`bytes` of one JSON object offline, or an already-decoded `dict`) → a frozen typed event. No I/O, no clock, no float. Every vendor price/size is converted via `Decimal(str(x))` (NEVER `float()`), then `.is_finite()`-checked. A float that is NaN/Inf, a `None` in a required slot, or a missing/extra-malformed field raises — never silently coerced (spec §14 fail-closed).

```python
# scripts/recorder/event.py
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Dict, Optional, Tuple, Type

PRICE_SCALE = Decimal("1e-9")        # Databento fixed-point: int * 1e-9 USD (tier-2 re-verify; offline fixtures use strings)
PRICE_QUANTUM = Decimal("0.0001")    # 4dp Reg-NMS sub-penny tick floor; quantize at parse so str(Decimal) is canonical
SIZE_QUANTUM = Decimal("1")          # MAJOR 3: sizes are whole shares; quantize to integer so str(Decimal) is canonical

class UnknownSchema(ValueError): ...        # schema not in SCHEMA_REGISTRY -> explicit reject (no silent fallback)
class MalformedRecord(ValueError): ...      # missing/extra-typed required field -> FATAL (fail-closed)
class NonFinitePrice(ValueError): ...       # NaN/Inf/None where a finite Decimal is required (S2)
class PrecisionLoss(MalformedRecord): ...   # MAJOR 4: a price/size that does NOT round-trip through its quantum -> FATAL

@dataclass(frozen=True)
class Provenance:
    dataset: str
    schema: str                   # the typed schema tag; dispatch key (also keys SCHEMA_REGISTRY)
    instrument_id: int            # from definitions; symbol identity (replaces token_ids)
    symbol: str
    vendor_seq: Optional[int]     # BLOCKER 1: vendor sequence (renamed from `seq`; `seq` collides with journal _RESERVED).
                                  #            None where the dataset has no meaningful per-venue seq.
    ts_event_utc: str             # ISO-8601 UTC, derived from vendor ts_event (spec §11 persist-UTC)
    ts_recv_utc: str              # recorder receipt stamp, ISO-8601 UTC
    reconnect_epoch: int          # stamped by recorder, 0 at first connect, bumped per reconnect (S4 input)

@dataclass(frozen=True)
class QuoteEvent:                # tbbo / bbo-1s / bbo-1m / mbp-1 (L1 top-of-book NBBO; tbbo is the primary NBBO source)
    provenance: Provenance
    bid_px: Decimal; bid_sz: Decimal
    ask_px: Decimal; ask_sz: Decimal

@dataclass(frozen=True)
class TradeEvent:               # trades
    provenance: Provenance
    price: Decimal; size: Decimal
    side: str                    # "A"|"B"|"N" (vendor aggressor flag; "N"=none/unknown)

@dataclass(frozen=True)
class BarEvent:                 # ohlcv-1s / ohlcv-1m (vendor-emitted bars; distinct from resampled Bar §J)
    provenance: Provenance
    open: Decimal; high: Decimal; low: Decimal; close: Decimal; volume: Decimal

@dataclass(frozen=True)
class DepthLevel:
    px: Decimal                  # quantized to PRICE_QUANTUM, ROUND_HALF_EVEN, AT PARSE (round-trip checked)
    sz: Decimal                  # quantized to SIZE_QUANTUM (whole shares), >= 0, round-trip checked
    ct: int                      # order count at level; 0 if unsupported. PARSED+CARRIED, but EXCLUDED from book_hash (§D)

@dataclass(frozen=True)
class DepthEvent:               # mbp-10 (L2, <=10 levels each side; treated as a FULL snapshot — replace-on-apply)
    provenance: Provenance
    bids: Tuple[DepthLevel, ...]  # length<=10, vendor order, index 0 = best/inside
    asks: Tuple[DepthLevel, ...]

@dataclass(frozen=True)
class DefinitionEvent:          # definitions (instrument_id <-> symbol/MIC identity)
    provenance: Provenance       # carries dataset/schema/instrument_id/symbol/vendor_seq for uniform dispatch
    mic: str; raw_symbol: str

# --- MAJOR 6: frozen schema -> Event dispatch registry (data, not prose) -------------------------
# Dispatch is: parse() reads the vendor `schema` string, looks it up in SCHEMA_REGISTRY to get the
# target dataclass, builds it, and stamps Provenance.schema with the SAME string. Downstream code
# (recorder, replay) dispatches via `isinstance(ev, <EventType>)`; the typed `provenance.schema`
# string is the secondary discriminator. Anything not a key here -> UnknownSchema (no silent skip).
SCHEMA_REGISTRY: Dict[str, Type] = {
    "tbbo":        QuoteEvent,   # PRIMARY NBBO (frozen decision; matches M1 plan "tbbo (primary NBBO)")
    "bbo-1s":      QuoteEvent,
    "bbo-1m":      QuoteEvent,
    "mbp-1":       QuoteEvent,
    "trades":      TradeEvent,
    "ohlcv-1s":    BarEvent,
    "ohlcv-1m":    BarEvent,
    "mbp-10":      DepthEvent,
    "definitions": DefinitionEvent,
}

def parse(record, *, dataset: str, schema: str, reconnect_epoch: int, ts_recv_utc: str) -> object:
    """Dispatch on `schema` via SCHEMA_REGISTRY -> one of the *Event dataclasses above.
    - schema not in SCHEMA_REGISTRY -> UnknownSchema (no silent reinterpretation).
    - missing/invalid required field -> MalformedRecord.
    - a float/NaN/Inf or None in a price/size slot -> NonFinitePrice.
    - a price/size that does NOT round-trip through its quantum (e.g. a real sub-$1 sub-penny
      price like '0.00005' that would quantize to '0.0000') -> PrecisionLoss (MAJOR 4 fail-loud).
    - >10 levels on a depth side -> MalformedRecord (mbp-10 invariant).
    Prices converted via Decimal(str(x)) then quantized to PRICE_QUANTUM (ROUND_HALF_EVEN);
    sizes converted via Decimal(str(x)) then quantized to SIZE_QUANTUM, so str(Decimal) is
    canonical by construction for BOTH. NEVER returns a float anywhere in its output."""
    ...

def _quantize_checked(raw, quantum: Decimal, *, field: str) -> Decimal:
    """MAJOR 3 + MAJOR 4 single chokepoint. d = Decimal(str(raw)); reject non-finite (NonFinitePrice);
    q = d.quantize(quantum, ROUND_HALF_EVEN); if q != d -> raise PrecisionLoss(field, raw) — the quantize
    silently changed the value (e.g. sub-$1 sub-penny price zeroed, or a fractional share where whole
    shares are required). On success returns the canonical quantized Decimal whose str() is stable
    (e.g. '300.0' and '300' both -> Decimal('300') -> '300'; '1.5' and '1.50' both -> '1.5000')."""
    ...
```
**Raises:** `UnknownSchema`, `MalformedRecord`, `NonFinitePrice`, `PrecisionLoss`.

**BLOCKER 1 (vendor_seq):** `Provenance.vendor_seq` is the renamed vendor sequence. The journal's own monotonic `seq` (`journal.py:120`) is reserved (`journal.py:21`); a top-level `seq` field raises `ValueError` at `journal.py:114`. Verified: `set({...,'seq',...}) & _RESERVED == {'seq'}`; after rename the intersection is empty.

**MAJOR 4 (sub-$1 fail-loud):** M1 scope EXCLUDES sub-$1 symbols. `_quantize_checked` RAISES `PrecisionLoss` (a `MalformedRecord` subtype) when a price quantizes to a value it would not round-trip to — so a genuine sub-penny sub-$1 price is never silently rewritten to `0.0000`. Fixture row 8 (§L) asserts this raises. (Matches §14: data corruption fails loud, never silent.)

**MAJOR 3 (size canonicalization):** sizes pass through the SAME `_quantize_checked` with `SIZE_QUANTUM = Decimal("1")` (whole shares). This makes `str(Decimal)` canonical for sizes too (`'300.0'`→`Decimal('300')`→`'300'`), closing the `'300.0' != '300'` gap that prices' quantization did not. A fractional share (`'1.5'` size) does not round-trip to `SIZE_QUANTUM` and raises `PrecisionLoss`. Fixture row in §L.3 carries a `300.0`-style size proving canonicalization; the rule is `str(Decimal(raw).quantize(SIZE_QUANTUM))`, NOT `.normalize()` (which would render `300` as `'3E+2'` — verified — and is therefore rejected).

**MAJOR 6 (registry):** `SCHEMA_REGISTRY` is the single frozen `{schema_str: EventType}` map; dispatch is `isinstance` on the dataclass plus the typed `provenance.schema` string. Two build agents cannot enumerate divergent schema sets.

**S6 binding:** every parsed event carries `provenance.instrument_id`+`symbol`+`vendor_seq`+`reconnect_epoch` so `test_databento_event_parser.py` asserts correlation metadata is present (S6 contributor; S6 itself owned by M0 journal tests). **S2 binding:** the `Decimal(str(x))` + quantize-checked + `.is_finite()` conversion is the M1 "re-verified where floats are born" point the spec §9 S2 row demands.

---

### B2. `to_row(event) -> dict` / `from_row(row) -> Event` — the flat persistence ↔ replay seam (BLOCKER 2)

`*Event` dataclasses are NESTED (a `Provenance` sub-object + Decimal/tuple payloads), but `JournalWriter.append` (`journal.py:110`) takes a FLAT dict. S3 requires the replay-re-derived `book_hash` to byte-match record-time, which is ONLY possible if persistence and replay agree on the EXACT flat field names. This section is that single source of truth — `persistence.write_event` (§E) and `replay.py` (§H) both bind to it; neither invents its own shape.

**Module:** `scripts/recorder/event_row.py` (imported `from recorder.event_row import to_row, from_row`).

**Common provenance prefix** flattened onto EVERY row (no `seq`/`hash`/`ts_utc`/`run_id` keys — those are journal-owned and reserved, `journal.py:21`):

| Flat field | Source | Type-in-row |
|---|---|---|
| `schema` | `provenance.schema` | str |
| `dataset` | `provenance.dataset` | str |
| `instrument_id` | `provenance.instrument_id` | int |
| `symbol` | `provenance.symbol` | str |
| `vendor_seq` | `provenance.vendor_seq` | int or null (BLOCKER 1 rename) |
| `ts_event_utc` | `provenance.ts_event_utc` | str (UTC ISO-8601) |
| `ts_recv_utc` | `provenance.ts_recv_utc` | str (UTC ISO-8601) |
| `reconnect_epoch` | `provenance.reconnect_epoch` | int |

**Per-event-type payload fields** (appended to the prefix; Decimals are passed AS Decimal — the serializer renders them as strings at `serializer.py:40-43`; nested lists are lists-of-strings/ints):

- **QuoteEvent** (`schema` ∈ {tbbo,bbo-1s,bbo-1m,mbp-1}): `bid_px`(Dec) `bid_sz`(Dec) `ask_px`(Dec) `ask_sz`(Dec).
- **TradeEvent** (`schema`=trades): `price`(Dec) `size`(Dec) `side`(str).
- **BarEvent** (`schema` ∈ {ohlcv-1s,ohlcv-1m}): `open`(Dec) `high`(Dec) `low`(Dec) `close`(Dec) `volume`(Dec).
- **DepthEvent** (`schema`=mbp-10): `bids`(list of `[px_str, sz_str, ct_int]`) `asks`(same) `derived_book_hash`(str or null). The level encoding `[px, sz, ct]` is EXACTLY the fixture encoding (§L.3); `from_row` rebuilds each `DepthLevel(px=Decimal(px_str), sz=Decimal(sz_str), ct=ct_int)` and `to_row` renders each level as `[str(level.px), str(level.sz), level.ct]`. `derived_book_hash` is computed by the writer (§E), persisted on the row, and re-checked on replay.
- **DefinitionEvent** (`schema`=definitions): `mic`(str) `raw_symbol`(str).

```python
# scripts/recorder/event_row.py
from typing import Optional
from recorder.event import (Provenance, QuoteEvent, TradeEvent, BarEvent,
                            DepthEvent, DepthLevel, DefinitionEvent, SCHEMA_REGISTRY)

def to_row(event, *, derived_book_hash: Optional[str] = None) -> dict:
    """Flatten ANY *Event into the flat dict above. Decimals stay Decimal (serializer renders).
    No journal-reserved key (seq/hash/run_id/decision_id/order_id/ts_utc/event_type) is produced.
    For a DepthEvent, derived_book_hash is included (None if not yet computed)."""
    ...

def from_row(row: dict) -> object:
    """Inverse: rebuild the exact *Event from a flat persisted/replayed row. schema -> SCHEMA_REGISTRY
    selects the dataclass; Decimal fields rebuilt via Decimal(str_value); depth levels rebuilt from
    [px_str, sz_str, ct_int]. The rebuilt DepthEvent feeds EquityBookState (§C) so replay re-derives
    book_hash from the SAME field names persistence wrote. Unknown schema -> UnknownSchema."""
    ...
```
**Raises:** `UnknownSchema` (`from_row` on an unregistered schema), `MalformedRecord` (missing flat field). **BLOCKER 2 binding:** `to_row`/`from_row` are the SINGLE flat-shape source; `replay.py` rebuilds `DepthEvent`→`BookSnapshot` from exactly these fields, so the re-derived `book_hash` byte-matches record-time (S3). A round-trip test (`test_row_roundtrip_is_identity`) asserts `from_row(to_row(ev)) == ev` for every event type.

---

### C. `scripts/recorder/book_state.py` — `EquityBookState` (L2 ladder) + `TradeTape`

```python
# scripts/recorder/book_state.py
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple

class BookStateError(Exception):
    """Crossed/locked book surfaced (not auto-corrected), or a ladder-invariant violation."""

@dataclass(frozen=True)
class BookSnapshot:
    symbol: str
    instrument_id: int
    bids: Tuple["DepthLevel", ...]   # canonical: sorted px DESC (best first), zero-size already-canonicalized at hash time
    asks: Tuple["DepthLevel", ...]   # canonical: sorted px ASC (best first)
    crossed: bool                    # best_bid >= best_ask (recorded, NOT silently fixed -> recorder data_quality_alert)
    # NOTE: BookSnapshot is the ONLY input to book_hash. Nothing time-varying (ts/vendor_seq/epoch) is included.

class EquityBookState:
    """Per-symbol L2 depth ladder (<=10 levels/side). Decimal prices, Decimal sizes, int counts only.
    Pure / no IO / no clock. Deterministic: identical event sequences => identical state => identical book_hash.
    mbp-10 is treated as a FULL snapshot: apply REPLACES the ladder (replace-on-apply, frozen for offline
    fixtures). [Documented seam: if a real HISTORICAL mbp-10 pull proves incremental deltas, apply() switches
    to delta-apply — this is a HISTORICAL-verified tier-2 task (§P 2a), verifiable NOW with the entitled key,
    NOT a live-blocked unknown.]"""

    def __init__(self, symbol: str, instrument_id: int) -> None: ...
    def apply(self, ev: "DepthEvent") -> None:
        """Replace the ladder from an mbp-10 snapshot (replace-on-apply; documented delta-apply seam above).
        A crossed book (best_bid >= best_ask) is recorded and surfaced via snapshot().crossed (S4 input) —
        NOT auto-corrected."""
        ...
    def apply_quote(self, ev: "QuoteEvent") -> None:
        """Update the 1-level ladder from an L1 tbbo/bbo event."""
        ...
    def best_bid(self) -> Optional[Tuple[Decimal, Decimal]]: ...   # (px, sz)
    def best_ask(self) -> Optional[Tuple[Decimal, Decimal]]: ...
    def snapshot(self) -> BookSnapshot:
        """Return the canonical BookSnapshot — the exact input to book_hash (§D)."""
        ...

class TradeTape:
    """Append-only ring of recent TradeEvent for the modeled-fill VWAP input (read-only here)."""
    def __init__(self, symbol: str, maxlen: int = 1024) -> None: ...
    def record(self, ev: "TradeEvent") -> None: ...
    def recent(self, n: int) -> Tuple["TradeEvent", ...]: ...
    def last(self) -> Optional["TradeEvent"]: ...
```
**Raises:** `BookStateError`. **Fail-closed:** a crossed/locked book is never silently normalized; `snapshot().crossed` is the flag the recorder turns into a `data_quality_alert`.

---

### D. `scripts/recorder/book_hash.py` — L2 MBP-10 canonical depth-ladder hash (THE correctness centerpiece)

This is the hash `replay.py` re-derives and `test_replay_hashes.py` pins against `replay_expected_hashes.json`. It MUST be byte-stable across runs, machines, Python builds, and dict/vendor-insertion order. It reuses `agent.serializer.row_hash` so there is exactly ONE hashing convention in the repo.

```python
# scripts/recorder/book_hash.py
from decimal import Decimal
from typing import List, Sequence, Tuple
from agent.serializer import row_hash          # reuse the ONE M0 hashing primitive (serializer.py:53)

MAX_LEVELS = 10
BOOK_HASH_VERSION = 2                            # 2 = L2 MBP-10 ladder; isolates from any future L3/MBO hash

def _canonical_side(levels: Sequence["DepthLevel"], *, descending: bool) -> List[list]:
    """Normalize one side to a deterministic list of [px_str, sz_str]:
      1. Drop levels with sz == 0 (an empty level == no level; vendor padding/omission collapse to one form).
      2. Coalesce duplicate prices by SUMMING sizes (vendor may split one price across rows).
      3. Sort by price: bids DESCENDING, asks ASCENDING. Price is the SOLE sort key (ties impossible post-coalesce).
      4. Truncate to MAX_LEVELS.
      5. Render px and sz as str(Decimal). Prices are ALREADY quantized to PRICE_QUANTUM and sizes to
         SIZE_QUANTUM at parse (§B), so str(Decimal('1.5000')) and str(Decimal('300')) are canonical — the
         '1.5'/'1.50' AND '300.0'/'300' gaps are both closed structurally (MAJOR 3 covered here too).
    ct (order count) is NOT included. Raises if any px/sz is not a finite Decimal or sz < 0.
    Returns [[px_str, sz_str], ...]."""
    ...

def canonical_book_payload(snapshot: "BookSnapshot") -> dict:
    """The EXACT dict fed to row_hash. Exposed separately so a test can assert the pre-hash structure
    WITHOUT depending on the sha256 output (white-box determinism test)."""
    return {
        "v": BOOK_HASH_VERSION,
        "symbol": snapshot.symbol,
        "instrument_id": snapshot.instrument_id,
        "bids": _canonical_side(snapshot.bids, descending=True),
        "asks": _canonical_side(snapshot.asks, descending=False),
    }

def book_hash(snapshot: "BookSnapshot") -> str:
    """Deterministic sha256 hex of the canonical L2 ladder. Identity (symbol+instrument_id) is INCLUDED so two
    symbols with an identical ladder hash differently. ALL provenance (ts_event/ts_recv, vendor_seq,
    reconnect_epoch, per-level ct, venue, flags) is EXCLUDED, so the SAME physical book re-derives the SAME hash
    on replay, on a different host, and AFTER a reconnect (new epoch/ts, unchanged book) — exactly the
    replay-stability S3 asserts."""
    return row_hash(canonical_book_payload(snapshot))
```

**Why this is deterministic & replay-stable (frozen rationale):**
- **Decimal-as-string only; float entry impossible.** The payload contains only `str`, `int`, and nested lists of those. `row_hash`→`dumps`→`_reject_floats`/`_default` RAISES on any float (`serializer.py:27-29`) or non-finite Decimal (`serializer.py:42`), so a machine-dependent float repr can NEVER enter the hash — it fails loud instead of hashing silently.
- **Ordering is intrinsic to price, not insertion.** `_canonical_side` sorts by price; `dumps(sort_keys=True)` (`serializer.py:50`) sorts dict keys. Vendor row order, Python dict-insertion order, and hash-seed randomization cannot change the bytes.
- **Quantization-at-parse closes `1.5` vs `1.50` (prices) AND `300.0` vs `300` (sizes).** Every price is quantized to `PRICE_QUANTUM` and every size to `SIZE_QUANTUM` (ROUND_HALF_EVEN) in `event.parse` BEFORE it ever reaches a snapshot, so `str(Decimal)` is canonical by construction for both. This is structural (the parser owns it), not a downstream discipline. (MAJOR 3: the earlier "sizes are integers, same render path" claim was false and is now fixed by `SIZE_QUANTUM` quantization with a round-trip check.)
- **Zero-size + duplicate-price normalization** collapses two vendor encodings of the same economic book to one hash, eliminating a class of false replay mismatches.
- **Provenance + ct excluded** ⇒ cross-machine/cross-time/post-reconnect replay re-derives identical bytes.
- **Versioned (`"v":2`)** isolates the L2 rewrite from a future L3/MBO hash so versions cannot silently collide.

**Raises:** `ValueError`/`TypeError` propagated from `dumps` on a float/non-finite Decimal (intentional fail-loud).

---

### E. `scripts/recorder/persistence.py` — append-only JSONL writer (WRAPS `agent.journal.JournalWriter`)

`persistence.py` **WRAPS/REUSES `agent.journal.JournalWriter`** — it does NOT re-implement the lock/seq/hash/tail loop. This is a frozen decision: two journal implementations would drift on partial-write/tamper semantics and silently break S3/S6. **MINOR 9 (rotation): daily rotation is OUT of M1-offline scope.** M1 fixtures + tests are SINGLE-FILE; the per-file writer is byte-identical to M0; daily rotation / cross-file concatenation lands in a later milestone (see the §M table and §P).

```python
# scripts/recorder/persistence.py
from typing import Optional
from agent.journal import JournalWriter, replay as journal_replay, JournalCorruption
from recorder.event_row import to_row

# Recorder stream names (one file per stream; M1 = single file per stream, NO rotation):
STREAM_EVENTS = "events"                      # parsed market events (+ derived book_hash on depth rows)
STREAM_DATA_QUALITY = "data_quality_alerts"   # gap / reconnect / heartbeat / crossed-book alerts
STREAM_STATUS = "status"                      # heartbeat / connection-state transitions

class EventWriter:
    """Thin recorder facade over JournalWriter. One EventWriter per resolved stream path.
    Inherits VERBATIM: single-writer lock per resolved path (journal.py:74,115), per-stream monotonic
    journal seq (journal.py:116,120), one dumps(row)+'\\n' write per row (journal.py:128), row hash
    (journal.py:126), sort_keys (serializer.py:50), truncated-tail repair on open (journal.py:99),
    JournalCorruption on a complete corrupt line (journal.py:57), _RESERVED-key collision guard
    (journal.py:113-114). Decimal fields are passed as Decimal; the serializer renders them as strings.

    run_id: the recorder SHARES the injected orchestrator/agent run_id namespace (it does NOT mint its
    own), so recorder events + alerts correlate cross-stream with the rest of the agent for S6."""
    def __init__(self, path, run_id: str, *, clock=None) -> None: ...
    def write_event(self, event, *, decision_id: Optional[str] = None,
                    order_id: Optional[str] = None) -> dict:
        """Append one hash-stamped recorder row built by recorder.event_row.to_row(event), i.e. the FLAT
        field set defined in §B2: schema, dataset, instrument_id, symbol, vendor_seq, ts_event_utc,
        ts_recv_utc, reconnect_epoch, plus the per-type payload, plus (for a DepthEvent) derived_book_hash
        = book_hash(EquityBookState.apply(event).snapshot()) computed by the recorder and passed through.
        reconnect_epoch is carried INSIDE the flat row (from provenance) — it is NOT a journal kwarg.
        Returns the persisted row (incl. journal seq + hash). No flat field collides with _RESERVED
        (verified: vendor_seq, not seq) -> never raises at journal.py:114."""
        ...
    def record(self, event_type: str, fields: dict, *,
               decision_id: Optional[str] = None, order_id: Optional[str] = None) -> dict:
        """Lower-level escape hatch for alert/status rows; same contract as JournalWriter.append."""
        ...

def replay_stream(path) -> list:
    """Re-read a recorder stream hash-verified (delegates to agent.journal.replay so the truncated-tail
    rule and JournalCorruption semantics are the SAME code path as M0). Single-file in M1."""
    return journal_replay(path)
```
**Raises:** `agent.journal.JournalCorruption` (inherited), `ValueError` (reserved-key collision, inherited from `journal.py:114` — cannot fire for the `to_row` field set, which uses `vendor_seq` not `seq`). **BLOCKER 1+2 binding:** `write_event` persists EXACTLY the `to_row` flat shape (§B2) using `vendor_seq`; `replay.py` rebuilds via `from_row`. **S3 binding:** because persistence delegates to the M0 journal, truncated-tail and corrupt-line semantics are exactly those tested at `tests/agent/test_journal_replay.py:77,87,157`.

---

### F. `scripts/recorder/recorder.py` — single-writer ingest loop + always-on reconnect

```python
# scripts/recorder/recorder.py
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class BackoffPolicy:
    base_ms: int = 250
    factor: int = 2
    cap_ms: int = 30000          # backoff DELAY cap (spec §5: unbounded ATTEMPTS, capped delay)
    alert_after_ms: int = 60000  # sustained-disconnect threshold -> data_quality_alert
    # DELIBERATELY no `max_attempts` field: the ported 5-attempt cap is structurally absent (spec §5 tier1 "new").

@dataclass(frozen=True)
class RecorderStats:
    events_written: int
    alerts_emitted: int
    reconnects: int
    final_book_hashes: dict      # {symbol -> book_hash}

class Recorder:
    """Drives transport.stream(symbols) -> event.parse -> EquityBookState.apply -> EventWriter.write_event,
    plus sequence/heartbeat checks. Single writer per stream (inherited from persistence/journal).
    Deterministic offline when fed a Fake/Flaky transport + injected clock + injected sleep (no real sleep).

    Reconnect: UNBOUNDED attempts, exponential backoff = min(cap_ms, base_ms*factor**n). On a disconnect lasting
    > alert_after_ms, emit a `data_quality_alert` ROW (NOT a silent exit). Each reconnect increments
    reconnect_epoch, stamped on every subsequent parsed event (S4 input). A detected sequence gap emits a
    `data_quality_alert` row and keeps running (gap is logged, not fatal).

    Reconnect is driven by the transport: a FlakyTransport (§F2, in tests/lib/fakes.py) signals a disconnect by
    raising TransportDisconnected; the recorder catches it, bumps reconnect_epoch, sleeps the (injected) backoff,
    re-calls transport.stream, and emits a prolonged_disconnect alert if the gap exceeded alert_after_ms."""

    def __init__(
        self,
        transport,                       # MarketDataTransport (injected Fake/Flaky offline)
        writer: "EventWriter",
        *,
        dataset: str,
        schema: str,
        symbols,
        clock,                           # injected; FakeClock offline (no real wall clock)
        sleep,                           # injected async sleep; no-op/recorded offline (no real sleep)
        backoff: BackoffPolicy = BackoffPolicy(),
        sequence_tracker: "SequenceTracker",
        heartbeat: "HeartbeatMonitor",
    ) -> None: ...

    async def run(self, max_events: Optional[int] = None) -> RecorderStats:
        """Main loop. Returns only on explicit stop or when max_events is reached (so tests terminate
        deterministically without a real EOS); a disconnect reconnects (never exits silently)."""
        ...

    @property
    def reconnect_epoch(self) -> int: ...
```
**Raises:** nothing on the offline happy path; `data_quality_alert` rows are DATA, not exceptions. An injected unrecoverable transport surfaces as a `data_quality_alert` then retries — never a silent exit. **Non-blocking-alert note:** alert emission must not stall the reconnect loop on a slow disk; the writer call is the same single-writer-locked path as M0 and is treated as fast/bounded.

---

### F2. `FlakyTransport` + `TransportDisconnected` — fault-injection transport (MAJOR 5)

Lives in `tests/lib/fakes.py` **alongside `FakeTransport`** (which it mirrors structurally: an `async def stream` generator, `fakes.py:15`). Two test files depend on it — `test_reconnect_alert.py` and `test_sequence_gap_detection.py` — so its interface is frozen here, not invented per-test.

```python
# tests/lib/fakes.py  (ADD; FakeTransport + FakeClock unchanged at fakes.py:9,20)

class TransportDisconnected(Exception):
    """Raised by FlakyTransport.stream to signal a (recoverable) disconnect to the recorder loop.
    The recorder CATCHES this, increments reconnect_epoch, sleeps the injected backoff, and re-calls
    stream() (a fresh async generator from the next segment). It is NEVER a fatal/silent exit."""

class FlakyTransport:
    """A MarketDataTransport that replays scripted frames and injects faults via inline _control rows.
    Mirrors FakeTransport (async def stream + yield). The PARSER never sees a _control row — FlakyTransport
    consumes/strips them; the recorder never parses them.

    frames: an ordered list of dicts. Two kinds:
      - DATA frame: a normal vendor record dict (yielded, after json-bytes-encoding, to the recorder).
      - CONTROL frame: {'_control': <verb>, ...}. Honored verbs (frozen):
          {'_control':'disconnect', 'after_seq': <int>}  -> on reaching it, RAISE TransportDisconnected
                                                            (the recorder bumps reconnect_epoch + may alert).
          {'_control':'reconnect'}                        -> marks the start of the next live segment
                                                            (the next stream() call resumes here).
    control_aware=True (default) honors _control verbs; control_aware=False yields data frames only
    (treating the file as a plain FakeTransport-style script, ignoring control rows).

    Disconnect signaling: stream() yields data frames until a 'disconnect' control row, then raises
    TransportDisconnected. A subsequent stream() call resumes AFTER the matching 'reconnect' row (or after
    the disconnect row if none). This is how a single fixture (flaky_transport_gap.jsonl, §L.4) drives
    reconnect_epoch += 1 and, combined with the injected FakeClock crossing alert_after_ms, a
    prolonged_disconnect data_quality_alert."""

    def __init__(self, frames, *, control_aware: bool = True): ...

    async def stream(self, symbols):
        """async generator. Yields json-bytes of each DATA frame; on a 'disconnect' control row raises
        TransportDisconnected; resumes after 'reconnect' on the next call. Stateful across calls so the
        recorder's reconnect loop advances through segments deterministically."""
        ...
```
**MAJOR 5 binding:** constructor (`FlakyTransport(frames, *, control_aware=True)`), the exact `_control` verbs (`disconnect`/`reconnect`), how a disconnect surfaces (raises `TransportDisconnected`, not a silent end-of-iteration), and how the recorder maps it (`reconnect_epoch += 1` + a `prolonged_disconnect` alert when `FakeClock` crosses `alert_after_ms`) are all specified. `_control` rows are stripped before the parser, so `event.parse` never sees them (matches §L.4 note).

---

### G. `scripts/recorder/status.py` — heartbeat + sequence/gap detection (S4 inputs)

```python
# scripts/recorder/status.py
from dataclasses import dataclass
from enum import Enum
from typing import Optional

class SequencePolicy(str, Enum):
    MONOTONIC = "monotonic"      # vendor_seq must be prev+1; a jump => gap
    NONE = "none"                # dataset has no meaningful per-venue seq (EQUS.MINI composite) => never a gap

@dataclass(frozen=True)
class GapReport:
    symbol: str
    expected_seq: Optional[int]  # expected vendor_seq
    got_seq: Optional[int]       # observed vendor_seq
    gap_size: Optional[int]
    kind: str                    # "gap" | "reset_to_zero" | "no_seq_semantics" | "heartbeat_timeout"

class SequenceTracker:
    """Per-symbol monotonic-seq watcher KEYED to the dataset's sequencing semantics (from the verified_matrix).
    Observes ev.provenance.vendor_seq (BLOCKER 1 rename).
    policy=NONE -> never reports a 'gap' (composite feed; vendor_seq null/0) -> NO false alerts.
    policy=MONOTONIC -> vendor_seq jump > 1 -> kind='gap', gap_size = got - expected.
    A vendor_seq RESET to 0 mid-stream is kind='reset_to_zero' (a reconnect/epoch marker, NOT a gap)."""
    def __init__(self, symbol: str, *, policy: SequencePolicy) -> None: ...
    def observe(self, ev) -> Optional[GapReport]: ...

class HeartbeatMonitor:
    """Injected-clock freshness watcher. quiet > timeout_ms -> GapReport(kind='heartbeat_timeout').
    Produces the freshness/epoch/gap INPUTS the M5 execution_preflight consumes (S4 inputs only; full S4 in M5)."""
    def __init__(self, *, timeout_ms: int, clock) -> None: ...
    def touch(self, symbol: str, now_ms: int) -> None: ...
    def check(self, symbol: str, now_ms: int) -> Optional[GapReport]: ...
    def stale_symbols(self, now_ms: int) -> tuple: ...

def make_data_quality_alert(*, cause: str, symbol=None, detail=None, down_ms=None,
                            reconnect_epoch: int = 0) -> dict:
    """Build the data_quality_alert row body. `cause` enumerated:
    'sequence_gap' | 'reset_to_zero' | 'heartbeat_timeout' | 'prolonged_disconnect' | 'crossed_book'.
    Written via EventWriter to the data_quality_alerts stream (sharing the agent run_id) — NEVER a silent exit."""
    ...
```
**Raises:** none (returns data/Optional). **S4-inputs binding:** `GapReport` + `reconnect_epoch` + heartbeat-staleness are the freshness/epoch/gap inputs M5's preflight consumes.

---

### H. `scripts/recorder/replay.py` — re-derive book_hash from recorded streams (S3)

```python
# scripts/recorder/replay.py
from dataclasses import dataclass
from typing import Iterable, List, Optional, Dict
from recorder.book_hash import book_hash
from recorder.book_state import EquityBookState
from recorder.event_row import from_row           # BLOCKER 2: rebuild Event from the flat persisted row
from recorder.persistence import replay_stream

class ReplayHashMismatch(Exception):
    """A re-derived book_hash does not match the recorded/expected hash (S3 failure)."""

@dataclass(frozen=True)
class ReplayResult:
    rows: list                                   # hash-verified persisted rows (via persistence.replay_stream)
    rederived_book_hashes: Dict[tuple, str]      # {(symbol, vendor_seq) -> book_hash} recomputed from a fresh state
    final_snapshots: Dict[str, "BookSnapshot"]
    ok: bool
    first_mismatch: Optional[dict]

def replay_book_hashes(depth_stream_path) -> ReplayResult:
    """Re-read the depth stream (hash-verified via replay_stream), rebuild each DepthEvent via from_row
    (§B2), feed it through a fresh EquityBookState, and re-derive book_hash at each step. Pure function of
    the recorded events -> deterministic. Single-file in M1 (no rotation; minor 9). Keyed on
    (symbol, vendor_seq)."""
    ...

def derive_book_hashes(events: Iterable, *, symbols=None) -> ReplayResult:
    """Same derivation directly from parsed events (used by the fixture-driven test path). Keyed on
    (symbol, vendor_seq)."""
    ...

def assert_matches_expected(result: ReplayResult, expected_path) -> None:
    """Compare {(symbol, vendor_seq) -> book_hash} against
    tests/fixtures/databento/replay_expected_hashes.json byte-for-byte. Raise ReplayHashMismatch (with
    symbol+vendor_seq+both hashes) on the FIRST divergence."""
    ...
```
**Raises:** `ReplayHashMismatch`, `JournalCorruption` (from underlying replay). **BLOCKER 1+2 binding:** replay rebuilds events via `from_row` and keys on `(symbol, vendor_seq)` — matching `write_event`'s flat shape and the renamed sequence. **S3 binding:** `test_replay_hashes.py` runs the derivation on `mbp10_depth_sample.jsonl`, asserts byte-equality with `replay_expected_hashes.json`, AND asserts idempotency (same input replayed twice → identical hashes).

---

### I. `scripts/recorder/reconcile.py` — dual-hash reconcile harness (offline shape only in M1)

```python
# scripts/recorder/reconcile.py
from dataclasses import dataclass
from typing import Iterable, List, Optional

@dataclass(frozen=True)
class ReconcileReport:
    matched: int
    mismatches: tuple                # ({symbol, vendor_seq, recorded_hash, reference_hash}, ...)
    missing_in_recorded: tuple
    missing_in_reference: tuple
    ok: bool                         # True iff no mismatches and no missing rows

def reconcile_against_fixture(recorded_path, reference_path) -> ReconcileReport:
    """OFFLINE: compare the recorder's re-derived hashes against a pinned reference fixture (a second
    recorded stream / golden file). Keyed on (symbol, vendor_seq). The credentialed Databento-historical
    pull is tier-2, stubbed/skipped offline. ok=False on ANY mismatch or missing row (fail-closed); the CLI
    maps not-ok to a non-zero exit (never silently swallowed, spec §7 reconcile). Single-file in M1
    (no rotation; minor 9)."""
    ...

def reconcile_against_historical(recorded_path, historical_loader):
    """Tier-2 STUB: historical_loader is injected and lazily builds the SDK client (HISTORICAL API per the
    2026-06-09 access constraint — verifiable NOW with the entitled key, §P 2a). Offline tests never call it.
    Raises NotImplementedError in M1 offline scope."""
    ...
```
**Raises:** `NotImplementedError` (historical stub). Offline path returns a result object.

---

### J. `scripts/recorder/bar_cache.py` — DETERMINISTIC ET-session-boundary resampler (S2 re-verify + DST)

Placed at `scripts/recorder/bar_cache.py` (imported `from recorder.bar_cache import ...`) for cohesion with the recorder bar path. The OFFLINE resampler core is **stdlib-only** (`zoneinfo`) — no `exchange_calendars` in the offline path, so `test_no_network_no_creds`-style purity holds. (`exchange_calendars`, pinned in M1 requirements, is used by M2's session gate, not M1's resampler; the M1 resampler buckets by ET wall-clock date only and does NOT know half-days/holidays — that calendar awareness is explicitly M2.)

```python
# scripts/recorder/bar_cache.py
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo                  # STDLIB — DST-correct; no exchange_calendars in the offline core

ET = ZoneInfo("America/New_York")
VWAP_QUANTUM = Decimal("0.0001")               # 4dp, matches book_hash/event price scale

class EmptyWindowVWAP(ValueError):
    """Raised if a caller tries to MATERIALIZE a VWAP for a zero-volume window (S2: floats are born here)."""

@dataclass(frozen=True)
class Bar:
    symbol: str
    interval: str                  # "1s" | "1m" | "1d"
    bucket_start_utc: str          # bucket OPEN, UTC ISO-8601 (the ET-aligned boundary converted to UTC)
    bucket_end_utc: str            # bucket CLOSE (exclusive), UTC ISO-8601
    session_date_et: str           # "YYYY-MM-DD" ET trading-session date this bucket belongs to
    open: Decimal; high: Decimal; low: Decimal; close: Decimal
    volume: Decimal
    vwap: Optional[Decimal]        # None for an empty window — NEVER NaN/Inf; quantized VWAP_QUANTUM otherwise
    trade_count: int

def et_session_date(ts_utc_iso: str) -> str:
    """Map a UTC instant to its ET trading-session date. Converts UTC->ET via zoneinfo (DST-correct), then takes
    the ET calendar date. e.g. 2026-06-09T20:00:00Z -> ET 2026-06-09 (EDT, UTC-4);
    2026-12-09T21:00:00Z -> ET 2026-12-09 (EST, UTC-5). Operates on an UNAMBIGUOUS UTC instant so the fall-back
    fold never affects assignment (UTC->ET is always unambiguous)."""
    ...

def resample(events: Iterable["TradeEvent"], *, interval: str) -> List[Bar]:
    """Deterministic resampler. Bucket boundaries computed in ET wall-clock (via zoneinfo) then persisted as UTC
    (bucket_start_utc/bucket_end_utc) + session_date_et (spec §11). For each NON-EMPTY bucket emit a Bar; SKIP
    empty buckets entirely (no fabricated zero-volume bar). vwap = (sum px*sz)/(sum sz) in Decimal, quantized
    VWAP_QUANTUM ROUND_HALF_EVEN. If a caller forces a bar on an empty window, raise EmptyWindowVWAP — NEVER
    produce Decimal('NaN')/Decimal('Infinity'). Input sorted defensively by ts_event_utc for determinism."""
    ...
```

**DST handling (frozen rules):**
- All bucketing is done in ET wall-clock via `zoneinfo.ZoneInfo("America/New_York")`, stdlib, DST-correct. The resampler buckets each event by converting its UTC instant to ET INDEPENDENTLY (`dt_utc.astimezone(ET)`, floor to interval in ET, then `.astimezone(UTC)` for `bucket_start_utc`) — it NEVER enumerates wall-clock minutes and NEVER assumes a fixed UTC offset.
- **RTH close 16:00 ET** → 20:00 UTC (EDT/summer) / 21:00 UTC (EST/winter). Because boundaries are computed in ET, the close lands at the correct UTC instant on both sides of a DST change — a single hardcoded UTC offset would be WRONG by one hour across the transition.
- **Spring-forward (nonexistent 02:00–03:00 ET):** the skipped hour yields no bucket there (no synthetic empty bar); harmless for RTH (09:30–16:00).
- **Fall-back (ambiguous 01:00–02:00 ET, repeated hour):** disambiguated by the underlying UTC instant (UTC is monotonic), so the "same" wall-clock hour maps to two distinct buckets — NO double-count, no 25h day.

**S2 empty-window VWAP (frozen):** `resample` SKIPS zero-volume buckets; a forced empty-window VWAP raises `EmptyWindowVWAP` instead of `0/0=NaN` or `x/0=Inf`. As a second wall, `serializer.dumps` rejects a non-finite Decimal (`serializer.py:42`) and floats entirely (`serializer.py:27-29`), so even a regression cannot persist a NaN VWAP — but the resampler fails earlier and more legibly.

---

### K. `scripts/recorder/verify_databento_entitlements.py` — offline entitlement verifier (+ 2026-06-09 historical-only constraint)

Offline mode reads a FAKED `list-schemas` JSON (no network, no creds) and turns a `planned_matrix` into a `verified_matrix`. **No silent fallback:** an unavailable `(dataset, schema)` WITHOUT a downgrade note is a hard failure. Credentialed mode is a `NotImplementedError` stub never reached offline.

**2026-06-09 access constraint (folded in):** the provisioned key (`.secrets/databento.json`, user_id `<databento-user-id>`) entitles **HISTORICAL data only**; **live realtime is a separate paid subscription, NOT provisioned.** Because live ≡ historical *schema* (locked decision), the full parse/book_hash/replay/reconcile stack builds AND verifies against historical pulls now (same schemas: tbbo, mbp-10, ohlcv-*, trades, definitions). The `verified_matrix` therefore carries a per-cell `access` field and a top-level `live_subscription: "pending"`.

```python
# scripts/recorder/verify_databento_entitlements.py
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

@dataclass(frozen=True)
class PlannedCell:                 # MINOR 7: the verify() input element is a PlannedCell dataclass, not a bare tuple
    dataset: str                   # placeholder code OK offline, e.g. "EQUS.MINI"
    schema: str                    # e.g. "tbbo"
    use: str                       # "L1_nbbo" | "L2_depth" | "status" | ...

@dataclass(frozen=True)
class VerifiedCell:
    dataset: str
    schema: str
    use: str
    available: bool                # True iff schema present in this dataset's list-schemas
    access: str                    # 2026-06-09: "historical" | "live" | "both" (offline default "historical")
    downgrade: Optional[str]       # REQUIRED non-None when available is False, else UnverifiableSchema

@dataclass(frozen=True)
class VerifiedMatrix:
    cells: Tuple[VerifiedCell, ...]
    all_available: bool
    downgrades: Tuple[VerifiedCell, ...]
    live_subscription: str         # 2026-06-09: top-level "pending" offline (live realtime not provisioned)

class UnverifiableSchema(RuntimeError):
    """An unavailable (dataset,schema) was left without an explicit downgrade note (no-silent-fallback)."""

def planned_matrix() -> Tuple[PlannedCell, ...]:
    """The pinned M1 placeholder matrix as PlannedCell instances: EQUS.MINI L1 schemas + a depth-dataset
    mbp-10 + status (downgraded). Hardcoded/offline. Depth dataset code stays the placeholder string
    '<DEPTH_DATASET>' (the real entitled code is a tier-2 historical-verified deliverable, §P 2a)."""

def list_schemas_offline(response_path) -> Dict[str, List[str]]:
    """Pure file read of the FAKED list-schemas fixture: {dataset: [schema, ...]}. No client, no creds."""

def verify(planned: Iterable[PlannedCell], schemas_by_dataset: Dict[str, List[str]],
           downgrades: Optional[Dict[Tuple[str, str], str]] = None,
           access_by_cell: Optional[Dict[Tuple[str, str], str]] = None) -> VerifiedMatrix:
    """MINOR 7: `planned` is an iterable of PlannedCell. For each cell:
       available = cell.schema in schemas_by_dataset.get(cell.dataset, []).
       access    = access_by_cell.get((cell.dataset, cell.schema), 'historical')  # 2026-06-09 default
       An unavailable cell with no registered downgrade -> UnverifiableSchema (no silent fallback).
    `downgrades` is keyed by (dataset, schema) tuples (aligned to PlannedCell-derived keys; MINOR 7).
    Asserts EQUS.MINI has NO 'mbp-10' and NO 'status' (the verified-2026-06-08 fact) so a regression that
    silently maps depth onto EQUS.MINI fails loudly. Sets live_subscription='pending' (live not provisioned)."""

def write_artifact(verified: VerifiedMatrix, out_path) -> None:
    """Write the verified_matrix via agent.serializer.dumps (Decimal-safe, sorted, canonical). Includes the
    per-cell `access` field and top-level `live_subscription`."""

def verify_credentialed(cfg) -> VerifiedMatrix:
    """Tier-2 STUB: lazily `import databento`, read `.secrets/databento.json`, call the HISTORICAL list-schemas
    API. Records each verified cell access='historical' and live_subscription='pending' (live realtime is the
    deferred paid subscription, §P 2b). Raises NotImplementedError in M1 offline scope and is NEVER reached by
    an offline test."""

def main(argv: Optional[List[str]] = None) -> int:
    """CLI (argparse, fixed arg array, never shell=True): --offline --list-schemas <fixture> -> verified_matrix
    to stdout/--write-artifact; without --offline -> verify_credentialed (stub). Exit 0 iff every planned cell
    is available OR carries an explicit downgrade note; non-zero otherwise."""
```

**planned_matrix → verified_matrix shape (offline test core), with the frozen fixture:**
- INPUT `planned` (PlannedCell instances; MINOR 7): `[PlannedCell("EQUS.MINI","tbbo","L1_nbbo"), PlannedCell("EQUS.MINI","mbp-10","L2_depth"), PlannedCell("EQUS.MINI","status","status"), PlannedCell("<DEPTH_DATASET>","mbp-10","L2_depth")]`.
- INPUT `schemas_by_dataset` (FAKED): `{"EQUS.MINI": ["tbbo","bbo-1s","bbo-1m","trades","ohlcv-1s","ohlcv-1m","definitions"], "<DEPTH_DATASET>": ["mbp-10","trades","definitions"]}`.
- INPUT `downgrades` (keyed by `(dataset, schema)` tuples; MINOR 7): `{("EQUS.MINI","mbp-10"): "depth -> <DEPTH_DATASET>", ("EQUS.MINI","status"): "status -> broker (Alpaca) + exchange_calendars (M2)"}`.
- OUTPUT one `VerifiedCell` per planned cell: `tbbo`→available=True, access="historical"; `(EQUS.MINI, mbp-10)`→available=False + downgrade="depth -> <DEPTH_DATASET>"; `(EQUS.MINI, status)`→available=False + downgrade="status -> broker (Alpaca) + exchange_calendars (M2)"; `(<DEPTH_DATASET>, mbp-10)`→available=True, access="historical". `all_available=False` (deliberate downgrades) but NO exception because every unavailable cell carries a note. Top-level `live_subscription="pending"`. An unavailable cell with `downgrade=None` raises `UnverifiableSchema`.

---

### L. Fixture schemas (every fixture in the plan) — JSONL/JSON + sample rows

All `.jsonl` = one JSON object per line. **All prices AND sizes are strings (Decimal-as-string); timestamps are UTC ISO-8601 strings; `ct` is an int.** Floats are forbidden anywhere so the fixtures load cleanly through the serializer. Every row carries `dataset`, `schema`, `instrument_id`, `symbol`, and the renamed **`vendor_seq`** (BLOCKER 1) so the parser produces full `Provenance`.

**1. `tests/fixtures/databento/equs_mini_tbbo_sample.jsonl`** — L1 NBBO (`tbbo`, the primary NBBO source). Fields: `{dataset,schema,instrument_id,symbol,vendor_seq,ts_event,ts_recv,bid_px,bid_sz,ask_px,ask_sz}`.
```
{"dataset":"EQUS.MINI","schema":"tbbo","instrument_id":1001,"symbol":"AAPL","vendor_seq":1001,"ts_event":"2026-06-09T13:30:00.100000Z","ts_recv":"2026-06-09T13:30:00.100200Z","bid_px":"201.1500","bid_sz":"300","ask_px":"201.1600","ask_sz":"200"}
{"dataset":"EQUS.MINI","schema":"tbbo","instrument_id":1001,"symbol":"AAPL","vendor_seq":1002,"ts_event":"2026-06-09T13:30:00.250000Z","ts_recv":"2026-06-09T13:30:00.250300Z","bid_px":"201.1500","bid_sz":"300","ask_px":"201.1700","ask_sz":"150"}
```

**2. `tests/fixtures/databento/equs_mini_sequence_zero_sample.jsonl`** — `vendor_seq` RESET to 0 after a reconnect (must be `reset_to_zero`, NOT a gap). Same `tbbo` schema; one row with `vendor_seq` then a row with `vendor_seq: 0`.
```
{"dataset":"EQUS.MINI","schema":"tbbo","instrument_id":1001,"symbol":"AAPL","vendor_seq":1050,"ts_event":"2026-06-09T13:30:05.000000Z","ts_recv":"2026-06-09T13:30:05.000100Z","bid_px":"201.2000","bid_sz":"100","ask_px":"201.2100","ask_sz":"100"}
{"dataset":"EQUS.MINI","schema":"tbbo","instrument_id":1001,"symbol":"AAPL","vendor_seq":0,"ts_event":"2026-06-09T13:30:06.000000Z","ts_recv":"2026-06-09T13:30:06.000100Z","bid_px":"201.2000","bid_sz":"100","ask_px":"201.2200","ask_sz":"120"}
```

**3. `tests/fixtures/databento/mbp10_depth_sample.jsonl`** — L2 `mbp-10` depth ladder (drives book_hash + replay). Fields: `{dataset,schema,instrument_id,symbol,vendor_seq,ts_event,bids,asks}` where each level is `[px_str, sz_str, ct_int]`. MUST include: (a) a zero-size padding level, (b) a duplicate price split across two entries, and (c) a `300.0`-style size proving SIZE_QUANTUM canonicalization (MAJOR 3).
```
{"dataset":"<DEPTH_DATASET>","schema":"mbp-10","instrument_id":1001,"symbol":"AAPL","vendor_seq":2001,"ts_event":"2026-06-09T13:30:00.500000Z","bids":[["201.1500","300.0",3],["201.1500","0",0],["201.1400","200",2],["201.1400","300",3]],"asks":[["201.1600","200",2],["201.1700","400",4]]}
{"dataset":"<DEPTH_DATASET>","schema":"mbp-10","instrument_id":1001,"symbol":"AAPL","vendor_seq":2002,"ts_event":"2026-06-09T13:30:00.700000Z","bids":[["201.1500","250",2],["201.1400","500",5]],"asks":[["201.1600","200",2]]}
```
(Row `vendor_seq:2001` carries: `"300.0"` size → must canonicalize to `Decimal('300')` → str `"300"` (MAJOR 3); a `["201.1500","0",0]` zero-size padding level → dropped; and `201.1400` split across two entries (`"200"`+`"300"`) → coalesced to `"500"`. So `test_zero_size_and_duplicate_price_normalized` AND `test_size_300pt0_canonicalizes` both have real material.)

**4. `tests/fixtures/databento/flaky_transport_gap.jsonl`** — injected `vendor_seq` gap (e.g. 2001→2004, missing 2002/2003) plus `_control` rows the FlakyTransport fake (§F2) consumes. Fields as `mbp-10` (or `tbbo`). The PARSER IGNORES `_control` rows; only FlakyTransport reads them (verbs `disconnect`/`reconnect`, §F2).
```
{"dataset":"<DEPTH_DATASET>","schema":"mbp-10","instrument_id":1001,"symbol":"AAPL","vendor_seq":2001,"ts_event":"2026-06-09T13:31:00.000000Z","bids":[["201.3000","100",1]],"asks":[["201.3100","100",1]]}
{"_control":"disconnect","after_seq":2001}
{"_control":"reconnect"}
{"dataset":"<DEPTH_DATASET>","schema":"mbp-10","instrument_id":1001,"symbol":"AAPL","vendor_seq":2004,"ts_event":"2026-06-09T13:31:01.000000Z","bids":[["201.3000","100",1]],"asks":[["201.3300","80",1]]}
```

**5. `tests/fixtures/databento/list_schemas_response.json`** — faked list-schemas (drives the entitlement verifier). Shape `{dataset: [schema, ...]}`. EQUS.MINI deliberately lacks `mbp-10` and `status`.
```json
{"EQUS.MINI":["tbbo","bbo-1s","bbo-1m","trades","ohlcv-1s","ohlcv-1m","definitions"],
 "<DEPTH_DATASET>":["mbp-10","trades","definitions"]}
```

**6. `tests/fixtures/databento/replay_expected_hashes.json`** — expected `book_hash` per `(symbol, vendor_seq)` (BLOCKER 1 key rename) for `mbp10_depth_sample.jsonl`. Hash VALUES are placeholders filled by the first green TDD run (golden-by-construction: run `book_hash` once, pin the output — values are GENERATED from `canonical_book_payload`, never hand-guessed); the SHAPE is frozen.
```json
{"v":2,"book_hash_algo":"row_hash(canonical_book_payload(snapshot))",
 "hashes":[{"symbol":"AAPL","vendor_seq":2001,"book_hash":"<sha256hex-of-canonical-ladder-2001>"},
           {"symbol":"AAPL","vendor_seq":2002,"book_hash":"<sha256hex-of-canonical-ladder-2002>"}]}
```

**7. `tests/fixtures/bars/dst_boundary_events.jsonl`** — trades straddling an ET session close + a DST transition. Use the **2026-11-01 fall-back** (EST begins). Fields: `{dataset,schema,instrument_id,symbol,vendor_seq,ts_event,price,size,side}`.
```
{"dataset":"EQUS.MINI","schema":"trades","instrument_id":1001,"symbol":"AAPL","vendor_seq":3001,"ts_event":"2026-10-30T19:59:30.000000Z","price":"201.5000","size":"100","side":"B"}
{"dataset":"EQUS.MINI","schema":"trades","instrument_id":1001,"symbol":"AAPL","vendor_seq":3002,"ts_event":"2026-11-02T14:30:00.000000Z","price":"202.0000","size":"150","side":"A"}
```
(2026-10-30 19:59:30Z = 15:59:30 ET (EDT, UTC-4), inside RTH on the 2026-10-30 session; 2026-11-02 14:30:00Z = 09:30 ET (EST, UTC-5), RTH open on the 2026-11-02 session. Both land on the correct ET session date; the fixture proves the EDT→EST offset flip is handled. For a mid-session DST bucket test, add a third pair on the transition-day session itself if a 1m-bucket assertion is desired.)

**8. `tests/fixtures/databento/sub_dollar_subpenny_sample.jsonl`** — MAJOR 4 fail-loud fixture. A sub-$1 symbol with a sub-penny price (`0.00005`) that would quantize to `0.0000`. Parsing this row MUST raise `PrecisionLoss` (M1 excludes sub-$1 symbols). Fields as `tbbo`.
```
{"dataset":"EQUS.MINI","schema":"tbbo","instrument_id":1099,"symbol":"PENNYX","vendor_seq":9001,"ts_event":"2026-06-09T13:30:00.000000Z","ts_recv":"2026-06-09T13:30:00.000100Z","bid_px":"0.00005","bid_sz":"1000","ask_px":"0.00006","ask_sz":"1000"}
```
(`Decimal('0.00005').quantize(Decimal('0.0001'), ROUND_HALF_EVEN) == Decimal('0.0000')` — verified — i.e. it does NOT round-trip, so `_quantize_checked` raises `PrecisionLoss`. `test_subpenny_price_raises_precision_loss` asserts this.)

---

### M. Conventions-to-mirror table (with M0 source pointers)

| Convention (M1 must mirror) | M0 source pointer | M1 enforcement point |
|---|---|---|
| Decimal-as-string; reject float; reject non-finite | `scripts/agent/serializer.py:27-29` (`_reject_floats`), `:42` (non-finite), `:47-50` (`dumps`) | `event.parse`, `book_hash` payload, `bar_cache` vwap, all `persistence` rows |
| Row hash = sha256 of canonical JSON | `scripts/agent/serializer.py:53-55` (`row_hash`) | `book_hash` reuses `row_hash`; persistence row hash via journal |
| Per-stream monotonic seq + single writer lock (shared per resolved path) | `scripts/agent/journal.py:74` (`_state_for`), `:115` (lock), `:62` (`_StreamState`) | `persistence.EventWriter` wraps `JournalWriter` |
| One write per row (`fh.write(dumps(row)+"\n")`) | `scripts/agent/journal.py:128` | inherited via `JournalWriter` |
| Truncated tail dropped; corrupt complete line / tampered hash fatal | `scripts/agent/journal.py:28-59`, `:99`; tests `test_journal_replay.py:77,87,98,157` | `persistence.replay_stream` delegates to `journal.replay`; `replay.py` builds on it |
| Reserved-key collision rejected (`vendor_seq` NOT `seq` — BLOCKER 1) | `scripts/agent/journal.py:21` (`_RESERVED`), `:113-114` (raise) | `to_row` uses `vendor_seq`; `EventWriter.write_event` never collides |
| `run_id`/`decision_id`/`order_id` + per-stream journal `seq` (S6) | `scripts/agent/journal.py:119,120,122-125` | `EventWriter` shares the agent `run_id`; events carry `instrument_id`+`symbol`+`vendor_seq` |
| Injected clock (no wall-time non-determinism) | `scripts/agent/journal.py:91` (`clock=`); `tests/lib/fakes.py:20` (`FakeClock`) | `Recorder(clock=,sleep=)`, `EventWriter(clock=)`, `HeartbeatMonitor(clock=)` |
| ET-logic / UTC-persist | spec §11; `scripts/agent/journal.py:84` (`_utc_now_iso`) | `bar_cache.et_session_date`, `Bar.*_utc` |
| Transport seam = `@runtime_checkable` Protocol, `async stream(symbols)->AsyncIterator[bytes]` | `scripts/agent/marketdata/base.py:11-13`; `tests/lib/fakes.py:15` (`FakeTransport.stream` is `async def`+`yield`); contract test `tests/agent/test_marketdata_transport.py:11,17` | `DatabentoTransport.stream` is `async def`+`yield`; `FlakyTransport` mirrors it; `isinstance` passes structurally |
| No SDK import / no socket offline | `scripts/agent/broker/alpaca.py:1-9` (imports no alpaca); `tests/agent/test_no_network_no_creds.py:19,24` | `databento.py` lazy import only in `_build_real_client`; `verify_*.py` credentialed stub |
| Deterministic fakes, no network/clock/random | `tests/lib/fakes.py:9,15,20` | EXTEND with `FlakyTransport`/`TransportDisconnected` (§F2); REUSE `FakeClock`, mirror `FakeTransport` |
| Import root `<repo>/scripts` on `sys.path` | `tests/__init__.py:11-14`; `conftest.py` (both already exist) | create `scripts/recorder/__init__.py` + `tests/recorder/__init__.py` (empty) |
| Subprocess via fixed arg array, never `shell=True` | spec §11; CLAUDE.md | verifier `main(argv)` uses argparse, no shell |
| Daily rotation / retention — **OUT of M1-offline (MINOR 9)** | `config/data_retention.json` | M1 = single-file per stream; rotation + cross-file concatenation deferred to a later milestone (no M1 component owns it) |
| Tokens/forgery posture (mirrored discipline, not M1-built) | `scripts/agent/execution_preflight.py` | M1 produces S4 INPUTS only (freshness/epoch/gap); no token minting in M1 |

---

### N. Test list — each test file → cases → safety invariant

**`tests/recorder/test_transport_fakes.py`** (no-net / seam)
- `test_databento_transport_is_a_marketdata_transport` → `isinstance(DatabentoTransport(cfg, raw_source=...), MarketDataTransport)` (mirrors `test_marketdata_transport.py:17`). (seam)
- `test_streams_injected_bytes_in_order` → offline fake replays fixture bytes in order via `async for`. (determinism)
- `test_unsubscribed_symbol_raises` → fail-closed symbol validation (ValueError before source access). (S1-spirit)
- `test_import_adds_no_databento_to_sys_modules_and_no_socket` → `"databento" not in sys.modules` after import+stream; `socket.socket` patched-to-raise never hit (mirrors `test_no_network_no_creds.py:24`). (no-net)
- `test_credentialed_build_is_notimplemented_offline` → `_build_real_client` raises `NotImplementedError`. (no-net)
- `test_flaky_transport_disconnect_raises_transport_disconnected` → a `_control:disconnect` row makes `stream` raise `TransportDisconnected` and resume after `reconnect` (§F2). (fault-injection seam)

**`tests/recorder/test_databento_event_parser.py`** (S6 contributor; S2 born seam)
- `test_parses_tbbo_quote_decimal_prices` → px/sz are `Decimal`, never float. (S2/S6)
- `test_parses_mbp10_depth_ladder` → ≤10-level ladder; px/sz Decimal, ct int. (S2/S6)
- `test_parses_trade_and_bar_and_definition` → trades/ohlcv/definitions dispatch correctly via SCHEMA_REGISTRY. (S6)
- `test_schema_registry_dispatch_isinstance` → each registered schema returns its mapped dataclass; dispatch by `isinstance` + `provenance.schema` (MAJOR 6). (registry)
- `test_provenance_carries_correlation_metadata` → every event has instrument_id+symbol+**vendor_seq**+reconnect_epoch. (S6 / BLOCKER 1)
- `test_float_or_nonfinite_price_raises` → a float/NaN price raises `NonFinitePrice`, never silently coerced. (S2)
- `test_subpenny_price_raises_precision_loss` → `sub_dollar_subpenny_sample.jsonl` row raises `PrecisionLoss` (MAJOR 4 fail-loud; sub-$1 excluded). (S2/§14)
- `test_size_300pt0_canonicalizes` → a `"300.0"` size parses to `Decimal('300')` whose `str` is `"300"` (MAJOR 3); a fractional `"1.5"` size raises `PrecisionLoss`. (determinism)
- `test_row_roundtrip_is_identity` → `from_row(to_row(ev)) == ev` for every event type (BLOCKER 2). (seam integrity)
- `test_unknown_schema_raises` → `UnknownSchema`, no silent skip. (S6 / no-silent-fallback)
- `test_malformed_record_is_fatal` → missing/invalid required field raises `MalformedRecord`. (fail-closed §14)
- `test_more_than_10_depth_levels_is_malformed` → mbp-10 invariant enforced. (fail-closed)

**`tests/recorder/test_sequence_gap_detection.py`** (S4 inputs)
- `test_gap_fires_on_injected_seq_jump` → `flaky_transport_gap.jsonl` 2001→2004 yields `GapReport(kind="gap", gap_size=3)` (keyed on `vendor_seq`). (S4)
- `test_seq_reset_to_zero_is_not_a_gap` → `equs_mini_sequence_zero_sample.jsonl` → `kind="reset_to_zero"`, no gap. (S4)
- `test_gap_detection_disabled_when_policy_none` → `policy=NONE` never fires a gap (composite feed). (S4)
- `test_heartbeat_stale_after_timeout` → staleness flagged via FakeClock → `kind="heartbeat_timeout"`. (S4)
- `test_reconnect_epoch_stamped_on_events` → epoch increments and is on each subsequent event (driven by `FlakyTransport` disconnect, §F2). (S4)

**`tests/recorder/test_replay_hashes.py`** (S3, binds `book_hash`)
- `test_replay_rederives_expected_book_hashes` → `mbp10_depth_sample.jsonl` re-derives `replay_expected_hashes.json` byte-for-byte (keyed `(symbol, vendor_seq)`). (S3)
- `test_replay_is_idempotent` → same input replayed twice → identical hashes. (S3)
- `test_book_hash_is_order_independent` → shuffled vendor level order → identical hash. (S3 determinism)
- `test_canonical_payload_excludes_provenance` → changing ts/vendor_seq/epoch/ct does NOT change book_hash (asserts `canonical_book_payload` directly). (S3 / white-box)
- `test_book_hash_includes_identity` → different symbol OR instrument_id → different hash. (S3)
- `test_zero_size_and_duplicate_price_normalized` → padded/omitted + split-price encodings of the same book hash identically (fixture row 2001). (S3)
- `test_size_canonicalization_into_book_hash` → a `"300.0"` size and a `"300"` size produce the SAME book_hash (MAJOR 3). (S3)
- `test_float_price_into_book_hash_raises` → a float reaching the payload raises via serializer. (S2/S3)
- `test_persisted_depth_row_replays_to_same_hash` → `write_event(DepthEvent)` then `replay.py` via `from_row` re-derives the persisted `derived_book_hash` (BLOCKER 2 end-to-end). (S3)
- `test_replay_drops_truncated_tail_not_fatal` → mirrors journal partial-write rule. (S3)
- `test_replay_corrupt_midline_is_fatal` → `JournalCorruption` on a complete corrupt line. (S3)
- `test_replay_detects_tampered_hash` → tampered persisted row → `JournalCorruption` (mirrors `test_journal_replay.py:98`). (S3)

**`tests/recorder/test_bar_cache.py`** (S2 re-verify + DST)
- `test_empty_window_vwap_rejects_naninf` → forced empty window raises `EmptyWindowVWAP`; no NaN/Inf row emitted; and `dumps({"vwap": Decimal("nan")})` raises (binds `serializer.py:42`). (S2)
- `test_vwap_is_decimal_quantized_half_even` → vwap is Decimal at 4dp, finite, when window non-empty. (S2)
- `test_empty_buckets_are_skipped_not_zero_volume_rows` → no fabricated zero-volume bars. (S2)
- `test_et_session_date_edt_event` → 2026-06-09T20:00:00Z → ET date 2026-06-09. (DST/§11)
- `test_et_session_date_est_event` → 2026-12-09T21:00:00Z → ET date 2026-12-09. (DST/§11)
- `test_dst_boundary_fixture_assigns_correct_sessions` → `dst_boundary_events.jsonl`: both trades land on correct ET session dates across the EDT→EST flip. (DST/§11)
- `test_bucket_boundaries_persisted_utc` → `bucket_start_utc`/`bucket_end_utc` correct on both sides of DST; no 25h day on fall-back. (§11)
- `test_resample_deterministic_on_shuffled_input` → shuffled input → identical bars. (determinism)

**`tests/recorder/test_entitlement_verifier.py`** (offline; no-net)
- `test_verify_marks_equs_mini_has_no_mbp10` → `(EQUS.MINI, mbp-10)` available=False. (matrix correctness)
- `test_verify_marks_equs_mini_has_no_status_with_downgrade` → `(EQUS.MINI, status)` available=False + downgrade set. (no-silent-fallback)
- `test_depth_dataset_has_mbp10` → `(<DEPTH_DATASET>, mbp-10)` available=True. (matrix correctness)
- `test_absent_schema_without_downgrade_raises` → `UnverifiableSchema` / non-zero exit. (no-silent-fallback)
- `test_verify_takes_plannedcell_instances` → `verify` consumes `PlannedCell(...)` instances; `downgrades` keyed by `(dataset,schema)` (MINOR 7). (contract shape)
- `test_verified_matrix_shape` → output is `VerifiedMatrix(cells, all_available, downgrades, live_subscription)` with per-cell `access`. (contract shape / 2026-06-09)
- `test_access_field_and_live_subscription_pending` → every offline cell `access="historical"`; top-level `live_subscription="pending"` (2026-06-09 constraint). (access shape)
- `test_write_artifact_is_decimal_safe_canonical` → `write_artifact` round-trips through `agent.serializer.dumps`. (determinism)
- `test_no_databento_import_no_socket_offline` → faked response only; SDK/socket never touched. (no-net)

**`tests/recorder/test_reconnect_alert.py`** (S4 input / liveness)
- `test_sustained_disconnect_emits_data_quality_alert` → `FlakyTransport` disconnect held > `alert_after_ms` (FakeClock) writes a `prolonged_disconnect` `data_quality_alert` row (§F2). (S4)
- `test_recorder_does_not_exit_silently_on_disconnect` → loop catches `TransportDisconnected`, reconnects, never returns silently. (liveness, spec §5)
- `test_backoff_is_capped` → delay = min(cap_ms, base*factor**n); attempts unbounded; `BackoffPolicy` has no `max_attempts`. (spec §5)
- `test_reconnect_increments_epoch` → epoch bumps per reconnect, stamped on events. (S4)
- `test_no_real_sleep_offline` → injected `sleep` recorded, wall-clock untouched. (determinism/no-net)

---

### O. Consensus addendum — antithesis + tradeoff tensions (recorded so the build agent does not relitigate)

- **Antithesis to excluding `ct` from book_hash:** the M5 depth-VWAP modeled fill MIGHT consume order-count. Excluding it now means a future consumer needs a v3 bump. **Resolution:** `ct` is PARSED and CARRIED on `DepthLevel` (available to M5), just EXCLUDED from the hash — so we keep replay stability now (count churn that doesn't move price/size won't inflate "book changed") AND lose no data. If M5 proves it needs ct in the hash, bump to `"v":3` then.
- **Tradeoff tension — quantization-at-parse vs raw fidelity:** quantizing to 4dp at parse makes `str(Decimal)` canonical (closing `1.5`/`1.50`) but discards sub-0.0001 precision. For US equities (Reg-NMS sub-penny floor) this is correct; **a sub-$1 sub-penny price that would NOT round-trip now RAISES `PrecisionLoss` (MAJOR 4) rather than silently zeroing** — M1 excludes sub-$1 symbols by contract. If a tier-2 dataset ever delivers finer price granularity, the quantum is a single named constant (`PRICE_QUANTUM`) to revisit. Without quantization, book_hash determinism depends on a downstream discipline (P3's risk) — the structural choice is strictly safer.
- **Tradeoff tension — size quantization (MAJOR 3):** sizes are quantized to `SIZE_QUANTUM = Decimal("1")` (whole shares) with a round-trip check, NOT `.normalize()` (which renders `300` as `'3E+2'` — verified — breaking the canonical string). A genuine fractional-share dataset would need a `SIZE_QUANTUM` revisit (single named constant), and would currently fail loud via `PrecisionLoss` rather than silently mis-hash.
- **Tradeoff tension — mbp-10 replace-on-apply vs delta-apply:** the contract treats each mbp-10 record as a full snapshot (`EquityBookState.apply` = REPLACE-on-snapshot), with a documented seam in `apply()` to switch to delta-apply. Offline fixtures honor replace-on-apply. **This is now a HISTORICAL-verified tier-2 task (§P 2a) — confirmable NOW against a historical mbp-10 pull with the entitled key, NOT blocked on the live subscription.**

---

### P. Tier-2 acceptance — split into HISTORICAL-verified (now) vs LIVE-verified (deferred)

Per the 2026-06-09 access constraint, M1's tier-2 stop-condition is SPLIT. The entitled key (`.secrets/databento.json`, user_id `<databento-user-id>`) covers **historical only**; live realtime is a separate, un-provisioned paid subscription. Because live ≡ historical *schema*, the full parse/book_hash/replay/reconcile stack verifies against historical pulls now.

**(2a) HISTORICAL-verified — runnable NOW with the current key (no live subscription needed):**
- Exact dataset codes resolved (replace `<DEPTH_DATASET>` with the real entitled code) via the credentialed HISTORICAL `list-schemas`.
- Per-`(dataset, schema)` availability recorded in the `verified_matrix` with `access="historical"`.
- A one-symbol sample HISTORICAL pull per dataset (tbbo, mbp-10, ohlcv-*, trades, definitions).
- Sequence-number semantics confirmed from HISTORICAL records (drives `SequencePolicy` per dataset).
- **mbp-10 snapshot-vs-delta confirmation** against a real historical mbp-10 pull (the `EquityBookState.apply` replace-vs-delta seam, §C/§O).

**(2b) LIVE-verified — DEFERRED, explicit blocker on the paid subscription:**
- Real live-gateway reconnect / heartbeat / gap behavior against the realtime feed.
- In M1, reconnect/gap/heartbeat logic is tested ONLY against fixtures (`FlakyTransport`, §F2); real-gateway validation is deferred in writing until the live realtime subscription is provisioned.
- `verified_matrix.live_subscription` stays `"pending"` until then.

**Deferred to tier-2 (account/cost-gated) — the genuinely human/account decisions still open:**
- The exact `mbp-10` depth dataset code (the entitled US-equities depth dataset replacing `<DEPTH_DATASET>`).
- Whether to BUY the live realtime subscription (cost decision; gates 2b).
- Final NBBO cost confirmation for the chosen `tbbo` dataset (cost/billing, not a schema decision).

*(All other prior "open questions" are now FROZEN: NBBO source = `tbbo`; recorder SHARES the agent `run_id`; golden book_hash values are generated by the first green run; `<DEPTH_DATASET>` placeholder stands offline; `EquityBookState.apply` = REPLACE-on-snapshot with a documented delta-apply seam.)*

---

### References (file:line evidence, re-verified at HEAD `70115f8`)
- `scripts/agent/serializer.py:27-29,40-43,42,47-50,53-55` — float-reject, Decimal-as-string, non-finite-reject, `dumps`, `row_hash` (the only canonicalization/hash path M1 reuses).
- `scripts/agent/journal.py:21,24,28-59,74,99,110,113-114,116,120,126,128` — `_RESERVED`, `JournalCorruption`, replay/tail rule, per-path shared seq+lock, tail repair, `append`, collision raise, seq stamp, row hash, single `fh.write`.
- `scripts/agent/marketdata/base.py:11-15` — `@runtime_checkable MarketDataTransport` (`:11` decorator, `:12` class), `stream -> AsyncIterator[bytes]` (`:13`).
- `tests/lib/fakes.py:9,15,20-31` — `FakeTransport` (`:9`), its `async def stream`+`yield` (`:15`); `FakeClock` (`:20`) to reuse/extend; `FlakyTransport` to ADD here.
- `tests/agent/test_marketdata_transport.py:9-13,17` — `async for` consumption + `isinstance` contract style to mirror.
- `tests/agent/test_no_network_no_creds.py:19,24` — the offline invariant every M1 module keeps green (`databento` not in `sys.modules`; no socket).
- `tests/agent/test_journal_replay.py:77,87,98,157` — truncated-tail/corrupt-line/tampered-hash semantics M1 inherits.
- `tests/__init__.py:11-14`; `conftest.py` — import-root bootstrap (`<repo>/scripts` on `sys.path`); both already exist.
- `scripts/agent/broker/alpaca.py:1-9` — "no SDK at module scope" precedent for `databento.py`.
- spec §5/§5.1/§11/§14 — tier-1 responsibilities, dataset matrix (EQUS.MINI has no mbp-10/status), ET-logic/UTC-persist, data-quality-as-load-bearing-risk.
- M1 plan (`docs/superpowers/plans/2026-06-08-M1-data-tier-implementation-plan.md`) — files/fixtures/tests/invariant map authoritative scope; "tbbo (primary NBBO)" (plan line 29); tier-2 = explicit account blocker (plan line 102).

---

## Post-review corrections (2026-06-09) — adversarial review found 9 confirmed defects (each repro'd)

The build landed green (300 tests), then an independent 5-lens adversarial review (each finding verified by a
runnable repro) confirmed 9 real defects. These corrections are now part of the frozen contract; fixes are TDD
(failing regression test first). 3 are contract-level decisions, the rest are implementation bugs.

**Contract-level decisions (new/changed behavior):**
- **C1 (was finding #3) — sequence anomaly taxonomy.** `SequenceTracker.observe` (status.py) MUST distinguish
  three cases against `expected = last+1`: `got > expected` → `GapReport(kind="gap", gap_size=got-expected)`
  (positive count of missing msgs); `got == last` (repeat) → `GapReport(kind="duplicate", gap_size=None)`;
  `got < last` (backward) → `GapReport(kind="out_of_order", gap_size=None)`. A negative `gap_size` is forbidden.
  `GapReport.kind` enum gains `"out_of_order"|"duplicate"`; the `make_data_quality_alert` cause enum (status.py)
  and the `recorder._detect_sequence` dispatch gain `cause="out_of_order"` and `cause="duplicate"`. Both are
  fail-closed S4 signals, surfaced not swallowed.
- **C2 (was finding #6) — replay verifies EVERY row in order, no key-uniquing.** `replay.py`/`reconcile.py` MUST
  NOT collapse persisted depth rows into a `dict` keyed by `(symbol, vendor_seq)` (last-write-wins masks
  corrupt/duplicate/null-seq rows → S3 hole). Verify each persisted row's `derived_book_hash` against its own
  re-derived hash IN ROW ORDER (carry `(key, recorded_hash, event)` as an ordered list). A colliding/duplicate
  key is itself an anomaly, not silently overwritten.
- **C3 (was finding #2) — book_hash self-canonicalizes (defense-in-depth).** `book_hash._canonical_side` MUST
  requantize `px`→`PRICE_QUANTUM` and `sz`→`SIZE_QUANTUM` before `str(Decimal)`, and `event_row._level_from_row`
  MUST route rebuilt levels through `event._quantize_checked`, so the hash is correct regardless of how a
  `DepthLevel` was constructed (e.g. a hand-authored reference stream in `reconcile_against_fixture`). The normal
  parse→record path already produces canonical strings, so record-path hashes are UNCHANGED — this only closes
  the off-parse-path bypass.

**Implementation bugs to fix (TDD):**
- **C4 (#1/#7) — bar_cache `1d` bucket end is off by ±1h on DST days.** `_bucket_end_utc_str` must advance the
  `1d` end in ET wall-clock (next ET midnight) then convert to UTC, not add a fixed 24h UTC `timedelta`. Add a
  `1d` resample regression test on BOTH transition days (2026-03-08 spring-forward, 2026-11-01 fall-back).
  (Code may already be partially patched; ensure correct + add the missing regression test.)
- **C5 (#4) — bar_cache `_parse_utc` rejects `+00:00`-offset ISO-8601.** Accept any tz-aware ISO-8601 UTC via
  `datetime.fromisoformat` (normalize trailing `Z`), not only a literal `Z` suffix — the repo's own
  `datetime.now(timezone.utc).isoformat()` emits `+00:00`.
- **C6 (#5) — reconcile must escalate a single-stream replay hash divergence.** `reconcile_against_fixture` MUST
  fold each side's `replay_book_hashes(...).ok` into its result as a hard failure (a stale/wrong persisted
  `derived_book_hash` currently reports `ok=True`).
- **C7 (#8) — crossed_book alert path has zero coverage.** Add an integration test: `Recorder.run` over a
  FakeTransport carrying one crossed mbp-10 frame, then assert exactly one `cause="crossed_book"` alert row.
- **C8 (#9) — heartbeat-boundary test is vacuous.** Pin the `>` (strict) timeout semantics: stale at
  `TIMEOUT_MS+1`, not at exactly `TIMEOUT_MS`; assert both sides.
- **C9 (#10) — `reset_to_zero` GapReport.expected_seq is a meaningless `1`.** Set `expected_seq=None` for a
  reset (no meaningful expected), and add a test asserting it.

---

## Round-2 corrections (2026-06-09) — focused 2nd adversarial round found 9 more confirmed defects (each repro'd)

After C1-C9, a focused round 2 (fix-diffs / under-reviewed modules / completeness) confirmed 9 more defects
(2 major, 7 minor). These are now part of the frozen contract; all fixes are TDD with a mutation-verified
regression test. Intent decisions are pinned here so the implementation has no ambiguity.

- **D1 (R2#1, minor) — book_hash must fail loud symmetrically.** `book_hash._canonical_side` MUST use the SAME
  round-trip guard as the parser (call `event._quantize_checked(px, PRICE_QUANTUM, ...)` / `(sz, SIZE_QUANTUM, ...)`,
  which RAISES `PrecisionLoss` on a value that does not round-trip), not a bare `.quantize`. A sub-quantum price
  reaching book_hash off the parse path must RAISE, not silently collapse to `0.0000`. Record-path hashes unchanged.
- **D2 (R2#2, minor→fail-closed) — null vendor_seq must not crash the recorder.** `SequenceTracker.observe` MUST
  guard `got is None` (and the recorder's per-event `except`) so a malformed/null vendor_seq under MONOTONIC
  emits a `data_quality_alert` and the loop CONTINUES — never a `TypeError` that kills `run()`.
- **D3 (R2#3, MAJOR) — prolonged_disconnect measured from a STABLE origin.** Capture the disconnect instant ONCE
  (in `run()`'s except, only when not already disconnected) and measure `down_ms` against that fixed origin on
  EVERY reconnect attempt, so a multi-attempt outage whose cumulative downtime exceeds `alert_after_ms` emits the
  `prolonged_disconnect` alert. Reset the origin only on a successful receive. Test the multi-attempt path.
- **D4 (R2#4, minor) — trades `side` is a closed vocabulary.** Validate `side ∈ {"A","B","N"}` at parse in
  `_build_trade`; an unknown side RAISES `MalformedRecord` (fail-closed, not fail-open). Test the raise.
- **D5 (R2#5, minor) — per-row hash verification keyed by stream ordinal.** Key `rederived_book_hashes` / the
  expected map on a per-stream ordinal (row index), not `(symbol, vendor_seq)`, so null/duplicate vendor_seq rows
  each get their own verified slot (completes C2 for the null-seq case). Test two null-vendor_seq depth rows.
- **D6 (R2#6, MAJOR) — per-symbol sequence tracking.** The Recorder MUST hold `{symbol -> SequenceTracker}`
  (lazily created like `_book_for`) and route `observe()` to the tracker for `ev.provenance.symbol`; every
  `GapReport`/alert attributes the OBSERVED event's symbol. A single shared tracker cross-contaminates the 20-50
  name universe. Test an interleaved two-symbol stream: a gap in symbol B must not be masked by symbol A's seq.
- **D7 (R2#7, minor) — S2 empty-window VWAP guard is LIVE.** `resample` skips empty buckets (no bar — that is the
  NaN/Inf-safe behavior), AND the internal VWAP computation RAISES `EmptyWindowVWAP` if ever called with zero
  volume/count. Make the raise reachable + test it asserts the raise (the named S2 test must exercise the raise,
  not `== []`).
- **D8 (R2#8, minor) — no dead `no_seq_semantics`.** A NONE-`SequencePolicy` tracker performs no gap detection
  and returns `None` (no false gaps). Remove `no_seq_semantics` from the LIVE `GapReport.kind` enum and §G unless
  a consumer needs it; the NONE-policy downgrade is recorded once via `status`, not per-observe. No dead enum value.
- **D9 (R2#9, minor) — connected-quiet heartbeat: wire or scope out, no dead code.** If the connected-but-quiet
  `heartbeat_timeout` path can be exercised DETERMINISTICALLY offline (injected clock + a quiet-stream fake), wire
  `heartbeat.check`/`stale_symbols` into the stream loop and test it. Otherwise scope connected-quiet heartbeat
  detection to a later (live-feed) milestone IN WRITING and remove the dead wiring — no silent unreachable feature.

---

## Round-3 corrections (2026-06-09) — convergence round found 3 more (all MAJOR, integration-level; each repro'd)

A 3rd round (fix-diffs / end-to-end integration / cross-cutting) found 3 major defects that unit tests missed.
Now part of the frozen contract; fixes are TDD with mutation-verified regression tests.

- **E1 (R3#1, MAJOR) — `prolonged_disconnect` is ONE-SHOT per outage.** The D3 origin-stabilisation made `down_ms`
  exceed `alert_after_ms` on EVERY reconnect attempt, so the alert re-fired each attempt (a 10-min outage → ~19
  duplicate rows). Add a dedup flag (`self._prolonged_alerted`, mirroring the heartbeat `_stale_alerted` dedup):
  emit `prolonged_disconnect` at most once per outage; clear the flag on a successful receive (next to the origin
  reset). Regression: a multi-attempt single outage crossing the threshold several times emits EXACTLY ONE alert.
- **E2 (R3#2, MAJOR) — replay/reconcile must SKIP non-event rows.** When no `alert_writer` is injected, the
  recorder routes `data_quality_alert` rows (which have NO `schema` field) into the events stream. `replay_book_hashes`
  calls `from_row` unconditionally → `MalformedRecord('missing schema')` crashes the whole replay. Guard:
  `if 'schema' not in row: continue` before `from_row` (depth-hash verification applies only to schema-bearing
  event rows). `reconcile_against_fixture` inherits the fix. Regression: a stream mixing a depth row + an alert
  row replays/reconciles cleanly.
- **E3 (R3#3, MAJOR) — one malformed frame must NOT kill ingest for the universe.** `Recorder.run()`'s per-frame
  body MUST catch the parse fail-closed family (`MalformedRecord`/`NonFinitePrice`/`PrecisionLoss`) for a SINGLE
  frame, emit a loud `cause="malformed_record"` `data_quality_alert` (with the offending detail), and CONTINUE to
  the next frame — never re-raise out of `run()`. This is the contract-consistent reading of §14 "fail loud, not
  silent": the anomaly is recorded loudly in the alert stream, and the corrupt frame is never persisted as valid,
  but one bad frame from one symbol does not stop recording every symbol. (Completes D2 for the non-null malformed
  case — D2 was only implemented/tested for `vendor_seq=None`, which `parse` accepts.) Regression: a mid-stream
  non-null malformed `vendor_seq` AND a malformed price each → surviving frames recorded + a `malformed_record`
  alert row written; `run()` does not raise.

---

## Round-4 corrections (2026-06-09) — convergence round found 1 major + 1 minor (each repro'd)

- **F1 (R4#1, MAJOR) — per-frame fail-closed containment covers the FULL pipeline, not just parse().** E3 wrapped
  only `parse()`; a frame that parses fine but raises `BookStateError` in the apply/persist path (e.g. a
  mismatched `instrument_id` from symbol recycling / a corporate-action remap — a real vendor condition, since
  equity symbol strings are not stable ids) still propagated out of `run()` and killed ingest for the whole
  universe. The per-frame recoverable family is `{MalformedRecord, NonFinitePrice, PrecisionLoss, BookStateError}`;
  any of these for a SINGLE frame → emit a loud `data_quality_alert` (`cause="malformed_record"` for the parse
  family, `cause="book_state_error"` for `BookStateError`, carrying `symbol`+`instrument_id` where available) and
  CONTINUE. `TransportDisconnected` still routes to reconnect; ALL OTHER exceptions still propagate (fail-fast on
  a programmer error — do NOT use a bare `except`). Regression: a 3-frame single-symbol stream whose middle frame
  has a mismatched `instrument_id` → 2 good frames recorded + exactly 1 alert + `run()` does not raise.
- **F2 (R4#2, minor) — test the §K EQUS.MINI-pollution regression guard.** The guard (verify() raises
  `UnverifiableSchema` if EQUS.MINI ever lists `mbp-10`/`status`) is correct in production but had NO test
  exercising the polluted branch (green-washed). Add a TDD test feeding a polluted schema map (EQUS.MINI with
  `mbp-10`, and a second case with `status`) asserting `UnverifiableSchema` is raised — making the regression
  detector load-bearing under mutation.

---

## Round-5 corrections (2026-06-09) — final convergence gate found 1 major + 3 green-washed test gaps

- **G1 (R5#1, MAJOR) — advance the seq tracker only for PERSISTED frames.** `_process_event` ran
  `_detect_sequence(ev)` BEFORE `_maybe_book_hash(ev)`; when a frame raises `BookStateError` (the F1 path), the
  per-symbol `SequenceTracker` baseline had already advanced to seq N but no journal row was written → the tracker
  baseline diverges from the journal (a later reconnect mis-fires `reset_to_zero`, a real gap reports the wrong
  `gap_size`, and the S3 invariant "every tracked seq has a journal row" breaks). Fix: reorder so
  `_detect_sequence(ev)` runs AFTER `_maybe_book_hash(ev)` succeeds and before `write_event` — the tracker only
  advances for frames that actually persist. Regression: a frame that trips `BookStateError` must NOT advance the
  tracker baseline (the next good frame's gap is reported correctly).
- **G2/G3/G4 (minor, green-washed) — production correct, add load-bearing tests.** (G2) book_hash drops a
  STANDALONE unique-price zero-size padding level — every existing test masked it via a same-price coalesce-sum;
  add a standalone unique-price zero-size case asserting padding-present == padding-omitted. (G3) the negative
  depth-level size guard (`event.py`) has no test — add an mbp-10 level with `sz<0` asserting `MalformedRecord`.
  (G4) `from_row`'s missing-required-field raise (the §B2 BLOCKER-2 seam) has no test — add a flat row missing a
  required field asserting `MalformedRecord`. Each must go RED when its production guard is mutated away.
