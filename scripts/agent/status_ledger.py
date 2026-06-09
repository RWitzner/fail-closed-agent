"""M2 §E — status/halt/CA ledger → ``journal/status.jsonl``.

M2 is the FIRST producer of the ``status`` stream (``STREAM_STATUS`` is defined at
``persistence.py:49`` but unwritten at HEAD; this schema is net-new). ``StatusLedger`` is a
thin FACADE over ``recorder.persistence.EventWriter`` (which wraps
``agent.journal.JournalWriter``): every row goes ``StatusLedger`` →
``EventWriter.record`` → ``JournalWriter.append`` → ``agent.serializer``. **No new writer,
no new hash, no new serialization.** One ``StatusLedger`` per resolved ``status.jsonl``
path so the per-stream monotonic ``seq`` + writer lock stay shared (``journal.py:70-81``);
M2 mints **no** ``run_id`` — it shares the injected agent/orchestrator ``run_id`` (S6).

Field-naming rule (BLOCKER-1 lesson): NO payload field may be named
``seq``/``event_type``/``run_id``/``hash``/``decision_id``/``order_id``/``ts_utc`` (the
journal ``_RESERVED`` set, ``journal.py:21``; a collision raises at ``journal.py:114``). A
vendor ordinal is ``source_ca_id`` (§D), never ``seq``. ``rules_hash`` is a non-reserved
plain provenance string. ``ts_market_utc`` (the market instant of the transition) is a
PAYLOAD field, distinct from the journal's own ``ts_utc`` write-stamp.

DETERMINISM (DET-2): no set/frozenset ever enters a row — only ``provenance_sources`` (a
sorted list) and the flat ``provenance`` list of sublists. Decimals stay Decimal (the
serializer renders them as strings and rejects floats / non-finite — ``serializer.py:27-44``).
"""
from decimal import Decimal
from typing import Optional

from agent.corporate_actions import (
    AdjustmentEvent,
    CaProvenance,
    CaSource,
    CaType,
    DurableId,
    FreezeReason,
    ValidationStatus,
)
from agent.journal import JournalCorruption  # re-exported; either import path is valid
from recorder.event_row import MalformedRecord
from recorder.persistence import EventWriter, STREAM_STATUS, replay_stream

__all__ = [
    "STATUS_LEDGER_VERSION",
    "EVT_SESSION_TRANSITION",
    "EVT_HALT_TRANSITION",
    "EVT_LULD_TRANSITION",
    "EVT_SSR_TRANSITION",
    "EVT_CORPORATE_ACTION",
    "EVT_CA_BLACKOUT_TRANSITION",
    "EVT_BROKER_ADJUST_FREEZE",
    "StatusLedger",
    "canonical_status_payload",
    "ca_to_row",
    "ca_from_row",
    "replay_status",
    "rehydrate_state",
    "EventWriter",
    "STREAM_STATUS",
    "JournalCorruption",
    "MalformedRecord",
]

STATUS_LEDGER_VERSION = 1  # payload-schema version; FIRST key in every canonical_*_payload helper

# event_type tags (M2's choice; none collides with the journal _RESERVED set — journal.py:21):
EVT_SESSION_TRANSITION = "session_transition"
EVT_HALT_TRANSITION = "halt_transition"
EVT_LULD_TRANSITION = "luld_transition"
EVT_SSR_TRANSITION = "ssr_transition"
EVT_CORPORATE_ACTION = "corporate_action"
EVT_CA_BLACKOUT_TRANSITION = "ca_blackout_transition"
EVT_BROKER_ADJUST_FREEZE = "broker_adjust_freeze"


def _require_decimal(value, *, field: str) -> Decimal:
    """A REQUIRED non-null Decimal field (fail-closed). Rejects None/float/non-Decimal up
    front so an absent band / factor never reaches the journal as a null-priced row (MED-8).
    bool is an int subclass — reject it too."""
    if value is None:
        raise ValueError(f"{field}: required non-null Decimal (got None)")
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, Decimal):
        raise TypeError(f"{field}: must be a Decimal (got {type(value).__name__}={value!r})")
    if not value.is_finite():
        raise ValueError(f"{field}: non-finite Decimal not allowed ({value!r})")
    return value


def _optional_decimal(value, *, field: str) -> Optional[Decimal]:
    """An OPTIONAL Decimal field (None allowed — e.g. SSR prior_close before the first
    close). A present value must still be a finite Decimal (no float, no bool)."""
    if value is None:
        return None
    return _require_decimal(value, field=field)


def canonical_status_payload(*, version: int, body: dict) -> dict:
    """Pure helper exposing the pre-hash payload with ``'v'`` FIRST (white-box determinism;
    mirrors ``canonical_book_payload``, book_hash.py:90). The journal still computes the row
    hash via ``row_hash``. ``ts_market_utc`` IS a payload field (a transition is a dated
    FACT — divergence from ``book_hash``); the journal's own ``ts_utc``/``seq`` write-stamps
    are journal-owned and RE-READ (not recomputed) on replay, so byte-replay stability holds.
    """
    return {"v": version, **body}


def ca_to_row(adjustment: AdjustmentEvent) -> dict:
    """Flat persistence form (mirrors ``event_row.to_row``, event_row.py:77). Persists the
    FULL per-source provenance (DET-1) so ``ca_from_row(ca_to_row(ev)) == ev``:
    ``provenance = [[source, source_ca_id, announced_ts_utc, ts_recv_utc], ...]``.
    ``provenance_sources = sorted(s.value for s in provenance_set)`` is a DERIVED convenience
    list; ``provenance_set`` (the frozenset) is NEVER serialized (DET-2). Decimals stay Decimal.
    """
    durable = adjustment.durable_id
    return {
        "cusip": durable.cusip,
        "figi": durable.figi,
        "durable_key": durable.key(),
        "ticker": durable.ticker,
        "ca_type": adjustment.ca_type.value,
        "ex_date_et": adjustment.ex_date_et,
        "factor": adjustment.factor,
        "cash_amount": adjustment.cash_amount,
        "provenance": [
            [p.source.value, p.source_ca_id, p.announced_ts_utc, p.ts_recv_utc]
            for p in adjustment.provenance
        ],
        "provenance_sources": sorted(s.value for s in adjustment.provenance_set),
        "provenance_independent": adjustment.provenance_independent,
        "validation_status": adjustment.validation_status.value,
        "blackout": adjustment.blackout,
        "blackout_from_et": adjustment.blackout_from_et,
        "blackout_to_et": adjustment.blackout_to_et,
    }


def _require_field(row: dict, key: str):
    if key not in row:
        raise MalformedRecord(f"missing required CA row field {key!r}")
    return row[key]


def _opt_decimal_from(value) -> Optional[Decimal]:
    if value is None:
        return None
    return Decimal(str(value))


def ca_from_row(row: dict) -> AdjustmentEvent:
    """Exact inverse of ``ca_to_row``: ``ca_from_row(ca_to_row(ev)) == ev`` (mirrors
    ``event_row.from_row``, event_row.py:117). Rebuilds ``provenance_set`` from the persisted
    ``provenance`` list; Decimals via ``Decimal(str(value))``; a missing field raises
    ``MalformedRecord``."""
    if not isinstance(row, dict):
        raise MalformedRecord(f"CA row must be a dict (got {type(row).__name__})")

    durable = DurableId(
        cusip=_require_field(row, "cusip"),
        figi=_require_field(row, "figi"),
        ticker=_require_field(row, "ticker"),
    )
    raw_prov = _require_field(row, "provenance")
    provenance = []
    for entry in raw_prov:
        if not isinstance(entry, (list, tuple)) or len(entry) != 4:
            raise MalformedRecord(f"provenance entry must be a 4-list (got {entry!r})")
        source_str, source_ca_id, announced_ts_utc, ts_recv_utc = entry
        provenance.append(
            CaProvenance(
                source=CaSource(source_str),  # out-of-vocab -> ValueError (fail-closed)
                source_ca_id=source_ca_id,
                announced_ts_utc=announced_ts_utc,
                ts_recv_utc=ts_recv_utc,
            )
        )
    provenance_set = frozenset(p.source for p in provenance)

    return AdjustmentEvent(
        durable_id=durable,
        symbol=durable.ticker,
        ca_type=CaType(_require_field(row, "ca_type")),
        ex_date_et=_require_field(row, "ex_date_et"),
        factor=_opt_decimal_from(_require_field(row, "factor")),
        cash_amount=_opt_decimal_from(_require_field(row, "cash_amount")),
        provenance_set=provenance_set,
        provenance=tuple(provenance),
        validation_status=ValidationStatus(_require_field(row, "validation_status")),
        provenance_independent=_require_field(row, "provenance_independent"),
        blackout_from_et=_require_field(row, "blackout_from_et"),
        blackout_to_et=_require_field(row, "blackout_to_et"),
    )


class StatusLedger:
    """Facade over ``EventWriter`` for ``journal/status.jsonl``. ONE ``StatusLedger`` per
    resolved path; shares the injected ``run_id``. Carries ``rules_hash`` as a plain
    non-reserved provenance field on every row (config.py:17), keying each transition to the
    effective config that produced it. NO new hashing/serialization."""

    def __init__(self, writer: EventWriter, *, rules_hash: str) -> None:
        self._writer = writer
        self._rules_hash = rules_hash

    def _common(self, *, symbol: str, instrument_id: int, ts_market_utc: str) -> dict:
        """The common row prefix on every status row (§E)."""
        return {
            "v": STATUS_LEDGER_VERSION,
            "symbol": symbol,
            "instrument_id": instrument_id,
            "ts_market_utc": ts_market_utc,
            "rules_hash": self._rules_hash,
        }

    def record_session_transition(self, *, symbol, instrument_id, from_state, to_state, cause,
                                  session_date_et, ts_market_utc, decision_id=None) -> dict:
        fields = self._common(symbol=symbol, instrument_id=instrument_id, ts_market_utc=ts_market_utc)
        fields.update(
            from_state=from_state,
            to_state=to_state,
            cause=cause,
            session_date_et=session_date_et,
        )
        return self._writer.record(EVT_SESSION_TRANSITION, fields, decision_id=decision_id)

    def record_halt_transition(self, *, symbol, instrument_id, from_state, to_state, halt_reason,
                               ts_market_utc, decision_id=None) -> dict:
        fields = self._common(symbol=symbol, instrument_id=instrument_id, ts_market_utc=ts_market_utc)
        fields.update(
            from_state=from_state,
            to_state=to_state,
            halt_reason=halt_reason,
        )
        return self._writer.record(EVT_HALT_TRANSITION, fields, decision_id=decision_id)

    def record_luld_transition(self, *, symbol, instrument_id, from_state, to_state, luld_tier,
                               reference_px: Decimal, lower_px: Decimal, upper_px: Decimal,
                               doubled: bool, ts_market_utc) -> dict:
        """A ``luld_transition`` records a CONCRETE band change -> the three band prices are
        REQUIRED non-null Decimals (MED-8; the Dec-str row schema is NOT nullable for these).
        An ABSENT band is NOT a ``luld_transition``: it routes through the decider as
        ``luld_band_unknown`` (§B step 5b) -> a halt/session transition, never a null-priced
        LULD row."""
        reference_px = _require_decimal(reference_px, field="reference_px")
        lower_px = _require_decimal(lower_px, field="lower_px")
        upper_px = _require_decimal(upper_px, field="upper_px")
        if not isinstance(doubled, bool):
            raise TypeError(f"doubled: must be a bool (got {type(doubled).__name__})")
        fields = self._common(symbol=symbol, instrument_id=instrument_id, ts_market_utc=ts_market_utc)
        fields.update(
            from_state=from_state,
            to_state=to_state,
            luld_tier=luld_tier,
            reference_px=reference_px,
            lower_px=lower_px,
            upper_px=upper_px,
            doubled=doubled,
        )
        return self._writer.record(EVT_LULD_TRANSITION, fields)

    def record_ssr_transition(self, *, symbol, instrument_id, from_state, to_state,
                              prior_close_px: Optional[Decimal], ts_market_utc) -> dict:
        prior_close_px = _optional_decimal(prior_close_px, field="prior_close_px")
        fields = self._common(symbol=symbol, instrument_id=instrument_id, ts_market_utc=ts_market_utc)
        fields.update(
            from_state=from_state,
            to_state=to_state,
            prior_close_px=prior_close_px,
        )
        return self._writer.record(EVT_SSR_TRANSITION, fields)

    def record_corporate_action(self, *, adjustment: AdjustmentEvent, instrument_id: int,
                                ts_market_utc, decision_id=None) -> dict:
        """Flatten an ``AdjustmentEvent`` via ``ca_to_row`` and ``EventWriter.record``.
        Decimals stay Decimal. ``instrument_id`` is a REQUIRED arg (HIGH-1): the common row
        prefix carries it (§E), but the ``AdjustmentEvent`` does NOT (the CA feed is
        identity-DURABLE — CUSIP/FIGI — not market-data-numeric). CA identity-OF-RECORD is the
        ``durable_key``; ``instrument_id`` is the CURRENT numeric id at emit time, supplied by
        the caller (which holds the symbol<->instrument_id map from definitions/book_state)."""
        fields = self._common(
            symbol=adjustment.durable_id.ticker,
            instrument_id=instrument_id,
            ts_market_utc=ts_market_utc,
        )
        fields.update(ca_to_row(adjustment))
        return self._writer.record(EVT_CORPORATE_ACTION, fields, decision_id=decision_id)

    def record_broker_adjust_freeze(self, *, freeze_signal, instrument_id: int, ts_market_utc) -> dict:
        """``instrument_id`` REQUIRED for the same reason as ``record_corporate_action``
        (HIGH-1); the ``FreezeSignal`` carries the durable id, the caller supplies the current
        numeric id."""
        durable = freeze_signal.durable_id
        fields = self._common(
            symbol=durable.ticker,
            instrument_id=instrument_id,
            ts_market_utc=ts_market_utc,
        )
        fields.update(
            cusip=durable.cusip,
            figi=durable.figi,
            durable_key=durable.key(),
            ticker=durable.ticker,
            prev_qty=_require_decimal(freeze_signal.prev_qty, field="prev_qty"),
            curr_qty=_require_decimal(freeze_signal.curr_qty, field="curr_qty"),
            immediate_reconcile=bool(freeze_signal.immediate_reconcile),
            reason=freeze_signal.reason.value
            if isinstance(freeze_signal.reason, FreezeReason)
            else freeze_signal.reason,
        )
        return self._writer.record(EVT_BROKER_ADJUST_FREEZE, fields)


def replay_status(status_path) -> list:
    """Re-read ``journal/status.jsonl`` hash-verified via
    ``recorder.persistence.replay_stream`` (delegates to ``agent.journal.replay``,
    journal.py:28) — SAME truncated-tail + ``JournalCorruption`` semantics as M1."""
    return replay_stream(status_path)


# event_types that describe a per-SYMBOL tradability/session/halt/LULD/SSR transition.
_SYMBOL_STATE_EVENTS = frozenset(
    {
        EVT_SESSION_TRANSITION,
        EVT_HALT_TRANSITION,
        EVT_LULD_TRANSITION,
        EVT_SSR_TRANSITION,
    }
)

# event_types describing a per-durable_key CA blackout / freeze transition.
_CA_STATE_EVENTS = frozenset(
    {
        EVT_CORPORATE_ACTION,
        EVT_CA_BLACKOUT_TRANSITION,
        EVT_BROKER_ADJUST_FREEZE,
    }
)


def rehydrate_state(rows) -> dict:
    """PURE fold in ASCENDING journal ``seq`` order (the row's stamped ``seq``, journal.py:120).
    'latest-row-wins' = HIGHEST ``seq`` per (symbol | durable_key) — NOT ``ts_market_utc``,
    which can repeat across distinct transitions stamped at the same market instant (DET-5).
    Returns ``{"sessions": {symbol -> latest tradability/session row},
    "corporate_actions": {durable_key -> latest CA blackout/freeze row}}``. Replaying the SAME
    rows yields the SAME state (order-independent: the fold sorts by ``seq`` first)."""
    sessions: dict = {}
    corporate_actions: dict = {}
    for row in sorted(rows, key=lambda r: r["seq"]):
        event_type = row.get("event_type")
        if event_type in _SYMBOL_STATE_EVENTS:
            sessions[row["symbol"]] = row  # higher seq overwrites — latest-row-wins
        elif event_type in _CA_STATE_EVENTS:
            corporate_actions[row["durable_key"]] = row
    return {"sessions": sessions, "corporate_actions": corporate_actions}
