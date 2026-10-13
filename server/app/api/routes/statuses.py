"""Commit status checks — the endpoints a CI system reports to.

Reporting requires write access to the repository. A CI runner should use an access
token with the ``repo:status`` scope: it can report statuses (and read) but cannot push
code, so a leaked CI token cannot change what is being tested. ``repo:write`` also
reports, as before. Reading requires only read access, so a dashboard can show the
state of a build without being able to fake one.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.crud import RepositoryCRUD
from database.database import get_db
from database.models import User

from app.core.dependencies import get_current_user
from app.core.permissions import _require_repository_read_access, require_repository_access
from app.services import access_tokens as token_service
from app.services import status_checks
from app.services import webhooks as hook_service


router = APIRouter()


class StatusReportRequest(BaseModel):
    context: str = Field(..., min_length=1, max_length=120)
    state: str = Field(..., min_length=1, max_length=20)
    description: Optional[str] = Field(None, max_length=300)
    target_url: Optional[str] = Field(None, max_length=500)


def _repository_or_404(db: Session, repo_id: str):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    return repository


@router.post("/api/repository/{repo_id}/commits/{commit_id}/statuses")
async def report_status(
    repo_id: str,
    commit_id: str,
    request: StatusReportRequest,
    http_request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Report a build or test result against a commit."""
    repository = _repository_or_404(db, repo_id)
    require_repository_access(
        db, current_user.username, repository, "report status in", required_scope="write"
    )
    # The central scope check lets repo:status tokens reach this route; anything
    # scoped narrower (repo:read) was already refused there.
    scopes = getattr(http_request.state, "token_scopes", None)
    if scopes is not None and token_service.SCOPE_REPO_STATUS not in scopes:
        raise HTTPException(
            status_code=403,
            detail="Reporting a status needs a token with the 'repo:status' scope.",
        )

    try:
        status = status_checks.report(
            db,
            repository,
            commit_id,
            context=request.context,
            state=request.state,
            description=request.description,
            target_url=request.target_url,
            reporter_id=current_user.id,
        )
    except status_checks.StatusError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    hook_service.dispatch(db, hook_service.EVENT_STATUS_REPORTED, repo_id, {
        "repository": {"id": repo_id, "name": repository.name},
        "status": status_checks.serialize(status),
    })
    try:
        from app.services import notifications as notify
        notify.required_check_failed(db, repository, status)
    except Exception:
        db.rollback()

    return {
        "success": True,
        "status": status_checks.serialize(status),
        "combined": status_checks.combined(db, repository, commit_id),
    }


@router.get("/api/repository/{repo_id}/commits/{commit_id}/statuses")
async def get_statuses(
    repo_id: str,
    commit_id: str,
    history: bool = False,
    branch: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The current state of each check on a commit, or the full report history.

    ``branch`` says which branch the commit is headed for, so the required checks
    shown are that branch's: the repository-wide list plus its protection rules.
    """
    repository = _repository_or_404(db, repo_id)
    _require_repository_read_access(db, current_user, repository, "read statuses")

    if history:
        return {
            "success": True,
            "history": [
                status_checks.serialize(row)
                for row in status_checks.history(db, commit_id, repository_id=repository.id)
            ],
        }

    return {"success": True, "combined": status_checks.combined(db, repository, commit_id, branch)}


@router.get("/api/repository/{repo_id}/required-checks")
async def get_required_checks(
    repo_id: str,
    branch: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Which checks must pass before a merge, and who may report each.

    Without ``branch``, the repository-wide list; with it, everything that applies to
    merges into that branch.
    """
    repository = _repository_or_404(db, repo_id)
    _require_repository_read_access(db, current_user, repository, "read required checks")
    rules = status_checks.required_check_rules(db, repository, branch)
    return {
        "success": True,
        "branch": branch,
        "required_status_checks": [rule.context for rule in rules],
        "required_checks": [rule.as_dict() for rule in rules],
    }
