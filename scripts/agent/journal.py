"""Append-only event-sourced journal (spec §7; invariants S3, S6).

Each row carries `event_type`, `run_id`, a per-stream monotonic `seq`, optional
`decision_id`/`order_id` correlation IDs, the caller's flat fields, and a row
`hash` over everything else. A single in-process writer lock serializes appends.
`replay` re-reads a stream, verifies every hash, and drops one truncated trailing
line (a crash mid-write); a corrupt non-trailing line is fatal (`JournalCorruption`).
"""
import json
import threading
from pathlib import Path

from agent.serializer import dumps, row_hash

_RESERVED = {"event_type", "run_id", "seq", "hash", "decision_id", "order_id"}


class JournalCorruption(Exception):
    """A non-trailing stream line failed to parse or its hash did not verify."""


def replay(path) -> list:
    """Return the rows of a stream, hash-verified; drop a single truncated tail."""
    p = Path(path)
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    if text == "":
        return []
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
            if i == last_idx:
                break  # truncated/invalid trailing line -> partial write, drop it
            raise JournalCorruption(f"stream line {i} corrupt: {exc}") from exc
        rows.append(row)
    return rows


class JournalWriter:
    """Single-writer, append-only writer for one stream file."""

    def __init__(self, path, run_id: str):
        self._path = Path(path)
        self._run_id = run_id
        self._lock = threading.Lock()
        self._repair_truncated_tail()
        existing = replay(self._path)
        self._seq = existing[-1]["seq"] if existing else 0

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
        with self._lock:
            self._seq += 1
            row = dict(fields)
            row["event_type"] = event_type
            row["run_id"] = self._run_id
            row["seq"] = self._seq
            if decision_id is not None:
                row["decision_id"] = decision_id
            if order_id is not None:
                row["order_id"] = order_id
            row["hash"] = row_hash(row)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(dumps(row) + "\n")
            return row
