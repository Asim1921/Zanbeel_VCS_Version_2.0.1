"""Security and operations: attempt kinds, escalating locks, backup jobs, upgrade history.

* ``login_attempts.kind`` -- password, otp or reset_request, so a password-reset code
  can be throttled without its guesses locking the account's password sign-in.
* ``account_locks.lock_count`` -- consecutive locks, each twice as long as the last.
* ``backup_records`` -- job status, checksums, the schema revision captured, blobs
  hard-linked from the previous backup, and the results of re-verification and of the
  restore drill.
* ``schema_migration_runs`` -- which upgrades ran, when, and where the copy taken
  before each one is.
* Indexes: every index the models declare that the database lacks (tables built by the
  old startup patches were written by hand and missed them), and the removal of
  hand-made indexes that duplicate a model index under another name.

Written to check before it changes anything, like every revision after 0002: on a
database upgraded from before Alembic, 0002's ``create_all`` may already have built
these columns.

Revision ID: 0003_security_and_operations
Revises: 0002_startup_patches
Create Date: 2026-10-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_security_and_operations"
down_revision: Union[str, None] = "0002_startup_patches"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _new_columns():
    return {
        "login_attempts": [
            sa.Column("kind", sa.String(20), nullable=False, server_default="password"),
        ],
        "account_locks": [
            sa.Column("lock_count", sa.Integer(), nullable=False, server_default="1"),
        ],
        "backup_records": [
            sa.Column("kind", sa.String(20), nullable=False, server_default="manual"),
            sa.Column("status", sa.String(20), nullable=False, server_default="complete"),
            sa.Column("format", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("database_sha256", sa.String(64), nullable=True),
            sa.Column("schema_revision", sa.String(64), nullable=True),
            sa.Column("linked_blobs", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("verify_status", sa.String(20), nullable=True),
            sa.Column("verified_at", sa.DateTime(), nullable=True),
            sa.Column("verify_report", sa.Text(), nullable=True),
            sa.Column("restore_test_status", sa.String(20), nullable=True),
            sa.Column("restore_tested_at", sa.DateTime(), nullable=True),
            sa.Column("restore_test_report", sa.Text(), nullable=True),
            sa.Column("pruned_at", sa.DateTime(), nullable=True),
        ],
    }


#: Hand-made index -> the model index that covers the same columns. The hand-made one
#: is dropped only once the model's exists, so no table is ever left without either.
_DUPLICATE_INDEXES = (
    ("commit_signatures", "ix_commit_signatures_repo", "ix_commit_signatures_repository_id"),
    ("pull_request_reviews", "ix_pull_request_reviews_pr", "ix_pull_request_reviews_pull_request_id"),
    ("access_tokens", "ix_access_tokens_hash", "ix_access_tokens_token_hash"),
    ("ssh_keys", "ix_ssh_keys_user", "ix_ssh_keys_user_id"),
    ("webhooks", "ix_webhooks_repo", "ix_webhooks_repository_id"),
    ("commit_statuses", "ix_commit_statuses_repo", "ix_commit_statuses_repository_id"),
    ("backup_records", "ix_backup_records_time", "ix_backup_records_created_at"),
)


def upgrade() -> None:
    from app.db import schema_ops
    from database.database import Base
    from database.models import SchemaMigrationRun

    connection = op.get_bind()

    for table, columns in _new_columns().items():
        for column in columns:
            schema_ops.add_column_if_missing(connection, table, column)

    if not schema_ops.has_table(connection, SchemaMigrationRun.__tablename__):
        SchemaMigrationRun.__table__.create(bind=connection)

    result = schema_ops.create_missing_model_indexes(connection, Base.metadata)
    for refused in result["refused"]:
        # Existing rows break a uniqueness rule. Leave the index out rather than fail
        # the upgrade; the schema check names it until the duplicates are resolved.
        print(f"[warn] unique index {refused['index']} on {refused['table']} not created: "
              f"{refused['reason']}")

    for table, legacy, model in _DUPLICATE_INDEXES:
        if model in schema_ops.index_names(connection, table):
            schema_ops.drop_index_if_exists(connection, table, legacy)


def downgrade() -> None:
    connection = op.get_bind()
    from app.db import schema_ops

    if schema_ops.has_table(connection, "schema_migration_runs"):
        op.drop_table("schema_migration_runs")
    for table, columns in _new_columns().items():
        present = schema_ops.column_names(connection, table)
        drop = [column.name for column in columns if column.name in present]
        if not drop:
            continue
        with op.batch_alter_table(table) as batch:
            for name in drop:
                batch.drop_column(name)
