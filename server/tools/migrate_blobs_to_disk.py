#!/usr/bin/env python3
"""Move file contents out of the database and onto the filesystem.

Runs in two phases, deliberately separated so the destructive one cannot start until the
data is provably safe elsewhere:

  1. **export**  Read every inline blob, write it to the blob store, verify it reads back
     byte-for-byte. Touches no database rows, so it is interruptible and re-runnable.

  2. **detach**  Clear ``file_objects.content``. Refuses to start unless *every* inline row
     already has a verified on-disk copy. The live schema declares the column ``NOT NULL``,
     so on SQLite this rebuilds the table (fast -- it writes NULLs, not blobs) and on
     PostgreSQL it drops the constraint first.

Typical run:

    python tools/migrate_blobs_to_disk.py --status
    python tools/migrate_blobs_to_disk.py --export
    python tools/migrate_blobs_to_disk.py --detach
    python tools/migrate_blobs_to_disk.py --vacuum     # reclaim the freed pages

Do this before the PostgreSQL cutover: moving ~4 GB of bytea is what makes that migration
slow, and the largest blob sits at 89% of Postgres's 1 GiB per-field hard limit.

Back up the database first. --detach rewrites a table.
"""

import argparse
import os
import sys

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_ALLOW_INSECURE_AUTH", "1")

from sqlalchemy import text  # noqa: E402

from app.services.blob_store import store  # noqa: E402
from database.database import engine  # noqa: E402


def human(num_bytes):
    value = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024


def is_sqlite():
    return engine.dialect.name == "sqlite"


def survey(conn):
    total, total_bytes = conn.execute(
        text("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM file_objects")
    ).one()
    inline, inline_bytes = conn.execute(
        text("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM file_objects WHERE content IS NOT NULL")
    ).one()
    return {"objects": total, "bytes": total_bytes, "inline": inline, "inline_bytes": inline_bytes}


def cmd_status():
    with engine.connect() as conn:
        s = survey(conn)
    disk = store.stats()
    print(f"database   : {s['objects']:,} objects, {human(s['bytes'])} tracked")
    print(f"  inline   : {s['inline']:,} rows still holding bytes, {human(s['inline_bytes'])}")
    print(f"blob store : {disk['root']}")
    print(f"  on disk  : {disk['objects']:,} blobs, {human(disk['bytes'])}")
    if s["inline"]:
        print("\nNext: --export")
    else:
        print("\nAll content is on disk. Run --vacuum to reclaim database pages.")
    return 0


def cmd_export(batch_size):
    """Copy inline blobs to disk and verify. No database writes."""
    with engine.connect() as conn:
        s = survey(conn)
        if not s["inline"]:
            print("Nothing to export -- no rows hold inline content.")
            return 0
        print(f"Exporting {s['inline']:,} blobs ({human(s['inline_bytes'])}) to {store.root}")

        done = skipped = failed = 0
        moved_bytes = 0
        last_hash = ""
        while True:
            rows = conn.execute(
                text(
                    "SELECT hash, content FROM file_objects "
                    "WHERE content IS NOT NULL AND hash > :last "
                    "ORDER BY hash LIMIT :lim"
                ),
                {"last": last_hash, "lim": batch_size},
            ).all()
            if not rows:
                break
            for blob_hash, content in rows:
                last_hash = blob_hash
                if content is None:
                    continue
                try:
                    if store.exists(blob_hash) and store.get(blob_hash) == content:
                        skipped += 1
                        continue
                    store.put(content, blob_hash)
                    if store.get(blob_hash) != content:
                        raise IOError("read-back mismatch")
                    done += 1
                    moved_bytes += len(content)
                except Exception as exc:            # noqa: BLE001 - report and keep going
                    failed += 1
                    print(f"\n  ! {blob_hash}: {exc}")
            print(f"  exported {done + skipped:,} / {s['inline']:,} ({human(moved_bytes)})", end="\r")

        print()
        print(f"\nExported {done:,} ({human(moved_bytes)}), {skipped:,} already present, {failed} failed.")
        if failed:
            print("Fix the failures before running --detach.")
            return 1
        print("Next: --detach")
        return 0


def _unverified_rows(conn):
    """Inline rows whose bytes are not yet provably on disk."""
    bad = []
    last_hash = ""
    while True:
        rows = conn.execute(
            text(
                "SELECT hash, content FROM file_objects "
                "WHERE content IS NOT NULL AND hash > :last ORDER BY hash LIMIT 500"
            ),
            {"last": last_hash},
        ).all()
        if not rows:
            return bad
        for blob_hash, content in rows:
            last_hash = blob_hash
            if store.get(blob_hash) != content:
                bad.append(blob_hash)


def cmd_detach():
    with engine.connect() as conn:
        s = survey(conn)
        if not s["inline"]:
            print("Nothing to detach -- content column is already empty.")
            return 0

        print(f"Verifying {s['inline']:,} inline blobs against the store...")
        unverified = _unverified_rows(conn)
        if unverified:
            print(f"\nREFUSING: {len(unverified)} row(s) have no verified copy on disk, e.g.:")
            for blob_hash in unverified[:5]:
                print(f"  {blob_hash}")
            print("\nRun --export again first.")
            return 1
        print("All inline blobs verified on disk.")

    print("Clearing the column...")
    if is_sqlite():
        # SQLite cannot drop NOT NULL in place, so rebuild. This writes NULLs rather than
        # copying blobs, so it is fast, and dropping the old table frees its pages.
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            conn.execute(text("""
                CREATE TABLE file_objects_new (
                    hash VARCHAR(40) NOT NULL,
                    content BLOB,
                    size INTEGER NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    mime_type VARCHAR(100),
                    PRIMARY KEY (hash)
                )
            """))
            conn.execute(text(
                "INSERT INTO file_objects_new (hash, content, size, created_at, mime_type) "
                "SELECT hash, NULL, size, created_at, mime_type FROM file_objects"
            ))
            conn.execute(text("DROP TABLE file_objects"))
            conn.execute(text("ALTER TABLE file_objects_new RENAME TO file_objects"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_file_objects_hash ON file_objects (hash)"))
            conn.execute(text("PRAGMA foreign_keys=ON"))
    else:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE file_objects ALTER COLUMN content DROP NOT NULL"))
            conn.execute(text("UPDATE file_objects SET content = NULL WHERE content IS NOT NULL"))

    with engine.connect() as conn:
        after = survey(conn)
    print(f"Done. Inline rows remaining: {after['inline']:,}")
    print("\nThe database file will not shrink until you run --vacuum.")
    return 0


def cmd_vacuum():
    if not is_sqlite():
        print("On PostgreSQL run this in a maintenance window (it takes an exclusive lock):")
        print("  psql -c 'VACUUM FULL file_objects;'")
        return 0
    print("Vacuuming (needs free disk roughly equal to the current database size)...")
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("VACUUM")
    print("Done.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="report progress (default)")
    group.add_argument("--export", action="store_true", help="copy inline blobs to disk and verify")
    group.add_argument("--detach", action="store_true", help="clear the content column (destructive)")
    group.add_argument("--vacuum", action="store_true", help="reclaim freed pages after --detach")
    parser.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args()

    if args.export:
        return cmd_export(args.batch_size)
    if args.detach:
        return cmd_detach()
    if args.vacuum:
        return cmd_vacuum()
    return cmd_status()


if __name__ == "__main__":
    raise SystemExit(main())
