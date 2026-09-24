"""FoxNest application factory.

Builds the FastAPI application: CORS, the startup schema bootstrap, and every router.
Nothing else should import this module apart from the process entrypoints (``server.py``
and any ASGI server pointed at ``app.main:app``) — importing it from a route or service
module would create an import cycle.
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from database.database import create_tables

from app.api import register_routers
from app.config import CORS_ORIGINS
from app.services.refs import ReferenceError
from app.db.schema_check import verify_or_fail
from app.db.migrations import (
    ensure_access_tokens_table,
    ensure_branch_protection_tables,
    ensure_commit_signatures_table,
    ensure_commit_statuses_table,
    ensure_fork_columns,
    ensure_merge_conflict_branch_sessions,
    ensure_merge_conflict_tables,
    ensure_password_reset_otp_table,
    ensure_pull_request_comments_table,
    ensure_pull_request_reviews_table,
    ensure_repositories_branch_policy_column,
    ensure_security_tables,
    ensure_server_hooks_table,
    ensure_ssh_key_tables,
    ensure_user_permissions_issue_id_column,
    ensure_user_permissions_show_in_web_ui_column,
    ensure_users_last_login_at_column,
    ensure_users_password_hash_column,
    ensure_versioning_schema,
    ensure_webhook_tables,
)

app = FastAPI(
    title="FoxNest Server",
    description="Central repository server for the FoxNest version control system",
    version="2.0.0",
)

# CORS: reflect configured origins only. Never pair Access-Control-Allow-Origin: * with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
)


# A refused reference change answers in one shape everywhere, whichever route it came
# from, so a client can branch on `error` instead of parsing prose.
@app.exception_handler(ReferenceError)
async def _reference_error_handler(request, exc: ReferenceError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": exc.code,
            "code": exc.code,
            "message": exc.message,
            "detail": exc.message,
            **exc.payload,
        },
    )


# Create tables on startup
@app.on_event("startup")
async def startup_event():
    """Initialize database tables"""
    print("=" * 60)
    print("FoxNest Server Starting...")
    print("=" * 60)
    print("Initializing database...")

    # Create all tables if they don't exist
    create_tables()
    ensure_users_password_hash_column()
    ensure_users_last_login_at_column()
    ensure_user_permissions_show_in_web_ui_column()
    ensure_user_permissions_issue_id_column()
    ensure_versioning_schema()
    ensure_commit_signatures_table()
    ensure_password_reset_otp_table()
    ensure_branch_protection_tables()
    ensure_merge_conflict_tables()
    # Must follow the create above: it rebuilds that table to relax a constraint.
    ensure_merge_conflict_branch_sessions()
    ensure_pull_request_reviews_table()
    ensure_pull_request_comments_table()
    ensure_fork_columns()
    ensure_repositories_branch_policy_column()
    ensure_access_tokens_table()
    ensure_ssh_key_tables()
    ensure_webhook_tables()
    ensure_commit_statuses_table()
    ensure_server_hooks_table()
    ensure_security_tables()

    # The patches above each swallow their own errors, so "nothing raised"
    # has never meant "the schema is right". Ask the database instead, and
    # refuse to serve requests against a schema we know is wrong.
    verify_or_fail()

    print("✓ Database tables created/verified")
    print("  - users (with role and team_lead_id)")
    print("  - repositories")
    print("  - commits")
    print("  - commit_files")
    print("  - commit_parents")
    print("  - file_objects")
    print("  - repository_tags")
    print("  - tags")
    print("  - releases")
    print("  - branches")
    print("  - pull_requests")
    print("  - activities")
    print("  - pending_commits")
    print("  - pending_repositories")
    print("  - pending_user_registrations")
    print("  - user_permissions")
    print("  - repository_stars")
    print("=" * 60)


register_routers(app)
