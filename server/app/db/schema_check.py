"""Verify the live schema actually matches the models.

The audit's complaint about the startup patches was precise: each
``ensure_*`` function wraps its ``ALTER TABLE`` in a try/except that prints a
warning and continues, so **a failed migration leaves the server running
against a schema it believes it patched**. Rows then fail at query time, deep
inside a request, with an error that names a column rather than the migration
that never ran.

Catching those exceptions better would not fix it. A patch can also fail to take
effect without raising at all — the wrong database, a partially applied
statement, a table someone dropped by hand. So this does not trust the patches:
it asks the database what it actually has and compares that against the models
SQLAlchemy is going to query.

Drift is fatal by default. A server that knows its schema is wrong should not
serve requests as though it were right; ``FOXNEST_ALLOW_SCHEMA_DRIFT=1`` exists
for the operator who has looked and decided to proceed anyway.
"""

import os
from typing import Any, Dict, List

from sqlalchemy import inspect

from database.database import Base, engine

# Importing the models is what registers them on Base.metadata. Without this the
# metadata is empty, every comparison finds nothing missing, and the check
# reports a healthy schema no matter how broken the database is.
import database.models  # noqa: F401


class SchemaDrift(RuntimeError):
    """The live schema is missing something the models require."""


def _allow_drift() -> bool:
    return os.getenv("FOXNEST_ALLOW_SCHEMA_DRIFT", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def inspect_schema() -> Dict[str, Any]:
    """Compare declared models against the live database.

    Reports only what would actually break a query: tables the models expect and
    the database lacks, and columns within those tables. Extra tables and extra
    columns are left alone — they are usually a rolled-back feature or another
    application sharing the database, and neither stops this one working.
    """
    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())

    missing_tables: List[str] = []
    missing_columns: List[Dict[str, Any]] = []

    for table_name, table in sorted(Base.metadata.tables.items()):
        if table_name not in live_tables:
            missing_tables.append(table_name)
            continue

        live_columns = {c["name"] for c in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name not in live_columns:
                missing_columns.append(
                    {
                        "table": table_name,
                        "column": column.name,
                        "type": str(column.type),
                        "nullable": bool(column.nullable),
                    }
                )

    return {
        "tables_expected": len(Base.metadata.tables),
        "tables_present": len(live_tables & set(Base.metadata.tables)),
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "healthy": not missing_tables and not missing_columns,
        "drift_allowed": _allow_drift(),
    }


def describe(report: Dict[str, Any]) -> str:
    """A message an operator can act on without reading the code."""
    lines = []
    if report["missing_tables"]:
        lines.append("  missing tables: " + ", ".join(report["missing_tables"]))
    for entry in report["missing_columns"][:25]:
        lines.append(f"  missing column: {entry['table']}.{entry['column']} ({entry['type']})")
    remaining = len(report["missing_columns"]) - 25
    if remaining > 0:
        lines.append(f"  ... and {remaining} more column(s)")
    return "\n".join(lines)


def verify_or_fail() -> Dict[str, Any]:
    """Check the schema at startup. Raises SchemaDrift unless drift is allowed."""
    report = inspect_schema()

    if report["healthy"]:
        print(
            f"[ok] Schema verified: {report['tables_present']}/{report['tables_expected']} "
            "tables present, no missing columns"
        )
        return report

    print("=" * 60)
    print("SCHEMA DRIFT DETECTED")
    print("The database does not match the models this server will query.")
    print(describe(report))
    print("")
    print("A migration did not take effect. Queries against the objects above")
    print("will fail at request time.")
    print("")
    print("  Inspect : py -3.11 tools/schema.py --check")
    print("  Override: set FOXNEST_ALLOW_SCHEMA_DRIFT=1 to start anyway")
    print("=" * 60)

    if not _allow_drift():
        raise SchemaDrift(
            f"{len(report['missing_tables'])} missing table(s) and "
            f"{len(report['missing_columns'])} missing column(s). "
            "Refusing to start against a schema the server cannot serve."
        )

    print("[warn] FOXNEST_ALLOW_SCHEMA_DRIFT is set; continuing despite drift")
    return report
