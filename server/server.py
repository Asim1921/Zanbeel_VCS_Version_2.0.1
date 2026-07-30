#!/usr/bin/env python3
"""
FoxNest Server - Central repository server for the FoxNest version control system with SQL Database
"""
 
import os
import json
import hashlib
import shutil
import asyncio
import threading
import re
import difflib
import logging
import binascii
import zipfile
import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List
import base64
from types import SimpleNamespace
import hmac
import secrets

from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, UploadFile, File, Form, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import inspect, text, func, desc
import uvicorn

# Database imports
from database.database import get_db, create_tables, SessionLocal, engine
from database.models import (
    User,
    Repository,
    Commit,
    CommitFile,
    FileObject,
    PendingRepository,
    PendingCommitFile,
    PendingUserRegistration,
    UserPermission,
    Branch,
    Tag,
    Release,
    PullRequest,
    CommitParent,
    FileLineage,
    Issue,
    IssueComment,
    IssueWatcher,
    Milestone,
)
from database.crud import (
    UserCRUD,
    RepositoryCRUD,
    CommitCRUD,
    FileObjectCRUD,
    ActivityCRUD,
    PendingCommitCRUD,
    PendingRepositoryCRUD,
    PendingUserRegistrationCRUD,
    UserPermissionCRUD,
    BranchCRUD,
    TagCRUD,
    ReleaseCRUD,
    PullRequestCRUD,
    IssueCRUD,
    IssueLabelCRUD,
    MilestoneCRUD,
    IssueLinkCRUD,
    parse_issue_references,
    parse_mentions,
    NotificationCRUD,
    IssueEventCRUD,
    IssueAccessRequestCRUD,
    RepositoryStarCRUD,
    contributor_count_for_repository,
    UserEngagementCRUD,
)

# Auto-docs generator
import docs_generator
import project_docs_generator
import docx_utils
import autodocs_v2

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

SERVER_ROOT = Path("/tmp/foxnest_server")  # Keep for backward compatibility
REPOS_DIR = SERVER_ROOT / "repositories"
DOCS_DIR = SERVER_ROOT / "docs"  # Store generated docs
DOCS_DIR.mkdir(parents=True, exist_ok=True)

DOCS_GENERATION_JOBS: Dict[str, Dict[str, Any]] = {}
DOCS_GENERATION_LOCK = threading.Lock()

AUTH_SECRET = os.getenv("FOXNEST_AUTH_SECRET", "")
if not AUTH_SECRET:
    AUTH_SECRET = "foxnest-dev-secret-change-me"

TOKEN_EXPIRY_HOURS = int(os.getenv("FOXNEST_TOKEN_EXPIRY_HOURS", "12"))
PWD_ITERATIONS = int(os.getenv("FOXNEST_PASSWORD_ITERATIONS", "200000"))
PASSWORD_SETUP_KEY = os.getenv("FOXNEST_PASSWORD_SETUP_KEY", "")
http_bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger("foxnest.versioning")

# Pydantic models for request/response
class CreateRepositoryRequest(BaseModel):
    username: str
    repo_name: str
    description: Optional[str] = None

class PushCommitRequest(BaseModel):
    commit: Dict[str, Any]
    archive: Optional[bool] = False
    branch: Optional[str] = None
    pusher: Optional[str] = None
    expected_head_commit_id: Optional[str] = None

class BranchCreateRequest(BaseModel):
    name: str
    from_commit: Optional[str] = None

class BranchRenameRequest(BaseModel):
    new_name: str

class BranchHeadUpdateRequest(BaseModel):
    head_commit_id: str

class TagCreateRequest(BaseModel):
    name: str
    commit_id: Optional[str] = None
    message: Optional[str] = None

class ReleaseCreateRequest(BaseModel):
    version: str
    tag: Optional[str] = None
    title: Optional[str] = None
    notes: Optional[str] = None

class PullRequestCreateRequest(BaseModel):
    title: str
    description: Optional[str] = None
    source_branch: str
    target_branch: str

class FileRollbackRequest(BaseModel):
    path: str
    target_commit_id: str
    branch: Optional[str] = None
    summary: Optional[str] = None
    expected_head_commit_id: Optional[str] = None

class BranchRollbackRequest(BaseModel):
    branch: str
    target_commit_id: str
    summary: Optional[str] = None
    expected_head_commit_id: Optional[str] = None

class MergePullRequestRequest(BaseModel):
    expected_head_commit_id: Optional[str] = None


class IssueCreateRequest(BaseModel):
    title: str
    description: Optional[str] = None
    issue_type: str = "task"
    priority: str = "medium"
    assigned_to: Optional[str] = None
    milestone_id: Optional[int] = None
    labels: Optional[List[str]] = None


class IssueUpdateRequest(BaseModel):
    status: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    issue_type: Optional[str] = None
    assigned_to: Optional[str] = None
    milestone_id: Optional[int] = None


class IssueCommentCreateRequest(BaseModel):
    body: str


class IssueWatchRequest(BaseModel):
    watch: bool = True


class IssueLabelUpsertRequest(BaseModel):
    name: str
    color: Optional[str] = None


class MilestoneCreateRequest(BaseModel):
    title: str
    description: Optional[str] = None
    due_date: Optional[str] = None


class RepositoryResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    repo_id: Optional[str] = None

class CommitResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    commit_id: Optional[str] = None

class CommitsResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    commits: Optional[List[Dict[str, Any]]] = None

class RepositoriesResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    repositories: Optional[List[Dict[str, Any]]] = None

class UpdateRepositoryDetailsRequest(BaseModel):
    g1_coordinator: Optional[str] = None
    tested: Optional[bool] = None

class UpdateRepositoryDetailsResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    repository: Optional[Dict[str, Any]] = None

class CreatePermissionRequest(BaseModel):
    username: str
    repo_id: str
    permission_level: str  # read, write, team_lead, admin
    granted_by: Optional[str] = None

class ReviewCommitRequest(BaseModel):
    reviewer_username: Optional[str] = None
    action: str  # approve or reject
    comment: Optional[str] = None

class LoginRequest(BaseModel):
    username: str
    password: str

class ChangePasswordRequest(BaseModel):
    current_password: Optional[str] = None
    new_password: str

class BootstrapPasswordRequest(BaseModel):
    username: str
    new_password: str
    setup_key: str

class RegistrationRequest(BaseModel):
    username: str
    password: str
    email: Optional[str] = None
    full_name: Optional[str] = None
    requested_role: str = 'developer'
    requested_team_lead_username: Optional[str] = None

class ReviewRegistrationRequest(BaseModel):
    action: str  # approve or reject
    comment: Optional[str] = None
    role: Optional[str] = None
    team_lead_id: Optional[int] = None

class MarkNotificationsReadRequest(BaseModel):
    ids: List[int]

# Create FastAPI app
app = FastAPI(title="FoxNest Server", description="Central repository server for the FoxNest version control system", version="2.0.0")

# Add CORS middleware
cors_origins = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://192.168.15.207:5173",
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Add custom middleware to handle all OPTIONS requests
@app.middleware("http")
async def cors_options_middleware(request, call_next):
    """Handle all OPTIONS requests before they reach route handlers"""
    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Allow-Credentials": "true",
        }
        return JSONResponse(content={"message": "OK"}, status_code=200, headers=headers)
    
    response = await call_next(request)
    
    # Add CORS headers to all responses
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    
    return response

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

def _encode_cursor(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")

def _decode_cursor(cursor: Optional[str]) -> Dict[str, Any]:
    if not cursor:
        return {}
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        pass
    raise HTTPException(status_code=400, detail="Invalid cursor")

def _raise_head_mismatch(branch_name: str, expected_head: Optional[str], actual_head: Optional[str]):
    raise HTTPException(
        status_code=409,
        detail={
            "code": "HEAD_MISMATCH",
            "branch": branch_name,
            "expected_head": expected_head,
            "actual_head": actual_head,
            "message": "Branch head changed. Refresh and retry."
        }
    )

# Helper functions
def repository_to_dict(repo: Repository, db: Optional[Session] = None, actor: Optional[User] = None) -> Dict[str, Any]:
    """Convert Repository model to dictionary"""
    can_write = False
    can_manage = False
    if db is not None and actor is not None:
        can_write = can_write_repository(db, actor, repo)
        can_manage = can_manage_repository(db, actor, repo)

    out = {
        "id": repo.id,
        "name": repo.name,
        "description": repo.description,
        "owner": repo.owner.username,
        "created_at": repo.created_at.isoformat() if repo.created_at else None,
        "updated_at": repo.updated_at.isoformat() if repo.updated_at else None,
        "commits": [commit.id for commit in repo.commits],
        "head": repo.head_commit_id,
        "is_archived": repo.is_archived,
        "archived_at": repo.archived_at.isoformat() if repo.archived_at else None,
        "archived_reason": repo.archived_reason,
        "language": repo.language,
        "size": repo.size_bytes,
        "is_public": repo.is_public,
        "g1_coordinator": repo.g1_coordinator,
        "tested": repo.tested,
        "instruction_manual_filename": repo.instruction_manual_filename,
        "has_instruction_manual": bool(repo.instruction_manual_path),
        "current_user_can_write": can_write,
        "current_user_can_manage": can_manage,
    }
    if db is not None:
        out["branch_count"] = (
            db.query(Branch).filter(Branch.repository_id == repo.id).count()
        )
        out["star_count"] = RepositoryStarCRUD.count_for_repository(db, repo.id)
        out["contributor_count"] = contributor_count_for_repository(db, repo)
        if actor is not None:
            out["starred_by_me"] = RepositoryStarCRUD.is_starred_by_user(
                db, actor.id, repo.id
            )
        else:
            out["starred_by_me"] = False
    else:
        out["branch_count"] = 0
        out["star_count"] = 0
        out["contributor_count"] = 1
        out["starred_by_me"] = False
    return out

def commit_to_dict(commit: Commit, include_files: bool = False) -> Dict[str, Any]:
    """Convert Commit model to dictionary"""
    parents = []
    if getattr(commit, "parent_links", None):
        parents = [link.parent_commit_id for link in sorted(commit.parent_links, key=lambda item: item.parent_order)]
    elif commit.parent_commit_id:
        parents = [commit.parent_commit_id]

    commit_dict = {
        "id": commit.id,
        "repository_id": commit.repository_id,
        "author": commit.author.username,
        "parent": commit.parent_commit_id,
        "parents": parents,
        "message": commit.message,
        "timestamp": commit.created_at.isoformat() if commit.created_at else None,
        "tree_hash": commit.tree_hash
    }
    
    if include_files:
        files = {}
        for commit_file in commit.files:
            if commit_file.file_object:
                content_b64 = base64.b64encode(commit_file.file_object.content).decode()
                files[commit_file.file_hash] = {
                    "path": commit_file.file_path,
                    "content": content_b64,
                    "size": commit_file.file_size,
                    "mime_type": getattr(commit_file.file_object, "mime_type", None)
                }
        commit_dict["files"] = files
    else:
        commit_dict["files"] = [
            {
                "path": cf.file_path,
                "hash": cf.file_hash,
                "size": cf.file_size
            }
            for cf in commit.files
        ]
    
    return commit_dict

def _get_commit_parents(db: Session, commit_id: Optional[str]) -> List[str]:
    if not commit_id:
        return []
    links = db.query(CommitParent).filter(CommitParent.commit_id == commit_id).order_by(CommitParent.parent_order).all()
    if links:
        return [link.parent_commit_id for link in links]
    commit = db.query(Commit).filter(Commit.id == commit_id).first()
    if commit and commit.parent_commit_id:
        return [commit.parent_commit_id]
    return []

def _collect_reachable_commits(db: Session, head_commit_id: Optional[str], stop_at: Optional[str] = None) -> List[str]:
    if not head_commit_id:
        return []
    visited = set()
    stack = [head_commit_id]
    while stack:
        current = stack.pop()
        if not current or current in visited:
            continue
        visited.add(current)
        if stop_at and current == stop_at:
            continue
        parents = _get_commit_parents(db, current)
        for parent_id in parents:
            if parent_id and parent_id not in visited:
                stack.append(parent_id)
    return list(visited)

def _is_ancestor(db: Session, ancestor_id: str, descendant_id: str) -> bool:
    if not ancestor_id or not descendant_id:
        return False
    if ancestor_id == descendant_id:
        return True
    visited = set()
    stack = [descendant_id]
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        for parent_id in _get_commit_parents(db, current):
            if parent_id == ancestor_id:
                return True
            if parent_id and parent_id not in visited:
                stack.append(parent_id)
    return False

def _normalize_branch_name(name: str) -> str:
    normalized = (name or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Branch name is required")
    if normalized.startswith("-"):
        raise HTTPException(status_code=400, detail="Branch name cannot start with '-' ")
    if ".." in normalized or "//" in normalized or normalized.endswith("/"):
        raise HTTPException(status_code=400, detail="Invalid branch name")
    if not re.match(r"^[A-Za-z0-9._/-]+$", normalized):
        raise HTTPException(status_code=400, detail="Branch name contains invalid characters")
    return normalized

def _get_default_branch(db: Session, repo_id: str) -> Branch:
    branch = db.query(Branch).filter(Branch.repository_id == repo_id, Branch.is_default == True).first()
    if branch:
        return branch
    # Ensure a default branch exists
    branch = BranchCRUD.create_branch(db, repo_id, "main", is_default=True)
    return branch

def _resolve_branch(db: Session, repo_id: str, branch_name: Optional[str]) -> Branch:
    if branch_name:
        normalized = _normalize_branch_name(branch_name)
        branch = BranchCRUD.get_branch(db, repo_id, normalized)
        if not branch:
            raise HTTPException(status_code=404, detail="Branch not found")
        return branch
    return _get_default_branch(db, repo_id)

def _get_commit_tree(db: Session, commit_id: Optional[str]) -> Dict[str, bytes]:
    if not commit_id:
        return {}
    commit = db.query(Commit).filter(Commit.id == commit_id).first()
    if not commit:
        return {}
    tree = {}
    for commit_file in commit.files:
        if commit_file.file_object:
            tree[commit_file.file_path] = commit_file.file_object.content
    return tree

def _get_ancestors_with_depth(db: Session, commit_id: Optional[str]) -> Dict[str, int]:
    if not commit_id:
        return {}
    depth = {commit_id: 0}
    queue = [(commit_id, 0)]
    while queue:
        current, dist = queue.pop(0)
        for parent_id in _get_commit_parents(db, current):
            if parent_id and parent_id not in depth:
                depth[parent_id] = dist + 1
                queue.append((parent_id, dist + 1))
    return depth

def _find_merge_base(db: Session, commit_a: Optional[str], commit_b: Optional[str]) -> Optional[str]:
    if not commit_a or not commit_b:
        return None
    depth_a = _get_ancestors_with_depth(db, commit_a)
    depth_b = _get_ancestors_with_depth(db, commit_b)
    common = set(depth_a.keys()) & set(depth_b.keys())
    if not common:
        return None
    return min(common, key=lambda cid: depth_a[cid] + depth_b[cid])

def _try_decode_text(content: Optional[bytes]) -> Optional[str]:
    if content is None:
        return None
    try:
        return content.decode("utf-8")
    except Exception:
        return None

def _merge_text(base: Optional[bytes], ours: Optional[bytes], theirs: Optional[bytes]) -> tuple[bytes, bool]:
    base_text = _try_decode_text(base) or ""
    ours_text = _try_decode_text(ours) or ""
    theirs_text = _try_decode_text(theirs) or ""
    if ours_text == theirs_text:
        return ours_text.encode("utf-8"), False
    if base_text == ours_text:
        return theirs_text.encode("utf-8"), False
    if base_text == theirs_text:
        return ours_text.encode("utf-8"), False
    merged = (
        "<<<<<<< OURS\n"
        f"{ours_text}"
        "\n=======\n"
        f"{theirs_text}"
        "\n>>>>>>> THEIRS\n"
    )
    return merged.encode("utf-8"), True

def _merge_trees(base: Dict[str, bytes], ours: Dict[str, bytes], theirs: Dict[str, bytes]) -> tuple[Dict[str, bytes], List[str]]:
    merged = {}
    conflicts = []
    paths = set(base.keys()) | set(ours.keys()) | set(theirs.keys())
    for path in sorted(paths):
        base_content = base.get(path)
        our_content = ours.get(path)
        their_content = theirs.get(path)

        if our_content == their_content:
            if our_content is not None:
                merged[path] = our_content
            continue

        if base_content == our_content:
            if their_content is not None:
                merged[path] = their_content
            continue

        if base_content == their_content:
            if our_content is not None:
                merged[path] = our_content
            continue

        if _try_decode_text(our_content) is not None and _try_decode_text(their_content) is not None:
            merged_content, has_conflict = _merge_text(base_content, our_content, their_content)
            merged[path] = merged_content
            if has_conflict:
                conflicts.append(path)
        else:
            # Binary or undecodable conflicts - keep ours but report conflict
            if our_content is not None:
                merged[path] = our_content
            conflicts.append(path)

    return merged, conflicts

SEMVER_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
DIFF_MAX_LINES = 5000
DIFF_MAX_BYTES = 1024 * 1024  # 1 MB per side for inline diffs

def _validate_semver(version: str) -> str:
    normalized = (version or "").strip()
    if not SEMVER_RE.match(normalized):
        raise HTTPException(status_code=400, detail="Version must follow semantic versioning (e.g., 1.2.3 or v1.2.3)")
    return normalized

def _get_commit_for_branch(db: Session, repository: Repository, branch_name: Optional[str]) -> Optional[Commit]:
    """Resolve the commit object for the selected branch (or repository head by default)."""
    commit_id = repository.head_commit_id
    if branch_name:
        resolved_branch = _resolve_branch(db, repository.id, branch_name)
        commit_id = resolved_branch.head_commit_id
    if not commit_id:
        return None
    return db.query(Commit).filter(Commit.id == commit_id).first()

def _build_files_payload_from_commit(commit: Commit, include_content: bool) -> Dict[str, Any]:
    files_dict: Dict[str, Any] = {}
    folders_set = set()

    for commit_file in commit.files:
        file_path = commit_file.file_path.replace('\\', '/')

        path_parts = file_path.split('/')
        for i in range(len(path_parts) - 1):
            folders_set.add('/'.join(path_parts[:i + 1]))

        file_entry = {
            "size": commit_file.file_size,
            "hash": commit_file.file_hash,
            "mime_type": commit_file.file_object.mime_type if commit_file.file_object else None,
            "is_binary": None
        }

        if include_content and commit_file.file_object:
            try:
                content = commit_file.file_object.content.decode('utf-8')
                is_binary = False
            except UnicodeDecodeError:
                content = base64.b64encode(commit_file.file_object.content).decode('utf-8')
                is_binary = True

            file_entry["content"] = content
            file_entry["is_binary"] = is_binary

        files_dict[file_path] = file_entry

    return {
        "files": files_dict,
        "folders": sorted(list(folders_set))
    }

def _tree_to_commit_payload(tree: Dict[str, bytes]) -> Dict[str, str]:
    return {
        path: base64.b64encode(content).decode("utf-8")
        for path, content in tree.items()
    }

def _is_vendor_path(path: str) -> bool:
    """Return True for dependency/vendor directories that should be ignored for docs."""
    normalized = (path or "").replace('\\', '/').lower()
    vendor_markers = [
        'node_modules/',
        'venv/',
        '.venv/',
        '__pycache__/',
        '.git/',
        'dist/',
        'build/'
    ]
    return any(marker in normalized for marker in vendor_markers)

def _is_binary_or_large(content: Optional[bytes]) -> bool:
    if content is None:
        return False
    if len(content) > DIFF_MAX_BYTES:
        return True
    return _try_decode_text(content) is None

def _build_side_by_side_diff(previous_text: str, current_text: str) -> Dict[str, Any]:
    previous_lines = previous_text.splitlines()
    current_lines = current_text.splitlines()
    matcher = difflib.SequenceMatcher(a=previous_lines, b=current_lines)

    rows: List[Dict[str, Any]] = []
    truncated = False

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if len(rows) >= DIFF_MAX_LINES:
            truncated = True
            break

        if tag == "equal":
            for idx in range(i2 - i1):
                rows.append({
                    "type": "same",
                    "previous": previous_lines[i1 + idx],
                    "current": current_lines[j1 + idx]
                })
                if len(rows) >= DIFF_MAX_LINES:
                    truncated = True
                    break
        elif tag == "delete":
            for line in previous_lines[i1:i2]:
                rows.append({"type": "removed", "previous": line, "current": ""})
                if len(rows) >= DIFF_MAX_LINES:
                    truncated = True
                    break
        elif tag == "insert":
            for line in current_lines[j1:j2]:
                rows.append({"type": "added", "previous": "", "current": line})
                if len(rows) >= DIFF_MAX_LINES:
                    truncated = True
                    break
        else:
            old_block = previous_lines[i1:i2]
            new_block = current_lines[j1:j2]
            max_len = max(len(old_block), len(new_block))
            for idx in range(max_len):
                rows.append({
                    "type": "changed",
                    "previous": old_block[idx] if idx < len(old_block) else "",
                    "current": new_block[idx] if idx < len(new_block) else ""
                })
                if len(rows) >= DIFF_MAX_LINES:
                    truncated = True
                    break

    stats = {
        "added": sum(1 for row in rows if row["type"] in ["added", "changed"] and row["current"]),
        "removed": sum(1 for row in rows if row["type"] in ["removed", "changed"] and row["previous"]),
        "unchanged": sum(1 for row in rows if row["type"] == "same")
    }

    return {
        "rows": rows,
        "stats": stats,
        "truncated": truncated,
        "max_lines": DIFF_MAX_LINES
    }

def get_actor_user(db: Session, actor_username: Optional[str]) -> User:
    """Resolve and validate the requesting user for permission-sensitive actions."""
    if not actor_username:
        raise HTTPException(status_code=403, detail="actor_username is required")

    user = UserCRUD.get_user_by_username(db, actor_username)
    if not user:
        raise HTTPException(status_code=403, detail=f"User '{actor_username}' does not exist")
    if not user.is_active:
        raise HTTPException(status_code=403, detail=f"User '{actor_username}' is inactive")

    return user

def can_manage_repository(db: Session, actor: User, repository: Repository) -> bool:
    """Return True when actor can perform management actions (archive/delete/admin-level)."""
    if not actor:
        return False

    if actor.id == repository.owner_id:
        return True

    actor_role = getattr(actor, 'role', 'developer')
    if actor_role in ['team_lead', 'admin']:
        return True

    if UserPermissionCRUD.has_permission(db, actor.username, repository.id, 'team_lead'):
        return True
    if UserPermissionCRUD.has_permission(db, actor.username, repository.id, 'admin'):
        return True

    return False


def user_sees_all_repository_branches(db: Session, actor: User, repository: Repository) -> bool:
    """Admins/owners, commit authors, and non-issue-scoped collaborators see every branch.

    Users with only issue-scoped permission rows (issue_id set) and no commits on this repo
    only see the default branch.
    """
    if not actor:
        return False
    if can_manage_repository(db, actor, repository):
        return True
    if actor.id == repository.owner_id:
        return True
    if db.query(Commit).filter(
        Commit.repository_id == repository.id,
        Commit.author_id == actor.id,
    ).first():
        return True
    if db.query(UserPermission).filter(
        UserPermission.repository_id == repository.id,
        UserPermission.user_id == actor.id,
        UserPermission.issue_id.is_(None),
    ).first():
        return True
    return False


def can_write_repository(db: Session, actor: User, repository: Repository) -> bool:
    """Return True when actor can modify repository content/metadata (write+)."""
    if not actor:
        return False

    if actor.id == repository.owner_id:
        return True

    actor_role = getattr(actor, 'role', 'developer')
    if actor_role in ['team_lead', 'admin']:
        return True

    # If the user has already authored commits in this repository,
    # allow continued write access to their contributed code.
    authored_commit = db.query(Commit).filter(
        Commit.repository_id == repository.id,
        Commit.author_id == actor.id
    ).first()
    if authored_commit:
        return True

    return UserPermissionCRUD.has_permission(db, actor.username, repository.id, 'write')

def require_repository_access(db: Session, actor_username: Optional[str], repository: Repository, action: str, required_scope: str = "manage") -> User:
    """Validate actor has required access to repository; raise 403 otherwise."""
    actor = get_actor_user(db, actor_username)
    if required_scope == "write":
        allowed = can_write_repository(db, actor, repository)
    else:
        allowed = can_manage_repository(db, actor, repository)

    if not allowed:
        raise HTTPException(
            status_code=403,
            detail=f"User '{actor.username}' is not allowed to {action} repository '{repository.name}'"
        )
    return actor

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")

def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)

def hash_password(password: str) -> str:
    if not password:
        raise ValueError("Password cannot be empty")
    salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PWD_ITERATIONS).hex()
    return f"pbkdf2_sha256${PWD_ITERATIONS}${salt}${pwd_hash}"

def verify_password(password: str, stored_hash: str) -> bool:
    if not password or not stored_hash:
        return False
    try:
        algorithm, iterations_str, salt, pwd_hash = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_str)
        check_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations).hex()
        return hmac.compare_digest(check_hash, pwd_hash)
    except Exception:
        return False

def create_access_token(username: str, role: str, expires_delta: Optional[timedelta] = None) -> str:
    expires = datetime.utcnow() + (expires_delta or timedelta(hours=TOKEN_EXPIRY_HOURS))
    payload = {
        "sub": username,
        "role": role,
        "exp": int(expires.timestamp()),
        "iat": int(datetime.utcnow().timestamp())
    }
    payload_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_encoded = _b64url_encode(payload_json)
    signature = hmac.new(AUTH_SECRET.encode("utf-8"), payload_encoded.encode("utf-8"), hashlib.sha256).digest()
    signature_encoded = _b64url_encode(signature)
    return f"{payload_encoded}.{signature_encoded}"

def decode_access_token(token: str) -> Dict[str, Any]:
    try:
        payload_part, signature_part = token.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token format")

    expected_signature = hmac.new(AUTH_SECRET.encode("utf-8"), payload_part.encode("utf-8"), hashlib.sha256).digest()
    given_signature = _b64url_decode(signature_part)
    if not hmac.compare_digest(expected_signature, given_signature):
        raise HTTPException(status_code=401, detail="Invalid token signature")

    try:
        payload = json.loads(_b64url_decode(payload_part).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    exp = payload.get("exp")
    if not exp or int(exp) < int(datetime.utcnow().timestamp()):
        raise HTTPException(status_code=401, detail="Token expired")

    return payload

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(http_bearer), db: Session = Depends(get_db)) -> User:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Authentication required")

    payload = decode_access_token(credentials.credentials)
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token subject")

    user = UserCRUD.get_user_by_username(db, username)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User is inactive or not found")
    return user


def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(http_bearer),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Same as get_current_user when Authorization is sent; otherwise None (no 401)."""
    if not credentials or not credentials.credentials:
        return None
    try:
        payload = decode_access_token(credentials.credentials)
    except HTTPException:
        return None
    username = payload.get("sub")
    if not username:
        return None
    user = UserCRUD.get_user_by_username(db, username)
    if not user or not user.is_active:
        return None
    return user

def require_admin_user(current_user: User = Depends(get_current_user)) -> User:
    role = getattr(current_user, "role", "developer")
    if role not in ["team_lead", "admin"]:
        raise HTTPException(status_code=403, detail="Admin/team lead role required")
    return current_user


def require_reviewer_user(current_user: User = Depends(get_current_user)) -> User:
    """Alias for reviewer-gated endpoints (team_lead/admin)."""
    return require_admin_user(current_user)

def _is_privileged_role(role: Optional[str]) -> bool:
    return (role or "developer") in ["team_lead", "admin"]


def _user_can_view_issues(db: Session, actor: User, repository: Repository) -> bool:
    if _is_privileged_role(getattr(actor, "role", "developer")):
        return True
    accessible_ids = {r.id for r in RepositoryCRUD.get_accessible_repositories_for_user(db, actor)}
    return repository.id in accessible_ids


def _actor_has_issue_only_access(db: Session, actor: Optional[User], repository: Repository) -> bool:
    """True when actor only has issue-scoped access (no commits / no non-issue permission / not manager).

    Used to prevent issue-only users from browsing repo contents (branches/files/docs), even when
    the repository is public in the UI.
    """
    if not actor:
        return False
    if can_manage_repository(db, actor, repository):
        return False
    if db.query(Commit).filter(Commit.repository_id == repository.id, Commit.author_id == actor.id).first():
        return False
    # Non-issue permission present → not issue-only
    if db.query(UserPermission).filter(
        UserPermission.repository_id == repository.id,
        UserPermission.user_id == actor.id,
        UserPermission.issue_id.is_(None),
    ).first():
        return False
    # Any issue-scoped permission → issue-only
    return db.query(UserPermission).filter(
        UserPermission.repository_id == repository.id,
        UserPermission.user_id == actor.id,
        UserPermission.issue_id.isnot(None),
    ).first() is not None


def _user_can_read_repository_contents(db: Session, actor: Optional[User], repository: Repository) -> bool:
    """Permission to read repository *contents* (branches/files/commits/docs)."""
    if not actor:
        return False
    if can_manage_repository(db, actor, repository):
        return True
    if db.query(Commit).filter(Commit.repository_id == repository.id, Commit.author_id == actor.id).first():
        return True
    # Only non-issue-scoped collaborators can read contents.
    return db.query(UserPermission).filter(
        UserPermission.repository_id == repository.id,
        UserPermission.user_id == actor.id,
        UserPermission.issue_id.is_(None),
    ).first() is not None


def _require_repository_read_access(db: Session, actor: User, repository: Repository, action: str) -> None:
    """Enforce read-only access to repository contents/metadata."""
    # Issue-only users are not allowed to browse repo contents.
    if _actor_has_issue_only_access(db, actor, repository):
        raise HTTPException(status_code=403, detail=f"Not allowed to {action} for this repository")
    # Public visibility does not imply authenticated users can browse the repo in the web UI.
    # Only real collaborators (owner/admin, commit authors, non-issue permissions) can read contents.
    if _user_can_read_repository_contents(db, actor, repository):
        return
    raise HTTPException(status_code=403, detail=f"Not allowed to {action} for this repository")


def _require_repository_checkout_access(
    db: Session,
    actor: User,
    repository: Repository,
    branch: Optional[str],
    action: str,
) -> None:
    """Allow fetching code for working on issues, without allowing full repo browsing.

    - Full collaborators (owner/admin, commit authors, non-issue permissions) can fetch any branch.
    - Issue-only users may fetch the default branch only (so they can work on assigned issues),
      but still cannot browse the repo (files/docs/branches) beyond that.
    """
    if _user_can_read_repository_contents(db, actor, repository):
        return

    if _actor_has_issue_only_access(db, actor, repository):
        default_branch = _get_default_branch(db, repository.id).name
        requested = _normalize_branch_name(branch) if branch else default_branch
        if requested == default_branch:
            return
        raise HTTPException(status_code=403, detail=f"Not allowed to {action} for this branch")

    raise HTTPException(status_code=403, detail=f"Not allowed to {action} for this repository")


def _issue_comment_count(db: Session, issue_id: int) -> int:
    return int(db.query(func.count(IssueComment.id)).filter(IssueComment.issue_id == issue_id).scalar() or 0)


def _issue_to_list_dict(db: Session, issue: Issue) -> Dict[str, Any]:
    return {
        "number": issue.number,
        "title": issue.title,
        "status": issue.status,
        "issue_type": issue.issue_type,
        "priority": issue.priority,
        "assigned_to": issue.assigned_to.username if issue.assigned_to else None,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
        "comment_count": _issue_comment_count(db, issue.id),
    }


def _issue_to_detail_dict(db: Session, repo_id: str, issue: Issue) -> Dict[str, Any]:
    labels = []
    for link in issue.label_links or []:
        if link.label:
            labels.append({"name": link.label.name, "color": link.label.color})

    comments_out = []
    for c in sorted(issue.comments or [], key=lambda x: x.created_at or datetime.min):
        comments_out.append({
            "id": c.id,
            "body": c.body,
            "author": c.author.username if c.author else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })

    events_out = []
    for ev in IssueEventCRUD.list_events(db, issue.id, limit=200):
        payload = {}
        if ev.payload_json:
            try:
                payload = json.loads(ev.payload_json)
            except Exception:
                payload = {}
        events_out.append({
            "id": ev.id,
            "type": ev.event_type,
            "actor": ev.actor.username if ev.actor else None,
            "created_at": ev.created_at.isoformat() if ev.created_at else None,
            "payload": payload,
        })

    branches = [{"name": b.branch_name} for b in IssueLinkCRUD.list_linked_branches(db, repo_id, issue.id)]
    prs = []
    for link in IssueLinkCRUD.list_linked_pull_requests(db, issue.id):
        pr = db.query(PullRequest).filter(PullRequest.id == link.pull_request_id).first()
        if pr:
            prs.append({"id": pr.id, "title": pr.title, "status": pr.status, "link_type": link.link_type})
    commits_out = []
    for link in IssueLinkCRUD.list_linked_commits(db, issue.id):
        c = db.query(Commit).filter(Commit.id == link.commit_id).first()
        if c:
            commits_out.append({"id": c.id, "message": c.message, "link_type": link.link_type})

    return {
        "number": issue.number,
        "title": issue.title,
        "description": issue.description,
        "status": issue.status,
        "issue_type": issue.issue_type,
        "priority": issue.priority,
        "created_by": issue.created_by.username if issue.created_by else None,
        "assigned_to": issue.assigned_to.username if issue.assigned_to else None,
        "created_at": issue.created_at.isoformat() if issue.created_at else None,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
        "labels": labels,
        "comments": comments_out,
        "events": events_out,
        "links": {
            "branches": branches,
            "pull_requests": prs,
            "commits": commits_out,
        },
    }


def _issue_permissions_for_actor(db: Session, actor: User, repository: Repository, issue: Issue) -> Dict[str, Any]:
    """Return a lightweight permission matrix for issue actions.

    Good-practice policy:
    - Closing is allowed for repository managers and the issue creator.
    - Reopening remains restricted to repository managers (manage-level).
    - Assignees/creators can move work between open/in_progress/resolved.
    """
    can_manage = can_manage_repository(db, actor, repository)
    is_assignee = bool(issue.assigned_to_id and actor and actor.id == issue.assigned_to_id)
    is_creator = bool(issue.created_by_id and actor and actor.id == issue.created_by_id)
    can_update_work_status = can_manage or is_assignee or is_creator
    return {
        "can_close": bool(can_manage or is_creator),
        "can_reopen": bool(can_manage),
        "can_update_work_status": bool(can_update_work_status),
    }


def _sync_issues_for_new_commit(db: Session, repo_id: str, commit: Commit, branch_name: str, actor_id: int) -> None:
    default_branch = _get_default_branch(db, repo_id)
    is_default = bool(default_branch and default_branch.name == branch_name)
    text = commit.message or ""
    for ref in parse_issue_references(text):
        issue = IssueCRUD.get_issue_by_number(db, repo_id, ref["number"])
        if not issue:
            continue
        IssueLinkCRUD.link_commit(db, issue.id, commit.id, ref["link_type"])
        IssueLinkCRUD.link_branch(db, repo_id, issue.id, branch_name)
        if ref["close"] and is_default:
            try:
                IssueCRUD.transition_status(db, issue, "closed", actor_id=actor_id)
            except ValueError:
                pass


def _sync_issues_for_pr_created(db: Session, repo_id: str, pr: PullRequest) -> None:
    text = f"{pr.title or ''}\n{pr.description or ''}"
    for ref in parse_issue_references(text):
        issue = IssueCRUD.get_issue_by_number(db, repo_id, ref["number"])
        if not issue:
            continue
        IssueLinkCRUD.link_pull_request(db, issue.id, pr.id, ref["link_type"])


def _sync_issues_for_pr_merged(db: Session, repo_id: str, pr: PullRequest, merge_commit: Commit, actor_id: int) -> None:
    text = f"{pr.title or ''}\n{pr.description or ''}\n{merge_commit.message or ''}"
    for ref in parse_issue_references(text):
        if not ref["close"]:
            continue
        issue = IssueCRUD.get_issue_by_number(db, repo_id, ref["number"])
        if not issue:
            continue
        IssueLinkCRUD.link_commit(db, issue.id, merge_commit.id, "closes")
        IssueLinkCRUD.link_pull_request(db, issue.id, pr.id, "closes")
        try:
            IssueCRUD.transition_status(db, issue, "closed", actor_id=actor_id)
        except ValueError:
            pass


def _issue_notification_recipients(issue: Issue, exclude_user_id: Optional[int] = None) -> List[int]:
    """Return user ids to notify for an issue (creator, assignee, watchers)."""
    user_ids: List[int] = []
    for uid in [issue.created_by_id, issue.assigned_to_id]:
        if uid and uid not in user_ids:
            user_ids.append(uid)
    for w in (issue.watchers or []):
        if w and getattr(w, "user_id", None) and w.user_id not in user_ids:
            user_ids.append(w.user_id)
    if exclude_user_id and exclude_user_id in user_ids:
        user_ids = [u for u in user_ids if u != exclude_user_id]
    return user_ids


def _notify_issue_commit_submitted(
    db: Session,
    repo_id: str,
    issue: Issue,
    actor: User,
    pending_commit_id: str,
    branch_name: str,
    message: str,
) -> None:
    # Human-visible audit trail in issue comments + events
    IssueCRUD.add_comment(
        db,
        issue,
        author_id=actor.id,
        body=(
            f"Submitted commit `{pending_commit_id[:12]}` for review on branch `{branch_name}`.\n\n"
            f"Message:\n{message}"
        ),
    )
    IssueEventCRUD.add_event(
        db,
        issue.id,
        "issue_commit_submitted",
        actor_id=actor.id,
        payload={
            "repository_id": repo_id,
            "issue_number": issue.number,
            "pending_commit_id": pending_commit_id,
            "branch": branch_name,
            "message": message,
        },
    )

    payload = {"repository_id": repo_id, "issue_number": issue.number, "branch": branch_name, "pending_commit_id": pending_commit_id}
    for uid in _issue_notification_recipients(issue, exclude_user_id=actor.id):
        NotificationCRUD.create_notification(
            db,
            user_id=uid,
            notification_type="issue_commit_submitted",
            title=f"Issue #{issue.number}: commit submitted for review",
            body=f"@{actor.username} submitted `{pending_commit_id[:12]}` on `{branch_name}`",
            payload={**payload, "actor": actor.username},
            dedupe_key=f"issue:{issue.id}:pending:{pending_commit_id}:submitted",
        )


def _notify_issue_commit_reviewed(
    db: Session,
    repo_id: str,
    issue: Issue,
    reviewer: User,
    commit_id: str,
    branch_name: Optional[str],
    action: str,
    comment: Optional[str] = None,
) -> None:
    action_norm = (action or "").strip().lower()
    if action_norm not in {"approved", "rejected"}:
        action_norm = "reviewed"
    suffix = f"\n\nReviewer note:\n{comment}" if comment else ""

    IssueCRUD.add_comment(
        db,
        issue,
        author_id=reviewer.id,
        body=(
            f"{action_norm.title()} commit `{commit_id[:12]}`"
            + (f" on branch `{branch_name}`" if branch_name else "")
            + f".{suffix}"
        ),
    )
    IssueEventCRUD.add_event(
        db,
        issue.id,
        f"issue_commit_{action_norm}",
        actor_id=reviewer.id,
        payload={
            "repository_id": repo_id,
            "issue_number": issue.number,
            "commit_id": commit_id,
            "branch": branch_name,
            "review_comment": comment,
        },
    )

    payload = {"repository_id": repo_id, "issue_number": issue.number, "branch": branch_name, "commit_id": commit_id, "status": action_norm}
    for uid in _issue_notification_recipients(issue, exclude_user_id=reviewer.id):
        NotificationCRUD.create_notification(
            db,
            user_id=uid,
            notification_type=f"issue_commit_{action_norm}",
            title=f"Issue #{issue.number}: commit {action_norm}",
            body=f"@{reviewer.username} {action_norm} `{commit_id[:12]}`" + (f" on `{branch_name}`" if branch_name else ""),
            payload={**payload, "actor": reviewer.username, "review_comment": comment},
            dedupe_key=f"issue:{issue.id}:commit:{commit_id}:{action_norm}",
        )


def _notify_pending_commit_reviewer(
    db: Session,
    repository: Repository,
    pending_commit: "PendingCommit",
    actor: User,
    team_lead_name: Optional[str],
    branch_name: str,
) -> None:
    """Notify the appropriate reviewer(s) that a pending commit needs review."""
    recipients: List[int] = []

    # Prefer the author's assigned team lead if present.
    if actor.team_lead_id:
        recipients.append(actor.team_lead_id)

    # Fallback to repository owner (common when dev isn't assigned to a team lead).
    if repository.owner_id and repository.owner_id not in recipients:
        recipients.append(repository.owner_id)

    # Always notify admins as a safety net.
    try:
        admin_ids = [
            u.id
            for u in db.query(User).filter(User.role == "admin", User.is_active == True).all()  # noqa: E712
            if u and u.id
        ]
        for uid in admin_ids:
            if uid not in recipients:
                recipients.append(uid)
    except Exception:
        pass

    payload = {
        "repository_id": repository.id,
        "repository_name": repository.name,
        "pending_commit_id": pending_commit.id,
        "branch": branch_name,
        "author": actor.username,
        "team_lead": team_lead_name,
        "actions": [
            {"type": "OPEN_ISSUE", "payload": {"repository_id": repository.id, "issue_number": None}},
        ],
    }

    for uid in recipients:
        if uid == actor.id:
            continue
        NotificationCRUD.create_notification(
            db,
            user_id=uid,
            notification_type="pending_commit_review",
            title=f"Pending commit review: {repository.name}",
            body=f"@{actor.username} submitted `{pending_commit.id[:12]}` on `{branch_name}`",
            payload=payload,
            dedupe_key=f"pending_commit:{pending_commit.id}:review_request:{uid}",
        )


def _maybe_notify_issue_mentions(db: Session, body: str, actor: User, issue: Issue, context: str) -> None:
    if not body:
        return
    for username in parse_mentions(body):
        target = UserCRUD.get_user_by_username(db, username)
        if not target or not target.is_active:
            continue
        NotificationCRUD.create_notification(
            db,
            user_id=target.id,
            notification_type="issue_mention",
            title=f"You were mentioned in {context}",
            body=f"@{actor.username} mentioned you on issue #{issue.number}",
            payload={"repository_id": issue.repository_id, "issue_number": issue.number},
            dedupe_key=None,
        )


def _access_request_reason_text(
    issue: Issue,
    override_description: Optional[str],
    *,
    fallback: str,
) -> str:
    """Use the issue creator's description when present; otherwise a short fallback line."""
    raw = override_description if override_description is not None else (issue.description or "")
    text = (raw or "").strip()
    return text if text else fallback


def _handle_issue_access_requests(db: Session, issue: Issue, creator: User, repository: Repository, description: str) -> None:
    """Create access requests for mentioned users who don't own the repository"""
    if not description:
        return

    mentioned_usernames = parse_mentions(description)
    for username in mentioned_usernames:
        mentioned_user = UserCRUD.get_user_by_username(db, username)
        if not mentioned_user or not mentioned_user.is_active:
            continue
        reason = _access_request_reason_text(
            issue,
            description,
            fallback=f"Tagged in issue #{issue.number}: {issue.title}",
        )
        _maybe_create_issue_access_request_for_user(
            db,
            issue,
            creator,
            repository,
            mentioned_user,
            reason,
        )


def _maybe_create_issue_access_request_for_user(
    db: Session,
    issue: Issue,
    creator: User,
    repository: Repository,
    target_user: User,
    request_reason: str,
) -> None:
    if not target_user or not target_user.is_active:
        return
    if target_user.id == repository.owner_id:
        return
    # IMPORTANT: Issue-scoped permission for other issues must NOT suppress a new access request.
    # Only global (non-issue) collaborators or contributors (commit authors) should bypass requests.
    if db.query(Commit).filter(Commit.repository_id == repository.id, Commit.author_id == target_user.id).first():
        return
    if db.query(UserPermission).filter(
        UserPermission.repository_id == repository.id,
        UserPermission.user_id == target_user.id,
        UserPermission.issue_id.is_(None),
    ).first():
        return

    try:
        access_req = IssueAccessRequestCRUD.create_request(
            db,
            issue_id=issue.id,
            requested_by_id=creator.id,
            requested_user_id=target_user.id,
            repo_id=repository.id,
            request_reason=request_reason,
        )
        if not access_req:
            return

        repo_owner = repository.owner
        if repo_owner:
            NotificationCRUD.create_notification(
                db,
                user_id=repo_owner.id,
                notification_type="issue_access_request_created",
                title="Repository Access Request",
                body=f"@{target_user.username} needs access to '{repository.name}' (issue #{issue.number})",
                payload={
                    "issue_number": issue.number,
                    "issue_title": issue.title,
                    "repository_id": repository.id,
                    "repository_name": repository.name,
                    "requested_user": target_user.username,
                    "access_request_id": access_req.id,
                    "action_required": True,
                    "actions": [
                        {"type": "APPROVE", "url": f"/api/access-requests/{access_req.id}/approve"},
                        {"type": "DENY", "url": f"/api/access-requests/{access_req.id}/deny"},
                        {"type": "REVIEW", "url": f"/repository/{repository.id}/issues/{issue.number}"},
                    ],
                },
                dedupe_key=f"access_req_{access_req.id}_{repo_owner.id}",
            )

        # Notify ONLY the issue creator's reviewer chain (team lead/admin), not all admins.
        try:
            reviewers: List[User] = []
            # Primary: creator's team lead (if set and active)
            if creator and getattr(creator, "team_lead_id", None):
                tl = db.query(User).filter(User.id == creator.team_lead_id).first()
                if tl and tl.is_active:
                    reviewers.append(tl)

            # Secondary: repository owner if they are privileged (common for owner-led review workflows)
            repo_owner = repository.owner
            if not reviewers and repo_owner and repo_owner.is_active and _is_privileged_role(getattr(repo_owner, "role", "developer")):
                reviewers.append(repo_owner)

            # If creator is already privileged, notify them (they can approve)
            if creator and _is_privileged_role(getattr(creator, "role", "developer")):
                if creator.is_active and all(r.id != creator.id for r in reviewers):
                    reviewers.append(creator)

            # Fallback: one admin (lowest id) so requests never get stuck
            if not reviewers:
                admin = db.query(User).filter(User.is_active == True, User.role == "admin").order_by(User.id.asc()).first()  # noqa: E712
                if admin:
                    reviewers.append(admin)

            for reviewer in reviewers:
                if not reviewer or not reviewer.id:
                    continue
                # Don't double-notify the owner notification (owner gets a separate, owner-scoped request)
                if repo_owner and reviewer.id == repo_owner.id:
                    continue
                NotificationCRUD.create_notification(
                    db,
                    user_id=reviewer.id,
                    notification_type="issue_access_request_created",
                    title="Repository Access Request",
                    body=f"@{target_user.username} needs access to '{repository.name}' (issue #{issue.number})",
                    payload={
                        "issue_number": issue.number,
                        "issue_title": issue.title,
                        "repository_id": repository.id,
                        "repository_name": repository.name,
                        "requested_user": target_user.username,
                        "requested_by": creator.username if creator else None,
                        "access_request_id": access_req.id,
                        "action_required": True,
                        "actions": [
                            {"type": "APPROVE", "url": f"/api/access-requests/{access_req.id}/approve"},
                            {"type": "DENY", "url": f"/api/access-requests/{access_req.id}/deny"},
                            {"type": "REVIEW", "url": f"/repository/{repository.id}/issues/{issue.number}"},
                        ],
                    },
                    dedupe_key=f"access_req_{access_req.id}_{reviewer.id}",
                )
        except Exception:
            pass

        NotificationCRUD.create_notification(
            db,
            user_id=target_user.id,
            notification_type="issue_access_request_pending",
            title="Access Request Pending",
            body=f"You were mentioned or assigned to issue #{issue.number} for '{repository.name}'. Awaiting approval from repository owner.",
            payload={
                "issue_number": issue.number,
                "repository_id": repository.id,
                "repository_name": repository.name,
                "access_request_id": access_req.id,
                "status": "pending",
            },
            dedupe_key=f"access_req_user_{access_req.id}_{target_user.id}",
        )
    except Exception as e:
        print(f"Error creating access request for {target_user.username}: {str(e)}")
        return


@app.post("/api/auth/login")
async def login(request: LoginRequest, db: Session = Depends(get_db)):
    user = UserCRUD.get_user_by_username(db, request.username)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User is inactive")
    if not user.password_hash:
        raise HTTPException(status_code=403, detail="Password not set. Use bootstrap password setup.")
    if not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    # Track last login for admin engagement monitoring
    try:
        user.last_login_at = datetime.utcnow()
        db.commit()
        db.refresh(user)
    except Exception:
        db.rollback()

    token = create_access_token(user.username, getattr(user, "role", "developer"))
    return {
        "success": True,
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "team_lead_id": user.team_lead_id,
            "is_active": user.is_active,
            "last_login_at": user.last_login_at.isoformat() if getattr(user, "last_login_at", None) else None,
        }
    }

@app.get("/api/auth/me")
async def auth_me(current_user: User = Depends(get_current_user)):
    return {
        "success": True,
        "user": {
            "id": current_user.id,
            "username": current_user.username,
            "email": current_user.email,
            "full_name": current_user.full_name,
            "role": current_user.role,
            "team_lead_id": current_user.team_lead_id,
            "is_active": current_user.is_active
        }
    }

@app.post("/api/auth/change-password")
async def change_password(request: ChangePasswordRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not request.new_password or len(request.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    if current_user.password_hash and not verify_password(request.current_password or "", current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    current_user.password_hash = hash_password(request.new_password)
    db.commit()
    return {"success": True, "message": "Password updated successfully"}

@app.post("/api/auth/bootstrap-password")
async def bootstrap_password(request: BootstrapPasswordRequest, db: Session = Depends(get_db)):
    if not PASSWORD_SETUP_KEY:
        raise HTTPException(status_code=403, detail="Password bootstrap is disabled")
    if request.setup_key != PASSWORD_SETUP_KEY:
        raise HTTPException(status_code=403, detail="Invalid setup key")
    if not request.new_password or len(request.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    user = UserCRUD.get_user_by_username(db, request.username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.password_hash:
        raise HTTPException(status_code=400, detail="Password already set for this user")

    user.password_hash = hash_password(request.new_password)
    db.commit()
    return {"success": True, "message": f"Password initialized for {user.username}"}

@app.post("/api/auth/register-request")
async def register_request(request: RegistrationRequest, db: Session = Depends(get_db)):
    """Create a self-registration request that requires admin approval."""
    username = request.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    if not request.password or len(request.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    requested_role = (request.requested_role or 'developer').strip().lower()
    if requested_role not in ['developer', 'team_lead']:
        raise HTTPException(status_code=400, detail="requested_role must be developer or team_lead")

    if UserCRUD.get_user_by_username(db, username):
        raise HTTPException(status_code=400, detail="Username already exists")

    existing_pending = PendingUserRegistrationCRUD.get_by_username(db, username, status='pending')
    if existing_pending:
        raise HTTPException(status_code=400, detail="A pending registration already exists for this username")

    password_hash = hash_password(request.password)
    pending = PendingUserRegistrationCRUD.create_request(
        db,
        username=username,
        password_hash=password_hash,
        email=request.email,
        full_name=request.full_name,
        requested_role=requested_role,
        requested_team_lead_username=request.requested_team_lead_username
    )

    return {
        "success": True,
        "message": "Registration request submitted. Awaiting admin approval.",
        "request_id": pending.id,
        "status": pending.status
    }

# Notification endpoints
@app.get("/api/notifications")
async def list_notifications(
    unread_only: bool = False,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List notifications for the current user."""
    notifications = NotificationCRUD.list_notifications(
        db,
        user_id=current_user.id,
        unread_only=unread_only,
        limit=limit
    )
    
    return {
        "success": True,
        "notifications": [
            {
                "id": n.id,
                "type": n.notification_type,
                "title": n.title,
                "body": n.body,
                "payload": json.loads(n.payload_json) if n.payload_json else {},
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat() if n.created_at else None,
                "read_at": n.read_at.isoformat() if n.read_at else None
            }
            for n in notifications
        ]
    }

@app.post("/api/notifications/mark-read")
async def mark_notifications_read(
    request: MarkNotificationsReadRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Mark notifications as read."""
    if not request.ids:
        raise HTTPException(status_code=400, detail="ids required")
    
    updated_count = NotificationCRUD.mark_read(
        db,
        user_id=current_user.id,
        notification_ids=request.ids
    )
    
    return {
        "success": True,
        "updated": updated_count
    }


@app.get("/api/admin/pending-counts")
async def get_pending_counts(
    current_user: User = Depends(require_reviewer_user),
    db: Session = Depends(get_db),
):
    """Counts for sidebar/dashboard badges (pending commits/repos/users + access requests)."""
    role = (getattr(current_user, "role", "") or "").lower()

    # Pending commits
    pending_commits = PendingCommitCRUD.get_all_pending_commits(db, status="pending")
    if role == "team_lead":
        # Same policy as /api/pending-commits: team leads see their developers + repos they own.
        pending_commits = [
            pc
            for pc in pending_commits
            if (
                (pc.author and pc.author.team_lead_id == current_user.id)
                or (pc.repository and pc.repository.owner_id == current_user.id)
            )
        ]
    pending_commits_count = len(pending_commits)

    # Access requests
    try:
        if role == "admin":
            access_requests_count = len(IssueAccessRequestCRUD.list_pending_requests(db, repo_id=None, status="pending", limit=1000))
        elif role == "team_lead":
            from database.models import IssueAccessRequest
            access_requests_count = int(
                db.query(IssueAccessRequest)
                .join(User, User.id == IssueAccessRequest.requested_by_id)
                .filter(IssueAccessRequest.status == "pending", User.team_lead_id == current_user.id)
                .count()
            )
        else:
            user_repos = db.query(Repository).filter(Repository.owner_id == current_user.id).all()
            repo_ids = [r.id for r in user_repos]
            access_requests_count = 0
            for rid in repo_ids:
                access_requests_count += len(IssueAccessRequestCRUD.list_pending_requests(db, repo_id=rid, status="pending"))
    except Exception:
        access_requests_count = 0

    # Pending repositories (admin sees all; team_lead sees theirs)
    try:
        pending_repos = PendingRepositoryCRUD.get_all_pending_repositories(db, status="pending")
        if role == "team_lead":
            pending_repos = [pr for pr in pending_repos if pr.team_lead and pr.team_lead.username == current_user.username]
        pending_repos_count = len(pending_repos)
    except Exception:
        pending_repos_count = 0

    # Pending user registrations (admin only)
    pending_users_count = 0
    if role == "admin":
        try:
            pending_users_count = len(PendingUserRegistrationCRUD.list_requests(db, status="pending"))
        except Exception:
            pending_users_count = 0

    return {
        "success": True,
        "counts": {
            "pending_commits": pending_commits_count,
            "access_requests": access_requests_count,
            "pending_repositories": pending_repos_count,
            "pending_users": pending_users_count,
        },
    }

@app.get("/api/admin/pending-user-registrations")
async def list_pending_user_registrations(
    status: str = 'pending',
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db)
):
    requests = PendingUserRegistrationCRUD.list_requests(db, status)
    return {
        "success": True,
        "pending_registrations": [
            {
                "id": req.id,
                "username": req.username,
                "email": req.email,
                "full_name": req.full_name,
                "requested_role": req.requested_role,
                "requested_team_lead_username": req.requested_team_lead_username,
                "status": req.status,
                "review_comment": req.review_comment,
                "reviewed_by": req.reviewer.username if req.reviewer else None,
                "reviewed_at": req.reviewed_at.isoformat() if req.reviewed_at else None,
                "created_at": req.created_at.isoformat() if req.created_at else None,
            }
            for req in requests
        ]
    }

@app.post("/api/admin/pending-user-registrations/{request_id}/review")
async def review_pending_user_registration(
    request_id: int,
    request: ReviewRegistrationRequest,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db)
):
    pending = PendingUserRegistrationCRUD.get_by_id(db, request_id)
    if not pending:
        raise HTTPException(status_code=404, detail="Pending registration not found")

    if pending.status != 'pending':
        raise HTTPException(status_code=400, detail=f"Request is already {pending.status}")

    action = (request.action or '').strip().lower()
    if action not in ['approve', 'reject']:
        raise HTTPException(status_code=400, detail="action must be approve or reject")

    if action == 'reject':
        pending.status = 'rejected'
        pending.review_comment = request.comment
        pending.reviewed_by_id = current_user.id
        pending.reviewed_at = datetime.utcnow()
        db.commit()
        return {"success": True, "status": "rejected", "message": "Registration request rejected"}

    if UserCRUD.get_user_by_username(db, pending.username):
        raise HTTPException(status_code=400, detail="Username already exists")

    final_role = (request.role or pending.requested_role or 'developer').strip().lower()
    if final_role not in ['developer', 'team_lead']:
        raise HTTPException(status_code=400, detail="Role must be developer or team_lead")

    team_lead_id = request.team_lead_id
    if final_role == 'developer' and team_lead_id is None and pending.requested_team_lead_username:
        team_lead = UserCRUD.get_user_by_username(db, pending.requested_team_lead_username)
        if team_lead and team_lead.role == 'team_lead':
            team_lead_id = team_lead.id

    if team_lead_id is not None:
        team_lead = UserCRUD.get_user_by_id(db, team_lead_id)
        if not team_lead or team_lead.role != 'team_lead':
            raise HTTPException(status_code=400, detail="Selected team lead is invalid")

    user = UserCRUD.create_user(
        db,
        username=pending.username,
        email=pending.email,
        full_name=pending.full_name,
        role=final_role,
        team_lead_id=team_lead_id,
        password_hash=pending.password_hash
    )

    pending.status = 'approved'
    pending.review_comment = request.comment
    pending.reviewed_by_id = current_user.id
    pending.reviewed_at = datetime.utcnow()
    db.commit()

    return {
        "success": True,
        "status": "approved",
        "message": "Registration request approved and user created",
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "team_lead_id": user.team_lead_id,
        }
    }

# API Endpoints
@app.get("/")
async def health_check():
    """Health check endpoint"""
    return {"status": "FoxNest Server v2.0 is running with SQL Database", "version": "2.0.0"}

@app.get("/api/download/client")
async def download_client():
    """Download the latest Fox client (fox.py)"""
    base = Path(__file__).parent.parent / "client"
    for rel in ("client1/fox.py", "fox.py"):
        client_path = base / rel
        if client_path.exists():
            return FileResponse(
                path=str(client_path),
                filename="fox.py",
                media_type="text/x-python",
                headers={"X-Fox-Client-Version": "1.0.5"},
            )
    raise HTTPException(status_code=404, detail="Client file not found")

@app.get("/api/download/fox.bat")
async def download_fox_bat():
    """Download a Windows .bat launcher that runs the local fox.py with Python"""
    bat_content = (
        "@echo off\r\n"
        ":: FoxNest client launcher - runs fox.py from the same directory\r\n"
        ":: Place this file alongside fox.py (e.g. C:\\Users\\bee56\\Desktop\\admins\\)\r\n"
        "setlocal\r\n"
        "set SCRIPT_DIR=%~dp0\r\n"
        "set FOX_PY=%SCRIPT_DIR%fox.py\r\n"
        "if not exist \"%FOX_PY%\" (\r\n"
        "    echo ERROR: fox.py not found in %SCRIPT_DIR%\r\n"
        "    echo Run: Invoke-WebRequest -Uri http://192.168.15.207:33333/api/download/client -OutFile fox.py\r\n"
        "    exit /b 1\r\n"
        ")\r\n"
        "python \"%FOX_PY%\" %*\r\n"
    )
    from fastapi.responses import Response
    return Response(
        content=bat_content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=fox.bat"}
    )

@app.get("/api/")
async def api_root():
    """API root endpoint"""
    return {"status": "FoxNest API v2.0 is running", "version": "2.0.0", "endpoints": [
        "/api/repository/create",
        "/api/repository/list",
        "/api/repositories/all",
        "/api/repository/{repo_id}",
        "/api/repository/{repo_id}/push",
        "/api/repository/{repo_id}/pull",
        "/api/repository/{repo_id}/commits",
        "/api/users",
        "/api/activities",
        "/api/download/client"
    ]}

@app.post("/api/repository/create")
async def create_repository(request: CreateRepositoryRequest, db: Session = Depends(get_db)):
    """Create a new repository"""
    repo_name = (request.repo_name or "").strip()
    if not request.username or not repo_name:
        raise HTTPException(status_code=400, detail="Username and repo_name required")
    
    try:
        # Check if user exists - DO NOT auto-create
        user = UserCRUD.get_user_by_username(db, request.username)
        if not user:
            raise HTTPException(
                status_code=403, 
                detail=f"User '{request.username}' does not exist. Please contact an administrator to create your account."
            )
        
        # Check if user is active
        if not user.is_active:
            raise HTTPException(
                status_code=403,
                detail=f"User '{request.username}' is inactive. Please contact an administrator."
            )
        
        # Determine the owner based on user role FIRST
        owner_username = request.username
        user_role = getattr(user, 'role', 'developer')
        
        if user_role == 'developer' and user.team_lead:
            # For developers, the team lead is the owner
            owner_username = user.team_lead.username
        
        # Generate repo_id with the OWNER's username (not the requester)
        repo_id = RepositoryCRUD.generate_repo_id(owner_username, repo_name)
        
        # Check if repository already exists (approved)
        existing_repo = RepositoryCRUD.get_repository(db, repo_id)
        if existing_repo:
            return {
                "success": True,
                "repo_id": existing_repo.id,
                "owner": existing_repo.owner.username,
                "message": "Repository already exists"
            }

        # Developer-approved repos are owned by the requester (not the team lead).
        requester_repo_id = RepositoryCRUD.generate_repo_id(request.username, repo_name)
        existing_requester_repo = RepositoryCRUD.get_repository(db, requester_repo_id)
        if existing_requester_repo:
            return {
                "success": True,
                "repo_id": existing_requester_repo.id,
                "owner": existing_requester_repo.owner.username,
                "message": "Repository already exists"
            }
        
        if user_role == 'developer' and user.team_lead:
            # Developer with team lead - team lead becomes owner
            owner_username = user.team_lead.username
            print(f"DEBUG - Repository creation: {request.username} (developer) requesting repo, owner will be team lead: {owner_username}")
            
            # Check if pending request already exists for this repo
            existing_pending = db.query(PendingRepository).filter(
                PendingRepository.repo_name == repo_name,
                PendingRepository.requested_by_id == user.id,
                PendingRepository.status == 'pending'
            ).first()
            
            if existing_pending:
                return {
                    "success": True,
                    "status": "pending_approval",
                    "team_lead": owner_username,
                    "pending_id": existing_pending.id,
                    "message": f"Repository creation request already pending approval from {owner_username}."
                }
            
            # For developers, create a pending repository request instead
            pending_repo = PendingRepositoryCRUD.create_pending_repository(
                db,
                repo_name=repo_name,
                description=request.description,
                requested_by_username=request.username,
                owner_username=owner_username
            )
            
            # Create activity for the request
            ActivityCRUD.create_activity(
                db, user.id, "request_repository", 
                f"Requested repository {repo_name}", None
            )
            
            return {
                "success": True,
                "status": "pending_approval",
                "team_lead": owner_username,
                "pending_id": pending_repo.id,
                "message": f"Repository creation request submitted. Awaiting approval from {owner_username}."
            }
        else:
            # Team lead or user without team lead can create directly
            print(f"DEBUG - Repository creation: {request.username} creating repo as owner (role: {user_role})")
            
            repository = RepositoryCRUD.create_repository(
                db, owner_username, repo_name, request.description
            )
            
            # Create activity
            ActivityCRUD.create_activity(
                db, user.id, "create_repository", 
                f"Created repository {repo_name}", repository.id
            )
            
            return {
                "success": True, 
                "repo_id": repository.id,
                "owner": owner_username,
                "message": None
            }
    
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/api/repository/list")
async def list_repositories(
    username: str,
    repo_name: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List repositories for a user"""
    if not username:
        raise HTTPException(status_code=400, detail="Username required")

    # Developers can only query their own repositories.
    if not _is_privileged_role(getattr(current_user, "role", "developer")) and username != current_user.username:
        raise HTTPException(status_code=403, detail="You can only view repositories you own")
    
    try:
        if repo_name:
            # Get specific repository
            repo_id = RepositoryCRUD.generate_repo_id(username, repo_name)
            repository = RepositoryCRUD.get_repository(db, repo_id)
            repositories = [repository] if repository else []
        else:
            target_user = UserCRUD.get_user_by_username(db, username)
            if target_user and not _is_privileged_role(getattr(current_user, "role", "developer")):
                # Owned + real collaboration (commits) + non-issue-hidden permissions
                repositories = RepositoryCRUD.get_repositories_for_web_browser(db, target_user)
            else:
                repositories = RepositoryCRUD.get_repositories_by_user(db, username)
        
        return {
            "success": True, 
            "repositories": [repository_to_dict(repo, db=db, actor=current_user) for repo in repositories]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/api/repositories/all")
async def list_all_repositories(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """List repositories by role scope (all for team lead/admin, accessible ones for developers)."""
    try:
        if _is_privileged_role(getattr(current_user, "role", "developer")):
            repositories = RepositoryCRUD.get_all_repositories(db)
        else:
            repositories = RepositoryCRUD.get_repositories_for_web_browser(db, current_user)

        return {
            "success": True, 
            "repositories": [repository_to_dict(repo, db=db, actor=current_user) for repo in repositories]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/api/repository/{repo_id}")
async def get_repository(
    repo_id: str,
    db: Session = Depends(get_db),
    actor: Optional[User] = Depends(get_optional_user),
):
    """Get repository information (stats + starred_by_me when client sends Bearer token)."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    return {"success": True, "repository": repository_to_dict(repository, db=db, actor=actor)}

@app.delete("/api/repository/{repo_id}")
async def delete_repository(repo_id: str, actor_username: Optional[str] = None, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Delete a repository permanently"""
    try:
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        resolved_actor = current_user.username if current_user else actor_username
        require_repository_access(db, resolved_actor, repository, "delete", required_scope="manage")
        
        repo_name = repository.name
        owner_name = repository.owner.username
        
        # Delete the repository
        RepositoryCRUD.delete_repository(db, repo_id)
        
        return {
            "success": True,
            "message": f"Repository '{repo_name}' owned by '{owner_name}' has been permanently deleted"
        }
    
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/api/repository/{repo_id}/archive")
async def archive_repository(repo_id: str, reason: Optional[str] = None, actor_username: Optional[str] = None, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Archive a repository (move to archive without deleting)"""
    try:
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        resolved_actor = current_user.username if current_user else actor_username
        require_repository_access(db, resolved_actor, repository, "archive", required_scope="manage")
        
        if repository.is_archived:
            raise HTTPException(status_code=400, detail="Repository is already archived")
        
        repo_name = repository.name
        owner_name = repository.owner.username
        
        # Archive the repository
        RepositoryCRUD.archive_repository(db, repo_id, reason=reason or "Archived via web interface")
        
        return {
            "success": True,
            "message": f"Repository '{repo_name}' owned by '{owner_name}' has been archived",
            "repo_id": repo_id
        }
    
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/api/repository/{repo_id}/push")
async def push_commit(repo_id: str, request: PushCommitRequest, db: Session = Depends(get_db)):
    """Push a commit to repository"""
    if not request.commit:
        raise HTTPException(status_code=400, detail="Commit data required")
    
    try:
        # Ensure repository exists
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")
        
        # Get the author username from commit data
        author_username = request.commit.get("author")
        if not author_username:
            raise HTTPException(status_code=400, detail="Commit author required")

        # Pusher identity can differ from commit author for rollback/history operations
        pusher_username = request.pusher or author_username
        
        # Check if repository is archived and user is trying regular push
        if repository.is_archived and not request.archive:
            raise HTTPException(
                status_code=400, 
                detail="Repository is archived. Use 'fox push --archive' to push to archived repository."
            )
        
        # Get the pusher user to check role and permissions
        user = UserCRUD.get_user_by_username(db, pusher_username)
        if not user:
            raise HTTPException(
                status_code=403,
                detail=f"User '{pusher_username}' does not exist. Please contact an administrator to create your account."
            )

        user_role = getattr(user, 'role', 'developer')
        
        # Resolve branch name (default if not specified)
        if request.branch:
            branch_name = _normalize_branch_name(request.branch)
        else:
            branch_name = _get_default_branch(db, repo_id).name

        current_branch = BranchCRUD.get_branch(db, repo_id, branch_name)
        actual_head = current_branch.head_commit_id if current_branch else None
        if request.expected_head_commit_id is not None and request.expected_head_commit_id != actual_head:
            _raise_head_mismatch(branch_name, request.expected_head_commit_id, actual_head)

        is_rollback_push = branch_name.startswith("rollback_")

        # Prevent non-privileged users from pushing someone else's regular commits
        if (
            pusher_username != author_username
            and not is_rollback_push
            and user_role not in ['team_lead', 'admin']
        ):
            raise HTTPException(
                status_code=403,
                detail="Only team leads/admins can push commits authored by another user on non-rollback branches."
            )

        # Check basic permissions (owner or has permission) using pusher identity
        is_owner = repository.owner.username == pusher_username
        has_write_permission = UserPermissionCRUD.has_permission(db, pusher_username, repo_id, 'write')
        
        if not is_owner and not has_write_permission:
            raise HTTPException(
                status_code=403, 
                detail=f"User '{pusher_username}' does not have permission to push to this repository. Please contact the repository owner or an administrator."
            )

        # Add repository_id to commit data
        commit_data = request.commit.copy()
        commit_data["repository_id"] = repo_id
        commit_data["branch"] = branch_name
        
        # Check if user role is 'developer' - developers always need approval regardless of ownership or permissions
        # Team leads can push directly
        user_is_developer = user_role == 'developer'
        
        # Debug logging
        print(f"DEBUG - Push request:")
        print(f"  - Branch: {branch_name}")
        print(f"  - Commit author: {author_username}")
        print(f"  - Pusher: {pusher_username}")
        print(f"  - User role: {user_role}")
        print(f"  - Is developer: {user_is_developer}")
        print(f"  - Is owner: {is_owner}")
        
        # If user is a developer, they ALWAYS need approval, regardless of being owner
        if user_is_developer:
            # Developer - create pending commit for approval
            pending_commit = PendingCommitCRUD.create_pending_commit(db, commit_data)
            
            # Get team lead info
            team_lead_name = None
            if user.team_lead:
                team_lead_name = user.team_lead.username
            
            # Create activity
            ActivityCRUD.create_activity(
                db, user.id, "pending_commit", 
                f"Created pending commit: {pending_commit.message[:50]}...", repo_id
            )

            # Notify reviewer(s) so admins/team leads see it immediately in the bell + counts.
            try:
                _notify_pending_commit_reviewer(
                    db,
                    repository=repository,
                    pending_commit=pending_commit,
                    actor=user,
                    team_lead_name=team_lead_name,
                    branch_name=branch_name,
                )
            except Exception:
                pass

            # Link issue/branch + notify issue creator/assignee/watchers immediately on submission.
            try:
                for ref in parse_issue_references(pending_commit.message or ""):
                    issue = IssueCRUD.get_issue_by_number(db, repo_id, ref["number"])
                    if not issue:
                        continue
                    IssueLinkCRUD.link_branch(db, repo_id, issue.id, branch_name)
                    _notify_issue_commit_submitted(
                        db,
                        repo_id=repo_id,
                        issue=issue,
                        actor=user,
                        pending_commit_id=pending_commit.id,
                        branch_name=branch_name,
                        message=pending_commit.message or "",
                    )
            except Exception:
                # Best-effort notifications; never fail the push because of this.
                pass
            
            return {
                "success": True, 
                "commit_id": pending_commit.id, 
                "status": "pending_approval",
                "team_lead": team_lead_name,
                "reviewer_hint": team_lead_name or (repository.owner.username if repository and repository.owner else None) or "team lead/admin",
                "message": (
                    f"Your {'rollback ' if is_rollback_push else ''}commit has been submitted for review by "
                    f"{team_lead_name or 'the team lead/admin'}."
                )
            }
        else:
            # Team lead or other role - check permissions for direct push
            is_team_lead = UserPermissionCRUD.has_permission(db, pusher_username, repo_id, 'team_lead')
            
            print(f"  - Has team_lead permission: {is_team_lead}")
            
            if not is_team_lead and not is_owner:
                raise HTTPException(
                    status_code=403,
                    detail=f"User '{pusher_username}' does not have team lead permission to push directly."
                )
            
            # Team lead or owner with non-developer role can push directly
            commit = CommitCRUD.create_commit(db, commit_data)

            # Update branch head (create if new branch is pushed)
            branch = current_branch or BranchCRUD.get_branch(db, repo_id, branch_name)
            if not branch:
                BranchCRUD.create_branch(db, repo_id, branch_name, head_commit_id=commit.id)
            else:
                BranchCRUD.update_branch_head(db, repo_id, branch_name, commit.id)
            
            # Create activity
            ActivityCRUD.create_activity(
                db, user.id, "push_commit", 
                f"Pushed commit to '{branch_name}' from {actual_head or 'None'} to {commit.id}: {commit.message[:50]}...", repo_id
            )

            _sync_issues_for_new_commit(db, repo_id, commit, branch_name, user.id)
            
            # Handle archiving based on flag
            if request.archive:
                RepositoryCRUD.archive_repository(db, repo_id, reason="Archived via push --archive command")
            
            return {"success": True, "commit_id": commit.id, "status": "merged"}
    
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        # Log and raise other exceptions
        import traceback
        print(f"Error in push_commit: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/api/repository/{repo_id}/pull")
async def pull_commits(
    repo_id: str,
    since_commit: Optional[str] = None,
    branch: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Pull commits from repository"""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    _require_repository_checkout_access(db, current_user, repository, branch, "pull commits")
    
    try:
        commits = CommitCRUD.get_commits_by_repository(db, repo_id)

        if branch:
            resolved_branch = _resolve_branch(db, repo_id, branch)
            reachable = set(_collect_reachable_commits(db, resolved_branch.head_commit_id))
            commits = [commit for commit in commits if commit.id in reachable]
        
        # Filter commits if since_commit is provided
        if since_commit:
            filtered_commits = []
            for commit in commits:
                if commit.id == since_commit:
                    break
                filtered_commits.append(commit)
            commits = filtered_commits
        
        return {
            "success": True, 
            "commits": [commit_to_dict(commit, include_files=True) for commit in commits],
            "head": repository.head_commit_id if not branch else _resolve_branch(db, repo_id, branch).head_commit_id
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/api/repository/{repo_id}/commits")
async def get_commits(
    repo_id: str,
    full: bool = False,
    branch: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get commit history"""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_checkout_access(db, current_user, repository, branch, "view commits")
    
    try:
        commits = CommitCRUD.get_commits_by_repository(db, repo_id)
        if branch:
            resolved_branch = _resolve_branch(db, repo_id, branch)
            reachable = set(_collect_reachable_commits(db, resolved_branch.head_commit_id))
            commits = [commit for commit in commits if commit.id in reachable]
        return {
            "success": True, 
            "commits": [commit_to_dict(commit, include_files=full) for commit in commits]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/api/repository/{repo_id}/files")
async def get_repository_files(
    repo_id: str,
    include_content: bool = True,
    branch: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get all files in repository from latest commit or pending commit"""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view repository files")
    
    try:
        files_dict = {}
        folders_set = set()
        commit_id = None
        commit_message = None
        is_pending = False
        
        selected_commit = _get_commit_for_branch(db, repository, branch)
        if selected_commit:
            commit_id = selected_commit.id
            commit_message = selected_commit.message
            payload = _build_files_payload_from_commit(selected_commit, include_content)
            files_dict = payload["files"]
            folders_set = set(payload["folders"])
        
        # If no approved commits, check for pending commits
        if not files_dict and not branch:
            from database.models import PendingCommit, FileObject
            pending_commits = db.query(PendingCommit).filter(
                PendingCommit.repository_id == repo_id,
                PendingCommit.status == 'pending'
            ).order_by(PendingCommit.created_at.desc()).all()
            
            if pending_commits:
                # Get files from the latest pending commit
                latest_pending = pending_commits[0]
                commit_id = latest_pending.id
                commit_message = latest_pending.message
                is_pending = True
                
                if latest_pending.files:
                    # New format: pending commit files stored by hash
                    for pending_file in latest_pending.files:
                        file_path = pending_file.file_path.replace('\\', '/')
                        path_parts = file_path.split('/')
                        for i in range(len(path_parts) - 1):
                            folder_path = '/'.join(path_parts[:i+1])
                            folders_set.add(folder_path)
                        
                        file_object = db.query(FileObject).filter(FileObject.hash == pending_file.file_hash).first()
                        if not file_object:
                            continue
                        
                        file_entry = {
                            "size": pending_file.file_size,
                            "hash": pending_file.file_hash,
                            "mime_type": file_object.mime_type,
                            "is_binary": None
                        }

                        if include_content:
                            try:
                                content = file_object.content.decode('utf-8')
                                is_binary = False
                            except UnicodeDecodeError:
                                content = base64.b64encode(file_object.content).decode('utf-8')
                                is_binary = True

                            file_entry["content"] = content
                            file_entry["is_binary"] = is_binary

                        files_dict[file_path] = file_entry
                else:
                    # Legacy format: files_data contains base64 content
                    import json
                    files_data = json.loads(latest_pending.files_data) if latest_pending.files_data else {}
                    
                    for file_path, file_content_b64 in files_data.items():
                        file_path = file_path.replace('\\', '/')
                        # Extract folder structure
                        path_parts = file_path.split('/')
                        for i in range(len(path_parts) - 1):
                            folder_path = '/'.join(path_parts[:i+1])
                            folders_set.add(folder_path)
                        
                        try:
                            # Decode base64 content
                            content_bytes = base64.b64decode(file_content_b64.encode())

                            file_entry = {
                                "size": len(content_bytes),
                                "hash": None,
                                "mime_type": None,
                                "is_binary": None
                            }

                            if include_content:
                                try:
                                    content = content_bytes.decode('utf-8')
                                    is_binary = False
                                except UnicodeDecodeError:
                                    content = file_content_b64
                                    is_binary = True

                                file_entry["content"] = content
                                file_entry["is_binary"] = is_binary

                            files_dict[file_path] = file_entry
                        except Exception as e:
                            print(f"Error processing file {file_path}: {e}")
                            continue
        
        if not files_dict:
            return {
                "success": True,
                "files": {},
                "folders": [],
                "message": "Repository is empty"
            }
        
        return {
            "success": True,
            "files": files_dict,
            "folders": sorted(list(folders_set)),
            "commit_id": commit_id,
            "commit_message": commit_message,
            "is_pending": is_pending
        }
    
    except Exception as e:
        import traceback
        print(f"Error in get_repository_files: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/api/repository/{repo_id}/file")
async def get_repository_file(
    repo_id: str,
    path: str,
    branch: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a single file from the latest commit or pending commit"""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view repository file")

    normalized_path = path.replace('\\', '/')
    path_variants = {normalized_path, normalized_path.replace('/', '\\')}

    try:
        selected_commit = _get_commit_for_branch(db, repository, branch)
        if selected_commit:
                commit_file = db.query(CommitFile).filter(
                    CommitFile.commit_id == selected_commit.id,
                    CommitFile.file_path.in_(list(path_variants))
                ).first()

                if commit_file and commit_file.file_object:
                    try:
                        content = commit_file.file_object.content.decode('utf-8')
                        is_binary = False
                    except UnicodeDecodeError:
                        content = base64.b64encode(commit_file.file_object.content).decode('utf-8')
                        is_binary = True

                    return {
                        "success": True,
                        "file": {
                            "path": normalized_path,
                            "content": content,
                            "is_binary": is_binary,
                            "size": commit_file.file_size,
                            "hash": commit_file.file_hash,
                            "mime_type": commit_file.file_object.mime_type
                        }
                    }

        # Fall back to latest pending commit only when branch is not explicitly selected
        from database.models import PendingCommit, FileObject
        pending_commits = []
        if not branch:
            pending_commits = db.query(PendingCommit).filter(
                PendingCommit.repository_id == repo_id,
                PendingCommit.status == 'pending'
            ).order_by(PendingCommit.created_at.desc()).all()

        if pending_commits:
            latest_pending = pending_commits[0]

            if latest_pending.files:
                pending_file = db.query(PendingCommitFile).filter(
                    PendingCommitFile.pending_commit_id == latest_pending.id,
                    PendingCommitFile.file_path.in_(list(path_variants))
                ).first()

                if pending_file:
                    file_object = db.query(FileObject).filter(FileObject.hash == pending_file.file_hash).first()
                    if file_object:
                        try:
                            content = file_object.content.decode('utf-8')
                            is_binary = False
                        except UnicodeDecodeError:
                            content = base64.b64encode(file_object.content).decode('utf-8')
                            is_binary = True

                        return {
                            "success": True,
                            "file": {
                                "path": normalized_path,
                                "content": content,
                                "is_binary": is_binary,
                                "size": pending_file.file_size,
                                "hash": pending_file.file_hash,
                                "mime_type": file_object.mime_type
                            }
                        }
            else:
                import json
                files_data = json.loads(latest_pending.files_data) if latest_pending.files_data else {}
                file_content_b64 = None
                for file_path, file_content in files_data.items():
                    if file_path.replace('\\', '/') == normalized_path:
                        file_content_b64 = file_content
                        break

                if file_content_b64:
                    content_bytes = base64.b64decode(file_content_b64.encode())
                    try:
                        content = content_bytes.decode('utf-8')
                        is_binary = False
                    except UnicodeDecodeError:
                        content = file_content_b64
                        is_binary = True

                    return {
                        "success": True,
                        "file": {
                            "path": normalized_path,
                            "content": content,
                            "is_binary": is_binary,
                            "size": len(content_bytes),
                            "hash": None,
                            "mime_type": None
                        }
                    }

        raise HTTPException(status_code=404, detail="File not found")

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"Error in get_repository_file: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/api/repository/{repo_id}/file/download")
async def download_repository_file(
    repo_id: str,
    path: str,
    branch: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download a single file from the latest commit or pending commit"""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "download repository file")

    normalized_path = path.replace('\\', '/')
    path_variants = {normalized_path, normalized_path.replace('/', '\\')}
    filename = normalized_path.split('/')[-1]

    try:
        selected_commit = _get_commit_for_branch(db, repository, branch)
        if selected_commit:
                commit_file = db.query(CommitFile).filter(
                    CommitFile.commit_id == selected_commit.id,
                    CommitFile.file_path.in_(list(path_variants))
                ).first()

                if commit_file and commit_file.file_object:
                    content_bytes = commit_file.file_object.content
                    media_type = commit_file.file_object.mime_type or 'application/octet-stream'
                    headers = {
                        "Content-Disposition": f'attachment; filename="{filename}"'
                    }
                    return Response(content=content_bytes, media_type=media_type, headers=headers)

        # Fall back to latest pending commit only when branch is not explicitly selected
        from database.models import PendingCommit, FileObject
        pending_commits = []
        if not branch:
            pending_commits = db.query(PendingCommit).filter(
                PendingCommit.repository_id == repo_id,
                PendingCommit.status == 'pending'
            ).order_by(PendingCommit.created_at.desc()).all()

        if pending_commits:
            latest_pending = pending_commits[0]

            if latest_pending.files:
                pending_file = db.query(PendingCommitFile).filter(
                    PendingCommitFile.pending_commit_id == latest_pending.id,
                    PendingCommitFile.file_path.in_(list(path_variants))
                ).first()

                if pending_file:
                    file_object = db.query(FileObject).filter(FileObject.hash == pending_file.file_hash).first()
                    if file_object:
                        media_type = file_object.mime_type or 'application/octet-stream'
                        headers = {
                            "Content-Disposition": f'attachment; filename="{filename}"'
                        }
                        return Response(content=file_object.content, media_type=media_type, headers=headers)
            else:
                import json
                files_data = json.loads(latest_pending.files_data) if latest_pending.files_data else {}
                file_content_b64 = None
                for file_path, file_content in files_data.items():
                    if file_path.replace('\\', '/') == normalized_path:
                        file_content_b64 = file_content
                        break

                if file_content_b64:
                    content_bytes = base64.b64decode(file_content_b64.encode())
                    headers = {
                        "Content-Disposition": f'attachment; filename="{filename}"'
                    }
                    return Response(content=content_bytes, media_type='application/octet-stream', headers=headers)

        raise HTTPException(status_code=404, detail="File not found")

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"Error in download_repository_file: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/api/repository/{repo_id}/file-history")
async def get_file_history(
    repo_id: str,
    path: str,
    branch: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
    follow_renames: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view file history")

    normalized_path = path.replace('\\', '/')
    page_size = max(1, min(limit, 500))

    commits = db.query(Commit).filter(Commit.repository_id == repo_id).order_by(Commit.created_at.asc(), Commit.id.asc()).all()
    if branch:
        resolved_branch = _resolve_branch(db, repo_id, branch)
        reachable = set(_collect_reachable_commits(db, resolved_branch.head_commit_id))
        commits = [commit for commit in commits if commit.id in reachable]

    tracked_paths = {normalized_path}
    if follow_renames:
        changed = True
        while changed:
            changed = False
            lineage_links = db.query(FileLineage).filter(
                FileLineage.repository_id == repo_id,
                FileLineage.old_path.in_(list(tracked_paths))
            ).all()
            for link in lineage_links:
                if link.new_path not in tracked_paths:
                    tracked_paths.add(link.new_path)
                    changed = True

            reverse_links = db.query(FileLineage).filter(
                FileLineage.repository_id == repo_id,
                FileLineage.new_path.in_(list(tracked_paths))
            ).all()
            for link in reverse_links:
                if link.old_path not in tracked_paths:
                    tracked_paths.add(link.old_path)
                    changed = True

    tracked_variants = set()
    for tracked in tracked_paths:
        tracked_variants.add(tracked)
        tracked_variants.add(tracked.replace('/', '\\'))

    commit_files = db.query(CommitFile).join(Commit, Commit.id == CommitFile.commit_id).filter(
        Commit.repository_id == repo_id,
        CommitFile.file_path.in_(list(tracked_variants))
    ).all()

    files_by_commit: Dict[str, List[CommitFile]] = {}
    for commit_file in commit_files:
        if branch and commit_file.commit_id not in reachable:
            continue
        files_by_commit.setdefault(commit_file.commit_id, []).append(commit_file)

    versions: List[Dict[str, Any]] = []
    last_included_hash: Optional[str] = None
    last_included_path: Optional[str] = None

    for commit in commits:
        commit_files_for_commit = files_by_commit.get(commit.id, [])
        if not commit_files_for_commit:
            continue

        commit_file = sorted(commit_files_for_commit, key=lambda item: item.file_path)[0]
        observed_path = commit_file.file_path.replace('\\', '/')

        # Keep per-file history focused on actual file revisions.
        if commit_file.file_hash == last_included_hash and observed_path == last_included_path:
            continue

        previous_hash = last_included_hash
        change_type = "added" if previous_hash is None else "modified"

        versions.append({
            "version_number": len(versions) + 1,
            "commit_id": commit.id,
            "timestamp": commit.created_at.isoformat() if commit.created_at else None,
            "author": commit.author.username if commit.author else None,
            "message": commit.message,
            "file_path": normalized_path,
            "observed_path": observed_path,
            "file_name": normalized_path.split('/')[-1],
            "file_hash": commit_file.file_hash,
            "file_size": commit_file.file_size,
            "change_type": change_type,
            "changed_from_hash": previous_hash,
            "lineage": {
                "requested_path": normalized_path,
                "observed_path": observed_path,
                "renamed": observed_path != normalized_path
            }
        })
        last_included_hash = commit_file.file_hash
        last_included_path = observed_path

    versions.reverse()

    cursor_data = _decode_cursor(cursor)
    start = int(cursor_data.get("offset", 0) or 0)
    if start < 0:
        start = 0
    page_versions = versions[start:start + page_size]
    next_offset = start + page_size
    has_more = next_offset < len(versions)
    next_cursor = _encode_cursor({"offset": next_offset}) if has_more else None

    logger.info(
        "file_history repo=%s path=%s branch=%s versions=%d page_size=%d start=%d",
        repo_id,
        normalized_path,
        branch or "main",
        len(versions),
        page_size,
        start,
    )
    return {
        "success": True,
        "path": normalized_path,
        "history_scope": "branch" if branch else "repository",
        "follow_renames": follow_renames,
        "lineage_paths": sorted(list(tracked_paths)) if follow_renames else [normalized_path],
        "versions": page_versions,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }

@app.get("/api/repository/{repo_id}/compare")
async def compare_commits(
    repo_id: str,
    from_commit: str,
    to_commit: str,
    path: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "compare commits")

    from_obj = CommitCRUD.get_commit(db, from_commit)
    to_obj = CommitCRUD.get_commit(db, to_commit)
    if not from_obj or from_obj.repository_id != repo_id:
        raise HTTPException(status_code=404, detail="from_commit not found in repository")
    if not to_obj or to_obj.repository_id != repo_id:
        raise HTTPException(status_code=404, detail="to_commit not found in repository")

    from_tree = _get_commit_tree(db, from_commit)
    to_tree = _get_commit_tree(db, to_commit)
    paths = set(from_tree.keys()) | set(to_tree.keys())
    if path:
        normalized_path = path.replace('\\', '/')
        paths = {normalized_path}

    comparisons: List[Dict[str, Any]] = []
    for file_path in sorted(paths):
        previous_content = from_tree.get(file_path)
        current_content = to_tree.get(file_path)
        if previous_content == current_content:
            continue

        status = "modified"
        if previous_content is None:
            status = "added"
        elif current_content is None:
            status = "removed"

        previous_text = _try_decode_text(previous_content)
        current_text = _try_decode_text(current_content)
        is_binary_or_large = _is_binary_or_large(previous_content) or _is_binary_or_large(current_content)

        file_diff: Dict[str, Any] = {
            "file_path": file_path,
            "status": status,
            "is_binary": is_binary_or_large,
            "previous": {
                "exists": previous_content is not None,
                "size": len(previous_content) if previous_content is not None else 0
            },
            "current": {
                "exists": current_content is not None,
                "size": len(current_content) if current_content is not None else 0
            }
        }

        if not is_binary_or_large and previous_text is not None and current_text is not None:
            side_by_side = _build_side_by_side_diff(previous_text, current_text)
            file_diff["diff"] = side_by_side
            file_diff["previous"]["content"] = previous_text
            file_diff["current"]["content"] = current_text
        else:
            file_diff["diff"] = {
                "rows": [],
                "stats": {"added": 0, "removed": 0, "unchanged": 0},
                "truncated": len(previous_content or b"") > DIFF_MAX_BYTES or len(current_content or b"") > DIFF_MAX_BYTES,
                "reason": "binary_or_large"
            }

        comparisons.append(file_diff)

    return {
        "success": True,
        "from_commit": from_commit,
        "to_commit": to_commit,
        "files": comparisons
    }

@app.post("/api/repository/{repo_id}/rollback/file")
async def rollback_file(
    repo_id: str,
    request: FileRollbackRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    if not can_write_repository(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Write permission required")

    rollback_branch = _resolve_branch(db, repo_id, request.branch) if request.branch else _get_default_branch(db, repo_id)
    if rollback_branch.is_default and not can_manage_repository(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Default branch rollback requires management permission")

    head_commit_id = rollback_branch.head_commit_id
    if not head_commit_id:
        raise HTTPException(status_code=400, detail="Branch has no commits to rollback")
    if request.expected_head_commit_id is not None and request.expected_head_commit_id != head_commit_id:
        _raise_head_mismatch(rollback_branch.name, request.expected_head_commit_id, head_commit_id)

    target_commit = CommitCRUD.get_commit(db, request.target_commit_id)
    if not target_commit or target_commit.repository_id != repo_id:
        raise HTTPException(status_code=404, detail="Target commit not found in repository")

    normalized_path = request.path.replace('\\', '/')
    head_tree = _get_commit_tree(db, head_commit_id)
    target_tree = _get_commit_tree(db, target_commit.id)

    if normalized_path not in head_tree and normalized_path not in target_tree:
        raise HTTPException(status_code=404, detail="File does not exist in source or target commit")

    new_tree = dict(head_tree)
    target_content = target_tree.get(normalized_path)
    if target_content is None:
        new_tree.pop(normalized_path, None)
    else:
        new_tree[normalized_path] = target_content

    if new_tree == head_tree:
        raise HTTPException(status_code=400, detail="Rollback would not change current branch state")

    rollback_commit_id = hashlib.sha1(
        f"rollback:file:{repo_id}:{rollback_branch.name}:{normalized_path}:{head_commit_id}:{target_commit.id}:{datetime.utcnow().isoformat()}".encode("utf-8")
    ).hexdigest()
    rollback_message = request.summary or f"rollback(file): {normalized_path} to {target_commit.id[:8]}"

    commit_data = {
        "id": rollback_commit_id,
        "repository_id": repo_id,
        "author": current_user.username,
        "parent": head_commit_id,
        "parents": [head_commit_id],
        "message": rollback_message,
        "files": _tree_to_commit_payload(new_tree)
    }

    commit = CommitCRUD.create_commit(db, commit_data)
    BranchCRUD.update_branch_head(db, repo_id, rollback_branch.name, commit.id)

    logger.info(
        "rollback_file repo=%s branch=%s path=%s actor=%s target=%s new_commit=%s",
        repo_id,
        rollback_branch.name,
        normalized_path,
        current_user.username,
        target_commit.id,
        commit.id,
    )

    ActivityCRUD.create_activity(
        db,
        user_id=current_user.id,
        activity_type="rollback_file",
        description=(
            f"Rolled back file '{normalized_path}' on branch '{rollback_branch.name}' "
            f"from {head_commit_id[:8]} to {commit.id[:8]} (target {target_commit.id[:8]})"
        ),
        repository_id=repo_id
    )

    return {
        "success": True,
        "branch": rollback_branch.name,
        "new_commit_id": commit.id,
        "rolled_back_path": normalized_path
    }

@app.post("/api/repository/{repo_id}/rollback/branch")
async def rollback_branch(
    repo_id: str,
    request: BranchRollbackRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    rollback_branch_obj = _resolve_branch(db, repo_id, request.branch)
    if rollback_branch_obj.is_default and not can_manage_repository(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Default branch rollback requires management permission")
    if not can_manage_repository(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Branch rollback requires management permission")

    current_head = rollback_branch_obj.head_commit_id
    if not current_head:
        raise HTTPException(status_code=400, detail="Branch has no commits to rollback")
    if request.expected_head_commit_id is not None and request.expected_head_commit_id != current_head:
        _raise_head_mismatch(rollback_branch_obj.name, request.expected_head_commit_id, current_head)

    target_commit = CommitCRUD.get_commit(db, request.target_commit_id)
    if not target_commit or target_commit.repository_id != repo_id:
        raise HTTPException(status_code=404, detail="Target commit not found in repository")
    if not _is_ancestor(db, target_commit.id, current_head):
        raise HTTPException(status_code=400, detail="Target commit must be an ancestor of current branch head")

    current_tree = _get_commit_tree(db, current_head)
    target_tree = _get_commit_tree(db, target_commit.id)
    if current_tree == target_tree:
        raise HTTPException(status_code=400, detail="Rollback would not change current branch state")

    rollback_commit_id = hashlib.sha1(
        f"rollback:branch:{repo_id}:{rollback_branch_obj.name}:{current_head}:{target_commit.id}:{datetime.utcnow().isoformat()}".encode("utf-8")
    ).hexdigest()
    rollback_message = request.summary or f"rollback(branch): {rollback_branch_obj.name} to {target_commit.id[:8]}"

    commit_data = {
        "id": rollback_commit_id,
        "repository_id": repo_id,
        "author": current_user.username,
        "parent": current_head,
        "parents": [current_head],
        "message": rollback_message,
        "files": _tree_to_commit_payload(target_tree)
    }

    commit = CommitCRUD.create_commit(db, commit_data)
    BranchCRUD.update_branch_head(db, repo_id, rollback_branch_obj.name, commit.id)

    logger.info(
        "rollback_branch repo=%s branch=%s actor=%s target=%s previous_head=%s new_commit=%s",
        repo_id,
        rollback_branch_obj.name,
        current_user.username,
        target_commit.id,
        current_head,
        commit.id,
    )

    ActivityCRUD.create_activity(
        db,
        user_id=current_user.id,
        activity_type="rollback_branch",
        description=(
            f"Rolled back branch '{rollback_branch_obj.name}' from {current_head[:8]} to {commit.id[:8]} "
            f"(target {target_commit.id[:8]})"
        ),
        repository_id=repo_id
    )

    return {
        "success": True,
        "branch": rollback_branch_obj.name,
        "new_commit_id": commit.id,
        "target_commit_id": target_commit.id
    }

# ─── Branches, Tags, Releases, Pull Requests ────────────────────────────────

@app.get("/api/repository/{repo_id}/branches")
async def list_branches(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    _require_repository_checkout_access(db, current_user, repository, None, "view branches")

    branches = BranchCRUD.get_branches_by_repository(db, repo_id)
    if not user_sees_all_repository_branches(db, current_user, repository):
        branches = [b for b in branches if b.is_default] or branches[:1]
    # Default branch first, then alphabetical.
    branches = sorted(
        branches,
        key=lambda b: (not bool(b.is_default), (b.name or "").lower()),
    )
    return {
        "success": True,
        "branches": [
            {
                "name": b.name,
                "head_commit_id": b.head_commit_id,
                "is_default": b.is_default,
                "created_at": b.created_at.isoformat() if b.created_at else None,
                "updated_at": b.updated_at.isoformat() if b.updated_at else None
            }
            for b in branches
        ]
    }


@app.post("/api/repository/{repo_id}/star")
async def star_repository(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "star")
    RepositoryStarCRUD.add_star(db, current_user.id, repo_id)
    return {
        "success": True,
        "star_count": RepositoryStarCRUD.count_for_repository(db, repo_id),
        "starred": True,
    }


@app.delete("/api/repository/{repo_id}/star")
async def unstar_repository(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "unstar")
    RepositoryStarCRUD.remove_star(db, current_user.id, repo_id)
    return {
        "success": True,
        "star_count": RepositoryStarCRUD.count_for_repository(db, repo_id),
        "starred": False,
    }


@app.post("/api/repository/{repo_id}/branches")
async def create_branch(
    repo_id: str,
    request: BranchCreateRequest,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "create branch in", required_scope="write")

    branch_name = _normalize_branch_name(request.name)
    if BranchCRUD.get_branch(db, repo_id, branch_name):
        raise HTTPException(status_code=400, detail="Branch already exists")

    head_commit_id = request.from_commit or repository.head_commit_id
    if head_commit_id and not CommitCRUD.get_commit(db, head_commit_id):
        raise HTTPException(status_code=400, detail="Invalid from_commit")

    branch = BranchCRUD.create_branch(db, repo_id, branch_name, head_commit_id=head_commit_id)
    return {
        "success": True,
        "branch": {
            "name": branch.name,
            "head_commit_id": branch.head_commit_id,
            "is_default": branch.is_default
        }
    }

@app.put("/api/repository/{repo_id}/branches/{branch_name}/rename")
async def rename_branch(
    repo_id: str,
    branch_name: str,
    request: BranchRenameRequest,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "rename branch in", required_scope="manage")

    old_name = _normalize_branch_name(branch_name)
    new_name = _normalize_branch_name(request.new_name)
    if BranchCRUD.get_branch(db, repo_id, new_name):
        raise HTTPException(status_code=400, detail="Target branch name already exists")

    branch = BranchCRUD.rename_branch(db, repo_id, old_name, new_name)
    return {"success": True, "branch": {"name": branch.name, "head_commit_id": branch.head_commit_id}}

@app.put("/api/repository/{repo_id}/branches/{branch_name}/head")
async def update_branch_head(
    repo_id: str,
    branch_name: str,
    request: BranchHeadUpdateRequest,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "update branch head in", required_scope="manage")

    if not CommitCRUD.get_commit(db, request.head_commit_id):
        raise HTTPException(status_code=400, detail="Commit not found")

    branch = BranchCRUD.update_branch_head(db, repo_id, _normalize_branch_name(branch_name), request.head_commit_id)
    return {"success": True, "branch": {"name": branch.name, "head_commit_id": branch.head_commit_id}}

@app.put("/api/repository/{repo_id}/branches/{branch_name}/default")
async def set_default_branch(
    repo_id: str,
    branch_name: str,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "set default branch in", required_scope="manage")

    branch = BranchCRUD.set_default_branch(db, repo_id, _normalize_branch_name(branch_name))
    if branch.head_commit_id:
        repository.head_commit_id = branch.head_commit_id
        db.commit()

    return {"success": True, "branch": {"name": branch.name, "is_default": branch.is_default}}

@app.delete("/api/repository/{repo_id}/branches/{branch_name}")
async def delete_branch(
    repo_id: str,
    branch_name: str,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "delete branch in", required_scope="manage")

    branch = BranchCRUD.get_branch(db, repo_id, _normalize_branch_name(branch_name))
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    if branch.is_default:
        raise HTTPException(status_code=400, detail="Cannot delete the default branch")

    BranchCRUD.delete_branch(db, repo_id, branch.name)
    return {"success": True, "message": f"Branch '{branch.name}' deleted"}

@app.get("/api/repository/{repo_id}/tags")
async def list_tags(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view tags")

    tags = TagCRUD.list_tags(db, repo_id)
    return {
        "success": True,
        "tags": [
            {
                "name": t.name,
                "commit_id": t.commit_id,
                "message": t.message,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "created_by": t.created_by.username if t.created_by else None
            }
            for t in tags
        ]
    }

@app.post("/api/repository/{repo_id}/tags")
async def create_tag(
    repo_id: str,
    request: TagCreateRequest,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "create tag in", required_scope="write")

    tag_name = _normalize_branch_name(request.name)
    commit_id = request.commit_id or repository.head_commit_id
    if not commit_id:
        raise HTTPException(status_code=400, detail="No commit available for tagging")
    if not CommitCRUD.get_commit(db, commit_id):
        raise HTTPException(status_code=400, detail="Commit not found")

    tag = TagCRUD.create_tag(db, repo_id, tag_name, commit_id, request.message, current_user.id)
    return {
        "success": True,
        "tag": {
            "name": tag.name,
            "commit_id": tag.commit_id,
            "message": tag.message
        }
    }

@app.delete("/api/repository/{repo_id}/tags/{tag_name}")
async def delete_tag(
    repo_id: str,
    tag_name: str,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "delete tag in", required_scope="manage")

    TagCRUD.delete_tag(db, repo_id, tag_name)
    return {"success": True, "message": f"Tag '{tag_name}' deleted"}

@app.get("/api/repository/{repo_id}/releases")
async def list_releases(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view releases")

    releases = ReleaseCRUD.list_releases(db, repo_id)
    return {
        "success": True,
        "releases": [
            {
                "version": r.version,
                "title": r.title,
                "notes": r.notes,
                "tag": r.tag.name if r.tag else None,
                "commit_id": r.tag.commit_id if r.tag else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "created_by": r.created_by.username if r.created_by else None
            }
            for r in releases
        ]
    }

@app.post("/api/repository/{repo_id}/releases")
async def create_release(
    repo_id: str,
    request: ReleaseCreateRequest,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "create release in", required_scope="write")

    version = _validate_semver(request.version)
    if ReleaseCRUD.get_release(db, repo_id, version):
        raise HTTPException(status_code=400, detail="Release version already exists")

    tag_name = request.tag or (version if version.startswith("v") else f"v{version}")
    tag = TagCRUD.get_tag(db, repo_id, tag_name)
    if not tag:
        commit_id = repository.head_commit_id
        if not commit_id:
            raise HTTPException(status_code=400, detail="No commit available for release")
        tag = TagCRUD.create_tag(db, repo_id, tag_name, commit_id, f"Release {version}", current_user.id)

    release = ReleaseCRUD.create_release(db, repo_id, tag.id, version, request.title, request.notes, current_user.id)
    return {
        "success": True,
        "release": {
            "version": release.version,
            "title": release.title,
            "notes": release.notes,
            "tag": tag.name,
            "commit_id": tag.commit_id
        }
    }

@app.get("/api/repository/{repo_id}/pull-requests")
async def list_pull_requests(
    repo_id: str,
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view pull requests")

    prs = PullRequestCRUD.list_pull_requests(db, repo_id, status)
    return {
        "success": True,
        "pull_requests": [
            {
                "id": pr.id,
                "title": pr.title,
                "description": pr.description,
                "source_branch": pr.source_branch,
                "target_branch": pr.target_branch,
                "status": pr.status,
                "created_by": pr.created_by.username if pr.created_by else None,
                "created_at": pr.created_at.isoformat() if pr.created_at else None,
                "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
                "merge_commit_id": pr.merge_commit_id
            }
            for pr in prs
        ]
    }

@app.post("/api/repository/{repo_id}/pull-requests")
async def create_pull_request(
    repo_id: str,
    request: PullRequestCreateRequest,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "create pull request in", required_scope="write")

    source_branch = _normalize_branch_name(request.source_branch)
    target_branch = _normalize_branch_name(request.target_branch)
    if source_branch == target_branch:
        raise HTTPException(status_code=400, detail="Source and target branches must differ")

    if not BranchCRUD.get_branch(db, repo_id, source_branch):
        raise HTTPException(status_code=404, detail="Source branch not found")
    if not BranchCRUD.get_branch(db, repo_id, target_branch):
        raise HTTPException(status_code=404, detail="Target branch not found")

    pr = PullRequestCRUD.create_pull_request(
        db,
        repo_id,
        request.title,
        request.description,
        source_branch,
        target_branch,
        current_user.id
    )
    _sync_issues_for_pr_created(db, repo_id, pr)
    return {
        "success": True,
        "pull_request": {
            "id": pr.id,
            "title": pr.title,
            "status": pr.status,
            "source_branch": pr.source_branch,
            "target_branch": pr.target_branch
        }
    }

@app.get("/api/repository/{repo_id}/pull-requests/{pr_id}")
async def get_pull_request(
    repo_id: str,
    pr_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view pull request")

    pr = PullRequestCRUD.get_pull_request(db, repo_id, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")

    return {
        "success": True,
        "pull_request": {
            "id": pr.id,
            "title": pr.title,
            "description": pr.description,
            "source_branch": pr.source_branch,
            "target_branch": pr.target_branch,
            "status": pr.status,
            "merge_commit_id": pr.merge_commit_id
        }
    }

@app.post("/api/repository/{repo_id}/pull-requests/{pr_id}/close")
async def close_pull_request(
    repo_id: str,
    pr_id: int,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    pr = PullRequestCRUD.get_pull_request(db, repo_id, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")

    resolved_actor = current_user.username if current_user else actor_username
    actor = get_actor_user(db, resolved_actor)
    if actor.id != pr.created_by_id and not can_manage_repository(db, actor, repository):
        raise HTTPException(status_code=403, detail="Not allowed to close this pull request")

    pr = PullRequestCRUD.close_pull_request(db, pr, reviewer_id=actor.id)
    return {"success": True, "status": pr.status}

@app.post("/api/repository/{repo_id}/pull-requests/{pr_id}/merge")
async def merge_pull_request(
    repo_id: str,
    pr_id: int,
    request: Optional[MergePullRequestRequest] = Body(default=None),
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    pr = PullRequestCRUD.get_pull_request(db, repo_id, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "merge pull request in", required_scope="manage")

    if pr.status != "open":
        raise HTTPException(status_code=400, detail=f"Pull request is {pr.status}")

    source_branch = BranchCRUD.get_branch(db, repo_id, pr.source_branch)
    target_branch = BranchCRUD.get_branch(db, repo_id, pr.target_branch)
    if not source_branch or not target_branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    pre_merge_target_head = target_branch.head_commit_id

    expected_head_commit_id = request.expected_head_commit_id if request else None
    if expected_head_commit_id is not None and expected_head_commit_id != target_branch.head_commit_id:
        _raise_head_mismatch(target_branch.name, expected_head_commit_id, target_branch.head_commit_id)

    base_commit_id = _find_merge_base(db, target_branch.head_commit_id, source_branch.head_commit_id)
    base_tree = _get_commit_tree(db, base_commit_id)
    target_tree = _get_commit_tree(db, target_branch.head_commit_id)
    source_tree = _get_commit_tree(db, source_branch.head_commit_id)
    merged_tree, conflicts = _merge_trees(base_tree, target_tree, source_tree)

    if conflicts:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "code": "MERGE_CONFLICT",
                "status": "conflicts",
                "source_branch": source_branch.name,
                "target_branch": target_branch.name,
                "message": "Merge has conflicts that require manual resolution.",
                "conflicts": sorted(conflicts)
            }
        )

    commit_id = hashlib.sha256(
        f"merge:{repo_id}:{pr.id}:{datetime.utcnow().isoformat()}".encode()
    ).hexdigest()[:40]

    commit_data = {
        "id": commit_id,
        "repository_id": repo_id,
        "author": current_user.username,
        "message": f"Merge branch '{source_branch.name}' into '{target_branch.name}'",
        "parents": [target_branch.head_commit_id, source_branch.head_commit_id],
        "timestamp": datetime.utcnow().isoformat()
    }

    file_entries = []
    for file_path, content in merged_tree.items():
        file_obj = FileObjectCRUD.store_file_object(db, content)
        file_entries.append(SimpleNamespace(
            file_path=file_path,
            file_hash=file_obj.hash,
            file_size=len(content)
        ))

    commit = CommitCRUD.create_commit_from_file_hashes(db, commit_data, file_entries)
    BranchCRUD.update_branch_head(db, repo_id, target_branch.name, commit.id)
    PullRequestCRUD.mark_merged(db, pr, merge_commit_id=commit.id, reviewer_id=current_user.id)

    ActivityCRUD.create_activity(
        db,
        user_id=current_user.id,
        activity_type="merge_pull_request",
        description=(
            f"Merged PR #{pr.id} {source_branch.name}->{target_branch.name} "
            f"from {pre_merge_target_head[:8] if pre_merge_target_head else 'None'} to {commit.id[:8]}"
        ),
        repository_id=repo_id
    )

    _sync_issues_for_pr_merged(db, repo_id, pr, commit, current_user.id)

    return {
        "success": True,
        "status": "merged",
        "merge_commit_id": commit.id
    }


# ---------------------------------------------------------------------------
# Issue tracking (GitHub-style, repo-scoped)
# ---------------------------------------------------------------------------


@app.get("/api/repository/{repo_id}/issues")
async def list_issues(
    repo_id: str,
    status: Optional[str] = None,
    search: Optional[str] = None,
    label: Optional[str] = None,
    milestone_id: Optional[int] = None,
    assignee: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not _user_can_view_issues(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Not allowed to view issues for this repository")

    items, total = IssueCRUD.list_issues(
        db,
        repo_id,
        status=status or None,
        search=search or None,
        milestone_id=milestone_id,
        label=label or None,
        assignee_username=assignee or None,
        limit=min(max(limit, 1), 200),
        offset=max(offset, 0),
        include_total=True,
    )
    return {
        "success": True,
        "issues": [_issue_to_list_dict(db, i) for i in items],
        "total": total,
    }


@app.post("/api/repository/{repo_id}/issues")
async def create_issue(
    repo_id: str,
    request: IssueCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository, "create issues in", required_scope="write")

    assigned_to_id = None
    assigned_user = None
    if request.assigned_to and request.assigned_to.strip():
        assignee_username = request.assigned_to.strip().lstrip('@')  # Remove @ prefix if present
        assignee_user = UserCRUD.get_user_by_username(db, assignee_username)
        if not assignee_user:
            raise HTTPException(status_code=400, detail=f"Assignee user '{assignee_username}' not found")
        assigned_to_id = assignee_user.id
        assigned_user = assignee_user

    milestone_id = request.milestone_id
    if milestone_id is not None:
        ms = MilestoneCRUD.get_milestone(db, repo_id, milestone_id)
        if not ms:
            raise HTTPException(status_code=400, detail="Milestone not found for this repository")

    issue = IssueCRUD.create_issue(
        db,
        repo_id,
        request.title,
        request.description or "",
        request.issue_type,
        request.priority,
        current_user.id,
        assigned_to_id=assigned_to_id,
        milestone_id=milestone_id,
        labels=request.labels or [],
    )
    if request.description:
        _maybe_notify_issue_mentions(db, request.description, current_user, issue, "issue description")
        
        # Handle access requests for mentioned users
        _handle_issue_access_requests(db, issue, current_user, repository, request.description)

    # Handle access requests for assigned users without repo access
    if assigned_user:
        assign_reason = _access_request_reason_text(
            issue,
            None,
            fallback=f"Assigned to issue #{issue.number}: {issue.title}",
        )
        _maybe_create_issue_access_request_for_user(
            db,
            issue,
            current_user,
            repository,
            assigned_user,
            assign_reason,
        )

    # Notify assignee
    if assigned_to_id:
        NotificationCRUD.create_notification(
            db,
            user_id=assigned_to_id,
            notification_type="issue_assigned",
            title=f"Assigned to issue #{issue.number}",
            body=f"@{current_user.username} assigned you to: {issue.title}",
            payload={"repository_id": repo_id, "issue_number": issue.number},
            dedupe_key=f"issue_assigned_{issue.id}_{assigned_to_id}",
        )

    return {"success": True, "issue": _issue_to_list_dict(db, issue)}


@app.get("/api/repository/{repo_id}/issues/{issue_number:int}")
async def get_issue_detail(
    repo_id: str,
    issue_number: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not _user_can_view_issues(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Not allowed to view issues for this repository")

    issue = IssueCRUD.get_issue_by_number(db, repo_id, issue_number)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    payload = _issue_to_detail_dict(db, repo_id, issue)
    payload["permissions"] = _issue_permissions_for_actor(db, current_user, repository, issue)
    return {"success": True, "issue": payload}


@app.put("/api/repository/{repo_id}/issues/{issue_number:int}")
async def update_issue(
    repo_id: str,
    issue_number: int,
    request: IssueUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository, "update issues in", required_scope="write")

    issue = IssueCRUD.get_issue_by_number(db, repo_id, issue_number)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    if request.status is not None:
        # Restrict "close/reopen" to repository owner/admins (manage-level).
        # Assignees/creators can move between open/in_progress/resolved only.
        ns = (request.status or "").strip().lower()
        if ns == "open" and not can_manage_repository(db, current_user, repository):
            raise HTTPException(status_code=403, detail="Only repository owner/admin can reopen issues")
        if ns == "closed":
            is_creator = bool(issue.created_by_id and current_user and current_user.id == issue.created_by_id)
            if not (can_manage_repository(db, current_user, repository) or is_creator):
                raise HTTPException(status_code=403, detail="Only repository owner/admin or issue creator can close issues")
        try:
            IssueCRUD.transition_status(db, issue, request.status, actor_id=current_user.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        db.refresh(issue)

    if request.title is not None:
        issue.title = request.title.strip() or issue.title
    if request.description is not None:
        issue.description = request.description
    if request.priority is not None:
        issue.priority = request.priority.strip().lower()
    if request.issue_type is not None:
        issue.issue_type = request.issue_type.strip().lower()

    assignee_user = None
    if request.assigned_to is not None:
        if request.assigned_to.strip() == "":
            IssueCRUD.set_assignee(db, issue, None, actor_id=current_user.id)
        else:
            assignee_user = UserCRUD.get_user_by_username(db, request.assigned_to.strip())
            if not assignee_user:
                raise HTTPException(status_code=400, detail=f"Assignee user '{request.assigned_to.strip()}' not found")
            IssueCRUD.set_assignee(db, issue, assignee_user.id, actor_id=current_user.id)

    if request.milestone_id is not None:
        if request.milestone_id == -1 or request.milestone_id == 0:
            issue.milestone_id = None
        else:
            ms = MilestoneCRUD.get_milestone(db, repo_id, request.milestone_id)
            if not ms:
                raise HTTPException(status_code=400, detail="Milestone not found for this repository")
            issue.milestone_id = ms.id

    db.commit()
    db.refresh(issue)

    if request.description is not None:
        _maybe_notify_issue_mentions(db, request.description, current_user, issue, "issue description")
        _handle_issue_access_requests(db, issue, current_user, repository, request.description)

    if assignee_user:
        assign_reason = _access_request_reason_text(
            issue,
            None,
            fallback=f"Assigned to issue #{issue.number}: {issue.title}",
        )
        _maybe_create_issue_access_request_for_user(
            db,
            issue,
            current_user,
            repository,
            assignee_user,
            assign_reason,
        )

    out = _issue_to_detail_dict(db, repo_id, issue)
    out["permissions"] = _issue_permissions_for_actor(db, current_user, repository, issue)
    return {"success": True, "issue": out}


@app.post("/api/repository/{repo_id}/issues/{issue_number:int}/comments")
async def add_issue_comment(
    repo_id: str,
    issue_number: int,
    request: IssueCommentCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository, "comment on issues in", required_scope="write")

    issue = IssueCRUD.get_issue_by_number(db, repo_id, issue_number)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    body = (request.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Comment body is required")

    IssueCRUD.add_comment(db, issue, current_user.id, body)
    _maybe_notify_issue_mentions(db, body, current_user, issue, "issue comment")
    
    # Notify issue creator if not the commenter
    if issue.created_by_id != current_user.id:
        NotificationCRUD.create_notification(
            db,
            user_id=issue.created_by_id,
            notification_type="issue_comment",
            title=f"New comment on issue #{issue.number}",
            body=f"@{current_user.username} commented: {body[:100]}...",
            payload={"repository_id": repo_id, "issue_number": issue.number},
            dedupe_key=None,
        )
    
    # Notify assignee if not the commenter
    if issue.assigned_to_id and issue.assigned_to_id != current_user.id:
        NotificationCRUD.create_notification(
            db,
            user_id=issue.assigned_to_id,
            notification_type="issue_comment",
            title=f"New comment on issue #{issue.number}",
            body=f"@{current_user.username} commented: {body[:100]}...",
            payload={"repository_id": repo_id, "issue_number": issue.number},
            dedupe_key=None,
        )
    
    db.refresh(issue)
    out = _issue_to_detail_dict(db, repo_id, issue)
    out["permissions"] = _issue_permissions_for_actor(db, current_user, repository, issue)
    return {"success": True, "issue": out}


@app.post("/api/repository/{repo_id}/issues/{issue_number:int}/watch")
async def watch_issue(
    repo_id: str,
    issue_number: int,
    request: IssueWatchRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not _user_can_view_issues(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Not allowed to watch issues for this repository")

    issue = IssueCRUD.get_issue_by_number(db, repo_id, issue_number)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    existing = db.query(IssueWatcher).filter(
        IssueWatcher.issue_id == issue.id,
        IssueWatcher.user_id == current_user.id,
    ).first()
    if request.watch:
        if not existing:
            db.add(IssueWatcher(issue_id=issue.id, user_id=current_user.id))
            db.commit()
    else:
        if existing:
            db.delete(existing)
            db.commit()

    return {"success": True}


@app.get("/api/repository/{repo_id}/issue-labels")
async def list_issue_labels(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not _user_can_view_issues(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Not allowed to view labels for this repository")

    labels = IssueLabelCRUD.list_labels(db, repo_id)
    return {
        "success": True,
        "labels": [{"id": lbl.id, "name": lbl.name, "color": lbl.color} for lbl in labels],
    }


@app.post("/api/repository/{repo_id}/issue-labels")
async def upsert_issue_label(
    repo_id: str,
    request: IssueLabelUpsertRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository, "manage issue labels in", required_scope="write")

    lbl = IssueLabelCRUD.upsert_label(db, repo_id, request.name.strip(), request.color)
    return {"success": True, "label": {"id": lbl.id, "name": lbl.name, "color": lbl.color}}


@app.get("/api/repository/{repo_id}/milestones")
async def list_milestones(
    repo_id: str,
    include_closed: bool = True,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not _user_can_view_issues(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Not allowed to view milestones for this repository")

    milestones = MilestoneCRUD.list_milestones(db, repo_id, include_closed=include_closed)
    return {
        "success": True,
        "milestones": [
            {
                "id": m.id,
                "title": m.title,
                "description": m.description,
                "due_date": m.due_date.isoformat() if m.due_date else None,
                "is_closed": m.is_closed,
            }
            for m in milestones
        ],
    }


# ==========================
# Issue Access Request Endpoints
# ==========================

@app.get("/api/access-requests/pending")
async def list_pending_access_requests(
    repo_id: Optional[str] = None,
    status: Optional[str] = "pending",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List access requests for repository owner.

    status can be: pending (default), approved, denied, or all (no filter).
    """
    try:
        status_filter = None if (status or "").lower() == "all" else status
        role = (getattr(current_user, "role", "") or "").lower()
        if repo_id:
            repository = RepositoryCRUD.get_repository(db, repo_id)
            if not repository:
                raise HTTPException(status_code=404, detail="Repository not found")
            if repository.owner_id != current_user.id and role not in {"admin", "team_lead"}:
                raise HTTPException(status_code=403, detail="Only repository owner can view access requests")
            requests = IssueAccessRequestCRUD.list_pending_requests(db, repo_id=repo_id, status=status_filter)
        else:
            if role == "admin":
                # Admin can view platform-wide.
                requests = IssueAccessRequestCRUD.list_pending_requests(db, repo_id=None, status=status_filter, limit=1000)
            elif role == "team_lead":
                # Team leads see requests created by their developers (requested_by.team_lead_id = me).
                from database.models import IssueAccessRequest
                q = db.query(IssueAccessRequest)
                if status_filter:
                    q = q.filter(IssueAccessRequest.status == status_filter)
                q = q.join(User, User.id == IssueAccessRequest.requested_by_id).filter(User.team_lead_id == current_user.id)
                requests = q.order_by(desc(IssueAccessRequest.created_at)).limit(1000).all()
            else:
                # Non-privileged users: only across repositories they own.
                user_repos = db.query(Repository).filter(Repository.owner_id == current_user.id).all()
                repo_ids = [r.id for r in user_repos]
                requests = []
                for rid in repo_ids:
                    requests.extend(IssueAccessRequestCRUD.list_pending_requests(db, repo_id=rid, status=status_filter))
        
        return {
            "success": True,
            "access_requests": [
                {
                    "id": req.id,
                    "issue_number": req.issue.number if req.issue else None,
                    "issue_title": req.issue.title if req.issue else None,
                    "issue_description": ((req.issue.description or "").strip() or None) if req.issue else None,
                    "repository_id": req.repository_id,
                    "repository_name": req.repository.name if req.repository else None,
                    "requested_user": req.requested_user.username if req.requested_user else None,
                    "requested_by": req.requested_by.username if req.requested_by else None,
                    "status": req.status,
                    # keep both keys for frontend/backward compatibility
                    "request_reason": req.request_reason,
                    "reason": req.request_reason,
                    "created_at": req.created_at.isoformat() if req.created_at else None,
                }
                for req in requests
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/access-requests/my-requests")
async def list_my_access_requests(
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List access requests for the current user"""
    try:
        requests = IssueAccessRequestCRUD.list_user_requests(db, current_user.id, status=status)
        
        return {
            "success": True,
            "access_requests": [
                {
                    "id": req.id,
                    "issue_number": req.issue.number if req.issue else None,
                    "issue_title": req.issue.title if req.issue else None,
                    "repository_id": req.repository_id,
                    "repository_name": req.repository.name if req.repository else None,
                    "status": req.status,
                    "created_at": req.created_at.isoformat() if req.created_at else None,
                    "reviewed_at": req.reviewed_at.isoformat() if req.reviewed_at else None,
                    "admin_comment": req.review_comment,
                }
                for req in requests
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/access-requests/{request_id}/approve")
async def approve_access_request(
    request_id: int,
    approval_request: Optional[dict] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Approve an access request and grant write permission"""
    try:
        access_req = IssueAccessRequestCRUD.get_request(db, request_id)
        if not access_req:
            raise HTTPException(status_code=404, detail="Access request not found")
        
        repository = access_req.repository
        role = (getattr(current_user, "role", "") or "").lower()
        if repository.owner_id != current_user.id and role not in {"admin", "team_lead"}:
            raise HTTPException(status_code=403, detail="Only repository owner/admin/team lead can approve access requests")
        
        if access_req.status != "pending":
            raise HTTPException(status_code=400, detail=f"Request already {access_req.status}")
        
        comment = approval_request.get("comment", "Approved") if approval_request else "Approved"
        
        perm = IssueAccessRequestCRUD.approve_request(db, request_id, current_user.id, comment)
        
        # Notify the user about approval with CLI setup guide
        _notify_user_access_approved(db, access_req, repository)
        
        return {
            "success": True,
            "message": f"Access granted to @{access_req.requested_user.username}",
            "permission": {
                "user": access_req.requested_user.username,
                "repository": repository.name,
                "permission_level": perm.permission_level,
                "granted_at": perm.granted_at.isoformat() if perm.granted_at else None,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/access-requests/{request_id}/deny")
async def deny_access_request(
    request_id: int,
    denial_request: Optional[dict] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Deny an access request"""
    try:
        access_req = IssueAccessRequestCRUD.get_request(db, request_id)
        if not access_req:
            raise HTTPException(status_code=404, detail="Access request not found")
        
        repository = access_req.repository
        role = (getattr(current_user, "role", "") or "").lower()
        if repository.owner_id != current_user.id and role not in {"admin", "team_lead"}:
            raise HTTPException(status_code=403, detail="Only repository owner/admin/team lead can deny access requests")
        
        if access_req.status != "pending":
            raise HTTPException(status_code=400, detail=f"Request already {access_req.status}")
        
        comment = denial_request.get("comment", "Request denied") if denial_request else "Request denied"
        
        req = IssueAccessRequestCRUD.deny_request(db, request_id, current_user.id, comment)
        
        # Notify the user about denial
        NotificationCRUD.create_notification(
            db,
            user_id=access_req.requested_user_id,
            notification_type="issue_access_request_denied",
            title="Access Request Denied",
            body=f"Your access request for '{repository.name}' was denied. Reason: {comment}",
            payload={
                "issue_number": access_req.issue.number if access_req.issue else None,
                "repository_id": repository.id,
                "repository_name": repository.name,
                "reason": comment,
            },
            dedupe_key=f"access_req_denied_{request_id}"
        )
        
        return {
            "success": True,
            "message": f"Access request denied",
            "request_status": {
                "user": access_req.requested_user.username,
                "repository": repository.name,
                "status": req.status,
                "reason": comment,
                "denied_at": req.reviewed_at.isoformat() if req.reviewed_at else None,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _notify_user_access_approved(db: Session, access_req, repository: Repository) -> None:
    """Send access summary in-app; full CLI quick start is download-only (.txt)."""
    repo_owner = repository.owner
    granted_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    issue_num = access_req.issue.number if access_req.issue else None
    issue_branch = str(issue_num) if issue_num is not None else "issue"

    summary_body = f"""✅ REPOSITORY ACCESS GRANTED

Repository: {repository.name}
Owner: @{repo_owner.username}
Permission Level: Developer (write access)
Granted At: {granted_at}
Repository ID: {repository.id}"""
    if issue_num is not None:
        summary_body += f"\nIssue: #{issue_num}"

    workdir_hint = repository.name.replace('"', '\\"')
    cli_quickstart = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUICK START - FoxNest CLI

0️⃣  LOGIN (REQUIRED)
   $ fox login

1️⃣  SET SERVER (REMOTE ORIGIN)
   $ fox set origin 192.168.15.207:33333 --global
   (Use the same host/port as the web app backend)

2️⃣  CREATE A WORKING FOLDER AND INIT
   PowerShell: mkdir "{workdir_hint}"; cd "{workdir_hint}"
   Bash:       mkdir -p "{workdir_hint}" && cd "{workdir_hint}"
   $ fox init --username "{access_req.requested_user.username}" --repo-name "{repository.name}"

3️⃣  LINK THIS LOCAL REPO TO THE REMOTE REPO ID
   $ fox set repo-id {repository.id}

4️⃣  PULL LATEST CHANGES
   $ fox pull

5️⃣  CREATE FEATURE BRANCH FOR ISSUE #{issue_num if issue_num is not None else 'N/A'}
   $ fox branch create feature/issue-{issue_branch} --checkout
   (Alternative: $ fox checkout -b feature/issue-{issue_branch})

6️⃣  MAKE CHANGES & COMMIT
   $ fox add src/yourfile.py
   $ fox commit -m "Fix: description (Fixes #{issue_branch})"

7️⃣  PUSH YOUR CHANGES
   $ fox push

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HELPFUL COMMANDS

Check status:        $ fox status
View commits:        $ fox log
Pull latest:         $ fox pull
Create branch:       $ fox branch create <branch-name> --checkout
Switch branch:       $ fox checkout <branch-name>
View file history:   $ fox log <filename>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    """.strip()

    cli_txt_download = f"{summary_body}\n\n{cli_quickstart}"

    NotificationCRUD.create_notification(
        db,
        user_id=access_req.requested_user_id,
        notification_type="issue_access_request_approved",
        title="✅ Repository Access Granted",
        body=summary_body,
        payload={
            "issue_number": issue_num,
            "repository_id": repository.id,
            "repository_name": repository.name,
            "repository_owner": repo_owner.username,
            "cli_setup_guide": cli_txt_download,
            "access_granted": True,
            "actions": [
                {
                    "type": "DOWNLOAD_TXT",
                    "payload": {
                        "filename": f"{repository.name}-foxnest-cli-quickstart.txt",
                        "text": cli_txt_download,
                    },
                }
            ],
        },
        dedupe_key=f"access_approved_{access_req.id}"
    )


@app.post("/api/repository/{repo_id}/milestones")
async def create_milestone(
    repo_id: str,
    request: MilestoneCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository, "create milestones in", required_scope="write")

    due = None
    if request.due_date:
        try:
            due = datetime.fromisoformat(request.due_date.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid due_date")

    m = MilestoneCRUD.create_milestone(
        db,
        repo_id,
        request.title.strip(),
        description=request.description,
        due_date=due,
        created_by_id=current_user.id,
    )
    return {
        "success": True,
        "milestone": {
            "id": m.id,
            "title": m.title,
            "description": m.description,
            "due_date": m.due_date.isoformat() if m.due_date else None,
            "is_closed": m.is_closed,
        },
    }


@app.post("/api/admin/create-sample-data")
async def create_sample_data(current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Create sample repositories for testing (development only)"""
    try:
        sample_repos = [
            {"username": "john_doe", "repo_name": "legacy-system", "description": "Legacy system components and utilities"},
            {"username": "jane_smith", "repo_name": "old-mobile-prototype", "description": "Initial mobile app prototype"},
            {"username": "mike_wilson", "repo_name": "experimental-ui", "description": "Experimental UI components library"},
            {"username": "sarah_connor", "repo_name": "temp-data-migration", "description": "Temporary scripts for data migration"},
        ]
        
        created_repos = []
        for repo_data in sample_repos:
            try:
                repository = RepositoryCRUD.create_repository(
                    db, repo_data["username"], repo_data["repo_name"], repo_data["description"]
                )
                created_repos.append(repository.id)
            except ValueError:
                # Repository already exists, skip
                repo_id = RepositoryCRUD.generate_repo_id(repo_data["username"], repo_data["repo_name"])
                created_repos.append(repo_id)
        
        return {"success": True, "created_repositories": created_repos}
    
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/users")
async def list_users(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """List all users"""
    try:
        users = db.query(User).all()
        return {
            "success": True,
            "users": [
                {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "full_name": user.full_name,
                    "role": user.role if hasattr(user, 'role') else 'developer',
                    "team_lead_id": user.team_lead_id if hasattr(user, 'team_lead_id') else None,
                    "team_lead_name": user.team_lead.username if hasattr(user, 'team_lead') and user.team_lead else None,
                    "created_at": user.created_at.isoformat() if user.created_at else None,
                    "last_login_at": user.last_login_at.isoformat() if getattr(user, "last_login_at", None) else None,
                    "is_active": user.is_active,
                    "repository_count": len(user.repositories)
                }
                for user in users
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.get("/api/admin/users/engagement")
async def get_users_engagement(
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Admin: per-user engagement metrics (status, login, work, commits, score)."""
    try:
        users = UserEngagementCRUD.get_all_user_engagement(db)
        summary = {
            "active": sum(1 for u in users if u["status"] == "active"),
            "idle": sum(1 for u in users if u["status"] == "idle"),
            "never_used": sum(1 for u in users if u["status"] == "never_used"),
            "total": len(users),
        }
        return {"success": True, "summary": summary, "users": users}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.get("/api/admin/users/{username}/detail")
async def get_user_engagement_detail(
    username: str,
    limit: int = 100,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Admin: user overview + activity feed + history timeline for detail tabs."""
    try:
        detail = UserEngagementCRUD.get_user_detail(db, username, limit=min(max(limit, 1), 250))
        if not detail:
            raise HTTPException(status_code=404, detail="User not found")
        return {"success": True, **detail}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.post("/api/users/create")
async def create_user(request: dict, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Create a new user"""
    try:
        username = request.get("username")
        email = request.get("email")
        full_name = request.get("full_name")
        password = request.get("password")
        role = request.get("role", "developer")  # Default to 'developer'
        team_lead_id = request.get("team_lead_id")  # ID of the team lead (for developers)
        
        if not username:
            raise HTTPException(status_code=400, detail="Username is required")
        
        if role not in ['developer', 'team_lead']:
            raise HTTPException(status_code=400, detail="Role must be 'developer' or 'team_lead'")
        
        # If role is developer and team_lead_id is provided, validate the team lead exists
        if role == 'developer' and team_lead_id:
            team_lead = UserCRUD.get_user_by_id(db, team_lead_id)
            if not team_lead:
                raise HTTPException(status_code=400, detail="Team lead user not found")
            if team_lead.role != 'team_lead':
                raise HTTPException(status_code=400, detail="Selected user is not a team lead")
        
        # Check if user already exists
        existing_user = UserCRUD.get_user_by_username(db, username)
        if existing_user:
            raise HTTPException(status_code=400, detail="Username already exists")
        
        password_hash = hash_password(password) if password else None
        user = UserCRUD.create_user(db, username, email, full_name, role, team_lead_id, password_hash=password_hash)
        
        return {
            "success": True,
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "full_name": user.full_name,
                "role": user.role,
                "team_lead_id": user.team_lead_id,
                "created_at": user.created_at.isoformat() if user.created_at else None
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.put("/api/users/{username}")
async def update_user(username: str, request: dict, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Update user information"""
    try:
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        is_self = current_user.username == username
        is_admin = getattr(current_user, "role", "developer") in ["team_lead", "admin"]
        if not is_self and not is_admin:
            raise HTTPException(status_code=403, detail="You can only update your own profile")
        
        if "email" in request:
            user.email = request["email"]
        if "full_name" in request:
            user.full_name = request["full_name"]
        if "is_active" in request:
            if not is_admin:
                raise HTTPException(status_code=403, detail="Only admin/team lead can change active status")
            user.is_active = request["is_active"]
        
        db.commit()
        db.refresh(user)
        
        return {
            "success": True,
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "full_name": user.full_name,
                "is_active": user.is_active
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.delete("/api/users/{username}")
async def delete_user(username: str, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Delete a user"""
    try:
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Check if user owns any repositories
        if len(user.repositories) > 0:
            raise HTTPException(
                status_code=400, 
                detail=f"Cannot delete user who owns repositories. User owns {len(user.repositories)} repository(ies)."
            )
        
        db.delete(user)
        db.commit()
        
        return {
            "success": True,
            "message": f"User {username} deleted successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/api/users/reset-password")
async def reset_user_password(request: dict, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Reset a user's password (admin only)"""
    try:
        username = request.get("username")
        new_password = request.get("new_password")
        
        if not username:
            raise HTTPException(status_code=400, detail="Username is required")
        
        if not new_password:
            raise HTTPException(status_code=400, detail="New password is required")
        
        if len(new_password) < 4:
            raise HTTPException(status_code=400, detail="Password must be at least 4 characters")
        
        # Find user
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            raise HTTPException(status_code=404, detail=f"User '{username}' not found")
        
        # Hash new password
        new_hash = hash_password(new_password)
        
        # Update password
        user.password_hash = new_hash
        user.updated_at = datetime.now()
        db.commit()
        
        # Log activity
        ActivityCRUD.create_activity(
            db,
            user_id=current_user.id,
            activity_type="password_reset",
            description=f"Reset password for user: {username}"
        )
        
        return {
            "success": True,
            "message": f"Password reset successful for user '{username}'",
            "username": username
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/api/activities")
async def get_recent_activities(limit: int = 20, db: Session = Depends(get_db)):
    """Get recent activities"""
    try:
        activities = ActivityCRUD.get_recent_activities(db, limit)
        return {
            "success": True,
            "activities": [
                {
                    "id": activity.id,
                    "user": activity.user.username,
                    "repository": activity.repository.name if activity.repository else None,
                    "activity_type": activity.activity_type,
                    "description": activity.description,
                    "created_at": activity.created_at.isoformat() if activity.created_at else None
                }
                for activity in activities
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.put("/api/repository/{repo_id}/details")
async def update_repository_details(repo_id: str, request: UpdateRepositoryDetailsRequest, actor_username: Optional[str] = None, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Update repository G1 coordinator and testing status"""
    try:
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        resolved_actor = current_user.username if current_user else actor_username
        require_repository_access(db, resolved_actor, repository, "update", required_scope="write")

        repository = RepositoryCRUD.update_repository_details(
            db, 
            repo_id, 
            g1_coordinator=request.g1_coordinator,
            tested=request.tested
        )
        
        return UpdateRepositoryDetailsResponse(
            success=True,
            repository={
                "id": repository.id,
                "name": repository.name,
                "g1_coordinator": repository.g1_coordinator,
                "tested": repository.tested,
                "instruction_manual_filename": repository.instruction_manual_filename
            }
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/api/repository/{repo_id}/upload-manual")
async def upload_instruction_manual(
    repo_id: str,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """Upload instruction manual PDF for a repository"""
    try:
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        resolved_actor = current_user.username if current_user else actor_username
        require_repository_access(db, resolved_actor, repository, "upload instruction manual for", required_scope="write")

        # Validate file type
        if not file.filename.lower().endswith('.pdf'):
            raise HTTPException(status_code=400, detail="Only PDF files are allowed")
        
        # Create uploads directory if it doesn't exist
        uploads_dir = Path("/tmp/foxnest_uploads")
        uploads_dir.mkdir(exist_ok=True)
        
        # Generate unique filename
        file_extension = Path(file.filename).suffix
        unique_filename = f"{repo_id}_manual_{datetime.now().strftime('%Y%m%d_%H%M%S')}{file_extension}"
        file_path = uploads_dir / unique_filename
        
        # Save file
        content = await file.read()
        with open(file_path, "wb") as f:
            f.write(content)
        
        # Update repository with file path
        repository = RepositoryCRUD.update_repository_details(
            db,
            repo_id,
            instruction_manual_path=str(file_path),
            instruction_manual_filename=file.filename
        )
        
        return {
            "success": True,
            "message": "Instruction manual uploaded successfully",
            "filename": file.filename,
            "repository": {
                "id": repository.id,
                "name": repository.name,
                "instruction_manual_filename": repository.instruction_manual_filename
            }
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/api/repository/{repo_id}/download-manual")
async def download_instruction_manual(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download instruction manual PDF for a repository"""
    try:
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        _require_repository_read_access(db, current_user, repository, "download instruction manual")
        
        if not repository.instruction_manual_path:
            raise HTTPException(status_code=404, detail="No instruction manual found for this repository")
        
        file_path = Path(repository.instruction_manual_path)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Instruction manual file not found on server")
        
        return FileResponse(
            path=str(file_path),
            filename=repository.instruction_manual_filename or "instruction_manual.pdf",
            media_type="application/pdf"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

# User Permissions Management Endpoints
@app.post("/api/permissions/create")
async def create_permission(request: CreatePermissionRequest, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Grant a user permission to a repository"""
    try:
        permission = UserPermissionCRUD.create_permission(
            db,
            request.username,
            request.repo_id,
            request.permission_level,
            request.granted_by
        )
        
        return {
            "success": True,
            "permission": {
                "user": permission.user.username,
                "repository": permission.repository.name,
                "permission_level": permission.permission_level,
                "granted_at": permission.granted_at.isoformat() if permission.granted_at else None
            }
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.put("/api/permissions/update")
async def update_permission(request: CreatePermissionRequest, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Update a user's permission level for a repository"""
    try:
        permission = UserPermissionCRUD.create_permission(
            db,
            request.username,
            request.repo_id,
            request.permission_level,
            request.granted_by
        )
        
        return {
            "success": True,
            "permission": {
                "user": permission.user.username,
                "repository": permission.repository.name,
                "permission_level": permission.permission_level,
                "granted_at": permission.granted_at.isoformat() if permission.granted_at else None
            }
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/api/permissions/repository/{repo_id}")
async def get_repository_permissions(repo_id: str, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Get all permissions for a repository"""
    try:
        permissions = UserPermissionCRUD.get_repository_permissions(db, repo_id)
        return {
            "success": True,
            "permissions": [
                {
                    "user": p.user.username,
                    "permission_level": p.permission_level,
                    "granted_by": p.granted_by.username if p.granted_by else None,
                    "granted_at": p.granted_at.isoformat() if p.granted_at else None
                }
                for p in permissions
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/api/permissions/user/{username}")
async def get_user_permissions(username: str, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Get all permissions for a user"""
    try:
        permissions = UserPermissionCRUD.get_user_permissions(db, username)
        return {
            "success": True,
            "permissions": [
                {
                    "repository_id": p.repository.id,
                    "repository_name": p.repository.name,
                    "permission_level": p.permission_level,
                    "granted_by": p.granted_by.username if p.granted_by else None,
                    "granted_at": p.granted_at.isoformat() if p.granted_at else None
                }
                for p in permissions
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.delete("/api/permissions/revoke")
async def revoke_permission(username: str, repo_id: str, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Revoke a user's permission to a repository"""
    try:
        success = UserPermissionCRUD.revoke_permission(db, username, repo_id)
        if success:
            return {"success": True, "message": "Permission revoked successfully"}
        else:
            raise HTTPException(status_code=404, detail="Permission not found")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

# Pending Commits Management Endpoints
@app.get("/api/pending-commits")
async def get_all_pending_commits(
    status: str = 'pending', 
    team_lead_username: str = None,
    current_user: User = Depends(require_reviewer_user),
    db: Session = Depends(get_db)
):
    """Get all pending commits across all repositories, optionally filtered by team lead"""
    try:
        pending_commits = PendingCommitCRUD.get_all_pending_commits(db, status)
        
        # Determine which team lead to filter by
        filter_team_lead_username = team_lead_username
        
        # If no specific team lead is requested and current user is a team lead,
        # filter to commits they are responsible for:
        # - commits from developers assigned to them, OR
        # - commits targeting repositories they own (common in issue-tracking access flow)
        current_user_role = getattr(current_user, 'role', '').lower()
        if not filter_team_lead_username and current_user_role == 'team_lead':
            filter_team_lead_username = current_user.username
        
        # Filter by team lead if specified or auto-determined
        if filter_team_lead_username:
            team_lead = UserCRUD.get_user_by_username(db, filter_team_lead_username)
            if team_lead:
                # Team lead can review:
                # - commits from their assigned developers
                # - commits into repositories they own (even if developer has no team_lead_id)
                pending_commits = [
                    pc for pc in pending_commits 
                    if (
                        (pc.author and pc.author.team_lead_id == team_lead.id)
                        or (pc.repository and pc.repository.owner_id == team_lead.id)
                    )
                ]
        
        return {
            "success": True,
            "pending_commits": [
                {
                    "id": pc.id,
                    "repository_id": pc.repository.id,
                    "repository_name": pc.repository.name,
                    "author": pc.author.username,
                    "author_full_name": pc.author.full_name,
                    "team_lead_name": pc.author.team_lead.username if pc.author.team_lead else None,
                    "message": pc.message,
                    "created_at": pc.created_at.isoformat() if pc.created_at else None,
                    "status": pc.status,
                    "reviewed_by": pc.reviewer.username if pc.reviewer else None,
                    "reviewed_at": pc.reviewed_at.isoformat() if pc.reviewed_at else None,
                    "review_comment": pc.review_comment
                }
                for pc in pending_commits
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/api/repository/{repo_id}/pending-commits")
async def get_repository_pending_commits(repo_id: str, status: str = None, current_user: User = Depends(require_reviewer_user), db: Session = Depends(get_db)):
    """Get pending commits for a specific repository"""
    try:
        pending_commits = PendingCommitCRUD.get_pending_commits_by_repository(db, repo_id, status)
        return {
            "success": True,
            "pending_commits": [
                {
                    "id": pc.id,
                    "author": pc.author.username,
                    "message": pc.message,
                    "created_at": pc.created_at.isoformat() if pc.created_at else None,
                    "status": pc.status,
                    "reviewed_by": pc.reviewer.username if pc.reviewer else None,
                    "reviewed_at": pc.reviewed_at.isoformat() if pc.reviewed_at else None,
                    "review_comment": pc.review_comment
                }
                for pc in pending_commits
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/api/pending-commits/{commit_id}/review")
async def review_pending_commit(commit_id: str, request: ReviewCommitRequest, current_user: User = Depends(require_reviewer_user), db: Session = Depends(get_db)):
    """Approve or reject a pending commit"""
    try:
        if request.action == "approve":
            pending, commit = PendingCommitCRUD.approve_pending_commit(
                db, commit_id, current_user.username, request.comment
            )
            try:
                branch_name = None
                if pending and getattr(pending, "files_data", None):
                    try:
                        raw = json.loads(pending.files_data)
                        if isinstance(raw, dict) and "meta" in raw:
                            branch_name = raw.get("meta", {}).get("branch")
                    except Exception:
                        branch_name = None

                _sync_issues_for_new_commit(db, commit.repository_id, commit, branch_name or _get_default_branch(db, commit.repository_id).name, current_user.id)

                # Notify on approval (commit now exists and will show in linked commits)
                for ref in parse_issue_references(commit.message or ""):
                    issue = IssueCRUD.get_issue_by_number(db, commit.repository_id, ref["number"])
                    if not issue:
                        continue
                    _notify_issue_commit_reviewed(
                        db,
                        repo_id=commit.repository_id,
                        issue=issue,
                        reviewer=current_user,
                        commit_id=commit.id,
                        branch_name=branch_name,
                        action="approved",
                        comment=request.comment,
                    )
            except Exception:
                pass
            return {
                "success": True,
                "message": "Commit approved and merged",
                "commit_id": commit.id,
                "status": "approved"
            }
        elif request.action == "reject":
            pending = PendingCommitCRUD.reject_pending_commit(
                db, commit_id, current_user.username, request.comment
            )
            try:
                branch_name = None
                if pending and getattr(pending, "files_data", None):
                    try:
                        raw = json.loads(pending.files_data)
                        if isinstance(raw, dict) and "meta" in raw:
                            branch_name = raw.get("meta", {}).get("branch")
                    except Exception:
                        branch_name = None
                # Notify issue participants that the submitted commit was rejected.
                for ref in parse_issue_references(getattr(pending, "message", "") or ""):
                    issue = IssueCRUD.get_issue_by_number(db, pending.repository_id, ref["number"])
                    if not issue:
                        continue
                    _notify_issue_commit_reviewed(
                        db,
                        repo_id=pending.repository_id,
                        issue=issue,
                        reviewer=current_user,
                        commit_id=pending.id,
                        branch_name=branch_name,
                        action="rejected",
                        comment=request.comment,
                    )
            except Exception:
                pass
            return {
                "success": True,
                "message": "Commit rejected",
                "status": "rejected"
            }
        else:
            raise HTTPException(status_code=400, detail="Invalid action. Use 'approve' or 'reject'")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/api/pending-repositories")
async def get_all_pending_repositories(
    status: str = 'pending', 
    team_lead_username: str = None,
    current_user: User = Depends(require_reviewer_user),
    db: Session = Depends(get_db)
):
    """Get all pending repository requests, optionally filtered by team lead"""
    try:
        # Determine which team lead to filter by
        filter_team_lead_username = team_lead_username
        
        # If no specific team lead is requested and current user is NOT admin,
        # automatically filter to show only their developers' requests
        current_user_role = getattr(current_user, 'role', '').lower()
        if not filter_team_lead_username and current_user_role == 'team_lead':
            filter_team_lead_username = current_user.username
        
        if filter_team_lead_username:
            pending_repos = PendingRepositoryCRUD.get_pending_repositories_by_team_lead(db, filter_team_lead_username, status)
        else:
            pending_repos = PendingRepositoryCRUD.get_all_pending_repositories(db, status)
        
        return {
            "success": True,
            "pending_repositories": [
                {
                    "id": pr.id,
                    "repo_name": pr.repo_name,
                    "description": pr.description,
                    "requested_by": pr.requested_by.username,
                    "requested_by_full_name": pr.requested_by.full_name,
                    "owner": pr.owner.username,
                    "owner_full_name": pr.owner.full_name,
                    "created_at": pr.created_at.isoformat() if pr.created_at else None,
                    "status": pr.status,
                    "reviewed_by": pr.reviewer.username if pr.reviewer else None,
                    "reviewed_at": pr.reviewed_at.isoformat() if pr.reviewed_at else None,
                    "review_comment": pr.review_comment
                }
                for pr in pending_repos
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/api/pending-repositories/{pending_id}/review")
async def review_pending_repository(pending_id: int, request: ReviewCommitRequest, current_user: User = Depends(require_reviewer_user), db: Session = Depends(get_db)):
    """Approve or reject a pending repository"""
    try:
        if request.action == "approve":
            pending, repository = PendingRepositoryCRUD.approve_pending_repository(
                db, pending_id, current_user.username, request.comment
            )
            return {
                "success": True,
                "message": "Repository approved and created",
                "repo_id": repository.id,
                "status": "approved"
            }
        elif request.action == "reject":
            pending = PendingRepositoryCRUD.reject_pending_repository(
                db, pending_id, current_user.username, request.comment
            )
            return {
                "success": True,
                "message": "Repository request rejected",
                "status": "rejected"
            }
        else:
            raise HTTPException(status_code=400, detail="Invalid action. Use 'approve' or 'reject'")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

# ─── Documentation Endpoints ─────────────────────────────────────────────────

def _update_docs_job(repo_id: str, **fields):
    with DOCS_GENERATION_LOCK:
        existing = DOCS_GENERATION_JOBS.get(repo_id, {})
        existing.update(fields)
        DOCS_GENERATION_JOBS[repo_id] = existing


_ALL_DOC_STEPS = [
    "Setup",
    "README.md",
    "API Documentation",
    "Architecture",
    "Installation Guide",
    "Database Schema",
    "Requirements (SRS)",
    "Use Cases",
    "Release Notes",
    "User Manual",
    "API Usage Guide",
    "Database ER",
]


def _parse_selected_docs_param(selected_docs_raw: Optional[str]) -> Optional[List[str]]:
    if not selected_docs_raw:
        return None
    selected = [token.strip() for token in str(selected_docs_raw).split(",") if token.strip()]
    return selected or None


def _resolve_progress_steps(selected_docs: Optional[List[str]]) -> tuple[List[str], Optional[List[str]], List[str]]:
    selected_names, invalid = project_docs_generator.parse_selected_project_docs(selected_docs)
    if selected_names is None:
        return list(_ALL_DOC_STEPS), ["all"], invalid

    steps = ["Setup"] + [step for step in _ALL_DOC_STEPS[1:] if step in selected_names]
    return steps, sorted(selected_names), invalid


def _make_docs_progress_callback(repo_id: str):
    """Return a callback that updates per-document progress in the job dict."""
    def callback(doc_name: str, status: str):
        now = datetime.utcnow().isoformat()
        with DOCS_GENERATION_LOCK:
            job = DOCS_GENERATION_JOBS.get(repo_id)
            if not job:
                return
            progress = job.get("docs_progress", [])
            for entry in progress:
                if entry["name"] == doc_name:
                    entry["status"] = status
                    if status == "generating" and not entry.get("started_at"):
                        entry["started_at"] = now
                    elif status in {"done", "skipped", "error"}:
                        entry["finished_at"] = now
                    break
            job["docs_progress"] = progress
            DOCS_GENERATION_JOBS[repo_id] = job
    return callback


def _generate_repository_docs_sync(repo_id: str, lang_filter: Optional[str] = None):
    db = SessionLocal()
    try:
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise ValueError("Repository not found")

        commits_desc = CommitCRUD.get_commits_by_repository(db, repo_id, limit=200)
        if not commits_desc:
            return {
                "success": False,
                "error": "Repository has no commits. Push code first.",
                "repo_id": repo_id
            }

        latest_commit = commits_desc[0]
        source_commit = None
        for commit in commits_desc:
            if any(not f.file_path.startswith("docs/") for f in commit.files):
                source_commit = commit
                break
        if not source_commit:
            return {
                "success": False,
                "error": "No source code files found in repository commits.",
                "repo_id": repo_id
            }

        temp_dir = Path("/tmp") / f"foxnest_docs_{repo_id}"
        temp_dir.mkdir(parents=True, exist_ok=True)

        for commit_file in source_commit.files:
            if commit_file.file_path.startswith("docs/"):
                continue
            if _is_vendor_path(commit_file.file_path):
                continue
            file_path = temp_dir / commit_file.file_path
            file_path.parent.mkdir(parents=True, exist_ok=True)

            if commit_file.file_object:
                with open(file_path, "wb") as f:
                    f.write(commit_file.file_object.content)

        docs_output = DOCS_DIR / repo_id
        docs_output.mkdir(parents=True, exist_ok=True)

        results = docs_generator.generate_docs(
            str(temp_dir),
            output_dir=str(docs_output),
            lang_filter=lang_filter,
            verbose=False
        )

        docx_utils.convert_markdown_tree(docs_output)

        shutil.rmtree(temp_dir, ignore_errors=True)

        if isinstance(results, dict) and results.get("error"):
            return {
                "success": False,
                "error": results.get("error"),
                "repo_id": repo_id
            }

        languages = []
        total_files = 0
        total_symbols = 0
        total_documented = 0
        if isinstance(results, dict):
            languages = sorted(results.keys())
            for stats in results.values():
                if not isinstance(stats, dict):
                    continue
                total_files += stats.get("files", 0)
                documented = stats.get("documented", 0)
                total_documented += documented
                if "symbols" in stats:
                    total_symbols += stats.get("symbols", 0)
                else:
                    total_symbols += documented + stats.get("undocumented", 0)

        coverage = (total_documented / total_symbols * 100) if total_symbols > 0 else 0

        doc_files = []
        file_entries = []

        for commit_file in source_commit.files:
            if commit_file.file_path.startswith("docs/"):
                continue
            file_size = commit_file.file_size
            if file_size is None and commit_file.file_object:
                file_size = commit_file.file_object.size
            file_entries.append(SimpleNamespace(
                file_path=commit_file.file_path,
                file_hash=commit_file.file_hash,
                file_size=file_size or 0
            ))

        for doc_file_path in docs_output.rglob("*"):
            if doc_file_path.is_file():
                rel_path = doc_file_path.relative_to(docs_output)
                with open(doc_file_path, "rb") as f:
                    content = f.read()

                file_obj = FileObjectCRUD.store_file_object(db, content)
                doc_path = f"docs/{rel_path}"

                doc_files.append(doc_path)
                file_entries.append(SimpleNamespace(
                    file_path=doc_path,
                    file_hash=file_obj.hash,
                    file_size=len(content)
                ))

        if doc_files:
            commit_id = hashlib.sha256(
                f"{repo_id}:docs:{datetime.now().isoformat()}".encode()
            ).hexdigest()[:40]

            commit_data = {
                "id": commit_id,
                "repository_id": repo_id,
                "author": repository.owner.username,
                "message": f"Auto-generate documentation ({len(doc_files)} files, {coverage:.1f}% coverage)",
                "parent": latest_commit.id,
                "timestamp": datetime.now().isoformat()
            }

            CommitCRUD.create_commit_from_file_hashes(db, commit_data, file_entries)

        ActivityCRUD.create_activity(
            db=db,
            user_id=repository.owner_id,
            activity_type="generate_docs",
            description=f"Generated docs for repo {repo_id}. Languages: {', '.join(languages)}" if languages else f"Generated docs for repo {repo_id}.",
            repository_id=repo_id
        )

        return {
            "success": True,
            "message": "Documentation generated and committed to repository",
            "repo_id": repo_id,
            "docs_url": f"/api/repository/{repo_id}/docs",
            "coverage": round(coverage, 1),
            "files_analyzed": total_files,
            "languages": languages,
            "docs_files_added": len(doc_files),
            "results": results
        }
    except Exception as e:
        temp_dir = Path("/tmp") / f"foxnest_docs_{repo_id}"
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        db.close()


def _run_generate_docs_job(repo_id: str, lang_filter: Optional[str] = None):
    _update_docs_job(
        repo_id,
        status="running",
        started_at=datetime.utcnow().isoformat(),
        finished_at=None,
        error=None
    )
    try:
        result = _generate_repository_docs_sync(repo_id, lang_filter)
        if result.get("success"):
            _update_docs_job(
                repo_id,
                status="completed",
                finished_at=datetime.utcnow().isoformat(),
                result={
                    "coverage": result.get("coverage"),
                    "files_analyzed": result.get("files_analyzed"),
                    "languages": result.get("languages", []),
                    "docs_files_added": result.get("docs_files_added", 0)
                },
                error=None
            )
        else:
            _update_docs_job(
                repo_id,
                status="failed",
                finished_at=datetime.utcnow().isoformat(),
                error=result.get("error", "Documentation generation failed")
            )
    except Exception as e:
        _update_docs_job(
            repo_id,
            status="failed",
            finished_at=datetime.utcnow().isoformat(),
            error=str(e)
        )


def _generate_project_documentation_internal(repo_id: str, llm_model: str, db: Session, selected_docs: Optional[List[str]] = None, progress_callback=None) -> Dict[str, Any]:
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    commits_desc = CommitCRUD.get_commits_by_repository(db, repo_id, limit=200)
    if not commits_desc:
        return {
            "success": False,
            "error": "Repository has no commits. Push code first."
        }

    def _is_source_extension(path: str) -> bool:
        return Path(path).suffix.lower() in {
            ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts", ".go", ".rs",
            ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".php", ".rb", ".swift", ".sql",
            ".sh", ".ps1", ".yml", ".yaml", ".toml", ".json", ".md"
        }

    def _score_commit(commit_obj) -> tuple:
        non_doc_files = [
            f for f in commit_obj.files
            if f.file_path and not f.file_path.startswith("docs/") and f.file_object
        ]
        source_files = [f for f in non_doc_files if _is_source_extension(f.file_path)]

        approx_lines = 0
        sampled = 0
        for f in source_files:
            if sampled >= 120:
                break
            try:
                text = f.file_object.content.decode("utf-8", errors="ignore")
                approx_lines += len(text.splitlines())
            except Exception:
                continue
            sampled += 1

        return (len(source_files), approx_lines, len(non_doc_files))

    source_commit = None
    candidate_commits = [
        c for c in commits_desc
        if any(f.file_path and not f.file_path.startswith("docs/") for f in c.files)
    ]
    if candidate_commits:
        source_commit = max(candidate_commits, key=_score_commit)

    if not source_commit:
        return {
            "success": False,
            "error": "No source code files found in repository commits."
        }

    # ── Reconstruct the full HEAD file tree ──────────────────────────────────
    # Each commit only stores the files that changed in that push (delta model).
    # To get the COMPLETE project state at HEAD we must walk ALL commits newest→
    # oldest and keep the first (= latest) version of every path seen.
    _SKIP_DIR_PREFIXES = (
        "docs/", "node_modules/", "vendor/", ".git/", "venv/", ".venv/",
        "__pycache__/", "build/", "dist/", ".mypy_cache/", ".pytest_cache/",
        ".tox/", ".idea/", ".vscode/", ".next/", ".nuxt/", "_archived_",
        ".fox/",
    )
    _SKIP_BINARY_EXT = {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
        ".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls",
        ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
        ".exe", ".dll", ".so", ".dylib", ".lib", ".a",
        ".pyc", ".pyo", ".class", ".o",
        ".woff", ".woff2", ".ttf", ".eot", ".otf",
        ".mp4", ".mp3", ".avi", ".mov", ".wav",
        ".db", ".sqlite", ".sqlite3",
    }
    _MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MB per file

    # path → (content_bytes, file_hash, file_size)
    head_tree: Dict[str, tuple] = {}
    for commit in commits_desc:          # newest → oldest (already sorted)
        for cf in commit.files:
            path = cf.file_path
            if not path or path in head_tree:
                continue
            if any(path.startswith(pfx) for pfx in _SKIP_DIR_PREFIXES):
                continue
            if Path(path).suffix.lower() in _SKIP_BINARY_EXT:
                continue
            if cf.file_object and cf.file_object.content:
                raw = cf.file_object.content
                if len(raw) > _MAX_FILE_BYTES:
                    continue
                head_tree[path] = (raw, cf.file_hash, cf.file_size or len(raw))

    if not head_tree:
        return {
            "success": False,
            "error": "No readable source files found in repository history.",
        }

    print(f"  📂 HEAD tree: {len(head_tree)} files collected across all commits")

    temp_dir = Path("/tmp") / f"foxnest_project_docs_{repo_id}"
    # Always wipe the temp dir so stale files from a previous run don't pollute
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        for rel_path, (content_bytes, _fhash, _fsize) in head_tree.items():
            file_path = temp_dir / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(file_path, "wb") as f:
                    f.write(content_bytes)
            except Exception:
                pass

        docs_output = DOCS_DIR / repo_id / "project"

        # Isolated AutoDocs v2 pipeline with safe fallback to legacy generator.
        use_v2 = os.getenv("FOXNEST_AUTODOCS_V2_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
        if use_v2:
            try:
                service = autodocs_v2.AutoDocsV2Service(
                    templates_dir=Path(__file__).resolve().parent.parent / "docs" / "templates"
                )
                results = service.generate(
                    project_path=temp_dir,
                    output_dir=docs_output,
                    llm_model=llm_model,
                    selected_docs=selected_docs,
                    project_name_override=repository.name,
                )
            except Exception as v2_err:
                print(f"  ⚠ AutoDocs v2 failed, falling back to legacy generator: {v2_err}")
                results = project_docs_generator.generate_project_docs(
                    str(temp_dir),
                    output_dir=str(docs_output),
                    llm_model=llm_model,
                    project_name=repository.name,
                    selected_docs=selected_docs,
                    progress_callback=progress_callback
                )
        else:
            results = project_docs_generator.generate_project_docs(
                str(temp_dir),
                output_dir=str(docs_output),
                llm_model=llm_model,
                project_name=repository.name,
                selected_docs=selected_docs,
                progress_callback=progress_callback
            )

        if results.get("error"):
            return {
                "success": False,
                "error": results.get("error"),
                "repo_id": repo_id,
                "skipped_docs": results.get("skipped_docs", [])
            }

        if not results.get("generated_files"):
            return {
                "success": False,
                "error": "Failed to generate project documentation",
                "repo_id": repo_id,
                "skipped_docs": results.get("skipped_docs", [])
            }

        # Markdown → Word only after AutoDocs v2 has finished: Qwen, templates, repo-aware context,
        # and per-document validation/retries. Do not convert partial or failed runs.
        if (results.get("stats") or {}).get("pipeline") == "autodocs_v2":
            try:
                docx_utils.convert_project_autodocs_md_to_docx(docs_output)
                docx_utils.rewrite_autodocs_project_index(docs_output, repository.name)
            except Exception as conv_err:
                print(f"  ⚠ Project docs MD→DOCX (post-v2 only): {conv_err}")

        doc_files = []
        file_entries_by_path = {}

        def add_file_entry(path, file_hash, file_size):
            file_entries_by_path[path] = SimpleNamespace(
                file_path=path,
                file_hash=file_hash,
                file_size=file_size
            )

        # Register all HEAD-tree source files in the commit entries
        for rel_path, (_content, fhash, fsize) in head_tree.items():
            add_file_entry(rel_path, fhash, fsize)

        docs_root = DOCS_DIR / repo_id
        index_path = docs_root / "index.md"
        project_index_rel = "project/index.md"
        if index_path.exists():
            with open(index_path, "r", encoding="utf-8", errors="ignore") as f:
                index_content = f.read()
        else:
            index_content = "# Documentation\n\n"
            index_content += f"*Auto-generated by FoxNest on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"

        if project_index_rel not in index_content:
            index_content += "## Project Documentation\n\n"
            index_content += f"- [Project Docs]({project_index_rel})\n\n"

        api_index = docs_root / "api" / "index.md"
        if "api/index.md" not in index_content and api_index.exists():
            index_content += "## API Documentation\n\n"
            index_content += "- [API Reference](api/index.md)\n\n"

        dep_index = docs_root / "dependencies" / "index.md"
        if "dependencies/index.md" not in index_content and dep_index.exists():
            index_content += "## Dependencies\n\n"
            index_content += "- [Dependencies](dependencies/index.md)\n\n"

        index_path.parent.mkdir(parents=True, exist_ok=True)
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(index_content)

        def _is_valid_generated_doc(path: Path, content: bytes) -> bool:
            if not content or len(content) < 64:
                return False
            if path.suffix.lower() == ".md":
                text_content = content.decode("utf-8", errors="ignore")
                if len(text_content.strip()) < 120:
                    return False
                if "## " not in text_content and "# " not in text_content:
                    return False
                if re.search(r"\[(Project Name|Your Name)\]|TODO|YYYY-MM-DD", text_content, flags=re.IGNORECASE):
                    return False
            return True

        valid_doc_files = []
        if docs_root.exists():
            for doc_file_path in docs_root.rglob("*"):
                if not doc_file_path.is_file():
                    continue
                rel_path = doc_file_path.relative_to(docs_root)
                with open(doc_file_path, "rb") as f:
                    content = f.read()
                if _is_valid_generated_doc(doc_file_path, content):
                    valid_doc_files.append((rel_path, content))

        for rel_path, content in valid_doc_files:
            file_obj = FileObjectCRUD.store_file_object(db, content)
            doc_path = f"docs/{rel_path}"
            doc_files.append(doc_path)
            add_file_entry(doc_path, file_obj.hash, len(content))

        file_entries = list(file_entries_by_path.values())

        if doc_files:
            commit_id = hashlib.sha256(
                f"{repo_id}:project_docs:{datetime.now().isoformat()}".encode()
            ).hexdigest()[:40]

            commit_data = {
                "id": commit_id,
                "repository_id": repo_id,
                "author": repository.owner.username,
                "message": f"Auto-generate project documentation ({len(doc_files)} files)",
                "parent": commits_desc[0].id,
                "timestamp": datetime.now().isoformat()
            }

            CommitCRUD.create_commit_from_file_hashes(db, commit_data, file_entries)
        else:
            return {
                "success": False,
                "error": "Generated documentation failed validation; no docs were committed.",
                "repo_id": repo_id,
                "skipped_docs": results.get("skipped_docs", [])
            }

        ActivityCRUD.create_activity(
            db=db,
            user_id=repository.owner_id,
            activity_type="generate_project_docs",
            description=f"Generated comprehensive project docs for repo {repo_id}",
            repository_id=repo_id
        )

        return {
            "success": True,
            "message": "Project documentation generated and committed to repository",
            "repo_id": repo_id,
            "llm_model": llm_model,
            "docs_url": f"/api/repository/{repo_id}/docs/project",
            "documents": [Path(f).name for f in results.get("generated_files", [])],
            "skipped_docs": results.get("skipped_docs", []),
            "stats": results.get("stats", {}),
            "selected_docs": results.get("stats", {}).get("selected_docs", ["all"]),
            "project_info": {
                "name": results.get("project_info", {}).get("name", repository.name),
                "languages": list(results.get("project_info", {}).get("languages", [])),
                "frameworks": list(results.get("project_info", {}).get("frameworks", [])),
                "files": results.get("project_info", {}).get("file_count", 0),
                "lines": results.get("project_info", {}).get("line_count", 0),
            }
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _run_generate_project_docs_job(repo_id: str, llm_model: str, selected_docs: Optional[List[str]] = None):
    progress_steps, selected_doc_names, invalid = _resolve_progress_steps(selected_docs)
    if invalid:
        _update_docs_job(
            repo_id,
            status="failed",
            mode="project",
            llm_model=llm_model,
            finished_at=datetime.utcnow().isoformat(),
            error=f"Invalid doc names: {', '.join(invalid)}"
        )
        return

    _update_docs_job(
        repo_id,
        status="running",
        mode="project",
        llm_model=llm_model,
        selected_docs=selected_doc_names,
        started_at=datetime.utcnow().isoformat(),
        finished_at=None,
        error=None,
        docs_progress=[
            {"name": step, "status": "pending", "started_at": None, "finished_at": None}
            for step in progress_steps
        ]
    )
    db = SessionLocal()
    callback = _make_docs_progress_callback(repo_id)
    try:
        result = _generate_project_documentation_internal(
            repo_id=repo_id,
            llm_model=llm_model,
            db=db,
            selected_docs=selected_docs,
            progress_callback=callback
        )

        if isinstance(result, dict) and result.get("success"):
            _update_docs_job(
                repo_id,
                status="completed",
                mode="project",
                llm_model=result.get("llm_model", llm_model),
                finished_at=datetime.utcnow().isoformat(),
                result={
                    "documents": result.get("documents", []),
                    "docs_url": result.get("docs_url"),
                    "project_info": result.get("project_info", {}),
                    "skipped_docs": result.get("skipped_docs", []),
                    "selected_docs": result.get("selected_docs", selected_doc_names or ["all"]),
                },
                error=None
            )
        else:
            _update_docs_job(
                repo_id,
                status="failed",
                mode="project",
                llm_model=llm_model,
                finished_at=datetime.utcnow().isoformat(),
                error=(result or {}).get("error", "Project docs generation failed") if isinstance(result, dict) else "Project docs generation failed"
            )
    except Exception as e:
        _update_docs_job(
            repo_id,
            status="failed",
            mode="project",
            llm_model=llm_model,
            finished_at=datetime.utcnow().isoformat(),
            error=str(e)
        )
    finally:
        db.close()

@app.post("/api/repository/{repo_id}/generate-docs")
async def generate_repository_docs(
    repo_id: str,
    lang_filter: Optional[str] = None,
    selected_docs: Optional[str] = None,
    background: bool = True,
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db)
):
    """Generate documentation for a repository."""
    try:
        # Backward-compatible behavior for older clients:
        # If no explicit language filter is provided, treat generate-docs as
        # project-doc generation using the configured Qwen model.
        if not lang_filter:
            selected_docs_list = _parse_selected_docs_param(selected_docs)
            progress_steps, selected_doc_names, invalid = _resolve_progress_steps(selected_docs_list)
            if invalid:
                raise HTTPException(status_code=400, detail=f"Invalid doc names: {', '.join(invalid)}")

            # Verify repository exists quickly using request session
            repository = RepositoryCRUD.get_repository(db, repo_id)
            if not repository:
                raise HTTPException(status_code=404, detail="Repository not found")

            resolved_llm_model = os.getenv("FOXNEST_PROJECT_DOCS_MODEL", "qwen2.5-coder:32b")

            with DOCS_GENERATION_LOCK:
                existing = DOCS_GENERATION_JOBS.get(repo_id)
                if existing and existing.get("status") in {"queued", "running"}:
                    return {
                        "success": True,
                        "message": "Project documentation generation already in progress",
                        "repo_id": repo_id,
                        "status": existing.get("status", "running"),
                        "mode": existing.get("mode", "project"),
                        "llm_model": existing.get("llm_model", resolved_llm_model),
                        "selected_docs": existing.get("selected_docs", selected_doc_names or ["all"]),
                        "docs_url": f"/api/repository/{repo_id}/docs/project",
                        "status_url": f"/api/repository/{repo_id}/docs-status"
                    }

            _update_docs_job(
                repo_id,
                status="queued",
                mode="project",
                llm_model=resolved_llm_model,
                queued_at=datetime.utcnow().isoformat(),
                started_at=None,
                finished_at=None,
                error=None,
                result=None,
                lang_filter=None,
                selected_docs=selected_doc_names,
                docs_progress=[
                    {"name": step, "status": "pending", "started_at": None, "finished_at": None}
                    for step in progress_steps
                ]
            )
            background_tasks.add_task(_run_generate_project_docs_job, repo_id, resolved_llm_model, selected_docs_list)

            return {
                "success": True,
                "message": "Project documentation generation started in background",
                "repo_id": repo_id,
                "status": "processing",
                "mode": "project",
                "llm_model": resolved_llm_model,
                "selected_docs": selected_doc_names or ["all"],
                "docs_url": f"/api/repository/{repo_id}/docs/project",
                "status_url": f"/api/repository/{repo_id}/docs-status"
            }

        # Verify repository exists quickly using request session
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")
        if background:
            with DOCS_GENERATION_LOCK:
                existing = DOCS_GENERATION_JOBS.get(repo_id)
                if existing and existing.get("status") in {"queued", "running"}:
                    return {
                        "success": True,
                        "message": "Documentation generation already in progress",
                        "repo_id": repo_id,
                        "status": existing.get("status", "running"),
                        "docs_url": f"/api/repository/{repo_id}/docs",
                        "status_url": f"/api/repository/{repo_id}/docs-status"
                    }

            _update_docs_job(
                repo_id,
                status="queued",
                queued_at=datetime.utcnow().isoformat(),
                started_at=None,
                finished_at=None,
                error=None,
                result=None,
                lang_filter=lang_filter
            )
            background_tasks.add_task(_run_generate_docs_job, repo_id, lang_filter)

            return {
                "success": True,
                "message": "Documentation generation started in background",
                "repo_id": repo_id,
                "status": "processing",
                "docs_url": f"/api/repository/{repo_id}/docs",
                "status_url": f"/api/repository/{repo_id}/docs-status"
            }

        result = await run_in_threadpool(_generate_repository_docs_sync, repo_id, lang_filter)
        return result
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Documentation generation failed: {str(e)}")


@app.get("/api/repository/{repo_id}/docs-status")
async def get_repository_docs_status(repo_id: str):
    with DOCS_GENERATION_LOCK:
        job = DOCS_GENERATION_JOBS.get(repo_id)

    docs_path = DOCS_DIR / repo_id
    docs_exist = docs_path.exists() and any(docs_path.rglob("*.md"))

    if not job:
        return {
            "success": True,
            "repo_id": repo_id,
            "status": "not_started",
            "docs_available": bool(docs_exist)
        }

    return {
        "success": True,
        "repo_id": repo_id,
        "status": job.get("status", "unknown"),
        "queued_at": job.get("queued_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
        "result": job.get("result"),
        "docs_progress": job.get("docs_progress", []),
        "docs_available": bool(docs_exist)
    }


@app.get("/api/repository/{repo_id}/docs")
async def get_repository_docs(
    repo_id: str,
    db: Session = Depends(get_db),
    actor: Optional[User] = Depends(get_optional_user),
):
    """Get list of generated documentation files for a repository."""
    try:
        # Verify repository exists
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        if not actor:
            raise HTTPException(status_code=401, detail="Authentication required")
        _require_repository_read_access(db, actor, repository, "view docs")
        
        docs_path = DOCS_DIR / repo_id
        if not docs_path.exists():
            return {
                "success": False,
                "error": "No documentation generated yet. Use POST /api/repository/{repo_id}/generate-docs first.",
                "repo_id": repo_id
            }
        
        # List all doc files
        doc_files = []
        for file_path in docs_path.rglob("*"):
            if file_path.is_file():
                rel_path = file_path.relative_to(docs_path)
                doc_files.append({
                    "path": str(rel_path),
                    "size": file_path.stat().st_size,
                    "url": f"/api/repository/{repo_id}/docs/{rel_path}"
                })
        
        # Load metadata if exists
        meta_file = docs_path / "docs_meta.json"
        metadata = None
        if meta_file.exists():
            with open(meta_file, "r") as f:
                metadata = json.load(f)
        
        return {
            "success": True,
            "repo_id": repo_id,
            "docs_path": str(docs_path),
            "files": doc_files,
            "metadata": metadata,
            "index_url": f"/api/repository/{repo_id}/docs/index.md",
            "archive_url": f"/api/repository/{repo_id}/docs/archive.zip",
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve docs: {str(e)}")


@app.get("/api/repository/{repo_id}/docs/archive.zip")
async def download_repository_docs_archive(
    repo_id: str,
    db: Session = Depends(get_db),
    actor: Optional[User] = Depends(get_optional_user),
):
    """Download all generated documentation files as a single ZIP."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not actor:
        raise HTTPException(status_code=401, detail="Authentication required")
    _require_repository_read_access(db, actor, repository, "download docs")
    docs_path = DOCS_DIR / repo_id
    if not docs_path.exists() or not docs_path.is_dir():
        raise HTTPException(status_code=404, detail="No documentation generated for this repository")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in docs_path.rglob("*"):
            if file_path.is_file():
                arc = str(file_path.relative_to(docs_path))
                zf.write(file_path, arcname=arc)
    buffer.seek(0)
    safe_name = f"{repository.name or repo_id}-documentation.zip"
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@app.get("/api/repository/{repo_id}/docs/{file_path:path}")
async def serve_repository_doc_file(
    repo_id: str,
    file_path: str,
    db: Session = Depends(get_db),
    actor: Optional[User] = Depends(get_optional_user),
):
    """Serve a specific documentation file."""
    try:
        # Verify repository exists
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")
        if not actor:
            raise HTTPException(status_code=401, detail="Authentication required")
        _require_repository_read_access(db, actor, repository, "view docs")
        
        docs_path = DOCS_DIR / repo_id
        if not docs_path.exists():
            raise HTTPException(status_code=404, detail="No documentation generated for this repository")
        
        full_path = docs_path / file_path
        
        # Security check: prevent path traversal
        if not str(full_path.resolve()).startswith(str(docs_path.resolve())):
            raise HTTPException(status_code=403, detail="Access denied")
        
        if not full_path.exists() or not full_path.is_file():
            raise HTTPException(status_code=404, detail="Documentation file not found")
        
        # Determine media type
        media_type = "text/plain"
        if file_path.endswith(".md"):
            media_type = "text/markdown"
        elif file_path.endswith(".html"):
            media_type = "text/html"
        elif file_path.endswith(".json"):
            media_type = "application/json"
        elif file_path.endswith(".css"):
            media_type = "text/css"
        elif file_path.endswith(".js"):
            media_type = "application/javascript"
        
        # Prefer inline viewing in the browser for text docs (avoid "only one download" confusion).
        disposition = "inline" if media_type.startswith("text/") or media_type in (
            "application/javascript",
            "application/json",
        ) else "attachment"

        return FileResponse(
            path=full_path,
            media_type=media_type,
            filename=full_path.name,
            content_disposition_type=disposition,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to serve documentation file: {str(e)}")


@app.post("/api/repository/{repo_id}/generate-project-docs")
async def generate_project_documentation(
    repo_id: str,
    llm_model: Optional[str] = None,
    selected_docs: Optional[str] = None,
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db)
):
    """Generate comprehensive project documentation (README, API, Architecture, etc.) using Qwen LLM."""
    try:
        # Verify repository exists
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")
        
        resolved_llm_model = llm_model or os.getenv("FOXNEST_PROJECT_DOCS_MODEL", "qwen2.5-coder:32b")
        selected_docs_list = _parse_selected_docs_param(selected_docs)
        progress_steps, selected_doc_names, invalid = _resolve_progress_steps(selected_docs_list)
        if invalid:
            raise HTTPException(status_code=400, detail=f"Invalid doc names: {', '.join(invalid)}")
        
        # Check if docs generation is already in progress
        with DOCS_GENERATION_LOCK:
            existing = DOCS_GENERATION_JOBS.get(repo_id)
            if existing and existing.get("status") in {"queued", "running"}:
                return {
                    "success": True,
                    "message": "Project documentation generation already in progress",
                    "repo_id": repo_id,
                    "status": existing.get("status", "processing"),
                    "llm_model": resolved_llm_model,
                    "selected_docs": existing.get("selected_docs", selected_doc_names or ["all"]),
                    "docs_url": f"/api/repository/{repo_id}/docs/project",
                    "status_url": f"/api/repository/{repo_id}/docs-status"
                }
        
        # Queue the docs generation for background processing
        _update_docs_job(
            repo_id,
            status="queued",
            mode="project",
            llm_model=resolved_llm_model,
            queued_at=datetime.utcnow().isoformat(),
            started_at=None,
            finished_at=None,
            error=None,
            result=None,
            selected_docs=selected_doc_names,
            docs_progress=[
                {"name": step, "status": "pending", "started_at": None, "finished_at": None}
                for step in progress_steps
            ]
        )
        
        # Add task to background queue - don't wait for it
        background_tasks.add_task(_run_generate_project_docs_job, repo_id, resolved_llm_model, selected_docs_list)
        
        return {
            "success": True,
            "message": "Project documentation generation started in background",
            "repo_id": repo_id,
            "status": "processing",
            "llm_model": resolved_llm_model,
            "selected_docs": selected_doc_names or ["all"],
            "docs_url": f"/api/repository/{repo_id}/docs/project",
            "status_url": f"/api/repository/{repo_id}/docs-status"
        }
    except HTTPException:
        raise
    except Exception as e:
        return {
            "success": False,
            "error": f"Error queuing documentation generation: {str(e)}"
        }


if __name__ == "__main__":
    print(f"Starting FoxNest Server v2.0 with SQL Database...")
    print(f"Database URL: {os.getenv('DATABASE_URL', 'sqlite:///./foxnest.db')}")
    
    host = os.getenv("SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("SERVER_PORT", "33333"))
    debug = os.getenv("DEBUG", "True").lower() == "true"
    
    # Increase timeout for large file uploads
    uvicorn.run(
        app, 
        host=host, 
        port=port, 
        log_level="info" if not debug else "debug",
        timeout_keep_alive=300,  # 5 minutes keep-alive
    )
