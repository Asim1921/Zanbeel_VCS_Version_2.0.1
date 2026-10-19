"""One maintenance job at a time: backups, restore drills and garbage collection.

A backup copies the blobs its database snapshot references; garbage collection deletes
blobs nothing references. Run together, collection can delete a blob between the
snapshot and the copy, and the backup is missing content its own database points at.
The 2.0.1 backup tried to notice this afterwards with a second copy pass. Here the two
simply never overlap: each takes this lock, and a second job is refused (409) rather
than queued behind a long one.

The lock is an operating-system lock on a file beside the backups, so it also holds
between the server and ``tools/backup.py`` running at the same time. It is released by
the OS if the process dies, so a crash cannot leave it stuck.
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

_local = threading.Lock()
_holder: Optional[dict] = None


class MaintenanceBusy(Exception):
    """Another maintenance job holds the lock."""

    def __init__(self, holder: Optional[dict]):
        self.holder = holder or {}
        job = self.holder.get("job") or "another maintenance job"
        since = self.holder.get("started_at")
        super().__init__(f"{job[:1].upper() + job[1:]} is already running"
                         + (f" (since {since})" if since else "") + ". Try again when it finishes.")


def _lock_dir() -> Path:
    from app.services.backup import backup_root

    root = backup_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _try_os_lock(handle) -> bool:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _os_unlock(handle) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _read_holder(directory: Path) -> Optional[dict]:
    try:
        return json.loads((directory / ".maintenance.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Ticket:
    """A held lock. ``release`` may be called from another thread (the job's worker)."""

    def __init__(self, job: str, handle, directory: Path):
        self.job = job
        self._handle = handle
        self._directory = directory
        self._released = False

    def release(self) -> None:
        global _holder
        if self._released:
            return
        self._released = True
        try:
            (self._directory / ".maintenance.json").unlink()
        except OSError:
            pass
        _os_unlock(self._handle)
        try:
            self._handle.close()
        except OSError:
            pass
        _holder = None
        _local.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def acquire(job: str) -> Ticket:
    """Take the lock or raise MaintenanceBusy. Never waits."""
    global _holder
    if not _local.acquire(blocking=False):
        raise MaintenanceBusy(_holder)
    try:
        directory = _lock_dir()
        handle = open(directory / ".maintenance.lock", "a+b")
        if not _try_os_lock(handle):
            handle.close()
            raise MaintenanceBusy(_read_holder(directory))
    except BaseException:
        _local.release()
        raise
    _holder = {"job": job, "pid": os.getpid(),
               "started_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z"}
    try:
        (directory / ".maintenance.json").write_text(json.dumps(_holder), encoding="utf-8")
    except OSError:
        pass
    return Ticket(job, handle, directory)


def current() -> Optional[dict]:
    """What holds the lock right now, in this process or another; None when free."""
    if _holder:
        return dict(_holder)
    directory = _lock_dir()
    handle = open(directory / ".maintenance.lock", "a+b")
    try:
        if _try_os_lock(handle):
            _os_unlock(handle)
            return None
        return _read_holder(directory) or {"job": "maintenance job"}
    finally:
        handle.close()
