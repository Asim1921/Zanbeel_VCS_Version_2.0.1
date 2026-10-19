"""Verified backup, restore, and restore drills for the database and its blob store.

The gap: the only backup was one stale 520 KB ``foxnest.db.bak`` beside the live
database, with no restore path anyone had run -- for the system holding the team's
source code. 2.0.1 added backups, but "verified" meant only that each blob *file
existed*; nothing was ever read back, the database copy was never checked, every
backup was a full copy of the blob store (three of them, 11 GB, on the live disk), and
no restore had ever been tried. This replaces it.

**The database and the blob store are one unit.** Rows hold only hashes, so a
database copy without its blobs restores an index pointing at nothing. Every backup
holds both, plus the generated documents, and records them together.

**Verified means read back.** A backup is verified when the database snapshot passes
SQLite's ``integrity_check`` and matches its recorded SHA-256, and every blob the
snapshot references decodes to bytes that hash to its own name. Content addressing
makes that last check complete: a blob that reads back wrong cannot hash to its name.

**Blobs are hard-linked from the previous verified backup.** Content-addressed blobs
never change, so a blob already in the last backup is linked rather than copied again:
each backup costs its database plus whatever is new. Every blob is still re-read and
re-hashed, so a linked copy that rotted is caught (and replaced from the live store).

**A restore drill proves it.** ``test_restore`` restores a backup into a scratch
directory with the same code a real restore uses, upgrades that copy to this server's
schema, checks it against the models, reads every blob back and resolves every branch
head's files. That is "a backup has been taken and restored as a test".

**A real restore keeps what it replaces.** The live database, its ``-wal``/``-shm``/
``-journal`` files, the documents and the search index are moved aside, not deleted.
Copying a database over a live one while a stale write-ahead log survives beside it is
one of the ways to corrupt SQLite; moving the log aside with its database avoids it.

**One job at a time.** Backups, drills and garbage collection share a lock
(``maintenance``), so collection cannot delete a blob between a snapshot and its copy.

SQLite only. PostgreSQL deployments use ``pg_dump`` alongside a copy of the blob store
until the migration planned for the next cycle.
"""

import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.config import AUTH_SECRET, BLOB_DIR, DOCS_DIR, SERVER_VERSION
from database.database import ACTIVE_DATABASE_URL, SessionLocal
from database.models import BackupRecord

FORMAT = 2
MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "database.sqlite3"
BLOBS_DIRNAME = "blobs"
DOCS_DIRNAME = "docs"
SCRATCH_DIRNAME = ".restore-tests"

#: Sessions for background jobs. Tests point this at their own database.
session_factory: Callable[[], Session] = SessionLocal

#: Rows counted into the manifest and compared after a restore.
COUNTED_TABLES = ("users", "repositories", "commits", "branches", "commit_files",
                  "file_objects", "pull_requests", "issues", "tags", "releases")


class BackupError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# --- settings -------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def _env_flag(name: str, default: str) -> bool:
    return (os.getenv(name, default) or "").strip().lower() not in {"0", "false", "no", "off"}


def keep_count() -> int:
    """Verified backups kept by rotation (FOXNEST_BACKUP_KEEP, default 7)."""
    return max(1, _env_int("FOXNEST_BACKUP_KEEP", 7))


def interval_hours() -> int:
    """Hours between scheduled backups (FOXNEST_BACKUP_INTERVAL_HOURS); 0 turns it off."""
    return max(0, _env_int("FOXNEST_BACKUP_INTERVAL_HOURS", 0))


def stale_hours() -> int:
    """A newest verified backup older than this is flagged (FOXNEST_BACKUP_STALE_HOURS)."""
    return max(1, _env_int("FOXNEST_BACKUP_STALE_HOURS", 48))


def test_restore_after_scheduled() -> bool:
    return _env_flag("FOXNEST_BACKUP_TEST_RESTORE", "1")


def backup_root() -> Path:
    """Where backups are written.

    Defaults to a sibling of the blob store rather than inside it, so a backup never
    becomes an input to the next backup. Point FOXNEST_BACKUP_DIR at another disk: a
    backup on the same disk as the data does not survive losing that disk.
    """
    configured = os.getenv("FOXNEST_BACKUP_DIR")
    if configured:
        return Path(configured)
    return Path(BLOB_DIR).resolve().parent / "backups"


def _store():
    from app.services import blob_store

    return blob_store.store


def _live_db_path(db: Optional[Session] = None) -> Path:
    from app.db.upgrade import sqlite_path

    url = (db.get_bind().url.render_as_string(hide_password=False)
           if db is not None else ACTIVE_DATABASE_URL)
    path = sqlite_path(url)
    if path is None:
        raise BackupError(
            "Automated backup supports SQLite only. For PostgreSQL use pg_dump alongside a "
            "copy of the blob store.",
            status_code=501,
        )
    return path


def signing_key_fingerprint() -> str:
    """Identifies the commit-signing key without revealing it.

    Commits are signed with FOXNEST_AUTH_SECRET. A backup restored onto a server with a
    different secret reports every signature invalid, so the manifest records which
    key signed the history and a restore says when they differ.
    """
    return hmac.new(AUTH_SECRET.encode("utf-8"), b"zanbeel-backup:signing-key-check",
                    hashlib.sha256).hexdigest()[:16]


def _existing_ancestor(path: Path) -> Path:
    path = Path(path).resolve()
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def same_volume(a: Path, b: Path) -> Optional[bool]:
    try:
        return os.stat(_existing_ancestor(a)).st_dev == os.stat(_existing_ancestor(b)).st_dev
    except OSError:
        return None


# --- reading a snapshot ---------------------------------------------------------------

def _connect_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)


def _reference_queries() -> List[str]:
    """Every column that names a blob -- the list garbage collection uses -- plus the
    object table itself, so an unreferenced but stored blob is kept too."""
    from app.services.gc import BLOB_REFERENCE_COLUMNS

    queries = [f"SELECT {c.name} FROM {c.table.name} WHERE {c.name} IS NOT NULL"
               for c in BLOB_REFERENCE_COLUMNS]
    queries.append("SELECT hash FROM file_objects WHERE hash IS NOT NULL")
    return queries


def _snapshot_references(db_path: Path) -> Tuple[Set[str], Set[str]]:
    """(every blob hash the snapshot refers to, those whose bytes are inline in it).

    Rows written before blobs moved to disk keep their content in file_objects.content
    and have no blob file. They are part of the snapshot already, not missing.
    """
    seen: Set[str] = set()
    inline: Set[str] = set()
    connection = _connect_ro(db_path)
    try:
        for query in _reference_queries():
            try:
                for (value,) in connection.execute(query):
                    if value:
                        seen.add(str(value).strip().lower())
            except sqlite3.Error:
                continue  # A table an older snapshot lacks references nothing.
        try:
            for (value,) in connection.execute(
                    "SELECT hash FROM file_objects WHERE content IS NOT NULL AND length(content) > 0"):
                inline.add(str(value).strip().lower())
        except sqlite3.Error:
            pass
    finally:
        connection.close()
    return seen, inline


def _inline_ok(connection: sqlite3.Connection, digest: str) -> bool:
    row = connection.execute("SELECT content FROM file_objects WHERE hash = ?", (digest,)).fetchone()
    return bool(row and row[0] is not None and _hashes_to(bytes(row[0]), digest))


def _integrity(db_path: Path) -> str:
    """SQLite's own check, or why the file could not even be read as a database."""
    try:
        connection = _connect_ro(db_path)
        try:
            rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return f"unreadable: {exc}"
    return "ok" if rows == ["ok"] else "; ".join(rows[:5])


def _row_counts(db_path: Path) -> Dict[str, int]:
    counts = {}
    connection = _connect_ro(db_path)
    try:
        for table in COUNTED_TABLES:
            try:
                counts[table] = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                pass
    finally:
        connection.close()
    return counts


def _schema_revision(db_path: Path) -> Optional[str]:
    connection = _connect_ro(db_path)
    try:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()


# --- bytes ----------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hashes_to(content: bytes, digest: str) -> bool:
    """Blob names are SHA-1 (what the store computes) or SHA-256 (what a client may send)."""
    if len(digest) == 64:
        return hashlib.sha256(content).hexdigest() == digest
    return hashlib.sha1(content).hexdigest() == digest


def _blob_ok(path: Path, digest: str) -> bool:
    from app.services.blob_store import _unframe

    try:
        return _hashes_to(_unframe(Path(path).read_bytes()), digest)
    except OSError:
        return False


def _blob_path(root: Path, digest: str) -> Path:
    return Path(root) / digest[:2] / digest


def _valid_digest(digest: str) -> bool:
    return len(digest) >= 4 and all(ch in "0123456789abcdef" for ch in digest)


def _atomic_copy(source: Path, dest: Path) -> None:
    """Copy to a temporary file in the destination directory, then rename into place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".tmp-")
    os.close(fd)
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _link_or_copy(source: Path, dest: Path, link: bool) -> str:
    """Hard-link when asked and possible, else copy. Returns 'linked' or 'copied'."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if link:
        try:
            if dest.exists():
                dest.unlink()
            os.link(source, dest)
            return "linked"
        except OSError:
            pass  # Another volume, or a filesystem without hard links.
    _atomic_copy(source, dest)
    return "copied"


def _tree_files(root: Path) -> List[Path]:
    return sorted(p for p in Path(root).rglob("*") if p.is_file()) if Path(root).is_dir() else []


# --- taking a backup ------------------------------------------------------------------

def _previous_verified(db: Session, exclude_id: Optional[int] = None) -> Optional[Path]:
    """Blobs directory of the newest verified backup still on disk, to link from."""
    query = (db.query(BackupRecord)
             .filter(BackupRecord.verified.is_(True), BackupRecord.status == "complete")
             .order_by(BackupRecord.created_at.desc(), BackupRecord.id.desc()))
    for record in query.limit(10):
        if record.id == exclude_id:
            continue
        blobs = Path(record.path) / BLOBS_DIRNAME
        if blobs.is_dir():
            return blobs
    return None


def _snapshot_database(source: Path, target: Path) -> None:
    """A consistent copy through SQLite, not a byte copy of a file that may be
    mid-transaction (which yields a database that opens and is subtly wrong)."""
    reader = sqlite3.connect(str(source), timeout=60)
    try:
        reader.execute("VACUUM INTO ?", (str(target),))
    finally:
        reader.close()


def create_backup(db: Session, label: Optional[str] = None, actor_id: Optional[int] = None,
                  kind: str = "manual", record: Optional[BackupRecord] = None,
                  progress: Optional[Callable[[str], None]] = None) -> BackupRecord:
    """Snapshot the database, gather every blob it references, write the manifest, verify.

    The caller holds the maintenance lock (``run_backup`` and ``start_backup`` do).
    Returns the record, complete whether or not it verified; raises BackupError only
    when no backup could be written at all.
    """
    say = progress or (lambda message: None)
    source_db = _live_db_path(db)
    started = time.monotonic()
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    safe_label = "".join(c for c in (label or kind) if c.isalnum() or c in "-_")[:60] or kind
    destination = backup_root() / f"{stamp}-{safe_label}"
    # Two backups in the same second (a scheduled one and a manual one, a double click)
    # get distinct directories rather than the second failing.
    suffix = 2
    while destination.exists():
        destination = backup_root() / f"{stamp}-{safe_label}-{suffix}"
        suffix += 1

    if record is None:
        record = BackupRecord(label=safe_label, path=str(destination), kind=kind,
                              status="running", format=FORMAT, created_by_id=actor_id)
        db.add(record)
        db.commit()
        db.refresh(record)
    else:
        record.path = str(destination)
        record.label = safe_label
        db.commit()

    blobs_dest = destination / BLOBS_DIRNAME
    try:
        blobs_dest.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        _fail(db, record, "A backup with that name already exists", started)
        raise BackupError("A backup with that name already exists", 409)
    except OSError as exc:
        _fail(db, record, f"Could not create the backup directory: {exc}", started)
        raise BackupError(f"Could not create the backup directory: {exc}", 500)

    db_dest = destination / DATABASE_NAME
    try:
        say("snapshotting the database")
        _snapshot_database(source_db, db_dest)
        integrity = _integrity(db_dest)
        db_sha = _sha256(db_dest)
    except Exception as exc:
        shutil.rmtree(destination, ignore_errors=True)
        _fail(db, record, f"Database snapshot failed: {exc}", started)
        raise BackupError(f"Database snapshot failed: {exc}", 500)

    store_root = Path(_store().root)
    previous = _previous_verified(db, exclude_id=record.id)
    hashes, inline = _snapshot_references(db_dest)
    say(f"gathering {len(hashes)} referenced blobs"
        + (f" (linking unchanged ones from {previous.parent.name})" if previous else ""))

    missing: List[str] = []
    corrupt: List[str] = []
    linked = copied = 0
    blob_bytes = 0
    snapshot = _connect_ro(db_dest)
    try:
        for digest in sorted(hashes):
            if not _valid_digest(digest):
                continue
            dest = _blob_path(blobs_dest, digest)
            live = _blob_path(store_root, digest)
            earlier = _blob_path(previous, digest) if previous else None

            how = None
            if earlier is not None and earlier.is_file():
                how = _link_or_copy(earlier, dest, link=True)
            elif live.is_file():
                how = _link_or_copy(live, dest, link=False)
            elif digest in inline:
                continue  # Held in the snapshot itself.
            else:
                missing.append(digest)
                continue

            # Read every one back. A linked blob that rotted in the earlier backup is
            # replaced from the live store; if that is bad too, the backup is not good.
            if not _blob_ok(dest, digest):
                replaced = False
                if live.is_file() and _blob_ok(live, digest):
                    dest.unlink()
                    _atomic_copy(live, dest)
                    replaced = _blob_ok(dest, digest)
                    how = "copied"
                if not replaced:
                    corrupt.append(digest)
                    continue
            if how == "linked":
                linked += 1
            else:
                copied += 1
            blob_bytes += dest.stat().st_size
        inline_bad = [d for d in sorted(inline) if not _blob_path(blobs_dest, d).is_file()
                      and not _inline_ok(snapshot, d)]
        corrupt.extend(inline_bad)
    finally:
        snapshot.close()

    say("copying generated documents")
    docs = _copy_docs(Path(DOCS_DIR), destination / DOCS_DIRNAME)

    problems = []
    if integrity != "ok":
        problems.append(f"database integrity check failed: {integrity}")
    if missing:
        problems.append(f"{len(missing)} referenced blob(s) are not in the store")
    if corrupt:
        problems.append(f"{len(corrupt)} blob(s) do not match their hash")
    verified = not problems

    manifest = {
        "format": FORMAT,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "label": safe_label,
        "kind": kind,
        "server_version": SERVER_VERSION,
        "schema_revision": _schema_revision(db_dest),
        "database": {
            "file": DATABASE_NAME,
            "bytes": db_dest.stat().st_size,
            "sha256": db_sha,
            "integrity": integrity,
            "row_counts": _row_counts(db_dest),
        },
        "blobs": {
            "dir": BLOBS_DIRNAME,
            "referenced": len(hashes),
            "stored": linked + copied,
            "inline": len(inline),
            "linked_from_previous": linked,
            "copied": copied,
            "bytes": blob_bytes,
            "missing": missing[:200],
            "missing_count": len(missing),
            "corrupt": corrupt[:200],
            "corrupt_count": len(corrupt),
        },
        "docs": docs,
        "signing_key_fingerprint": signing_key_fingerprint(),
        "source": {
            "database": str(source_db),
            "blob_dir": str(store_root),
            "docs_dir": str(DOCS_DIR),
            "same_volume_as_data": same_volume(destination, source_db),
        },
        "verified": verified,
        "problems": problems,
    }
    _write_json(destination / MANIFEST_NAME, manifest)

    record.status = "complete"
    record.format = FORMAT
    record.database_bytes = manifest["database"]["bytes"]
    record.database_sha256 = db_sha
    record.schema_revision = manifest["schema_revision"]
    record.blob_bytes = blob_bytes
    record.blob_count = linked + copied
    record.linked_blobs = linked
    record.missing_blobs = len(missing)
    record.verified = verified
    record.verified_at = datetime.utcnow()
    record.verify_status = "passed" if verified else "failed"
    record.error = "; ".join(problems) or None
    record.finished_at = datetime.utcnow()
    record.duration_ms = int((time.monotonic() - started) * 1000)
    db.commit()
    db.refresh(record)
    _audit(
        db, actor_id, "backup_created",
        description=(f"Took a {'verified' if verified else 'NOT verified'} {kind} backup: "
                     f"{record.blob_count} blobs ({linked} unchanged, linked), "
                     f"{_human(record.database_bytes + record.blob_bytes)}"),
        target_type="backup", target_id=record.id,
        payload={"path": record.path, "verified": verified, "problems": problems},
    )
    return record


def _fail(db: Session, record: BackupRecord, message: str, started: float) -> None:
    record.status = "failed"
    record.verified = False
    record.error = message
    record.finished_at = datetime.utcnow()
    record.duration_ms = int((time.monotonic() - started) * 1000)
    db.commit()
    _audit(db, record.created_by_id, "backup_failed", description=message,
                 target_type="backup", target_id=record.id)


def _copy_docs(source: Path, dest: Path) -> Dict[str, Any]:
    files = {}
    total = 0
    for path in _tree_files(source):
        relative = path.relative_to(source).as_posix()
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        files[relative] = _sha256(target)
        total += target.stat().st_size
    return {"dir": DOCS_DIRNAME, "count": len(files), "bytes": total, "files": files}


def _audit(db: Session, actor_id: Optional[int], action: str, **kwargs) -> None:
    """Record in the activity log when a person started the job.

    The log records what people did; a scheduled backup has no one to attribute it to,
    and its record here is the account of it.
    """
    if not actor_id:
        return
    from app.services import audit

    audit.record(db, actor_id, action, **kwargs)


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _human(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# --- verifying a backup ---------------------------------------------------------------

def read_manifest(path: str) -> Dict[str, Any]:
    root = Path(path)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise BackupError("That backup has no manifest", 404)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"Manifest is unreadable: {exc}", 500)
    if "format" not in manifest:  # 2.0.1 backups
        manifest = {
            "format": 1,
            "created_at": manifest.get("created_at"),
            "database": {"file": manifest.get("database_file", DATABASE_NAME)},
            "blobs": {"dir": manifest.get("blob_dir", BLOBS_DIRNAME)},
            "docs": None,
        }
    return manifest


def verify_backup(path: str, full: bool = True) -> Dict[str, Any]:
    """Re-check a backup on disk against its own manifest. Independent of the database
    row, so a backup copied to another machine can be checked there -- the only kind of
    backup that survives losing this one.

    ``full`` re-reads and re-hashes every blob; without it, presence only (fast).
    """
    started = time.monotonic()
    root = Path(path)
    manifest = read_manifest(path)
    problems: List[str] = []

    db_path = root / manifest["database"].get("file", DATABASE_NAME)
    if not db_path.is_file():
        raise BackupError("The database snapshot is missing from this backup", 404)

    recorded_sha = manifest["database"].get("sha256")
    db_sha = _sha256(db_path)
    if recorded_sha and recorded_sha != db_sha:
        problems.append("the database snapshot has changed since the backup was taken")
    integrity = _integrity(db_path)
    if integrity != "ok":
        problems.append(f"database integrity check failed: {integrity}")

    blobs_root = root / manifest["blobs"].get("dir", BLOBS_DIRNAME)
    hashes, inline = _snapshot_references(db_path)
    missing, corrupt = [], []
    present = 0
    snapshot = _connect_ro(db_path)
    try:
        for digest in sorted(hashes):
            if not _valid_digest(digest):
                continue
            blob = _blob_path(blobs_root, digest)
            if blob.is_file():
                present += 1
                if full and not _blob_ok(blob, digest):
                    corrupt.append(digest)
            elif digest in inline:
                if full and not _inline_ok(snapshot, digest):
                    corrupt.append(digest)
            else:
                missing.append(digest)
    finally:
        snapshot.close()
    if missing:
        problems.append(f"{len(missing)} referenced blob(s) are missing")
    if corrupt:
        problems.append(f"{len(corrupt)} blob(s) do not match their hash")

    docs = manifest.get("docs") or {}
    docs_root = root / (docs.get("dir") or DOCS_DIRNAME)
    docs_bad = [name for name, digest in (docs.get("files") or {}).items()
                if not (docs_root / name).is_file() or (full and _sha256(docs_root / name) != digest)]
    if docs_bad:
        problems.append(f"{len(docs_bad)} generated document(s) are missing or changed")

    return {
        "path": str(root),
        "format": manifest.get("format", 1),
        "full": full,
        "database_bytes": db_path.stat().st_size,
        "database_sha256": db_sha,
        "integrity": integrity,
        "schema_revision": _schema_revision(db_path),
        "referenced_hashes": len(hashes),
        "present": present,
        "inline": len(inline),
        "missing": len(missing),
        "missing_sample": missing[:20],
        "corrupt": len(corrupt),
        "corrupt_sample": corrupt[:20],
        "docs_checked": len(docs.get("files") or {}),
        "docs_problems": docs_bad[:20],
        "signing_key_matches": (manifest.get("signing_key_fingerprint") in (None, signing_key_fingerprint())),
        "problems": problems,
        "verified": not problems,
        "manifest_created_at": manifest.get("created_at"),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


# --- restoring ------------------------------------------------------------------------

_SIDECARS = ("-wal", "-shm", "-journal")


def _move_aside(path: Path, stamp: str) -> Optional[str]:
    if not path.exists():
        return None
    displaced = path.with_name(f"{path.name}.replaced-{stamp}")
    # A rename in the same directory, never shutil.move: when a rename fails, move falls
    # back to copying and then deleting, and a delete that fails leaves a stray copy.
    # Windows can hold a file for a moment after the process that had it open exits
    # (and a virus scanner can hold it longer), so a stopped server's database may not
    # move on the first try.
    for attempt in range(10):
        try:
            os.replace(path, displaced)
            return str(displaced)
        except PermissionError:
            if attempt == 9:
                raise BackupError(
                    f"{path} is in use. Stop the server (and anything else using it) and try again.", 409)
            time.sleep(0.5)
    return str(displaced)


def restore_backup(path: str, target_db: Optional[str] = None,
                   target_blobs: Optional[str] = None, target_docs: Optional[str] = None,
                   force: bool = False, link: bool = False,
                   move_search_index: Optional[bool] = None) -> Dict[str, Any]:
    """Restore a backup over a database, blob store and docs folder (the live ones by default).

    Refuses unless the backup verifies (full read-back), unless ``force`` is set:
    restoring a snapshot known to be missing blobs produces a repository whose history
    cannot be checked out, and it is better to fail loudly than to hand back something
    that looks restored. What is replaced is moved aside, never deleted, so choosing
    the wrong backup is itself recoverable. Every byte written is read back and checked.

    ``link`` hard-links blobs instead of copying them (the restore drill uses it; a
    real restore copies, so the live store does not share files with the backup).
    """
    root = Path(path)
    started = time.monotonic()
    verification = verify_backup(str(root), full=True)
    if not verification["verified"] and not force:
        raise BackupError(
            "Refusing to restore: " + "; ".join(verification["problems"])
            + ". Pass force to restore anyway.",
            409,
        )
    manifest = read_manifest(str(root))
    db_source = root / manifest["database"].get("file", DATABASE_NAME)
    blobs_source = root / manifest["blobs"].get("dir", BLOBS_DIRNAME)
    docs_meta = manifest.get("docs") or {}
    docs_source = root / (docs_meta.get("dir") or DOCS_DIRNAME)

    live = target_db is None
    db_target = Path(target_db) if target_db else _live_db_path()
    if live:
        # This process's own pooled connection (opened when the database module was
        # imported) would otherwise hold the file it is about to replace.
        from database.database import engine as app_engine
        app_engine.dispose()
    blob_target = Path(target_blobs) if target_blobs else Path(_store().root)
    docs_target = Path(target_docs) if target_docs else Path(DOCS_DIR)
    if db_target.resolve() == db_source.resolve():
        raise BackupError("The restore target is the backup's own database", 400)

    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    moved = {}
    db_target.parent.mkdir(parents=True, exist_ok=True)
    moved["database"] = _move_aside(db_target, stamp)
    for suffix in _SIDECARS:
        sidecar = db_target.with_name(db_target.name + suffix)
        if sidecar.exists():
            moved[f"database{suffix}"] = _move_aside(sidecar, stamp)

    _atomic_copy(db_source, db_target)
    if _sha256(db_target) != verification["database_sha256"]:
        raise BackupError("The restored database does not match the backup's copy", 500)

    restored = skipped = replaced = 0
    bad: List[str] = []
    for blob in _tree_files(blobs_source):
        digest = blob.name
        if digest.startswith(".tmp-") or not _valid_digest(digest):
            continue
        dest = _blob_path(blob_target, digest)
        if dest.is_file():
            if _blob_ok(dest, digest):
                skipped += 1
                continue
            dest.unlink()  # A damaged live copy is exactly what a restore is for.
            replaced += 1
        _link_or_copy(blob, dest, link=link)
        if not _blob_ok(dest, digest):
            bad.append(digest)
        restored += 1
    if bad:
        raise BackupError(f"{len(bad)} blob(s) did not read back correctly after restoring", 500)

    docs_restored = 0
    if docs_source.is_dir():
        moved["docs"] = _move_aside(docs_target, stamp)
        for source in _tree_files(docs_source):
            target = docs_target / source.relative_to(docs_source)
            _atomic_copy(source, target)
            docs_restored += 1
    docs_target.mkdir(parents=True, exist_ok=True)

    # The code search index is derived from the restored data; the old one describes
    # the database that was just moved aside. Set aside, it is rebuilt on next start.
    if move_search_index if move_search_index is not None else live:
        from app.services import search_index

        index = Path(search_index.default_path())
        moved["search_index"] = _move_aside(index, stamp)
        for suffix in ("-wal", "-shm"):
            sidecar = index.with_name(index.name + suffix)
            if sidecar.exists():
                _move_aside(sidecar, stamp)

    return {
        "restored_database": str(db_target),
        "moved_aside": {k: v for k, v in moved.items() if v},
        "previous_database_moved_to": moved.get("database"),
        "restored_blobs": restored,
        "already_present_blobs": skipped,
        "replaced_damaged_blobs": replaced,
        "blob_target": str(blob_target),
        "restored_docs": docs_restored,
        "docs_target": str(docs_target),
        "schema_revision": verification["schema_revision"],
        "verified_before_restore": verification["verified"],
        "signing_key_matches": verification["signing_key_matches"],
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def test_restore(path: str, keep: bool = False,
                 progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Restore a backup into a scratch directory and prove the result is usable.

    Uses ``restore_backup`` -- the code a real restore runs -- then upgrades the
    restored database to this server's schema the way a restart would, checks it against
    the models, compares row counts with the manifest, and resolves the files of every
    branch head. Nothing live is touched.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import upgrade
    from app.db.schema_check import inspect_schema
    from app.services.blob_store import BlobStore
    from database.models import Branch, CommitFile, FileObject

    say = progress or (lambda message: None)
    started = time.monotonic()
    manifest = read_manifest(path)
    scratch = backup_root() / SCRATCH_DIRNAME / datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
    scratch.mkdir(parents=True, exist_ok=True)
    problems: List[str] = []
    report: Dict[str, Any] = {"path": str(path), "scratch": str(scratch)}
    engine = None
    try:
        say("restoring into a scratch directory")
        restored = restore_backup(path, target_db=str(scratch / "foxnest.db"),
                                  target_blobs=str(scratch / "blobs"),
                                  target_docs=str(scratch / "docs"),
                                  link=True, move_search_index=False)
        report["restore"] = restored
        if not restored["signing_key_matches"]:
            problems.append("commits in this backup were signed with a different "
                            "FOXNEST_AUTH_SECRET; they will not verify on this server")

        say("upgrading the restored database to this server's schema")
        url = "sqlite:///" + (scratch / "foxnest.db").as_posix()
        migration = upgrade.upgrade_database(url, snapshot=False, log=lambda m: None)
        report["upgraded_from"] = migration["from"]
        report["upgraded_through"] = migration["applied"]

        engine = create_engine(url)
        schema = inspect_schema(engine)
        report["schema_healthy"] = schema["healthy"]
        if not schema["healthy"]:
            problems.append("the restored database does not match the models after upgrading")

        say("comparing row counts")
        recorded = (manifest.get("database") or {}).get("row_counts") or {}
        counts = _row_counts(scratch / "foxnest.db")
        report["row_counts"] = counts
        differing = {t: (recorded[t], counts.get(t)) for t in recorded if counts.get(t) != recorded[t]}
        if differing:
            problems.append(f"row counts differ from the manifest: {differing}")

        say("resolving every branch head's files")
        store = BlobStore(scratch / "blobs")
        session = sessionmaker(bind=engine)()
        try:
            heads = {(b.repository_id, b.name): b.head_commit_id
                     for b in session.query(Branch).filter(Branch.head_commit_id.isnot(None))}
            files_checked = 0
            unreadable = []
            seen: Set[str] = set()
            for (repo_id, name), commit_id in heads.items():
                for (file_hash,) in session.query(CommitFile.file_hash).filter(
                        CommitFile.commit_id == commit_id):
                    digest = (file_hash or "").lower()
                    if not digest or digest in seen:
                        continue
                    seen.add(digest)
                    files_checked += 1
                    content = store.get(digest)
                    if content is None:
                        row = session.query(FileObject).filter(FileObject.hash == file_hash).first()
                        content = row.content if row is not None else None
                    if content is None or not _hashes_to(bytes(content), digest):
                        unreadable.append(f"{repo_id}:{name}")
            report["branches_checked"] = len(heads)
            report["head_files_checked"] = files_checked
            if unreadable:
                problems.append(f"{len(unreadable)} file(s) on branch heads do not read back "
                                f"(first: {unreadable[0]})")
        finally:
            session.close()
    except BackupError as exc:
        problems.append(exc.message)
    except Exception as exc:
        problems.append(f"{type(exc).__name__}: {exc}")
    finally:
        if engine is not None:
            engine.dispose()
        if not keep:
            _remove_tree(scratch)
            report.pop("scratch", None)

    report["problems"] = problems
    report["passed"] = not problems
    report["duration_ms"] = int((time.monotonic() - started) * 1000)
    return report


def _remove_tree(path: Path) -> None:
    """Delete a directory; Windows can hold a just-closed SQLite file for a moment."""
    for _ in range(10):
        shutil.rmtree(path, ignore_errors=True)
        if not Path(path).exists():
            return
        time.sleep(0.3)


# --- rotation -------------------------------------------------------------------------

def prune_backups(db: Session, keep: Optional[int] = None,
                  actor_id: Optional[int] = None) -> List[int]:
    """Delete backups beyond the newest ``keep`` verified ones.

    Never removes the newest verified backup or a running one. An unverified, failed or
    interrupted backup is removed once a newer verified one exists: it cannot be
    restored from, and keeping it only hides how much space the good ones use.
    """
    keep = keep or keep_count()
    rows = (db.query(BackupRecord)
            .filter(BackupRecord.status.in_(("complete", "failed", "interrupted")))
            .order_by(BackupRecord.created_at.desc(), BackupRecord.id.desc())
            .all())
    verified = [r for r in rows if r.status == "complete" and r.verified]
    if not verified:
        return []
    newest_verified = verified[0]
    doomed = verified[keep:]
    doomed += [r for r in rows if not (r.status == "complete" and r.verified)
               and (r.created_at or datetime.min) < (newest_verified.created_at or datetime.min)]
    pruned = []
    for record in doomed:
        if record.id == newest_verified.id:
            continue
        _remove_tree(Path(record.path))
        record.status = "pruned"
        record.pruned_at = datetime.utcnow()
        pruned.append(record.id)
    if pruned:
        db.commit()
        _audit(db, actor_id, "backup_pruned",
                     description=f"Removed {len(pruned)} old backup(s), keeping the newest {keep} verified",
                     target_type="backup", payload={"pruned": pruned})
    return pruned


# --- jobs -----------------------------------------------------------------------------

def _run(job: Callable[[Session], None], ticket) -> None:
    db = session_factory()
    try:
        job(db)
    finally:
        db.close()
        ticket.release()


def _launch(job: Callable[[Session], None], ticket, wait: bool) -> None:
    if wait:
        _run(job, ticket)
        return
    threading.Thread(target=_run, args=(job, ticket), daemon=True,
                     name="zanbeel-maintenance").start()


def start_backup(db: Session, label: Optional[str] = None, actor_id: Optional[int] = None,
                 kind: str = "manual", then_test_restore: bool = False,
                 wait: bool = False) -> BackupRecord:
    """Take the lock now (MaintenanceBusy if it is held), record the backup as running,
    and do the work on a background thread -- or here, with ``wait``."""
    from app.services import maintenance

    _live_db_path(db)  # Refuse PostgreSQL before taking the lock.
    ticket = maintenance.acquire("a backup")
    try:
        record = BackupRecord(label=label or kind, path="", kind=kind, status="running",
                              format=FORMAT, created_by_id=actor_id)
        db.add(record)
        db.commit()
        db.refresh(record)
    except BaseException:
        ticket.release()
        raise
    record_id = record.id

    def job(session: Session) -> None:
        row = session.query(BackupRecord).filter(BackupRecord.id == record_id).first()
        try:
            row = create_backup(session, label=label, actor_id=actor_id, kind=kind, record=row)
        except BackupError:
            return
        except Exception as exc:
            session.rollback()
            row = session.query(BackupRecord).filter(BackupRecord.id == record_id).first()
            _fail(session, row, f"{type(exc).__name__}: {exc}", time.monotonic())
            return
        if row.verified:
            prune_backups(session, actor_id=actor_id)
            if then_test_restore:
                _test_restore_record(session, row, actor_id)

    _launch(job, ticket, wait)
    db.expire_all()
    return db.query(BackupRecord).filter(BackupRecord.id == record_id).first()


def _test_restore_record(session: Session, row: BackupRecord, actor_id: Optional[int]) -> None:
    row.restore_test_status = "running"
    session.commit()
    report = test_restore(row.path)
    row.restore_test_status = "passed" if report["passed"] else "failed"
    row.restore_tested_at = datetime.utcnow()
    row.restore_test_report = json.dumps(report)
    session.commit()
    _audit(
        session, actor_id, "backup_restore_tested",
        description=(f"Test-restored backup {row.label} ({row.id}): "
                     + ("passed" if report["passed"] else "FAILED: " + "; ".join(report["problems"]))),
        target_type="backup", target_id=row.id,
        payload={"passed": report["passed"], "problems": report["problems"],
                 "duration_ms": report["duration_ms"]},
    )


def start_restore_test(db: Session, record: BackupRecord, actor_id: Optional[int] = None,
                       wait: bool = False) -> BackupRecord:
    from app.services import maintenance

    if record.status != "complete":
        raise BackupError(f"That backup is {record.status}; only a complete backup can be restored", 409)
    ticket = maintenance.acquire("a restore drill")
    record.restore_test_status = "running"
    db.commit()
    record_id = record.id

    def job(session: Session) -> None:
        row = session.query(BackupRecord).filter(BackupRecord.id == record_id).first()
        try:
            _test_restore_record(session, row, actor_id)
        except Exception as exc:
            session.rollback()
            row = session.query(BackupRecord).filter(BackupRecord.id == record_id).first()
            row.restore_test_status = "failed"
            row.restore_tested_at = datetime.utcnow()
            row.restore_test_report = json.dumps({"passed": False,
                                                  "problems": [f"{type(exc).__name__}: {exc}"]})
            session.commit()

    _launch(job, ticket, wait)
    db.expire_all()
    return db.query(BackupRecord).filter(BackupRecord.id == record_id).first()


def start_verify(db: Session, record: BackupRecord, actor_id: Optional[int] = None,
                 wait: bool = False) -> BackupRecord:
    from app.services import maintenance

    if record.status != "complete":
        raise BackupError(f"That backup is {record.status}; there is nothing to verify", 409)
    ticket = maintenance.acquire("a backup verification")
    record.verify_status = "running"
    db.commit()
    record_id = record.id

    def job(session: Session) -> None:
        row = session.query(BackupRecord).filter(BackupRecord.id == record_id).first()
        try:
            report = verify_backup(row.path, full=True)
        except BackupError as exc:
            report = {"verified": False, "problems": [exc.message]}
        except Exception as exc:
            report = {"verified": False, "problems": [f"{type(exc).__name__}: {exc}"]}
        row.verify_status = "passed" if report["verified"] else "failed"
        row.verified = bool(report["verified"])
        row.verified_at = datetime.utcnow()
        row.verify_report = json.dumps(report)
        session.commit()
        _audit(session, actor_id, "backup_verified",
                     description=(f"Re-verified backup {row.label} ({row.id}): "
                                  + ("passed" if report["verified"] else "FAILED: "
                                     + "; ".join(report["problems"]))),
                     target_type="backup", target_id=row.id,
                     payload={"verified": report["verified"], "problems": report["problems"]})

    _launch(job, ticket, wait)
    db.expire_all()
    return db.query(BackupRecord).filter(BackupRecord.id == record_id).first()


def recover_interrupted(db: Session) -> int:
    """At startup: jobs recorded as running with nobody holding the lock were cut off."""
    from app.services import maintenance

    if maintenance.current() is not None:
        return 0
    count = 0
    for row in db.query(BackupRecord).filter(BackupRecord.status == "running"):
        row.status = "interrupted"
        row.verified = False
        row.error = "The server stopped before this backup finished."
        row.finished_at = datetime.utcnow()
        count += 1
    for row in db.query(BackupRecord).filter(
            (BackupRecord.verify_status == "running") | (BackupRecord.restore_test_status == "running")):
        if row.verify_status == "running":
            row.verify_status = "failed"
        if row.restore_test_status == "running":
            row.restore_test_status = "failed"
            row.restore_test_report = json.dumps({"passed": False, "problems": [
                "The server stopped before the drill finished."]})
        count += 1
    if count:
        db.commit()
    return count


# --- the schedule ---------------------------------------------------------------------

_scheduler_stop = threading.Event()
_scheduler_thread: Optional[threading.Thread] = None


def _newest_verified(db: Session) -> Optional[BackupRecord]:
    return (db.query(BackupRecord)
            .filter(BackupRecord.status == "complete", BackupRecord.verified.is_(True))
            .order_by(BackupRecord.created_at.desc(), BackupRecord.id.desc())
            .first())


def scheduled_backup_due(db: Session, now: Optional[datetime] = None) -> bool:
    hours = interval_hours()
    if hours <= 0:
        return False
    newest = _newest_verified(db)
    if newest is None or newest.created_at is None:
        return True
    return (now or datetime.utcnow()) - newest.created_at >= timedelta(hours=hours)


def run_schedule_once() -> Optional[int]:
    """Take a scheduled backup if one is due. Returns its id, or None."""
    from app.services import maintenance

    db = session_factory()
    try:
        if not scheduled_backup_due(db):
            return None
        try:
            record = start_backup(db, label="scheduled", kind="scheduled",
                                  then_test_restore=test_restore_after_scheduled(), wait=True)
        except maintenance.MaintenanceBusy:
            return None
        except BackupError:
            return None
        return record.id
    finally:
        db.close()


def start_scheduler(check_every_seconds: int = 600) -> None:
    """Scheduled backups (FOXNEST_BACKUP_INTERVAL_HOURS > 0), checked every ten minutes.

    Due is decided from the database -- the newest verified backup's age -- so a restart
    neither skips one nor takes an extra, and two processes sharing the data cannot
    both run one: the second finds the lock held.
    """
    global _scheduler_thread
    if interval_hours() <= 0 or (_scheduler_thread and _scheduler_thread.is_alive()):
        return
    _scheduler_stop.clear()

    def loop():
        # A short first wait, so a backup that came due while the server was down is
        # taken soon after start without competing with startup itself.
        delay = 30
        while not _scheduler_stop.wait(timeout=delay):
            delay = check_every_seconds
            try:
                run_schedule_once()
            except Exception:
                pass

    _scheduler_thread = threading.Thread(target=loop, daemon=True, name="zanbeel-backup-schedule")
    _scheduler_thread.start()


def stop_scheduler() -> None:
    _scheduler_stop.set()


# --- reporting ------------------------------------------------------------------------

def summary(db: Session) -> Dict[str, Any]:
    """What the Operations page shows above the list."""
    from app.services import maintenance

    newest = _newest_verified(db)
    age_hours = None
    if newest is not None and newest.created_at is not None:
        age_hours = round((datetime.utcnow() - newest.created_at).total_seconds() / 3600, 1)
    root = backup_root()
    try:
        source = _live_db_path(db)
        volume = same_volume(root, source)
    except BackupError:
        volume = None
    newest_drill = (db.query(BackupRecord)
                    .filter(BackupRecord.restore_test_status.in_(("passed", "failed")))
                    .order_by(BackupRecord.restore_tested_at.desc())
                    .first())
    return {
        "destination": str(root),
        "same_volume_as_data": volume,
        "schedule": {
            "interval_hours": interval_hours(),
            "keep": keep_count(),
            "test_restore": test_restore_after_scheduled(),
            "next_due_at": ((newest.created_at + timedelta(hours=interval_hours())).isoformat()
                            if newest is not None and newest.created_at and interval_hours() else None),
        },
        "last_verified": serialize(newest) if newest is not None else None,
        "last_verified_age_hours": age_hours,
        "stale": age_hours is None or age_hours > stale_hours(),
        "stale_after_hours": stale_hours(),
        "last_restore_test": ({
            "backup_id": newest_drill.id,
            "status": newest_drill.restore_test_status,
            "at": newest_drill.restore_tested_at.isoformat() if newest_drill.restore_tested_at else None,
        } if newest_drill is not None else None),
        "running": maintenance.current(),
    }


def _loads(value: Optional[str]) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def serialize(record: BackupRecord) -> dict:
    return {
        "id": record.id,
        "label": record.label,
        "kind": record.kind or "manual",
        "status": record.status or "complete",
        "format": record.format or 1,
        "path": record.path,
        "database_bytes": record.database_bytes,
        "database_sha256": record.database_sha256,
        "schema_revision": record.schema_revision,
        "blob_bytes": record.blob_bytes,
        "blob_count": record.blob_count,
        "linked_blobs": record.linked_blobs or 0,
        "total_bytes": (record.database_bytes or 0) + (record.blob_bytes or 0),
        "verified": bool(record.verified),
        "missing_blobs": record.missing_blobs,
        "duration_ms": record.duration_ms,
        "error": record.error,
        "created_by": record.created_by.username if record.created_by else None,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        "verify_status": record.verify_status,
        "verified_at": record.verified_at.isoformat() if record.verified_at else None,
        "verify_report": _loads(record.verify_report),
        "restore_test_status": record.restore_test_status,
        "restore_tested_at": record.restore_tested_at.isoformat() if record.restore_tested_at else None,
        "restore_test_report": _loads(record.restore_test_report),
        "pruned_at": record.pruned_at.isoformat() if record.pruned_at else None,
    }
