"""Search endpoints for repositories, commits, code and file paths.

All require authentication and return only what the caller may already read, so search
cannot be used to discover repositories they have no access to.

Sync on purpose: a code search can read files off disk for up to a few seconds, and in
an ``async def`` that would hold up every other request while it ran.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import User

from app.core.dependencies import get_current_user, require_admin_user
from app.services import search as search_service
from app.services import search_index


router = APIRouter()


def _answer(call):
    try:
        return {"success": True, **call()}
    except search_service.SearchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get("/api/search/repositories")
def search_repositories(
    q: Optional[str] = None,
    owner: Optional[str] = None,
    archived: Optional[bool] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Find repositories by name, description or owner."""
    return _answer(lambda: search_service.search_repositories(
        db, current_user, query=q, owner=owner, archived=archived, limit=limit, offset=offset))


@router.get("/api/search/commits")
def search_commits(
    q: Optional[str] = None,
    repository_id: Optional[str] = None,
    author: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Find commits by message or id prefix, optionally in one repository or by one author."""
    return _answer(lambda: search_service.search_commits(
        db, current_user, query=q or "", repository_id=repository_id, author=author,
        limit=limit, offset=offset))


@router.get("/api/search/code")
def search_code(
    q: str,
    repository_id: Optional[str] = None,
    branch: Optional[str] = None,
    path: Optional[str] = None,
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = Query(search_service.MAX_MATCHES, ge=1, le=search_service.MAX_MATCHES),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Search file contents: every readable repository, or one repository at a branch."""
    return _answer(lambda: search_service.search_code(
        db, current_user, repository_id=repository_id, query=q, branch=branch,
        path_filter=path, regex=regex, case_sensitive=case_sensitive, limit=limit))


@router.get("/api/search/paths")
def search_paths(
    q: str,
    repository_id: Optional[str] = None,
    branch: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Go to file: paths containing every word of the query."""
    return _answer(lambda: search_service.search_paths(
        db, current_user, query=q, repository_id=repository_id, branch=branch, limit=limit))


@router.get("/api/admin/search-index")
def search_index_status(current_user: User = Depends(require_admin_user)):
    """How much is indexed and what is queued."""
    return {"success": True, "index": search_index.status()}


@router.post("/api/admin/search-index/rebuild")
def rebuild_search_index(current_user: User = Depends(require_admin_user)):
    """Drop the index and rebuild it from every repository's default branch."""
    if not search_index.enabled():
        raise HTTPException(status_code=503, detail="The search index is disabled on this server")
    queued = search_index.rebuild()
    return {"success": True, "queued": queued, "index": search_index.status()}
