"""Baseline: the point at which Alembic was adopted (2.0.1).

Deliberately empty. Databases of every age exist -- some created before passwords,
some by today's models -- so there is no single schema this revision could create.
Revision 0002 does that work: it brings a database of any age to the models.

A database from before Alembic has no ``alembic_version`` row, so it is below this
revision and an upgrade runs 0001, 0002 and everything after. A database someone
stamped at 0001 by hand (``tools/schema.py --stamp`` in 2.0.1) is at this revision
and an upgrade runs 0002 onward. Both arrive at the same schema.

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
