"""Commit status checks — the endpoints a CI system reports to.

Reporting requires write access to the repository, which is what a CI runner's
``repo:write`` access token gives it. Reading requires only read access, so a
dashboard can show the state of a build without being able to fake one.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.crud import RepositoryCRUD
from database.database import get_db
from database.models import User

from app.core.dependencies import get_current_user
from app.core.permissions import _require_repository_read_access, require_repository_access
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
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Report a build or test result against a commit."""
    repository = _repository_or_404(db, repo_id)
    require_repository_access(
        db, current_user.username, repository, "report status in", required_scope="write"
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
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The current state of each check on a commit, or the full report history."""
    repository = _repository_or_404(db, repo_id)
    _require_repository_read_access(db, current_user, repository, "read statuses")

    if history:
        return {
            "success": True,
            "history": [
                status_checks.serialize(row)
                for row in status_checks.history(db, commit_id)
            ],
        }

    return {"success": True, "combined": status_checks.combined(db, repository, commit_id)}


@router.get("/api/repository/{repo_id}/required-checks")
async def get_required_checks(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Which check contexts this repository requires before a merge."""
    repository = _repository_or_404(db, repo_id)
    _require_repository_read_access(db, current_user, repository, "read required checks")
    return {"success": True, "required_status_checks": status_checks.required_checks(repository)}
