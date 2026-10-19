"""Startup patches: everything the server used to patch in place at every start.

Until this revision the server ran ``create_tables()`` and twenty-five ``ensure_*``
functions on every start, each swallowing its own errors. This runs the same work once,
in the same order, on the migration's transaction, with errors left to propagate:

1. ``create_all`` creates every table the models declare and the database lacks,
   with today's columns. It never alters a table that exists.
2. The patch chain (``app/db/migrations.LEGACY_PATCHES``) adds the columns and
   indexes that tables created by older versions are missing, and rebuilds the one
   table whose constraint changed.

Every step checks before it changes anything, so this is correct for a new database,
one from before passwords existed, and one that had every patch already.

Revision ID: 0002_startup_patches
Revises: 0001_baseline
Create Date: 2026-10-14
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002_startup_patches"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from database.database import Base
    import database.models  # noqa: F401  (registers every model)
    from app.db.migrations import apply_legacy_patches

    connection = op.get_bind()
    Base.metadata.create_all(bind=connection)
    apply_legacy_patches(connection)


def downgrade() -> None:
    raise NotImplementedError(
        "0002 consolidates patches applied to databases of many ages; there is no single "
        "earlier schema to return to. Restore the pre-upgrade copy instead "
        "(tools/schema.py --status shows where it is)."
    )
