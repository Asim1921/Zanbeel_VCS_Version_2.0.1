"""Branch listing and mutation."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.crud import ActivityCRUD, BranchCRUD, CommitCRUD, RepositoryCRUD
from database.database import get_db
from database.models import MergeConflictSession, User

from app.core.dependencies import get_current_user
from app.core.permissions import (
    _require_repository_checkout_access, _require_repository_read_access,
    can_manage_repository, require_repository_access, user_sees_all_repository_branches,
)
from app.schemas import (
    BranchCreateRequest, BranchHeadUpdateRequest, BranchMergeRequest,
    BranchPolicyRequest, BranchPublishRequest, BranchRenameRequest, CopyFilesRequest,
    ResolveConflictsRequest,
)
from app.services import branch_ops, merge_conflicts, refs
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

    refs.check_reference_operation(
        db, repository=repository, actor=current_user, branch_name=branch_name,
        operation=refs.OP_CREATE, new_commit_id=head_commit_id,
    )
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

    branch = refs.rename_reference(
        db, repository=repository, actor=current_user,
        old_name=old_name, new_name=new_name,
    )
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

    # Straight through the reference service: this endpoint could previously move any
    # branch to any commit on a bare "manage" check, protection notwithstanding.
    branch = refs.update_reference(
        db, repository=repository, actor=current_user,
        branch_name=_normalize_branch_name(branch_name),
        new_commit_id=request.head_commit_id,
        expected_commit_id=getattr(request, "expected_head_commit_id", None),
    )
    return {
        "success": True,
        "branch": {
            "name": branch.name,
            "head_commit_id": branch.head_commit_id,
            "generation": branch.generation,
        },
    }

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

    refs.delete_reference(
        db, repository=repository, actor=current_user, branch_name=branch.name
    )
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


# --- conflict resolution for a direct branch merge --------------------------------
# A conflicted `fox merge` used to report the conflict and stop: resolution sessions
# belonged to pull requests, so there was nowhere to park the three sides. These are
# the same session endpoints, addressed by branch pair rather than by pull request.

def _load_branch_session(db: Session, repo_id: str, session_id: int):
    session = (
        db.query(MergeConflictSession)
        .filter(
            MergeConflictSession.id == session_id,
            MergeConflictSession.repository_id == repo_id,
        )
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Merge session not found")
    if session.pull_request_id is not None:
        # Addressed here it would skip the pull request's own review gate.
        raise HTTPException(
            status_code=400,
            detail={
                "code": "WRONG_SESSION_KIND",
                "message": f"Session {session_id} belongs to pull request "
                           f"#{session.pull_request_id}; resolve it there.",
                "pull_request_id": session.pull_request_id,
            },
        )
    return session


@router.post("/api/repository/{repo_id}/branches/conflicts")
async def start_branch_merge_conflict_session(
    repo_id: str,
    request: BranchMergeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Park a conflicted branch merge for manual resolution.

    Reuses an open session for the same pair when neither branch has moved, so
    reopening the resolver does not discard work already done.
    """
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository,
                              "resolve merge conflicts in", required_scope="manage")

    # Refuse on a branch that would never accept the result, rather than inviting
    # someone to work through a resolution that cannot land.
    refs.check_reference_operation(
        db, repository=repository, actor=current_user,
        branch_name=request.target_branch, operation=refs.OP_MERGE,
    )

    try:
        session = merge_conflicts.open_branch_session(
            db, repo_id, current_user,
            source_branch=request.source_branch,
            target_branch=request.target_branch,
        )
    except merge_conflicts.ConflictError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    return {"success": True, **merge_conflicts.get_bundle(db, session)}


@router.get("/api/repository/{repo_id}/branches/conflicts/{session_id}")
async def get_branch_merge_conflicts(
    repo_id: str,
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """All three sides of every conflicted file in a branch merge session."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view merge conflicts")

    session = _load_branch_session(db, repo_id, session_id)
    return {"success": True, **merge_conflicts.get_bundle(db, session)}


@router.post("/api/repository/{repo_id}/branches/conflicts/{session_id}/resolve")
async def resolve_branch_merge_conflicts(
    repo_id: str,
    session_id: int,
    request: ResolveConflictsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Apply the resolutions and complete the branch merge.

    Every conflicted path must be resolved; a partial submission is refused rather than
    merged with the remainder guessed. The target branch's review rules still apply --
    resolving conflicts is not a way to land unreviewed content.
    """
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository,
                              "resolve merge conflicts in", required_scope="manage")

    session = _load_branch_session(db, repo_id, session_id)
    try:
        result = merge_conflicts.resolve(
            db, session, request.resolutions, current_user,
            expected_head_commit_id=request.expected_head_commit_id,
        )
    except merge_conflicts.ConflictError as exc:
        raise HTTPException(status_code=exc.status_code,
                            detail={"message": exc.message, **exc.payload})
    return {"success": True, **result}


@router.post("/api/repository/{repo_id}/branches/conflicts/{session_id}/abort")
async def abort_branch_merge_conflicts(
    repo_id: str,
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Discard a branch merge session without merging anything."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository,
                              "resolve merge conflicts in", required_scope="manage")

    session = _load_branch_session(db, repo_id, session_id)
    try:
        result = merge_conflicts.abort(db, session, current_user)
    except merge_conflicts.ConflictError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return {"success": True, **result}


@router.get("/api/repository/{repo_id}/branches/conflicts")
async def list_branch_merge_sessions(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Open branch merge sessions, so an interrupted resolution can be found again."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view merge conflicts")

    rows = (
        db.query(MergeConflictSession)
        .filter(
            MergeConflictSession.repository_id == repo_id,
            MergeConflictSession.pull_request_id.is_(None),
            MergeConflictSession.status == "open",
        )
        .order_by(MergeConflictSession.id.desc())
        .all()
    )
    return {
        "success": True,
        "sessions": [
            {
                "id": row.id,
                "source_branch": row.source_branch,
                "target_branch": row.target_branch,
                "status": row.status,
                "files": len(row.files),
                "unresolved": sum(1 for f in row.files if not f.resolved_file_hash),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
    }
