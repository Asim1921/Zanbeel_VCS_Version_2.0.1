"""File- and branch-level rollback endpoints."""

import hashlib

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from database.crud import ActivityCRUD, BranchCRUD, CommitCRUD, RepositoryCRUD
from database.database import get_db
from database.models import User

from app.config import logger
from app.core.dependencies import get_current_user
from app.core.errors import _raise_head_mismatch
from app.core.permissions import can_manage_repository, can_write_repository
from app.schemas import (
    BranchRollbackRequest, CherryPickRequest, FileRollbackRequest, RebaseRequest,
    RevertRequest,
)
from app.services.branches import _get_default_branch, _resolve_branch
from app.services.commit_graph import _get_commit_tree, _is_ancestor
from app.services.history_ops import HistoryOpError, cherry_pick, rebase, revert
from app.services.merge import _tree_to_commit_payload


router = APIRouter()


@router.post("/api/repository/{repo_id}/rollback/file")
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

@router.post("/api/repository/{repo_id}/rollback/branch")
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


def _run_history_op(op, db, repo_id, request, current_user, verb):
    """Shared plumbing for cherry-pick and revert: permissions, optimistic concurrency,
    conflict reporting and the activity record."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not can_write_repository(db, current_user, repository):
        raise HTTPException(
            status_code=403,
            detail=f"You do not have permission to {verb} in this repository.",
        )

    branch = BranchCRUD.get_branch(db, repo_id, request.branch)
    if not branch:
        raise HTTPException(status_code=404, detail=f"Branch '{request.branch}' not found")
    if request.expected_head_commit_id is not None and \
            request.expected_head_commit_id != branch.head_commit_id:
        _raise_head_mismatch(branch.name, request.expected_head_commit_id, branch.head_commit_id)

    try:
        result = op(
            db, repo_id, request.commit_id, request.branch, current_user.username,
            mainline=request.mainline, message=request.message, dry_run=request.dry_run,
        )
    except HistoryOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    if result["status"] == "conflicts":
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "code": "MERGE_CONFLICT",
                **result,
                "message": (
                    f"{verb.capitalize()} touches files that changed on "
                    f"'{request.branch}'. Resolve these paths and retry."
                ),
            },
        )

    if result.get("commit_id"):
        ActivityCRUD.create_activity(
            db, current_user.id, verb.replace(" ", "_"),
            f"{verb} {request.commit_id[:8]} on '{request.branch}' as {result['commit_id'][:8]}",
            repo_id,
        )

    return {"success": True, **result}


@router.post("/api/repository/{repo_id}/cherry-pick")
async def cherry_pick_commit(
    repo_id: str,
    request: CherryPickRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Replay one commit's change on top of another branch.

    Adds a new commit rather than rewriting history. Overlapping edits come back as 409
    with the conflicting paths. `dry_run` reports whether it would apply cleanly.
    """
    return _run_history_op(cherry_pick, db, repo_id, request, current_user, "cherry-pick")


@router.post("/api/repository/{repo_id}/revert")
async def revert_commit(
    repo_id: str,
    request: RevertRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a commit that undoes an earlier one.

    The original commit is left in place, so the revert is itself revertible and nothing
    already pushed is mutated.
    """
    return _run_history_op(revert, db, repo_id, request, current_user, "revert")


@router.post("/api/repository/{repo_id}/rebase")
async def rebase_branch(
    repo_id: str,
    request: RebaseRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Replay a branch's own commits on top of another branch.

    Unlike cherry-pick and revert this moves the branch, so it is all-or-nothing: the
    whole sequence is simulated first and a conflict at any step aborts before anything
    is written. The response carries `previous_head` so the pre-rebase tip can be
    recovered if the result is not what was wanted.
    """
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    # Rewriting a branch is a stronger act than adding a commit to it.
    if not can_manage_repository(db, current_user, repository):
        raise HTTPException(
            status_code=403,
            detail="Rebasing rewrites a branch and requires manage access on this repository.",
        )

    branch = BranchCRUD.get_branch(db, repo_id, request.branch)
    if not branch:
        raise HTTPException(status_code=404, detail=f"Branch '{request.branch}' not found")
    if request.expected_head_commit_id is not None and \
            request.expected_head_commit_id != branch.head_commit_id:
        _raise_head_mismatch(branch.name, request.expected_head_commit_id, branch.head_commit_id)

    try:
        result = rebase(db, repo_id, request.branch, request.onto,
                        current_user.username, dry_run=request.dry_run)
    except HistoryOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    if result["status"] == "conflicts":
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "code": "MERGE_CONFLICT",
                **result,
                "message": (
                    f"Rebase stopped at '{result['failed_subject']}' "
                    f"({result['failed_at'][:8]}); nothing was written."
                ),
            },
        )

    if result["status"] == "rebased":
        ActivityCRUD.create_activity(
            db, current_user.id, "rebase",
            f"Rebased '{request.branch}' onto '{request.onto}': "
            f"{result['replayed']} commit(s), {result['previous_head'][:8]} -> {result['new_head'][:8]}",
            repo_id,
        )

    return {"success": True, **result}
