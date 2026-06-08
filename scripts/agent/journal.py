"""Append-only event-sourced journal (spec §7; invariants S3, S6).

Each row carries `event_type`, `run_id`, a per-stream monotonic `seq`, `ts_utc`,
optional `decision_id`/`order_id`, the caller's flat fields, and a row `hash` over
everything else.

- **Per-stream (not per-writer) seq + lock:** a path-keyed registry means multiple
  `JournalWriter` instances on the same stream share one monotonic seq (S6).
- **Tail integrity:** `replay` verifies every hash and drops ONLY a genuinely
  truncated tail — the last line when the file does not end in a newline (a crash
  mid-write). A newline-terminated bad line is a complete, corrupt record and is
  fatal (`JournalCorruption`).
"""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from agent.serializer import dumps, row_hash

_RESERVED = {"event_type", "run_id", "seq", "hash", "decision_id", "order_id", "ts_utc"}


class JournalCorruption(Exception):
    """A complete (newline-terminated) stream line failed to parse or hash-verify."""


def replay(path) -> list:
    """Return the rows of a stream, hash-verified; drop only a truncated tail."""
    p = Path(path)
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    if text == "":
        return []
    ended_with_newline = text.endswith("\n")
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts = parts[:-1]  # a clean stream ends with a newline
    rows = []
    last_idx = len(parts) - 1
    for i, line in enumerate(parts):
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or "hash" not in row:
                raise ValueError("row is not an object or is missing its hash")
            stored = row.pop("hash")
            if row_hash(row) != stored:
                raise ValueError("hash mismatch")
            row["hash"] = stored
        except (json.JSONDecodeError, ValueError) as exc:
            # Drop ONLY a genuinely truncated tail: the last line of a file that did
            # not end in a newline (a crash mid-write). A newline-terminated bad line
            # is a complete, corrupt record -> fatal.
            if i == last_idx and not ended_with_newline:
                break
            raise JournalCorruption(f"stream line {i} corrupt: {exc}") from exc
        rows.append(row)
    return rows


class _StreamState:
    __slots__ = ("lock", "seq")

    def __init__(self, seq: int):
        self.lock = threading.Lock()
        self.seq = seq


_streams = {}  # resolved path str -> _StreamState (shared across writer instances)
_streams_guard = threading.Lock()


def _state_for(path, initial_seq: int) -> _StreamState:
    key = str(Path(path).resolve())
    with _streams_guard:
        state = _streams.get(key)
        if state is None:
            state = _StreamState(initial_seq)
            _streams[key] = state
        return state


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JournalWriter:
    """Append-only writer for one stream file; seq + lock are shared per resolved path."""

    def __init__(self, path, run_id: str, clock=_utc_now_iso):
        self._path = Path(path)
        self._run_id = run_id
        self._clock = clock
        self._repair_truncated_tail()
        existing = replay(self._path)
        self._state = _state_for(self._path, existing[-1]["seq"] if existing else 0)

    def _repair_truncated_tail(self) -> None:
        """Drop a dangling partial line left by a crash, so appends land on a record
        boundary instead of concatenating onto garbage (which would later corrupt the
        whole stream)."""
        if not self._path.exists():
            return
        data = self._path.read_bytes()
        if data and not data.endswith(b"\n"):
            nl = data.rfind(b"\n")
            self._path.write_bytes(data[: nl + 1] if nl != -1 else b"")

    def append(self, event_type: str, fields: dict = None, *, decision_id=None, order_id=None) -> dict:
        fields = dict(fields or {})
        collisions = _RESERVED & set(fields)
        if collisions:
            raise ValueError(f"fields collide with reserved keys: {sorted(collisions)}")
        with self._state.lock:
            self._state.seq += 1
            row = dict(fields)
            row["event_type"] = event_type
            row["run_id"] = self._run_id
            row["seq"] = self._state.seq
            row["ts_utc"] = self._clock()
            if decision_id is not None:
                row["decision_id"] = decision_id
            if order_id is not None:
                row["order_id"] = order_id
            row["hash"] = row_hash(row)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(dumps(row) + "\n")
            return row
