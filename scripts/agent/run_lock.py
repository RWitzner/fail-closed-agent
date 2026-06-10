"""M5 §M.1 — PID run lock: ONE writer per journal tree. [stdlib only]

A lock file at ``<journal_dir>/.lock`` created with ``os.open(O_CREAT|O_EXCL)``
(atomic on every platform we run on) holding the owner's PID:

- a LIVE lock ⇒ refuse to start (``RunLockHeld``);
- released on clean exit (context manager; release is idempotent);
- a STALE lock from a dead PID (liveness via ``os.kill(pid, 0)`` semantics) is
  reclaimed, and the reclaim is REPORTED to the caller — ``acquire()`` returns a
  ``reclaimed`` flag (also kept as ``self.reclaimed``) so the orchestrator can
  journal the §M.1 ``status`` note (this module is stdlib-only and must not
  journal itself).

Documented build resolutions (report-listed):

1. A MALFORMED lock file (unreadable / non-integer content) ⇒ ``RunLockHeld``:
   liveness cannot be verified, so the lock is treated as held (fail-closed;
   operator removes it manually).
2. ``acquire()`` creates the journal dir if missing — the orchestrator acquires
   the lock at startup step 1, BEFORE any ledger exists.
3. Reclaim races are closed by retrying the ``O_EXCL`` create exactly once after
   unlinking the stale file; losing that race ⇒ ``RunLockHeld``.
4. ``PermissionError`` from ``os.kill(pid, 0)`` means the process EXISTS (owned
   by another user) ⇒ alive ⇒ refuse; any other ``OSError`` is treated as alive
   (fail-closed).
"""
import os
from pathlib import Path
from typing import Optional

__all__ = ["LOCK_FILENAME", "RunLock", "RunLockHeld"]

LOCK_FILENAME = ".lock"


class RunLockHeld(Exception):
    """The journal tree is locked (live owner, or liveness unverifiable)."""


def _pid_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` liveness probe; unknown errors read ALIVE (fail-closed)."""
    if pid <= 0:
        return False   # no real process has pid <= 0; verifiably dead garbage
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True    # exists, owned by another user
    except OSError:
        return True    # cannot verify -> treat as alive (fail-closed)
    return True


class RunLock:
    """PID lock at ``<journal_dir>/.lock`` (§M.1). Context-manager friendly."""

    def __init__(self, journal_dir) -> None:
        self._dir = Path(journal_dir)
        self._path = self._dir / LOCK_FILENAME
        self._held = False
        self.reclaimed = False   # True iff THIS acquire reclaimed a stale lock

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self) -> bool:
        """Take the lock or raise ``RunLockHeld``. Returns the reclaimed flag
        (True iff a stale dead-PID lock was reclaimed — the caller journals it)."""
        if self._held:
            raise RunLockHeld(f"run lock already held by this instance: {self._path}")
        self._dir.mkdir(parents=True, exist_ok=True)
        reclaimed = False
        try:
            self._create()
        except FileExistsError:
            pid = self._read_pid()
            if pid is None:
                raise RunLockHeld(
                    f"lock file {self._path} is malformed; cannot verify owner "
                    "liveness — refusing to start (fail-closed; remove the file "
                    "manually if the owner is known dead)")
            if _pid_alive(pid):
                raise RunLockHeld(
                    f"journal tree is locked by live pid {pid}: {self._path}")
            # Stale lock from a dead PID: reclaim, report (§M.1).
            try:
                os.unlink(self._path)
            except FileNotFoundError:
                pass
            try:
                self._create()
            except FileExistsError:
                raise RunLockHeld(
                    f"lost the reclaim race for {self._path} (another writer "
                    "recreated it)")
            reclaimed = True
        self._held = True
        self.reclaimed = reclaimed
        return reclaimed

    def release(self) -> None:
        """Release on clean exit. Idempotent; a no-op when not held."""
        if not self._held:
            return
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
        self._held = False

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    # -- internals --

    def _create(self) -> None:
        fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, (str(os.getpid()) + "\n").encode("ascii"))
        finally:
            os.close(fd)

    def _read_pid(self) -> Optional[int]:
        try:
            text = self._path.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError):
            return None
        text = text.strip()
        if not text.isdigit():
            return None   # malformed (incl. negative/empty) -> unverifiable
        return int(text)
