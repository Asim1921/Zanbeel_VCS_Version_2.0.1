"""Back up, verify, test-restore, restore and rotate Zanbeel's data.

Run from the ``server/`` directory with the project's interpreter:

    ..\\venv\\Scripts\\python.exe tools/backup.py --create [--label pre-upgrade] [--test-restore]
    ..\\venv\\Scripts\\python.exe tools/backup.py --list
    ..\\venv\\Scripts\\python.exe tools/backup.py --verify <path|id> [--quick]
    ..\\venv\\Scripts\\python.exe tools/backup.py --test-restore <path|id> [--keep]
    ..\\venv\\Scripts\\python.exe tools/backup.py --restore <path|id> [--force] [--yes]
    ..\\venv\\Scripts\\python.exe tools/backup.py --prune [--keep-count N]

A backup holds the database, every blob it references and the generated documents, and
is verified by reading every blob back. ``--test-restore`` restores it into a scratch
directory, upgrades that to this server's schema and reads it back, touching nothing
live: run it after every backup you would rely on.

``--restore`` replaces the live database, blob store and documents. Stop the server
first. What it replaces is moved aside (``*.replaced-<time>``), not deleted, so a
restore of the wrong backup can itself be undone. The search index is moved aside too
and rebuilds when the server starts. Start the server afterwards: if the backup is from
an older version, startup upgrades it (taking a copy first).

Exit status: 0 success, 1 a backup or check that did not verify, 2 an error or refusal.
"""

import argparse
import json
import os
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parent.parent
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def _session():
    from database.database import SessionLocal

    return SessionLocal()


def _resolve(target):
    """A backup directory, or the id of a recorded backup."""
    if str(target).isdigit():
        from database.models import BackupRecord

        db = _session()
        try:
            record = db.query(BackupRecord).filter(BackupRecord.id == int(target)).first()
            if record is None:
                raise SystemExit(f"No backup with id {target}")
            return record.path, record.id
        finally:
            db.close()
    return str(Path(target).resolve()), None


def do_create(label, test_restore):
    from app.services import backup, maintenance

    db = _session()
    try:
        print(f"Backing up to {backup.backup_root()} ...")
        record = backup.start_backup(db, label=label, kind="manual",
                                     then_test_restore=test_restore, wait=True)
    except maintenance.MaintenanceBusy as exc:
        print(f"Not started: {exc}")
        return 2
    except backup.BackupError as exc:
        print(f"Backup failed: {exc.message}")
        return 2
    finally:
        db.close()

    db = _session()
    try:
        from database.models import BackupRecord
        record = db.query(BackupRecord).filter(BackupRecord.id == record.id).first()
        print()
        print(f"  id        : {record.id}")
        print(f"  path      : {record.path}")
        print(f"  status    : {record.status}")
        print(f"  database  : {human(record.database_bytes)}  (schema {record.schema_revision})")
        print(f"  blobs     : {record.blob_count} objects, {human(record.blob_bytes)}; "
              f"{record.linked_blobs or 0} unchanged and linked from the previous backup")
        print(f"  duration  : {record.duration_ms} ms")
        print(f"  verified  : {'yes' if record.verified else 'NO'}")
        if record.error:
            print(f"  problems  : {record.error}")
        if test_restore and record.restore_test_status:
            report = json.loads(record.restore_test_report or "{}")
            print(f"  drill     : {record.restore_test_status}"
                  + (f" -- {'; '.join(report.get('problems') or [])}" if report.get("problems") else ""))
        same = backup.same_volume(backup.backup_root(), backup._live_db_path())
        if same:
            print()
            print("  note: this backup is on the same disk as the live data. Set")
            print("        FOXNEST_BACKUP_DIR to another disk, or copy it off this machine.")
        ok = record.verified and (not test_restore or record.restore_test_status == "passed")
        return 0 if ok else 1
    finally:
        db.close()


def do_list():
    from database.models import BackupRecord

    db = _session()
    try:
        rows = (db.query(BackupRecord).order_by(BackupRecord.created_at.desc(), BackupRecord.id.desc())
                .limit(50).all())
        if not rows:
            print("No backups recorded. Create one with --create.")
            return 0
        print(f"{'ID':>4} {'WHEN':<19} {'KIND':<9} {'STATUS':<11} {'DB':>9} {'BLOBS':>9} "
              f"{'LINKED':>6} OK  DRILL   PATH")
        for r in rows:
            when = (r.created_at.isoformat() if r.created_at else "")[:19].replace("T", " ")
            print(f"{r.id:>4} {when:<19} {(r.kind or 'manual'):<9} {(r.status or 'complete'):<11} "
                  f"{human(r.database_bytes):>9} {human(r.blob_bytes):>9} {r.linked_blobs or 0:>6} "
                  f"{'yes' if r.verified else 'NO ':<3} {(r.restore_test_status or '-'):<7} {r.path}")
        return 0
    finally:
        db.close()


def do_verify(target, quick):
    from app.services import backup

    path, _ = _resolve(target)
    try:
        result = backup.verify_backup(path, full=not quick)
    except backup.BackupError as exc:
        print(f"Verify failed: {exc.message}")
        return 2
    print(f"  path              : {result['path']}  (format {result['format']})")
    print(f"  database          : {human(result['database_bytes'])}, integrity {result['integrity']}, "
          f"schema {result['schema_revision']}")
    print(f"  referenced blobs  : {result['referenced_hashes']}  "
          f"({'re-hashed' if result['full'] else 'presence only'})")
    print(f"  present / inline  : {result['present']} / {result['inline']}")
    print(f"  missing / corrupt : {result['missing']} / {result['corrupt']}")
    print(f"  documents checked : {result['docs_checked']}")
    if not result["signing_key_matches"]:
        print("  signing key       : DIFFERENT from this server's FOXNEST_AUTH_SECRET")
    for problem in result["problems"]:
        print(f"  problem           : {problem}")
    print(f"  verified          : {'yes' if result['verified'] else 'NO'}  ({result['duration_ms']} ms)")
    return 0 if result["verified"] else 1


def do_test_restore(target, keep):
    from app.services import backup, maintenance

    path, record_id = _resolve(target)
    try:
        with maintenance.acquire("a restore drill"):
            report = backup.test_restore(path, keep=keep, progress=lambda m: print(f"  ... {m}"))
    except maintenance.MaintenanceBusy as exc:
        print(f"Not started: {exc}")
        return 2
    if record_id is not None:
        from datetime import datetime
        from database.models import BackupRecord
        db = _session()
        try:
            row = db.query(BackupRecord).filter(BackupRecord.id == record_id).first()
            row.restore_test_status = "passed" if report["passed"] else "failed"
            row.restore_tested_at = datetime.utcnow()
            row.restore_test_report = json.dumps(report)
            db.commit()
        finally:
            db.close()
    print()
    restore = report.get("restore") or {}
    print(f"  restored         : {restore.get('restored_blobs', 0)} blobs, "
          f"{restore.get('restored_docs', 0)} documents")
    if report.get("upgraded_through"):
        print(f"  upgraded through : {', '.join(report['upgraded_through'])}")
    print(f"  schema healthy   : {report.get('schema_healthy')}")
    print(f"  row counts       : {report.get('row_counts')}")
    print(f"  branch heads     : {report.get('branches_checked', 0)} branches, "
          f"{report.get('head_files_checked', 0)} files read back")
    for problem in report["problems"]:
        print(f"  problem          : {problem}")
    if keep and report.get("scratch"):
        print(f"  kept at          : {report['scratch']}")
    print(f"  result           : {'PASSED' if report['passed'] else 'FAILED'}  ({report['duration_ms']} ms)")
    return 0 if report["passed"] else 1


def do_restore(target, force, yes, target_db, target_blobs, target_docs):
    from app.services import backup, maintenance

    path, _ = _resolve(target)
    live = not (target_db or target_blobs or target_docs)
    if live and not yes:
        print("This replaces the LIVE database, blob store and generated documents with:")
        print(f"  {path}")
        print("Stop the server first. What is replaced is moved aside, not deleted.")
        answer = input("Type 'restore' to continue: ").strip().lower()
        if answer != "restore":
            print("Cancelled.")
            return 2
    try:
        with maintenance.acquire("a restore"):
            result = backup.restore_backup(path, target_db=target_db, target_blobs=target_blobs,
                                           target_docs=target_docs, force=force)
    except maintenance.MaintenanceBusy as exc:
        print(f"Not started: {exc}")
        return 2
    except backup.BackupError as exc:
        print(f"Restore refused: {exc.message}")
        return 2
    print()
    print(f"  restored database : {result['restored_database']}  (schema {result['schema_revision']})")
    for what, where in result["moved_aside"].items():
        print(f"  moved aside       : {what} -> {where}")
    print(f"  blobs             : {result['restored_blobs']} restored, "
          f"{result['already_present_blobs']} already present, "
          f"{result['replaced_damaged_blobs']} damaged copies replaced, into {result['blob_target']}")
    print(f"  documents         : {result['restored_docs']} into {result['docs_target']}")
    print(f"  backup verified   : {'yes' if result['verified_before_restore'] else 'NO (forced)'}")
    if not result["signing_key_matches"]:
        print("  WARNING           : the backup's commits were signed with a different")
        print("                      FOXNEST_AUTH_SECRET; restore that secret or signatures will")
        print("                      report as invalid.")
    print()
    print("Start the server: it opens the restored database (upgrading it first if it is")
    print("from an older version) and rebuilds the code search index in the background.")
    return 0


def do_prune(keep):
    from app.services import backup, maintenance

    db = _session()
    try:
        with maintenance.acquire("backup rotation"):
            pruned = backup.prune_backups(db, keep=keep)
    except maintenance.MaintenanceBusy as exc:
        print(f"Not started: {exc}")
        return 2
    finally:
        db.close()
    print(f"Removed {len(pruned)} backup(s)" + (f": {', '.join(map(str, pruned))}" if pruned else ""))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Zanbeel backup and restore")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_true", help="Take a verified backup")
    group.add_argument("--list", action="store_true", help="List recorded backups")
    group.add_argument("--verify", metavar="PATH_OR_ID", help="Re-check a backup on disk")
    group.add_argument("--test-restore", metavar="PATH_OR_ID", dest="test_restore_target",
                       help="Restore into a scratch directory and read it back")
    group.add_argument("--restore", metavar="PATH_OR_ID", help="Restore a backup over the live data")
    group.add_argument("--prune", action="store_true", help="Remove backups beyond the newest verified ones")
    parser.add_argument("--label", help="A name for this backup, e.g. 'pre-upgrade'")
    parser.add_argument("--test-restore-after", "--drill", action="store_true", dest="drill",
                        help="With --create: run a restore drill on the new backup")
    parser.add_argument("--quick", action="store_true", help="With --verify: presence only, no re-hashing")
    parser.add_argument("--keep", action="store_true", help="With --test-restore: keep the scratch copy")
    parser.add_argument("--keep-count", type=int, help="With --prune: verified backups to keep")
    parser.add_argument("--force", action="store_true", help="Restore even if the backup does not verify")
    parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    parser.add_argument("--target-db", help="Restore the database to this path instead")
    parser.add_argument("--target-blobs", help="Restore blobs into this directory instead")
    parser.add_argument("--target-docs", help="Restore documents into this directory instead")
    args = parser.parse_args()

    if args.create:
        return do_create(args.label, args.drill)
    if args.list:
        return do_list()
    if args.verify:
        return do_verify(args.verify, args.quick)
    if args.test_restore_target:
        return do_test_restore(args.test_restore_target, args.keep)
    if args.restore:
        return do_restore(args.restore, args.force, args.yes, args.target_db,
                          args.target_blobs, args.target_docs)
    if args.prune:
        return do_prune(args.keep_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
