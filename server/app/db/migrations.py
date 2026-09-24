"""Idempotent schema patches applied at startup so older deployments can upgrade
in place without a migration tool."""

from sqlalchemy import inspect, text

from database.database import engine


def ensure_users_password_hash_column():
    """Add users.password_hash for pre-auth databases."""
    try:
        inspector = inspect(engine)
        columns = [column["name"] for column in inspector.get_columns("users")]
        if "password_hash" in columns:
            return

        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)"))
        print("✓ Added missing column users.password_hash")
    except Exception as exc:
        print(f"⚠️ Unable to auto-add users.password_hash column: {exc}")


def ensure_users_last_login_at_column():
    """Add users.last_login_at for engagement / activity monitoring."""
    try:
        inspector = inspect(engine)
        columns = [column["name"] for column in inspector.get_columns("users")]
        if "last_login_at" in columns:
            return
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE users ADD COLUMN last_login_at DATETIME"))
        print("✓ Added missing column users.last_login_at")
    except Exception as exc:
        print(f"⚠️ Unable to auto-add users.last_login_at column: {exc}")


def ensure_user_permissions_show_in_web_ui_column():
    """Add user_permissions.show_in_web_ui for web vs CLI-oriented grants."""
    try:
        inspector = inspect(engine)
        columns = [column["name"] for column in inspector.get_columns("user_permissions")]
        if "show_in_web_ui" in columns:
            return
        with engine.begin() as connection:
            # SQLite stores BOOLEAN as INTEGER; default 1 = visible (legacy behavior).
            connection.execute(text(
                "ALTER TABLE user_permissions ADD COLUMN show_in_web_ui BOOLEAN DEFAULT 1"
            ))
        print("✓ Added missing column user_permissions.show_in_web_ui")
    except Exception as exc:
        print(f"⚠️ Unable to auto-add user_permissions.show_in_web_ui column: {exc}")


def ensure_user_permissions_issue_id_column():
    """Add user_permissions.issue_id for issue-scoped (temporary) grants."""
    try:
        inspector = inspect(engine)
        columns = [column["name"] for column in inspector.get_columns("user_permissions")]
        if "issue_id" in columns:
            return
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE user_permissions ADD COLUMN issue_id INTEGER REFERENCES issues(id)"
            ))
        print("✓ Added missing column user_permissions.issue_id")
    except Exception as exc:
        print(f"⚠️ Unable to auto-add user_permissions.issue_id column: {exc}")


def ensure_versioning_schema():
    """Create lineage table and critical indexes when upgrading older deployments."""
    try:
        with engine.begin() as connection:
            connection.execute(text(
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
                connection.execute(text(stmt))
        print("✓ Versioning schema verified (lineage + indexes)")
    except Exception as exc:
        print(f"⚠️ Unable to verify versioning schema: {exc}")


def ensure_commit_signatures_table():
    """Create the commit attestation table for deployments upgrading in place."""
    try:
        with engine.begin() as connection:
            connection.execute(text(
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
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_commit_signatures_repo "
                "ON commit_signatures(repository_id)"
            ))
        print("[ok] Commit signature table verified")
    except Exception as exc:
        print(f"[warn] Unable to create commit_signatures table: {exc}")


def ensure_branch_protection_tables():
    """Branch protection: the generation counter plus the policy, unlock, release and audit tables."""
    try:
        with engine.begin() as connection:
            # Compare-and-swap counter. Existing branches start at 0.
            cols = [row[1] for row in connection.execute(text("PRAGMA table_info(branches)"))]
            if "generation" not in cols:
                connection.execute(text(
                    "ALTER TABLE branches ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"
                ))

            connection.execute(text(
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
            connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_branch_policies_repo_pattern "
                "ON branch_protection_policies(repository_id, branch_pattern)"
            ))

            connection.execute(text(
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
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_branch_unlocks_repo_branch "
                "ON branch_unlocks(repository_id, branch_name)"
            ))

            connection.execute(text(
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
            connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_immutable_releases_repo_name "
                "ON immutable_releases(repository_id, name)"
            ))

            connection.execute(text(
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
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_ref_audit_repo_ref "
                "ON ref_audit_events(repository_id, reference)"
            ))
        print("[ok] Branch protection tables verified")
    except Exception as exc:
        print(f"[warn] Unable to create branch protection tables: {exc}")


def ensure_password_reset_otp_table():
    """Create the password-reset one-time-code table for deployments upgrading in place."""
    try:
        with engine.begin() as connection:
            connection.execute(text(
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
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_password_reset_otps_username "
                "ON password_reset_otps(username)"
            ))
        print("[ok] Password reset OTP table verified")
    except Exception as exc:
        print(f"[warn] Unable to create password_reset_otps table: {exc}")


def ensure_merge_conflict_tables():
    """Create the merge-conflict resolution tables where they do not already exist.

    Deployed databases already carry these (a previous developer added the schema but
    never wired anything to it); fresh ones get them from the models. This covers the
    gap for databases created in between.
    """
    try:
        with engine.begin() as connection:
            connection.execute(text(
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
            connection.execute(text(
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
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_merge_conflict_sessions_pr "
                "ON merge_conflict_sessions(pull_request_id, status)"
            ))
        print("[ok] Merge conflict tables verified")
    except Exception as exc:
        print(f"[warn] Unable to create merge conflict tables: {exc}")


def ensure_repositories_branch_policy_column():
    """Add repositories.branch_policy_json for branch protection rules.

    `branch_policies.get_branch_policy` has always read this column defensively via
    getattr, so it returned the defaults for every repository -- the column was never
    created. This adds it; a NULL still means "use the defaults".
    """
    try:
        inspector = inspect(engine)
        columns = [column["name"] for column in inspector.get_columns("repositories")]
        if "branch_policy_json" in columns:
            return
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE repositories ADD COLUMN branch_policy_json TEXT"))
        print("[ok] Added missing column repositories.branch_policy_json")
    except Exception as exc:
        print(f"[warn] Unable to auto-add repositories.branch_policy_json column: {exc}")


def ensure_pull_request_reviews_table():
    """Create the pull request review table for deployments upgrading in place."""
    try:
        with engine.begin() as connection:
            connection.execute(text(
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
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_pull_request_reviews_pr "
                "ON pull_request_reviews(pull_request_id)"
            ))
        print("[ok] Pull request review table verified")
    except Exception as exc:
        print(f"[warn] Unable to create pull_request_reviews table: {exc}")


def ensure_pull_request_comments_table():
    """Create the inline diff comment table for deployments upgrading in place."""
    try:
        with engine.begin() as connection:
            connection.execute(text(
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
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_pr_comments_pr_path "
                "ON pull_request_comments(pull_request_id, file_path)"
            ))
        print("[ok] Pull request comment table verified")
    except Exception as exc:
        print(f"[warn] Unable to create pull_request_comments table: {exc}")


def ensure_fork_columns():
    """Add the fork relationship columns for deployments upgrading in place."""
    for table, column, ddl in (
        ("repositories", "forked_from_id", "ALTER TABLE repositories ADD COLUMN forked_from_id VARCHAR(16)"),
        ("pull_requests", "source_repository_id", "ALTER TABLE pull_requests ADD COLUMN source_repository_id VARCHAR(16)"),
    ):
        try:
            existing = [c["name"] for c in inspect(engine).get_columns(table)]
            if column in existing:
                continue
            with engine.begin() as connection:
                connection.execute(text(ddl))
            print(f"[ok] Added missing column {table}.{column}")
        except Exception as exc:
            print(f"[warn] Unable to auto-add {table}.{column}: {exc}")


def ensure_access_tokens_table():
    """Create the personal access token table for deployments upgrading in place."""
    try:
        with engine.begin() as connection:
            connection.execute(text(
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
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_access_tokens_user_active "
                "ON access_tokens(user_id, revoked_at)"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_access_tokens_hash "
                "ON access_tokens(token_hash)"
            ))
        print("[ok] Access token table verified")
    except Exception as exc:
        print(f"[warn] Unable to create access_tokens table: {exc}")


def ensure_ssh_key_tables():
    """Create the SSH public key and challenge tables."""
    try:
        with engine.begin() as connection:
            connection.execute(text(
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
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_ssh_keys_user ON ssh_keys(user_id)"
            ))
            connection.execute(text(
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
        print("[ok] SSH key tables verified")
    except Exception as exc:
        print(f"[warn] Unable to create SSH key tables: {exc}")


def ensure_webhook_tables():
    """Create the webhook and webhook delivery tables."""
    try:
        with engine.begin() as connection:
            connection.execute(text(
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
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_webhooks_repo ON webhooks(repository_id)"
            ))
            connection.execute(text(
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
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_webhook_deliveries_hook_time "
                "ON webhook_deliveries(webhook_id, created_at)"
            ))
        print("[ok] Webhook tables verified")
    except Exception as exc:
        print(f"[warn] Unable to create webhook tables: {exc}")


def ensure_commit_statuses_table():
    """Create the commit status check table."""
    try:
        with engine.begin() as connection:
            connection.execute(text(
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
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_commit_statuses_commit_context "
                "ON commit_statuses(commit_id, context)"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_commit_statuses_repo "
                "ON commit_statuses(repository_id)"
            ))
        print("[ok] Commit status table verified")
    except Exception as exc:
        print(f"[warn] Unable to create commit_statuses table: {exc}")


def ensure_server_hooks_table():
    """Create the server-side hook policy table."""
    try:
        with engine.begin() as connection:
            connection.execute(text(
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
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_server_hooks_repo_type "
                "ON server_hooks(repository_id, hook_type)"
            ))
        print("[ok] Server hook table verified")
    except Exception as exc:
        print(f"[warn] Unable to create server_hooks table: {exc}")


def ensure_security_tables():
    """Create the login-attempt, lockout and backup tables."""
    try:
        with engine.begin() as connection:
            connection.execute(text(
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
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_login_attempts_user_time "
                "ON login_attempts(username, created_at)"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_login_attempts_ip_time "
                "ON login_attempts(ip_address, created_at)"
            ))
            connection.execute(text(
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
            connection.execute(text(
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
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_backup_records_time "
                "ON backup_records(created_at)"
            ))
        print("[ok] Security and backup tables verified")
    except Exception as exc:
        print(f"[warn] Unable to create security tables: {exc}")
