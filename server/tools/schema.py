"""Inspect and manage the database schema.

    py -3.11 tools/schema.py --check     # does the live schema match the models?
    py -3.11 tools/schema.py --stamp     # mark an existing database at baseline
    py -3.11 tools/schema.py --current   # which revision is applied
    py -3.11 tools/schema.py --history   # revisions available

``--check`` is the one that matters day to day. The startup patches swallow
their own errors, so "no error appeared" has never meant "the migration
applied". This asks the database what it actually has.
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

from app.db.schema_check import describe, inspect_schema  # noqa: E402


def do_check():
    report = inspect_schema()
    print(f"  tables expected : {report['tables_expected']}")
    print(f"  tables present  : {report['tables_present']}")
    print(f"  missing tables  : {len(report['missing_tables'])}")
    print(f"  missing columns : {len(report['missing_columns'])}")
    if not report["healthy"]:
        print()
        print(describe(report))
        print()
        print("  RESULT: DRIFT — the server will refuse to start unless")
        print("          FOXNEST_ALLOW_SCHEMA_DRIFT=1 is set.")
        return 1
    print()
    print("  RESULT: healthy — the live schema matches the models.")
    return 0


def _alembic_config():
    from alembic.config import Config

    config = Config(str(SERVER_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(SERVER_ROOT / "alembic"))
    return config


def do_stamp():
    from alembic import command

    print("Stamping the existing database at the baseline revision.")
    print("This records that the schema is already current; it runs no DDL.")
    command.stamp(_alembic_config(), "head")
    print("  done")
    return 0


def do_current():
    from alembic import command

    command.current(_alembic_config(), verbose=True)
    return 0


def do_history():
    from alembic import command

    command.history(_alembic_config(), verbose=False)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Zanbeel schema tools")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true",
                       help="Compare the live schema against the models")
    group.add_argument("--stamp", action="store_true",
                       help="Record an existing database as being at the baseline")
    group.add_argument("--current", action="store_true", help="Show the applied revision")
    group.add_argument("--history", action="store_true", help="List revisions")
    args = parser.parse_args()

    if args.check:
        return do_check()
    if args.stamp:
        return do_stamp()
    if args.current:
        return do_current()
    if args.history:
        return do_history()
    return 0


if __name__ == "__main__":
    sys.exit(main())
