"""Inspect and upgrade the database schema.

Run from the ``server/`` directory:

    ..\\venv\\Scripts\\python.exe tools/schema.py --status     # revision, and what is pending
    ..\\venv\\Scripts\\python.exe tools/schema.py --check      # does the live schema match the models?
    ..\\venv\\Scripts\\python.exe tools/schema.py --upgrade    # apply pending revisions (copy taken first)
    ..\\venv\\Scripts\\python.exe tools/schema.py --history    # revisions this code has
    ..\\venv\\Scripts\\python.exe tools/schema.py --stamp REV  # record REV without running anything

``--url sqlite:///path/to.db`` points any of these at another database.

The server applies pending revisions itself when it starts (unless
``FOXNEST_AUTO_MIGRATE=0``), so ``--upgrade`` is for an operator who wants to upgrade
first and start second -- for example to watch it, or to run it before a deploy.
Either way the upgrade is one transaction: it applies completely or not at all.

Exit status: 0 healthy / done, 1 drift or pending revisions, 2 an error.
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


def _engine(url):
    from sqlalchemy import create_engine

    if url:
        return create_engine(url)
    from database.database import engine

    return engine


def do_status(url):
    from app.db import upgrade

    state = upgrade.status(url)
    print(f"  database revision : {state['current'] or '(none recorded)'}")
    print(f"  this code's head  : {state['head']}")
    if state["unknown"]:
        print(f"  UNKNOWN revision  : {', '.join(state['unknown'])} -- upgraded by a newer server")
        return 1
    if state["pending"]:
        print(f"  pending           : {', '.join(state['pending'])}")
        print(f"  startup migration : {'on' if state['auto_migrate'] else 'off (FOXNEST_AUTO_MIGRATE=0)'}")
        return 1
    print("  pending           : none")
    from database.database import SessionLocal

    if not url:
        db = SessionLocal()
        try:
            runs = upgrade.recent_runs(db, limit=3)
        finally:
            db.close()
        for run in runs:
            print(f"  upgraded          : {run['finished_at']}  {run['from_revision'] or '(none)'} -> "
                  f"{run['to_revision']}"
                  + (f"\n                      copy before it: {run['snapshot_path']}"
                     if run["snapshot_path"] else ""))
    return 0


def do_check(url):
    from app.db.schema_check import describe, inspect_schema

    report = inspect_schema(_engine(url))
    print(f"  revision        : {report['revision'].get('current') or '(none recorded)'}"
          f" (head {report['revision'].get('head')})")
    print(f"  tables expected : {report['tables_expected']}")
    print(f"  tables present  : {report['tables_present']}")
    print(f"  missing tables  : {len(report['missing_tables'])}")
    print(f"  missing columns : {len(report['missing_columns'])}")
    print(f"  missing unique  : {len(report['missing_unique'])}")
    print(f"  missing indexes : {len(report['missing_indexes'])} (not fatal)")
    for entry in report["missing_indexes"][:10]:
        print(f"      {entry['table']}({', '.join(entry['columns'])})")
    if not report["healthy"]:
        print()
        print(describe(report))
        print()
        print("  RESULT: DRIFT -- the server will refuse to start unless")
        print("          FOXNEST_ALLOW_SCHEMA_DRIFT=1 is set.")
        return 1
    print()
    print("  RESULT: healthy -- the live schema matches the models.")
    return 0


def do_upgrade(url, snapshot):
    from app.db import upgrade

    try:
        report = upgrade.upgrade_database(url, snapshot=snapshot)
    except upgrade.MigrationError as exc:
        print(f"Upgrade failed: {exc}")
        return 2
    if not report["applied"]:
        print(f"  already at {report['to']}; nothing to do")
    return do_check(url)


def do_history():
    from alembic import command
    from app.db.upgrade import alembic_config

    command.history(alembic_config(), verbose=False)
    return 0


def do_stamp(url, revision):
    from alembic import command
    from app.db.upgrade import alembic_config

    print(f"Recording revision {revision} without running anything.")
    print("Use this only for a database you know is at that revision.")
    command.stamp(alembic_config(url or os.getenv("DATABASE_URL")), revision)
    print("  done")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Zanbeel schema tools")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", "--current", action="store_true", dest="status",
                       help="Show the database's revision and what is pending")
    group.add_argument("--check", action="store_true",
                       help="Compare the live schema against the models")
    group.add_argument("--upgrade", action="store_true",
                       help="Apply pending revisions, taking a copy of the database first")
    group.add_argument("--history", action="store_true", help="List revisions")
    group.add_argument("--stamp", metavar="REVISION",
                       help="Record REVISION as applied without running it")
    parser.add_argument("--url", help="Database URL (default: DATABASE_URL)")
    parser.add_argument("--no-snapshot", action="store_true",
                        help="With --upgrade: skip the copy taken before upgrading")
    args = parser.parse_args()

    try:
        if args.status:
            return do_status(args.url)
        if args.check:
            return do_check(args.url)
        if args.upgrade:
            return do_upgrade(args.url, snapshot=not args.no_snapshot)
        if args.history:
            return do_history()
        if args.stamp:
            return do_stamp(args.url, args.stamp)
    except Exception as exc:
        print(f"Error: {type(exc).__name__}: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
