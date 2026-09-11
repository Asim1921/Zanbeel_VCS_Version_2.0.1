"""Search endpoints for repositories, commits and code.

All three require authentication and return only what the caller may already
read, so search cannot be used to discover repositories they have no access to.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import User

from app.core.dependencies import get_current_user
from app.services import search as search_service


router = APIRouter()


@router.get("/api/search/repositories")
async def search_repositories(
    q: Optional[str] = None,
    owner: Optional[str] = None,
    archived: Optional[bool] = None,
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Find repositories by name, description or owner."""
    try:
        return {"success": True, **search_service.search_repositories(
            db, current_user, query=q, owner=owner, archived=archived, limit=limit
        )}
    except search_service.SearchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get("/api/search/commits")
async def search_commits(
    q: Optional[str] = None,
    repository_id: Optional[str] = None,
    author: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Find commits by message, optionally scoped to a repository or author."""
    try:
        return {"success": True, **search_service.search_commits(
            db, current_user, query=q or "", repository_id=repository_id,
            author=author, limit=limit,
        )}
    except search_service.SearchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get("/api/search/code")
async def search_code(
    repository_id: str,
    q: str,
    branch: Optional[str] = None,
    path: Optional[str] = None,
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = Query(search_service.MAX_MATCHES, ge=1, le=search_service.MAX_MATCHES),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Search file contents at a branch tip.

    Scoped to one repository on purpose — see the module docstring in
    ``app/services/search.py`` for why a store-wide content scan is not offered.
    """
    try:
        return {"success": True, **search_service.search_code(
            db, current_user, repository_id=repository_id, query=q, branch=branch,
            path_filter=path, regex=regex, case_sensitive=case_sensitive, limit=limit,
        )}
    except search_service.SearchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
