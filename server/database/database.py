from sqlalchemy import create_engine, MetaData
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv
import os

load_dotenv()

# Database URL - you can configure this in .env file
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://foxnest_user:foxnest_password@localhost/foxnest_db")

# For SQLite fallback (development)
SQLITE_URL = "sqlite:///./foxnest.db"

# Try PostgreSQL first, fallback to SQLite
try:
    engine = create_engine(DATABASE_URL, echo=True)
    # Test connection
    engine.connect()
    print(f"Connected to PostgreSQL: {DATABASE_URL}")
except Exception as e:
    print(f"PostgreSQL connection failed: {e}")
    print("Falling back to SQLite")
    engine = create_engine(SQLITE_URL, echo=True)

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
