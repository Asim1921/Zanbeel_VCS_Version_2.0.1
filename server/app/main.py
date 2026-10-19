"""FoxNest application factory.

Builds the FastAPI application: CORS, the startup schema bootstrap, and every router.
Nothing else should import this module apart from the process entrypoints (``server.py``
and any ASGI server pointed at ``app.main:app``) — importing it from a route or service
module would create an import cycle.
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api import register_routers
from app.config import CORS_ORIGINS, SERVER_VERSION
from app.services.refs import ReferenceError
from app.services.audit import AuditContextMiddleware
from app.db.schema_check import verify_or_fail
from app.db import upgrade as schema_upgrade

app = FastAPI(
    title="FoxNest Server",
    description="Central repository server for the FoxNest version control system",
    version=SERVER_VERSION,
)

# CORS: reflect configured origins only. Never pair Access-Control-Allow-Origin: * with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
    # Paged lists say where the next page is in these headers, and a 429 says how long
    # to wait; a browser on another origin can read neither unless they are exposed.
    expose_headers=["Link", "X-Total-Count", "Retry-After"],
)

# Lets the activity log see the request (credential, client address) from any layer.
app.add_middleware(AuditContextMiddleware)


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

    # Versioned migrations, applied once each, all or nothing (app/db/upgrade.py). A
    # failure raises and the server does not start: serving requests against a schema
    # half-way between two revisions is what the old startup patches used to do.
    if schema_upgrade.auto_migrate_enabled():
        schema_upgrade.upgrade_database()

    # "The upgrade succeeded" is still not "the schema is right". Ask the database,
    # and refuse to serve requests against a schema we know is wrong.
    verify_or_fail()

    # Code search: open the index and queue whatever is behind its default branch.
    # Runs on a background thread, so a large backlog does not delay startup.
    from app.services import search_index
    search_index.start()

    # The activity log is kept forever unless an administrator sets a retention period.
    from app.services import audit
    from database.database import SessionLocal
    from app.services import backup, rate_limit
    db = SessionLocal()
    try:
        removed = audit.prune(db)
        if removed:
            print(f"[ok] Pruned {removed} activity record(s) past FOXNEST_ACTIVITY_RETENTION_DAYS")
        rate_limit.prune(db)
        # A backup or drill recorded as running when no process holds the maintenance
        # lock was cut off by the last shutdown; say so instead of showing it running.
        interrupted = backup.recover_interrupted(db)
        if interrupted:
            print(f"[warn] Marked {interrupted} backup job(s) interrupted by the last shutdown")
    finally:
        db.close()

    # Scheduled backups, when FOXNEST_BACKUP_INTERVAL_HOURS is set.
    backup.start_scheduler()
    if backup.interval_hours():
        print(f"[ok] Backups scheduled every {backup.interval_hours()}h, keeping "
              f"{backup.keep_count()}, in {backup.backup_root()}")

    print("=" * 60)


@app.on_event("shutdown")
async def shutdown_event():
    """Stop background indexing so shutdown does not wait for a queue of repositories."""
    from app.services import backup, search_index
    search_index.stop()
    backup.stop_scheduler()


register_routers(app)
