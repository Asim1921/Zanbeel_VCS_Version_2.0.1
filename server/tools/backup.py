"""Back up, verify, list and restore Zanbeel.

Run from the ``server/`` directory:

    py -3.11 tools/backup.py --create --label nightly
    py -3.11 tools/backup.py --list
    py -3.11 tools/backup.py --verify <path>
    py -3.11 tools/backup.py --restore <path>            # refuses unless verified
    py -3.11 tools/backup.py --restore <path> --force    # restore anyway

The database and the blob store are backed up together and always must be:
rows hold only hashes, so a database without its blobs restores an index
pointing at nothing.

Restore moves the current database aside rather than deleting it, so choosing
the wrong snapshot is itself recoverable.
"""

import argparse
import os
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parent.parent
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from database.database import SessionLocal  # noqa: E402
from database.models import BackupRecord  # noqa: E402
from app.services import backup as backup_service  # noqa: E402


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def do_create(label):
    db = SessionLocal()
    try:
        print(f"Backing up to {backup_service.backup_root()} ...")
        record = backup_service.create_backup(db, label=label)
        print()
        print(f"  path      : {record.path}")
        print(f"  database  : {human(record.database_bytes)}")
        print(f"  blobs     : {record.blob_count} objects, {human(record.blob_bytes)}")
        print(f"  duration  : {record.duration_ms} ms")
        print(f"  verified  : {'yes' if record.verified else 'NO'}")
        if record.missing_blobs:
            print(f"  MISSING   : {record.missing_blobs} referenced blob(s) were not found")
        if record.error:
            print(f"  note      : {record.error}")
        return 0 if record.verified else 1
    except backup_service.BackupError as exc:
        print(f"Backup failed: {exc.message}")
        return 2
    finally:
        db.close()


def do_list():
    db = SessionLocal()
    try:
        rows = db.query(BackupRecord).order_by(BackupRecord.created_at.desc()).all()
        if not rows:
            print("No backups recorded. Create one with --create.")
            return 0
        print(f"{'WHEN':<21} {'LABEL':<16} {'DB':<10} {'BLOBS':<10} {'OBJECTS':<9} OK   PATH")
        for r in rows:
            when = (r.created_at.isoformat() if r.created_at else "")[:19].replace("T", " ")
            print(
                f"{when:<21} {r.label[:15]:<16} {human(r.database_bytes):<10} "
                f"{human(r.blob_bytes):<10} {r.blob_count:<9} "
                f"{'yes' if r.verified else 'NO ':<4} {r.path}"
            )
        return 0
    finally:
        db.close()


def do_verify(path):
    try:
        result = backup_service.verify_backup(path)
    except backup_service.BackupError as exc:
        print(f"Verify failed: {exc.message}")
        return 2
    print(f"  path                : {result['path']}")
    print(f"  database            : {human(result['database_bytes'])}")
    print(f"  referenced hashes   : {result['referenced_hashes']}")
    print(f"  present in backup   : {result['present']}")
    print(f"  missing             : {result['missing']}")
    if result["missing_sample"]:
        print(f"  missing sample      : {', '.join(result['missing_sample'][:5])}")
    print(f"  verified            : {'yes' if result['verified'] else 'NO'}")
    return 0 if result["verified"] else 1


def do_restore(path, force, target_db, target_blobs):
    print("This replaces the live database and merges blobs back into the store.")
    print("The current database is moved aside, not deleted.")
    try:
        result = backup_service.restore_backup(
            path, target_db=target_db, target_blobs=target_blobs, force=force
        )
    except backup_service.BackupError as exc:
        print(f"Restore refused: {exc.message}")
        return 2
    print()
    print(f"  restored database : {result['restored_database']}")
    if result["previous_database_moved_to"]:
        print(f"  previous kept at  : {result['previous_database_moved_to']}")
    print(f"  blobs restored    : {result['restored_blobs']} into {result['blob_target']}")
    print(f"  backup verified   : {'yes' if result['verified_before_restore'] else 'NO (forced)'}")
    print()
    print("Restart the server so it opens the restored database.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Zanbeel backup and restore")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_true", help="Take a verified backup")
    group.add_argument("--list", action="store_true", help="List recorded backups")
    group.add_argument("--verify", metavar="PATH", help="Re-check a backup on disk")
    group.add_argument("--restore", metavar="PATH", help="Restore a backup")
    parser.add_argument("--label", help="A name for this backup, e.g. 'pre-upgrade'")
    parser.add_argument("--force", action="store_true",
                        help="Restore even if the backup does not verify")
    parser.add_argument("--target-db", help="Restore the database to this path instead")
    parser.add_argument("--target-blobs", help="Restore blobs into this directory instead")
    args = parser.parse_args()

    if args.create:
        return do_create(args.label)
    if args.list:
        return do_list()
    if args.verify:
        return do_verify(args.verify)
    if args.restore:
        return do_restore(args.restore, args.force, args.target_db, args.target_blobs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
