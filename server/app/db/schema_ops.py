"""Idempotent building blocks for Alembic revisions.

Revision ``0002_startup_patches`` brings a database of any age to the models by running
``create_all`` and then the old startup patches. ``create_all`` creates missing tables
with today's columns, so a later revision that adds a column or creates a table must
not assume it is missing -- on a database upgraded from before Alembic it may already
be there. Every revision therefore checks before it changes anything, using these.

They take the revision's connection (``op.get_bind()``) so the checks see the
migration's own uncommitted changes.
"""

from typing import Iterable, List, Optional

from sqlalchemy import Column, Index, MetaData, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.schema import CreateColumn, CreateIndex


def has_table(connection, table: str) -> bool:
    return inspect(connection).has_table(table)


def column_names(connection, table: str) -> set:
    inspector = inspect(connection)
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def index_names(connection, table: str) -> set:
    inspector = inspect(connection)
    if not inspector.has_table(table):
        return set()
    names = {index["name"] for index in inspector.get_indexes(table) if index.get("name")}
    # A UNIQUE constraint is an index too, and is how a model's unique=True can appear.
    names |= {u["name"] for u in inspector.get_unique_constraints(table) if u.get("name")}
    return names


def add_column_if_missing(connection, table: str, column: Column) -> bool:
    """Add ``column`` to ``table`` unless it is there. Returns whether it was added.

    Uses the dialect's own DDL for the type and server default, so the column comes out
    the same as ``create_all`` would make it.
    """
    if not has_table(connection, table) or column.name in column_names(connection, table):
        return False
    if not column.nullable and column.server_default is None:
        raise ValueError(
            f"{table}.{column.name}: a NOT NULL column added to an existing table "
            "needs a server default for the rows already there"
        )
    ddl = str(CreateColumn(column).compile(dialect=connection.dialect))
    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
    return True


def drop_index_if_exists(connection, table: str, name: str) -> bool:
    if name not in index_names(connection, table):
        return False
    connection.execute(text(f"DROP INDEX {name}"))
    return True


def _declared_indexes(metadata: MetaData, tables: Optional[Iterable[str]] = None) -> List[Index]:
    """Every index the models declare: explicit Index objects and index=True columns."""
    wanted = set(tables) if tables else None
    found = []
    # By name rather than metadata.sorted_tables: index order does not depend on
    # foreign keys, and commits <-> repositories is a cycle that sorting warns about.
    for _name, table in sorted(metadata.tables.items()):
        if wanted is not None and table.name not in wanted:
            continue
        found.extend(sorted(table.indexes, key=lambda index: index.name or ""))
    return found


def create_missing_model_indexes(connection, metadata: MetaData) -> dict:
    """Create every index the models declare that the database lacks.

    Tables built by the old startup patches were written by hand, not by ``create_all``,
    so they lack the indexes the models ask for. A plain index can always be built. A
    unique one can fail on data that already breaks the rule; that one is skipped and
    reported rather than failing the whole upgrade, and the schema check then reports
    it as drift until the duplicates are resolved.

    Each attempt runs in a savepoint so a refused unique index leaves the rest of the
    migration's transaction intact.
    """
    created, refused = [], []
    for index in _declared_indexes(metadata):
        table = index.table.name
        if not has_table(connection, table):
            continue
        if index.name in index_names(connection, table):
            continue
        columns = column_names(connection, table)
        if any(col.name not in columns for col in index.columns):
            continue  # The schema check reports the missing column itself.
        savepoint = connection.begin_nested()
        try:
            connection.execute(CreateIndex(index))
            savepoint.commit()
            created.append(index.name)
        except (IntegrityError, OperationalError) as exc:
            savepoint.rollback()
            if not index.unique:
                raise
            refused.append({"index": index.name, "table": table,
                            "reason": str(getattr(exc, "orig", exc))[:200]})
    return {"created": created, "refused": refused}
