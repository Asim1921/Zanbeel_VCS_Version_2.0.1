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

try:
    engine = create_engine(DATABASE_URL, echo=SQL_ECHO)
    # Test connection
    engine.connect().close()
    backend = "SQLite" if _is_sqlite_url(DATABASE_URL) else "PostgreSQL"
    print(f"Connected to {backend}: {DATABASE_URL}")
except Exception as e:
    if _is_sqlite_url(DATABASE_URL):
        raise
    print(f"PostgreSQL connection failed: {e}")
    print("Falling back to SQLite")
    engine = create_engine(SQLITE_URL, echo=SQL_ECHO)
    print(f"Connected to SQLite: {SQLITE_URL}")

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
