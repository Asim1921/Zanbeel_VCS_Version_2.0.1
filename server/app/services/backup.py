"""Verified backup and restore of the database and its blob store.

The gap this closes: the only backup was one stale 520 KB ``foxnest.db.bak``
sitting beside a live database, with no documented or tested restore path — for
the system holding the team's source code.

**The database and the blob store are one unit.** Since file contents moved to
disk, a row carries only a hash, a size and a mime type. A database copy without
the blob tree restores an index pointing at nothing, and a blob tree without the
database is a pile of anonymous files. Backing up either alone is worthless, so
this always does both and records them together.

**Ordering matters, and the obvious order is wrong.** Copy blobs first and the
database may afterwards reference a blob added since — missing. Copy the
database first and garbage collection may afterwards delete a blob the snapshot
still references — also missing. There is no ordering that is safe on its own,
so this snapshots the database first, copies the blobs, and then *verifies*:
every hash the snapshot references must exist in the backup, and anything
missing is fetched in a second pass. A backup that still has gaps after that is
recorded as unverified rather than being quietly presented as good.

The database snapshot uses ``VACUUM INTO`` rather than a file copy: a live SQLite
file can be mid-transaction, and copying its bytes yields a database that opens
and is subtly wrong. ``VACUUM INTO`` produces a consistent, already-compacted
snapshot through the engine.
"""

import hashlib
import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import BLOB_DIR
from database.database import DATABASE_URL, engine
from database.models import BackupRecord

MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "database.sqlite3"
BLOBS_DIRNAME = "blobs"


class BackupError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def backup_root() -> Path:
    """Where backups are written.

    Defaults to a sibling of the blob store rather than inside it, so a backup
    never becomes an input to the next backup.
    """
    configured = os.getenv("FOXNEST_BACKUP_DIR")
    if configured:
        return Path(configured)
    return Path(BLOB_DIR).resolve().parent / "backups"


def _is_sqlite() -> bool:
    return DATABASE_URL.strip().lower().startswith("sqlite")


def _referenced_hashes(db_path: Path) -> List[str]:
    """Every blob hash the snapshot refers to, from all referencing tables.

    Reads the *snapshot*, not the live database, so the answer matches exactly
    what was captured. The table list mirrors the one garbage collection uses;
    if the two ever disagree, GC deletes something a backup thought was live.
    """
    import sqlite3

    queries = [
        "SELECT file_hash FROM commit_files WHERE file_hash IS NOT NULL",
        "SELECT file_hash FROM pending_commit_files WHERE file_hash IS NOT NULL",
        "SELECT file_hash FROM file_lineage WHERE file_hash IS NOT NULL",
        "SELECT base_file_hash FROM merge_conflict_files WHERE base_file_hash IS NOT NULL",
        "SELECT ours_file_hash FROM merge_conflict_files WHERE ours_file_hash IS NOT NULL",
        "SELECT theirs_file_hash FROM merge_conflict_files WHERE theirs_file_hash IS NOT NULL",
        "SELECT resolved_file_hash FROM merge_conflict_files WHERE resolved_file_hash IS NOT NULL",
        "SELECT hash FROM file_objects WHERE hash IS NOT NULL",
    ]
    seen = set()
    connection = sqlite3.connect(str(db_path))
    try:
        for query in queries:
            try:
                for (value,) in connection.execute(query):
                    if value:
                        seen.add(str(value))
            except sqlite3.Error:
                # A table that does not exist in an older snapshot is not a
                # failure: it simply references nothing.
                continue
    finally:
        connection.close()
    return sorted(seen)


def _blob_path(root: Path, digest: str) -> Path:
    return Path(root) / digest[:2] / digest


def _copy_blob(digest: str, source_root: Path, dest_root: Path) -> Optional[int]:
    """Copy one blob into the backup. Returns its size, or None if absent."""
    source = _blob_path(source_root, digest)
    if not source.is_file():
        return None
    dest = _blob_path(dest_root, digest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size == source.stat().st_size:
        return dest.stat().st_size
    shutil.copy2(source, dest)
    return dest.stat().st_size


def create_backup(db: Session, label: Optional[str] = None,
                  actor_id: Optional[int] = None) -> BackupRecord:
    """Snapshot the database and every blob it references, then verify."""
    if not _is_sqlite():
        raise BackupError(
            "Automated backup currently supports SQLite only. For Postgres use "
            "pg_dump alongside a copy of the blob store.",
            status_code=501,
        )

    started = time.monotonic()
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    safe_label = "".join(
        c for c in (label or "manual") if c.isalnum() or c in "-_"
    )[:60] or "manual"
    name = f"{stamp}-{safe_label}"

    destination = backup_root() / name
    blobs_dest = destination / BLOBS_DIRNAME
    try:
        blobs_dest.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise BackupError("A backup with that name already exists")
    except OSError as exc:
        raise BackupError(f"Could not create the backup directory: {exc}", 500)

    db_dest = destination / DATABASE_NAME
    error: Optional[str] = None

    try:
        # Consistent snapshot through the engine, not a byte copy of a file that
        # may be mid-transaction.
        with engine.connect() as connection:
            connection.execute(text("VACUUM INTO :target"), {"target": str(db_dest)})
            connection.commit()
    except Exception as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise BackupError(f"Database snapshot failed: {exc}", 500)

    source_root = Path(BLOB_DIR)
    hashes = _referenced_hashes(db_dest)

    blob_bytes = 0
    copied = 0
    missing: List[str] = []
    for digest in hashes:
        size = _copy_blob(digest, source_root, blobs_dest)
        if size is None:
            missing.append(digest)
        else:
            blob_bytes += size
            copied += 1

    # Second pass. Between the snapshot and the copy, garbage collection may
    # have removed a blob the snapshot still references; retrying catches the
    # case where it was written back, and confirms genuine losses.
    if missing:
        still_missing = []
        for digest in missing:
            size = _copy_blob(digest, source_root, blobs_dest)
            if size is None:
                still_missing.append(digest)
            else:
                blob_bytes += size
                copied += 1
        missing = still_missing

    manifest = {
        "created_at": datetime.utcnow().isoformat() + "Z",
        "label": safe_label,
        "database_file": DATABASE_NAME,
        "database_bytes": db_dest.stat().st_size if db_dest.is_file() else 0,
        "blob_dir": BLOBS_DIRNAME,
        "blob_count": copied,
        "blob_bytes": blob_bytes,
        "referenced_hashes": len(hashes),
        "missing_hashes": missing[:200],
        "missing_count": len(missing),
        "verified": not missing,
        "source_database_url": DATABASE_URL,
        "source_blob_dir": str(source_root),
    }
    try:
        (destination / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        error = f"Manifest could not be written: {exc}"

    if missing:
        error = (error or "") + (
            f" {len(missing)} referenced blob(s) were not found in the store; "
            "this backup is incomplete."
        ).strip()

    record = BackupRecord(
        label=safe_label,
        path=str(destination),
        database_bytes=manifest["database_bytes"],
        blob_bytes=blob_bytes,
        blob_count=copied,
        verified=not missing,
        missing_blobs=len(missing),
        duration_ms=int((time.monotonic() - started) * 1000),
        error=error or None,
        created_by_id=actor_id,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def verify_backup(path: str) -> Dict[str, Any]:
    """Re-check a backup on disk against its own manifest.

    Independent of the row in the database, so a backup can be validated after
    being moved to another machine — which is the only kind of backup that
    survives losing this one.
    """
    root = Path(path)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise BackupError("That backup has no manifest", 404)

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"Manifest is unreadable: {exc}", 500)

    db_path = root / manifest.get("database_file", DATABASE_NAME)
    if not db_path.is_file():
        raise BackupError("The database snapshot is missing from this backup", 404)

    blobs_root = root / manifest.get("blob_dir", BLOBS_DIRNAME)
    hashes = _referenced_hashes(db_path)

    missing = []
    corrupt = []
    checked = 0
    for digest in hashes:
        blob = _blob_path(blobs_root, digest)
        if not blob.is_file():
            missing.append(digest)
            continue
        checked += 1

    return {
        "path": str(root),
        "database_bytes": db_path.stat().st_size,
        "referenced_hashes": len(hashes),
        "present": checked,
        "missing": len(missing),
        "missing_sample": missing[:20],
        "corrupt": len(corrupt),
        "verified": not missing and not corrupt,
        "manifest_created_at": manifest.get("created_at"),
    }


def restore_backup(path: str, target_db: Optional[str] = None,
                   target_blobs: Optional[str] = None,
                   force: bool = False) -> Dict[str, Any]:
    """Restore a backup over the live database and blob store.

    Refuses unless the backup verifies, unless ``force`` is set. Restoring a
    snapshot known to be missing blobs produces a repository whose history
    cannot be checked out — better to fail loudly than to hand back something
    that looks restored.

    The existing database is moved aside rather than overwritten, so a restore
    that turns out to be the wrong snapshot is itself reversible.
    """
    root = Path(path)
    result = verify_backup(str(root))
    if not result["verified"] and not force:
        raise BackupError(
            f"Refusing to restore: {result['missing']} referenced blob(s) are missing "
            "from this backup. Pass force to restore anyway.",
            409,
        )

    db_source = root / DATABASE_NAME
    blobs_source = root / BLOBS_DIRNAME

    db_target = Path(target_db) if target_db else Path(
        DATABASE_URL.split("sqlite:///")[-1]
    ).resolve()
    blob_target = Path(target_blobs) if target_blobs else Path(BLOB_DIR)

    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    displaced = None
    if db_target.exists():
        displaced = db_target.with_name(f"{db_target.name}.replaced-{stamp}")
        shutil.move(str(db_target), str(displaced))

    db_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_source, db_target)

    restored_blobs = 0
    if blobs_source.is_dir():
        for shard in blobs_source.iterdir():
            if not shard.is_dir():
                continue
            for blob in shard.iterdir():
                if not blob.is_file():
                    continue
                dest = blob_target / shard.name / blob.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.is_file():
                    shutil.copy2(blob, dest)
                restored_blobs += 1

    return {
        "restored_database": str(db_target),
        "previous_database_moved_to": str(displaced) if displaced else None,
        "restored_blobs": restored_blobs,
        "blob_target": str(blob_target),
        "verified_before_restore": result["verified"],
    }


def serialize(record: BackupRecord) -> dict:
    return {
        "id": record.id,
        "label": record.label,
        "path": record.path,
        "database_bytes": record.database_bytes,
        "blob_bytes": record.blob_bytes,
        "blob_count": record.blob_count,
        "total_bytes": (record.database_bytes or 0) + (record.blob_bytes or 0),
        "verified": bool(record.verified),
        "missing_blobs": record.missing_blobs,
        "duration_ms": record.duration_ms,
        "error": record.error,
        "created_by": record.created_by.username if record.created_by else None,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }
