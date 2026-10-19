from sqlalchemy import create_engine, MetaData
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv
import os

load_dotenv()

# Database URL - you can configure this in .env file
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./foxnest.db")
SQLITE_URL = "sqlite:///./foxnest.db"

# Echo every SQL statement. Off unless explicitly asked for: with it on, each request
# writes its full query text (and bound parameters, which include password hashes) to
# the journal, which costs real throughput and buries anything worth reading.
SQL_ECHO = os.getenv("FOXNEST_SQL_ECHO", "").strip().lower() in {"1", "true", "yes", "on"}


def _is_sqlite_url(url: str) -> bool:
    return (url or "").strip().lower().startswith("sqlite:")


def _redacted(url: str) -> str:
    """The URL with any password removed, for logs."""
    try:
        from sqlalchemy.engine import make_url
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return url.split("@")[-1] if "@" in url else url


# A server configured for PostgreSQL used to fall back to a local SQLite file when the
# connection failed, print one line about it, and carry on -- accepting pushes into an
# empty database while the real one was unreachable. Now that is an error unless an
# operator asks for the fallback explicitly (local development only).
_ALLOW_SQLITE_FALLBACK = os.getenv("FOXNEST_ALLOW_SQLITE_FALLBACK", "").strip().lower() in {
    "1", "true", "yes", "on",
}

try:
    engine = create_engine(DATABASE_URL, echo=SQL_ECHO)
    # Test connection
    engine.connect().close()
    backend = "SQLite" if _is_sqlite_url(DATABASE_URL) else "PostgreSQL"
    print(f"Connected to {backend}: {_redacted(DATABASE_URL)}")
except Exception as e:
    if _is_sqlite_url(DATABASE_URL) or not _ALLOW_SQLITE_FALLBACK:
        raise SystemExit(
            f"Refusing to start: could not connect to {_redacted(DATABASE_URL)}: {e}"
            + ("" if _is_sqlite_url(DATABASE_URL) else
               "\nSet FOXNEST_ALLOW_SQLITE_FALLBACK=1 to fall back to a local SQLite file "
               "(development only: it is a different, empty database).")
        )
    print(f"PostgreSQL connection failed: {e}")
    print(f"FOXNEST_ALLOW_SQLITE_FALLBACK is set: falling back to {SQLITE_URL}")
    engine = create_engine(SQLITE_URL, echo=SQL_ECHO)
    print(f"Connected to SQLite: {SQLITE_URL}")

# The database actually in use: DATABASE_URL, unless the fallback above was taken.
# Migrations and backups act on this one, never on a URL the server is not serving.
ACTIVE_DATABASE_URL = engine.url.render_as_string(hide_password=False)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    """Dependency to get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def create_tables():
    """Create all tables if they don't exist"""
    from database.models import (
        User, Repository, Commit, CommitFile, FileObject,
        RepositoryTag, Branch, Activity, PendingCommit, PendingCommitFile, PendingRepository, UserPermission,
        CommitParent, Tag, Release, PullRequest, FileLineage
    )
    # Import models to ensure they're registered with Base
    # Then create all tables
    Base.metadata.create_all(bind=engine)
