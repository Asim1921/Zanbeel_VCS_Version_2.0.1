"""Verify the live schema actually matches the models, and is at this code's revision.

The startup patches used to wrap their ``ALTER TABLE`` in a try/except that printed a
warning and continued, so a failed migration left the server running against a schema
it believed it had patched. Migrations are now recorded revisions that fail loudly
(``app/db/upgrade.py``), but "the upgrade succeeded" is still not the same as "the
schema is right": the wrong database, a table someone dropped by hand, data that kept
a unique index from being built. So this does not trust the migrations either. It asks
the database what it has and compares that with what the models will query.

Fatal, because a query would fail or a rule the code relies on would not hold:

* a table or column the models declare is missing;
* a unique index or constraint the models declare is missing (it is what keeps a
  release name or an access-token hash from being used twice);
* the database is behind this code's revision (migrations pending) or at a revision
  this code does not know (upgraded by a newer server).

Reported but not fatal: a missing plain index (slower, not wrong). Ignored: extra
tables, columns and indexes, and on SQLite the type and nullability differences its
type affinity makes meaningless.

``FOXNEST_ALLOW_SCHEMA_DRIFT=1`` is for the operator who has looked and decided to start
anyway.
"""

import os
from typing import Any, Dict, List, Optional

from sqlalchemy import UniqueConstraint, inspect

from database.database import Base, engine as app_engine

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


def _revision_report(engine) -> Dict[str, Any]:
    from app.db import upgrade

    try:
        return upgrade.status(str(engine.url.render_as_string(hide_password=False)))
    except Exception as exc:  # A broken alembic setup is itself drift worth reporting.
        return {"error": f"{type(exc).__name__}: {exc}", "pending": [], "unknown": []}


def inspect_schema(engine=None) -> Dict[str, Any]:
    """Compare the declared models against a live database (the application's by default)."""
    engine = engine or app_engine
    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())

    missing_tables: List[str] = []
    missing_columns: List[Dict[str, Any]] = []
    missing_unique: List[Dict[str, Any]] = []
    missing_indexes: List[Dict[str, Any]] = []

    for table_name, table in sorted(Base.metadata.tables.items()):
        if table_name not in live_tables:
            missing_tables.append(table_name)
            continue

        live_columns = {c["name"] for c in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name not in live_columns:
                missing_columns.append({
                    "table": table_name,
                    "column": column.name,
                    "type": str(column.type),
                    "nullable": bool(column.nullable),
                })

        # Compared by column set, not name: a rule the old patches wrote inline (an
        # unnamed UNIQUE, a differently named index) still enforces the same thing.
        live_index_rows = inspector.get_indexes(table_name)
        live_unique = [frozenset(i["column_names"]) for i in live_index_rows if i.get("unique")]
        live_unique += [frozenset(u["column_names"])
                        for u in inspector.get_unique_constraints(table_name)]
        primary = inspector.get_pk_constraint(table_name).get("constrained_columns") or []
        if primary:
            live_unique.append(frozenset(primary))
        live_any = [frozenset(i["column_names"]) for i in live_index_rows] + live_unique
        live_names = {i["name"] for i in live_index_rows}

        declared_unique = [(index.name, frozenset(c.name for c in index.columns))
                           for index in table.indexes if index.unique]
        declared_unique += [(constraint.name, frozenset(c.name for c in constraint.columns))
                            for constraint in table.constraints
                            if isinstance(constraint, UniqueConstraint)]
        for name, columns in declared_unique:
            if columns <= live_columns and columns not in live_unique:
                missing_unique.append({"table": table_name, "index": name,
                                       "columns": sorted(columns)})
        for index in table.indexes:
            if index.unique:
                continue
            columns = frozenset(c.name for c in index.columns)
            if index.name not in live_names and columns not in live_any and columns <= live_columns:
                missing_indexes.append({"table": table_name, "index": index.name,
                                        "columns": sorted(columns)})

    revision = _revision_report(engine)
    revision_problem = None
    if revision.get("error"):
        revision_problem = f"The migration history could not be read: {revision['error']}"
    elif revision.get("unknown"):
        revision_problem = (
            f"The database is at revision {revision['unknown'][0]}, which this server does not "
            "have: it was upgraded by a newer version."
        )
    elif revision.get("pending"):
        revision_problem = (
            f"{len(revision['pending'])} migration(s) not applied: {', '.join(revision['pending'])}."
        )

    healthy = not (missing_tables or missing_columns or missing_unique or revision_problem)
    return {
        "tables_expected": len(Base.metadata.tables),
        "tables_present": len(live_tables & set(Base.metadata.tables)),
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_unique": missing_unique,
        "missing_indexes": missing_indexes,
        "revision": revision,
        "revision_problem": revision_problem,
        "healthy": healthy,
        "drift_allowed": _allow_drift(),
    }


def describe(report: Dict[str, Any]) -> str:
    """A message an operator can act on without reading the code."""
    lines = []
    if report.get("revision_problem"):
        lines.append("  " + report["revision_problem"])
    if report["missing_tables"]:
        lines.append("  missing tables: " + ", ".join(report["missing_tables"]))
    for entry in report["missing_columns"][:25]:
        lines.append(f"  missing column: {entry['table']}.{entry['column']} ({entry['type']})")
    remaining = len(report["missing_columns"]) - 25
    if remaining > 0:
        lines.append(f"  ... and {remaining} more column(s)")
    for entry in report.get("missing_unique", [])[:25]:
        lines.append(f"  missing unique rule: {entry['table']}({', '.join(entry['columns'])})"
                     + (f" [{entry['index']}]" if entry.get("index") else ""))
    return "\n".join(lines)


def verify_or_fail(engine=None) -> Dict[str, Any]:
    """Check the schema at startup. Raises SchemaDrift unless drift is allowed."""
    report = inspect_schema(engine)

    if report["healthy"]:
        revision = report["revision"].get("current") or "?"
        note = (f"; {len(report['missing_indexes'])} index(es) the models declare are missing"
                if report["missing_indexes"] else "")
        print(f"[ok] Schema verified at {revision}: {report['tables_present']}/"
              f"{report['tables_expected']} tables, no missing columns or unique rules{note}")
        return report

    print("=" * 60)
    print("SCHEMA DRIFT DETECTED")
    print("The database does not match what this server will query.")
    print(describe(report))
    print("")
    if report["revision"].get("pending") and not report["revision"].get("auto_migrate"):
        print("Startup migration is off (FOXNEST_AUTO_MIGRATE=0). Apply them with:")
        print("  ..\\venv\\Scripts\\python.exe tools/schema.py --upgrade")
    else:
        print("  Inspect : ..\\venv\\Scripts\\python.exe tools/schema.py --check")
    print("  Override: set FOXNEST_ALLOW_SCHEMA_DRIFT=1 to start anyway")
    print("=" * 60)

    if not _allow_drift():
        raise SchemaDrift(
            f"{len(report['missing_tables'])} missing table(s), "
            f"{len(report['missing_columns'])} missing column(s), "
            f"{len(report.get('missing_unique', []))} missing unique rule(s)"
            + (f"; {report['revision_problem']}" if report.get("revision_problem") else "")
            + ". Refusing to start against a schema the server cannot serve."
        )

    print("[warn] FOXNEST_ALLOW_SCHEMA_DRIFT is set; continuing despite drift")
    return report
