"""Append-only JSONL writer for recorder streams (contract §E).

`persistence.EventWriter` WRAPS / REUSES `agent.journal.JournalWriter` — it does
NOT re-implement the lock / seq / hash / tail loop. This is a frozen decision: two
journal implementations would drift on partial-write / tamper semantics and silently
break S3 / S6. So `EventWriter` inherits VERBATIM from `JournalWriter`:

- single-writer lock per resolved path (journal.py:74,115),
- per-stream monotonic journal `seq` (journal.py:116,120),
- one `dumps(row) + "\\n"` write per row (journal.py:128),
- row hash (journal.py:126), sorted keys (serializer.py:50),
- truncated-tail repair on open (journal.py:99),
- `JournalCorruption` on a complete corrupt line (journal.py:57),
- the `_RESERVED`-key collision guard (journal.py:113-114).

It persists EXACTLY the flat `recorder.event_row.to_row(event)` shape (§B2) — the
provenance prefix (schema, dataset, instrument_id, symbol, `vendor_seq`,
ts_event_utc, ts_recv_utc, reconnect_epoch) plus the per-type payload, plus (for a
DepthEvent) `derived_book_hash`. Because the flat shape uses `vendor_seq` (BLOCKER 1)
rather than `seq`, no field ever collides with the journal's reserved monotonic seq,
so `write_event` never raises at journal.py:114.

MINOR 9 (rotation): daily rotation is OUT of M1-offline scope. M1 is SINGLE-FILE per
stream; the per-file writer is byte-identical to M0.

`run_id`: the recorder SHARES the INJECTED orchestrator / agent run_id namespace (it
does NOT mint its own), so recorder events + alerts correlate cross-stream with the
rest of the agent for S6. The events / data_quality_alerts / status streams are
constructed with the same injected `run_id`.
"""
from typing import Optional

from agent.journal import JournalCorruption, JournalWriter
from agent.journal import replay as journal_replay
from recorder.event_row import to_row

__all__ = [
    "STREAM_EVENTS",
    "STREAM_DATA_QUALITY",
    "STREAM_STATUS",
    "EventWriter",
    "replay_stream",
    "JournalCorruption",
]

# Recorder stream event-type tags (one file per stream; M1 = single file, NO rotation).
STREAM_EVENTS = "events"                      # parsed market events (+ derived book_hash on depth rows)
STREAM_DATA_QUALITY = "data_quality_alerts"   # gap / reconnect / heartbeat / crossed-book alerts
STREAM_STATUS = "status"                      # heartbeat / connection-state transitions


class EventWriter:
    """Thin recorder facade over `JournalWriter`. One `EventWriter` per resolved
    stream path. Inherits lock / seq / hash / tail-repair / corruption semantics
    verbatim from M0's journal (see module docstring).
    """

    def __init__(self, path, run_id: str, *, clock=None) -> None:
        # JournalWriter's default clock is its own _utc_now_iso; only override when
        # a deterministic clock is injected (offline tests).
        if clock is None:
            self._journal = JournalWriter(path, run_id)
        else:
            self._journal = JournalWriter(path, run_id, clock=clock)

    def write_event(self, event, *, derived_book_hash: Optional[str] = None,
                    decision_id: Optional[str] = None,
                    order_id: Optional[str] = None) -> dict:
        """Append one hash-stamped recorder row built by `recorder.event_row.to_row`.

        The persisted row is the FLAT field set of §B2: schema, dataset,
        instrument_id, symbol, `vendor_seq`, ts_event_utc, ts_recv_utc,
        reconnect_epoch, plus the per-type payload, plus (for a DepthEvent)
        `derived_book_hash` = book_hash(EquityBookState.apply(event).snapshot())
        computed by the recorder and passed through here.

        `reconnect_epoch` rides INSIDE the flat row (from provenance) — it is NOT a
        journal kwarg. Decimal fields are passed AS Decimal; the serializer renders
        them as strings. No flat field collides with `_RESERVED` (uses `vendor_seq`,
        not `seq`), so this never raises at journal.py:114. Returns the persisted row
        (including the journal `seq` + `hash`).
        """
        fields = to_row(event, derived_book_hash=derived_book_hash)
        return self._journal.append(
            STREAM_EVENTS, fields,
            decision_id=decision_id, order_id=order_id,
        )

    def record(self, event_type: str, fields: dict, *,
               decision_id: Optional[str] = None,
               order_id: Optional[str] = None) -> dict:
        """Lower-level escape hatch for alert / status rows; same contract as
        `JournalWriter.append`. Used to write `data_quality_alerts` / `status` rows
        that share the injected `run_id` namespace.
        """
        return self._journal.append(
            event_type, fields,
            decision_id=decision_id, order_id=order_id,
        )


def replay_stream(path) -> list:
    """Re-read a recorder stream hash-verified.

    Delegates to `agent.journal.replay` so the truncated-tail rule and
    `JournalCorruption` semantics are the SAME code path as M0. Single-file in M1.
    """
    return journal_replay(path)
