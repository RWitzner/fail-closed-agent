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
import copy
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from agent.serializer import dumps, row_hash

_RESERVED = {"event_type", "run_id", "seq", "hash", "decision_id", "order_id", "ts_utc"}


class JournalCorruption(Exception):
    """A complete (newline-terminated) stream line failed to parse or hash-verify."""


def _file_state(stat_result) -> tuple:
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def _path_file_state(path: Path):
    try:
        return _file_state(os.stat(path))
    except FileNotFoundError:
        return None


def _replay_text(text: str) -> list:
    """Hash-verify rows already captured from one journal snapshot."""
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


def replay(path) -> list:
    """Return the rows of a stream, hash-verified; drop only a truncated tail."""
    p = Path(path)
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    return _replay_text(text)


class IncrementalJournalReader:
    """Replay-equivalent incremental reader for ONE single-writer stream.

    ``read()`` returns exactly what ``replay(path)`` would return. It parses only
    the appended suffix when the process-local ``JournalWriter`` state proves a
    serialized append from the reader's last verified snapshot; every other
    change gets a full descriptor-backed replay. This avoids O(day²) I/O and
    JSON/hash work in the M5 tick loop without trusting inode/mtime heuristics.
    Semantics mirrored from ``replay``:

    - only complete (newline-terminated) lines are consumed; a partial tail
      stays PENDING (not dropped) and is verified on a later call once the
      writer completes it;
    - a complete corrupt line raises ``JournalCorruption`` — and keeps raising
      on every subsequent call (the offset is committed only on full success);
    - a file that shrinks or disappears (external interference) triggers a
      fail-closed full re-read from scratch;
    - replacement, rewrite, or unauthorized mutation triggers the same full
      integrity check, and callers receive deep snapshots that cannot mutate
      the reader cache;
    - the opened descriptor and its path are checked again before candidate
      rows/offsets are committed; an unstable read retries once, then fails
      closed.

    The shared stream lock serializes reads with same-process writers; the tick
    loop still owns a single reader instance."""

    def __init__(self, path) -> None:
        self._path = Path(path)
        self._offset = 0          # bytes consumed, always at a newline boundary
        self._rows: list = []
        self._line_index = 0      # global line number for corruption messages
        self._version = None
        self._observed_size = None
        self._writer_lineage = None
        self._writer_generation = None
        self._state = _state_for(self._path)

    def _reset(self) -> None:
        self._offset = 0
        self._rows = []
        self._line_index = 0
        self._version = None
        self._observed_size = None
        self._writer_lineage = None
        self._writer_generation = None

    def _snapshot(self) -> list:
        return copy.deepcopy(self._rows)

    def _parse_candidate(self, chunk: bytes, start: int, rows: list,
                         index: int) -> tuple:
        newline_at = chunk.rfind(b"\n")
        if newline_at == -1:
            return rows, index, start  # only a partial tail so far: pending
        complete = chunk[: newline_at + 1]
        new_rows = []
        for line in complete.decode("utf-8").split("\n")[:-1]:
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or "hash" not in row:
                    raise ValueError("row is not an object or is missing its hash")
                stored = row.pop("hash")
                if row_hash(row) != stored:
                    raise ValueError("hash mismatch")
                row["hash"] = stored
            except (json.JSONDecodeError, ValueError) as exc:
                raise JournalCorruption(f"stream line {index} corrupt: {exc}") from exc
            new_rows.append(row)
            index += 1
        return rows + new_rows, index, start + newline_at + 1

    def _authorized_append(self, current: tuple) -> bool:
        return (
            self._version is not None
            and self._observed_size is not None
            and current[2] > self._observed_size
            and self._writer_lineage is not None
            and self._writer_generation is not None
            and self._state.initialized
            and self._state.lineage == self._writer_lineage
            and self._state.generation > self._writer_generation
            and self._state.file_state == current
        )

    def _commit_authorization(self, current: tuple) -> None:
        if self._state.initialized and self._state.file_state == current:
            self._writer_lineage = self._state.lineage
            self._writer_generation = self._state.generation
        else:
            self._writer_lineage = None
            self._writer_generation = None

    def _read_locked(self) -> list:
        for attempt in range(2):
            try:
                fh = open(self._path, "rb")
            except FileNotFoundError:
                if _path_file_state(self._path) is None:
                    self._reset()
                    return []
                if attempt == 0:
                    continue
                raise JournalCorruption("stream changed during incremental read")

            parse_error = None
            candidate = None
            read_complete = True
            with fh:
                before = _file_state(os.fstat(fh.fileno()))
                unchanged = (
                    self._version == before
                    and self._observed_size == before[2]
                )
                if unchanged:
                    candidate = (self._rows, self._line_index, self._offset)
                else:
                    incremental = self._authorized_append(before)
                    if incremental:
                        start = self._offset
                        rows = self._rows
                        index = self._line_index
                    else:
                        start = 0
                        rows = []
                        index = 0
                    fh.seek(start)
                    chunk = fh.read(before[2] - start)
                    read_complete = len(chunk) == before[2] - start
                    try:
                        candidate = self._parse_candidate(
                            chunk, start, rows, index)
                    except (JournalCorruption, UnicodeDecodeError) as exc:
                        parse_error = exc

                after = _file_state(os.fstat(fh.fileno()))
                path_after = _path_file_state(self._path)

            stable = (
                read_complete
                and before == after
                and path_after == before
            )
            if not stable:
                if attempt == 0:
                    continue
                raise JournalCorruption("stream changed during incremental read")
            if parse_error is not None:
                raise parse_error

            rows, line_index, offset = candidate
            self._rows = rows
            self._line_index = line_index
            self._offset = offset
            self._version = before
            self._observed_size = before[2]
            self._commit_authorization(before)
            return self._snapshot()

        raise JournalCorruption("stream changed during incremental read")

    def read(self) -> list:
        with self._state.lock:
            return self._read_locked()


class _StreamState:
    __slots__ = (
        "lock", "seq", "initialized", "file_state", "lineage", "generation",
    )

    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 0
        self.initialized = False
        self.file_state = None
        self.lineage = 0
        self.generation = 0


_streams = {}  # resolved path str -> _StreamState (shared across writer instances)
_streams_guard = threading.Lock()


def _state_for(path) -> _StreamState:
    key = str(Path(path).resolve())
    with _streams_guard:
        state = _streams.get(key)
        if state is None:
            state = _StreamState()
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
        self._state = _state_for(self._path)
        with self._state.lock:
            existing, current = self._stable_initial_snapshot()
            seq = existing[-1]["seq"] if existing else 0
            if not self._state.initialized:
                self._state.initialized = True
                self._state.lineage = 1
                self._state.generation = 0
            elif self._state.file_state != current:
                # A newly constructed writer is an explicit, full-verified
                # rebase. Invalidate every reader's prior append lineage.
                self._state.lineage += 1
                self._state.generation = 0
            self._state.seq = seq
            self._state.file_state = current

    def _stable_initial_snapshot(self) -> tuple[list, tuple | None]:
        """Repair and replay one descriptor-bound, path-stable file version."""
        for attempt in range(2):
            try:
                fd = os.open(self._path, os.O_RDWR)
            except FileNotFoundError:
                if _path_file_state(self._path) is None:
                    return [], None
                if attempt == 0:
                    continue
                break

            unstable = False
            parse_error = None
            rows = None
            snapshot = None
            with os.fdopen(fd, "r+b") as fh:
                before = _file_state(os.fstat(fh.fileno()))
                if _path_file_state(self._path) != before:
                    unstable = True
                else:
                    data = fh.read(before[2])
                    after_read = _file_state(os.fstat(fh.fileno()))
                    stable_read = (
                        len(data) == before[2]
                        and after_read == before
                        and _path_file_state(self._path) == before
                    )
                    if not stable_read:
                        unstable = True
                    else:
                        snapshot = before
                        if data and not data.endswith(b"\n"):
                            newline_at = data.rfind(b"\n")
                            repaired_size = newline_at + 1
                            os.ftruncate(fh.fileno(), repaired_size)
                            snapshot = _file_state(os.fstat(fh.fileno()))
                            stable_repair = (
                                snapshot[:2] == before[:2]
                                and snapshot[2] == repaired_size
                                and _path_file_state(self._path) == snapshot
                            )
                            if not stable_repair:
                                unstable = True
                            else:
                                fh.seek(0)
                                data = fh.read(repaired_size)
                                after_repair_read = _file_state(
                                    os.fstat(fh.fileno()))
                                stable_repair_read = (
                                    len(data) == repaired_size
                                    and after_repair_read == snapshot
                                    and _path_file_state(self._path) == snapshot
                                )
                                if not stable_repair_read:
                                    unstable = True

                    if not unstable:
                        try:
                            rows = _replay_text(data.decode("utf-8"))
                        except (JournalCorruption, UnicodeDecodeError) as exc:
                            parse_error = exc
                        after_parse = _file_state(os.fstat(fh.fileno()))
                        if (
                                after_parse != snapshot
                                or _path_file_state(self._path) != snapshot):
                            unstable = True

            if unstable:
                if attempt == 0:
                    continue
                break
            if parse_error is not None:
                raise parse_error
            return rows, snapshot

        raise JournalCorruption(
            "stream changed during JournalWriter initialization")

    def append(self, event_type: str, fields: dict = None, *, decision_id=None, order_id=None) -> dict:
        fields = dict(fields or {})
        collisions = _RESERVED & set(fields)
        if collisions:
            raise ValueError(f"fields collide with reserved keys: {sorted(collisions)}")
        with self._state.lock:
            next_seq = self._state.seq + 1
            row = dict(fields)
            row["event_type"] = event_type
            row["run_id"] = self._run_id
            row["seq"] = next_seq
            row["ts_utc"] = self._clock()
            if decision_id is not None:
                row["decision_id"] = decision_id
            if order_id is not None:
                row["order_id"] = order_id
            row["hash"] = row_hash(row)
            payload = (dumps(row) + "\n").encode("utf-8")
            expected = self._state.file_state
            flags = os.O_WRONLY | os.O_APPEND
            if expected is None:
                flags |= os.O_CREAT | os.O_EXCL
            try:
                fd = os.open(self._path, flags, 0o666)
            except (FileExistsError, FileNotFoundError) as exc:
                raise JournalCorruption(
                    "stream changed outside the active JournalWriter") from exc
            with os.fdopen(fd, "ab") as fh:
                before = _file_state(os.fstat(fh.fileno()))
                path_before = _path_file_state(self._path)
                expected_before = (
                    before[2] == 0 if expected is None else before == expected
                )
                if not expected_before or path_before != before:
                    raise JournalCorruption(
                        "stream changed outside the active JournalWriter")
                written = fh.write(payload)
                fh.flush()
                after = _file_state(os.fstat(fh.fileno()))
                path_after = _path_file_state(self._path)
                stable_append = (
                    written == len(payload)
                    and after[:2] == before[:2]
                    and after[2] == before[2] + len(payload)
                    and path_after == after
                )
                if not stable_append:
                    raise JournalCorruption(
                        "stream changed during JournalWriter append")
            self._state.seq = next_seq
            self._state.file_state = after
            self._state.generation += 1
            return row
