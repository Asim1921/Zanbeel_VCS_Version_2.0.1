"""The schema patches that used to run at every startup, now one recorded migration.

Each ``ensure_*`` function brings one part of an older database up to date and does
nothing when that part is already current, so the whole chain is safe to run against a
database of any age, including an empty one. They used to run on every start, each
wrapped in a try/except that printed a warning and carried on -- a failed patch left
the server running against a schema it believed it had applied.

Now they run once, in order, inside revision ``0002_startup_patches``, on the
migration's own connection and transaction (see ``app/db/upgrade.py``). Errors are not
caught: a patch that fails rolls the whole upgrade back and the server refuses to
start, naming the statement that failed.

Every schema change after these belongs in its own Alembic revision under
``alembic/versions/``. Do not add another function here.

Each function still works on its own (``ensure_x()`` opens a transaction on the
application engine), because tests and the end-to-end harnesses call them directly to
build databases of a particular age.
"""

from contextlib import contextmanager

from sqlalchemy import inspect, text

from database.database import engine


@contextmanager
def _connection(connection=None):
    """The caller's connection, or a transaction of our own on the application engine."""
    if connection is not None:
        yield connection
        return
    with engine.begin() as conn:
        yield conn


def _columns(connection, table):
    """Column names of ``table``, or an empty set when the table does not exist."""
    inspector = inspect(connection)
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _add_missing_columns(connection, table, columns):
    """ALTER TABLE ... ADD COLUMN for each (name, ddl_type) the table lacks.

    A table that does not exist is left alone: the models create it, columns included.
    """
    existing = _columns(connection, table)
    if not existing:
        return
    for name, ddl_type in columns:
        if name not in existing:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))


def ensure_users_password_hash_column(connection=None):
    """Add users.password_hash for pre-auth databases."""
    with _connection(connection) as conn:
        _add_missing_columns(conn, "users", (("password_hash", "VARCHAR(255)"),))


def ensure_users_last_login_at_column(connection=None):
    """Add users.last_login_at for engagement / activity monitoring."""
    with _connection(connection) as conn:
        _add_missing_columns(conn, "users", (("last_login_at", "DATETIME"),))


def ensure_user_permissions_show_in_web_ui_column(connection=None):
    """Add user_permissions.show_in_web_ui for web vs CLI-oriented grants."""
    with _connection(connection) as conn:
        # SQLite stores BOOLEAN as INTEGER; default 1 = visible (legacy behavior).
        _add_missing_columns(conn, "user_permissions", (("show_in_web_ui", "BOOLEAN DEFAULT 1"),))


def ensure_user_permissions_issue_id_column(connection=None):
    """Add user_permissions.issue_id for issue-scoped (temporary) grants."""
    with _connection(connection) as conn:
        _add_missing_columns(conn, "user_permissions",
                             (("issue_id", "INTEGER REFERENCES issues(id)"),))


def ensure_versioning_schema(connection=None):
    """Create lineage table and critical indexes when upgrading older deployments."""
    with _connection(connection) as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS file_lineage (
                id INTEGER PRIMARY KEY,
                repository_id VARCHAR(16) NOT NULL,
                commit_id VARCHAR(40) NOT NULL,
                old_path VARCHAR(500) NOT NULL,
                new_path VARCHAR(500) NOT NULL,
                file_hash VARCHAR(40) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        for stmt in [
            "CREATE INDEX IF NOT EXISTS ix_commits_repo_created ON commits(repository_id, created_at, id)",
            "CREATE INDEX IF NOT EXISTS ix_commit_files_commit_path ON commit_files(commit_id, file_path)",
            "CREATE INDEX IF NOT EXISTS ix_commit_files_path ON commit_files(file_path)",
            "CREATE INDEX IF NOT EXISTS ix_branches_repo_name ON branches(repository_id, name)",
            "CREATE INDEX IF NOT EXISTS ix_file_lineage_repo_old ON file_lineage(repository_id, old_path)",
            "CREATE INDEX IF NOT EXISTS ix_file_lineage_repo_new ON file_lineage(repository_id, new_path)",
            "CREATE INDEX IF NOT EXISTS ix_file_lineage_repo_commit ON file_lineage(repository_id, commit_id)",
        ]:
            conn.execute(text(stmt))


def ensure_commit_signatures_table(connection=None):
    """Create the commit attestation table for deployments upgrading in place."""
    with _connection(connection) as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS commit_signatures (
                commit_id VARCHAR(40) NOT NULL PRIMARY KEY,
                repository_id VARCHAR(16) NOT NULL,
                pusher_username VARCHAR(50) NOT NULL,
                pusher_id INTEGER,
                algorithm VARCHAR(32) NOT NULL,
                signature VARCHAR(128) NOT NULL,
                signed_at_text VARCHAR(40) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tree_digest_at_signing VARCHAR(64),
                author_at_signing VARCHAR(50),
                message_digest_at_signing VARCHAR(64)
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_commit_signatures_repo "
            "ON commit_signatures(repository_id)"
        ))


def ensure_branch_protection_tables(connection=None):
    """Branch protection: the generation counter plus the policy, unlock, release and audit tables."""
    with _connection(connection) as conn:
        # Compare-and-swap counter. Existing branches start at 0.
        _add_missing_columns(conn, "branches", (("generation", "INTEGER NOT NULL DEFAULT 0"),))

        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS branch_protection_policies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repository_id VARCHAR(16) NOT NULL,
                branch_pattern VARCHAR(200) NOT NULL,
                mode VARCHAR(20) NOT NULL DEFAULT 'open',
                policy_version INTEGER NOT NULL DEFAULT 1,
                rules_json TEXT,
                created_by_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_by_id INTEGER,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_branch_policies_repo_pattern "
            "ON branch_protection_policies(repository_id, branch_pattern)"
        ))

        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS branch_unlocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repository_id VARCHAR(16) NOT NULL,
                branch_name VARCHAR(200) NOT NULL,
                operations VARCHAR(200) NOT NULL,
                reason TEXT NOT NULL,
                requested_by_id INTEGER NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                consumed_at TIMESTAMP,
                consumed_by_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_branch_unlocks_repo_branch "
            "ON branch_unlocks(repository_id, branch_name)"
        ))

        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS immutable_releases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repository_id VARCHAR(16) NOT NULL,
                name VARCHAR(200) NOT NULL,
                commit_id VARCHAR(40) NOT NULL,
                manifest_json TEXT,
                signature VARCHAR(128),
                created_by_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        # The uniqueness is the immutability: a name can never be reused.
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_immutable_releases_repo_name "
            "ON immutable_releases(repository_id, name)"
        ))

        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS ref_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repository_id VARCHAR(16) NOT NULL,
                event_type VARCHAR(60) NOT NULL,
                reference VARCHAR(200) NOT NULL,
                operation VARCHAR(30),
                actor_id INTEGER,
                actor_username VARCHAR(50),
                old_commit_id VARCHAR(40),
                new_commit_id VARCHAR(40),
                old_generation INTEGER,
                new_generation INTEGER,
                policy_id INTEGER,
                policy_version INTEGER,
                mode VARCHAR(20),
                decision VARCHAR(20) NOT NULL,
                error_code VARCHAR(40),
                reason TEXT,
                occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                previous_event_hash VARCHAR(64),
                event_hash VARCHAR(64) NOT NULL
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_ref_audit_repo_ref "
            "ON ref_audit_events(repository_id, reference)"
        ))


def ensure_password_reset_otp_table(connection=None):
    """Create the password-reset one-time-code table for deployments upgrading in place."""
    with _connection(connection) as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS password_reset_otps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username VARCHAR(50) NOT NULL,
                email VARCHAR(100) NOT NULL,
                code_hash VARCHAR(64) NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                attempts INTEGER DEFAULT 0,
                consumed_at TIMESTAMP,
                requested_by_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_password_reset_otps_username "
            "ON password_reset_otps(username)"
        ))


def ensure_merge_conflict_tables(connection=None):
    """Create the merge-conflict resolution tables where they do not already exist.

    Deployed databases already carry these (a previous developer added the schema but
    never wired anything to it); fresh ones get them from the models. This covers the
    gap for databases created in between.
    """
    with _connection(connection) as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS merge_conflict_sessions (
                id INTEGER PRIMARY KEY,
                repository_id VARCHAR(16) NOT NULL,
                pull_request_id INTEGER NOT NULL,
                source_branch VARCHAR(100) NOT NULL,
                target_branch VARCHAR(100) NOT NULL,
                base_commit_id VARCHAR(40) NOT NULL,
                source_head_commit_id VARCHAR(40) NOT NULL,
                target_head_commit_id VARCHAR(40) NOT NULL,
                status VARCHAR(20),
                created_by_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS merge_conflict_files (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL,
                file_path VARCHAR(500) NOT NULL,
                base_file_hash VARCHAR(40),
                ours_file_hash VARCHAR(40),
                theirs_file_hash VARCHAR(40),
                resolved_file_hash VARCHAR(40),
                is_binary BOOLEAN,
                CONSTRAINT uq_merge_conflict_files_session_path UNIQUE (session_id, file_path)
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_merge_conflict_sessions_pr "
            "ON merge_conflict_sessions(pull_request_id, status)"
        ))


def ensure_merge_conflict_branch_sessions(connection=None):
    """Let a conflict session exist without a pull request.

    Sessions were modelled as belonging to a pull request, so a conflicted `fox merge`
    had nowhere to park its three sides: the merge reported the conflict and stopped,
    which is the state this feature exists to remove. Branch merges need the same
    resolution surface, so pull_request_id becomes optional.

    SQLite cannot drop a NOT NULL constraint in place, so the table is rebuilt and the
    rows copied. Existing sessions keep their pull request.
    """
    with _connection(connection) as conn:
        inspector = inspect(conn)
        if not inspector.has_table("merge_conflict_sessions"):
            return  # The models will create it correctly.
        pr_column = next(
            (c for c in inspector.get_columns("merge_conflict_sessions")
             if c["name"] == "pull_request_id"),
            None,
        )
        if pr_column is None or pr_column["nullable"]:
            return  # Already nullable, or the column is gone.

        conn.execute(text(
            """
            CREATE TABLE merge_conflict_sessions_rebuilt (
                id INTEGER PRIMARY KEY,
                repository_id VARCHAR(16) NOT NULL,
                pull_request_id INTEGER,
                source_branch VARCHAR(100) NOT NULL,
                target_branch VARCHAR(100) NOT NULL,
                base_commit_id VARCHAR(40),
                source_head_commit_id VARCHAR(40) NOT NULL,
                target_head_commit_id VARCHAR(40) NOT NULL,
                status VARCHAR(20),
                created_by_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            """
            INSERT INTO merge_conflict_sessions_rebuilt (
                id, repository_id, pull_request_id, source_branch, target_branch,
                base_commit_id, source_head_commit_id, target_head_commit_id,
                status, created_by_id, created_at, updated_at
            )
            SELECT id, repository_id, pull_request_id, source_branch, target_branch,
                   base_commit_id, source_head_commit_id, target_head_commit_id,
                   status, created_by_id, created_at, updated_at
            FROM merge_conflict_sessions
            """
        ))
        conn.execute(text("DROP TABLE merge_conflict_sessions"))
        conn.execute(text(
            "ALTER TABLE merge_conflict_sessions_rebuilt "
            "RENAME TO merge_conflict_sessions"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_merge_conflict_sessions_pr "
            "ON merge_conflict_sessions(pull_request_id, status)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_merge_conflict_sessions_branches "
            "ON merge_conflict_sessions(repository_id, target_branch, status)"
        ))


def ensure_merge_conflict_resolution_columns(connection=None):
    """Columns for fork-aware sessions, saved drafts and conflict kinds.

    Must run after ensure_merge_conflict_branch_sessions(): that rebuild copies a fixed
    column list, so anything added before it would be dropped again.
    """
    with _connection(connection) as conn:
        _add_missing_columns(conn, "merge_conflict_sessions", (
            ("source_repository_id", "VARCHAR(16)"),
            ("result_commit_id", "VARCHAR(40)"),
            ("resolved_by_id", "INTEGER"),
            ("resolved_at", "TIMESTAMP"),
        ))
        _add_missing_columns(conn, "merge_conflict_files", (
            ("conflict_kind", "VARCHAR(20)"),
            ("resolution", "VARCHAR(10)"),
            ("resolved_by_id", "INTEGER"),
            ("resolved_at", "TIMESTAMP"),
        ))
        if _columns(conn, "merge_conflict_files"):
            # Sessions resolved before drafts existed hold a resolved_file_hash but no
            # resolution; give them one so "resolved" means the same thing everywhere.
            conn.execute(text(
                "UPDATE merge_conflict_files SET resolution = 'content' "
                "WHERE resolution IS NULL AND resolved_file_hash IS NOT NULL"
            ))


def ensure_user_profile_columns(connection=None):
    """Profile fields on users, added where they do not exist yet."""
    with _connection(connection) as conn:
        _add_missing_columns(conn, "users", (
            ("avatar_blob_hash", "VARCHAR(64)"),
            ("avatar_content_type", "VARCHAR(64)"),
            ("bio", "TEXT"),
            ("location", "VARCHAR(100)"),
            ("website", "VARCHAR(200)"),
        ))


def ensure_repositories_branch_policy_column(connection=None):
    """Add repositories.branch_policy_json for branch protection rules.

    `branch_policies.get_branch_policy` has always read this column defensively via
    getattr, so it returned the defaults for every repository -- the column was never
    created. This adds it; a NULL still means "use the defaults".
    """
    with _connection(connection) as conn:
        _add_missing_columns(conn, "repositories", (("branch_policy_json", "TEXT"),))


def ensure_pull_request_reviews_table(connection=None):
    """Create the pull request review table for deployments upgrading in place."""
    with _connection(connection) as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS pull_request_reviews (
                id INTEGER PRIMARY KEY,
                pull_request_id INTEGER NOT NULL,
                reviewer_id INTEGER NOT NULL,
                state VARCHAR(20) NOT NULL,
                body TEXT,
                commit_id VARCHAR(40),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pull_request_reviews_pr "
            "ON pull_request_reviews(pull_request_id)"
        ))


def ensure_pull_request_comments_table(connection=None):
    """Create the inline diff comment table for deployments upgrading in place."""
    with _connection(connection) as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS pull_request_comments (
                id INTEGER PRIMARY KEY,
                pull_request_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                file_path VARCHAR(500) NOT NULL,
                line INTEGER NOT NULL,
                side VARCHAR(8) NOT NULL DEFAULT 'new',
                body TEXT NOT NULL,
                commit_id VARCHAR(40),
                in_reply_to_id INTEGER,
                resolved BOOLEAN DEFAULT 0,
                resolved_by_id INTEGER,
                resolved_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pr_comments_pr_path "
            "ON pull_request_comments(pull_request_id, file_path)"
        ))


def ensure_fork_columns(connection=None):
    """Add the fork relationship columns for deployments upgrading in place."""
    with _connection(connection) as conn:
        _add_missing_columns(conn, "repositories", (("forked_from_id", "VARCHAR(16)"),))
        _add_missing_columns(conn, "pull_requests", (("source_repository_id", "VARCHAR(16)"),))


def ensure_access_tokens_table(connection=None):
    """Create the personal access token table for deployments upgrading in place."""
    with _connection(connection) as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS access_tokens (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name VARCHAR(100) NOT NULL,
                prefix VARCHAR(16) NOT NULL,
                token_hash VARCHAR(64) NOT NULL UNIQUE,
                scopes VARCHAR(200) NOT NULL DEFAULT 'repo:read',
                expires_at TIMESTAMP,
                last_used_at TIMESTAMP,
                revoked_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_access_tokens_user_active "
            "ON access_tokens(user_id, revoked_at)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_access_tokens_hash "
            "ON access_tokens(token_hash)"
        ))


def ensure_ssh_key_tables(connection=None):
    """Create the SSH public key and challenge tables."""
    with _connection(connection) as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS ssh_keys (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title VARCHAR(100) NOT NULL,
                key_type VARCHAR(30) NOT NULL,
                public_key TEXT NOT NULL,
                fingerprint VARCHAR(80) NOT NULL UNIQUE,
                last_used_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_ssh_keys_user ON ssh_keys(user_id)"
        ))
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS ssh_challenges (
                id INTEGER PRIMARY KEY,
                nonce VARCHAR(64) NOT NULL UNIQUE,
                username VARCHAR(50) NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))


def ensure_credential_columns(connection=None):
    """Scopes on SSH keys, and the link from a sign-in token to the key that issued it.

    Must run after ensure_access_tokens_table() and ensure_ssh_key_tables(). Keys that
    predate scopes get repo:write -- what a developer's own key is for -- rather than
    the unscoped session they used to yield.
    """
    with _connection(connection) as conn:
        _add_missing_columns(conn, "ssh_keys",
                             (("scopes", "VARCHAR(200) NOT NULL DEFAULT 'repo:write'"),))
        _add_missing_columns(conn, "access_tokens", (("ssh_key_id", "INTEGER"),))
        if _columns(conn, "access_tokens"):
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_access_tokens_ssh_key_id "
                "ON access_tokens(ssh_key_id)"
            ))


def ensure_webhook_tables(connection=None):
    """Create the webhook and webhook delivery tables."""
    with _connection(connection) as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS webhooks (
                id INTEGER PRIMARY KEY,
                repository_id VARCHAR(16),
                url VARCHAR(500) NOT NULL,
                secret VARCHAR(128),
                events VARCHAR(300) NOT NULL DEFAULT '*',
                content_type VARCHAR(50) NOT NULL DEFAULT 'application/json',
                active BOOLEAN DEFAULT 1,
                created_by_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_webhooks_repo ON webhooks(repository_id)"
        ))
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                id INTEGER PRIMARY KEY,
                webhook_id INTEGER NOT NULL,
                event VARCHAR(60) NOT NULL,
                payload TEXT NOT NULL,
                status_code INTEGER,
                response_body TEXT,
                error TEXT,
                duration_ms INTEGER,
                success BOOLEAN DEFAULT 0,
                attempt INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_webhook_deliveries_hook_time "
            "ON webhook_deliveries(webhook_id, created_at)"
        ))


def ensure_commit_statuses_table(connection=None):
    """Create the commit status check table."""
    with _connection(connection) as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS commit_statuses (
                id INTEGER PRIMARY KEY,
                repository_id VARCHAR(16) NOT NULL,
                commit_id VARCHAR(40) NOT NULL,
                context VARCHAR(120) NOT NULL,
                state VARCHAR(20) NOT NULL,
                description VARCHAR(300),
                target_url VARCHAR(500),
                created_by_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_commit_statuses_commit_context "
            "ON commit_statuses(commit_id, context)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_commit_statuses_repo "
            "ON commit_statuses(repository_id)"
        ))


def ensure_server_hooks_table(connection=None):
    """Create the server-side hook policy table."""
    with _connection(connection) as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS server_hooks (
                id INTEGER PRIMARY KEY,
                repository_id VARCHAR(16),
                name VARCHAR(100) NOT NULL,
                hook_type VARCHAR(20) NOT NULL DEFAULT 'pre-receive',
                enabled BOOLEAN DEFAULT 1,
                config_json TEXT NOT NULL DEFAULT '{}',
                created_by_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_server_hooks_repo_type "
            "ON server_hooks(repository_id, hook_type)"
        ))


def ensure_integration_columns(connection=None):
    """Delivery ids on the webhook log, and the commit-status index the gate reads by.

    Must run after ensure_webhook_tables() and ensure_commit_statuses_table(). Rows
    logged before delivery ids existed keep a NULL id and show as single attempts.
    """
    with _connection(connection) as conn:
        _add_missing_columns(conn, "webhook_deliveries", (
            ("delivery_guid", "VARCHAR(40)"),
            ("redelivery_of", "VARCHAR(40)"),
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_webhook_deliveries_delivery_guid "
            "ON webhook_deliveries(delivery_guid)"
        ))
        # Statuses are now read per repository and commit, never by commit alone.
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_commit_statuses_repo_commit "
            "ON commit_statuses(repository_id, commit_id)"
        ))


def ensure_activity_columns(connection=None):
    """Structured audit fields on the activity log, and the indexes it is filtered by.

    Rows written before this keep NULL in the new columns and show as free text.
    """
    with _connection(connection) as conn:
        if not _columns(conn, "activities"):
            return
        _add_missing_columns(conn, "activities", (
            ("target_type", "VARCHAR(30)"),
            ("target_id", "VARCHAR(200)"),
            ("commit_id", "VARCHAR(40)"),
            ("payload_json", "TEXT"),
            ("credential", "VARCHAR(120)"),
            ("client_ip", "VARCHAR(64)"),
            ("repository_name", "VARCHAR(200)"),
            ("actor_username", "VARCHAR(50)"),
        ))
        for name, cols in (
            ("ix_activities_created_at", "created_at"),
            ("ix_activities_repository_time", "repository_id, created_at"),
            ("ix_activities_user_time", "user_id, created_at"),
            ("ix_activities_type_time", "activity_type, created_at"),
        ):
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON activities({cols})"))


def ensure_security_tables(connection=None):
    """Create the login-attempt, lockout and backup tables."""
    with _connection(connection) as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY,
                username VARCHAR(150) NOT NULL,
                ip_address VARCHAR(64),
                success BOOLEAN NOT NULL DEFAULT 0,
                user_agent VARCHAR(300),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_login_attempts_user_time "
            "ON login_attempts(username, created_at)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_login_attempts_ip_time "
            "ON login_attempts(ip_address, created_at)"
        ))
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS account_locks (
                id INTEGER PRIMARY KEY,
                username VARCHAR(150) NOT NULL UNIQUE,
                locked_until TIMESTAMP NOT NULL,
                failed_count INTEGER DEFAULT 0,
                reason VARCHAR(200),
                last_ip VARCHAR(64),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS backup_records (
                id INTEGER PRIMARY KEY,
                label VARCHAR(120) NOT NULL,
                path VARCHAR(500) NOT NULL,
                database_bytes BIGINT DEFAULT 0,
                blob_bytes BIGINT DEFAULT 0,
                blob_count INTEGER DEFAULT 0,
                verified BOOLEAN DEFAULT 0,
                missing_blobs INTEGER DEFAULT 0,
                duration_ms INTEGER,
                error TEXT,
                created_by_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_backup_records_time "
            "ON backup_records(created_at)"
        ))


#: The chain, in the order startup used to run it. Order matters where noted in the
#: docstrings: the merge-conflict rebuild before its columns, the token and key tables
#: before their columns, the webhook and status tables before the integration columns.
LEGACY_PATCHES = (
    ensure_users_password_hash_column,
    ensure_users_last_login_at_column,
    ensure_user_profile_columns,
    ensure_user_permissions_show_in_web_ui_column,
    ensure_user_permissions_issue_id_column,
    ensure_versioning_schema,
    ensure_commit_signatures_table,
    ensure_password_reset_otp_table,
    ensure_branch_protection_tables,
    ensure_merge_conflict_tables,
    ensure_merge_conflict_branch_sessions,
    ensure_merge_conflict_resolution_columns,
    ensure_pull_request_reviews_table,
    ensure_pull_request_comments_table,
    ensure_fork_columns,
    ensure_repositories_branch_policy_column,
    ensure_access_tokens_table,
    ensure_ssh_key_tables,
    ensure_credential_columns,
    ensure_webhook_tables,
    ensure_commit_statuses_table,
    ensure_server_hooks_table,
    ensure_integration_columns,
    ensure_activity_columns,
    ensure_security_tables,
)


def apply_legacy_patches(connection) -> None:
    """Run the whole chain on one connection. Any failure propagates."""
    for patch in LEGACY_PATCHES:
        patch(connection)
