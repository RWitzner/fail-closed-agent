# M2 (Market-state) — FROZEN, READY-TO-BUILD CONTRACT

**Status:** FROZEN (READY-TO-BUILD) — internal architect-panel + 5-lens adversarial critique, then **external review round 1 (GPT) applied** (see "External review round 1" at the end).
**Verified against HEAD:** `3e270eb` (M1 tier-1 + tier-2(2a) green, 395 tests, gates OFF)
**Style/format anchor:** `docs/superpowers/specs/2026-06-09-M1-tier1-contract.md`
**Scope decisions confirmed by Robin (2026-06-09):** `OPEN_CLOSE_BUFFER_S=0` (OFF); live CA CONFIRMED-clear DISABLED until upstream independence is human-verified (see §M).

A build agent handed only this document + the repo can TDD any one component below without guessing an interface. Every reuse claim cites a real `file:line` read at HEAD `3e270eb`.

> **Polymarket caveat (verified):** spec §15 names `scripts/auto_trader/market_status.py` / `market_status_cache.py` — these **DO NOT EXIST in this repo** (`ls scripts/` → no `auto_trader/`). They are the conceptual pattern ONLY. This contract specifies the M2 pattern FULLY from scratch; a build agent has only this contract + this repo.

---

## 0. Scope, ground rules, verified repo facts

**M2 is the PURE DECIDER + status/halt/CA ledger + corporate-action fail-closed layer** (spec §5 Tier 2, §8 items 4–5, §9 S7, §10 M2 row). M2 **submits no orders, mints no preflight token, opens nothing**. It produces the *tradability READ* that M4's `can_open()` and M5's `execution_preflight` will later consume — defined cleanly here, NOT built. Committed run-gates stay OFF (`config/agent_rules.json` `enabled=false`, `paper_trading.enabled=false`; `config/risk_rules.json` `live_trading.enabled=false` — verified at `config/agent_rules.json:1-2`, `config/risk_rules.json:1-3`, asserted by `tests/agent/test_config_canary.py:31-35`).

**Offline purity (hard constraint, non-negotiable).** No network, no credential reads. `exchange_calendars` is **NOT installed** (`.venv/lib/python3.14/site-packages` = `databento`, `databento_dbn`, `numpy`, `pandas` only; Python is **3.14.4**) and `requirements.txt:6` carries the placeholder `#   M2 (session/calendar gate): exchange_calendars==<pin>` (only `databento==0.79.0` at `requirements.txt:8` is active). An **unguarded top-level `import exchange_calendars` in ANY M2 module crashes `python3 -m unittest discover` at COLLECTION time** (`ModuleNotFoundError`) and fails all 395 tests, not one. Therefore the heavy lib lives behind an injectable `ScheduleProvider` seam, lazily imported only inside the credentialed/live provider build path — mirroring `DatabentoTransport` (`scripts/agent/marketdata/databento.py:82-89`). The offline suite runs against a **pinned stdlib-`zoneinfo` fixture calendar** and never touches the lib.

**Pure vs IO boundary (scoped).** The calendar query, the `TradabilityDecider`, the CA `cross_validate` function, and `MarketStateCache.get` are PURE functions of explicit injected inputs (no wall clock, hidden state, or network). `CorporateActionFeed.adjustments_for`/`is_blacked_out` are deterministic **only given fixed fetcher outputs** (offline = scripted fakes; live = a credentialed fetcher behind the lazy seam).

**Net-new modules** (all under `scripts/agent/`, which already imports via the `tests/__init__.py` + `conftest.py` shim): `market_calendar.py`, `market_state.py`, `market_state_cache.py`, `corporate_actions.py`, `status_ledger.py`, `session_liveness.py`. **Net-new fixture dirs:** `tests/fixtures/calendar/`, `tests/fixtures/corporate_actions/`, `tests/fixtures/market_state/`. None exist today.

### Verified repo facts (read at HEAD `3e270eb`)

- **ONE canonicalization path** = `agent.serializer.dumps`/`row_hash` (`serializer.py:47,53`): `dumps(row)` runs `_reject_floats(row)` then `json.dumps(row, sort_keys=True, separators=(",",":"), default=_default)`. `_reject_floats` (`serializer.py:27-36`) recursively raises `ValueError("float not allowed in serialized rows; use Decimal")` (`:29`) on ANY float in dict keys/values/list/tuple. `_default` (`serializer.py:39-44`) renders a **finite** `Decimal` via `str()`, raises on a non-finite Decimal (`:42`), and raises `TypeError(f"type not serializable: {type(obj).__name__}")` (`:44`) on anything else — **including a `frozenset`/`set`**. `row_hash` (`serializer.py:53-55`) = sha256 hex of `dumps(row)`. **M2 adds NO new hash/serialize routine.**
- Money newtypes `BrokerUSD`/`ModeledUSD` are distinct `Decimal` subclasses (`serializer.py:15-24`); `as_broker_usd(value)` raises `TypeError("broker ledger field requires BrokerUSD")` unless `value` is `BrokerUSD` (`serializer.py:58-61`). **`BrokerUSD` is a USD MONEY type, not a share-quantity type** — `OrderIntent.qty` is a plain `Decimal` (`broker/base.py:21,29`). M2's broker-adjust detector compares a plain **Decimal share count**, never `BrokerUSD`.
- Journal append-path: `JournalWriter.append(event_type, fields=None, *, decision_id=None, order_id=None) -> dict` (`journal.py:110`). `_RESERVED = {"event_type","run_id","seq","hash","decision_id","order_id","ts_utc"}` (`journal.py:21`); a colliding field raises `ValueError(f"fields collide with reserved keys: {sorted(collisions)}")` (`journal.py:112-114`). **Stamping order** (`journal.py:115-129`): under `self._state.lock` → `seq += 1`, `row = dict(fields)`, set `event_type`/`run_id`/`seq`/`ts_utc=self._clock()`, optional `decision_id`/`order_id`, THEN `row["hash"] = row_hash(row)`, then `fh.write(dumps(row)+"\n")` (one line). Per-stream monotonic `seq` + writer lock are keyed by **resolved path** in module registry `_streams` (`journal.py:70-81`). `_repair_truncated_tail` (`journal.py:99-108`) runs on every open. `replay(path)` (`journal.py:28-59`) hash-verifies every row, drops ONLY a non-newline-terminated trailing line (`:55-56`), raises `JournalCorruption` (`journal.py:24`) on a newline-terminated corrupt/hash-mismatch line (`:57`).
- `recorder.persistence.EventWriter` WRAPS `JournalWriter` (`persistence.py:52-64`); `EventWriter.record(event_type, fields, *, decision_id=None, order_id=None) -> dict` (`persistence.py:89-99`) delegates to `JournalWriter.append`. Stream tags: `STREAM_EVENTS="events"`, `STREAM_DATA_QUALITY="data_quality_alerts"`, `STREAM_STATUS="status"` (`persistence.py:47-49`); `JournalCorruption` re-exported in `__all__` (`persistence.py:43`). `replay_stream(path)` (`persistence.py:102-108`) delegates to `journal.replay`. The recorder **SHARES the injected `run_id`** (`persistence.py:26-29`). Single-file per stream, NO rotation (`persistence.py:23-24`). **`STREAM_STATUS` is defined/exported but has ZERO producers in `scripts/` today (`grep -rn STREAM_STATUS scripts/` → only the definition) — M2 is the FIRST writer of the `status` stream; there is no pre-existing status-row schema to conform to.**
- `recorder.event_row` flat seam: `to_row(event, *, derived_book_hash=None) -> dict` (`event_row.py:77-108`), `from_row(row) -> object` (`event_row.py:117`); `from_row(to_row(ev)) == ev` for every event type (`event_row.py:12`) **because `to_row` persists the FULL provenance prefix** (`event_row.py:46-56`); Decimals rebuilt via `Decimal(str(value))`; `_require` raises `MalformedRecord(f"missing required flat field {key!r}")` (`event_row.py:111-113`).
- Typed events are `@dataclass(frozen=True)` with a composed frozen `Provenance` (`event.py:52-60`); **`vendor_seq` rename lesson** (`event.py:14-18` BLOCKER 1; `Provenance.vendor_seq: Optional[int]` `event.py:57`). Closed-vocab `TRADE_SIDES = frozenset({"A","B","N"})` (`event.py:33`), unknown is FATAL. Graded exception tree rooted in `ValueError`: `UnknownSchema` (`event.py:37`), `MalformedRecord` (`event.py:41`), `NonFinitePrice` (`event.py:45`), `PrecisionLoss(MalformedRecord)` (`event.py:49`). Quanta: `PRICE_QUANTUM=Decimal("0.0001")` (`event.py:30`), `SIZE_QUANTUM=Decimal("1")` (`event.py:31`), `ROUND_HALF_EVEN` (`event.py:22`). Typed accessors `_require`/`_int_field`/`_str_field` reject wrong types incl. bool-as-int.
- `book_state.py`: `BookStateError` (`book_state.py:26`); `BookSnapshot` frozen with `crossed: bool` surfaced not auto-corrected (`book_state.py:31-37`, `:11-13`); `_check_identity(prov)` raises on `symbol`/`instrument_id` mismatch on every apply (`book_state.py:55-64`).
- `book_hash.py`: imports `from agent.serializer import row_hash` (`book_hash.py:22`, "reuse the ONE M0 hashing primitive"); `BOOK_HASH_VERSION=2` (`book_hash.py:31`); `canonical_book_payload(snapshot)` exposes `{"v": BOOK_HASH_VERSION, ...}` with `"v"` first (`book_hash.py:90-100`); re-runs `_quantize_checked` as defense-in-depth (`book_hash.py:62-65`).
- ET/DST mechanism: `ET = ZoneInfo("America/New_York")` (`bar_cache.py:26`), `UTC = timezone.utc` (`bar_cache.py:27`); `et_session_date(ts_utc_iso)` via `dt_utc.astimezone(ET)` (`bar_cache.py:31-43`); `_parse_utc` accepts `Z` and `+00:00`, rejects naive (`bar_cache.py:46-65`). DST-correct 1-day boundary: advance the ET **calendar date** then build a fresh `datetime(..., tzinfo=ET).astimezone(UTC)` (`bar_cache.py` `_bucket_end_utc_str`, the `interval=="1d"` branch) — NEVER a fixed 24h add. Header (`bar_cache.py:3-5`): stdlib `zoneinfo` only; calendar-awareness is M2.
- `recorder.status`: `EQUS_MINI_STATUS_DOWNGRADE` (`status.py:31-34`) — the WRITTEN record that "halt/LULD/SSR status primary source is broker (Alpaca) + exchange_calendars (M2). No silent fallback." `HeartbeatMonitor(*, timeout_ms, clock)` (`status.py:162`) is session-UNAWARE: `check` uses **strict** `now_ms - last > self._timeout_ms` (`status.py:178`); `stale_symbols(now_ms)` (`status.py:188-194`). `SequenceTracker` with `SequencePolicy` (`status.py:37-39,51`); EQUS.MINI policy=NONE → `observe()` returns None (`status.py:77-78`), so session boundaries are IRRELEVANT to the seq path. `make_data_quality_alert(*, cause, symbol=None, detail=None, down_ms=None, reconnect_epoch=0)` (`status.py:197-225`).
- The recorder consults `stale_symbols(now_ms)` at exactly TWO sites, both of which **unconditionally** call `_emit_alert(cause="heartbeat_timeout", symbol=symbol)`: `_check_connected_quiet` (`recorder.py:327-338`) and `_reconnect` (`recorder.py:340-370`). The injected ms clock is `self._clock.now_ms()` (`recorder.py:316`). `Recorder.__init__` (`recorder.py:154-168`) takes keyword-only injected deps (`clock`, `sequence_tracker`, `heartbeat`, `alert_writer=None`) — the seam point for an optional `liveness=None`. The seq path is `_detect_sequence` (`recorder.py:323`).
- Config: `tighten_only_merge(base, authoritative)` (`config.py:24-43`) — dicts recurse keeping base keys (`:25-32`), two bools → `base and authoritative` (`:33-34`), two non-bool numerics → `min(base, authoritative)` (`:35-41`), else keep base (`:42-43`). `rules_hash(config)` (`config.py:17-21`) = sha256 of `json.dumps(config, sort_keys=True, separators=(",",":"), allow_nan=False)` — a SEPARATE path from the serializer. Gates identity-strict: `strict_bool(value) = value is True` (`gates.py:10-11`); `opening_allowed`/`live_allowed` (`gates.py:23-30`). Committed config assembled inline as `{"agent_rules": load(...), "risk_rules": load(...)}` (`test_config_canary.py:23-27`) — **no runtime loader exists**; tighten-only canary `test_armed_overlay_cannot_loosen_committed_via_tighten_only` (`test_config_canary.py:60-64`).
- Lazy-SDK seam: `DatabentoTransport.__init__(config, *, raw_source=None, credentials_loader=None)` (`databento.py:42-48`); `stream()` delegates to the injected `raw_source` offline (`databento.py:82-85`, zero SDK import, zero socket); only `_build_real_client()` lazily imports databento on the credentialed branch (`databento.py:93-102`, raises `NotImplementedError` offline). `MarketDataTransport` is a `@runtime_checkable Protocol` (`marketdata/base.py`).
- `FakeClock(start_ms=0)` with `now_ms()`/`advance(ms)` (`tests/lib/fakes.py:83-94`) — the SAME injected ms clock `HeartbeatMonitor` uses. M2 introduces no new clock.
- `test_no_network_no_creds.py`: the **sys.modules-purity class `TestNoSdkImported`** imports `agent.broker.alpaca` + `agent.marketdata.base` and asserts `"alpaca"`/`"databento"` not in `sys.modules` (`:14-21`); a **separate class `TestNoSocketOpened`** imports `broker.base`/`execution_preflight`/`serializer` and patches `socket.socket` to raise (`:23-40`). Neither imports any M2 module nor mentions `exchange_calendars`. M2 MUST extend `TestNoSdkImported`.

**Calendar pin (frozen, EMPIRICALLY VERIFIED):** `exchange_calendars==4.13.2`. Verified in an isolated venv with `pandas==3.0.3` / `numpy==2.4.6` / Python 3.14.4: `ec.get_calendar('XNYS').is_session('2026-11-26') -> False` (Thanksgiving), `session_close('2026-11-02') -> 21:00Z` (EST), `session_close('2026-11-27') -> 18:00Z` (half-day 13:00 ET, EST). **`4.5.6` was tested and is BROKEN under pandas 3** (date lookups raise `DateOutOfBounds` with `<exception str() failed>`); do NOT pin it. The `requirements.txt` line stays a COMMENT until the live path is wired (mirrors how databento was carried before M1 tier-2). **MIC:** `XNYS` (NYSE). Both are provenance strings, never tighten-able thresholds (§G).

---

## A. `scripts/agent/market_calendar.py` — `ScheduleProvider` seam + pure ET schedule query

Exact analogue of `DatabentoTransport`: a Protocol with a pinned-fixture offline impl (stdlib `zoneinfo`, no heavy lib) and an `exchange_calendars`-backed impl whose `import exchange_calendars` lives ONLY inside its `_build_calendar()` method, reached only on the credentialed/live path. The query API is a **pure deterministic function of `(session_date_et | ts_utc, fixture-schedule)`**.

```python
# scripts/agent/market_calendar.py
from dataclasses import dataclass
from datetime import datetime, time, timezone
from enum import Enum
from typing import Dict, Optional, Protocol, runtime_checkable
from zoneinfo import ZoneInfo            # STDLIB — DST-correct; NO exchange_calendars in the offline core

ET = ZoneInfo("America/New_York")        # reuse bar_cache.py:26 mechanism exactly
UTC = timezone.utc

EXCHANGE_CALENDARS_PIN = "4.13.2"        # provenance (empirically verified vs pandas 3.0.3 / py3.14)
CALENDAR_MIC = "XNYS"                    # provenance

# Regular ET session boundaries. CODE CONSTANTS, NOT overlayable (§G):
PRE_OPEN_ET = time(4, 0)                 # 04:00 ET pre-market open
RTH_OPEN_ET = time(9, 30)               # 09:30 ET
RTH_CLOSE_ET = time(16, 0)              # 16:00 ET
EARLY_CLOSE_ET = time(13, 0)            # 13:00 ET half-day close
POST_CLOSE_ET = time(20, 0)            # 20:00 ET post-market close (17:00 on half-days; fixture-supplied)

class SessionPhase(str, Enum):           # closed vocabulary (event.TRADE_SIDES pattern, event.py:33)
    PRE = "pre"
    RTH = "rth"                           # CONTINUOUS session [09:30:00, 16:00:00) — owns the full window
    POST = "post"
    CLOSED = "closed"
    AUCTION = "auction"                   # halt-RESUMPTION re-opening auction ONLY (vendor-signalled, §B); NOT a clock window
    UNKNOWN = "unknown"                   # out-of-coverage date / failed lookup -> caller maps to NOT_TRADABLE (S7-spirit)

class CalendarError(ValueError):
    """Calendar query failure — fail-closed base (subclasses ValueError for broad catch)."""
class UnknownSessionDate(CalendarError):
    """A queried ET date is outside the provider's pinned coverage -> caller degrades to UNKNOWN/CLOSED."""

@dataclass(frozen=True)
class SessionSchedule:
    """Pure, immutable, hashable schedule for ONE ET session date. *_utc are UTC ISO-8601
    (persist-UTC, spec §11); None where a window does not exist (holiday/weekend ->
    is_trading_day=False, all windows None). *_utc are built via construct-in-ET-then-
    astimezone(UTC) (bar_cache.py '1d' branch) so the close lands at 20:00Z (EDT) / 21:00Z (EST)
    correctly across a DST flip."""
    session_date_et: str        # "YYYY-MM-DD" ET
    is_trading_day: bool        # False for weekend/holiday -> every window below is None
    is_early_close: bool        # True on a half-day (RTH close = 13:00 ET)
    pre_open_utc: Optional[str]
    rth_open_utc: Optional[str]
    rth_close_utc: Optional[str]    # 13:00 ET on a half-day, else 16:00 ET — DST-correct UTC
    post_close_utc: Optional[str]

@runtime_checkable
class ScheduleProvider(Protocol):
    """Injectable seam (mirrors marketdata.base.MarketDataTransport @runtime_checkable Protocol).
    OFFLINE = FixtureScheduleProvider; LIVE = ExchangeCalendarsScheduleProvider whose
    `import exchange_calendars` lives ONLY in its build path."""
    def schedule_for(self, session_date_et: str) -> SessionSchedule: ...
    def is_trading_day(self, session_date_et: str) -> bool: ...
    def calendar_pin(self) -> str: ...       # provenance string carried into status rows (§E)

class FixtureScheduleProvider:
    """OFFLINE, stdlib-only. Loads a pinned calendar fixture (§H.1) + zoneinfo. Imports NO
    exchange_calendars (keeps test_no_network_no_creds green). A queried date NOT in the fixture
    raises UnknownSessionDate (fail-closed: never a silent 'assume open').

    DST-gap guard (DET-6): when constructing each *_utc, assert the ET boundary round-trips
    (datetime(...,tzinfo=ET).astimezone(UTC).astimezone(ET) == constructed) and raise CalendarError
    otherwise, so a malformed fixture boundary that lands in the spring-forward skipped hour cannot
    silently fold-shift."""
    def __init__(self, fixture: dict, *, pin: str) -> None: ...   # pure dict; no client, no creds
    def schedule_for(self, session_date_et: str) -> SessionSchedule: ...
    def is_trading_day(self, session_date_et: str) -> bool: ...
    def calendar_pin(self) -> str: ...                            # e.g. "fixture:XNYS-2026-v1"

class ExchangeCalendarsScheduleProvider:
    """LIVE/credentialed. The ONLY `import exchange_calendars` in M2 lives inside _build_calendar(),
    reached solely on the live path — so importing this module offline pulls nothing heavy into
    sys.modules (mirrors DatabentoTransport._build_real_client, databento.py:93-102)."""
    def __init__(self, *, mic: str = CALENDAR_MIC, pin: str = EXCHANGE_CALENDARS_PIN) -> None: ...
    def _build_calendar(self):           # pragma: no cover - live
        """Lazily `import exchange_calendars`; build ec.get_calendar(mic). Raises NotImplementedError
        in M2 offline scope; never reached by an offline test."""
        raise NotImplementedError("exchange_calendars-backed provider lands when the live path is wired")
    def schedule_for(self, session_date_et: str) -> SessionSchedule: ...  # pragma: no cover - live
    def is_trading_day(self, session_date_et: str) -> bool: ...           # pragma: no cover - live
    def calendar_pin(self) -> str: ...

class MarketCalendar:
    """Pure query facade over an injected ScheduleProvider. No clock, no IO beyond the provider;
    deterministic given the provider."""
    def __init__(self, provider: ScheduleProvider) -> None: ...
    def session_date_for(self, ts_utc_iso: str) -> str:
        """UTC instant -> ET session date "YYYY-MM-DD" (reuses bar_cache.et_session_date semantics;
        accepts 'Z' and '+00:00', rejects naive)."""
        ...
    def phase_at(self, ts_utc_iso: str) -> SessionPhase:
        """PURE function of (instant, schedule). Convert ts_utc -> ET, classify against the schedule:
          - not is_trading_day -> CLOSED.
          - within [rth_open, rth_close) -> RTH.   (CONTINUOUS session owns the full window; the
            open/close cross is a point-in-time event, not a multi-minute suspension — MR-1.)
          - within [pre_open, rth_open) -> PRE.    within [rth_close, post_close) -> POST.
          - otherwise -> CLOSED.
        UnknownSessionDate -> caller (the TradabilityInputs builder) sets session_phase=UNKNOWN and
        reason 'calendar_unknown' (§B). DST-correct: comparison is on the parsed UTC instant against
        the schedule's *_utc fields. NOTE: SessionPhase.AUCTION is NEVER produced here — it is reserved
        for the halt-resumption re-opening auction the decider derives from vendor halt state (§B)."""
        ...
```
**Raises:** `UnknownSessionDate`/`CalendarError` (date outside coverage / DST-gap boundary — caller degrades to UNKNOWN); `NotImplementedError` (live `_build_calendar` offline). No exception on the offline happy path. **Offline-purity binding:** `import exchange_calendars` appears at exactly ONE line, inside `ExchangeCalendarsScheduleProvider._build_calendar`; `test_no_network_no_creds` is EXTENDED (§J) to assert `"exchange_calendars" not in sys.modules` after importing this module. **DST binding (frozen):** every ET boundary is `datetime(y,m,d,H,M,tzinfo=ET).astimezone(UTC)`; RTH close 16:00 ET → **20:00 UTC (EDT) / 21:00 UTC (EST)** — the EST/EDT 1h delta a fixed-offset bug misses (the discriminating axis, §J).

---

## B. `scripts/agent/market_state.py` — `SessionState` + halt/LULD/SSR + pure `TradabilityDecider`

Pure decider: ALL inputs injected via fetcher seams (calendar phase, vendor/broker status, NBBO, CA-blackout/freeze). No clock, no IO, no float. `SessionState` (phase, incl. HALTED) and `Tradability` (the READ M4/M5 consume) are **two distinct types** so a caller can never read "RTH" as permission to open.

```python
# scripts/agent/market_state.py
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import FrozenSet, Optional, Tuple
from agent.market_calendar import SessionPhase

# --- closed vocabularies (frozenset membership; out-of-vocab -> FATAL/most-restrictive, event.py:33) ---
class SessionState(str, Enum):
    PRE = "pre"; RTH = "rth"; POST = "post"; CLOSED = "closed"; AUCTION = "auction"
    HALTED = "halted"; UNKNOWN = "unknown"
SESSION_STATES: FrozenSet[str] = frozenset(s.value for s in SessionState)

class HaltState(str, Enum):
    NONE = "none"
    HALTED = "halted"             # any trading halt (news/regulatory/volatility code)
    PAUSED_LULD = "paused_luld"   # LULD 5-minute trading pause
    RESUMING = "resuming"         # halt clearing via a re-opening (indicative-price) auction (MR-5)
    UNKNOWN = "unknown"           # status feed unavailable/stale -> FAIL-CLOSED == HALTED-equivalent

class LuldState(str, Enum):
    NORMAL = "normal"             # inside the LULD band
    LIMIT = "limit"               # 5-minute limit state (NBBO pinned at a band edge)
    PAUSED = "paused"             # LULD trading pause triggered
    UNKNOWN = "unknown"           # band unknown/stale -> FAIL-CLOSED restrictive

class SsrState(str, Enum):
    INACTIVE = "inactive"
    ACTIVE = "active"             # Reg SHO Rule 201 in effect; rest-of-day + next trading day
    UNKNOWN = "unknown"           # FAIL-CLOSED: treat as ACTIVE for short-side decisions

class LuldTier(str, Enum):
    TIER1 = "tier1"               # S&P 500 / Russell 1000 / select ETPs
    TIER2 = "tier2"               # all other NMS securities
    UNKNOWN = "unknown"

class HaltReason(str, Enum):      # vendor/SIP-driven closed vocab; carried as provenance, not interpreted
    NONE = "none"; NEWS_PENDING = "news_pending"; NEWS_DISSEMINATION = "news_dissemination"
    LULD_PAUSE = "luld_pause"; VOLATILITY = "volatility"; REGULATORY = "regulatory"; UNKNOWN = "unknown"

class Tradability(str, Enum):
    """The single tradability READ M4.can_open / M5.execution_preflight consume (spec §8.3-4). M2 only
    PRODUCES this; it never acts on it."""
    TRADABLE = "tradable"             # continuous two-sided trading permitted (caller's own gates still apply)
    REDUCE_ONLY = "reduce_only"       # may decrease an existing position only (never open/increase)
    NOT_TRADABLE = "not_tradable"     # blocked entirely

# severity rank: LARGER == MORE restrictive; tighten-only merge takes the MAX rank (NEVER min()-merged, §G)
_SEVERITY = {Tradability.TRADABLE: 0, Tradability.REDUCE_ONLY: 1, Tradability.NOT_TRADABLE: 2}

# OPEN/CLOSE AVOIDANCE POLICY (MR-1): NOT a microstructure auction boundary. Continuous RTH owns the
# full [09:30:00,16:00:00) session. This is a CONSERVATIVE AGENT POLICY buffer, default OFF.
# DECIDED (Robin, 2026-06-09): stays 0/OFF in M2 — the decider models only market-state FACTS; open/close
# avoidance is a deliberate strategy/risk concern (M4/M7), not a hardcoded NOT_TRADABLE window here.
OPEN_CLOSE_BUFFER_S = 0           # 0 == OFF (M2 ships honest); a non-zero value is a deliberate policy knob

class MarketStateError(ValueError):
    """Decider invariant violation (out-of-vocabulary state) -> FATAL (fail-closed)."""

@dataclass(frozen=True)
class LuldBand:
    """Reg-NMS LULD price band. ALL Decimal (serializer rejects float). Bands are VENDOR/SIP-DRIVEN and
    tier/time dependent; M2 STORES the reported band, it does NOT compute the % (unknown band -> caller
    treats as most-restrictive). Bands are double-width during certain SIP/Plan-defined intervals near the
    open and/or close (exact minutes are SIP-defined and have been amended over time — M2 does NOT encode
    them) — M2 consumes ONLY the vendor `doubled` flag and computes NO % and NO window (MR-4/MED-4)."""
    reference_px: Decimal
    lower_px: Decimal
    upper_px: Decimal
    tier: LuldTier
    doubled: bool

@dataclass(frozen=True)
class Nbbo:
    """Injected two-sided NBBO read (from M1 book_state best_bid/best_ask). REPLACES the M1-era boolean
    orderbook flag with a LIVE two-sided check. Prices Decimal (float-reject)."""
    symbol: str
    best_bid: Optional[Decimal]
    best_ask: Optional[Decimal]
    bid_sz: Optional[Decimal]
    ask_sz: Optional[Decimal]
    ts_utc: str
    @property
    def two_sided(self) -> bool:
        """True iff BOTH sides present with positive size AND not crossed/locked (best_bid < best_ask).
        Anything else -> False (fail-closed: one-sided or crossed book is NOT continuously tradable)."""
        ...

@dataclass(frozen=True)
class StatusFlags:
    """Injected broker/vendor status read (Alpaca + calendar; EQUS.MINI has no status schema,
    status.py:31). Defaults are UNKNOWN == fail-closed. SSR is an INJECTED broker/SIP determination —
    M2 does NOT compute the 10% trigger (MR-3); prior_close is provenance context only."""
    symbol: str
    halt: HaltState = HaltState.UNKNOWN
    halt_reason: HaltReason = HaltReason.UNKNOWN
    luld: LuldState = LuldState.UNKNOWN
    luld_band: Optional[LuldBand] = None
    ssr: SsrState = SsrState.UNKNOWN
    prior_close: Optional[Decimal] = None   # SSR context ONLY (never used to compute SSR); Decimal-as-string
    source: str = "unknown"                 # provenance: "alpaca" | "calendar" | "unknown"

@dataclass(frozen=True)
class TradabilityInputs:
    """The COMPLETE injected input set — the decider reads nothing else (pure). The BUILDER of this
    struct is responsible for catching calendar UnknownSessionDate and setting session_phase=UNKNOWN
    (never a tradable phase) — see §A.phase_at and the builder contract below."""
    symbol: str
    instrument_id: int             # durable numeric identity (book_state identity discipline)
    ts_utc: str                    # the instant being decided (ISO-8601 UTC)
    session_date_et: str           # ET session date for ts_utc (LOW-2): the builder supplies it via
                                   #   MarketCalendar.session_date_for(ts_utc) so decide() stays PURE (no
                                   #   calendar call inside the decider); flows straight into Verdict.session_date_et
    session_phase: SessionPhase    # from MarketCalendar.phase_at (UNKNOWN on a failed lookup)
    status: StatusFlags            # from broker/vendor (UNKNOWN fields fail-closed)
    nbbo: Optional[Nbbo]           # from M1 book_state (None == no live book == NOT_TRADABLE)
    ca_blackout: bool              # from corporate_actions (§D; True == symbol blacked out)
    frozen: bool = False           # from corporate_actions broker-adjust detector (§D); True == hard freeze

@dataclass(frozen=True)
class Verdict:
    """The frozen tradability verdict the cache stores and M4/M5 consume. Identity+economic only;
    wall-clock excluded from canonical_verdict_payload (§E). reasons is an ORDERED, SORTED tuple of
    machine-readable blocker codes."""
    symbol: str
    instrument_id: int
    session_state: SessionState
    tradability: Tradability
    halt: HaltState
    luld: LuldState
    ssr: SsrState
    two_sided_nbbo: bool           # replaces the legacy boolean orderbook flag
    short_allowed: bool            # False if SSR active/unknown OR tradability != TRADABLE (Reg SHO 201, MR-3)
    reasons: Tuple[str, ...]       # e.g. ("ca_blackout","session_auction","status_unknown")
    ca_blackout: bool
    session_date_et: str

def merge_severity(a: Tradability, b: Tradability) -> Tradability:
    """Tighten-only: return the MORE restrictive (higher _SEVERITY rank). Total order -> associative,
    order-independent. Mirrors config tighten_only_merge's never-loosen posture at the verdict level."""
    ...

class TradabilityDecider:
    """PURE class. NO IO, NO clock, NO float, NO network. Every input is INJECTED via TradabilityInputs.
    Deterministic: identical inputs => identical Verdict (mirrors book_state purity). M2 computes NO
    LULD percentage and NO SSR 10% trigger — it ingests the vendor band/flag and fails closed on absence."""
    def decide(self, inputs: TradabilityInputs) -> Verdict:
        """Resolve by tighten-only accumulation (start TRADABLE, only tighten via merge_severity):
          1. inputs.frozen                       -> NOT_TRADABLE, state HALTED, reason 'ca_frozen' (terminal).
          2. inputs.ca_blackout                  -> NOT_TRADABLE, reason 'ca_blackout'.
          3. session_phase UNKNOWN               -> NOT_TRADABLE, state UNKNOWN, reason 'calendar_unknown'.
          4. session_phase CLOSED                -> NOT_TRADABLE, state CLOSED.
          5. status.halt == HALTED | PAUSED_LULD | UNKNOWN -> NOT_TRADABLE, state HALTED;
             status.halt == RESUMING -> NOT_TRADABLE, state AUCTION (the halt-resumption re-opening auction
                 interlock; held NOT_TRADABLE until status.halt == NONE — MR-5/MED-5; this is the ONLY producer
                 of SessionState.AUCTION, since calendar phase_at never emits it, §A);
             OR status.luld in {PAUSED, UNKNOWN} -> NOT_TRADABLE, state HALTED.
          5b. LULD-BAND-PRESENCE (S7-5b, fail-closed, HIGH-2): if session_phase == RTH and status.luld in
             {NORMAL, LIMIT} but status.luld_band is None -> NOT_TRADABLE, state HALTED, reason
             'luld_band_unknown'. An absent band during continuous RTH is an incomplete/inconsistent vendor
             report and is MORE severe than a known-at-edge band (it cannot be cross-checked) — this makes the
             §B 'unknown band -> most-restrictive' prose load-bearing. (With NO status source, status.luld
             defaults to UNKNOWN and already routes via step 5; 5b catches only the NORMAL/LIMIT-without-band
             inconsistency. LULD is an RTH mechanism, so PRE/POST with no band is NOT restricted here.)
          6. status.luld == LIMIT (band present) -> REDUCE_ONLY (limit state: allow exit, not open).
          7. LULD BAND CROSS-CHECK (S7-5): if inputs.nbbo present and status.luld_band present and a live
             NBB/NBO price is AT or THROUGH a band edge (luld_band_check False) -> at least REDUCE_ONLY,
             reason 'luld_band_edge' — a fail-closed cross-check of the vendor LuldState label against the
             live price the agent already holds. (Band None already routed NOT_TRADABLE via step 5/5b.)
          8. nbbo is None OR not nbbo.two_sided   -> NOT_TRADABLE, reason 'no_two_sided_nbbo'.
          9. session_phase in {PRE, POST}         -> REDUCE_ONLY (extended hours: conservative).
         10. OPEN_CLOSE_BUFFER_S > 0 and ts within [rth_open, rth_open+buffer) or
             [rth_close-buffer, rth_close)        -> REDUCE_ONLY, reason 'open_close_buffer' (POLICY; default OFF).
         11. else (RTH, two-sided, no halt/blackout/band-edge) -> TRADABLE.
        short_allowed = (final tradability == TRADABLE) AND status.ssr == INACTIVE
                        (ACTIVE/UNKNOWN -> no short; a CONSERVATIVE blanket block, stricter than Rule 201's
                        at-or-below-NBB test — MR-3). The Verdict NEVER opens anything; restriction is DATA,
                        not an exception. An out-of-vocabulary enum reaching decide() -> MarketStateError."""
        ...

    def luld_band_check(self, *, price: Decimal, band: Optional[LuldBand]) -> bool:
        """TRUE iff price is strictly inside (band.lower_px, band.upper_px). band is None (unknown) ->
        returns False (fail-closed: an unknown band is a band violation). Decimal-only. Wired into
        decide() step 7."""
        ...
```
**Raises:** `MarketStateError` (out-of-vocabulary enum — fail-closed, never coerced). Returns a `Verdict`; anomalies are surfaced as `NOT_TRADABLE`/`reasons`, mirroring `book_state` crossed-book-as-flag (`book_state.py:11-13`).

**Microstructure / regulatory correctness (stated + fail-closed; vendor/SIP-driven + configurable):**
- **LULD (Reg-NMS Plan):** **Tier 1** = S&P 500 / Russell 1000 / select ETPs; **Tier 2** = all other NMS stocks. Percentage bands are tier-and-price-dependent (the ±5% / ±10% / ±20% / `$0.15`-or-75% buckets) and are **double-width during certain SIP/Plan-defined intervals near the open and/or close** (the exact minutes are SIP/Plan-defined and have been amended over time — M2 does NOT encode them; MED-4); a band breach → a 5-minute **limit state** that may escalate to a trading **pause**. **M2 computes NO band and NO window** (EQUS.MINI has no status schema, `status.py:31`); it consumes ONLY the vendor-supplied `LuldBand`/`LuldState` (incl. the vendor `doubled` flag), cross-checks the live NBBO against the band (step 7), and treats `LuldState.UNKNOWN` **and an absent band during RTH** as most-restrictive → `NOT_TRADABLE` (steps 5/5b). `LuldState.LIMIT` with a present band → `REDUCE_ONLY`.
- **SSR (Reg SHO Rule 201):** triggered by a **≥10% intraday decline from the prior day's official close**; remains in effect **the remainder of that day AND the entire next trading day**; the rule itself restricts a short sale **at or below the NBB**. **M2 maps `SsrState.ACTIVE` (or `UNKNOWN`) → `short_allowed=False`, a STRICTER-than-rule blanket block** (a conservative over-approximation, fail-closed). The 10% computation and the day+next-day persistence are the **broker/SIP's** determination that M2 ingests as a flag. NBB-relative short pricing is a deliberate future loosening behind a contract change.
- **Halts / resumption:** halt reason codes are a closed vocabulary (`HaltReason`). Resumption runs through a re-opening (indicative-price) auction — M2 models this with `HaltState.RESUMING` (a vendor-signalled flag), held `NOT_TRADABLE` until the status feed signals continuous trading restored (`HaltState.NONE`). Unknown halt reason → `HALTED` (fail-closed). M2 does not model the auction price.
- **Sessions:** RTH **09:30–16:00 ET** (continuous, owns the full window — no hardcoded auction-suspension); pre 04:00–09:30; post 16:00–20:00; **early-close (half) days** end RTH at **13:00 ET**. All in ET (`bar_cache` zoneinfo), persisted UTC. **T+1 settlement** is CA context (§D), not session state.

---

## C. `scripts/agent/market_state_cache.py` — freshness-gated NON-BLOCKING cache (degrade-to-SAFE)

Per-`(symbol, instrument_id)` `Verdict` cache with **explicit TTL** driven by the **injected `FakeClock`** (`tests/lib/fakes.py:83-94`) — the SAME ms-clock seam `HeartbeatMonitor` uses (`status.py:162`). On staleness it degrades to the **most-restrictive** default — it NEVER blocks on a refresh and NEVER serves a stale "tradable". It unions open-position symbols into the refresh set so a held symbol is always re-evaluated. A stored verdict whose `instrument_id` ≠ the requested one (ticker reuse / stale definition) is a **miss → safe default** (MED-7).

```python
# scripts/agent/market_state_cache.py
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple
from agent.market_state import (HaltState, LuldState, SessionState, SsrState, Tradability, Verdict)

DEFAULT_FRESHNESS_TTL_MS = 2000   # CODE CONSTANT (overlay-as-config is a min()-merge trap — §G); ctor override for tests only

@dataclass(frozen=True)
class CacheEntry:
    verdict: Verdict
    refreshed_at_ms: int          # monotonic ms (from injected clock) when this verdict was computed

class MarketStateCache:
    """Freshness-gated, NON-BLOCKING per-symbol tradability cache. `clock` is the injected ms clock
    (FakeClock offline) — the SAME seam HeartbeatMonitor uses. No new clock abstraction.

    get() NEVER computes/refreshes inline and NEVER blocks: a stale OR missing entry returns
    safe_default_verdict() immediately (degrade-to-safe). Refresh is a separate, caller-driven step."""
    def __init__(self, *, clock, ttl_ms: int = DEFAULT_FRESHNESS_TTL_MS) -> None:
        """NEVER-LOOSEN CLAMP (HIGH-3, test-only override): ttl_ms exists only for tests; __init__ RAISES
        ValueError unless ttl_ms <= DEFAULT_FRESHNESS_TTL_MS — a caller may only SHORTEN (tighten) freshness,
        never lengthen it (a larger TTL == staler == less safe). The API-level mirror of the §G code-constant
        stance; a ctor param is not a loophole around a code constant."""
        ...

    def put(self, verdict: Verdict, *, now_ms: Optional[int] = None) -> None:
        """Store stamped at now_ms (defaults to clock.now_ms())."""
        ...

    def get(self, symbol: str, instrument_id: int, session_date_et: str,
            *, now_ms: Optional[int] = None) -> Verdict:
        """Return the cached Verdict IFF the stored entry is keyed to the SAME (symbol, instrument_id) AND
        (now_ms - refreshed_at_ms) <= ttl_ms (fresh at exactly ttl_ms, stale at ttl_ms+1 — mirrors
        HeartbeatMonitor.check strict '>' at status.py:178). The cache is keyed on (symbol, instrument_id): a
        stored verdict whose instrument_id != the requested one (a ticker reused for a new instrument / stale
        definition) is treated as a MISS -> safe_default_verdict (MED-7, fail-closed: never serve a prior
        instrument's 'tradable'). Otherwise, and for a missing/stale entry, return safe_default_verdict(...).
        NON-BLOCKING — no inline refresh."""
        ...

    @staticmethod
    def safe_default_verdict(symbol: str, instrument_id: int, session_date_et: str) -> Verdict:
        """The MOST-RESTRICTIVE verdict — EVERY enum field pinned to its most-restrictive member (S7-7):
        session_state=SessionState.UNKNOWN (pinned to UNKNOWN — the honest 'we don't know' state; NOT HALTED,
        which would assert a halt we have not observed — LOW-1), tradability=Tradability.NOT_TRADABLE,
        halt=HaltState.UNKNOWN, luld=LuldState.UNKNOWN, ssr=SsrState.UNKNOWN, two_sided_nbbo=False,
        short_allowed=False, ca_blackout=True, reasons=('cache_stale_safe_default',).
        Returned on any stale/missing/refresh-failure (spec §5 Tier-2 'degrades to safe default when stale')."""
        ...

    def is_fresh(self, symbol: str, *, now_ms: Optional[int] = None) -> bool: ...

    def refresh_set(self, *, candidate_symbols: Iterable[str],
                    open_position_symbols: Iterable[str]) -> Tuple[str, ...]:
        """UNION of candidate_symbols and currently-held (open-position) symbols, deduped, SORTED for
        determinism. A held symbol is ALWAYS in the refresh set even if it left the candidate universe
        (spec §5 Tier-2 'union open-position symbols into the refresh set')."""
        ...

    def stale_symbols(self, symbols: Iterable[str], *, now_ms: Optional[int] = None) -> Tuple[str, ...]:
        """Symbols whose entry is missing or older than ttl_ms (mirrors HeartbeatMonitor.stale_symbols shape)."""
        ...
```
**Raises:** none (returns data; stale/missing → `safe_default_verdict`). **Fail-closed bindings:** stale → safe default; missing → safe default; non-blocking. **TTL design (§G):** `ttl_ms` is a CODE CONSTANT with a ctor-only test override, NOT config-overlayable (larger TTL == staler == dangerous; `min()`-merge would not protect it).

---

## D. `scripts/agent/corporate_actions.py` — multi-source CA, tri-state, fail-closed (S7 centerpiece)

The S7 heart. Cross-validates Alpaca CA against a data-vendor CA; **≥2 INDEPENDENT sources (distinct `CaSource` AND distinct `source_ca_id`) required to CLEAR a blackout**; a lone, conflicting, or incomplete source stays blacked out; an **any-unexplained-broker-qty-delta detector** freezes the symbol and forces an **IMMEDIATE (not EOD) reconcile**; **durable CUSIP/FIGI identity** under unstable tickers. Mirrors: frozen dataclass + composed provenance (`event.py:52-60`), closed-vocab enum (`event.py:33`), graded exception tree (`event.py:37-49`), `Decimal(str(x))`+quantize chokepoint (`event.py` `_quantize_checked`), identity-on-every-apply (`book_state.py:55-64`).

```python
# scripts/agent/corporate_actions.py
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from enum import Enum
from typing import Callable, Dict, FrozenSet, Optional, Protocol, Tuple, runtime_checkable

FACTOR_QUANTUM = Decimal("0.00000001")   # 8dp split/adjust factor quantum (module-level Decimal; event.py:30 pattern)
CASH_QUANTUM = Decimal("0.0001")         # 4dp dividend/cash amounts (matches PRICE_QUANTUM, event.py:30)
MIN_INDEPENDENT_SOURCES = 2              # CODE CONSTANT (>=2 to clear; smaller==more-permissive -> NOT overlayable, §G)
BLACKOUT_LEAD_DAYS = 1                   # CODE CONSTANT (wider==safer -> NOT overlayable, §G)
BLACKOUT_TRAIL_DAYS = 1                  # CODE CONSTANT (T+1 settlement context, spec §8.7)

class CaType(str, Enum):                 # closed vocabulary; out-of-vocab -> FATAL (event.py:33 pattern)
    SPLIT = "split"; REVERSE_SPLIT = "reverse_split"; DIVIDEND = "dividend"; SPECIAL_DIVIDEND = "special_dividend"
    SPINOFF = "spinoff"; MERGER = "merger"; SYMBOL_CHANGE = "symbol_change"; OTHER = "other"
CA_TYPES: FrozenSet[str] = frozenset(t.value for t in CaType)

# CA types that REQUIRE a factor / cash to be a COMPLETE observation (S7-6 None-handling):
_FACTOR_REQUIRED = frozenset({CaType.SPLIT, CaType.REVERSE_SPLIT})
_CASH_REQUIRED = frozenset({CaType.DIVIDEND, CaType.SPECIAL_DIVIDEND})

class ValidationStatus(str, Enum):       # tri-state (spec §5 Tier-2 / S7)
    CONFIRMED = "confirmed_by_N_sources"          # >=2 INDEPENDENT sources agree -> blackout may CLEAR
    SINGLE_SOURCE_BLACKOUT = "single_source_blackout"   # exactly 1 independent source -> STAYS blacked out
    CONFLICTING_BLACKOUT = "conflicting_blackout"       # >=2 sources DISAGREE/incomplete -> STAYS blacked out (tightest)

class CaSource(str, Enum):
    ALPACA = "alpaca"; DATA_VENDOR = "data_vendor"   # two DISTINCT enum members == two candidate-independent sources

# --- graded exception tree (mirrors event.py:37-49) ---
class CorporateActionError(ValueError):
    """Base: any CA processing failure is fail-closed (S7)."""
class UnvalidatedAdjustment(CorporateActionError):
    """An adjustment lacking >=2 independent confirming sources was treated as applied -> reject (S7)."""
class ConflictingSources(CorporateActionError):
    """Two+ sources disagree on type/factor/ex-date (or an incomplete required field) -> conflicting_blackout (S7)."""
class BrokerAdjustedDuringBlackout(CorporateActionError):
    """Broker position qty changed without an agent-originated fill -> FREEZE + IMMEDIATE reconcile (S7)."""

@dataclass(frozen=True)
class DurableId:
    """Durable identity under unstable tickers (spec §5 Tier-2 'CUSIP/FIGI identity'). figi/cusip are the
    stable keys; ticker is the mutable display label. key() == figi if present else cusip; raises if BOTH
    None (a ticker-only identity is NOT durable -> fail-closed). Checked on EVERY apply (mirrors
    book_state._check_identity, book_state.py:55-64) so a ticker reuse cannot cross-contaminate CA state."""
    cusip: Optional[str]
    figi: Optional[str]
    ticker: str
    def key(self) -> str: ...

@dataclass(frozen=True)
class CaProvenance:
    """Composed (NOT inlined) provenance, mirroring event.Provenance (event.py:52-60). source_ca_id is the
    vendor's own CA id — NAMED source_ca_id, NEVER 'seq' (journal _RESERVED, journal.py:21; BLOCKER-1 lesson
    event.py:14). Wall-clock fields here are PERSISTED in the row (DET-1) but EXCLUDED from any hash payload
    that must re-derive across runs."""
    source: CaSource
    source_ca_id: str
    announced_ts_utc: str
    ts_recv_utc: str

@dataclass(frozen=True)
class SourceObservation:
    """ONE source's report of a CA. Decimal factors/cash only, via Decimal(str(x)).quantize (event.py
    _quantize_checked chokepoint); a float fails loud at serialize."""
    source: CaSource
    durable_id: DurableId
    ca_type: CaType
    ex_date_et: str                  # "YYYY-MM-DD" ET ex-date
    factor: Optional[Decimal]        # split/adjust factor (FACTOR_QUANTUM); None for pure cash div / symbol_change
    cash_amount: Optional[Decimal]   # dividend/cash (CASH_QUANTUM); None otherwise
    provenance: CaProvenance

@dataclass(frozen=True)
class AdjustmentEvent:
    """One cross-validated corporate action (replays identically). provenance_set is the in-memory FROZENSET
    of contributing CaSources; provenance is the ORDERED tuple of full CaProvenance (what gets persisted,
    DET-1). validation_status is the tri-state.

    NOTE (DET-2): provenance_set is an IN-MEMORY field ONLY. A set/frozenset must NEVER reach the serializer
    (serializer._default raises TypeError on frozenset). The single persisted form of the source set is
    provenance_sources = sorted(s.value for s in provenance_set), produced by ca_to_row (§E)."""
    durable_id: DurableId
    symbol: str                          # current ticker label at emit time (durable_id is truth)
    ca_type: CaType
    ex_date_et: str
    factor: Optional[Decimal]
    cash_amount: Optional[Decimal]
    provenance_set: FrozenSet[CaSource]  # in-memory ONLY (never serialized directly)
    provenance: Tuple[CaProvenance, ...] # one per contributing source — PERSISTED in full for exact round-trip
    validation_status: ValidationStatus
    provenance_independent: bool         # True iff >=2 distinct CaSource AND >=2 distinct source_ca_id (S7-3)
    blackout_from_et: str                # ex_date - BLACKOUT_LEAD_DAYS (inclusive)
    blackout_to_et: str                  # ex_date + BLACKOUT_TRAIL_DAYS (inclusive)
    @property
    def blackout(self) -> bool:
        """INDEFINITE (validation) blackout flag: True unless validation_status == CONFIRMED. A CONFIRMED event
        imposes only a date-BOUNDED ex-date-window blackout (handled by is_blacked_out), so its indefinite flag
        is False; a non-CONFIRMED event imposes an OPEN-ENDED blackout. This is NOT 'is this date blacked out' —
        that is is_blacked_out(on_date_et) below (the two were conflated; MED-6)."""
        ...

@runtime_checkable
class CaFetcher(Protocol):
    """Injectable per-source CA fetcher seam (mirrors databento raw_source injection, databento.py:46).
    Offline tests inject a scripted fake (tests/lib/fakes style); the live impl lazily reads the
    broker/vendor API behind the same lazy/credentialed seam (deferred, §M)."""
    def fetch(self, durable_id: DurableId) -> Tuple[SourceObservation, ...]: ...

def cross_validate(observations: Tuple[SourceObservation, ...], *,
                   lead_days: int = BLACKOUT_LEAD_DAYS, trail_days: int = BLACKOUT_TRAIL_DAYS) -> AdjustmentEvent:
    """PURE tri-state validation — a function of the observation/provenance set ONLY (referentially
    transparent: same set => same status => hand-auditable).
      - group observations by (durable_id.key(), ex_date_et) — the "same CA event" grain. ca_type is NOT in
        the grouping key (LOW-3): two sources reporting a DIFFERENT ca_type for the same (durable_id, ex_date)
        MUST land in the SAME group so the disagreement is VISIBLE -> CONFLICTING_BLACKOUT (grouping BY ca_type
        would split them into two single-source groups and HIDE the conflict). Genuinely distinct CAs differ in
        ex_date (a dividend vs a split) -> distinct groups, validated independently. Two real CAs sharing one
        ex_date is rare -> forced-conflict -> blackout (fail-closed, acceptable).
      - 0 observations -> CorporateActionError (nothing to validate).
      - independent sources = distinct CaSource members WHOSE source_ca_id values are ALSO distinct
        (S7-3: same source twice, OR two sources mirroring the same source_ca_id, does NOT count as 2).
      - INCOMPLETE observation (S7-6): a source whose ca_type is in _FACTOR_REQUIRED with factor=None,
        or in _CASH_REQUIRED with cash_amount=None, cannot contribute to CONFIRMED -> treated as a
        disagreement -> CONFLICTING_BLACKOUT.
      - any two sources DISAGREE on (ca_type, ex_date, factor-within-FACTOR_QUANTUM, cash-within-CASH_QUANTUM)
        -> CONFLICTING_BLACKOUT (disagreement DOMINATES agreement).
      - elif (>= MIN_INDEPENDENT_SOURCES independent) AND they AGREE AND all complete -> CONFIRMED (clearable).
      - else (exactly 1 independent / incomplete singletons) -> SINGLE_SOURCE_BLACKOUT.
    factor/cash compared after .quantize(..., ROUND_HALF_EVEN); a non-round-tripping value raises (fail-loud).
    NEVER-LOOSEN CLAMP (HIGH-3, test-only overrides): lead_days/trail_days exist ONLY so tests can exercise
    window arithmetic; cross_validate RAISES CorporateActionError unless lead_days >= BLACKOUT_LEAD_DAYS AND
    trail_days >= BLACKOUT_TRAIL_DAYS — a caller may only WIDEN, never SHRINK, the blackout (the API-level
    mirror of the code-constant / min()-trap stance, §G; a param is not a loophole around a code constant).
    blackout window = [ex_date-lead_days, ex_date+trail_days] ET, CLOSED/INCLUSIVE on both edges (S7-4).
    provenance_independent records whether the >=2-distinct-source_ca_id test passed (live trust gate, §M).
    Returns an AdjustmentEvent (blackout is DATA, not an exception — mirrors book_state crossed-book-as-flag)."""
    ...

class CorporateActionFeed:
    """Multi-source CA aggregator with INJECTABLE fetchers (offline = scripted fakes; NO network, NO SDK
    import at module scope). Builds SourceObservations and runs cross_validate; it NEVER applies an
    adjustment to a position (M2 is the decider, not the executor). adjustments_for/is_blacked_out are
    deterministic ONLY given fixed fetcher outputs (DET-4)."""
    def __init__(self, fetchers: Dict[CaSource, CaFetcher]) -> None: ...
    def adjustments_for(self, durable_id: DurableId, *, ts_recv_utc: str) -> Tuple[AdjustmentEvent, ...]:
        """Fetch every injected source, group, cross_validate; one AdjustmentEvent per (ca_type, ex_date)."""
        ...
    def is_blacked_out(self, durable_id: DurableId, *, on_date_et: str) -> bool:
        """ONE coherent rule (MED-6 — supersedes the earlier self-contradictory wording). True iff, for ANY
        AdjustmentEvent E of this durable_id, EITHER:
          • E.validation_status == CONFIRMED  AND  blackout_from_et <= on_date_et <= blackout_to_et
              — even a CONFIRMED CA blacks out its own ex-date WINDOW (spec §5 'treat ex-date/event windows as a
                trading blackout'); it CLEARS once on_date_et passes blackout_to_et;  OR
          • E.validation_status != CONFIRMED  AND  on_date_et >= blackout_from_et
              — an unconfirmed / single-source / conflicting / incomplete CA imposes an OPEN-ENDED blackout from
                its window start that NEVER self-clears; only a >=2-independent-source upgrade to CONFIRMED bounds
                it (S7-1/4, fail-closed).
        So CONFIRMED -> bounded window; non-CONFIRMED -> open-ended from window start (strictly more
        conservative). This is exactly why test_blackout_window_is_closed_inclusive asserts is_blacked_out True
        on BOTH window edges and False the day after ONLY for a CONFIRMED event (§J)."""
        ...

class FreezeReason(str, Enum):
    BROKER_ADJUSTED_DURING_BLACKOUT = "broker_adjusted_during_blackout"  # delta during a known CA blackout
    BROKER_ADJUSTED_NO_KNOWN_CA = "broker_adjusted_no_known_ca"          # delta with NO CA in the feed (S7-1)

class BrokerAdjustDetector:
    """Detects an UNEXPLAINED broker position-qty change (S7-1). Compares the BROKER qty (broker truth;
    a PLAIN Decimal share count sourced ONLY from broker.positions() — NOT BrokerUSD, which is USD;
    DET-3/OP-3). ANY qty != baseline that the agent did not originate is a freeze, regardless of blackout
    state — the blackout/known-CA state only LABELS the reason (FreezeReason). Until M5 fills exist there
    is no agent-originated-fill ledger to net against, so M2's posture is: any broker qty delta -> freeze
    + immediate reconcile.

    SEEDING (frozen, fail-closed S7-2): the baseline MUST be seeded EXPLICITLY via seed_baseline() from the
    broker position-of-record BEFORE any observe. observe_broker_qty with NO prior baseline for a symbol
    RAISES BrokerAdjustedDuringBlackout (it MUST NOT silently seed the first observation — a restart mid-CA
    would otherwise absorb an adjusted qty as the baseline). The precise SOD seeding sequence lands in M6
    reconcile — that is the cross-milestone seam (§M)."""
    def __init__(self) -> None:
        self._baseline: Dict[str, Decimal] = {}   # durable_id.key() -> last-known BROKER qty (plain Decimal shares)
        self._frozen: set = set()
    def seed_baseline(self, durable_id: DurableId, broker_qty: Decimal) -> None:
        """Set the baseline from the broker position-of-record (a CA-implied/modeled qty MUST NEVER seed
        or override this — the invariant tested by test_ca_implied_qty_cannot_override_broker_baseline)."""
        ...
    def observe_broker_qty(self, durable_id: DurableId, broker_qty: Decimal, *,
                           blacked_out: bool) -> Optional["FreezeSignal"]:
        """No baseline -> raise BrokerAdjustedDuringBlackout (S7-2). broker_qty != baseline -> return
        FreezeSignal(immediate_reconcile=True), reason BROKER_ADJUSTED_DURING_BLACKOUT if blacked_out else
        BROKER_ADJUSTED_NO_KNOWN_CA (S7-1), and mark frozen. Else (qty == baseline) return None. broker_qty
        MUST be a Decimal (whole shares); a float raises at the serializer wall on persistence."""
        ...
    def is_frozen(self, durable_id: DurableId) -> bool: ...

@dataclass(frozen=True)
class FreezeSignal:
    durable_id: DurableId
    symbol: str
    immediate_reconcile: bool        # ALWAYS True for an unexplained broker qty delta (NOT deferred to EOD)
    prev_qty: Decimal
    curr_qty: Decimal
    reason: FreezeReason
```
**Raises:** `UnvalidatedAdjustment`, `ConflictingSources`, `BrokerAdjustedDuringBlackout` (incl. missing-baseline), `CorporateActionError` (no durable id / out-of-vocab `ca_type` / non-round-tripping factor / **a loosening `lead_days`/`trail_days` override** — HIGH-3 clamp). **S7 bindings (each tested, §J):** lone/incomplete source → `SINGLE_SOURCE_BLACKOUT`/`CONFLICTING_BLACKOUT` (never clears); ≥2 agreeing independent (distinct `source_ca_id`) complete → `CONFIRMED`; ≥2 disagreeing/incomplete → `CONFLICTING_BLACKOUT`; same-source-twice or mirrored `source_ca_id` ≠ 2 independent; ANY unexplained broker qty delta (blackout OR no-known-CA) → `FreezeSignal(immediate_reconcile=True)` + frozen; missing baseline → freeze; identity matched on durable CUSIP/FIGI, never ticker; closed inclusive blackout window.

---

## E. Status/halt/CA ledger → `journal/status.jsonl`

M2 writes **explicit state-transition rows** (not only implicit alerts) to the `status` stream via the existing journal/persistence chain. **M2 is the FIRST producer of the `status` stream** (`STREAM_STATUS` is defined at `persistence.py:49` but unwritten at HEAD; the §E schema is net-new). A thin facade `StatusLedger` wraps `EventWriter` (which wraps `JournalWriter`, `persistence.py:52`) → `EventWriter.record` (`persistence.py:89`) → `JournalWriter.append` → `agent.serializer`. **No new writer, no new hash.** Open `journal/status.jsonl` by ONE canonical resolved path so the per-stream `seq` + writer lock stay shared (`journal.py:70-81`). M2 mints **no `run_id`** — shares the injected agent/orchestrator `run_id` (S6).

```python
# scripts/agent/status_ledger.py
from decimal import Decimal
from typing import Optional
from recorder.persistence import EventWriter, STREAM_STATUS, replay_stream  # JournalCorruption also re-exported here
from agent.journal import JournalCorruption                                 # (either import path is valid)

STATUS_LEDGER_VERSION = 1            # payload-schema version; FIRST key in every canonical_*_payload helper

# event_type tags (M2's choice; none collides with _RESERVED — journal.py:21):
EVT_SESSION_TRANSITION = "session_transition"
EVT_HALT_TRANSITION = "halt_transition"
EVT_LULD_TRANSITION = "luld_transition"
EVT_SSR_TRANSITION = "ssr_transition"
EVT_CORPORATE_ACTION = "corporate_action"
EVT_CA_BLACKOUT_TRANSITION = "ca_blackout_transition"
EVT_BROKER_ADJUST_FREEZE = "broker_adjust_freeze"

class StatusLedger:
    """Facade over EventWriter for journal/status.jsonl. ONE StatusLedger per resolved path; shares the
    injected run_id. Carries rules_hash as a plain non-reserved provenance field on every row (config.py:17),
    keying each transition to the effective config that produced it. NO new hashing/serialization."""
    def __init__(self, writer: EventWriter, *, rules_hash: str) -> None: ...

    def record_session_transition(self, *, symbol, instrument_id, from_state, to_state, cause,
                                  session_date_et, ts_market_utc, decision_id=None) -> dict: ...
    def record_halt_transition(self, *, symbol, instrument_id, from_state, to_state, halt_reason,
                               ts_market_utc, decision_id=None) -> dict: ...
    def record_luld_transition(self, *, symbol, instrument_id, from_state, to_state, luld_tier,
                               reference_px: Decimal, lower_px: Decimal, upper_px: Decimal,
                               doubled: bool, ts_market_utc) -> dict:
        """A luld_transition records a CONCRETE band change -> the three band prices are REQUIRED non-null
        Decimals (MED-8; matches the Dec-str row schema, which is NOT nullable for these). An ABSENT band is
        NOT a luld_transition: it routes through the decider as 'luld_band_unknown' (§B step 5b) -> a halt/
        session transition, never a null-priced LULD row."""
        ...
    def record_ssr_transition(self, *, symbol, instrument_id, from_state, to_state,
                              prior_close_px: Optional[Decimal], ts_market_utc) -> dict: ...
    def record_corporate_action(self, *, adjustment, instrument_id: int, ts_market_utc, decision_id=None) -> dict:
        """Flatten an AdjustmentEvent via ca_to_row (below) and EventWriter.record. Decimals stay Decimal.
        instrument_id is a REQUIRED arg (HIGH-1): the common row prefix carries it (§E), but the AdjustmentEvent
        does NOT (the CA feed is identity-DURABLE — CUSIP/FIGI — not market-data-numeric). CA/freeze identity-
        OF-RECORD is the durable_key; instrument_id is the CURRENT numeric id at emit time, passed by the caller
        (which holds the symbol<->instrument_id map from definitions/book_state) for correlation, so a build
        agent never guesses where it comes from."""
        ...
    def record_broker_adjust_freeze(self, *, freeze_signal, instrument_id: int, ts_market_utc) -> dict:
        """instrument_id REQUIRED for the same reason as record_corporate_action (HIGH-1); FreezeSignal carries
        the durable_id, the caller supplies the current numeric id."""
        ...

def canonical_status_payload(*, version: int, body: dict) -> dict:
    """Pure helper exposing the pre-hash payload with 'v' FIRST (white-box determinism test; mirrors
    canonical_book_payload, book_hash.py:90). The journal still computes the row hash via row_hash. NOTE:
    ts_market_utc IS a payload field (a transition is a dated FACT — divergence from book_hash, which
    excludes ts because a book is identical regardless of when observed). The journal's own ts_utc/seq
    write-stamps are journal-owned and RE-READ (not recomputed) on replay, so byte-replay stability holds."""
    return {"v": version, **body}

def ca_to_row(adjustment) -> dict:
    """Flat persistence form (mirrors event_row.to_row, event_row.py:77). Persists the FULL per-source
    provenance (DET-1) so the round-trip is EXACT: provenance = [[source, source_ca_id, announced_ts_utc,
    ts_recv_utc], ...]. provenance_sources = sorted(s.value for s in provenance_set) is a DERIVED
    convenience field; provenance_set (the frozenset) is NEVER serialized (DET-2). Decimals AS Decimal."""
    ...
def ca_from_row(row: dict) -> "AdjustmentEvent":
    """Exact inverse: ca_from_row(ca_to_row(ev)) == ev (mirrors event_row.from_row, event_row.py:117).
    Rebuilds provenance_set from the persisted provenance list; Decimals via Decimal(str(value));
    missing field -> MalformedRecord."""
    ...

def replay_status(status_path) -> list:
    """Re-read journal/status.jsonl hash-verified via recorder.persistence.replay_stream (delegates to
    agent.journal.replay, journal.py:28) — SAME truncated-tail + JournalCorruption semantics as M1."""
    return replay_stream(status_path)

def rehydrate_state(rows) -> dict:
    """PURE fold in ASCENDING journal `seq` order (the row's stamped seq, journal.py:120). 'latest-row-wins'
    = HIGHEST seq per (symbol | durable_key) — NOT ts_market_utc, which can repeat across distinct
    transitions stamped at the same market instant (DET-5). Returns {symbol -> latest tradability state}
    and {durable_key -> latest CA blackout/freeze state}. Replaying the SAME rows yields the SAME state."""
    ...
```

**Field-naming rule (frozen, BLOCKER-1 lesson):** NO payload field may be named `seq`/`event_type`/`run_id`/`hash`/`decision_id`/`order_id`/`ts_utc` (`journal.py:21`; collision raises at `journal.py:114`). A vendor/source ordinal is `source_ca_id` (§D), never `seq`. `rules_hash` is a non-reserved plain-string field. `ts_market_utc` (the market instant of the transition) is a payload field, distinct from the journal's `ts_utc` write stamp. **No set/frozenset ever enters a row (DET-2)** — only `provenance_sources` (a sorted list) and the flat `provenance` list of sublists.

**Row schemas** (the journal stamps `event_type`/`run_id`/`seq`/`ts_utc`/`hash`; everything else is the flat `fields` payload). Common prefix on every row: `symbol`(str), `instrument_id`(int), `ts_market_utc`(str), `rules_hash`(str), `v`(int = `STATUS_LEDGER_VERSION`). CA/freeze rows add `cusip`/`figi`/`durable_key`.

| event_type | additional flat fields |
|---|---|
| `session_transition` | `from_state`(str∈SESSION_STATES), `to_state`(str), `cause`(str∈{session_open,session_close,halt,resuming,calendar_unknown}), `session_date_et`(str) |
| `halt_transition` | `from_state`(str), `to_state`(str), `halt_reason`(str) |
| `luld_transition` | `from_state`(str), `to_state`(str), `luld_tier`(str), `reference_px`(Dec-str), `lower_px`(Dec-str), `upper_px`(Dec-str), `doubled`(bool) |
| `ssr_transition` | `from_state`(str), `to_state`(str), `prior_close_px`(Dec-str or null) |
| `corporate_action` | `cusip`(str or null), `figi`(str or null), `durable_key`(str), `ticker`(str), `ca_type`(str), `ex_date_et`(str), `factor`(Dec-str or null), `cash_amount`(Dec-str or null), `provenance`(list of `[source,source_ca_id,announced_ts_utc,ts_recv_utc]`), `provenance_sources`(sorted list of str), `provenance_independent`(bool), `validation_status`(str), `blackout`(bool), `blackout_from_et`(str), `blackout_to_et`(str) |
| `broker_adjust_freeze` | `cusip`/`figi`/`durable_key`/`ticker`, `prev_qty`(Dec-str), `curr_qty`(Dec-str), `immediate_reconcile`(bool=true), `reason`(str∈FreezeReason) |

**Sample rows** (as persisted; `seq`/`hash`/`run_id`/`ts_utc` stamped by `JournalWriter.append`, `journal.py:117-126`; keys sorted by `dumps`):
```
{"cause":"session_open","event_type":"session_transition","from_state":"closed","hash":"<sha256>","instrument_id":1001,"rules_hash":"<cfg-sha256>","run_id":"agent-2026-06-09","seq":41,"session_date_et":"2026-06-15","symbol":"AAPL","to_state":"rth","ts_market_utc":"2026-06-15T13:30:00.000000Z","ts_utc":"2026-06-15T13:30:00.005000+00:00","v":1}
{"doubled":false,"event_type":"luld_transition","from_state":"normal","hash":"<sha256>","instrument_id":1001,"lower_px":"200.5000","luld_tier":"tier1","reference_px":"201.5000","rules_hash":"<cfg-sha256>","run_id":"agent-2026-06-09","seq":42,"symbol":"AAPL","to_state":"paused","ts_market_utc":"2026-06-15T14:05:00.000000Z","ts_utc":"2026-06-15T14:05:00.010000+00:00","upper_px":"202.5000","v":1}
{"blackout":true,"blackout_from_et":"2026-06-30","blackout_to_et":"2026-07-02","ca_type":"split","cash_amount":null,"cusip":"TESTAAPL1","durable_key":"BBG000B9XRY4","event_type":"corporate_action","ex_date_et":"2026-07-01","factor":"4.00000000","figi":"BBG000B9XRY4","hash":"<sha256>","instrument_id":1001,"provenance":[["alpaca","ALP-1","2026-06-20T12:00:00.000000Z","2026-06-20T12:00:01.000000Z"]],"provenance_independent":false,"provenance_sources":["alpaca"],"rules_hash":"<cfg-sha256>","run_id":"agent-2026-06-09","seq":43,"ticker":"AAPL","ts_market_utc":"2026-06-20T20:00:00.000000Z","ts_utc":"2026-06-20T20:00:00.020000+00:00","v":1,"validation_status":"single_source_blackout"}
{"curr_qty":"400","cusip":"TESTAAPL1","durable_key":"BBG000B9XRY4","event_type":"broker_adjust_freeze","figi":"BBG000B9XRY4","hash":"<sha256>","immediate_reconcile":true,"instrument_id":1001,"prev_qty":"100","reason":"broker_adjusted_during_blackout","rules_hash":"<cfg-sha256>","run_id":"agent-2026-06-09","seq":44,"ticker":"AAPL","ts_market_utc":"2026-07-01T13:35:00.000000Z","ts_utc":"2026-07-01T13:35:00.030000+00:00","v":1}
```
**Raises:** `JournalCorruption` (inherited on replay). A `_RESERVED` collision would raise at `journal.py:114` — by construction none occur. A float anywhere raises at `serializer.py:29` (fail-closed: LULD bands / SSR prior-close / CA factor MUST be Decimal); a `frozenset` reaching `dumps` raises `TypeError` at `serializer.py:44` (DET-2 wall). **S3:** replay via `replay_stream` → identical truncated-tail (recoverable) vs newline-terminated-corrupt (`JournalCorruption`, fatal) semantics. **S6:** shares `run_id`; correlation via `decision_id`/`order_id`. **S7:** every blackout/freeze TRANSITION is a durable row, never a silent in-memory state.

---

## F. Session-aware gap-detection wiring (a SCOPED M1 edit to `recorder.py` — NO duplication)

M1's `HeartbeatMonitor` (`status.py:151-194`) is session-UNAWARE: `check` uses strict `now_ms - last > timeout_ms` (`status.py:178`). The recorder consults `stale_symbols(now_ms)` at exactly two sites — `_check_connected_quiet` (`recorder.py:327-338`) and `_reconnect` (`recorder.py:340-370`) — and both **unconditionally** call `_emit_alert(cause="heartbeat_timeout", symbol=symbol)`. M2 teaches "no-gap-alarm across a legitimately closed session" **without editing `status.py`'s pure timer** and **without touching the seq path** (`SequenceTracker` is `policy=NONE` for EQUS.MINI, `status.py:77-78`).

> **This REQUIRES a scoped M1 edit to `scripts/recorder/recorder.py` (OP-1 — corrected).** To make the seam do anything, the build agent MUST: (a) add a keyword-only `liveness=None` param to `Recorder.__init__` (`recorder.py:154-168`); (b) guard BOTH `_emit_alert(cause="heartbeat_timeout", ...)` call sites with `liveness.expected_live(symbol, now_ms)`. The edit is intentionally minimal. **`liveness is None` MUST reproduce byte-identical M1 behavior** (the existing `tests/recorder/test_recorder*` suite stays green). The seq path (`recorder.py:323` `_detect_sequence`) and `HeartbeatMonitor` itself are UNTOUCHED.

```python
# scripts/agent/session_liveness.py  (M2-owned; the recorder imports the TYPE only)
from typing import Protocol, runtime_checkable

@runtime_checkable
class SessionLiveness(Protocol):
    def expected_live(self, symbol: str, now_ms: int) -> bool:
        """True iff `symbol` is EXPECTED to be quoting at this ET instant (calendar phase in {PRE,RTH,POST}
        on a trading day, not CLOSED/UNKNOWN). PURE; calendar + clock-derivation injected."""
        ...

class CalendarLiveness:
    """PURE predicate: expected_live = (MarketCalendar.phase_at(now_ms -> ET instant) in {PRE,RTH,POST}).
    Built from the M2 calendar (injected). The recorder's two guarded sites consult this BEFORE
    _emit_alert(cause='heartbeat_timeout', ...): a legitimately closed/unknown session yields
    expected_live=False -> the heartbeat_timeout alert is SUPPRESSED. SequenceTracker is UNTOUCHED."""
    def __init__(self, *, calendar, clock_to_utc_iso) -> None: ...
    def expected_live(self, symbol: str, now_ms: int) -> bool: ...
```
**Posture (frozen):** a closed-session heartbeat is NOT an error and a CA blackout transition is a status row, not a raise — mirroring `status.py`'s "every anomaly is DATA, never an exception" (`status.py:13`).

---

## G. Config additions + `rules_hash` interplay (the single most important M2 config constraint)

**The `min()`-merge trap (verified `config.py:35-41`):** `tighten_only_merge` takes `min()` of two non-bool numerics — correct ONLY when **smaller == safer**. Any M2 quantity whose SAFE direction is LARGER (longer blackout window, longer cache TTL, wider LULD band, *higher* `min_ca_sources`) would be LOOSENED by an overlay supplying a smaller value. **Resolution (frozen): all M2 SAFETY numbers are CODE CONSTANTS, not config.**

| M2 quantity | Home | Why |
|---|---|---|
| `MIN_INDEPENDENT_SOURCES = 2` | **CODE CONSTANT** | smaller == more permissive → must NOT be `min()`-overlayable. |
| `BLACKOUT_LEAD_DAYS` / `BLACKOUT_TRAIL_DAYS` | **CODE CONSTANT** | wider window == safer → `min()` would shrink it. |
| `DEFAULT_FRESHNESS_TTL_MS` | **CODE CONSTANT** (ctor override for tests only) | larger TTL == staler == less safe. |
| `OPEN_CLOSE_BUFFER_S` (default 0) | **CODE CONSTANT** | a policy knob; default OFF (MR-1). |
| LULD band percentages / SSR 10% threshold | **vendor/SIP provenance (never config thresholds)** | SIP-driven; unknown → most-restrictive. |
| `exchange_calendars` pin + `CALENDAR_MIC` | **config (string) OR code constant** | provenance, not a threshold; a STRING is never `min()`-merged (type-mismatch → keep base, `config.py:42`). |

**Recommended config home:** a NEW nested block `agent_rules.market_state.{calendar_pin, calendar_mic}` for **provenance strings only**, so run-gate keys stay untouched at top level and merge recursively for free (`config.py:25-32`). Any M2 config bool MUST be a real JSON `true`/`false` (a string `"true"` reads `False` via `strict_bool`, `gates.py:11` — fail-closed but surprising; cf. `test_config_canary.py:56-59`).

**M2's tradability is a SEPARATE surface (a `Verdict`), NOT a run-gate** — it never feeds `opening_allowed`/`live_allowed` (`gates.py:23-30`); M2 touches none of those three keys.

**`rules_hash` wiring (new in M2 — verified defined but unused in `scripts/`):** M2 is the FIRST milestone to wire `config.rules_hash` (`config.py:17`) into emitted rows. Every M2 status/CA row carries `rules_hash` as a plain string provenance field (§E). `rules_hash` is the config-provenance path (plain JSON, `allow_nan=False`); DISTINCT from `serializer.row_hash` (Decimal-as-string ledger rows). **Do NOT route config through the Decimal serializer, nor status rows through `rules_hash`.** No runtime loader exists; M2 reuses the inline `{"agent_rules":..., "risk_rules":...}` assembly (`test_config_canary.py:23-27`) and hashes the ASSEMBLED dict (never per-file). `data_retention.json` is NOT in that dict — widening it is a deliberate stated decision only if status.jsonl rotation lands (deferred per M1 MINOR 9).

**Canary obligation:** add a tighten-only canary (reusing `tighten_only_merge`, no new code) proving a `market_state`/CA overlay attempting to LOOSEN is refused — mirroring `test_armed_overlay_cannot_loosen_committed_via_tighten_only` (`test_config_canary.py:60-64`). Because all M2 safety numbers are code constants, the canary asserts the overlay **cannot introduce them at all** (overlay-only keys dropped by `tighten_only_merge`).

---

## H. Fixtures (every M2 fixture, with sample rows)

All `.jsonl` = one JSON object per line; ALL prices/factors/qty as **Decimal-strings**; timestamps UTC ISO-8601 with `Z`; closed-vocab fields as their enum string; **no floats**. Mirrors `tests/fixtures/databento/equs_mini_tbbo_sample.jsonl` and the DST template `tests/fixtures/bars/dst_boundary_events.jsonl`.

**H.1 `tests/fixtures/calendar/nyse_2026_schedule.json`** — pinned fixture driving `FixtureScheduleProvider`. Includes a normal RTH day, a holiday (Thanksgiving, CLOSED), an early-close half-day (13:00 ET, the day after Thanksgiving), BOTH 2026 DST transition Sundays (non-trading), and the post-transition trading Mondays. **Instants cross-checked against real `exchange_calendars==4.13.2`.**
```json
{"mic":"XNYS","pin":"4.13.2","sessions":{
  "2026-06-15":{"is_trading_day":true,"is_early_close":false,"pre_open_et":"04:00","rth_open_et":"09:30","rth_close_et":"16:00","post_close_et":"20:00"},
  "2026-11-26":{"is_trading_day":false,"is_early_close":false,"pre_open_et":null,"rth_open_et":null,"rth_close_et":null,"post_close_et":null},
  "2026-11-27":{"is_trading_day":true,"is_early_close":true,"pre_open_et":"04:00","rth_open_et":"09:30","rth_close_et":"13:00","post_close_et":"17:00"},
  "2026-12-25":{"is_trading_day":false,"is_early_close":false,"pre_open_et":null,"rth_open_et":null,"rth_close_et":null,"post_close_et":null},
  "2026-03-08":{"is_trading_day":false,"is_early_close":false,"pre_open_et":null,"rth_open_et":null,"rth_close_et":null,"post_close_et":null},
  "2026-03-09":{"is_trading_day":true,"is_early_close":false,"pre_open_et":"04:00","rth_open_et":"09:30","rth_close_et":"16:00","post_close_et":"20:00"},
  "2026-11-01":{"is_trading_day":false,"is_early_close":false,"pre_open_et":null,"rth_open_et":null,"rth_close_et":null,"post_close_et":null},
  "2026-11-02":{"is_trading_day":true,"is_early_close":false,"pre_open_et":"04:00","rth_open_et":"09:30","rth_close_et":"16:00","post_close_et":"20:00"}}}
```
The provider converts `*_et` via zoneinfo construct-in-ET-then-`astimezone(UTC)`. Verified UTC closes: `2026-06-15`(EDT)→`20:00Z`; `2026-11-02`(EST)→`21:00Z`; `2026-11-27`(EST half-day 13:00 ET)→`18:00Z`.

**H.2 `tests/fixtures/calendar/session_instants.jsonl`** — at least one pinned instant per `SessionPhase` member (PRE, RTH, POST, CLOSED) + DST/EST-EDT discrimination. (AUCTION is vendor-derived in the decider, not a calendar instant — MR-1; covered in §H.3.)
```
{"ts_utc":"2026-06-15T12:00:00.000000Z","expect_date":"2026-06-15","expect_phase":"pre"}
{"ts_utc":"2026-06-15T13:30:00.000000Z","expect_date":"2026-06-15","expect_phase":"rth"}
{"ts_utc":"2026-06-15T20:00:00.000000Z","expect_date":"2026-06-15","expect_phase":"post"}
{"ts_utc":"2026-11-02T14:30:00.000000Z","expect_date":"2026-11-02","expect_phase":"rth"}
{"ts_utc":"2026-11-02T21:00:00.000000Z","expect_date":"2026-11-02","expect_phase":"post"}
{"ts_utc":"2026-11-27T18:30:00.000000Z","expect_date":"2026-11-27","expect_phase":"post"}
{"ts_utc":"2026-11-26T15:00:00.000000Z","expect_date":"2026-11-26","expect_phase":"closed"}
{"ts_utc":"2026-06-13T15:00:00.000000Z","expect_date":"2026-06-13","expect_phase":"closed"}
```
(Row 6: `2026-11-27` EST half-day RTH close = 13:00 ET = 18:00Z; an 18:30Z instant is POST. Row 8: `2026-06-13` is a Saturday → CLOSED.)

**H.3 `tests/fixtures/market_state/tradability_transitions.jsonl`** — one `TradabilityInputs` per session/halt/LULD/SSR/resumption transition + expected `Verdict`. `ssr` and `halt` are INJECTED broker/SIP determinations; `prior_close` is provenance context only (a decider that derives SSR from price is out-of-contract — OP-4).
```
{"symbol":"AAPL","instrument_id":1001,"session_phase":"rth","halt":"none","luld":"normal","ssr":"inactive","bid_px":"201.15","bid_sz":"300","ask_px":"201.16","ask_sz":"200","prior_close":"200.00","ca_blackout":false,"expect_tradability":"tradable","expect_short_allowed":true}
{"symbol":"AAPL","instrument_id":1001,"session_phase":"rth","halt":"halted","luld":"normal","ssr":"inactive","bid_px":null,"bid_sz":null,"ask_px":null,"ask_sz":null,"prior_close":"200.00","ca_blackout":false,"expect_tradability":"not_tradable"}
{"symbol":"AAPL","instrument_id":1001,"session_phase":"rth","halt":"resuming","luld":"normal","ssr":"inactive","bid_px":"200.00","bid_sz":"100","ask_px":"200.10","ask_sz":"100","prior_close":"200.00","ca_blackout":false,"expect_tradability":"not_tradable"}
{"symbol":"AAPL","instrument_id":1001,"session_phase":"rth","halt":"none","luld":"limit","ssr":"inactive","bid_px":"180.00","bid_sz":"100","ask_px":"181.00","ask_sz":"100","prior_close":"200.00","ca_blackout":false,"expect_tradability":"reduce_only"}
{"symbol":"AAPL","instrument_id":1001,"session_phase":"rth","halt":"none","luld":"normal","luld_band_lower":"200.50","luld_band_upper":"202.50","luld_tier":"tier1","ssr":"inactive","bid_px":"200.40","bid_sz":"100","ask_px":"200.50","ask_sz":"100","prior_close":"200.00","ca_blackout":false,"expect_tradability":"reduce_only"}
{"symbol":"AAPL","instrument_id":1001,"session_phase":"rth","halt":"none","luld":"normal","ssr":"inactive","bid_px":"201.00","bid_sz":"100","ask_px":"201.10","ask_sz":"100","prior_close":"200.00","ca_blackout":false,"expect_tradability":"not_tradable","expect_reason":"luld_band_unknown"}
{"symbol":"AAPL","instrument_id":1001,"session_phase":"rth","halt":"none","luld":"normal","ssr":"active","bid_px":"179.00","bid_sz":"100","ask_px":"179.10","ask_sz":"100","prior_close":"200.00","ca_blackout":false,"expect_tradability":"tradable","expect_short_allowed":false}
{"symbol":"AAPL","instrument_id":1001,"session_phase":"pre","halt":"none","luld":"normal","ssr":"inactive","bid_px":"200.00","bid_sz":"100","ask_px":"200.10","ask_sz":"100","prior_close":"200.00","ca_blackout":false,"expect_tradability":"reduce_only"}
{"symbol":"AAPL","instrument_id":1001,"session_phase":"rth","halt":"none","luld":"normal","ssr":"inactive","bid_px":"201.00","bid_sz":"100","ask_px":null,"ask_sz":null,"prior_close":"200.00","ca_blackout":false,"expect_tradability":"not_tradable"}
{"symbol":"AAPL","instrument_id":1001,"session_phase":"rth","halt":"none","luld":"normal","ssr":"inactive","bid_px":"201.00","bid_sz":"100","ask_px":"201.10","ask_sz":"100","prior_close":"200.00","ca_blackout":true,"expect_tradability":"not_tradable"}
{"symbol":"AAPL","instrument_id":1001,"session_phase":"unknown","halt":"unknown","luld":"unknown","ssr":"unknown","bid_px":null,"bid_sz":null,"ask_px":null,"ask_sz":null,"prior_close":null,"ca_blackout":false,"expect_tradability":"not_tradable"}
```
(Row 3: `resuming` → re-opening auction interlock → NOT_TRADABLE, `session_state=auction` (MR-5/MED-5). Row 5: NBO 200.50 sits AT the upper band edge → `luld_band_check` False → REDUCE_ONLY even with `luld:normal` (S7-5). Row 6: `luld:normal` but NO `luld_band_*` fields during RTH → band unknown → NOT_TRADABLE, reason `luld_band_unknown` (HIGH-2 step 5b). Row 7: injected `ssr:active` → short blocked, otherwise tradable. Row 9: one-sided NBBO → NOT_TRADABLE. Row 10: CA blackout dominates. Row 11: UNKNOWN everywhere → NOT_TRADABLE.)

**H.4 `tests/fixtures/corporate_actions/two_source_confirmed.jsonl`** — Alpaca + data_vendor agree on a 4:1 split, distinct `source_ca_id` → CONFIRMED, `provenance_independent=true`.
```
{"source":"alpaca","cusip":"TESTAAPL1","figi":"BBG000B9XRY4","ticker":"AAPL","ca_type":"split","ex_date_et":"2026-07-01","factor":"4.00000000","cash_amount":null,"source_ca_id":"ALP-1","announced_ts_utc":"2026-06-20T12:00:00.000000Z"}
{"source":"data_vendor","cusip":"TESTAAPL1","figi":"BBG000B9XRY4","ticker":"AAPL","ca_type":"split","ex_date_et":"2026-07-01","factor":"4.00000000","cash_amount":null,"source_ca_id":"DV-9","announced_ts_utc":"2026-06-20T12:05:00.000000Z"}
```

**H.5 `tests/fixtures/corporate_actions/single_source_blackout.jsonl`** — one source only → SINGLE_SOURCE_BLACKOUT.
```
{"source":"alpaca","cusip":"TESTAAPL1","figi":"BBG000B9XRY4","ticker":"AAPL","ca_type":"dividend","ex_date_et":"2026-08-01","factor":null,"cash_amount":"0.2500","source_ca_id":"ALP-2","announced_ts_utc":"2026-07-20T12:00:00.000000Z"}
```

**H.6 `tests/fixtures/corporate_actions/conflicting_blackout.jsonl`** — two sources disagree on factor → CONFLICTING_BLACKOUT.
```
{"source":"alpaca","cusip":"TESTMSFT1","figi":"BBG000BPH459","ticker":"MSFT","ca_type":"split","ex_date_et":"2026-09-01","factor":"2.00000000","cash_amount":null,"source_ca_id":"ALP-3","announced_ts_utc":"2026-08-20T12:00:00.000000Z"}
{"source":"data_vendor","cusip":"TESTMSFT1","figi":"BBG000BPH459","ticker":"MSFT","ca_type":"split","ex_date_et":"2026-09-01","factor":"3.00000000","cash_amount":null,"source_ca_id":"DV-7","announced_ts_utc":"2026-08-20T12:03:00.000000Z"}
```

**H.7 `tests/fixtures/corporate_actions/ticker_change.jsonl`** — same CUSIP/FIGI, ticker FB→META (durable identity holds).
```
{"source":"alpaca","cusip":"TESTMETA1","figi":"BBG000MM2P62","ticker":"FB","ca_type":"symbol_change","ex_date_et":"2026-10-01","factor":null,"cash_amount":null,"source_ca_id":"ALP-4","announced_ts_utc":"2026-09-25T12:00:00.000000Z"}
{"source":"data_vendor","cusip":"TESTMETA1","figi":"BBG000MM2P62","ticker":"META","ca_type":"symbol_change","ex_date_et":"2026-10-01","factor":null,"cash_amount":null,"source_ca_id":"DV-4","announced_ts_utc":"2026-09-25T12:02:00.000000Z"}
```

**H.8 `tests/fixtures/corporate_actions/incomplete_split.jsonl`** (S7-6) — two sources, type+ex_date agree, but one OMITS the required split factor → must NOT clear (CONFLICTING_BLACKOUT).
```
{"source":"alpaca","cusip":"TESTTSLA1","figi":"BBG000N9P426","ticker":"TSLA","ca_type":"split","ex_date_et":"2026-09-15","factor":"3.00000000","cash_amount":null,"source_ca_id":"ALP-5","announced_ts_utc":"2026-09-01T12:00:00.000000Z"}
{"source":"data_vendor","cusip":"TESTTSLA1","figi":"BBG000N9P426","ticker":"TSLA","ca_type":"split","ex_date_et":"2026-09-15","factor":null,"cash_amount":null,"source_ca_id":"DV-5","announced_ts_utc":"2026-09-01T12:02:00.000000Z"}
```

**H.9 `tests/fixtures/corporate_actions/mirrored_source_ca_id.jsonl`** (S7-3) — distinct CaSource but IDENTICAL `source_ca_id` (a resold single notice) → NOT 2 independent → stays blacked out.
```
{"source":"alpaca","cusip":"TESTAAPL1","figi":"BBG000B9XRY4","ticker":"AAPL","ca_type":"split","ex_date_et":"2026-07-01","factor":"4.00000000","cash_amount":null,"source_ca_id":"SIP-XREF-77","announced_ts_utc":"2026-06-20T12:00:00.000000Z"}
{"source":"data_vendor","cusip":"TESTAAPL1","figi":"BBG000B9XRY4","ticker":"AAPL","ca_type":"split","ex_date_et":"2026-07-01","factor":"4.00000000","cash_amount":null,"source_ca_id":"SIP-XREF-77","announced_ts_utc":"2026-06-20T12:00:00.000000Z"}
```

**H.10 `tests/fixtures/corporate_actions/broker_silent_adjust.json`** — broker qty changed (no known CA) → freeze + immediate reconcile.
```json
{"durable_key":"BBG000B9XRY4","cusip":"TESTAAPL1","figi":"BBG000B9XRY4","ticker":"AAPL","session_date_et":"2026-07-01","seed_qty":"100","observed_qty":"400","blacked_out":false}
```

**H.11 `tests/fixtures/market_state/status_replay_sample.jsonl`** — a hand-built `journal/status.jsonl`: valid rows, a truncated tail (no trailing newline), and a newline-terminated corrupt line, for the replay tests (truncated-tail recoverable vs corrupt-line fatal).

---

## I. Conventions-to-mirror table (with `file:line` source pointers, verified at HEAD `3e270eb`)

| Convention (M2 must mirror) | Source pointer | M2 enforcement point |
|---|---|---|
| Decimal-as-string; reject float; reject non-finite; reject set/frozenset | `serializer.py:27-44` | LULD bands, SSR prior-close, CA factor/cash → Decimal; provenance_set NEVER serialized |
| ONE row hash = sha256 of canonical JSON (the ONLY hash) | `serializer.py:53`; `book_hash.py:22,100` | `canonical_status_payload`/`canonical_ca_payload` reuse `row_hash`; `"v"` first |
| Status ledger via `EventWriter.record` wrapping `JournalWriter` | `persistence.py:52,89` | `StatusLedger` facade; never raw file writes; never re-implement lock/seq/hash/tail |
| Per-stream monotonic seq + single writer lock (resolved-path) | `journal.py:70-81` | open `status.jsonl` by ONE canonical resolved path |
| Append stamping order + one write per row | `journal.py:115-128` | inherited via `EventWriter`→`JournalWriter` |
| Truncated tail dropped; corrupt complete line fatal | `journal.py:28-59,99-108` | `replay_status` → `replay_stream` → `journal.replay` |
| Reserved-key collision rejected (`source_ca_id` NOT `seq`) | `journal.py:21,112-114`; `event.py:14` | CA/status `fields` avoid all 7 reserved names; `rules_hash` non-reserved |
| `run_id`/`decision_id`/`order_id` + per-stream seq (S6) | `journal.py:118-125`; `persistence.py:26-29` | `StatusLedger` SHARES injected `run_id`, mints none |
| Flat `to_row`/`from_row` round-trip identity (FULL provenance persisted) | `event_row.py:12,46-56,77,117` | `ca_to_row`/`ca_from_row`; `ca_from_row(ca_to_row(ev))==ev` |
| Single-file per stream, NO rotation | `persistence.py:23-24` | `status.jsonl` single-file in M2 |
| Frozen dataclass + composed frozen provenance; hash excludes wall-clock write-stamps | `event.py:52-60`; `book_state.py:31-37` | `Verdict`, `AdjustmentEvent`, `CaProvenance`, `SessionSchedule` frozen |
| Closed-vocabulary enum; out-of-vocab FATAL (fail-closed) | `event.py:33`; `book_state.py:11-13` | `SessionState`/`HaltState`/`LuldState`/`SsrState`/`ValidationStatus`/`CaType` |
| Graded exception tree rooted in ValueError | `event.py:37-49` | `CorporateActionError`→`UnvalidatedAdjustment`/`ConflictingSources`/`BrokerAdjustedDuringBlackout` |
| `Decimal(str(x))` + quantize chokepoint; named module-level quanta | `event.py:30-31` + `_quantize_checked` | `FACTOR_QUANTUM`/`CASH_QUANTUM`; CA/LULD numerics |
| Anomaly surfaced as flag/tri-state, never auto-corrected | `book_state.py:11-13,31-37` | CA tri-state `validation_status`; crossed/one-sided NBBO → flag |
| Identity checked on every mutation (durable id, not ticker) | `book_state.py:55-64` | `DurableId.key`; `BrokerAdjustDetector` keyed on durable id |
| ET-logic via stdlib `zoneinfo`; UTC-persist; DST-correct boundary | `bar_cache.py:26-27,31-43` + `1d` branch | `MarketCalendar`; `SessionSchedule.*_utc` |
| `_parse_utc` accepts `Z` and `+00:00`, rejects naive | `bar_cache.py:46-65` | calendar/decider timestamp parsing |
| Lazy-SDK seam (real SDK only on credentialed branch) | `databento.py:82-89,93-102` | `ExchangeCalendarsScheduleProvider._build_calendar`; CA fetchers injected |
| `@runtime_checkable` Protocol seam | `marketdata/base.py` | `ScheduleProvider`, `SessionLiveness`, `CaFetcher` |
| Injected `FakeClock` (no new clock, no wall-time) | `tests/lib/fakes.py:83-94`; `status.py:162` | `MarketStateCache(clock=)` |
| Money newtype vs plain Decimal qty | `serializer.py:15-24,58-61`; `broker/base.py:21,29` | broker qty is a PLAIN Decimal share count (NOT BrokerUSD) |
| tighten-only merge; `min()`==smaller-safe; bool AND; `rules_hash` provenance | `config.py:17,24-43` | safety numbers = code constants; `rules_hash` carried into rows |
| Identity-strict run gates; M2 read is a separate surface | `gates.py:10-30` | `Verdict` never feeds `opening_allowed` |
| EQUS.MINI status downgrade = M2 owns halt/LULD/SSR | `status.py:31-34` | M2 sources status from broker+calendar, NEVER from price action |
| Session-gap seam at the two `stale_symbols` sites (don't fork) | `recorder.py:327-338,340-370`; `status.py:188` | inject `SessionLiveness`; guard both `_emit_alert`; seq path untouched |
| HeartbeatMonitor strict-`>` staleness boundary | `status.py:178` | cache `get` freshness: fresh at exactly ttl, stale at ttl+1 |
| Deterministic fakes (no net/clock/random) | `tests/lib/fakes.py` | scripted fake providers/fetchers in the same style |
| Offline purity: no SDK in `sys.modules`, no socket | `test_no_network_no_creds.py:14-40` | extend `TestNoSdkImported` + `assertNotIn("exchange_calendars", ...)` |

---

## J. Test list — each test file → cases → safety invariant

**`tests/agent/test_no_network_no_creds.py`** (EXTEND existing `TestNoSdkImported` — the load-bearing offline-purity guard; BUILD-BLOCKING)
- EXTEND `TestNoSdkImported`: `import agent.market_calendar; import agent.market_state; import agent.market_state_cache; import agent.corporate_actions; import agent.status_ledger; import agent.session_liveness`, then `self.assertNotIn("exchange_calendars", sys.modules)` (keep `"alpaca"`/`"databento"` asserts). (offline-purity / S1-spirit)
- `test_no_module_scope_import_of_exchange_calendars` → AST/grep-check every M2 module: no module-scope `import exchange_calendars`. (offline-purity; prevents a future regression that crashes collection on a bare checkout)
- `test_fixture_provider_imports_no_exchange_calendars` → construct + query `FixtureScheduleProvider`; `"exchange_calendars" not in sys.modules`. (offline-purity)
- `test_live_provider_build_is_notimplemented_offline` → constructing `ExchangeCalendarsScheduleProvider` imports nothing; `_build_calendar` raises `NotImplementedError`. (offline-purity)
- `test_m2_flows_open_no_socket` → under `mock.patch("socket.socket", side_effect=AssertionError)`, decide + cache + CA cross-validate construct no socket. (offline-purity)

**`tests/agent/test_market_calendar.py`** (DST/§11 + fail-closed)
- `test_phase_pre_rth_post_closed` → each phase classified from `session_instants.jsonl`. (correctness)
- `test_continuous_rth_owns_full_window` → 09:30:00 and 15:59:59 ET are RTH, not blocked (MR-1). (correctness)
- `test_early_close_half_day` → `2026-11-27` 13:00 ET close; 13:30 ET is POST. (correctness)
- `test_holiday_and_weekend_are_closed` → `2026-11-26`/`2026-12-25` and a Saturday → CLOSED. (fail-closed)
- `test_unknown_date_raises_unknown_session_date` → fail-closed, never "assume open". (S7-spirit)
- `test_dst_est_vs_edt_rth_close_offset` → `2026-11-02`(EST) 16:00 ET → `21:00Z` vs `2026-06-15`(EDT) → `20:00Z` — the 1h EST/EDT delta a fixed-offset bug misses (OP-2; the discriminating axis). (DST/§11)
- `test_dst_half_day_est_close` → `2026-11-27`(EST) 13:00 ET → `18:00Z`. (DST/§11)
- `test_spring_forward_gap_boundary_rejected` → a fixture boundary in the 02:00-03:00 ET skipped hour → `CalendarError` (DET-6). (DST fail-closed)

**`tests/agent/test_market_state.py`** (decider across EVERY transition; S7 CA paths)
- `test_decider_is_pure_deterministic` → idempotent same-input. (determinism)
- `test_rth_two_sided_is_tradable`. (correctness)
- `test_no_two_sided_nbbo_blocks` + `test_crossed_book_blocks` → `two_sided_nbbo=False` → NOT_TRADABLE. (fail-closed; replaces boolean flag)
- `test_vendor_halt_and_unknown_halt_block`. (fail-closed)
- `test_resuming_halt_is_not_tradable` → `HaltState.RESUMING` → NOT_TRADABLE until NONE (MR-5). (fail-closed)
- `test_resuming_halt_sets_auction_state` → `HaltState.RESUMING` → `Verdict.session_state == SessionState.AUCTION` (NOT_TRADABLE); the ONLY producer of AUCTION (MED-5). (consistency)
- `test_luld_paused_or_unknown_blocks` + `test_luld_limit_is_reduce_only`. (microstructure/fail-closed)
- `test_luld_band_edge_forces_reduce_only` → NBO at upper band edge with `luld:normal` → REDUCE_ONLY (S7-5). (fail-closed cross-check)
- `test_normal_luld_with_missing_band_blocks` → `session_phase=RTH`, `luld=NORMAL`/`LIMIT`, `luld_band=None` → NOT_TRADABLE, reason `luld_band_unknown` (HIGH-2 step 5b); and PRE/POST with no band is NOT forced NOT_TRADABLE by this rule. (fail-closed)
- `test_luld_band_is_decimal_not_float` → float band raises at serialize. (S2)
- `test_ssr_active_or_unknown_blocks_short` → `short_allowed=False`; `test_decider_does_not_derive_ssr_from_price` (OP-4). (Reg SHO 201/MR-3)
- `test_closed_and_unknown_phase_block`. (correctness/fail-closed)
- `test_pre_post_is_reduce_only`. (conservative default)
- `test_ca_blackout_dominates` + `test_frozen_dominates_everything`. (S7)
- `test_severity_merge_is_tighten_only` → `merge_severity` order-independent, never loosens. (determinism/tighten-only)
- `test_unknown_state_raises_market_state_error`. (fail-closed)
- `test_every_transition_fixture` → `tradability_transitions.jsonl` rows → expected verdicts. (coverage)

**`tests/agent/test_market_state_cache.py`** (cache staleness → safe default; FakeClock)
- `test_fresh_entry_returned_within_ttl`. (freshness)
- `test_boundary_is_strict_greater_than` → fresh at exactly `ttl_ms`, stale at `ttl_ms+1` (mirrors `status.py:178`). (freshness boundary)
- `test_stale_entry_degrades_to_safe_default`. (fail-closed/non-blocking)
- `test_missing_entry_is_safe_default`. (fail-closed)
- `test_safe_default_every_enum_field_most_restrictive` (S7-7). (fail-closed)
- `test_get_is_non_blocking`. (determinism)
- `test_refresh_set_unions_open_positions` → held symbol outside the universe still in the set. (spec §5 Tier-2)
- `test_instrument_id_mismatch_returns_safe_default` → entry stored for `(AAPL, 1001)`, `get("AAPL", 2002, ...)` → `safe_default_verdict` (MED-7). (fail-closed)
- `test_ttl_not_config_overlayable`. (§G trap)
- `test_ttl_override_cannot_loosen` → `MarketStateCache(clock=…, ttl_ms=DEFAULT_FRESHNESS_TTL_MS+1)` raises `ValueError`; a smaller ttl is accepted (HIGH-3 clamp). (never-loosen)

**`tests/agent/test_corporate_actions.py`** (S7 — the primary invariant)
- `test_two_independent_sources_confirm` → `two_source_confirmed.jsonl` → CONFIRMED, `provenance_independent=true`, blackout=False. (S7)
- `test_single_source_stays_blacked_out` → SINGLE_SOURCE_BLACKOUT; `is_blacked_out` True. (S7)
- `test_conflicting_sources_blackout` → factor 2 vs 3 → CONFLICTING_BLACKOUT. (S7)
- `test_type_disagreement_same_exdate_conflicts` → two sources, same `(durable_key, ex_date)`, DIFFERENT `ca_type` → grouped together (ca_type NOT a group key) → CONFLICTING_BLACKOUT, NOT two single-source groups (LOW-3). (S7)
- `test_window_override_cannot_shrink` → `cross_validate(..., lead_days=0)` or `trail_days=0` raises `CorporateActionError`; a wider window is accepted (HIGH-3 clamp). (never-loosen)
- `test_incomplete_required_field_does_not_clear` → `incomplete_split.jsonl` (one factor None) → CONFLICTING_BLACKOUT (S7-6). (S7)
- `test_mirrored_source_ca_id_not_two_independent` → `mirrored_source_ca_id.jsonl` → not 2 independent → blacked out (S7-3). (S7)
- `test_same_source_twice_not_two_independent`. (S7)
- `test_validate_is_pure_function_of_provenance_set`. (determinism/S7)
- `test_blackout_window_is_closed_inclusive` → `is_blacked_out` True on `blackout_from_et` AND `blackout_to_et`, False day-after only when CONFIRMED (S7-4). (S7)
- `test_unknown_ca_type_raises`. (fail-closed)
- `test_factor_must_be_decimal_and_round_trip`. (S2)
- `test_durable_identity_survives_ticker_change` → `ticker_change.jsonl` (FB→META, same FIGI) → same `key`; reused ticker does not cross-contaminate (mirrors `book_state._check_identity`). (S7/identity)
- `test_ticker_only_identity_rejected`. (fail-closed)
- `test_any_unexplained_broker_delta_freezes` → qty 100→400 with `blacked_out=false` → `FreezeSignal(immediate_reconcile=True, reason=broker_adjusted_no_known_ca)` (S7-1). (S7)
- `test_broker_delta_during_blackout_labels_reason` → same delta with `blacked_out=true` → reason `broker_adjusted_during_blackout`. (S7)
- `test_missing_baseline_observe_raises` → restart mid-CA, no `seed_baseline()` → `BrokerAdjustedDuringBlackout` (S7-2). (S7)
- `test_ca_implied_qty_cannot_override_broker_baseline` (DET-3/OP-3). (S5-spirit)
- `test_qty_equal_baseline_is_no_freeze`. (S7)
- `test_frozenset_field_never_serialized_directly` → `dumps({"x": frozenset(...)})` raises `TypeError` (DET-2). (S2)

**`tests/agent/test_status_ledger.py`** (status.jsonl + corporate_action rows; replay/rehydrate — S3/S6)
- `test_transition_rows_round_trip_through_serializer`. (S2/S3)
- `test_corporate_action_row_roundtrips` → `ca_from_row(ca_to_row(ev)) == ev` incl. full per-source provenance (DET-1). (replay integrity)
- `test_corporate_action_row_carries_instrument_id` → `record_corporate_action(adjustment=…, instrument_id=1001, …)` emits a row whose `instrument_id == 1001`; same for `record_broker_adjust_freeze` (HIGH-1). (schema/correlation)
- `test_luld_transition_requires_non_null_band` → `record_luld_transition(..., reference_px=None)` is a type/contract error; a present-band row round-trips (MED-8). (schema)
- `test_rules_hash_carried_on_every_row`. (provenance/S9-spirit)
- `test_no_reserved_key_collision`. (S6)
- `test_canonical_payload_version_first`. (determinism)
- `test_rehydrate_state_latest_row_wins` + `test_rehydrate_orders_by_seq_not_ts` → two same-`ts_market_utc` transitions ordered by `seq` (DET-5). (S3)
- `test_replay_drops_truncated_tail_not_fatal`. (S3)
- `test_replay_corrupt_midline_is_fatal` → `JournalCorruption`. (S3)
- `test_replay_detects_tampered_hash`. (S3)
- `test_shared_seq_across_two_writers_same_path`. (S6)
- `test_float_in_band_or_factor_raises`. (S2)

**`tests/recorder/test_session_aware_gap.py`** (session-aware gap seam; recorder edit is SCOPED)
- `test_heartbeat_alarm_suppressed_when_session_closed` → injected closed-session liveness → 0 `heartbeat_timeout` alerts at BOTH guarded sites (`recorder.py:334`, `:366`). (no false alarm)
- `test_heartbeat_alarm_fires_when_session_open` → during RTH the alert still fires. (S4 input; no regression)
- `test_liveness_none_is_byte_identical_to_m1` → `liveness=None` → unchanged behavior. (back-compat)
- `test_seq_path_unaffected_by_session` → `SequencePolicy.NONE` never fires regardless of session. (no-conflation)

**`tests/agent/test_config_market_state.py`** (config tighten-only; reuses `tighten_only_merge`)
- `test_market_state_overlay_cannot_loosen` → an overlay flipping a CA/blackout gate or injecting a smaller threshold is refused (mirrors `test_config_canary.py:60-64`). (tighten-only)
- `test_min_ca_sources_not_overlayable_below_two`. (§G trap)
- `test_calendar_pin_string_not_min_merged`. (provenance)
- `test_rules_hash_over_assembled_dict_not_per_file`. (provenance)

Every fail-closed restriction has a named test that goes RED when its guard is mutated away (UNKNOWN→TRADABLE, 1-source-clears, incomplete-clears, mirrored-id-counts-as-2, stale-serves-tradable, ticker-identity-match, any-broker-delta-ignored, missing-baseline-silent-seed, band-edge-ignored, an unguarded `import exchange_calendars`).

---

## K. Consensus addendum — resolved tensions (so the build agent does not relitigate)

1. **`SessionState` vs `Tradability` are two types.** `SessionState` is the resolved phase (incl. HALTED/UNKNOWN); `Tradability` is the READ M4/M5 consume. Keeping them separate prevents reading "RTH" as "go".
2. **`ts_market_utc` IN the payload (divergence from `book_hash`).** A *transition* is a dated fact; the journal's own `ts_utc`/`seq` write-stamps are journal-owned and re-read (not recomputed) on replay, so byte-replay stability holds.
3. **All M2 safety numbers are CODE CONSTANTS, not config** — dodging the `min()`-merge foot-gun (`config.py:35-41`) entirely. Only provenance strings (calendar pin, MIC) live in config.
4. **CA independence is structural AND id-distinct** — two distinct `CaSource` members with distinct `source_ca_id` (S7-3). A mirrored id (resold notice) does NOT count as 2.
5. **The freeze is decoupled from blackout (S7-1).** ANY unexplained broker qty delta freezes + forces immediate reconcile; the blackout state only labels the reason. Missing baseline → freeze, never silent seed (S7-2).
6. **PRE/POST → REDUCE_ONLY; LULD `LIMIT`/band-edge → REDUCE_ONLY; PAUSED/UNKNOWN/RESUMING → NOT_TRADABLE.** Conservative; a band edge cross-checks the vendor label against the live price.
7. **M2 computes NO LULD percentage and NO SSR 10% trigger; the open/close cross is honest continuous RTH** (no hardcoded auction-suspension; `OPEN_CLOSE_BUFFER_S` default 0). `AUCTION` is reserved for vendor-signalled halt resumption.
8. **The session-liveness seam is a SCOPED M1 edit to `recorder.py`** (`liveness=None` byte-identical to M1), not a no-op import.
9. **Calendar fixture is the offline source of truth.** DST/calendar tests run ENTIRELY against `FixtureScheduleProvider` (zoneinfo); the real lib is never imported offline. The fixture instants were cross-checked against real `exchange_calendars==4.13.2`.

---

## L. Offline-now vs deferred (tier-2a/2b split — mirrors M1)

| Item | Offline-now (this build) | Deferred (account/cost-gated) |
|---|---|---|
| Calendar | `FixtureScheduleProvider` (stdlib zoneinfo), pinned fixture, all DST/half-day/holiday tests | `ExchangeCalendarsScheduleProvider` live wiring; `requirements.txt` pin un-commented; runtime re-verify on the live venv |
| Vendor/broker status (halt/LULD/SSR) | Injected `StatusFlags` from scripted fakes | Alpaca field → M2-enum mapping (Alpaca lands M5) |
| Corporate-action feed | Injected `CaFetcher` scripted fakes; full tri-state validation tested offline | Live Alpaca-CA + data-vendor-CA fetchers behind the lazy/credentialed seam |
| CONFIRMED-clear trust | Offline tests MAY clear with ≥2 independent sources | **Live CONFIRMED-clear DISABLED until upstream independence is human-verified** (DECIDED — Robin, 2026-06-09; see §M) |
| Broker-adjust baseline seeding | `seed_baseline` API + missing-baseline-raises tested | Exact SOD seeding sequence lands in M6 reconcile |

Nothing requiring an unprovisioned live subscription blocks the offline build — exactly as M1 tier-2b is deferred.

---

## M. Open items deliberately frozen / deferred

> **Scope decisions confirmed by Robin (2026-06-09), now frozen:**
> 1. **Open/close buffer** → `OPEN_CLOSE_BUFFER_S = 0` (OFF) in M2. The decider models only market-state facts; open/close avoidance is a strategy/risk concern (M4/M7), not a hardcoded NOT_TRADABLE window. The knob remains for a later deliberate change.
> 2. **Live CONFIRMED-clear** → **DISABLED** until the two live CA feeds are human-verified as genuinely independent (illusory-independence risk). Costs nothing now (M2 trades nothing; the live feed is deferred behind the credentialed seam); offline tests still exercise the full clear path against scripted fakes.

- **`exchange_calendars` pin = `4.13.2`** (empirically verified: installs + `is_session`/`session_close` correct under `pandas 3.0.3` / `numpy 2.4.6` / Python 3.14.4). `4.5.6` was tested and is BROKEN under pandas 3 (`DateOutOfBounds`/`<exception str() failed>`) — do NOT pin it. The `requirements.txt:6` line stays a COMMENT until the live path is wired; re-run the import + `is_session` + `session_close` runtime check on the live venv before un-commenting (Python-version + pandas axis, not just metadata).
- **Live broker/vendor status + CA feed wiring** deferred behind the lazy seam (Alpaca lands M5 per `requirements.txt:7`). Offline uses injected scripted fakes.
- **Live CONFIRMED-clear trust** — if Alpaca CA and the data-vendor CA resell the same exchange notice, "2 independent sources" is illusory; live CONFIRMED-clear stays DISABLED until human-verified (DECIDED — Robin, 2026-06-09, see note above).
- **`BrokerAdjustDetector` SOD baseline seeding sequence** lands in M6 reconcile; M2 ships `seed_baseline` + the fail-closed missing-baseline raise.
- **Runtime config loader** — none exists; M2 reuses the inline `{agent_rules, risk_rules}` assembly or introduces the first loader (a build-cost decision); any loader hashes the ASSEMBLED dict via `rules_hash`, never per-file. `data_retention.json` joins the hashed dict only if status.jsonl rotation lands (deferred).

---

## N. References (file:line evidence, verified at HEAD `3e270eb`)

- Serializer / hash / money: `scripts/agent/serializer.py:15-24,27-44,47-55,58-61`
- Journal append / reserved / replay / per-stream seq: `scripts/agent/journal.py:21,28-59,70-81,99-108,110-129`
- Persistence wrap / streams / record / replay_stream / single-file: `scripts/recorder/persistence.py:23-24,26-29,40,43,47-49,52-64,89-99,102-108`
- Event-row flat seam / full-provenance round-trip: `scripts/recorder/event_row.py:12,46-56,77-108,111-113,117`
- Typed events / vendor_seq lesson / closed vocab / exceptions / quanta: `scripts/recorder/event.py:14-18,22,30-33,37-49,52-60`
- Book state identity / crossed-as-flag: `scripts/recorder/book_state.py:11-13,26,31-37,55-64`
- Book hash reuse / version-first / requantize: `scripts/recorder/book_hash.py:22,31,62-65,90-100`
- ET/DST mechanism: `scripts/recorder/bar_cache.py:3-5,26-27,31-43,46-65` + `_bucket_end_utc_str` `1d` branch
- Status downgrade / heartbeat / seq policy / alert builder: `scripts/recorder/status.py:13,31-34,37-39,77-78,162,178,188-194,197-225`
- Recorder stale-symbols sites / init / clock: `scripts/recorder/recorder.py:154-168,316,323,327-338,340-370`
- Config merge / rules_hash / gates: `scripts/agent/config.py:17-21,24-43`; `scripts/agent/gates.py:10-30`
- Lazy SDK seam: `scripts/agent/marketdata/databento.py:42-48,82-85,93-102`
- FakeClock: `tests/lib/fakes.py:83-94`
- Offline-purity test: `tests/agent/test_no_network_no_creds.py:14-40`
- Committed config + tighten-only canary: `tests/agent/test_config_canary.py:23-27,31-35,56-64`; `config/agent_rules.json:1-2`; `config/risk_rules.json:1-3`
- Broker qty type: `scripts/agent/broker/base.py:21,29`
- Requirements pin placeholder: `requirements.txt:6-8`
- exchange_calendars empirical: `4.5.6` broken (`DateOutOfBounds`/`<exception str() failed>`) vs `4.13.2` correct (`is_session('2026-11-26')=False`, `session_close('2026-11-02')=21:00Z`, `session_close('2026-11-27')=18:00Z`) under `pandas 3.0.3`/`numpy 2.4.6`/Python 3.14.4

---

## External review round 1 (GPT, 2026-06-09) — verified against the contract text, then applied

Each finding was checked against the actual §-text before acting (not implemented blindly). 9 accepted as-stated, 1 accepted-with-softened-fix, 1 accepted-finding-but-pushed-back-on-the-proposed-fix.

| ID | Sev | Finding (verified) | Disposition |
|---|---|---|---|
| HIGH-1 | HIGH | `AdjustmentEvent`/`FreezeSignal` carry no `instrument_id`, but the ledger common row prefix + sample rows require it → a build agent would guess where it comes from. | **APPLIED.** `instrument_id` is now a REQUIRED explicit kwarg on `record_corporate_action` / `record_broker_adjust_freeze` (the CA feed is identity-DURABLE — CUSIP/FIGI — so the numeric id is supplied by the caller, NOT carried on the event). Added §J `test_corporate_action_row_carries_instrument_id`. |
| HIGH-2 | HIGH | `luld=NORMAL` + `luld_band=None` escaped restriction: step 7's `band known` guard skipped it and step 5 only caught `luld in {PAUSED,UNKNOWN}` — so the §B "unknown band → most-restrictive" prose was not load-bearing. | **APPLIED.** New decider **step 5b**: during RTH, `luld in {NORMAL,LIMIT}` with `luld_band is None` → NOT_TRADABLE, reason `luld_band_unknown` (LULD is an RTH mechanism; PRE/POST unaffected). Fixture row + §J `test_normal_luld_with_missing_band_blocks`. |
| HIGH-3 | HIGH | Safety numbers are code constants (good) but `cross_validate(lead_days,trail_days)` and `MarketStateCache(ttl_ms)` could still be LOOSENED via caller params. | **APPLIED.** Never-loosen clamps: `cross_validate` raises unless `lead_days>=BLACKOUT_LEAD_DAYS` and `trail_days>=BLACKOUT_TRAIL_DAYS`; `MarketStateCache.__init__` raises unless `ttl_ms<=DEFAULT_FRESHNESS_TTL_MS`. §J `test_window_override_cannot_shrink`, `test_ttl_override_cannot_loosen`. |
| MED-4 | MED | LULD double-width prose ("first 15 min") is disputed/outdated vs the SIP/Plan; non-load-bearing since M2 consumes only the vendor `doubled` flag. | **APPLIED (softened fix).** Removed the specific minute-windows from the `LuldBand` docstring + §B prose; state only that doubling occurs at SIP/Plan-defined intervals and that M2 encodes NO window — rather than assert a regulatory-history claim that cannot be verified offline. |
| MED-5 | MED | `SessionState.AUCTION` was defined but unreachable — `RESUMING` mapped to `HALTED`, so consumers never saw a distinct auction state. | **APPLIED.** Decider maps `HaltState.RESUMING` → `SessionState.AUCTION` (still NOT_TRADABLE) — now the sole producer of AUCTION (calendar `phase_at` never emits it). §J `test_resuming_halt_sets_auction_state`. |
| MED-6 | MED | `is_blacked_out` doc was self-contradictory (sentence 1: only non-CONFIRMED blackout; sentence 2: CONFIRMED blacks out in-window). | **APPLIED (finding); PUSHED BACK on the proposed fix.** GPT proposed "CONFIRMED never blacks out" — that contradicts spec §5 ("treat ex-date windows as a trading blackout") AND this contract's own `test_blackout_window_is_closed_inclusive` ("True on `from` AND `to`, False day-after **only when CONFIRMED**"). Applied the spec-coherent rule instead: **CONFIRMED → bounded ex-date window; non-CONFIRMED → open-ended from window start.** |
| MED-7 | MED | The per-symbol cache did not reject a cached verdict whose `instrument_id` mismatched the request (ticker reuse / stale definition could serve a prior instrument's "tradable"). | **APPLIED.** Cache keyed on `(symbol, instrument_id)`; mismatch → MISS → `safe_default_verdict`. §J `test_instrument_id_mismatch_returns_safe_default`. |
| MED-8 | MED | `record_luld_transition` typed band prices `Optional[Decimal]` but the row schema is non-null `Dec-str`. | **APPLIED.** Band prices are REQUIRED non-null `Decimal` (an absent band is `luld_band_unknown`, not a null-priced LULD row). §J `test_luld_transition_requires_non_null_band`. |
| LOW-1 | LOW | `safe_default_verdict` said `SessionState.UNKNOWN-or-HALTED`; a test needs one exact value. | **APPLIED.** Pinned to `SessionState.UNKNOWN` (honest "don't know"; HALTED would assert an unobserved halt). |
| LOW-2 | LOW | `TradabilityInputs` lacked `session_date_et` but `Verdict` requires it. | **APPLIED.** Added `session_date_et` to `TradabilityInputs`; the builder supplies it via `MarketCalendar.session_date_for(ts_utc)` so `decide()` stays pure. |
| LOW-3 | LOW | `cross_validate` grouped by `(durable_key, ca_type, ex_date)`, so a type DISAGREEMENT split sources into separate groups and hid the conflict. | **APPLIED.** Grouping is `(durable_key, ex_date_et)` only — `ca_type` is a validated-within-group field so a type disagreement on the same ex-date → CONFLICTING_BLACKOUT. §J `test_type_disagreement_same_exdate_conflicts`. |

**Net effect:** the decider's fail-closed posture is now complete on the LULD-band axis (5b), the AUCTION state is reachable, the blackout rule is internally coherent and spec-aligned, the cache cannot serve a cross-instrument verdict, and no safety quantity is loosenable via config OR caller param. The contract remains READY-TO-BUILD at the same depth; no API was left under-specified.
