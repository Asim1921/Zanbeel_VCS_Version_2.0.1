"""Branch listing and mutation."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.crud import ActivityCRUD, BranchCRUD, CommitCRUD, RepositoryCRUD
from database.database import get_db
from database.models import User

from app.core.dependencies import get_current_user
from app.core.permissions import (
    _require_repository_checkout_access, _require_repository_read_access,
    can_manage_repository, require_repository_access, user_sees_all_repository_branches,
)
from app.schemas import (
    BranchCreateRequest, BranchHeadUpdateRequest, BranchMergeRequest,
    BranchPolicyRequest, BranchPublishRequest, BranchRenameRequest, CopyFilesRequest,
)
from app.services import branch_ops
from app.services.branches import _get_default_branch, _normalize_branch_name


router = APIRouter()


@router.get("/api/repository/{repo_id}/branches")
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


@router.post("/api/repository/{repo_id}/branches")
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

    # The repository's branch policy may demand more than plain write access.
    try:
        branch_ops.enforce(db, current_user, repository, "create_branch", branch_name)
    except branch_ops.BranchOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    if BranchCRUD.get_branch(db, repo_id, branch_name):
        raise HTTPException(status_code=400, detail="Branch already exists")

    head_commit_id = request.from_commit
    if not head_commit_id and request.from_branch:
        source = BranchCRUD.get_branch(db, repo_id, _normalize_branch_name(request.from_branch))
        if not source:
            raise HTTPException(
                status_code=404, detail=f"Branch '{request.from_branch}' not found"
            )
        head_commit_id = source.head_commit_id
    head_commit_id = head_commit_id or repository.head_commit_id
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

@router.put("/api/repository/{repo_id}/branches/{branch_name}/rename")
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

@router.put("/api/repository/{repo_id}/branches/{branch_name}/head")
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

@router.put("/api/repository/{repo_id}/branches/{branch_name}/default")
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

@router.delete("/api/repository/{repo_id}/branches/{branch_name}")
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


def _branch_op(fn, db, repo_id, current_user, *args, **kwargs):
    """Shared plumbing: resolve the repository, run the operation, translate errors."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    try:
        return repository, fn(db, repository, current_user, *args, **kwargs)
    except branch_ops.BranchOpError as exc:
        if exc.payload:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"message": exc.message, **exc.payload},
            )
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get("/api/repository/{repo_id}/branch-policy")
async def get_branch_policy(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The repository's branch protection policy, plus the caller's own scope.

    Returns the built-in defaults when the repository has never set one.
    """
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view branch policy")

    return {
        "success": True,
        "policy": branch_ops.get_policy(repository),
        "your_scope": branch_ops.actor_scope(db, current_user, repository),
        "default_branch": _get_default_branch(db, repo_id).name,
    }


@router.put("/api/repository/{repo_id}/branch-policy")
async def update_branch_policy(
    repo_id: str,
    request: BranchPolicyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Replace the branch protection policy. Requires manage access."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not can_manage_repository(db, current_user, repository):
        raise HTTPException(
            status_code=403,
            detail="Changing the branch policy requires manage access on this repository.",
        )

    supplied = {k: v for k, v in request.model_dump().items() if v is not None}
    try:
        policy = branch_ops.set_policy(db, repository, supplied)
    except branch_ops.BranchOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    ActivityCRUD.create_activity(
        db, current_user.id, "update_branch_policy",
        f"Updated branch policy ({len(policy.get('protected_branches', []))} protected branch(es))",
        repo_id,
    )
    return {"success": True, "policy": policy}


@router.post("/api/repository/{repo_id}/branches/merge")
async def merge_branches(
    repo_id: str,
    request: BranchMergeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Merge one branch into another without going through a pull request."""
    _repo, result = _branch_op(
        branch_ops.merge_branches, db, repo_id, current_user,
        request.source_branch, request.target_branch,
        expected_head_commit_id=request.expected_head_commit_id,
        dry_run=request.dry_run,
    )
    return {"success": True, **result}


@router.post("/api/repository/{repo_id}/branches/publish")
async def publish_branch(
    repo_id: str,
    request: BranchPublishRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fast-forward one branch to another.

    Refuses when the target has diverged rather than quietly creating a merge commit --
    the caller asked for the safe operation.
    """
    _repo, result = _branch_op(
        branch_ops.publish_branch, db, repo_id, current_user,
        request.source_branch, request.target_branch,
        expected_head_commit_id=request.expected_head_commit_id,
    )
    return {"success": True, **result}


@router.post("/api/repository/{repo_id}/branches/copy-files")
async def copy_files_between_branches(
    repo_id: str,
    request: CopyFilesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Take specific paths from one branch onto another, leaving the rest untouched.

    This is the endpoint `fox` has been calling since it was written; the server never
    implemented it.
    """
    _repo, result = _branch_op(
        branch_ops.copy_files, db, repo_id, current_user,
        request.source_branch, request.target_branch, request.paths,
        expected_head_commit_id=request.expected_head_commit_id,
    )
    return {"success": True, **result}
