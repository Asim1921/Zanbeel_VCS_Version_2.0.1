"""Baseline: the schema as it stood when Alembic was adopted.

Deliberately a no-op upgrade. The schema already exists on every deployment,
built by ``create_tables()`` and seventeen ``ensure_*`` startup patches. Emitting
CREATE TABLE here would either fail against a live database or, worse, be run
against a fresh one and diverge from the models.

So this revision exists to be *stamped*, not run:

    py -3.11 tools/schema.py --stamp     # existing database, already at baseline
    py -3.11 -m alembic upgrade head     # thereafter, applies real revisions

Every schema change from here on gets its own revision with real upgrade and
downgrade bodies. ``tools/schema.py --check`` compares the live database against
the models, so a revision that did not take effect is caught rather than
assumed.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-09
"""
from typing import Sequence, Union

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Intentionally empty. See the module docstring.
    pass


def downgrade() -> None:
    # There is nothing below the baseline to go back to.
    pass
