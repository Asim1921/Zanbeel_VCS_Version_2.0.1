"""The activity log, as a feed for everyone and an audit view for administrators.

Everyone sees what happened in the repositories they can read, plus events about their
own account. Administrators (the role, on a password session or an admin-scoped token)
see everything, including the client address of each change, and can export it.

It used to return every row on the server to anyone signed in -- repository names,
users and commit subjects from repositories the reader could not open -- and filtered
nothing: the web page fetched up to 500 rows and searched those in the browser.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from database.crud import RepositoryCRUD
from database.database import get_db
from database.models import User

from app.core.dependencies import get_current_user, has_admin_powers, require_admin_user
from app.core.pagination import paginate_query, set_page_headers
from app.core.permissions import _require_repository_read_access, readable_repository_ids
from app.services import audit


router = APIRouter()

EXPORT_MAX_ROWS = 50_000


def _types(raw: Optional[str]):
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _query(db, viewer, admin, *, repository_id=None, **filters):
    try:
        return audit.build_query(
            db,
            readable=readable_repository_ids(db, viewer),
            viewer_id=viewer.id,
            all_account_events=admin,
            repository_id=repository_id,
            **filters,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _filters(user, type, since, until, q, target_type, target_id, commit):
    return {
        "user": user, "types": _types(type), "since": since, "until": until, "text": q,
        "target_type": target_type, "target_id": target_id, "commit_id": commit,
    }


@router.get("/api/activities")
def list_activities(
    request: Request,
    response: Response,
    limit: Optional[int] = Query(20, ge=1, le=500),
    offset: Optional[int] = Query(0, ge=0),
    user: Optional[str] = None,
    repository_id: Optional[str] = None,
    type: Optional[str] = Query(None, description="One type, or several separated by commas"),
    since: Optional[str] = None,
    until: Optional[str] = None,
    q: Optional[str] = Query(None, description="Words in the description"),
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    commit: Optional[str] = Query(None, description="A commit id or its prefix"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Activity the caller may see, newest first, filtered and paged in the database."""
    admin = has_admin_powers(request, current_user)
    query = _query(db, current_user, admin, repository_id=repository_id,
                   **_filters(user, type, since, until, q, target_type, target_id, commit))
    rows, meta = paginate_query(query, limit, offset)
    set_page_headers(request, response, meta)
    return {
        "success": True,
        "pagination": meta,
        "activities": [audit.serialize(row, include_ip=admin) for row in rows],
    }


@router.get("/api/activities/types")
def activity_types(current_user: User = Depends(get_current_user)):
    """Every activity type with its label and category, for filters and display."""
    return {
        "success": True,
        "types": [
            {"type": name, "label": entry["label"], "category": entry["category"]}
            for name, entry in audit.TYPES.items()
        ],
        "categories": [audit.CATEGORY_CODE, audit.CATEGORY_REVIEW, audit.CATEGORY_REPOSITORY,
                       audit.CATEGORY_SETTINGS, audit.CATEGORY_ACCESS, audit.CATEGORY_OPERATIONS],
    }


@router.get("/api/activities/export")
def export_activities(
    request: Request,
    user: Optional[str] = None,
    repository_id: Optional[str] = None,
    type: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    q: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    commit: Optional[str] = None,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """The filtered log as CSV, for an auditor's spreadsheet. Administrators only."""
    query = _query(db, current_user, True, repository_id=repository_id,
                   **_filters(user, type, since, until, q, target_type, target_id, commit))
    rows = query.limit(EXPORT_MAX_ROWS).all()
    return Response(
        content=audit.to_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="zanbeel-activity.csv"',
            "X-Zanbeel-Rows": str(len(rows)),
            "X-Zanbeel-Truncated": "1" if len(rows) >= EXPORT_MAX_ROWS else "0",
        },
    )


@router.get("/api/repository/{repo_id}/activity")
def repository_activity(
    repo_id: str,
    request: Request,
    response: Response,
    limit: Optional[int] = Query(30, ge=1, le=500),
    offset: Optional[int] = Query(0, ge=0),
    user: Optional[str] = None,
    type: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    q: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    commit: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One repository's activity, for anyone who can read the repository."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view activity")
    admin = has_admin_powers(request, current_user)
    query = _query(db, current_user, admin, repository_id=repo_id,
                   **_filters(user, type, since, until, q, target_type, target_id, commit))
    rows, meta = paginate_query(query, limit, offset)
    set_page_headers(request, response, meta)
    return {
        "success": True,
        "repository": {"id": repository.id, "name": repository.name},
        "pagination": meta,
        "activities": [audit.serialize(row, include_ip=admin) for row in rows],
    }
